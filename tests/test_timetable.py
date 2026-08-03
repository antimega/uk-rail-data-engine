"""STP overlay resolution - the most error-prone step in the pipeline.

CIF gives a base schedule and then amends it. Several records can cover the same
date and the winner is decided by priority C > N > O > P, where a cancellation
winning means the train does not run at all. Getting this wrong produces a
timetable that looks entirely plausible while running cancelled trains.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rail.model import build_timetable

START = dt.date(2026, 8, 3)  # a Monday
WEEKDAYS = dict(monday=True, tuesday=True, wednesday=True, thursday=True,
                friday=True, saturday=False, sunday=False)

SCHEDULE_SCHEMA = pa.schema(
    [("line_no", pa.int64()), ("train_uid", pa.string()),
     ("runs_from", pa.date32()), ("runs_to", pa.date32()),
     *[(d, pa.bool_()) for d in
       ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")],
     ("bank_holiday_running", pa.string()), ("train_status", pa.string()),
     ("train_category", pa.string()), ("train_identity", pa.string()),
     ("stp_indicator", pa.string())]
)

EXTRA_SCHEMA = pa.schema(
    [("line_no", pa.int64()), ("atoc_code", pa.string()), ("retail_train_id", pa.string())]
)

STOP_SCHEMA = pa.schema(
    [("line_no", pa.int64()), ("record_type", pa.string()), ("location", pa.string()),
     ("public_arrival", pa.int32()), ("public_departure", pa.int32()),
     ("scheduled_arrival", pa.int32()), ("scheduled_departure", pa.int32()),
     ("platform", pa.string()), ("activity", pa.string())]
)


def schedule(line_no, uid, stp, runs_from, runs_to, status="P", **days):
    return {"line_no": line_no, "train_uid": uid, "stp_indicator": stp,
            "runs_from": runs_from, "runs_to": runs_to,
            "bank_holiday_running": None, "train_status": status,
            "train_category": "OO", "train_identity": "1A01",
            **{**WEEKDAYS, **days}}


def stop(line_no, kind, location, arrive=None, depart=None, activity="T "):
    return {"line_no": line_no, "record_type": kind, "location": location,
            "public_arrival": arrive, "public_departure": depart,
            "scheduled_arrival": None, "scheduled_departure": None,
            "platform": None, "activity": activity}


#: The second schedule file. Same record layout as the main one, and the
#: difference that matters is invisible here: its `location` is a CRS where the
#: main file's is a TIPLOC.
Z_SCHEDULE_SCHEMA = SCHEDULE_SCHEMA
Z_EXTRA_SCHEMA = pa.schema(
    [("line_no", pa.int64()), ("atoc_code", pa.string())])
Z_STOP_SCHEMA = STOP_SCHEMA


@pytest.fixture
def build(tmp_path):
    def _build(schedules, stops=(), horizon_days=13,
               z_schedules=(), z_stops=(), z_extra=(), operational_only=None):
        directory = tmp_path / "tt"
        directory.mkdir(exist_ok=True)
        pq.write_table(pa.Table.from_pylist(list(schedules), schema=SCHEDULE_SCHEMA),
                       directory / "schedule.parquet")
        pq.write_table(pa.Table.from_pylist([], schema=EXTRA_SCHEMA),
                       directory / "schedule_extra.parquet")
        pq.write_table(pa.Table.from_pylist(list(stops), schema=STOP_SCHEMA),
                       directory / "stop_time.parquet")
        if z_schedules or z_stops:
            pq.write_table(
                pa.Table.from_pylist(list(z_schedules), schema=Z_SCHEDULE_SCHEMA),
                directory / "z_schedule.parquet")
            pq.write_table(
                pa.Table.from_pylist(list(z_extra), schema=Z_EXTRA_SCHEMA),
                directory / "z_schedule_extra.parquet")
            pq.write_table(pa.Table.from_pylist(list(z_stops), schema=Z_STOP_SCHEMA),
                           directory / "z_stop_time.parquet")
        connection = duckdb.connect()
        # `build_timetable` resolves each stop's CRS as it goes, so it needs the
        # crosswalk `build_reference` writes - which the real pipeline builds
        # first. Here the fixture's locations are their own CRS plus "TIP", so
        # the mapping is mechanical.
        connection.execute("create table station_tiploc (crs varchar, tiploc varchar)")
        connection.execute("create table tiploc_crs (crs varchar, tiploc varchar)")
        operational_only = operational_only or {}
        for location in {s["location"] for s in stops}:
            operational_crs = operational_only.get(location, location[:3])
            connection.execute(
                "insert into tiploc_crs values (?, ?)",
                [operational_crs, location],
            )
            if location not in operational_only:
                connection.execute("insert into station_tiploc values (?, ?)",
                                   [location[:3], location])
        counts = build_timetable(connection, directory, start=START,
                                 horizon_days=horizon_days)
        return connection, counts
    return _build


def dates_for(connection, uid="A00001"):
    return [r[0] for r in connection.execute(
        "select date from service_date where train_uid = ? order by date", [uid]
    ).fetchall()]


def test_permanent_schedule_runs_only_on_its_days(build):
    connection, _ = build([schedule(1, "A00001", "P", START, START + dt.timedelta(days=6))])

    # Monday to Sunday, weekdays only.
    assert dates_for(connection) == [START + dt.timedelta(days=i) for i in range(5)]


def test_overlay_beats_the_permanent_schedule_on_its_dates(build):
    wednesday = START + dt.timedelta(days=2)
    connection, _ = build([
        schedule(1, "A00001", "P", START, START + dt.timedelta(days=6)),
        schedule(100, "A00001", "O", wednesday, wednesday),
    ])
    won = dict(connection.execute(
        "select date, schedule_id from service_date where train_uid = 'A00001'"
    ).fetchall())

    assert won[wednesday] == 100  # the overlay, not the base
    assert won[START] == 1
    assert len(won) == 5  # the train still runs five days


def test_cancellation_removes_the_day_entirely(build):
    thursday = START + dt.timedelta(days=3)
    connection, counts = build([
        schedule(1, "A00001", "P", START, START + dt.timedelta(days=6)),
        schedule(100, "A00001", "C", thursday, thursday),
    ])

    assert thursday not in dates_for(connection)
    assert len(dates_for(connection)) == 4
    assert counts.cancelled_dates == 1


def test_new_schedule_outranks_an_overlay(build):
    tuesday = START + dt.timedelta(days=1)
    connection, _ = build([
        schedule(1, "A00001", "P", START, START + dt.timedelta(days=6)),
        schedule(100, "A00001", "O", tuesday, tuesday),
        schedule(200, "A00001", "N", tuesday, tuesday),
    ])
    winner = connection.execute(
        "select schedule_id from service_date where date = ?", [tuesday]
    ).fetchone()

    assert winner == (200,)  # C > N > O > P


def test_cancellation_outranks_everything(build):
    tuesday = START + dt.timedelta(days=1)
    connection, _ = build([
        schedule(1, "A00001", "P", START, START + dt.timedelta(days=6)),
        schedule(200, "A00001", "N", tuesday, tuesday),
        schedule(300, "A00001", "C", tuesday, tuesday),
    ])

    assert tuesday not in dates_for(connection)


def test_other_trains_are_unaffected_by_a_cancellation(build):
    tuesday = START + dt.timedelta(days=1)
    connection, _ = build([
        schedule(1, "A00001", "P", START, START + dt.timedelta(days=6)),
        schedule(2, "B00002", "P", START, START + dt.timedelta(days=6)),
        schedule(300, "A00001", "C", tuesday, tuesday),
    ])

    assert tuesday not in dates_for(connection, "A00001")
    assert tuesday in dates_for(connection, "B00002")


def test_stops_attach_to_the_preceding_schedule(build):
    connection, _ = build(
        [schedule(1, "A00001", "P", START, START),
         schedule(10, "B00002", "P", START, START)],
        [stop(2, "LO", "EUSTON", depart=450),
         stop(3, "LT", "BHAMNWS", arrive=540),
         stop(11, "LO", "KNGX", depart=600),
         stop(12, "LT", "YORK", arrive=720)],
    )
    rows = connection.execute("""
        select s.train_uid, ss.seq, ss.location
        from schedule_stop ss join train_schedule s using (schedule_id)
        order by s.train_uid, ss.seq
    """).fetchall()

    assert rows == [
        ("A00001", 1, "EUSTON"), ("A00001", 2, "BHAMNWS"),
        ("B00002", 1, "KNGX"), ("B00002", 2, "YORK"),
    ]


def test_passing_points_are_not_public_stops(build):
    connection, _ = build(
        [schedule(1, "A00001", "P", START, START)],
        [stop(2, "LO", "EUSTON", depart=450),
         # No public time and no boarding activity: a passing point.
         stop(3, "LI", "LNGROCK", activity="  "),
         stop(4, "LT", "BHAMNWS", arrive=540)],
    )
    public = connection.execute(
        "select location from schedule_stop where is_public order by seq"
    ).fetchall()

    assert public == [("EUSTON",), ("BHAMNWS",)]


def test_overnight_times_are_unwrapped_past_midnight(build):
    """Public times are minutes after midnight and wrap; journeys must not."""
    connection, _ = build(
        [schedule(1, "A00001", "P", START, START)],
        [stop(2, "LO", "PADTLL", depart=1412),   # 23:32
         stop(3, "LI", "LIVSTLL", arrive=1423, depart=1424),
         stop(4, "LT", "SHENFLD", arrive=27)],   # 00:27 the next day
    )
    rows = connection.execute("""
        select location, day_offset, arrival_minutes, departure_minutes
        from schedule_stop order by seq
    """).fetchall()

    assert rows[0] == ("PADTLL", 0, None, 1412)
    assert rows[1] == ("LIVSTLL", 0, 1423, 1424)
    # 00:27 becomes 1467, so the journey is 55 minutes, not minus 1,385.
    assert rows[2] == ("SHENFLD", 1, 1467, None)
    assert rows[2][2] - rows[0][3] == 55


def test_a_stop_straddling_midnight_carries_forward(build):
    """Arrive 23:59, leave 00:05 - later stops are on the next day too."""
    connection, _ = build(
        [schedule(1, "A00001", "P", START, START)],
        [stop(2, "LO", "AAA", depart=1430),
         stop(3, "LI", "BBB", arrive=1439, depart=5),
         stop(4, "LT", "CCC", arrive=30)],
    )
    rows = connection.execute("""
        select location, day_offset, arrival_minutes, departure_minutes
        from schedule_stop order by seq
    """).fetchall()

    assert rows[1] == ("BBB", 0, 1439, 1445)  # 00:05 is 1445 absolute
    # Without carrying the rollover, CCC would land at 30 and go backwards.
    assert rows[2] == ("CCC", 1, 1470, None)


def test_every_journey_is_monotonic_after_unwrapping(build):
    connection, _ = build(
        [schedule(1, "A00001", "P", START, START)],
        [stop(2, "LO", "AAA", depart=1400),
         stop(3, "LI", "BBB", arrive=1439, depart=2),
         stop(4, "LI", "CCC", arrive=40, depart=45),
         stop(5, "LT", "DDD", arrive=90)],
    )
    times = [r[0] for r in connection.execute("""
        select coalesce(arrival_minutes, departure_minutes) from schedule_stop order by seq
    """).fetchall()]

    assert times == sorted(times)


def test_non_passenger_services_are_flagged_not_deleted(build):
    connection, _ = build([
        schedule(1, "A00001", "P", START, START, status="P"),
        schedule(2, "F00002", "P", START, START, status="F"),  # freight
    ])
    flags = dict(connection.execute(
        "select train_uid, is_passenger from train_schedule"
    ).fetchall())

    assert flags == {"A00001": True, "F00002": False}
    # Still resolved into running dates - filtering is the caller's choice.
    assert dates_for(connection, "F00002") == [START]


# --- the second schedule file -----------------------------------------------
#
# `ZTR` carries the services the main CIF cannot express: Hovertravel's crossing
# of the Solent, rail-replacement coaches, Red Funnel, the Metropolitan line
# beyond Harrow. It was parsed from the day the layouts were written and read by
# nothing until it was noticed that no journey anywhere used a hovercraft.


def test_ztr_schedules_are_loaded_alongside_the_main_file(build):
    """Both files, one `train_schedule`, told apart by `source`."""
    connection, counts = build(
        [schedule(1, "A00001", "P", START, START + dt.timedelta(days=6))],
        [stop(2, "LO", "AAATIP", depart=600), stop(3, "LT", "BBBTIP", arrive=660)],
        z_schedules=[schedule(1, "Z00001", "P", START, START + dt.timedelta(days=6),
                              status="S")],
        z_extra=[{"line_no": 2, "atoc_code": "QH"}],
        z_stops=[stop(2, "LO", "XRD", depart=700), stop(3, "LT", "SHV", arrive=710)],
    )

    assert connection.execute(
        "select source, count(*) from train_schedule group by 1 order by 1"
    ).fetchall() == [("cif", 1), ("ztr", 1)]
    assert counts.schedules == 2


def test_a_ztr_schedule_id_cannot_collide_with_a_cif_one():
    """The two files are numbered separately - the real ones overlap, CIF
    running 16,949 to 7,901,571 and ZTR 1 to 31,355 - so a ZTR line number is
    offset out of reach rather than trusted to be distinct."""
    from rail.model.timetable import ZTR_SCHEDULE_OFFSET

    assert ZTR_SCHEDULE_OFFSET > 7_901_571


def test_the_two_files_name_locations_differently_and_both_resolve(build):
    """**The trap this was always going to spring.** `stop_time.location` is a
    TIPLOC and `z_stop_time.location` is a CRS, so a union of the two matches
    only half against `station_tiploc` - and the half that misses is dropped
    silently rather than failing. `crs` is resolved once, at build, for both."""
    connection, _ = build(
        [schedule(1, "A00001", "P", START, START + dt.timedelta(days=6))],
        [stop(2, "LO", "AAATIP", depart=600), stop(3, "LT", "BBBTIP", arrive=660)],
        z_schedules=[schedule(1, "Z00001", "P", START, START + dt.timedelta(days=6),
                              status="S")],
        z_extra=[{"line_no": 2, "atoc_code": "QH"}],
        z_stops=[stop(2, "LO", "XRD", depart=700), stop(3, "LT", "SHV", arrive=710)],
    )

    resolved = connection.execute("""
        select s.source, ss.location, ss.crs
        from schedule_stop ss join train_schedule s using (schedule_id)
        order by s.source, ss.seq
    """).fetchall()

    assert resolved == [
        ("cif", "AAATIP", "AAA"), ("cif", "BBBTIP", "BBB"),
        # A ZTR location is already a CRS and passes straight through.
        ("ztr", "XRD", "XRD"), ("ztr", "SHV", "SHV"),
    ]


def test_operational_crs_does_not_turn_a_timing_point_into_a_station(build):
    connection, _ = build(
        [schedule(1, "A00001", "P", START, START)],
        [
            stop(2, "LO", "AAATIP", depart=600),
            stop(3, "LI", "MILESPL", arrive=630),
        ],
        operational_only={"MILESPL": "MLP"},
    )

    assert connection.execute(
        "select crs, operational_crs from schedule_stop where location = 'MILESPL'"
    ).fetchone() == (None, "MLP")


def test_a_ztr_service_gets_running_dates_like_any_other(build):
    """Same STP resolution, same day-of-week rules. Nothing about the second
    file is special once it is in `train_schedule`."""
    connection, _ = build(
        [schedule(1, "A00001", "P", START, START + dt.timedelta(days=6))],
        [stop(2, "LO", "AAATIP", depart=600)],
        z_schedules=[schedule(1, "Z00001", "P", START, START + dt.timedelta(days=6),
                              status="S")],
        z_extra=[{"line_no": 2, "atoc_code": "QH"}],
        z_stops=[stop(2, "LO", "XRD", depart=700)],
    )

    assert dates_for(connection, "Z00001") == [
        START + dt.timedelta(days=i) for i in range(5)]


def test_the_build_works_without_a_ztr_file(build):
    """Older snapshots have none, and an ingest could fail to write one. The
    main file must not depend on its being there."""
    connection, counts = build(
        [schedule(1, "A00001", "P", START, START + dt.timedelta(days=6))],
        [stop(2, "LO", "AAATIP", depart=600)])

    assert counts.schedules == 1
    assert connection.execute(
        "select distinct source from train_schedule").fetchall() == [("cif",)]
