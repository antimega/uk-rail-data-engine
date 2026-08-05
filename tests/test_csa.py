"""The Connection Scan engine.

Networks are built by hand here rather than loaded from DuckDB, so each test
pins one rule of the algorithm. The rule most easily got wrong is the
distinction between *arriving* somewhere and being *ready to board* there:
changing trains costs the station's minimum change time, staying on the same
train costs nothing.
"""

from __future__ import annotations

import datetime as dt

import pytest

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from rail.engine import UNREACHABLE, earliest_arrival
from rail.engine.network import Network, _load_footpaths


def network(connections, *, stations, change=None, footpaths=None, associations=(),
            operators=None, toc_change=None, train_uids=None):
    """connections: (from, to, depart, arrive, trip) using station names.

    `operators` maps trip -> TOC code; `toc_change` is {(station, from, to):
    minutes}, the TSI records.
    """
    index = {crs: i for i, crs in enumerate(stations)}
    ordered = sorted(connections, key=lambda c: c[2])
    links = [[] for _ in stations]
    for link in footpaths or []:
        source, target, minutes, opens, closes = link[:5]
        # Mode numbering follows RSPS5047: 4 is the Underground, 1 a walk.
        mode = link[5] if len(link) > 5 else "1"
        links[index[source]].append((index[target], minutes, opens, closes, mode))

    # Associations: (trip_a, trip_b, station) means the two trips are the same
    # physical train from that station, so a passenger may stay aboard.
    stride = len(stations) + 1
    key = lambda trip_index, station: trip_index * stride + station
    record_keys, requirements = set(), {}
    for trip_a, trip_b, crs in associations:
        station = index[crs]
        for arriving, boarding in ((trip_a, trip_b), (trip_b, trip_a)):
            record_keys.add(key(arriving, station))
            requirements.setdefault((boarding, station), set()).add(key(arriving, station))

    unlock, needs = [], []
    for position, connection in enumerate(ordered):
        trip_index = connection[4]
        arrival_key = key(trip_index, index[connection[1]])
        unlock.append(arrival_key if arrival_key in record_keys else None)
        required = requirements.get((trip_index, index[connection[0]]))
        needs.append(tuple(required) if required else None)

    rules = {(index[crs], a, b): minutes
             for (crs, a, b), minutes in (toc_change or {}).items()}
    # A trip's own ordered stops, as `load_network` collects them: path
    # reconstruction traces back along the train, not between stations.
    trips = max((c[4] for c in connections), default=-1) + 1
    stops: list[list[int]] = [[] for _ in range(trips)]
    tocs: list[str | None] = [None] * trips
    modes_of: list[str] = ["0"] * trips
    # The trip's own clock, filled exactly as `load_network` fills it: nothing
    # arrives at the first stop, and each departure is written back onto the
    # stop the next connection leaves. `calls_to` reads these.
    trip_arr: list[list[int]] = [[] for _ in range(trips)]
    trip_dep: list[list[int]] = [[] for _ in range(trips)]
    trip_call_arr: list[list[int | None]] = [[] for _ in range(trips)]
    trip_call_dep: list[list[int | None]] = [[] for _ in range(trips)]
    for connection in ordered:
        source, target, depart, arrive, trip_index = connection[:5]
        seq = stops[trip_index]
        if not seq:
            seq.append(index[source])
            trip_arr[trip_index].append(depart)
            trip_dep[trip_index].append(depart)
            trip_call_arr[trip_index].append(None)
            trip_call_dep[trip_index].append(depart)
            tocs[trip_index] = (operators or {}).get(trip_index)
        else:
            trip_dep[trip_index][-1] = depart
            trip_call_dep[trip_index][-1] = depart
        seq.append(index[target])
        trip_arr[trip_index].append(arrive)
        trip_dep[trip_index].append(arrive)
        trip_call_arr[trip_index].append(arrive)
        trip_call_dep[trip_index].append(None)
    return Network(
        date=dt.date(2026, 8, 4),
        stations=list(stations),
        index=index,
        names=list(stations),
        from_station=[index[c[0]] for c in ordered],
        to_station=[index[c[1]] for c in ordered],
        departure=[c[2] for c in ordered],
        arrival=[c[3] for c in ordered],
        trip=[c[4] for c in ordered],
        change=[(change or {}).get(crs, 5) for crs in stations],
        footpaths=links,
        trip_count=max((c[4] for c in connections), default=-1) + 1,
        assoc_unlock=unlock,
        assoc_needs=needs,
        toc=[(operators or {}).get(c[4]) for c in ordered],
        toc_change=rules,
        toc_change_stations=frozenset(station for station, _, _ in rules),
        trip_stops=stops,
        trip_arrival=trip_arr,
        trip_departure=trip_dep,
        trip_call_arrival=trip_call_arr,
        trip_call_departure=trip_call_dep,
        assoc_stride=stride,
        trip_toc=tocs,
        trip_mode=modes_of,
        trip_uid=[(train_uids or {}).get(trip, "") for trip in range(trips)],
    )


def arrivals(result):
    return {j.crs: j.arrival for j in result.reached()}


def test_direct_connection():
    net = network([("A", "B", 600, 660, 0)], stations=["A", "B"])
    assert arrivals(earliest_arrival(net, "A", 540)) == {"B": 660}


def test_a_train_departing_before_you_cannot_be_boarded():
    net = network([("A", "B", 500, 560, 0)], stations=["A", "B"])
    result = earliest_arrival(net, "A", 540)

    assert result.arrival[net.index["B"]] == UNREACHABLE
    assert arrivals(result) == {}


def test_staying_on_the_same_train_costs_no_change_time():
    """B has a 60-minute change time, but we never get off."""
    net = network(
        [("A", "B", 600, 660, 0), ("B", "C", 665, 700, 0)],
        stations=["A", "B", "C"],
        change={"B": 60},
    )
    assert arrivals(earliest_arrival(net, "A", 540))["C"] == 700


def test_changing_trains_respects_the_minimum_change_time():
    """The 665 departure is unreachable off a 660 arrival with a 10-min change."""
    net = network(
        [("A", "B", 600, 660, 0),
         ("B", "C", 665, 700, 1),   # too tight
         ("B", "C", 690, 740, 2)],  # catchable
        stations=["A", "B", "C"],
        change={"B": 10},
    )
    assert arrivals(earliest_arrival(net, "A", 540))["C"] == 740


def test_a_change_station_is_one_calling_point_with_both_its_times():
    """It ends one trip and begins the next, so it appears in both - and the
    halves carry different times. The arrival the passenger made is on the
    incoming row, the departure they caught on the outgoing one; the other two
    numbers belong to trains rather than to the journey.

    Left as two rows, a restriction band matching `exists` over them takes
    whichever is nearer its window and so bites early, which on a morning band
    withdraws a fare that is valid.
    """
    net = network(
        # The connecting train has been sitting at B since 640; the passenger
        # arrives at 660 and leaves at 690, and those are the two that count.
        [("A", "B", 600, 660, 0), ("B", "C", 690, 740, 1)],
        stations=["A", "B", "C"],
        change={"B": 10},
    )
    calls = earliest_arrival(net, "A", 540).calls_to("C")

    assert [c[0] for c in calls] == ["A", "B", "C"]
    station, arrived, departed, changed = calls[1]
    assert (station, changed) == ("B", True)
    assert (arrived, departed) == (660, 690)


def test_a_change_that_exactly_meets_the_minimum_is_allowed():
    net = network(
        [("A", "B", 600, 660, 0), ("B", "C", 670, 700, 1)],
        stations=["A", "B", "C"],
        change={"B": 10},
    )
    assert arrivals(earliest_arrival(net, "A", 540))["C"] == 700


def test_no_change_time_is_charged_at_the_origin():
    net = network(
        [("A", "B", 540, 600, 0)], stations=["A", "B"], change={"A": 30}
    )
    assert arrivals(earliest_arrival(net, "A", 540)) == {"B": 600}


def test_fixed_links_carry_you_between_stations():
    """Cross-London: arrive at B, walk to C, catch a train onward."""
    net = network(
        [("A", "B", 600, 660, 0), ("C", "D", 685, 700, 1)],
        stations=["A", "B", "C", "D"],
        footpaths=[("B", "C", 10, 0, 1440)],
    )
    result = arrivals(earliest_arrival(net, "A", 540))

    # 660 arrival + 5 changing at B + 10 walking.
    assert result["C"] == 675
    assert result["D"] == 700


def test_a_link_is_charged_the_interchange_time_at_both_ends():
    """RSPS5046 5.10.1.3, stated again at 5.11.1.3.

    A fixed link's transit time is summated with the minimum interchange times
    at the stations at either end - not used instead of them. Treating a link
    as door-to-door made two journeys in five out of York look up to three
    hours quicker than they are.
    """
    net = network(
        [("A", "B", 600, 660, 0), ("C", "D", 685, 700, 1), ("C", "D", 690, 705, 2)],
        stations=["A", "B", "C", "D"],
        change={"B": 7, "C": 9},
        footpaths=[("B", "C", 10, 0, 1440)],
    )
    result = earliest_arrival(net, "A", 540)

    # Being at C: 660 + 7 (change at B) + 10 walking.
    assert arrivals(result)["C"] == 677
    # Boarding there costs C's own 9 minutes on top, so 686 is the earliest
    # departure catchable and the 685 one is missed.
    assert arrivals(result)["D"] == 705


def test_a_link_from_the_origin_charges_no_interchange():
    """Nothing is being changed off at the station you start from."""
    net = network(
        [("B", "C", 556, 600, 0)],
        stations=["A", "B", "C"],
        change={"A": 30},
        footpaths=[("A", "B", 10, 0, 1440)],
    )
    result = earliest_arrival(net, "A", 540)

    assert arrivals(result)["B"] == 550  # 540 + 10, not 540 + 30 + 10
    assert arrivals(result)["C"] == 600  # boardable from 555, departs 556


def test_a_fixed_link_outside_its_opening_hours_is_not_used():
    """The Underground is not a 03:00 option."""
    net = network(
        [("A", "B", 600, 660, 0)],
        stations=["A", "B", "C"],
        footpaths=[("B", "C", 10, 420, 1140)],  # 07:00-19:00
    )
    assert "C" in arrivals(earliest_arrival(net, "A", 540))

    late = network(
        [("A", "B", 1400, 1460, 0)],  # arrives 00:20 the next day
        stations=["A", "B", "C"],
        footpaths=[("B", "C", 10, 420, 1140)],
    )
    assert "C" not in arrivals(earliest_arrival(late, "A", 1380))


def test_link_hours_are_wall_clock_on_overnight_journeys():
    """Arrival minutes run past 1440; opening hours do not."""
    net = network(
        [("A", "B", 1400, 1500, 0)],  # arrives 01:00 the following day
        stations=["A", "B", "C"],
        footpaths=[("B", "C", 10, 1, 1439)],  # runs essentially all day
    )
    assert arrivals(earliest_arrival(net, "A", 1380))["C"] == 1515


def test_unreachable_stations_are_omitted_not_reported_as_zero():
    net = network([("A", "B", 600, 660, 0)], stations=["A", "B", "ISLAND"])
    result = earliest_arrival(net, "A", 540)

    assert "ISLAND" not in arrivals(result)
    assert result.arrival[net.index["ISLAND"]] == UNREACHABLE


def test_the_origin_is_not_listed_as_a_destination():
    net = network([("A", "B", 600, 660, 0)], stations=["A", "B"])
    assert "A" not in arrivals(earliest_arrival(net, "A", 540))


def test_the_quickest_of_several_routes_wins():
    net = network(
        [("A", "B", 600, 700, 0),   # slow direct
         ("A", "C", 600, 620, 1),   # fast via C
         ("C", "B", 630, 660, 2)],
        stations=["A", "B", "C"],
        change={"C": 5},
    )
    assert arrivals(earliest_arrival(net, "A", 540))["B"] == 660


def test_journey_minutes_are_measured_from_the_requested_departure():
    net = network([("A", "B", 600, 660, 0)], stations=["A", "B"])
    journey = earliest_arrival(net, "A", 540).reached()[0]

    assert journey.arrival == 660
    assert journey.minutes == 120  # includes the wait at the origin


def test_journey_time_excludes_the_wait_for_the_first_train():
    """Two different questions - "how long does it take" and "when can I be
    there" - and conflating them is how a CLI column came to report one as the
    other. On the real feed, York to Cardiff is 4h23 of travelling and 4h59
    from a 09:00 query, the train leaving at 09:36.
    """
    net = network([("A", "B", 600, 660, 0)], stations=["A", "B"])
    result = earliest_arrival(net, "A", 540)

    assert result.departure_to("B") == 600
    assert result.journey_minutes_to("B") == 60
    assert result.reached()[0].minutes == 120
    assert result.journey_minutes() == {"B": 60}


def test_journey_time_spans_a_change_of_trains():
    """It is the whole journey, waits in the middle included - only the wait
    *before* it starts is excluded."""
    net = network(
        [("A", "B", 600, 630, 0), ("B", "C", 700, 730, 1)],
        stations=["A", "B", "C"], change={"B": 5},
    )
    result = earliest_arrival(net, "A", 540)

    assert result.departure_to("C") == 600
    assert result.journey_minutes_to("C") == 130   # 600 -> 730, the 70-min wait at B included


def test_a_journey_beginning_with_a_walk_starts_when_the_query_does():
    """Nothing is charged at the origin - RSPS5046's interchange rules are
    about changing between trains - so a fixed link off the front can be taken
    the moment the query starts and there is no boarding time to report."""
    net = network(
        [("B", "C", 700, 730, 0)], stations=["A", "B", "C"],
        footpaths=[("A", "B", 10, 0, 1439)],
    )
    result = earliest_arrival(net, "A", 540)

    assert result.departure_to("C") == 540
    assert result.journey_minutes_to("C") == 190


def test_an_unreached_station_has_no_departure_or_journey_time():
    net = network([("A", "B", 600, 660, 0)], stations=["A", "B", "Z"])
    result = earliest_arrival(net, "A", 540)

    assert result.departure_to("Z") is None
    assert result.journey_minutes_to("Z") is None
    assert result.departure_to("ZZZ") is None


def test_unknown_origin_is_an_error():
    net = network([("A", "B", 600, 660, 0)], stations=["A", "B"])
    with pytest.raises(KeyError):
        earliest_arrival(net, "ZZZ", 540)


# --- joins and splits --------------------------------------------------------
#
# Two schedules can be one physical train: a portion joins at Southampton, or
# divides at Crianlarich. A passenger staying aboard makes no interchange, so
# the station's minimum change time must not apply - and in the real feed those
# portions are often booked four minutes apart against a five-minute allowance.


def test_a_tight_join_is_missed_without_the_association():
    net = network(
        [("A", "JUNC", 600, 660, 0),   # portion arrives 660
         ("JUNC", "B", 664, 700, 1)],  # main train leaves 664 - four minutes
        stations=["A", "JUNC", "B"],
        change={"JUNC": 5},
    )
    assert "B" not in arrivals(earliest_arrival(net, "A", 540))


def test_the_association_lets_the_passenger_stay_aboard():
    net = network(
        [("A", "JUNC", 600, 660, 0), ("JUNC", "B", 664, 700, 1)],
        stations=["A", "JUNC", "B"],
        change={"JUNC": 5},
        associations=[(0, 1, "JUNC")],
    )
    assert arrivals(earliest_arrival(net, "A", 540))["B"] == 700


def test_an_association_does_not_help_if_the_portion_has_already_gone():
    net = network(
        [("A", "JUNC", 600, 660, 0), ("JUNC", "B", 655, 700, 1)],  # left before we arrived
        stations=["A", "JUNC", "B"],
        change={"JUNC": 5},
        associations=[(0, 1, "JUNC")],
    )
    assert "B" not in arrivals(earliest_arrival(net, "A", 540))


def test_the_permission_is_tied_to_the_association_station():
    """The partner calls elsewhere before the join; the association must not
    excuse the change time at those earlier stations.

    Roughly one association in seven has the partner calling beyond the join
    point, so a train-wide "you may stay aboard" flag would let a passenger board
    it somewhere the association says nothing about.
    """
    net = network(
        [
            # The portion runs FAR -> MID -> JUNC, then joins and continues to B.
            ("FAR", "MID", 604, 620, 1),
            ("MID", "JUNC", 621, 662, 1),
            ("JUNC", "B", 664, 700, 1),
            # Two ways out of A: to FAR, arriving just too late to board the
            # portion there, and to JUNC, where the association applies.
            ("A", "FAR", 590, 601, 2),
            ("A", "JUNC", 590, 660, 3),
        ],
        stations=["A", "FAR", "MID", "JUNC", "B"],
        change={"FAR": 5, "JUNC": 5},
        associations=[(3, 1, "JUNC")],
    )
    reached = arrivals(earliest_arrival(net, "A", 580))

    assert reached["B"] == 700     # joined at JUNC, where the association is
    assert "MID" not in reached    # not boarded at FAR: 601 + 5 misses the 604


def test_the_association_works_in_the_other_direction_too():
    """A divide: ride the main train in, continue on the portion."""
    net = network(
        [("A", "JUNC", 600, 660, 1), ("JUNC", "B", 664, 700, 0)],
        stations=["A", "JUNC", "B"],
        change={"JUNC": 5},
        associations=[(0, 1, "JUNC")],
    )
    assert arrivals(earliest_arrival(net, "A", 540))["B"] == 700


def test_a_normal_interchange_still_applies_to_unassociated_trains():
    net = network(
        [("A", "JUNC", 600, 660, 0),
         ("JUNC", "B", 664, 700, 1),
         ("JUNC", "C", 670, 720, 2)],  # unrelated train, comfortably after
        stations=["A", "JUNC", "B", "C"],
        change={"JUNC": 5},
        associations=[(0, 1, "JUNC")],
    )
    reached = arrivals(earliest_arrival(net, "A", 540))

    assert reached["B"] == 700  # via the association
    assert reached["C"] == 720  # via an ordinary change, which had time


def test_a_call_with_no_public_arrival_does_not_sever_the_train():
    """A public call may carry a departure and no arrival.

    10,144 mid-journey stops across 7,492 schedules do, and requiring the
    arrival cut the train in two there. The 12:03 Paddington to Penzance became
    two separate trains because Exeter St Davids has no public arrival time, so
    York to Penzance came out 42 minutes late - the fastest journey ended at
    Exeter and had to wait for another service.
    """
    net = network(
        # B is called at with a departure only: the connection into it takes the
        # departure as the arrival, which is a sound upper bound.
        [("A", "B", 600, 660, 0), ("B", "C", 660, 700, 0)],
        stations=["A", "B", "C"],
    )
    result = arrivals(earliest_arrival(net, "A", 540))

    assert result["B"] == 660
    # Staying aboard needs no change time, so C is reached on the same trip.
    assert result["C"] == 700


def test_the_operator_of_every_leg_is_recorded():
    """RGK states route conditions against operators, and no list of calling
    points can settle them: route 00085 is "TPE ONLY"."""
    net = network(
        [("A", "B", 600, 660, 0), ("B", "C", 670, 700, 1)],
        stations=["A", "B", "C"],
        change={"B": 5},
        operators={0: "TP", 1: "GR"},
    )
    result = earliest_arrival(net, "A", 540)

    assert result.operators_to("B") == {"TP"}
    assert result.operators_to("C") == {"TP", "GR"}


def test_a_fixed_link_belongs_to_no_operator():
    net = network(
        [("A", "B", 600, 660, 0)],
        stations=["A", "B", "C"],
        footpaths=[("B", "C", 10, 0, 1440)],
        operators={0: "TP"},
    )
    result = earliest_arrival(net, "A", 540)

    # Walking to C adds nobody, but the train that got you to B still counts.
    assert result.operators_to("C") == {"TP"}


# --- counting changes --------------------------------------------------------


def test_a_direct_train_makes_no_changes():
    result = earliest_arrival(network(
        [("A", "B", 600, 630, 1), ("B", "C", 630, 700, 1)],
        stations=["A", "B", "C"]), "A", 540)

    assert result.changes_to("C") == 0


def test_each_new_train_is_a_change():
    result = earliest_arrival(network(
        [("A", "B", 600, 630, 1), ("B", "C", 640, 700, 2), ("C", "D", 710, 730, 3)],
        stations=["A", "B", "C", "D"]), "A", 540)

    assert result.changes_to("B") == 0
    assert result.changes_to("C") == 1
    assert result.changes_to("D") == 2


def test_a_fixed_link_is_a_change_of_trains():
    """Walking between stations means leaving one train and boarding another,
    which is exactly what a "no changes" restriction bars."""
    result = earliest_arrival(network(
        [("A", "B", 600, 630, 1), ("C", "D", 700, 730, 2)],
        stations=["A", "B", "C", "D"],
        footpaths=[("B", "C", 10, 0, 1439)]), "A", 540)

    assert result.changes_to("C") == 1   # the walk itself
    assert result.changes_to("D") == 2   # the walk, then a new train


def test_staying_aboard_a_joined_portion_is_not_a_change():
    """Two schedules, one physical train. The association exists precisely to
    say the through journey is sold as one train, so counting a change here
    would refuse a "valid on booked service only" fare on the journeys the
    association describes. From Weymouth at 06:00 this moves six destinations
    from one change to none."""
    joined = network(
        [("A", "B", 600, 630, 1), ("B", "C", 634, 700, 2)],
        stations=["A", "B", "C"],
        associations=[(1, 2, "B")])
    # The portion departs B four minutes after arrival, inside the 5-minute
    # change time, so it is only boardable at all through the association.
    result = earliest_arrival(joined, "A", 540)

    assert result.arrival[joined.index["C"]] == 700
    assert result.changes_to("C") == 0


def test_an_unassociated_tight_connection_is_simply_not_boardable():
    """The mirror of the above: without the association the portion cannot be
    reached, so the zero above is the association working rather than the
    change count being blind."""
    result = earliest_arrival(network(
        [("A", "B", 600, 630, 1), ("B", "C", 634, 700, 2)],
        stations=["A", "B", "C"]), "A", 540)

    assert result.arrival[2] >= UNREACHABLE


def test_an_unreached_station_reports_no_changes():
    result = earliest_arrival(network(
        [("A", "B", 600, 630, 1)], stations=["A", "B", "C"]), "A", 540)

    assert result.changes_to("C") == 0
    assert result.changes_to("ZZZ") == 0


def test_changes_are_reported_for_every_station_reached():
    result = earliest_arrival(network(
        [("A", "B", 600, 630, 1), ("B", "C", 640, 700, 2)],
        stations=["A", "B", "C"]), "A", 540)

    assert result.changes() == {"B": 0, "C": 1}


# --- how fixed links are chosen ----------------------------------------------


ALF_SCHEMA = pa.schema([
    ("mode", pa.string()), ("origin", pa.string()), ("destination", pa.string()),
    ("duration", pa.int64()), ("start_time", pa.int64()), ("end_time", pa.int64()),
    ("priority", pa.int64()), ("start_date", pa.string()), ("end_date", pa.string()),
    *[(d, pa.bool_()) for d in
      ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")],
])
FLF_SCHEMA = pa.schema([
    ("mode", pa.string()), ("origin", pa.string()), ("destination", pa.string()),
    ("duration", pa.int64()),
])


def footpaths(tmp_path, *, alf=(), flf=(), stations=("AAA", "BBB")):
    """Links as `load_network` builds them, keyed by station name."""
    directory = tmp_path / "tt"
    directory.mkdir(exist_ok=True)
    days = {d: True for d in
            ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")}
    pq.write_table(pa.Table.from_pylist([
        {"mode": mode, "origin": o, "destination": d, "duration": minutes,
         "start_time": start, "end_time": end, "priority": priority,
         "start_date": None, "end_date": None, **days}
        for o, d, minutes, start, end, priority, mode in alf
    ], schema=ALF_SCHEMA), directory / "additional_fixed_link.parquet")
    pq.write_table(pa.Table.from_pylist([
        {"mode": mode, "origin": o, "destination": d, "duration": minutes}
        for o, d, minutes, mode in flf
    ], schema=FLF_SCHEMA), directory / "fixed_link.parquet")

    index = {crs: i for i, crs in enumerate(stations)}
    links = _load_footpaths(duckdb.connect(), dt.date(2026, 8, 4),
                            lambda crs: index[crs], directory)
    return {stations[source]: sorted(
                (stations[target], minutes, opens, closes, mode)
                for target, minutes, opens, closes, mode in entries)
            for source, entries in links.items()}


def test_every_time_window_for_a_pair_is_kept(tmp_path):
    """970 of the 1,149 ALF pairs carry more than one row, and they are usually
    the same link at different times of day. Keeping only the quickest threw the
    other windows away, so Cannon Street to Waterloo - 22 minutes by day, 24 at
    night - simply had no link at 03:00."""
    links = footpaths(tmp_path, alf=[
        ("AAA", "BBB", 22, 420, 1140, 4, "TUBE"),
        ("AAA", "BBB", 24, 1, 419, 4, "TRANSFER"),
    ])

    assert links["AAA"] == [("BBB", 22, 420, 1140, "4"),
                            ("BBB", 24, 1, 419, "5")]


def test_the_highest_priority_link_wins(tmp_path):
    """RSPS5046 5.11.1.2: where more than one link joins a pair on a given day
    and time, the Priority field chooses - and 5.11.2 says 1 to 7 "with 1 being
    lowest priority", so the highest wins rather than the quickest. Only three
    pairs in the feed carry more than one value."""
    links = footpaths(tmp_path, alf=[
        ("AAA", "BBB", 7, 420, 1439, 4, "TUBE"),
        ("AAA", "BBB", 7, 1, 1439, 6, "TRANSFER"),
    ])

    assert links["AAA"] == [("BBB", 7, 1, 1439, "5")]


def test_priority_beats_a_quicker_link(tmp_path):
    """The spec says the priority decides, not the duration - which is the whole
    point of the field, and the thing that was being ignored."""
    links = footpaths(tmp_path, alf=[
        ("AAA", "BBB", 2, 1, 1439, 1, "TUBE"),
        ("AAA", "BBB", 30, 1, 1439, 7, "WALK"),
    ])

    assert links["AAA"] == [("BBB", 30, 1, 1439, "1")]


def test_a_link_runs_both_ways(tmp_path):
    """RSPS5046 5.10.2.3 writes an FLF record as "WALK BETWEEN AHV AND NCM IN 10
    MINUTES", and the data settles it beyond argument: of 1,149 ALF pairs and
    1,224 FLF pairs, **not one carries a reverse record**. The files state each
    link once and expect it read both ways. Taking them as one-way used half of
    every fixed link - Victoria to Abbey Wood went by rail via Blackfriars in 65
    minutes, because the tube to Whitechapel is listed only the other way round.
    """
    links = footpaths(tmp_path, alf=[("AAA", "BBB", 9, 420, 1140, 4, "TUBE")])

    assert links["AAA"] == [("BBB", 9, 420, 1140, "4")]
    assert links["BBB"] == [("AAA", 9, 420, 1140, "4")]


def test_flf_only_fills_pairs_alf_does_not_mention(tmp_path):
    """ALF carries day validity and opening hours, so it is authoritative where
    it applies - otherwise a tube link gets offered at 03:00. And because a link
    runs both ways, an ALF record covers the reverse too."""
    links = footpaths(
        tmp_path,
        alf=[("AAA", "BBB", 9, 420, 1140, 4, "TUBE")],
        flf=[("AAA", "BBB", 3, "WALK"), ("BBB", "CCC", 4, "WALK")],
        stations=["AAA", "BBB", "CCC"],
    )

    assert links["AAA"] == [("BBB", 9, 420, 1140, "4")]   # ALF wins outright
    assert links["BBB"] == [("AAA", 9, 420, 1140, "4"),
                            ("CCC", 4, 0, 1440, "1")]     # FLF fills a new pair


def test_the_quickest_still_wins_within_flf(tmp_path):
    """FLF carries no windows, so there is nothing else to choose on."""
    links = footpaths(tmp_path, flf=[("AAA", "BBB", 9, "WALK"),
                                     ("AAA", "BBB", 4, "TUBE")])

    assert links["AAA"] == [("BBB", 4, 0, 1440, "4")]


# --- TOC-specific interchange times ------------------------------------------


def interchange_world(*, toc_change=None, gap=8):
    """Arrive at B on an AA train, then leave on a BB train `gap` minutes later.

    The station's own change time is 5, so without a TSI record the connection
    is made whenever `gap` is 5 or more.
    """
    return network(
        [("A", "B", 600, 630, 0), ("B", "C", 630 + gap, 700, 1)],
        stations=["A", "B", "C"],
        operators={0: "AA", 1: "BB"},
        toc_change=toc_change,
    )


def test_without_a_record_the_stations_own_change_time_applies():
    assert arrivals(earliest_arrival(interchange_world(), "A", 540)) == {
        "B": 630, "C": 700}


def test_a_shorter_toc_record_makes_a_connection_the_station_would_refuse():
    """Victoria is 15 minutes generally and 10 between Southern and
    Southeastern; Gatwick 10 and 5; Luton 10 and 4."""
    tight = interchange_world(gap=3)

    assert "C" not in arrivals(earliest_arrival(tight, "A", 540))
    assert "C" in arrivals(earliest_arrival(
        interchange_world(gap=3, toc_change={("B", "AA", "BB"): 3}), "A", 540))


def test_a_longer_toc_record_refuses_a_connection_the_station_would_allow():
    """The case that decides the implementation. Finsbury Park is 5 minutes
    generally and **15** for anything involving Grand Central, so a record that
    merely competed with the station's own time - `min(default, record)` - would
    sell a connection that cannot be made."""
    world = interchange_world(gap=8, toc_change={("B", "AA", "BB"): 15})

    assert "C" not in arrivals(earliest_arrival(world, "A", 540))


def test_a_record_is_directional():
    """RSPS5046 5.12.1.2 says so in as many words: "SE > SN does not
    automatically equate to SN > SE"."""
    reversed_only = interchange_world(gap=3, toc_change={("B", "BB", "AA"): 3})

    assert "C" not in arrivals(earliest_arrival(reversed_only, "A", 540))


def test_a_record_for_another_operator_does_not_apply():
    world = interchange_world(gap=8, toc_change={("B", "AA", "ZZ"): 15})

    # The AA -> BB change is not governed, so the station's own 5 applies.
    assert "C" in arrivals(earliest_arrival(world, "A", 540))


def test_the_origin_is_charged_nothing_even_at_a_governed_station():
    """A passenger starting at Finsbury Park is not changing off anything."""
    net = network(
        [("A", "B", 540, 600, 0)],
        stations=["A", "B"],
        operators={0: "BB"},
        toc_change={("A", "AA", "BB"): 30},
    )

    assert arrivals(earliest_arrival(net, "A", 540)) == {"B": 600}


def test_a_fixed_link_arrival_never_matches_a_record():
    """5.12 is about changing between trains, so a walk in is charged the
    station's own time."""
    net = network(
        [("B", "C", 620, 700, 1)],
        stations=["A", "B", "C"],
        operators={1: "BB"},
        footpaths=[("A", "B", 10, 0, 1439)],
        toc_change={("B", "AA", "BB"): 60},
    )

    # Walk A->B arriving 550, plus B's own 5 minutes, boards the 620.
    assert arrivals(earliest_arrival(net, "A", 540))["C"] == 700


def test_the_quickest_way_in_wins_when_several_are_possible():
    """Each way of having arrived is charged its own change time, so a later
    arrival on a governed operator can still board sooner than an earlier one
    that is not."""
    net = network(
        [("A", "B", 600, 630, 0),      # AA, arrives 630, governed at 2 minutes
         ("A", "B", 590, 620, 2),      # ZZ, arrives 620, station default 5
         ("B", "C", 632, 700, 1)],
        stations=["A", "B", "C"],
        operators={0: "AA", 1: "BB", 2: "ZZ"},
        toc_change={("B", "AA", "BB"): 2},
    )

    # ZZ would be ready at 625 and AA at 632: either boards the 632.
    assert arrivals(earliest_arrival(net, "A", 540))["C"] == 700


def test_the_same_schedule_on_two_days_is_two_trains():
    """The router loads two days at a time, shifting the second by 1440 minutes,
    so a schedule appearing on both is two physical trains. Sharing a trip index
    would let a passenger "stay aboard" from tonight's service onto tomorrow's -
    boarding at 23:00 and arriving somewhere the same train reaches at 08:00 the
    next morning without ever getting off."""
    net = network(
        [("A", "B", 1380, 1400, 0),          # tonight, trip 0
         ("B", "C", 1380 + 1440, 1400 + 1440, 1)],  # tomorrow, a separate trip
        stations=["A", "B", "C"],
    )
    result = earliest_arrival(net, "A", 1300)

    # Reachable, but only by waiting at B - the two are not one train.
    assert arrivals(result)["C"] == 1400 + 1440
    assert result.changes_to("C") == 1


def test_a_path_is_traced_back_along_the_train_not_between_stations():
    """A through train passes stations whose own best arrival is later than the
    moment it went by, and following *their* history leads somewhere the
    passenger never was.

    Here the A-train runs A → B → C without stopping usefully at B, and a
    separate later service reaches B from C. Walking back station by station
    goes C, B, C and stops on its own loop guard; walking back along the train
    gives A, B, C. This is the Kingussie case: the sleeper passes it around
    22:00 having boarded at Euston, while Kingussie's own earliest arrival is
    tomorrow morning's train from Aviemore. 56 of 2,843 paths from Euston at
    21:00 were affected and no daytime query was, which is why it went
    unnoticed.
    """
    net = network(
        [("A", "B", 600, 610, 0), ("B", "C", 610, 620, 0),   # the through train
         ("C", "B", 700, 990, 1)],                            # a later service back
        stations=["A", "B", "C"],
    )
    result = earliest_arrival(net, "A", 540)

    assert result.path_to("C") == ["A", "B", "C"]
    assert result.arrival[net.index["B"]] == 610


def test_a_path_over_a_fixed_link_still_reconstructs():
    """Tracing by train has to fall back to the single step a link makes, since
    a link is nobody's train and has no stop sequence to follow."""
    net = network(
        [("B", "C", 700, 730, 1)],
        stations=["A", "B", "C"],
        footpaths=[("A", "B", 5, 0, 1439)],
    )

    assert earliest_arrival(net, "A", 540).path_to("C") == ["A", "B", "C"]


def test_the_origin_is_its_own_path():
    net = network([("A", "B", 600, 610, 0)], stations=["A", "B"])

    assert earliest_arrival(net, "A", 540).path_to("A") == ["A"]


def test_the_operator_comes_from_the_train_not_the_station():
    """The counterpart of tracing the path by train. A station's own
    `arrived_by` names whichever service reached it soonest, which on a through
    journey is often not the train the passenger is on - Euston to Inverness
    reported the sleeper *and* LNER, and zero changes, because Kingussie's own
    best arrival is an LNER service the passenger never boarded."""
    net = network(
        [("A", "B", 600, 610, 0), ("B", "C", 610, 620, 0),
         ("C", "B", 700, 990, 1)],
        stations=["A", "B", "C"],
        operators={0: "CS", 1: "GR"},
    )
    result = earliest_arrival(net, "A", 540)

    assert result.operators_to("C") == {"CS"}
    assert result.changes_to("C") == 0


def test_a_set_down_stop_stays_on_the_path_but_cannot_be_boarded():
    """A public call with an arrival and no departure is set-down only: you may
    alight, not join. Requiring a departure severed the train there, so the
    sleeper lost Stirling, Dunblane, Gleneagles, Perth, Dunkeld, Pitlochry,
    Blair Atholl, Dalwhinnie and Newtonmore from its calling points - 19,589
    such calls across 8,659 schedules, nearly twice the mirror case. The
    arrival was still right, because a trip is boarded once and every
    connection of it relaxes; the *path* was what went wrong, and paths are
    what the route conditions and the routeing guide are judged on.
    """
    net = network(
        [("A", "B", 600, 610, 0), ("B", "C", 610, 620, 0)],
        stations=["A", "B", "C", "D"],
    )
    # B is set-down only on this train.
    net.boardable = [True, False]
    result = earliest_arrival(net, "A", 540)

    assert result.path_to("C") == ["A", "B", "C"]
    # Someone starting at B cannot join it there.
    assert "C" not in arrivals(earliest_arrival(net, "B", 540))


def test_the_journey_the_calling_points_and_the_changes_agree():
    """They are one walk, because keeping them separate is how they came to
    disagree: the sleeper to Inverness reported one change and two operators on
    a journey that is one train from Euston to Inverness."""
    net = network(
        [("A", "B", 600, 610, 0), ("B", "C", 610, 620, 0),
         ("C", "D", 700, 720, 1)],
        stations=["A", "B", "C", "D"],
        operators={0: "CS", 1: "SR"},
    )
    result = earliest_arrival(net, "A", 540)

    assert result.path_to("D") == ["A", "B", "C", "D"]
    assert result.trips_to("D") == [0, 1]
    assert result.changes_to("D") == 1
    assert result.operators_to("D") == {"CS", "SR"}


def test_the_journey_exposes_fares_train_uids_and_leg_specific_calls():
    net = network(
        [("A", "B", 600, 610, 0), ("B", "C", 620, 630, 1)],
        stations=["A", "B", "C"],
        train_uids={0: "C04660", 1: "P66915"},
    )
    result = earliest_arrival(net, "A", 540)

    assert result.train_uids_to("C") == {"C04660", "P66915"}
    assert result.train_calls_to("C") == [
        ("C04660", "A", None, 600),
        ("C04660", "B", 610, None),
        ("P66915", "B", None, 620),
        ("P66915", "C", 630, None),
    ]


# --- profiling a window ------------------------------------------------------


def test_a_window_is_minimised_over_journey_time_not_elapsed():
    """`best_over_window` sweeps departures and keeps the best per station, and
    "best" has to mean the shortest *journey*.

    `Journey.minutes` counts from each sampled departure, so it is the journey
    plus however long you waited for it. Minimising that can never see below the
    wait - at a station served rarely, every sample in the window contains a long
    one, so the reported figure stays stuck at wait-plus-journey however finely
    the window is swept.

    Here one train leaves at 10:00 and takes an hour. Sampling on the hour from
    09:00, the smallest elapsed time is 120 minutes (waiting 09:00 to 10:00, then
    travelling) and the journey is 60. The window must report 60.

    Measured on the real feed from York over 09:00-20:00, minimising elapsed
    overstated the journey to every one of 2,729 stations, by a median of 6
    minutes and up to 42.
    """
    from rail.engine import best_over_window

    net = network([("A", "B", 600, 660, 0)], stations=["A", "B"])

    best = best_over_window(net, "A", first_departure=540, last_departure=660,
                            step=60)

    assert best == {"B": 60}, "the window reported elapsed time, not the journey"


def test_a_window_keeps_the_shortest_journey_not_the_earliest_arrival():
    """Two trains: a slow early one and a fast later one. The earliest arrival
    is the slow train, but the shortest journey is the fast one, and profiling
    asks how well connected the pair is rather than when you could first be
    there."""
    from rail.engine import best_over_window

    net = network(
        [("A", "B", 540, 720, 0),      # 09:00, three hours
         ("A", "B", 780, 840, 1)],     # 13:00, one hour
        stations=["A", "B"],
    )

    best = best_over_window(net, "A", first_departure=540, last_departure=840,
                            step=60)

    assert best == {"B": 60}


def test_the_timetable_directory_is_found_when_not_given():
    """`load_network` without `timetable_dir` must not quietly build a lesser
    network.

    Two things that change the answer live in that Parquet rather than in the
    database: the fixed links, and the operator-specific interchange times.
    Omitting them raises nothing - it returns a network with fewer edges, and
    from York on a weekday that is **172 of 2,901 destinations** gone, plus every
    journey changing between two named operators mistimed.

    The obvious library call is `load_network(connection, date)`, so that call
    has to be the right one. Pinned at the source level because the behaviour
    needs a built database to exercise, and the failure is a silently smaller
    answer rather than an error.
    """
    import inspect

    from rail.engine import network as network_module

    source = inspect.getsource(network_module.load_network)
    assert "if timetable_dir is None:" in source
    assert "_discover_timetable_dir()" in source, (
        "load_network must find the timetable directory when it is not given"
    )

    # And the discovery itself must never raise: it is a convenience, and a
    # caller with no ingested snapshot should still get a database-only network.
    assert network_module._discover_timetable_dir.__doc__
    discovery = inspect.getsource(network_module._discover_timetable_dir)
    assert "except Exception" in discovery and "return None" in discovery
