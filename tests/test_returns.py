"""Return tickets: which kind, and when you may come back.

The arithmetic here is not a guess — two validity codes in the real feed pin it,
and each has its own test below. Everything else follows from those two.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rail.model.returns import (
    build_ticket_validity,
    return_window,
    return_windows,
    returnable_on,
)

TODAY = dt.date.today()
FOREVER = dt.date(2999, 12, 31)
LONG_AGO = dt.date(1991, 3, 1)

#: A Wednesday, which is what makes validity code 49 testable.
WEDNESDAY = dt.date(2026, 8, 5)
SATURDAY = dt.date(2026, 8, 8)

TVL_SCHEMA = pa.schema([
    ("validity_code", pa.string()), ("start_date", pa.date32()),
    ("end_date", pa.date32()), ("description", pa.string()),
    ("out_days", pa.int64()), ("out_months", pa.int64()),
    ("ret_days", pa.int64()), ("ret_months", pa.int64()),
    ("ret_after_days", pa.int64()), ("ret_after_months", pa.int64()),
    ("ret_after_day", pa.string()),
    ("break_out", pa.bool_()), ("break_in", pa.bool_()),
    ("out_description", pa.string()), ("rtn_description", pa.string()),
])


def validity(code, description, *, out_days=1, out_months=0, ret_days=0,
             ret_months=0, ret_after_days=0, ret_after_months=0,
             ret_after_day=None, break_out=True, break_in=True,
             out_description=None, rtn_description=None, start=LONG_AGO,
             end=FOREVER):
    return {"validity_code": code, "start_date": start, "end_date": end,
            "description": description, "out_days": out_days,
            "out_months": out_months, "ret_days": ret_days,
            "ret_months": ret_months, "ret_after_days": ret_after_days,
            "ret_after_months": ret_after_months, "ret_after_day": ret_after_day,
            "break_out": break_out, "break_in": break_in,
            "out_description": out_description or description,
            "rtn_description": rtn_description or description}


@pytest.fixture
def world(tmp_path):
    """Ticket types and validity codes, built as `rail build` builds them."""

    def _build(*, validities, tickets):
        directory = tmp_path / "fares"
        directory.mkdir(exist_ok=True)
        pq.write_table(pa.Table.from_pylist(list(validities), schema=TVL_SCHEMA),
                       directory / "ticket_validity.parquet")

        c = duckdb.connect()
        c.execute("create table ticket_type_current "
                  "(ticket_code varchar, description varchar, tkt_type varchar, "
                  " validity_code varchar, is_walk_up boolean)")
        for code, description, tkt_type, validity_code in tickets:
            c.execute("insert into ticket_type_current values (?, ?, ?, ?, true)",
                      [code, description, tkt_type, validity_code])
        build_ticket_validity(c, directory)
        return c

    return _build


# --- the two codes that pin the arithmetic -----------------------------------


def test_a_day_return_comes_back_the_same_day(world):
    """Validity 06 "ON DATE SHOWN" is an ordinary Day Return with ret_days = 1.

    So `ret_days` counts days *inclusive of the outward day*. Read as a count of
    days after it, a Day Return would be valid for two.
    """
    connection = world(
        validities=[validity("06", "ON DATE SHOWN", ret_days=1)],
        tickets=[("CDR", "OFF-PEAK DAY R", "R", "06")],
    )
    window = return_window(connection, "CDR", WEDNESDAY)

    assert window.kind == "same_day"
    assert (window.earliest, window.latest) == (WEDNESDAY, WEDNESDAY)


def test_the_five_day_return_matches_the_feeds_own_prose(world):
    """Validity 49 states its rule twice, and the two must agree.

    Numerically it is ret_days = 5, ret_after_days = 4. In prose it is
    out_description 'OUT ON WED' and rtn_description 'RTN ON SUN'. Wednesday to
    Sunday is four days and it is both the first and the last permitted return,
    so this single code fixes both rules at once: earliest is
    `outward + ret_after_days`, latest is `outward + ret_days - 1`. Move either
    by a day and the prose stops matching.
    """
    connection = world(
        validities=[validity("49", "FIVE DAY RTN", ret_days=5, ret_after_days=4,
                             out_description="OUT ON WED",
                             rtn_description="RTN ON SUN")],
        tickets=[("BOR", "BUS OPEN RETURN", "R", "49")],
    )
    window = return_window(connection, "BOR", WEDNESDAY)

    sunday = dt.date(2026, 8, 9)
    assert (window.earliest, window.latest) == (sunday, sunday)
    assert window.note == "RTN ON SUN"


# --- the weekend rule --------------------------------------------------------


def test_the_saturday_night_rule_permits_a_sunday_return(world):
    """Validity 98 carries ret_after_day = 'SA' and is the old rule that you
    must stay away over a Saturday night.

    RSPS5045 4.7.2 field 11 says return travel is not permitted until the day
    specified has *passed*, so the earliest return is the Sunday. This is the
    only one of the three codes whose real-world behaviour is documented outside
    the feed, which is why it decides the reading: returning *on* the Saturday
    would mean not having stayed the Saturday night.
    """
    connection = world(
        validities=[validity("98", "AS ADVERTISED", ret_months=1,
                             ret_after_day="SA")],
        tickets=[("SVS", "SAVER RETURN", "R", "98")],
    )
    window = return_window(connection, "SVS", WEDNESDAY)

    assert window.earliest == dt.date(2026, 8, 9)   # the Sunday, not the Saturday
    assert window.after_weekday == "SA"


def test_a_weekend_return_is_unusable_on_a_midweek_outward(world):
    """The whole point of `ret_after_day`, and why an empty window is kept.

    Validity 59 "WKND 3 Days" says: out one day, back within three, and not
    until Sunday has passed. Leave on a Wednesday and you must be back by the
    Friday but may not travel until the Monday — nothing satisfies both, so the
    ticket is not for that outward date. Reading the days alone makes it look
    like an ordinary three-day return valid any day of the week, and clamping
    the window so it is never empty would sell exactly that.
    """
    connection = world(
        validities=[validity("59", "WKND 3 Days", ret_days=3, ret_after_day="SU")],
        tickets=[("WKE", "Long Wkend Rtn", "R", "59")],
    )
    midweek = return_window(connection, "WKE", WEDNESDAY)
    weekend = return_window(connection, "WKE", SATURDAY)

    assert midweek.is_empty
    assert not midweek.covers(dt.date(2026, 8, 10))
    # Out on the Saturday, back on the Monday: the shape the product is for.
    assert not weekend.is_empty
    assert weekend.earliest == dt.date(2026, 8, 10)


# --- what decides the shape --------------------------------------------------


def test_the_ticket_type_decides_a_return_not_the_validity_record(world):
    """185 current single ticket types point at a validity code carrying a
    return period, because the same code also serves returns. Reading the
    validity alone would call every one of them a return.
    """
    connection = world(
        validities=[validity("81", "ON DATE SHOWN", ret_days=1)],
        tickets=[("1U2", "OFF PK - 1ST PK", "S", "81"),
                 ("SDR", "ANYTIME DAY R", "R", "81")],
    )

    assert return_window(connection, "1U2", WEDNESDAY) is None
    assert return_window(connection, "SDR", WEDNESDAY) is not None


def test_a_season_is_not_treated_as_a_return(world):
    connection = world(
        validities=[validity("00", "(USE SEASON)", out_days=0)],
        tickets=[("TRV", "TRAVELCARD", "N", "00")],
    )

    assert return_window(connection, "TRV", WEDNESDAY) is None


def test_months_are_a_calendar_offset(world):
    """A one-month return runs to the same day of the next month. Days are
    inclusive of the outward day and months are not, which follows from the Day
    Return above: there is no day zero for days, and no inclusive reading for
    months that makes sense."""
    connection = world(
        validities=[validity("13", "1DYOUT 1MTHRTN", ret_months=1)],
        tickets=[("SVR", "OFF-PEAK R", "R", "13")],
    )
    window = return_window(connection, "SVR", WEDNESDAY)

    assert window.kind == "period"
    assert (window.earliest, window.latest) == (WEDNESDAY, dt.date(2026, 9, 5))


def test_a_month_offset_clamps_to_the_end_of_a_short_month(world):
    connection = world(
        validities=[validity("13", "1DYOUT 1MTHRTN", ret_months=1)],
        tickets=[("SVR", "OFF-PEAK R", "R", "13")],
    )
    window = return_window(connection, "SVR", dt.date(2026, 1, 31))

    assert window.latest == dt.date(2026, 2, 28)


def test_a_return_with_no_return_period_falls_back_to_the_outward_window(world):
    """`OG8` "Open Golf 8 Day" is the only walk-up case: validity 95 gives eight
    outward days and no return period, the eight days covering both legs."""
    connection = world(
        validities=[validity("95", "EIGHT DAYS", out_days=8)],
        tickets=[("OG8", "Open Golf 8 Day", "R", "95")],
    )
    window = return_window(connection, "OG8", WEDNESDAY)

    assert window.inferred
    assert (window.earliest, window.latest) == (WEDNESDAY, dt.date(2026, 8, 12))


def test_a_prose_note_that_only_repeats_the_description_is_dropped(world):
    """The prose is worth surfacing when it says something the numbers do not —
    'RTN ON SUN', 'BEFORE 1200'. Repeating the code's own name is noise."""
    connection = world(
        validities=[validity("13", "1DYOUT 1MTHRTN", ret_months=1)],
        tickets=[("SVR", "OFF-PEAK R", "R", "13")],
    )

    assert return_window(connection, "SVR", WEDNESDAY).note is None


# --- the batched form --------------------------------------------------------


def test_returnable_on_selects_only_tickets_that_permit_the_date(world):
    connection = world(
        validities=[validity("06", "ON DATE SHOWN", ret_days=1),
                    validity("13", "1DYOUT 1MTHRTN", ret_months=1)],
        tickets=[("CDR", "OFF-PEAK DAY R", "R", "06"),
                 ("SVR", "OFF-PEAK R", "R", "13"),
                 ("CDS", "OFF-PEAK DAY S", "S", "06")],
    )

    same_day = returnable_on(connection, WEDNESDAY, WEDNESDAY)
    next_week = returnable_on(connection, WEDNESDAY, dt.date(2026, 8, 12))

    assert same_day == {"CDR", "SVR"}
    # The day return cannot come back a week later; the open return can.
    assert next_week == {"SVR"}
    # Singles are absent rather than excluded — they answer a different
    # question, and two of them are often the cheaper answer.
    assert "CDS" not in same_day


def test_the_batched_windows_match_the_single_lookup(world):
    """Two entry points, one implementation. Expressing the rules a second time
    in SQL would be faster and would eventually disagree."""
    connection = world(
        validities=[validity("49", "FIVE DAY RTN", ret_days=5, ret_after_days=4,
                             rtn_description="RTN ON SUN"),
                    validity("59", "WKND 3 Days", ret_days=3, ret_after_day="SU")],
        tickets=[("BOR", "BUS OPEN RETURN", "R", "49"),
                 ("WKE", "Long Wkend Rtn", "R", "59")],
    )
    batched = return_windows(connection, WEDNESDAY)

    for code in ("BOR", "WKE"):
        assert batched[code] == return_window(connection, code, WEDNESDAY)


def test_the_kinds_are_counted_at_build_time(world):
    counts = None
    connection = world(
        validities=[validity("06", "ON DATE SHOWN", ret_days=1),
                    validity("13", "1DYOUT 1MTHRTN", ret_months=1),
                    validity("59", "WKND 3 Days", ret_days=3, ret_after_day="SU")],
        tickets=[("CDR", "OFF-PEAK DAY R", "R", "06"),
                 ("SVR", "OFF-PEAK R", "R", "13"),
                 ("WKE", "Long Wkend Rtn", "R", "59"),
                 ("CDS", "OFF-PEAK DAY S", "S", "06")],
    )
    counts = connection.execute(
        "select return_kind, count(*) from ticket_return_kind group by 1"
    ).fetchall()

    assert dict(counts) == {"same_day": 1, "period": 1, "multi_day": 1, "none": 1}


def test_a_validity_code_that_has_expired_is_not_current(world):
    """TVL is a version history like everything else in these feeds."""
    connection = world(
        validities=[validity("06", "ON DATE SHOWN", ret_days=1,
                             start=dt.date(1991, 1, 1), end=dt.date(2000, 1, 1))],
        tickets=[("CDR", "OFF-PEAK DAY R", "R", "06")],
    )

    assert connection.execute(
        "select count(*) from ticket_validity_current").fetchone()[0] == 0
