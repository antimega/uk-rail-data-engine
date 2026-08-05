"""Loading one service date into a compact connection set.

A *connection* is one train travelling between two consecutive public calls.
The Connection Scan Algorithm needs them as flat arrays sorted by departure
time, so everything is loaded into plain Python lists - list indexing beats
numpy scalar indexing inside a tight sequential loop, and the scan is
inherently sequential.

Transfers come from three places:

* the station's own minimum change time (MSN),
* TOC-specific interchange times where an operator needs longer (TSI),
* fixed links between *different* stations - the Underground hop from Euston to
  King's Cross, or a walk between neighbouring stations (FLF and ALF).

Without fixed links, one-to-all routing silently misses every journey that has
to cross London.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field as dc_field
from pathlib import Path

import duckdb

#: Used when MSN gives no minimum change time for a station.
DEFAULT_CHANGE_MINUTES = 5

#: Fixed links are only usable while the mode runs; ALF gives a window, FLF
#: does not, so an FLF link is treated as always available. Minutes, like every
#: other time in the engine.
ALL_DAY = (0, 24 * 60)

#: RSPS5047 4.12.3 field 6 numbers the transport modes, and RGK states route
#: conditions against them - 95 routes do, e.g. "must include an Underground
#: leg". ALF and FLF name the same modes in words, so this is the join between
#: them. A train is mode 0.
TRAIN_MODE = "0"
LINK_MODES = {
    "WALK": "1", "BUS": "2", "FERRY": "3", "TUBE": "4",
    "TRANSFER": "5", "METRO": "6", "TRAM": "7",
}

#: Not every service in the timetable is a train. CIF train status B and 5 are
#: buses, S and 4 are ships - the Wightlink catamaran from Portsmouth Harbour to
#: Ryde Pier Head is a scheduled service, not a fixed link, and calling it a
#: train made every Isle of Wight fare fail its "must include a ferry" condition
#: for the wrong reason. 9,447 bus and 1,269 ship schedules run in this feed.
STATUS_MODES = {"B": "2", "5": "2", "S": "3", "4": "3"}


@dataclass
class Network:
    date: dt.date
    #: CRS code per station index, and the reverse lookup.
    stations: list[str]
    index: dict[str, int]
    names: list[str]

    #: Connections, sorted by departure time.
    from_station: list[int]
    to_station: list[int]
    departure: list[int]
    arrival: list[int]
    trip: list[int]

    #: Minutes needed to change trains, per station.
    change: list[int]
    #: Fixed links out of each station:
    #: (destination, minutes, open from, open until, mode code).
    footpaths: list[list[tuple[int, int, int, int, str]]]
    trip_count: int

    #: Association keys pack (trip, station) as `trip * assoc_stride + station`,
    #: so the scan can decode which train a joined portion was joined *from*.
    assoc_stride: int = 1

    #: Whether a passenger may join the train at this connection's origin.
    #: False at a set-down-only call, which carries a public arrival and no
    #: public departure.
    boardable: list[bool] = dc_field(default_factory=list)

    #: Operator and transport mode per trip. Per *trip*, not per station: a
    #: station's own `arrived_by` names whichever service reached it soonest,
    #: which on a through journey is often not the train the passenger is on.
    trip_toc: list[str | None] = dc_field(default_factory=list)
    trip_mode: list[str] = dc_field(default_factory=list)
    #: Six-character CIF Train UID per trip. Unlike the internal schedule and
    #: trip indexes, this is the identity used by fares SR/SD/SQ records.
    trip_uid: list[str] = dc_field(default_factory=list)

    #: The ordered station sequence of each trip, so a journey can be traced
    #: back along the train it was actually on. Walking back station by station
    #: is not enough: a through train passes stations whose own best arrival is
    #: later than the moment it went by, and following *their* history leads
    #: somewhere the passenger never was.
    trip_stops: list[list[int]] = dc_field(default_factory=list)

    #: When each trip reaches and leaves each of those stops, parallel to
    #: `trip_stops`. Needed because a restriction band may name a station the
    #: journey merely passes through - 32,206 of the 33,216 current bands name
    #: one - and it is judged on the time *this* journey was there, which a
    #: station's own earliest arrival does not give.
    trip_arrival: list[list[int]] = dc_field(default_factory=list)
    trip_departure: list[list[int]] = dc_field(default_factory=list)
    #: Actual public fields, without the timing fallbacks above. SQ records
    #: distinguish arrivals from departures, so a trip origin must not acquire
    #: a synthetic arrival merely because its departure times the connection.
    trip_call_arrival: list[list[int | None]] = dc_field(default_factory=list)
    trip_call_departure: list[list[int | None]] = dc_field(default_factory=list)

    #: RSPS5046 5.12: the minimum interchange time when changing *between two
    #: particular operators* at a station, which overrides the station's own.
    #: Keyed (station index, arriving TOC, departing TOC). 35 records over 20
    #: stations, and **directional** - 5.12.1.2 says so in as many words: "SE >
    #: SN does not automatically equate to SN > SE". No day, date or time
    #: qualification either (5.12.1.3): every record applies 24/7.
    toc_change: dict[tuple[int, str, str], int] = dc_field(default_factory=dict)
    #: The stations any of those records mention, so the scan can skip the
    #: machinery entirely at the other 3,089.
    toc_change_stations: frozenset[int] = frozenset()

    #: Association support, per connection index. `assoc_unlock[i]` is the key to
    #: record on arriving, when arriving there aboard this trip lets a passenger
    #: stay on for a joined portion. `assoc_needs[i]` holds the keys that would
    #: permit boarding this connection with no interchange, for exactly the
    #: station it departs from. Both are mostly None, and are plain list lookups
    #: so the scan stays fast.
    assoc_unlock: list[int | None] = dc_field(default_factory=list)
    assoc_needs: list[tuple[int, ...] | None] = dc_field(default_factory=list)

    #: Operating company per connection, from the schedule's BX record. None
    #: where the feed gives none, and for fixed links, which have no operator.
    #: RGK states route conditions against these: 00085 "TPE ONLY" is `T:TP`
    #: plus `X` on 25 others.
    toc: list[str | None] = dc_field(default_factory=list)
    #: Transport mode per connection, numbered as RSPS5047 does. Mostly a train,
    #: but the timetable also carries buses and ships.
    mode: list[str] = dc_field(default_factory=list)

    @property
    def connection_count(self) -> int:
        return len(self.departure)


_CONNECTION_SQL = """
with running as (
    -- The operator comes from the BX record and is what RGK's TOC conditions
    -- are tested against: route 00085 is "TPE ONLY", meaning `T:TP` and `X` on
    -- everyone else.
    --
    -- **Two days are loaded, not one.** A schedule's own overnight wrap is
    -- already handled by `day_offset`, but a journey continuing onto a service
    -- whose *schedule* is dated the next day was invisible: the Caledonian
    -- Sleeper divides at Carstairs and its Fort William portion is a separate
    -- schedule dated D+1, so Euston at 21:00 could not reach Fort William,
    -- Aberdeen, Glasgow or Edinburgh at all. The next day's services are shifted
    -- by 1440 minutes so the clock keeps running past midnight.
    select sd.schedule_id, s.train_uid, s.atoc_code, s.train_status,
           case when sd.date = $date then 0 else 1440 end as day_shift
    from service_date sd
    join train_schedule s using (schedule_id)
    where sd.date in ($date, $date + 1) and s.is_passenger
),
calls as (
    select ss.schedule_id, running.train_uid, running.atoc_code,
           running.train_status, ss.seq, ss.crs,
           ss.arrival_minutes + running.day_shift as arrival_minutes,
           ss.departure_minutes + running.day_shift as departure_minutes,
           running.day_shift
    from schedule_stop ss
    join running using (schedule_id)
    where ss.is_public and ss.crs is not null
)
select schedule_id,
       day_shift,
       train_uid,
       atoc_code,
       train_status,
       crs as from_crs,
       lead(crs) over w as to_crs,
       -- The mirror of the `arr` coalesce below, and nearly twice as common:
       -- 19,589 public calls across 8,659 schedules carry an *arrival* and no
       -- departure. Requiring the departure severs the train there, so the
       -- northbound sleeper lost Stirling, Dunblane, Gleneagles, Perth,
       -- Dunkeld, Pitlochry, Blair Atholl, Dalwhinnie and Newtonmore from its
       -- calling points. The arrival time was still right - a trip is boarded
       -- once and every connection of it relaxes - but the *path* silently
       -- bridged the gap, and paths are what the route conditions and the
       -- routeing guide are judged on.
       coalesce(departure_minutes, arrival_minutes) as dep,
       -- A public arrival with no public departure is a set-down stop: you may
       -- alight there and not board. Riding through is fine; boarding is not.
       departure_minutes is not null as boardable,
       arrival_minutes as from_call_arrival,
       departure_minutes as from_call_departure,
       lead(arrival_minutes) over w as to_call_arrival,
       lead(departure_minutes) over w as to_call_departure,
       -- A public call may carry a departure and no arrival: 10,144 mid-journey
       -- stops across 7,492 schedules do. Requiring the arrival severed the
       -- train there and made everything beyond it unreachable on that service -
       -- the 12:03 Paddington to Penzance became two trains because Exeter St
       -- Davids has no public arrival time, so York to Penzance came out 42
       -- minutes late. The departure is a sound upper bound on being there: the
       -- train cannot leave before the passenger has arrived.
       coalesce(lead(arrival_minutes) over w, lead(departure_minutes) over w) as arr
from calls
-- Partition by the day too: the same schedule_id runs on both days, and merging
-- them would invent a connection from tonight's last call to tomorrow's first.
window w as (partition by schedule_id, day_shift order by seq)
qualify to_crs is not null and dep is not null and arr is not null and arr >= dep
order by dep, schedule_id
"""


def _discover_timetable_dir() -> Path | None:
    """The latest ingested timetable snapshot, or None if there is not one.

    Found rather than required, because two things that materially change the
    answer live in that Parquet rather than in the database: the fixed links
    (`ALF`/`FLF`) and the operator-specific interchange times (`TSI`). Leaving
    them out does not fail - it quietly returns a smaller, slower network. From
    York on a weekday it loses **172 of 2,901 destinations**, and every journey
    that would have changed between two named operators is mistimed.

    A default that is silently the worse answer is a trap, so the argument is
    optional and its absence means "work it out", not "do without".
    """
    from ..acquire import Feed, SnapshotStore
    from ..config import load_config

    try:
        config = load_config()
        manifest = SnapshotStore(config.raw_dir).latest(Feed.TIMETABLE)
        if manifest is None:
            return None
        path = config.parquet_dir / Feed.TIMETABLE.value / Path(manifest.filename).stem
        return path if path.exists() else None
    except Exception:  # noqa: BLE001 - discovery is a convenience, never a failure
        return None


def load_network(
    connection: duckdb.DuckDBPyConnection,
    date: dt.date,
    *,
    timetable_dir: Path | None = None,
) -> Network:
    """Build the connection set for one date.

    `timetable_dir` is discovered when not given - see `_discover_timetable_dir`
    for why the default must not be the network without fixed links. Pass it
    explicitly to pin a particular snapshot.
    """
    if timetable_dir is None:
        timetable_dir = _discover_timetable_dir()
    rows = connection.execute(_CONNECTION_SQL, {"date": date}).fetchall()
    if not rows:
        raise RuntimeError(
            f"no services on {date} - is it inside the built horizon? "
            "See `rail build --horizon`."
        )

    station_rows = connection.execute(
        "select crs, name, interchange_minutes from station order by crs"
    ).fetchall()

    index: dict[str, int] = {}
    stations: list[str] = []
    names: list[str] = []
    change: list[int] = []
    for crs, name, interchange in station_rows:
        index[crs] = len(stations)
        stations.append(crs)
        names.append(name)
        change.append(interchange if interchange is not None else DEFAULT_CHANGE_MINUTES)

    def station_id(crs: str) -> int:
        """Stations can appear in the timetable without an MSN entry."""
        existing = index.get(crs)
        if existing is not None:
            return existing
        index[crs] = len(stations)
        stations.append(crs)
        names.append(crs)
        change.append(DEFAULT_CHANGE_MINUTES)
        return index[crs]

    # Keyed on (schedule, day) rather than schedule alone: the same schedule
    # runs on both loaded days and they are two different physical trains, so
    # sharing a trip index would let a passenger "stay aboard" from tonight's
    # service onto tomorrow's.
    trip_ids: dict[tuple[int, int], int] = {}
    from_station: list[int] = []
    to_station: list[int] = []
    departure: list[int] = []
    arrival: list[int] = []
    trip: list[int] = []
    toc: list[str | None] = []
    mode: list[str] = []

    boardable: list[bool] = []
    from_call_arrival: list[int | None] = []
    from_call_departure: list[int | None] = []
    to_call_arrival: list[int | None] = []
    to_call_departure: list[int | None] = []
    trip_uids: list[str] = []
    for (schedule_id, day_shift, train_uid, atoc_code, status, from_crs, to_crs, dep,
         can_board, from_arrive, from_depart, to_arrive, to_depart, arr) in rows:
        boardable.append(bool(can_board))
        from_call_arrival.append(
            None if from_arrive is None else int(from_arrive)
        )
        from_call_departure.append(
            None if from_depart is None else int(from_depart)
        )
        to_call_arrival.append(None if to_arrive is None else int(to_arrive))
        to_call_departure.append(None if to_depart is None else int(to_depart))
        trip_index = trip_ids.setdefault((schedule_id, day_shift), len(trip_ids))
        if trip_index == len(trip_uids):
            trip_uids.append(train_uid)
        from_station.append(station_id(from_crs))
        to_station.append(station_id(to_crs))
        departure.append(int(dep))
        arrival.append(int(arr))
        trip.append(trip_index)
        toc.append(atoc_code)
        mode.append(STATUS_MODES.get(status, TRAIN_MODE))

    # Loading footpaths can introduce stations the timetable never called at,
    # so materialise the per-station lists only once it is done.
    links = _load_footpaths(connection, date, station_id, timetable_dir)
    footpaths: list[list[tuple[int, int, int, int, str]]] = [[] for _ in stations]
    for source, entries in links.items():
        footpaths[source] = entries

    assoc_unlock, assoc_needs = _load_associations(
        connection, date, trip_ids, index, trip, from_station, to_station
    )
    toc_change = _load_toc_interchange(connection, index, timetable_dir)

    # Connections arrive ordered by departure, so a trip's own stops are
    # scattered through the array; collect them once here rather than per query.
    trip_stops: list[list[int]] = [[] for _ in range(len(trip_ids))]
    # The clock at each of those stops: when the train reaches it and when it
    # leaves. A restriction band naming a station mid-journey is judged against
    # *these* times, and they cannot be recovered afterwards - a station's own
    # earliest arrival is whenever the first train gets there, which on a
    # through journey is often not the moment this one went by.
    trip_arrival: list[list[int]] = [[] for _ in range(len(trip_ids))]
    trip_departure: list[list[int]] = [[] for _ in range(len(trip_ids))]
    trip_call_arrival: list[list[int | None]] = [
        [] for _ in range(len(trip_ids))
    ]
    trip_call_departure: list[list[int | None]] = [
        [] for _ in range(len(trip_ids))
    ]
    trip_toc: list[str | None] = [None] * len(trip_ids)
    trip_mode: list[str] = [TRAIN_MODE] * len(trip_ids)
    # One pass. The connection array is sorted by departure and a train only
    # moves forward, so a trip's own connections are met in order - which means
    # the stop last appended is always the one this connection leaves.
    for position, trip_index in enumerate(trip):
        stops = trip_stops[trip_index]
        if not stops:
            stops.append(from_station[position])
            # Nothing arrives at the first stop; the train starts there.
            trip_arrival[trip_index].append(departure[position])
            trip_departure[trip_index].append(departure[position])
            trip_call_arrival[trip_index].append(from_call_arrival[position])
            trip_call_departure[trip_index].append(
                from_call_departure[position]
            )
            trip_toc[trip_index] = toc[position]
            trip_mode[trip_index] = mode[position]
        else:
            # This connection leaves the stop appended last, so its departure
            # belongs there. Assigning by position rather than looking the
            # station up: a circular service calls at one station twice, and
            # `index` would find the wrong call.
            trip_departure[trip_index][-1] = departure[position]
        stops.append(to_station[position])
        trip_arrival[trip_index].append(arrival[position])
        # Provisional: overwritten when the next connection leaves here, and
        # left as the arrival at the last stop, where the train goes no further.
        trip_departure[trip_index].append(arrival[position])
        trip_call_arrival[trip_index].append(to_call_arrival[position])
        trip_call_departure[trip_index].append(to_call_departure[position])

    return Network(
        date=date,
        stations=stations,
        index=index,
        names=names,
        from_station=from_station,
        to_station=to_station,
        departure=departure,
        arrival=arrival,
        trip=trip,
        toc=toc,
        mode=mode,
        change=change,
        footpaths=footpaths,
        trip_count=len(trip_ids),
        assoc_unlock=assoc_unlock,
        assoc_needs=assoc_needs,
        toc_change=toc_change,
        toc_change_stations=frozenset(station for station, _, _ in toc_change),
        trip_stops=trip_stops,
        trip_arrival=trip_arrival,
        trip_departure=trip_departure,
        trip_call_arrival=trip_call_arrival,
        trip_call_departure=trip_call_departure,
        boardable=boardable,
        assoc_stride=len(index) + 1,
        trip_toc=trip_toc,
        trip_mode=trip_mode,
        trip_uid=trip_uids,
    )


def _load_toc_interchange(
    connection: duckdb.DuckDBPyConnection,
    index: dict[str, int],
    timetable_dir: Path | None,
) -> dict[tuple[int, str, str], int]:
    """TSI: the change time between two named operators at one station.

    RSPS5046 5.12.1.1 - "This data overrides the minimum interchange time at a
    station for a journey when changing from one TOC to another." It overrides
    rather than competes, which is the part that matters: at Finsbury Park a
    change involving Grand Central takes 15 minutes against the station's own 5,
    so taking the smaller of the two would sell a connection that cannot be made.
    """
    if timetable_dir is None:
        return {}
    tsi = timetable_dir / "toc_interchange.parquet"
    if not tsi.exists():
        return {}
    return {
        (index[crs], from_toc, to_toc): int(minutes)
        for crs, from_toc, to_toc, minutes in connection.execute(
            f"select crs, from_toc, to_toc, minutes "
            f"from read_parquet('{tsi.as_posix()}') where minutes is not null"
        ).fetchall()
        if crs in index
    }


def _load_associations(
    connection: duckdb.DuckDBPyConnection,
    date: dt.date,
    trip_ids: dict[tuple[int, int], int],
    index: dict[str, int],
    trip: list[int],
    from_station: list[int],
    to_station: list[int],
) -> tuple[list[int | None], list[tuple[int, ...] | None]]:
    """Turn the day's joins and splits into per-connection lookups.

    Each link says two schedules are the same physical train from a given
    station, so a passenger may move between them there without an interchange.
    The permission is tied to that one station: about one association in seven
    has the partner calling elsewhere too, and a train-wide flag would let a
    passenger board it at a station they have never reached.
    """
    empty: tuple[list[int | None], list[tuple[int, ...] | None]] = (
        [None] * len(trip), [None] * len(trip)
    )
    try:
        # Links whose base runs on either loaded day. `assoc_day_offset` says
        # where the partner sits: 0 the same day, 1 over next midnight. A base
        # on the second day whose partner is on a third is dropped, since that
        # day's services are not loaded.
        rows = connection.execute(
            """
            select base_schedule_id, assoc_schedule_id,
                   base_unlock_crs, assoc_board_crs,
                   case when date = $date then 0 else 1440 end as base_shift,
                   assoc_day_offset
            from association_link
            where date in ($date, $date + 1)
              and (date = $date or assoc_day_offset = 0)
            """,
            {"date": date},
        ).fetchall()
    except duckdb.CatalogException:
        return empty  # database built before associations existed
    if not rows:
        return empty

    stride = len(index) + 1
    key = lambda trip_index, station: trip_index * stride + station

    record_keys: set[int] = set()
    requirements: dict[tuple[int, int], set[int]] = {}

    for (base_schedule, assoc_schedule, unlock_crs, board_crs,
         base_shift, assoc_offset) in rows:
        base = trip_ids.get((base_schedule, base_shift))
        assoc = trip_ids.get((assoc_schedule, base_shift + 1440 * assoc_offset))
        # Two stations, not one. A split at an operational stop - the Highland
        # sleeper divides at Edinburgh, where nobody boards or alights - means
        # the passenger must already be aboard by the base's last public call
        # before it (Preston), and carries on from the portion's first public
        # call after it (Edinburgh). Where the split is itself a public call,
        # which is the usual shape, both are that station and nothing changes.
        unlock_station = index.get(unlock_crs)
        board_station = index.get(board_crs)
        if base is None or assoc is None:
            continue  # a portion with no public connections on this date
        if unlock_station is None or board_station is None:
            continue
        # Either portion may be the one carrying the passenger onward, so the
        # permission runs both ways.
        for (arriving, at_station), (boarding, from_station_) in (
            ((base, unlock_station), (assoc, board_station)),
            ((assoc, board_station), (base, unlock_station)),
        ):
            record_keys.add(key(arriving, at_station))
            requirements.setdefault((boarding, from_station_), set()).add(
                key(arriving, at_station)
            )

    unlock: list[int | None] = [None] * len(trip)
    needs: list[tuple[int, ...] | None] = [None] * len(trip)
    for i, trip_index in enumerate(trip):
        arrival_key = key(trip_index, to_station[i])
        if arrival_key in record_keys:
            unlock[i] = arrival_key
        required = requirements.get((trip_index, from_station[i]))
        if required:
            needs[i] = tuple(required)
    return unlock, needs


_WEEKDAY_COLUMNS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)


def _load_footpaths(
    connection: duckdb.DuckDBPyConnection,
    date: dt.date,
    station_id,
    timetable_dir: Path | None,
) -> dict[int, list[tuple[int, int, int, int, str]]]:
    links: dict[int, list[tuple[int, int, int, int, str]]] = {}
    if timetable_dir is None:
        return links

    # ALF covers a subset of FLF's pairs but carries day validity and opening
    # hours, so it is authoritative where it applies - otherwise a tube link
    # would be offered at 03:00. FLF fills in the pairs ALF does not mention.
    #
    # **Links run both ways.** RSPS5046 5.10.2.3 writes an FLF record as "WALK
    # BETWEEN AHV AND NCM IN 10 MINUTES" and 5.11.1.1 describes ALF as "links
    # *between* two stations… and the method and time of travel *between*". The
    # data settles it beyond argument: of 1,149 ALF pairs and 1,224 FLF pairs,
    # **not one carries a reverse record** - the files state each link once and
    # expect it read in both directions. Taking them as one-way used half of
    # every fixed link, and Victoria to Abbey Wood went by rail via Blackfriars
    # in 65 minutes because the tube to Whitechapel is listed only the other way
    # round.
    #
    # This is the opposite of the routeing guide's map links, which *are*
    # directional and do carry the reverse wherever it is valid - there,
    # unioning them invents permissions. Two files, two conventions, and the
    # data says which is which.
    #
    # **Every ALF row for a pair is kept, not just the quickest.** 970 of the
    # 1,149 pairs carry more than one row, and they are usually the same link at
    # different times of day - Charing Cross to Victoria is a 17-minute transfer
    # before 07:00 and a 7-minute tube after it. Keeping only the quickest threw
    # away the windows that covered the rest of the day, so the link simply did
    # not exist at 03:00. The scan checks each window itself, so several rows per
    # pair cost nothing.
    alf_rows: dict[tuple[int, int], list[tuple[int, int, int, int, str]]] = {}
    alf_priority: dict[tuple[int, int], int] = {}
    flf_rows: dict[tuple[int, int], tuple[int, int, int, int, str]] = {}

    alf = (timetable_dir / "additional_fixed_link.parquet")
    if alf.exists():
        weekday = _WEEKDAY_COLUMNS[date.weekday()]
        # F= and U= bound a link to a date range - engineering-work
        # replacements, event shuttles. 229 links carry one and 107 of those
        # apply on a single day, so ignoring them offers a one-day bus every
        # day of the year. The dates arrive as the feed writes them, dd/mm/yyyy.
        for origin, destination, minutes, start, end, mode, priority in (
            connection.execute(
                f"""
                select origin, destination, duration, start_time, end_time,
                       mode, priority
                from read_parquet('{alf.as_posix()}')
                where {weekday}
                  and (start_date is null
                       or strptime(start_date, '%d/%m/%Y') <= $date)
                  and (end_date is null
                       or strptime(end_date, '%d/%m/%Y') >= $date)
                """,
                {"date": dt.datetime.combine(date, dt.time())},
            ).fetchall()
        ):
            if origin == destination or minutes is None:
                continue
            here, there = station_id(origin), station_id(destination)
            # RSPS5046 5.11.1.2: where more than one link joins a pair on a
            # given day and time, "the choice of which link should be used in a
            # journey is determined by the Priority Field" - and 5.11.2 says
            # 1 to 7 "with 1 being lowest priority", so the *highest* wins. Only
            # 3 pairs in this feed carry more than one value, and there the
            # durations happen to match; what changes is the mode, which RGK's
            # `L`/`N` conditions are judged against.
            rank = priority if priority is not None else 0
            row = (
                int(minutes),
                start if start is not None else 0,
                end if end is not None else 24 * 60,
                LINK_MODES.get((mode or "").upper(), ""),
            )
            for pair in ((here, there), (there, here)):
                if rank < alf_priority.get(pair, -1):
                    continue
                if rank > alf_priority.get(pair, -1):
                    alf_priority[pair] = rank
                    alf_rows[pair] = []
                alf_rows[pair].append(row)

    flf = (timetable_dir / "fixed_link.parquet")
    if flf.exists():
        for origin, destination, minutes, mode in connection.execute(
            f"select origin, destination, duration, mode "
            f"from read_parquet('{flf.as_posix()}')"
        ).fetchall():
            if origin == destination or minutes is None:
                continue
            here, there = station_id(origin), station_id(destination)
            candidate = (int(minutes), ALL_DAY[0], ALL_DAY[1],
                         LINK_MODES.get((mode or "").upper(), ""))
            for pair in ((here, there), (there, here)):
                if pair in alf_rows:
                    continue
                # FLF carries no windows, so within it the quickest still wins.
                if pair not in flf_rows or candidate[0] < flf_rows[pair][0]:
                    flf_rows[pair] = candidate

    for (source, target), rows in alf_rows.items():
        for minutes, opens, closes, mode in rows:
            links.setdefault(source, []).append((target, minutes, opens, closes, mode))
    for (source, target), (minutes, opens, closes, mode) in flf_rows.items():
        links.setdefault(source, []).append((target, minutes, opens, closes, mode))
    return links
