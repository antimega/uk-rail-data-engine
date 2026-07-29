"""Connection Scan Algorithm - one-to-all earliest arrival.

One pass over the day's connections in departure order answers "how early can I
be at every station", which is exactly the shape of the question being asked.
RAPTOR would work too, but CSA's data structure is a single sorted array and
its best case is precisely the one-to-all query.

Two clocks per station, and conflating them is the classic bug:

* ``arrival`` - the earliest you can *be* at a station. This is the answer.
* ``ready``  - the earliest you can *board* there, which is the arrival plus
  that station's minimum change time. Staying on the same train needs no
  change, so a boarded trip bypasses it entirely.

Times are minutes from midnight on the query date and may exceed 1440 for
overnight travel, matching ``schedule_stop.arrival_minutes``.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field as dc_field

from .network import LINK_MODES, Network, TRAIN_MODE

UNREACHABLE = 1 << 30

#: What a fixed link falls back to when it names no mode of its own. A walk is
#: the honest default: it is the one thing a passenger can always do between two
#: stations, and it is what FLF means by a link with nothing else said about it.
WALK_MODE = LINK_MODES["WALK"]


@dataclass(frozen=True)
class Leg:
    """One part of a journey as a passenger would describe it.

    A leg is a vehicle boarded or a walk taken, not a calling point - the 22
    stations between York and Penzance are three legs. `operator` is None for a
    fixed link, which has no trip and therefore nobody running it.
    """

    board: str
    alight: str
    #: As `rail.engine.network` numbers them: 0 train, 1 walk, 2 bus, 3 ferry,
    #: 4 tube, 5 transfer, 6 metro, 7 tram.
    mode: str
    operator: str | None


@dataclass
class Journey:
    crs: str
    name: str
    arrival: int
    minutes: int


@dataclass
class ScanResult:
    origin: str
    depart_after: int
    #: Earliest arrival per station index; UNREACHABLE where none.
    arrival: list[int]
    network: Network
    #: How each station was reached: the station before it. Walking back gives
    #: the full sequence of calling points, because every connection is one hop
    #: between consecutive public calls - which is what route conditions like
    #: "NOT VIA CHELTENHAM" have to be tested against.
    previous: list[int | None] = dc_field(default_factory=list)
    #: The operator of the connection that reached each station, or None where a
    #: fixed link did. Walking back alongside `previous` gives every operator the
    #: journey used, which is what RGK's TOC conditions are tested against -
    #: route 00085 is "TPE ONLY" and no list of calling points can settle it.
    arrived_by: list[str | None] = dc_field(default_factory=list)
    #: The transport mode of the connection that reached each station, numbered
    #: as RSPS5047 4.12.3 does - 0 a train, 4 the Underground, 2 a bus. RGK
    #: states conditions against these on 95 routes.
    arrived_mode: list[str | None] = dc_field(default_factory=list)
    #: Where each trip was boarded, by station index. Recorded when the scan
    #: first boards it, which is the only moment that knows.
    boarded_at: list[int | None] = dc_field(default_factory=list)
    #: *When* each trip was boarded - the departure time of the connection that
    #: first boarded it. Recorded alongside `boarded_at` for the same reason,
    #: and what turns an arrival into a journey time: `Journey.minutes` counts
    #: from the query, so York to Cardiff is 4h59 from a 09:00 query and 4h23 of
    #: actual travelling, the train leaving at 09:36.
    boarded_time: list[int | None] = dc_field(default_factory=list)
    #: The trip that reached each station, or None where a fixed link did.
    #: Two consecutive legs on the same trip are one train; anything else is a
    #: change. 36 of the current restrictions bar changing at all.
    arrived_trip: list[int | None] = dc_field(default_factory=list)
    #: For a portion joined to a train already ridden, the (trip, station) it
    #: was joined from. A journey has to be traced back through *that*, not
    #: through whatever reached the junction soonest: the sleeper's Aberdeen
    #: portion is joined at Edinburgh by a passenger who has been aboard since
    #: Preston and never set foot on the platform.
    joined_from: list[tuple[int, int] | None] = dc_field(default_factory=list)
    #: Trips boarded through an association rather than by interchanging - a
    #: joined portion of a train already ridden. Two schedules, one physical
    #: train, and no change of trains: counting one would refuse a "no changes"
    #: fare on exactly the through journeys the association exists to describe.
    joined_trip: bytearray = dc_field(default_factory=bytearray)

    def _segments(self, crs: str) -> list[tuple[int | None, list[int]]]:
        """The journey to `crs` as (trip, stations) pairs, origin first.

        One walk, used by everything that asks about the journey rather than
        the arrival - the calling points, the trains, the changes. Keeping them
        on separate walks is how they came to disagree: `path_to` traced the
        train while `changes_to` and `operators_to` still went station to
        station, so the sleeper to Inverness reported one change and two
        operators on a journey that is one train throughout.

        The walk is by train, not by station, because a through service passes
        places whose own best arrival is later than the moment it went by.
        Kingussie's earliest arrival is tomorrow morning's train *from*
        Aviemore, so following Kingussie's history leads away from the journey
        the passenger is on. A trip is None for a fixed link, which belongs to
        no train and contributes its two ends.
        """
        index = self.network.index.get(crs)
        if index is None or self.arrival[index] >= UNREACHABLE:
            return []
        origin_index = self.network.index.get(self.origin)
        found: list[tuple[int | None, list[int]]] = []
        station = index
        seen = {index}
        while station != origin_index:
            trip = self.arrived_trip[station] if self.arrived_trip else None
            boarded = (self.boarded_at[trip]
                       if trip is not None and self.boarded_at else None)
            step = None
            joined = (self.joined_from[trip]
                      if trip is not None and self.joined_from else None)
            if trip is not None and boarded is not None:
                stops = self.network.trip_stops[trip]
                try:
                    start = stops.index(boarded)
                    end = len(stops) - 1 - stops[::-1].index(station)
                except ValueError:
                    start = end = None
                if start is not None and end is not None and end > start:
                    found.append((trip, stops[start:end + 1]))
                    step = boarded
                    if joined is not None:
                        # Joined from another train: continue along *it*, from
                        # where the passenger was already aboard, rather than
                        # asking how the junction could otherwise be reached.
                        from_trip, at_station = joined
                        from_stops = self.network.trip_stops[from_trip]
                        from_boarded = (self.boarded_at[from_trip]
                                        if self.boarded_at else None)
                        if from_boarded is not None and at_station in from_stops:
                            try:
                                a = from_stops.index(from_boarded)
                                b = len(from_stops) - 1 - from_stops[::-1].index(at_station)
                            except ValueError:
                                a = b = None
                            if a is not None and b is not None and b > a:
                                found.append((from_trip, from_stops[a:b + 1]))
                                step = from_boarded
            if step is None:
                previous = self.previous[station] if self.previous else None
                if previous is None:
                    break
                found.append((None, [previous, station]))
                step = previous
            if step in seen:
                break
            seen.add(step)
            station = step
        return list(reversed(found))

    def path_to(self, crs: str) -> list[str]:
        """Stations passed through on the way to `crs`, origin first."""
        walked: list[int] = []
        for _trip, stops in self._segments(crs):
            walked.extend(stops if not walked else stops[1:])
        if walked:
            return [self.network.stations[i] for i in walked]
        index = self.network.index.get(crs)
        return [] if index is None or self.arrival[index] >= UNREACHABLE else [crs]

    def calls_to(self, crs: str) -> list[tuple[str, int, int, bool]]:
        """Every calling point on the way to `crs`, with when the journey was
        there and whether it changed trains: `(station, arrived, departed,
        changed)`, in minutes.

        The times come from the trip rather than from each station's own
        arrival, for the reason `path_to` walks the trip: on a through journey a
        station's earliest arrival is usually the moment some *other* train got
        there first.

        **`changed` is what a restriction band actually needs.** RSPS5045
        4.19.8 field 9 offers "arrivals at, departures from or changing at",
        and field 10 calls the location "a journey origin/destination or via
        location" - so only the `V` marker means a station in the middle, and it
        means *changing* there, not passing through. Three `V` bands are in
        force.

        Read off the same `_segments` walk as `path_to` and `changes_to`, so a
        calling point cannot appear here and not there.
        """
        segments = self._segments(crs)
        calls: list[tuple[str, int, int, bool]] = []
        # Where the journey changes: the first station of every segment after
        # the first. A joined portion is not a change - two schedules, one
        # train - and `changes_to` already knows which those are.
        joined = self.joined_trip
        boundaries: set[str] = set()
        for position, (trip, stops) in enumerate(segments):
            if position == 0 or not stops:
                continue
            if (trip is not None and joined and trip < len(joined)
                    and joined[trip]):
                continue
            boundaries.add(self.network.stations[stops[0]])

        for trip, stops in segments:
            if trip is None:
                # A fixed link. Its ends are timed by the legs either side, and
                # an untimed pair would invite a band judged against a time
                # nobody has.
                continue
            timetable = self.network.trip_stops[trip]
            arrivals = self.network.trip_arrival[trip]
            departures = self.network.trip_departure[trip]
            # `stops` is a contiguous run of this trip, so find where it starts
            # rather than assuming it begins at the trip's own origin.
            start = None
            for at in range(len(timetable) - len(stops) + 1):
                if timetable[at:at + len(stops)] == stops:
                    start = at
                    break
            if start is None:
                continue
            for offset, station in enumerate(stops):
                at = start + offset
                name = self.network.stations[station]
                calls.append((name, arrivals[at], departures[at],
                              name in boundaries))
        return calls

    def calls(self) -> dict[str, list[tuple[str, int, int, bool]]]:
        """Calling points, times and change points for every station reached."""
        return {j.crs: self.calls_to(j.crs) for j in self.reached()}

    def trips_to(self, crs: str) -> list[int]:
        """The trains the journey to `crs` is made of, in order."""
        return [trip for trip, _ in self._segments(crs) if trip is not None]

    def changes_to(self, crs: str) -> int:
        """How many times the journey to `crs` changes train.

        One per boundary between segments - every fixed link is its own, since
        walking between stations means leaving one train and boarding another.
        A portion joined to the train already ridden is **not** a change: two
        schedules, one physical train, which is what the association says.
        """
        segments = self._segments(crs)
        joined = self.joined_trip
        changes = 0
        for position, (trip, _stops) in enumerate(segments):
            if position == 0:
                continue
            if (trip is not None and joined and trip < len(joined)
                    and joined[trip]):
                continue
            changes += 1
        return changes

    def changes(self) -> dict[str, int]:
        """Changes needed to reach every station reached."""
        return {
            journey.crs: self.changes_to(journey.crs) for journey in self.reached()
        }

    def departure_to(self, crs: str) -> int | None:
        """When the journey to `crs` actually starts - the first boarding.

        Read off the same `_segments` walk as `path_to` and `changes_to`, for
        the reason recorded throughout this file: three walks that ought to
        agree eventually do not, and the sleeper reporting one change on a
        journey that is one train throughout is how that was found.

        Where the journey begins with a fixed link there is no boarding to
        report and the answer is `depart_after`: nothing is charged at the
        origin, so the walk can start the moment the query does.

        None where `crs` was never reached.
        """
        segments = self._segments(crs)
        if not segments:
            index = self.network.index.get(crs)
            if index is None or self.arrival[index] >= UNREACHABLE:
                return None
            return self.depart_after
        for trip, _stops in segments:
            if trip is None:
                # A walk off the front of the journey costs nothing at the
                # origin, so the journey starts when the query does.
                return self.depart_after
            when = self.boarded_time[trip] if self.boarded_time else None
            if when is not None:
                return when
        return self.depart_after

    def journey_minutes_to(self, crs: str) -> int | None:
        """How long the journey to `crs` takes, as a passenger experiences it.

        **Not `Journey.minutes`**, which counts from the query and therefore
        includes the wait for the first train. York to Cardiff is 4h23 of
        travelling and 4h59 from a 09:00 query because the train leaves at
        09:36, and a CLI column once labelled the second as the first.

        Both are useful and they answer different questions - "how long does it
        take" against "when can I be there" - so both are exposed and neither is
        allowed to stand in for the other.
        """
        index = self.network.index.get(crs)
        if index is None or self.arrival[index] >= UNREACHABLE:
            return None
        started = self.departure_to(crs)
        return None if started is None else self.arrival[index] - started

    def journey_minutes(self) -> dict[str, int]:
        """Journey time to every station reached, in minutes of travelling."""
        return {
            journey.crs: minutes
            for journey in self.reached()
            if (minutes := self.journey_minutes_to(journey.crs)) is not None
        }

    def paths(self) -> dict[str, list[str]]:
        """The route taken to every station reached."""
        return {
            journey.crs: self.path_to(journey.crs) for journey in self.reached()
        }

    def operators_to(self, crs: str) -> set[str]:
        """Every operator whose train the journey to `crs` uses.

        Empty where the whole journey was fixed links, which carry no operator.
        """
        return {self.network.trip_toc[t] for t in self.trips_to(crs)
                if self.network.trip_toc[t]}

    def operators(self) -> dict[str, set[str]]:
        """The operators used to reach every station reached."""
        return {
            journey.crs: self.operators_to(journey.crs) for journey in self.reached()
        }

    def modes_to(self, crs: str) -> set[str]:
        """Every transport mode the journey to `crs` uses.

        Trains contribute their trip's mode; a fixed link contributes its own,
        which is how an Underground hop between London terminals is told from a
        walk between neighbours.
        """
        found = {self.network.trip_mode[t] for t in self.trips_to(crs)}
        for station in self.path_to(crs):
            at = self.network.index.get(station)
            if at is not None and self.arrived_trip and self.arrived_trip[at] is None:
                link_mode = self.arrived_mode[at] if self.arrived_mode else None
                if link_mode:
                    found.add(link_mode)
        return found

    def legs_to(self, crs: str) -> list[Leg]:
        """The journey to `crs` as the parts a passenger would recognise.

        One `Leg` per vehicle boarded and per walk taken, in order. The three
        kinds the network holds all come out the same shape, which is the point
        of doing this here rather than in each caller:

        * an ordinary train - a trip, an operator, mode 0;
        * **a timetabled bus or ferry - also just a trip.** The Solent
          hovercraft is a `QH` trip of mode 3 and the coach that connects to it
          a `QH` trip of mode 2, no different structurally from an LNER train;
        * **a fixed link - no trip at all.** Its mode is not on a trip, because
          there is none: it is recorded against the station the link *arrives*
          at, so King's Cross to Paddington reads as mode 4, a tube hop, rather
          than as a train.

        `board` is where each part is joined, so the boarding stations of every
        leg but the first are exactly the places the journey changes.
        """
        legs: list[Leg] = []
        for trip, stops in self._segments(crs):
            board = self.network.stations[stops[0]]
            alight = self.network.stations[stops[-1]]
            if trip is None:
                at = self.network.index.get(alight)
                mode = (self.arrived_mode[at]
                        if at is not None and self.arrived_mode else "") or WALK_MODE
                legs.append(Leg(board, alight, mode, None))
            else:
                legs.append(Leg(board, alight, self.network.trip_mode[trip],
                                self.network.trip_toc[trip]))
        return legs

    def modes(self) -> dict[str, set[str]]:
        """The modes used to reach every station reached."""
        return {
            journey.crs: self.modes_to(journey.crs) for journey in self.reached()
        }

    def reached(self) -> list[Journey]:
        """Every station reachable from the origin, soonest first."""
        found = []
        for index, arrived in enumerate(self.arrival):
            if arrived >= UNREACHABLE or self.network.stations[index] == self.origin:
                continue
            found.append(
                Journey(
                    crs=self.network.stations[index],
                    name=self.network.names[index],
                    arrival=arrived,
                    minutes=arrived - self.depart_after,
                )
            )
        found.sort(key=lambda journey: journey.minutes)
        return found


#: Marks 'arrived here by starting here'. Distinct from a fixed link because
#: no interchange is charged at the origin at all.
_ORIGIN = object()


def earliest_arrival(network: Network, origin: str, depart_after: int) -> ScanResult:
    """Scan the day's connections once, from `origin` at `depart_after`."""
    origin_index = network.index.get(origin)
    if origin_index is None:
        raise KeyError(f"unknown station {origin!r}")

    station_count = len(network.stations)
    arrival = [UNREACHABLE] * station_count
    ready = [UNREACHABLE] * station_count
    boarded = bytearray(network.trip_count)
    #: Where each trip was boarded, for tracing a journey back along it.
    boarded_at: list[int | None] = [None] * network.trip_count
    boarded_time: list[int | None] = [None] * network.trip_count
    #: Trips first boarded through an association. The CSA boards each trip once,
    #: so this records how that happened rather than tracking it per connection.
    joined_trip = bytearray(network.trip_count)
    joined_from: list[tuple[int, int] | None] = [None] * network.trip_count
    stride = network.assoc_stride

    arrival[origin_index] = depart_after
    ready[origin_index] = depart_after
    previous: list[int | None] = [None] * station_count
    arrived_by: list[str | None] = [None] * station_count
    arrived_mode: list[str | None] = [None] * station_count
    arrived_trip: list[int | None] = [None] * station_count

    # Local aliases: this loop runs a quarter of a million times.
    from_station = network.from_station
    to_station = network.to_station
    departure = network.departure
    arrive_at = network.arrival
    trip_of = network.trip
    change = network.change
    footpaths = network.footpaths
    operator = network.toc or [None] * len(departure)
    leg_mode = network.mode or [TRAIN_MODE] * len(departure)
    can_board = network.boardable or [True] * len(departure)
    assoc_unlock = network.assoc_unlock or [None] * len(departure)
    assoc_needs = network.assoc_needs or [None] * len(departure)
    toc_change = network.toc_change
    toc_change_stations = network.toc_change_stations
    #: For the 20 stations carrying a TSI rule, when we got there by each means:
    #: the arriving operator, or None for a fixed link or the origin itself.
    #: `ready` cannot answer this, being one number per station derived from the
    #: earliest arrival by any means - and TSI *overrides* the station's own time
    #: rather than competing with it, so the arriving operator has to be known.
    #: At Finsbury Park a change involving Grand Central takes 15 minutes against
    #: the station's 5, and taking the smaller would sell a connection that
    #: cannot be made.
    arrived_on: dict[int, dict[object, int]] = {}

    def note_arrival(station: int, at: int, operator_code: str | None) -> None:
        if station not in toc_change_stations:
            return
        by_toc = arrived_on.setdefault(station, {})
        if at < by_toc.get(operator_code, UNREACHABLE):
            by_toc[operator_code] = at

    def boardable_at(station: int, departing: str | None) -> int:
        """Earliest a passenger may board a `departing` service at `station`.

        Every way of having arrived is charged its own change time: the TSI
        record for that pair of operators where one exists, the station's own
        otherwise. Fixed links never match a TSI record, which is right - 5.12
        is about changing between trains - and at the origin nothing is charged
        because the passenger is not changing off anything.

        Deliberately **not** ``min(ready[station], …)``. `ready` is one number
        per station, derived from the earliest arrival by any means and the
        station's own change time, so mixing it in would quietly restore the
        default for an arrival the TSI record governs. That is invisible where
        the record is shorter and wrong where it is longer: Finsbury Park's
        Grand Central changes are 15 minutes against the station's 5.
        """
        by_toc = arrived_on.get(station)
        if not by_toc:
            return ready[station]
        best = UNREACHABLE
        for arriving, at in by_toc.items():
            if arriving is _ORIGIN:
                cost = 0
            else:
                rule = toc_change.get((station, arriving, departing)) if (
                    arriving is not None and departing is not None) else None
                cost = rule if rule is not None else change[station]
            if at + cost < best:
                best = at + cost
        return best
    #: (trip, station) key -> when we reached that station aboard that trip.
    unlocked: dict[int, int] = {}

    def walk(station: int) -> None:
        # RSPS5046 5.10.1.3 and 5.11.1.3 both say it explicitly: a fixed link's
        # transit time is *summated with* the minimum interchange times at the
        # stations at either end, not used instead of them. So the walk starts
        # at `ready` - the arrival already increased by this station's change
        # time - and boarding at the far end still costs that station's own.
        # Treating the link as door-to-door instead made 4 journeys in 10 from
        # York look 30 minutes quicker than they are.
        at = ready[station]
        if at >= UNREACHABLE:
            return
        # Link opening hours are wall-clock, but `at` keeps counting past 1440
        # on overnight journeys, so compare the time of day.
        time_of_day = at % 1440
        for target, minutes, opens, closes, mode in footpaths[station]:
            if time_of_day < opens or time_of_day > closes:
                continue
            landed = at + minutes
            if landed < arrival[target]:
                arrival[target] = landed
                previous[target] = station
                # A fixed link is nobody's train, but it has a mode of its own.
                arrived_by[target] = None
                arrived_mode[target] = mode
                arrived_trip[target] = None
            # Recorded even when the arrival is not an improvement: a later
            # arrival on a different operator can still board sooner where its
            # TSI record is shorter.
            note_arrival(target, landed, None)
            boardable = landed + change[target]
            if boardable < ready[target]:
                ready[target] = boardable

    note_arrival(origin_index, depart_after, _ORIGIN)

    # At the origin the passenger is not changing off anything, so `ready`
    # holds the query time and no interchange is charged on the way out.
    walk(origin_index)

    # Nothing departing before we do can be boarded.
    for i in range(bisect_left(departure, depart_after), len(departure)):
        trip = trip_of[i]
        if not boarded[trip]:
            source = from_station[i]
            limit = (boardable_at(source, operator[i])
                     if source in toc_change_stations else ready[source])
            # This train may be a joined portion of one we are already on, in
            # which case there is no interchange to make and the station's
            # change time does not apply. Tested *first* and independently of
            # the ordinary check, because both can be true at once - a divide
            # that dwells longer than the interchange allowance - and it is
            # still one physical train. Deciding by whichever test happened to
            # pass would call the sleeper's Inverness portion a change of train.
            required = assoc_needs[i]
            satisfied = None
            if required is not None:
                for k in required:
                    if unlocked.get(k, UNREACHABLE) <= departure[i]:
                        satisfied = k
                        break
            stayed_aboard = satisfied is not None
            # `can_board` is false at a set-down-only call - a public arrival
            # with no public departure. The connection still exists, so anyone
            # already aboard rides through it and it stays on the path; what it
            # will not do is let somebody join here.
            if stayed_aboard or (can_board[i] and departure[i] >= limit):
                boarded[trip] = 1
                boarded_at[trip] = source
                boarded_time[trip] = departure[i]
                joined_trip[trip] = 1 if stayed_aboard else 0
                if satisfied is not None:
                    joined_from[trip] = (satisfied // stride, satisfied % stride)
            else:
                continue

        target = to_station[i]
        landed = arrive_at[i]

        note_arrival(target, landed, operator[i])

        # Record this even when the arrival is not an improvement: staying
        # aboard through a join is about which train we are on, not how early.
        unlock_key = assoc_unlock[i]
        if unlock_key is not None and landed < unlocked.get(unlock_key, UNREACHABLE):
            unlocked[unlock_key] = landed

        if landed >= arrival[target]:
            continue

        arrival[target] = landed
        previous[target] = from_station[i]
        arrived_by[target] = operator[i]
        arrived_mode[target] = leg_mode[i]
        arrived_trip[target] = trip_of[i]
        boardable = landed + change[target]
        if boardable < ready[target]:
            ready[target] = boardable
        walk(target)

    return ScanResult(
        origin=origin,
        depart_after=depart_after,
        arrival=arrival,
        network=network,
        previous=previous,
        arrived_by=arrived_by,
        arrived_mode=arrived_mode,
        arrived_trip=arrived_trip,
        joined_trip=joined_trip,
        joined_from=joined_from,
        boarded_at=boarded_at,
        boarded_time=boarded_time,
    )


def best_over_window(
    network: Network,
    origin: str,
    *,
    first_departure: int,
    last_departure: int,
    step: int = 30,
) -> dict[str, int]:
    """Shortest journey to each station over a window of departure times.

    A single departure time answers "if I leave at 09:00"; sweeping the window
    answers "how well connected is this pair across the day", which is the more
    useful comparison between a Sunday and a weekday.

    **The minimum is over journey time, not over `Journey.minutes`.** Both are
    "the best" in some sense and only one of them is a journey: `minutes` counts
    from each sampled departure, so it is the journey *plus* however long you
    waited for it, and minimising it can never see below the wait. At a station
    served twice a day, every sample in the window includes a long wait, so the
    answer stayed stuck at wait-plus-journey however finely the window was swept.

    Measured from York over 09:00-20:00 in half-hour steps, minimising `minutes`
    instead overstated the journey to **every one of 2,729 stations** - a median
    of 6 minutes and up to 42. Egginton read 1h29 against an actual 0h47.

    Returns journey minutes per station, so the window has no arrival time to
    report: the arrival belongs to one departure and this answers across many.
    """
    best: dict[str, int] = {}
    for depart in range(first_departure, last_departure + 1, step):
        result = earliest_arrival(network, origin, depart)
        for journey in result.reached():
            minutes = result.journey_minutes_to(journey.crs)
            if minutes is None:
                continue
            current = best.get(journey.crs)
            if current is None or minutes < current:
                best[journey.crs] = minutes
    return best
