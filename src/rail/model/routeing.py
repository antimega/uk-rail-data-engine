"""Permitted routes, from the National Routeing Guide.

The guide answers the question the fares feed cannot: given a ticket between two
stations, which physical routes may you actually take? "ANY PERMITTED" means any
route the guide permits, not any route at all.

The model has three layers:

* Every station maps to one or more **routeing points** - 272 of them, against
  2,500-odd stations. Achanalt routes via Dingwall; Alexandra Palace is a
  routeing point in its own right.
* Between a pair of routeing points the guide lists **permitted routes**, and
  each is an ordered *chain* of maps: Alexandra Palace to Crianlarich has routes
  such as AC → CG → EG → FW.
* Each **map** is a set of links between routeing points.

A journey is permitted if the routeing points it passes through form a path
inside the maps of at least one listed route.

Per RSPS5047 the map sequence is ordered and geographically continuous, running
from the start routeing point on the first map to the end on the last, so the
chain is matched in order rather than unioned. Links are directional and the
file carries both directions explicitly where both are valid, so they are kept
directional too.

**Easements are applied**, from RGF. 1,595 of the 2,521 are positive - they
grant a route the maps refuse - and 926 are negative, withdrawing one the maps
allow. Where an easement matches a journey but its applicability turns on
something a list of calling points cannot settle (the ticket, the train, the
passenger), the verdict becomes *unknown* rather than being guessed either way.

**Route conditions** live in ``rail.model.fares``, built from RGK by
:func:`_build_route_rules` here.

:meth:`RouteingGuide.permits` judges one journey; :meth:`routings` inverts it
and lists every route on offer between two stations.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import duckdb

from .distance import Distances

from ..parse.routeing import read_routeing


@dataclass
class Easement:
    """One published exception to what the maps allow.

    ``grants`` is the whole point: a positive easement permits a route the maps
    refuse, a negative one withdraws a route they allow. Not applying easements
    is therefore not the conservative choice it looks like - it is too strict in
    one direction and too lax in the other.

    An easement's applicability can also turn on things a list of calling points
    cannot settle. ``route_codes`` and ``ticket_codes`` are answerable once the
    caller knows which fare it is asking about, and ``tocs`` once it knows which
    trains were caught; ``unsettleable`` covers the rest - the train's UID, and
    the sleeper easements, which depend on who is travelling. Anything still
    open makes the verdict *unknown* rather than being guessed either way.

    ``tocs`` comes from **RGH**, not from RGF's own `D` records. RGF gives eight
    easements an operator and RGH gives 942, one of which is in both - so
    reading only RGF left the guide deciding on eight easements where the feed
    describes 624 of the ones held here.
    """

    ref: str
    grants: bool
    unsettleable: bool
    route_codes: frozenset[str]
    ticket_codes: frozenset[str]
    start_date: dt.date | None
    end_date: dt.date | None
    days: tuple[bool, ...]
    origins: frozenset[str]
    destinations: frozenset[str]
    applicable: frozenset[str]
    via: frozenset[str]
    excluded: frozenset[str]
    #: Stations a doubleback is permitted through, from location modifier 6.
    doubleback: frozenset[str] = frozenset()
    #: Operators this easement is tied to, from RGH. Empty means it applies
    #: whoever ran the trains.
    tocs: frozenset[str] = frozenset()

    def runs_on(self, date: dt.date) -> bool:
        if self.start_date and date < self.start_date:
            return False
        if self.end_date and date > self.end_date:
            return False
        return self.days[date.weekday()]

    def matches(self, origin: str, destination: str, path: list[str]) -> bool:
        """Does this easement speak to this journey?

        Each set of locations is a set of alternatives - an easement listing six
        origins applies to a journey from any of them - so the test is
        membership, not coverage. Exclusions are the one that must hold for all.
        """
        if self.origins and origin not in self.origins:
            return False
        if self.destinations and destination not in self.destinations:
            return False
        travelled = set(path)
        if self.applicable and not (self.applicable & travelled):
            return False
        if self.via and not (self.via & travelled):
            return False
        if self.excluded & travelled:
            return False
        return True

    def settled_for(
        self,
        route_code: str | None,
        ticket_code: str | None,
        operators: set[str] | None = None,
    ) -> bool | None:
        """Does this easement apply to this ticket, on these trains?

        True it applies, False it does not, None the question cannot be
        answered. An easement naming ticket routes applies only on those routes,
        so knowing the fare's route both rules it in and rules it out - and the
        same holds for the operators it names.

        **Not knowing the operators is not a reason to refuse**, the same guard
        the RGK TOC conditions needed: with none supplied the answer is None,
        not False. A caller that has a path but no trains would otherwise have
        every RGH easement silently withdrawn from it.
        """
        if self.unsettleable:
            return None
        if self.route_codes:
            if route_code is None:
                return None
            if route_code not in self.route_codes:
                return False
        if self.ticket_codes:
            if ticket_code is None:
                return None
            if ticket_code not in self.ticket_codes:
                return False
        if self.tocs:
            if not operators:
                return None
            # Any of them: an easement listing four operators speaks to a
            # journey that used any one, exactly as its station lists work.
            if not (self.tocs & operators):
                return False
        return True


#: A permitted route consisting of this map alone means "LONDON" - RSPS5047
#: 4.8.1.3. The map itself carries only six Thameslink links (4.6.1.1), so it
#: cannot be walked from an arbitrary origin to an arbitrary destination; the
#: journey is validated as two halves with a transfer between, which is what
#: `_permits_across_london` does.
LONDON_MAP = "LO"


@dataclass
class Routing:
    """One routing the guide permits between two stations."""

    #: The chain of maps, in order, as RGR gives it.
    maps: tuple[str, ...]
    #: Routeing points from the origin's to the destination's, where the chain
    #: can be walked. Empty for a London routing, which has no single path.
    points: list[str]
    via_london: bool
    start_point: str
    end_point: str

    @property
    def walkable(self) -> bool:
        return bool(self.points)


@dataclass
class RouteingCounts:
    points: int
    station_mappings: int
    routes: int
    links: int
    easements: int


def build_routeing(
    connection: duckdb.DuckDBPyConnection,
    routeing_zip: Path,
) -> RouteingCounts:
    """Load the routeing guide into DuckDB."""
    tables = read_routeing(routeing_zip)
    for name, table in tables.items():
        connection.register(f"_rg_{name}", table)
        connection.execute(f"create or replace table {name} as select * from _rg_{name}")
        connection.unregister(f"_rg_{name}")

    _build_route_rules(connection)

    scalar = lambda sql: connection.execute(sql).fetchone()[0]
    return RouteingCounts(
        points=scalar("select count(*) from routeing_point"),
        station_mappings=scalar("select count(*) from station_routeing_point"),
        routes=scalar("select count(distinct route_id) from permitted_route"),
        links=scalar("select count(*) from routeing_map_link"),
        easements=scalar("select count(*) from easement_text"),
    )


#: Easement types 1 (sleeper) and 2 (disabled passenger) apply to a particular
#: traveller rather than to the journey, so a general query cannot settle them.
GENERAL_EASEMENT_TYPES = ("3", "4")

#: RGF dates are ddmmyyyy, like the fares feed's.
def _easement_date(value: str | None) -> dt.date | None:
    if not value or len(value) != 8 or not value.isdigit():
        return None
    day, month, year = int(value[:2]), int(value[2:4]), int(value[4:])
    try:
        return dt.date(year, month, day)
    except ValueError:
        # 31122999 is the open-ended sentinel and is a real date; anything else
        # impossible is a data error and means "no bound".
        return None


def _load_easements(connection: duckdb.DuckDBPyConnection) -> list[Easement]:
    """Read RGF into objects, or return nothing if the tables are absent."""
    try:
        rows = connection.execute("""
            select e.easement_ref, e.easement_class, e.easement_type,
                   e.start_date, e.end_date,
                   e.monday, e.tuesday, e.wednesday, e.thursday,
                   e.friday, e.saturday, e.sunday,
                   (select list({'ref': d.detail_ref, 'code': d.detail_code})
                    from easement_detail d
                    where d.easement_ref = e.easement_ref) as details,
                   (select list(h.toc) from easement_toc h
                    where h.easement_ref = e.easement_ref) as tocs,
                   list({'crs': l.crs, 'modifier': l.modifier})
            from easement e
            left join easement_location l using (easement_ref)
            group by all
        """).fetchall()
    except duckdb.Error:
        return []

    easements = []
    for (ref, klass, kind, start, end, mo, tu, we, th, fr, sa, su,
         details, tocs, locations) in rows:
        by_modifier: dict[str, set[str]] = {}
        for entry in locations or ():
            if entry and entry.get("crs"):
                by_modifier.setdefault(entry["modifier"], set()).add(entry["crs"])

        # RSPS5047 4.10.4: detail 1 is a train UID, 2 a TOC, 3 a ticket route,
        # 4 a ticket code. The UID describes a particular train, which nothing
        # here can identify; the rest are answerable - the ticket from the fare
        # in hand, the operators from the journey the router found.
        by_detail: dict[str, set[str]] = {}
        for entry in details or ():
            if entry and entry.get("code"):
                by_detail.setdefault(entry["ref"], set()).add(entry["code"])

        easements.append(Easement(
            ref=ref,
            grants=klass == "1",
            # Only the train UID is now unsettleable. Operators used to be here
            # too, on the strength of RGF's eight `D` records - but RGH names
            # 942 easements against 35 operators, and the router already
            # collects the operator of every leg for RGK's own TOC conditions.
            # A question the engine can answer is not an unknown.
            unsettleable=bool(by_detail.get("1"))
                or kind not in GENERAL_EASEMENT_TYPES,
            route_codes=frozenset(by_detail.get("3", ())),
            ticket_codes=frozenset(by_detail.get("4", ())),
            # RGF's own operator records and RGH's, unioned: they overlap on
            # exactly one easement, so both are needed and neither is a subset.
            tocs=frozenset(by_detail.get("2", ()))
                | frozenset(x for x in (tocs or ()) if x),
            start_date=_easement_date(start),
            end_date=_easement_date(end),
            days=(mo, tu, we, th, fr, sa, su),
            origins=frozenset(by_modifier.get("2", ())),
            destinations=frozenset(by_modifier.get("3", ())),
            applicable=frozenset(by_modifier.get("1", ())),
            via=frozenset(by_modifier.get("4", ())),
            excluded=frozenset(by_modifier.get("5", ())),
            # RSPS5047 4.10.3 modifier 6: "the station to which a doubleback is
            # allowed for doubleback easements", with a NOTE promising a
            # matching modifier-4 via record "for backwards compatibility".
            #
            # **That promise does not hold here.** 83 of the 322 doubleback
            # records have no via record for the same station, so a consumer
            # trusting the note loses that station from the easement entirely -
            # easement 701612 permits a doubleback through Wimbledon and names
            # Wimbledon nowhere else. Recorded separately for that reason.
            doubleback=frozenset(by_modifier.get("6", ())),
        ))
    return easements


#: Entry types naming a location, which a list of calling points can be judged
#: against. 'T'/'X' name TOCs and 'L'/'N' transport modes; the router records
#: neither per leg, so a route whose conditions are only those gets no verdict
#: rather than a free pass.
LOCATION_ENTRY_TYPES = ("A", "I", "E")


def _build_route_rules(connection: duckdb.DuckDBPyConnection) -> None:
    """Flatten RGK's route conditions into (route, sense, station) triples.

    ``is_group`` says the CRS stands for one station of a routeing guide group
    and the whole group is meant - so "not via Birmingham" excludes Aston and
    Duddeston too, which is exactly the sort of thing the fares feed's own RTE
    records cannot express. Expanded here so the query is a plain join.

    The left joins matter: a condition that is not a group, or whose group has
    no members listed, still has to keep its own station.
    """
    connection.execute(f"""
        create or replace table route_rule as
        select distinct
               c.route_code,
               c.entry_type,
               coalesce(m2.crs, c.crs) as crs
        from route_condition c
        left join station_group_member m1
          on c.is_group and m1.crs = c.crs
        left join station_group_member m2
          on m2.group_code = m1.group_code
        where c.entry_type in {LOCATION_ENTRY_TYPES}
          and c.crs is not null
    """)

    # Which routes RGK has anything to say about at all, so the fares feed's
    # poorer RTE records are used only where RGK is silent.
    connection.execute("""
        create or replace table route_rgk_covered as
        select distinct route_code from route_rule
        union
        select distinct route_code from route_london where london_marker in ('0', '1')
    """)


class RouteingGuide:
    """Loaded once, then asked about many journeys."""

    def __init__(
        self,
        points: set[str],
        nodes: set[str],
        station_points: dict[str, list[str]],
        station_group: dict[str, str],
        routes: dict[tuple[str, str], list[tuple[str, ...]]],
        map_links: dict[str, set[tuple[str, str]]],
    ) -> None:
        self.points = points
        self.nodes = nodes
        self.station_points = station_points
        self.station_group = station_group
        self.routes = routes
        self.map_links = map_links
        self._reach_cache: dict[tuple[str, ...], dict[str, set[str]]] = {}
        #: Stations from or to which a cross-London transfer is permitted.
        self.cross_london: set[str] = set()
        #: Published exceptions to what the maps allow, from RGF.
        self.easements: list[Easement] = []
        self._easement_index: dict[str, list[Easement]] | None = None
        #: Routeing group code -> its main station, for display.
        self.group_main: dict[str, str] = {}
        #: Rail mileages from RGD, which sections 7.1.2 and 7.2.4 are written
        #: against. Empty until `load` fills it, and the shortest-route rule then
        #: simply never fires rather than guessing.
        self.distances: Distances = Distances()
        #: RGX: a station created since NFM64 -> the older station whose fares
        #: the guide substitutes for it. RSPS5047 4.14.1.1 says the equivalent
        #: is "the NFM station code that should be used when obtaining fares for
        #: Routeing Guide Fare checking".
        self.equivalent_station: dict[str, str] = {}

    @classmethod
    def load(cls, connection: duckdb.DuckDBPyConnection) -> "RouteingGuide":
        points = {
            row[0] for row in connection.execute("select crs from routeing_point").fetchall()
        }

        station_points: dict[str, list[str]] = {}
        for crs, point in connection.execute(
            "select crs, routeing_point from station_routeing_point"
        ).fetchall():
            station_points.setdefault(crs, []).append(point)

        routes: dict[tuple[str, str], list[tuple[str, ...]]] = {}
        for origin, destination, chain in connection.execute("""
            select origin, destination, list(map_code order by seq)
            from permitted_route group by origin, destination, route_id
        """).fetchall():
            routes.setdefault((origin, destination), []).append(tuple(chain))

        map_links: dict[str, set[tuple[str, str]]] = {}
        for map_code, source, target in connection.execute(
            "select map_code, from_crs, to_crs from routeing_map_link"
        ).fetchall():
            # Directional: the file already carries the reverse record wherever
            # the reverse is permitted, so treating them as undirected would
            # invent links the guide does not grant.
            map_links.setdefault(map_code, set()).add((source, target))

        nodes = {
            row[0] for row in connection.execute("select crs from routeing_node").fetchall()
        }
        station_group = dict(connection.execute(
            "select crs, group_code from station_group_member"
        ).fetchall())
        cross_london = {
            row[0] for row in connection.execute(
                "select crs from london_station where cross_london"
            ).fetchall()
        }

        guide = cls(points, nodes, station_points, station_group, routes, map_links)
        guide.cross_london = cross_london
        guide.easements = _load_easements(connection)
        # RGD. Without it the shortest-route classification simply never fires,
        # which is the behaviour before it was parsed.
        guide.distances = Distances.load(connection)
        try:
            guide.equivalent_station = dict(connection.execute("""
                select crs, equivalent_crs from routeing_new_station
                where current_date between start_date and end_date
            """).fetchall())
        except duckdb.CatalogException:
            guide.equivalent_station = {}
        guide.group_main = dict(connection.execute(
            "select group_code, crs from routeing_group"
        ).fetchall())
        return guide

    def _for_fares(self, crs: str) -> str:
        """The station code the guide checks fares against.

        RSPS5047 4.14: a station built since NFM64 has no fares of its own in
        the guide's world, and the New Stations file names the older station to
        use instead. Only applied where the station has no routeing point of its
        own, because the guide's own mapping is the better answer where it
        exists - 25 of the 30 stations new enough to be in RGX already have one.
        """
        if crs in self.station_points or crs in self.points:
            return crs
        return self.equivalent_station.get(crs, crs)

    def points_for(self, crs: str) -> list[str]:
        """The routeing points a station routes via."""
        mapped = self.station_points.get(crs)
        if mapped:
            return mapped
        node = self.node_of(crs)
        return [node] if node in self.points else []

    def node_of(self, crs: str) -> str:
        """A station's identity on the maps: its group, if it is in one.

        Aston is not a node; Birmingham Group is, and Aston is inside it.
        """
        return self.station_group.get(crs, crs)

    def routings(self, origin: str, destination: str) -> list[Routing]:
        """Every routing the guide permits between two stations.

        The inverse of :meth:`permits`: instead of judging one journey, list the
        routes on offer. Each is a chain of maps, walked into the routeing
        points it passes through - York to Penzance gives one via Birmingham and
        Bristol, one via Manchester and Crewe, and the London route.
        """
        found: list[Routing] = []
        seen: set[tuple[str, ...]] = set()
        for start in self.points_for(origin):
            for end in self.points_for(destination):
                for chain in self.routes.get((start, end), ()):
                    if chain in seen:
                        continue
                    seen.add(chain)
                    via_london = tuple(chain) == (LONDON_MAP,)
                    found.append(Routing(
                        maps=tuple(chain),
                        points=[] if via_london else self._walk(chain, start, end),
                        via_london=via_london,
                        start_point=start,
                        end_point=end,
                    ))
        # The shortest path first, then London, then anything unwalkable.
        found.sort(key=lambda r: (not r.walkable, r.via_london, len(r.points)))
        return found

    def _walk(self, chain: tuple[str, ...], start: str, end: str) -> list[str]:
        """The shortest node sequence across a chain of maps.

        Breadth-first rather than every simple path: the busiest maps carry 180
        links and enumerating all of them would not terminate usefully.
        """
        adjacency: dict[str, set[str]] = {}
        for code in chain:
            for source, target in self.map_links.get(code, ()):
                adjacency.setdefault(source, set()).add(target)

        came_from: dict[str, str | None] = {start: None}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            if node == end:
                break
            for step in adjacency.get(node, ()):
                if step not in came_from:
                    came_from[step] = node
                    queue.append(step)
        if end not in came_from:
            return []
        route, node = [], end
        while node is not None:
            route.append(node)
            node = came_from[node]
        return list(reversed(route))

    def main_station(self, code: str) -> str:
        """A routeing point as somewhere a passenger has heard of.

        Routeing points may be group codes, and `G02` means nothing to anyone;
        RGG names Birmingham New Street as the group's main station.
        """
        return self.group_main.get(code, code)

    def permits(
        self,
        origin: str,
        destination: str,
        path: list[str],
        date: dt.date | None = None,
        route_code: str | None = None,
        ticket_code: str | None = None,
        changes: int | None = None,
        operators: set[str] | None = None,
    ) -> bool | None:
        """Is `path` a permitted route from `origin` to `destination`?

        None means the guide has nothing to say - an unknown station, a pair it
        does not list, or an easement whose applicability cannot be settled -
        and must not be read as a refusal.

        Easements are only consulted when a `date` is given, since every one of
        them carries validity dates and days of the week.

        `route_code` and `ticket_code` name the fare being asked about. Most
        easements that would otherwise be left open are open only because they
        say "customers with tickets routed X", so supplying the route of the
        fare in hand settles them - in both directions, since an easement naming
        ticket routes does not apply to a ticket routed otherwise.

        `changes` is how many times the journey changes train. **RSPS5047 7.1.1
        makes a through train permitted outright**, before any map is consulted,
        and says so in as many words: "No further checks are required."

        `operators` are the operators the journey actually used, which is what
        an RGH easement is tied to. Omitting them leaves those easements open
        rather than refusing them.
        """
        # Section 7.1 classifies the journey before the maps are reached, and
        # its first two classifications are blanket permissions. Judging every
        # journey by the maps alone - which is what this did - is strictly
        # harsher than the guide.
        blanket = self._permitted_outright(origin, destination, path, changes)
        if blanket:
            return True

        verdict = self._permits_by_map(origin, destination, path)
        if date is None or not self.easements:
            return verdict
        return self._apply_easements(
            verdict, origin, destination, path, date, route_code, ticket_code,
            operators,
        )

    def _permitted_outright(
        self,
        origin: str,
        destination: str,
        path: list[str],
        changes: int | None,
    ) -> bool:
        """RSPS5047 7.1.1 and 7.1.2 - permitted with no further checks.

        Both are stated as classifications of the journey rather than as
        properties of a route, and both end "No further checks are required":

        * 7.1.1, no change of train at any intermediate location;
        * 7.1.2/7.1.3, the shortest route by rail, or within 3 miles of it.

        Deliberately returns False rather than None where it cannot tell, since
        the caller falls through to the maps and easements - this only ever adds
        permissions, so being unable to answer costs nothing.
        """
        if changes == 0:
            return True
        if not self.distances:
            return False
        return self.distances.within_shortest_margin(
            origin, destination, path) is True

    def _apply_easements(
        self,
        verdict: bool | None,
        origin: str,
        destination: str,
        path: list[str],
        date: dt.date,
        route_code: str | None = None,
        ticket_code: str | None = None,
        operators: set[str] | None = None,
    ) -> bool | None:
        """Let the published exceptions override what the maps concluded.

        A negative easement beats a positive one where both match: the guide
        does not say which wins, and refusing is the answer that cannot sell
        someone an invalid ticket.

        A conditional easement only unsettles the verdict when it could actually
        change it. A positive easement grants permission, so it tells you
        nothing you did not already know about a journey the maps permit; a
        negative one withdraws permission, so it cannot make a refusal worse.
        Treating every matching conditional easement as doubt turned 1,059 of
        York's 2,828 destinations from permitted to unknown for no reason -
        mostly on the strength of a TransPennine engineering diversion that only
        applies to particular ticket routes.
        """
        granted = refused = False
        maybe_granted = maybe_refused = False
        for easement in self._easements_touching(origin, destination, path):
            if not easement.runs_on(date):
                continue
            if not easement.matches(origin, destination, path):
                continue
            applies = easement.settled_for(route_code, ticket_code, operators)
            if applies is False:
                continue
            if easement.grants:
                if applies is None:
                    maybe_granted = True
                else:
                    granted = True
            elif applies is None:
                maybe_refused = True
            else:
                refused = True

        if refused:
            return False
        if granted:
            return True
        if maybe_refused and verdict is not False:
            return None
        if maybe_granted and verdict is not True:
            return None
        return verdict

    def _easements_touching(
        self, origin: str, destination: str, path: list[str]
    ) -> list[Easement]:
        """The easements that could possibly match, by station.

        Every easement names at least one station under origin, destination,
        applicable, via or doubleback - none in RGF names none - and each of
        those sets that is non-empty has to intersect the journey for the
        easement to match. So
        an easement mentioning no station on this journey cannot apply, and
        checking all 2,521 against every destination is 7 million comparisons
        for nothing: the sweep from York went from 0.06 s to 0.88 s before this
        index existed.
        """
        if self._easement_index is None:
            index: dict[str, list[Easement]] = {}
            for easement in self.easements:
                # Doubleback targets count as named stations. The spec promises
                # a matching `via` record for each, but 83 of the 322 do not
                # have one - easement 701612 permits a doubleback through
                # Wimbledon and names Wimbledon nowhere else - so leaving them
                # out drops the easement from every journey it governs.
                named = (easement.origins | easement.destinations
                         | easement.applicable | easement.via
                         | easement.doubleback)
                for crs in named:
                    index.setdefault(crs, []).append(easement)
            self._easement_index = index

        index = self._easement_index
        seen: dict[int, Easement] = {}
        for crs in {origin, destination, *path}:
            for easement in index.get(crs, ()):
                seen[id(easement)] = easement
        return list(seen.values())

    def _permits_by_map(
        self, origin: str, destination: str, path: list[str]
    ) -> bool | None:
        """The verdict from the maps alone, before any easement applies."""
        # A station too new for the guide's own data routes as the station RGX
        # names in its place. Today this changes no verdict - the five stations
        # with no routeing point are also absent from RGX, being newer still -
        # but a station opening between two routeing-feed releases lands exactly
        # in that gap, which is what the file is for.
        origin_points = self.points_for(self._for_fares(origin))
        destination_points = self.points_for(self._for_fares(destination))
        if not origin_points or not destination_points:
            return None

        # The journey reduced to the nodes it passes through, each station
        # standing for its group where it has one.
        travelled: list[str] = []
        for crs in path:
            node = self.node_of(crs)
            if node in self.nodes and (not travelled or travelled[-1] != node):
                travelled.append(node)


        listed = any(
            self.routes.get((start, end))
            for start in origin_points for end in destination_points
        )

        for start in origin_points:
            for end in destination_points:
                for chain in self.routes.get((start, end), ()):
                    if self._chain_covers(chain, [start, *travelled, end]):
                        return True

        # A journey through London is not listed as one route; it is validated
        # as two halves, so this has to be tried before concluding anything.
        if self._permits_across_london(origin, destination, path):
            return True

        # Nothing listed for this pair and no London split available: the guide
        # has no opinion, which is not the same as refusing.
        return False if listed else None

    def _permits_across_london(
        self, origin: str, destination: str, path: list[str]
    ) -> bool:
        """Validate a journey through London as two halves plus a transfer.

        The guide handles cross-London separately: you travel to one London
        station, transfer, and travel on from another. Each half is checked
        against the guide in its own right.
        """
        entries = [
            i for i, crs in enumerate(path) if crs in self.cross_london
        ]
        for i in entries:
            for j in entries:
                if j <= i:
                    continue
                # The map verdict for each half, not the full one: easements are
                # applied once, to the journey as a whole.
                if (
                    self._permits_by_map(origin, path[i], path[: i + 1]) is not False
                    and self._permits_by_map(path[j], destination, path[j:]) is not False
                ):
                    return True
        return False

    def _reachable_from(self, chain: tuple[str, ...]) -> dict[str, set[str]]:
        """Which nodes each node can reach across the chain's maps."""
        cached = self._reach_cache.get(chain)
        if cached is not None:
            return cached

        adjacency: dict[str, set[str]] = {}
        for map_code in chain:
            for source, target in self.map_links.get(map_code, ()):
                adjacency.setdefault(source, set()).add(target)

        reachable: dict[str, set[str]] = {}
        for origin in adjacency:
            seen = {origin}
            queue = [origin]
            while queue:
                node = queue.pop()
                for neighbour in adjacency.get(node, ()):
                    if neighbour not in seen:
                        seen.add(neighbour)
                        queue.append(neighbour)
            reachable[origin] = seen

        self._reach_cache[chain] = reachable
        return reachable

    def _chain_covers(self, chain: tuple[str, ...], sequence: list[str]) -> bool:
        """Can the journey be traced across this chain of maps?

        Not by direct adjacency: a train passes through map nodes without
        calling there, so the nodes it *does* call at are a subsequence of a
        path through the map. On map YA, York links to Northallerton rather than
        Darlington, yet York to Newcastle via Darlington is plainly permitted.
        Each observed node must therefore be *reachable* from the previous one.
        """
        reachable = self._reachable_from(chain)
        ordered = [
            node for i, node in enumerate(sequence)
            if i == 0 or node != sequence[i - 1]
        ]
        for current, following in zip(ordered, ordered[1:]):
            if following not in reachable.get(current, ()):
                return False
        return True
