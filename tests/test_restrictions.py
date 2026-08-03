"""Time restrictions.

A restriction code names time bands during which a fare may *not* be used. The
awkward parts, all pinned down below: the feed carries both the current and the
next version of the restrictions and the travel date picks between them; the
date ranges are MMDD rather than DDMM; and a band's dates come from its own TD
records where it has them and from the restriction header otherwise.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rail.model.restrictions import (
    applicable_bands,
    build_restrictions,
    marker_for,
    restriction_notes,
)

CURRENT_START, CURRENT_END = dt.date(2026, 7, 5), dt.date(2026, 10, 31)
FUTURE_START, FUTURE_END = dt.date(2026, 11, 1), dt.date(2999, 12, 31)

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

RD_SCHEMA = pa.schema([
    ("cf_mkr", pa.string()), ("start_date", pa.date32()), ("end_date", pa.date32()),
])
TR_SCHEMA = pa.schema([
    ("cf_mkr", pa.string()), ("restriction_code", pa.string()),
    ("sequence_no", pa.string()), ("out_ret", pa.string()),
    ("time_from", pa.int32()), ("time_to", pa.int32()),
    ("arr_dep_via", pa.string()), ("location", pa.string()),
    # RSPS5045 4.19.8 field 13: 'Y' charges a minimum fare rather than
    # barring the fare outright.
    ("min_fare_flag", pa.bool_()),
])
TD_SCHEMA = pa.schema([
    ("cf_mkr", pa.string()), ("restriction_code", pa.string()),
    ("sequence_no", pa.string()), ("out_ret", pa.string()),
    ("date_from", pa.string()), ("date_to", pa.string()),
    *[(d, pa.bool_()) for d in DAYS],
])
#: 4.19.3. Only `change_ind` matters to the model - whether a change of trains
#: is allowed at all - and `describe_restriction` reads the rest.
RH_SCHEMA = pa.schema([
    ("cf_mkr", pa.string()), ("restriction_code", pa.string()),
    ("description", pa.string()), ("desc_out", pa.string()),
    ("desc_ret", pa.string()), ("type_out", pa.string()),
    ("type_ret", pa.string()), ("change_ind", pa.bool_()),
])
HD_SCHEMA = pa.schema([
    ("cf_mkr", pa.string()), ("restriction_code", pa.string()),
    ("date_from", pa.string()), ("date_to", pa.string()),
    *[(d, pa.bool_()) for d in DAYS],
])
#: 4.19.10 field 7. A band with TT records applies only to those operators'
#: trains; one with none applies to everybody's.
TT_SCHEMA = pa.schema([
    ("cf_mkr", pa.string()), ("restriction_code", pa.string()),
    ("sequence_no", pa.string()), ("out_ret", pa.string()),
    ("toc_code", pa.string()),
])


def weekdays_only(**overrides):
    days = {d: d not in ("saturday", "sunday") for d in DAYS}
    days.update(overrides)
    return days


def band(code, seq, *, frm, to, sense="D", location="EUS", marker="C",
         out_ret="O", minimum_fare=False):
    return {"cf_mkr": marker, "restriction_code": code, "sequence_no": seq,
            "out_ret": out_ret, "time_from": frm, "time_to": to,
            "arr_dep_via": sense, "location": location,
            "min_fare_flag": minimum_fare}


@pytest.fixture
def restrictions(tmp_path):
    def _build(*, bands, header_dates=(), band_dates=(), headers=(), band_tocs=()):
        directory = tmp_path / "fares"
        directory.mkdir(exist_ok=True)
        pq.write_table(
            pa.Table.from_pylist(
                [{"cf_mkr": "C", "start_date": CURRENT_START, "end_date": CURRENT_END},
                 {"cf_mkr": "F", "start_date": FUTURE_START, "end_date": FUTURE_END}],
                schema=RD_SCHEMA),
            directory / "restriction_dates.parquet")
        pq.write_table(pa.Table.from_pylist(list(bands), schema=TR_SCHEMA),
                       directory / "restriction_time.parquet")
        pq.write_table(pa.Table.from_pylist(list(band_dates), schema=TD_SCHEMA),
                       directory / "restriction_time_date.parquet")
        pq.write_table(pa.Table.from_pylist(list(header_dates), schema=HD_SCHEMA),
                       directory / "restriction_header_date.parquet")
        pq.write_table(pa.Table.from_pylist(list(headers), schema=RH_SCHEMA),
                       directory / "restriction_header.parquet")
        pq.write_table(pa.Table.from_pylist(list(band_tocs), schema=TT_SCHEMA),
                       directory / "restriction_time_toc.parquet")

        connection = duckdb.connect()
        build_restrictions(connection, directory)
        return connection

    return _build


def codes_on(connection, date):
    return {row[0] for row in applicable_bands(connection, date)}


# --- which set of restrictions applies ---------------------------------------


def test_the_travel_date_chooses_the_current_or_future_restriction_set(restrictions):
    connection = restrictions(bands=[band("0W", "0001", frm=270, to=565)])

    assert marker_for(connection, dt.date(2026, 8, 4)) == "C"
    assert marker_for(connection, dt.date(2026, 12, 1)) == "F"


def test_a_date_beyond_both_windows_reads_the_future_set(restrictions):
    connection = restrictions(bands=[band("0W", "0001", frm=270, to=565)])
    assert marker_for(connection, dt.date(3000, 1, 1)) == "F"


def test_bands_from_the_other_set_are_not_returned(restrictions):
    connection = restrictions(
        bands=[band("0W", "0001", frm=270, to=565, marker="C"),
               band("9Z", "0001", frm=270, to=565, marker="F")],
        header_dates=[
            {"cf_mkr": "C", "restriction_code": "0W", "date_from": "0101",
             "date_to": "1231", **weekdays_only()},
            {"cf_mkr": "F", "restriction_code": "9Z", "date_from": "0101",
             "date_to": "1231", **weekdays_only()},
        ],
    )

    assert codes_on(connection, dt.date(2026, 8, 4)) == {"0W"}   # current
    assert codes_on(connection, dt.date(2026, 12, 1)) == {"9Z"}  # future


# --- when a band bites -------------------------------------------------------


def test_peak_restrictions_do_not_apply_at_weekends(restrictions):
    connection = restrictions(
        bands=[band("0W", "0001", frm=270, to=565)],
        header_dates=[{"cf_mkr": "C", "restriction_code": "0W", "date_from": "0101",
                       "date_to": "1231", **weekdays_only()}],
    )

    assert codes_on(connection, dt.date(2026, 8, 4)) == {"0W"}  # Tuesday
    assert codes_on(connection, dt.date(2026, 8, 1)) == set()   # Saturday
    assert codes_on(connection, dt.date(2026, 8, 2)) == set()   # Sunday


def test_date_ranges_are_mmdd_not_ddmm(restrictions):
    """0104-0402 is 4 January to 2 April. Read as DDMM it would be nonsense.

    Restriction 0W in the real feed runs 0104-0402, 0407-0501, 0505-0522,
    0526-0828, 0901-1223 - as MMDD the gaps are Easter, both May bank holidays,
    the August bank holiday and Christmas, which is when peak restrictions lift.
    """
    connection = restrictions(
        bands=[band("0W", "0001", frm=270, to=565)],
        header_dates=[{"cf_mkr": "C", "restriction_code": "0W", "date_from": "0901",
                       "date_to": "1223", **weekdays_only()}],
    )

    assert codes_on(connection, dt.date(2026, 9, 15)) == {"0W"}  # inside 1 Sep - 23 Dec
    assert codes_on(connection, dt.date(2026, 8, 25)) == set()   # before it starts
    assert codes_on(connection, dt.date(2026, 12, 28)) == set()  # Christmas gap


def test_a_band_with_its_own_dates_ignores_the_header_dates(restrictions):
    connection = restrictions(
        bands=[band("0W", "0001", frm=270, to=565)],
        header_dates=[{"cf_mkr": "C", "restriction_code": "0W", "date_from": "0101",
                       "date_to": "1231", **weekdays_only()}],
        band_dates=[{"cf_mkr": "C", "restriction_code": "0W", "sequence_no": "0001",
                     "out_ret": "O", "date_from": "0929", "date_to": "1003",
                     **weekdays_only()}],
    )

    assert codes_on(connection, dt.date(2026, 9, 30)) == {"0W"}  # inside the band's own window
    assert codes_on(connection, dt.date(2026, 8, 4)) == set()    # header says yes, band says no


def test_a_band_without_its_own_dates_falls_back_to_the_header(restrictions):
    connection = restrictions(
        bands=[band("0W", "0001", frm=270, to=565),
               band("0W", "0002", frm=901, to=1124)],
        header_dates=[{"cf_mkr": "C", "restriction_code": "0W", "date_from": "0101",
                       "date_to": "1231", **weekdays_only()}],
        # Only sequence 0001 has its own dates.
        band_dates=[{"cf_mkr": "C", "restriction_code": "0W", "sequence_no": "0001",
                     "out_ret": "O", "date_from": "0929", "date_to": "1003",
                     **weekdays_only()}],
    )
    august = applicable_bands(connection, dt.date(2026, 8, 4))

    # 0002 still applies from the header; 0001 does not, being outside its window.
    assert {row[2] for row in august} == {901}


def test_a_band_with_no_dates_at_all_never_applies(restrictions):
    """No header and no band dates means nothing says when it is in force."""
    connection = restrictions(bands=[band("0W", "0001", frm=270, to=565)])
    assert codes_on(connection, dt.date(2026, 8, 4)) == set()


# --- what a band says --------------------------------------------------------


def test_a_band_carries_the_operators_it_is_qualified_to(restrictions):
    """RSPS5045 4.19.10 field 7, sorted so the set is a stable key."""
    connection = restrictions(
        bands=[band("RD", "0001", frm=1, to=1439, location=None)],
        header_dates=[{"cf_mkr": "C", "restriction_code": "RD", "date_from": "0101",
                       "date_to": "1231", **weekdays_only(saturday=True,
                                                          sunday=True)}],
        band_tocs=[{"cf_mkr": "C", "restriction_code": "RD", "sequence_no": "0001",
                    "out_ret": "O", "toc_code": toc} for toc in ("VT", "GR")],
    )
    assert applicable_bands(connection, dt.date(2026, 8, 4))[0][-1] == ["GR", "VT"]


def test_two_bands_of_one_shape_keep_their_own_operators(restrictions):
    """The `distinct` trap: bands are collapsed by shape, not by sequence.

    Two bands identical in time, sense and location but naming different
    operators must stay two rows. Collapsed, one's qualifier would govern the
    other - and since a band with no qualifier at all is the common case, the
    collapse would silently hand an unconditional bar someone else's operators.
    """
    connection = restrictions(
        bands=[band("RD", "0001", frm=1, to=1439, location=None),
               band("RD", "0002", frm=1, to=1439, location=None),
               band("RD", "0003", frm=1, to=1439, location=None)],
        header_dates=[{"cf_mkr": "C", "restriction_code": "RD", "date_from": "0101",
                       "date_to": "1231", **weekdays_only()}],
        band_tocs=[{"cf_mkr": "C", "restriction_code": "RD", "sequence_no": "0001",
                    "out_ret": "O", "toc_code": "GR"},
                   {"cf_mkr": "C", "restriction_code": "RD", "sequence_no": "0002",
                    "out_ret": "O", "toc_code": "XC"}],
    )
    rows = applicable_bands(connection, dt.date(2026, 8, 4))

    # Three bands of one shape: two qualified differently, one not at all.
    assert sorted(str(row[-1]) for row in rows) == ["None", "['GR']", "['XC']"]


def test_a_band_carries_its_location_and_sense(restrictions):
    connection = restrictions(
        bands=[band("1C", "0001", frm=270, to=599, sense="A", location="KGX")],
        header_dates=[{"cf_mkr": "C", "restriction_code": "1C", "date_from": "0101",
                       "date_to": "1231", **weekdays_only()}],
    )
    code, out_ret, frm, to, sense, location, minimum_fare, tocs = applicable_bands(
        connection, dt.date(2026, 8, 4)
    )[0]
    # No TT records, so the band applies to every operator's trains.
    assert tocs is None

    assert (code, out_ret, sense, location) == ("1C", "O", "A", "KGX")
    # Not a minimum-fare band, so it bars the fare rather than repricing it.
    assert minimum_fare is False
    # 04:30 to 09:59 - the morning peak arrival ban into King's Cross.
    assert (frm, to) == (270, 599)


# --- the header pool, for a consumer that wants a line of text ---------------
#
# `describe_restriction` renders every band of one code and comes to 18 KB.
# `restriction_notes` is the header alone, for every code at once, and adds one
# derived fact: whether the restriction has anything to say about the way home.


def test_the_pool_carries_the_operators_own_prose(restrictions, tmp_path):
    connection = restrictions(
        bands=[band("9I", "0001", frm=270, to=565)],
        headers=[{"cf_mkr": "C", "restriction_code": "9I", "description": "OFF-PEAK",
                  "desc_out": "NO DEP FROM EUS PRE 0926", "desc_ret": "NO ARR IN LDN PRE 1130",
                  "type_out": "N", "type_ret": "N", "change_ind": True}],
    )
    note = restriction_notes(connection, dt.date(2026, 8, 4), tmp_path / "fares")["9I"]

    assert note.description == "OFF-PEAK"
    assert note.note_out == "NO DEP FROM EUS PRE 0926"
    assert note.note_return == "NO ARR IN LDN PRE 1130"


def test_a_restriction_bars_the_return_only_when_it_has_a_return_band(
        restrictions, tmp_path):
    """The bands decide it, not the prose and not the ticket's name.

    `1X` "IRELAND VIA CAIRNRYAN" says "VALID AT ANYTIME" in both notes and
    carries no bands at all; `9I` says much the same thing twice over in prose
    and carries 24 return bands. Only one of them may be shown to a passenger
    as unconditional on the way back.
    """
    headers = [
        {"cf_mkr": "C", "restriction_code": code, "description": "", "desc_out": "",
         "desc_ret": "", "type_out": "N", "type_ret": "N", "change_ind": True}
        for code in ("9I", "1X", "OO")
    ]
    connection = restrictions(
        bands=[
            band("9I", "0001", frm=270, to=565),
            band("9I", "0002", frm=270, to=680, out_ret="R"),
            # Outward bands only - a morning peak going out, nothing coming back.
            band("OO", "0001", frm=270, to=565),
            # `1X` gets no bands whatsoever.
        ],
        headers=headers,
    )
    notes = restriction_notes(connection, dt.date(2026, 8, 4), tmp_path / "fares")

    assert notes["9I"].bars_return is True
    assert notes["OO"].bars_return is False
    assert notes["1X"].bars_return is False


def test_the_pool_reads_the_marker_the_travel_date_selects(restrictions, tmp_path):
    """The feed ships the current restrictions and the next set side by side.

    A pool built for the wrong marker would put November's prose beside an
    August fare, which reads as plausible and is wrong.
    """
    connection = restrictions(
        bands=[band("9I", "0001", frm=270, to=565, out_ret="R", marker="F")],
        headers=[
            {"cf_mkr": "C", "restriction_code": "9I", "description": "NOW",
             "desc_out": "", "desc_ret": "", "type_out": "N", "type_ret": "N",
             "change_ind": True},
            {"cf_mkr": "F", "restriction_code": "9I", "description": "NEXT",
             "desc_out": "", "desc_ret": "", "type_out": "N", "type_ret": "N",
             "change_ind": True},
        ],
    )
    fares = tmp_path / "fares"

    current = restriction_notes(connection, dt.date(2026, 8, 4), fares)["9I"]
    future = restriction_notes(connection, dt.date(2026, 11, 15), fares)["9I"]

    assert (current.description, current.bars_return) == ("NOW", False)
    # The only return band in the file belongs to the future set.
    assert (future.description, future.bars_return) == ("NEXT", True)
