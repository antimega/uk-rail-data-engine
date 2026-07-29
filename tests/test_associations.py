"""Resolving which trains are joined on which days.

Associations carry their own STP indicators and resolve exactly like schedules,
C > N > O > P, so a cancelled association has to stop applying on the days it is
cancelled - otherwise the router keeps a passenger aboard a portion that is not
there.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rail.model import build_associations

MONDAY = dt.date(2026, 8, 3)
TUESDAY = dt.date(2026, 8, 4)
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

AA_SCHEMA = pa.schema([
    ("base_uid", pa.string()), ("assoc_uid", pa.string()),
    ("assoc_location", pa.string()), ("assoc_cat", pa.string()),
    ("assoc_date_ind", pa.string()), ("association_type", pa.string()),
    ("stp_indicator", pa.string()),
    ("start_date", pa.date32()), ("end_date", pa.date32()),
    *[(d, pa.bool_()) for d in DAYS],
])


def association(base, assoc, *, cat="JJ", stp="P", date_ind="S",
                start=MONDAY, end=TUESDAY, location="SOTON"):
    return {"base_uid": base, "assoc_uid": assoc, "assoc_location": location,
            "assoc_cat": cat, "assoc_date_ind": date_ind, "association_type": "P",
            "stp_indicator": stp, "start_date": start, "end_date": end,
            **{d: True for d in DAYS}}


@pytest.fixture
def built(tmp_path):
    def _build(associations, stops=None, tiplocs=None):
        directory = tmp_path / "tt"
        directory.mkdir(exist_ok=True)
        pq.write_table(pa.Table.from_pylist(list(associations), schema=AA_SCHEMA),
                       directory / "association.parquet")

        c = duckdb.connect()
        # Two trains, both running Monday and Tuesday, both calling at SOTON.
        c.execute("""create table service_date as select * from (values
            (1, 'BASE01', date '2026-08-03'), (1, 'BASE01', date '2026-08-04'),
            (2, 'PORT02', date '2026-08-03'), (2, 'PORT02', date '2026-08-04'),
            (3, 'LONE03', date '2026-08-03')
        ) t(schedule_id, train_uid, date)""")
        c.execute("create table station_tiploc (crs varchar, tiploc varchar)")
        for crs, tiploc in (tiplocs or [("SOU", "SOTON")]):
            c.execute("insert into station_tiploc values (?, ?)", [crs, tiploc])
        # `seq` matters now: a split at an operational stop is resolved to the
        # last public call before it and the first public call after.
        c.execute("create table schedule_stop "
                  "(schedule_id bigint, location varchar, is_public boolean, seq bigint)")
        for row in (stops or [(1, "SOTON", True, 1), (2, "SOTON", True, 1),
                              (3, "SOTON", True, 1)]):
            c.execute("insert into schedule_stop values (?, ?, ?, ?)", list(row))

        counts = build_associations(c, directory)
        return c, counts

    return _build


def dates_for(connection):
    return sorted(r[0] for r in connection.execute(
        "select distinct date from association_link").fetchall())


def test_a_permanent_association_links_both_trains(built):
    connection, counts = built([association("BASE01", "PORT02")])

    assert counts.links == 2  # one per running day
    assert dates_for(connection) == [MONDAY, TUESDAY]
    row = connection.execute(
        "select base_schedule_id, assoc_schedule_id, crs from association_link limit 1"
    ).fetchone()
    assert row == (1, 2, "SOU")


def test_a_cancelled_association_does_not_apply(built):
    connection, _ = built([
        association("BASE01", "PORT02"),
        association("BASE01", "PORT02", stp="C", start=TUESDAY, end=TUESDAY),
    ])

    # Monday still joined; Tuesday's join is cancelled.
    assert dates_for(connection) == [MONDAY]


def test_an_overlay_beats_the_permanent_record(built):
    connection, _ = built([
        association("BASE01", "PORT02", cat="JJ"),
        association("BASE01", "PORT02", cat="VV", stp="O",
                    start=TUESDAY, end=TUESDAY),
    ])
    by_date = dict(connection.execute(
        "select date, assoc_cat from association_link").fetchall())

    assert by_date[MONDAY] == "JJ"
    assert by_date[TUESDAY] == "VV"


def test_a_next_day_association_resolves_on_the_following_day(built):
    """RSPS5046 5.5.8.2 field 9: `N` is "over next midnight", and the offset is
    on the *associated* schedule. All 234 in this feed are Caledonian Sleeper
    divides - the base leaves Euston at 21:15 and the portion is a separate
    schedule dated the next day, departing 04:28, with no same-day overlap at
    all. Resolving them on the base date found nothing, so the link did not
    exist and Euston at 21:00 could not reach Fort William."""
    connection, counts = built([association("BASE01", "PORT02", date_ind="N")])
    rows = connection.execute(
        "select date, assoc_day_offset from association_link order by date"
    ).fetchall()

    assert counts.links > 0
    assert counts.next_day == counts.links
    # The base runs Monday; the partner is picked up from Tuesday.
    assert rows[0] == (MONDAY, 1)


def test_a_same_day_association_carries_no_offset(built):
    connection, counts = built([association("BASE01", "PORT02")])

    assert counts.next_day == 0
    assert connection.execute(
        "select distinct assoc_day_offset from association_link").fetchall() == [(0,)]


def test_both_trains_must_run_that_day(built):
    """LONE03 runs on Monday only, so Tuesday produces no link."""
    connection, _ = built([association("BASE01", "LONE03")])

    assert dates_for(connection) == [MONDAY]


def test_an_unknown_association_location_produces_no_link(built):
    connection, counts = built(
        [association("BASE01", "PORT02", location="NOWHERE")]
    )

    assert counts.links == 0


def test_only_join_and_divide_categories_are_used(built):
    """Other categories do not mean a passenger can stay aboard."""
    connection, counts = built([association("BASE01", "PORT02", cat="NP")])

    assert counts.links == 0


def test_a_split_at_an_operational_stop_still_makes_a_link(built):
    """A train is divided where the operation happens, which is routinely a
    stop nobody boards or alights at. The Highland sleeper splits at Edinburgh
    and the Inverness portion's Edinburgh entry has no times and `is_public`
    false, because the passengers stay aboard. Requiring a public call there
    dropped every Aberdeen and Fort William portion, and Euston to Aberdeen came
    out 53 minutes late with two changes against a through train.

    The link resolves to the base's last public call *before* the split - for
    the sleeper that is Preston - and the portion's first public call after it,
    because the Aberdeen portion leaves Edinburgh before the Inverness portion
    reaches its own next public call at Stirling.
    """
    connection, counts = built(
        [association("BASE01", "PORT02")],
        stops=[(1, "PRESTON", True, 1), (1, "SOTON", False, 2),
               (2, "SOTON", True, 1)],
        tiplocs=[("SOU", "SOTON"), ("PRE", "PRESTON")],
    )

    assert counts.links > 0
    assert connection.execute(
        "select distinct base_unlock_crs, assoc_board_crs from association_link"
    ).fetchall() == [("PRE", "SOU")]


def test_a_public_split_uses_that_station_for_both_ends(built):
    """The usual shape, where nothing changes."""
    connection, _ = built([association("BASE01", "PORT02")])

    assert connection.execute(
        "select distinct base_unlock_crs, assoc_board_crs from association_link"
    ).fetchall() == [("SOU", "SOU")]
