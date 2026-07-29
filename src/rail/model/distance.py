"""How far it is, by rail and as the crow flies.

Two different distances from two different sources, and conflating them would be
a mistake — they answer different questions and one of them is a *rule*.

**Rail miles (RGD, RSPS5047 4.9).** A station-link is "a section of line between
two adjacent stations over which there is a passenger service", with its distance
in miles to two decimals. 5,874 of them, every one carrying a reverse record, and
none disagreeing with its reverse — so the graph is undirected in practice even
though it is stored both ways. This is not a convenience: **the routeing guide's
own rules are written against it**, and without it two whole sections of the
guide cannot be evaluated at all.

**Crow-flies (`station.easting`/`northing`).** Straight-line distance from OS
grid references. Useful for asking questions of the data — how far is this really,
what is the least direct journey on the network — and **useless as a routeing
rule**, because the guide never mentions it. Kept firmly separate for that
reason. MSN's own grid references are only about a kilometre accurate, so the
supplementary TIPLOC file is what makes this worth computing; see
`acquire/geography.py` for the licensing, which is not the DTD licence.

## The rule this unlocks

RSPS5047 section 7.1 classifies a journey *before* any map is consulted, and the
first two classifications are blanket permissions:

* **7.1.1** — "If there is no change of train at any intermediate location on
  the journey, then the journey is on a through train and is permitted. **No
  further checks are required.**"
* **7.1.2/7.1.3** — "The journey is permitted if it is the shortest distance
  between the origin and destination, or is within a specified margin of the
  length of the shortest route… Currently the allowed margin is **3 miles**. No
  further checks are required."

Neither was implemented, so every journey was being judged by the maps alone —
which is strictly harsher than the guide. Both short-circuits are cheap, and the
second is the reason RGD had to be parsed.

## Measuring the journey the router found

The guide's shortest route is over *adjacent* stations, but a journey's calling
points skip most of them: York to Newcastle calls at Darlington, not at every
station between. So the length of an actual journey is taken as the sum of the
shortest rail distance between each consecutive pair of calling points.

That is an approximation in one direction only — a train could in principle take
a longer way round between two calls than the shortest line — so it can make a
journey look shorter than it is, never longer. Since the rule it feeds is a
*permission* within a 3-mile margin, the error is on the permissive side, and
`journey_miles` returns None the moment any leg cannot be measured rather than
guessing at a total.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

import duckdb

#: RSPS5047 7.1.3, and stated there as a current value rather than a constant of
#: nature — "Currently the allowed margin is 3 miles."
SHORTEST_ROUTE_MARGIN_MILES = 3.0

#: Metres per mile, for turning OS grid distances into the guide's units. Only
#: used for crow-flies figures, never for a routeing decision.
_METRES_PER_MILE = 1609.344


@dataclass
class Distances:
    """Rail distances from RGD, plus optional grid references for crow-flies."""

    #: CRS -> [(neighbour, miles)], both directions present.
    adjacent: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    #: CRS -> (easting, northing) in OS metres, where known.
    grid: dict[str, tuple[int, int]] = field(default_factory=dict)

    @classmethod
    def load(cls, connection: duckdb.DuckDBPyConnection) -> "Distances":
        adjacent: dict[str, list[tuple[str, float]]] = {}
        try:
            links = connection.execute(
                "select from_crs, to_crs, miles from station_link"
            ).fetchall()
        except duckdb.CatalogException:
            links = []
        for source, target, miles in links:
            adjacent.setdefault(source, []).append((target, miles))

        grid: dict[str, tuple[int, int]] = {}
        for crs, easting, northing in connection.execute(
            "select crs, easting, northing from station "
            "where easting is not null and northing is not null"
        ).fetchall():
            grid[crs] = (easting, northing)
        return cls(adjacent=adjacent, grid=grid)

    def __bool__(self) -> bool:
        return bool(self.adjacent)

    # --- rail distance -------------------------------------------------------

    def shortest_miles(self, origin: str, destination: str) -> float | None:
        """Shortest rail distance between two stations, or None if unreachable.

        Plain Dijkstra over 5,874 links. `shortest_from` is the one to use when
        several destinations are wanted, since a single scan answers them all.
        """
        if origin == destination:
            return 0.0
        return self.shortest_from(origin, stop_at=destination).get(destination)

    def shortest_from(
        self, origin: str, stop_at: str | None = None
    ) -> dict[str, float]:
        """Shortest rail distance from `origin` to everywhere it can reach.

        `stop_at` ends the search early when only one destination is wanted; the
        result is then complete only for stations settled before it.
        """
        if origin not in self.adjacent:
            return {}
        best: dict[str, float] = {origin: 0.0}
        queue: list[tuple[float, str]] = [(0.0, origin)]
        settled: set[str] = set()
        while queue:
            distance, station = heapq.heappop(queue)
            if station in settled:
                continue
            settled.add(station)
            if station == stop_at:
                break
            for neighbour, miles in self.adjacent.get(station, ()):
                through = distance + miles
                if through < best.get(neighbour, math.inf):
                    best[neighbour] = through
                    heapq.heappush(queue, (through, neighbour))
        return best

    def journey_miles(self, path: list[str]) -> float | None:
        """How long the journey along `path` is, in rail miles.

        The calling points skip most adjacent stations, so each consecutive pair
        is measured by its own shortest rail distance and the legs are summed.
        None where any leg cannot be measured — a bus or ferry leg, or one of the
        Elizabeth Line stations RSPS5047 6.1.6.2 says carry no station links at
        all — because a total missing a leg would understate the journey and the
        rule it feeds is a permission.
        """
        if len(path) < 2:
            return 0.0
        total = 0.0
        for source, target in zip(path, path[1:]):
            if source == target:
                continue
            leg = self.shortest_miles(source, target)
            if leg is None:
                return None
            total += leg
        return total

    def within_shortest_margin(
        self,
        origin: str,
        destination: str,
        path: list[str],
        margin: float = SHORTEST_ROUTE_MARGIN_MILES,
    ) -> bool | None:
        """RSPS5047 7.1.2: is this journey the shortest route, or near enough?

        True permits the journey outright with no further checks. None means the
        question cannot be answered — an unmeasurable leg, or a pair with no rail
        path — and must not be read as a refusal.

        7.2.4.2 is applied too: a journey calling at the same place twice cannot
        satisfy the shortest-route condition however short it is.
        """
        shortest = self.shortest_miles(origin, destination)
        if shortest is None:
            return None
        stops = [crs for crs in path if crs]
        if len(set(stops)) != len(stops):
            return False
        travelled = self.journey_miles(path)
        if travelled is None:
            return None
        return travelled <= shortest + margin

    # --- straight line, which is not a rule ----------------------------------

    def crow_flies_miles(self, origin: str, destination: str) -> float | None:
        """Straight-line distance between two stations, or None if unplaced.

        OS grid references are planar metres, so this is plain Pythagoras rather
        than a great-circle formula — over Great Britain the projection error is
        far smaller than the question deserves.

        **Not a routeing rule.** The guide never mentions straight-line distance;
        this is here to ask questions of the data, not to permit a journey.
        """
        here, there = self.grid.get(origin), self.grid.get(destination)
        if here is None or there is None:
            return None
        return math.dist(here, there) / _METRES_PER_MILE

    def directness(self, origin: str, destination: str) -> float | None:
        """Rail miles divided by straight-line miles: how indirect the line is.

        1.0 would be a railway built along the straight line. Coastal and valley
        routes run far above it, which is what makes this interesting to query
        rather than useful to decide anything with.
        """
        straight = self.crow_flies_miles(origin, destination)
        rail = self.shortest_miles(origin, destination)
        if not straight or rail is None:
            return None
        return rail / straight
