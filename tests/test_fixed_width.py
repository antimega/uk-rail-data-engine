"""Tests for the fixed-width reader.

These verify the *machinery*: slicing, type conversion, null handling,
multi-record dispatch and schema unioning. They do not prove the field offsets
in ``rail.layouts`` match the real feed - nothing short of real data can. That
check lives in ``rail validate``, which cross-references parsed values against
each other (every stop location must exist as a TIPLOC, every CRS must be three
letters, and so on).
"""

from __future__ import annotations

import datetime as dt

import pytest

from rail.layouts.spec import FileSpec, Kind, RecordSpec, fields
from rail.parse import read_fixed_width


def make_line(width: int, **placements: str) -> bytes:
    """Build a fixed-width line: ``make_line(20, p0="BS", p3="ABC")``."""
    buffer = bytearray(b" " * width)
    for key, value in placements.items():
        start = int(key[1:])
        encoded = value.encode("latin-1")
        buffer[start : start + len(encoded)] = encoded
    return bytes(buffer)


def write(tmp_path, name, lines):
    path = tmp_path / name
    path.write_bytes(b"\r\n".join(lines) + b"\r\n")
    return path


SIMPLE = FileSpec(
    extension="TST",
    feed="test",
    key_start=0,
    key_length=1,
    records={
        "A": RecordSpec(
            "thing",
            fields(
                ("code", 1, 4),
                ("count", 5, 3, Kind.INT),
                ("end_date", 8, 8, Kind.DATE),
                ("flag", 16, 1, Kind.BOOL),
            ),
        )
    },
    ignore=("Z",),
)


def test_text_int_and_bool(tmp_path):
    path = write(
        tmp_path,
        "a.TST",
        [make_line(20, p0="A", p1="EUS", p5="042", p8="01012026", p16="Y")],
    )
    tables, _ = read_fixed_width(path, SIMPLE)
    row = tables["thing"].to_pylist()[0]

    assert row["code"] == "EUS"
    assert row["count"] == 42
    assert row["end_date"] == dt.date(2026, 1, 1)
    assert row["flag"] is True
    assert row["record_type"] == "A"


def test_blank_fields_become_null(tmp_path):
    path = write(tmp_path, "a.TST", [make_line(20, p0="A")])
    row = read_fixed_width(path, SIMPLE)[0]["thing"].to_pylist()[0]

    assert row["code"] is None
    assert row["count"] is None
    assert row["end_date"] is None
    assert row["flag"] is False  # a missing flag is false, not unknown


def test_open_ended_sentinel_is_kept_as_a_real_date(tmp_path):
    """31122999 stays a date so `d BETWEEN start AND end` works unchanged."""
    path = write(tmp_path, "a.TST", [make_line(20, p0="A", p8="31122999")])
    row = read_fixed_width(path, SIMPLE)[0]["thing"].to_pylist()[0]

    assert row["end_date"] == dt.date(2999, 12, 31)


@pytest.mark.parametrize("raw", ["99999999", "00000000", "31022026", "abcdefgh"])
def test_impossible_dates_null_out_without_raising(tmp_path, raw):
    path = write(tmp_path, "a.TST", [make_line(20, p0="A", p8=raw)])
    tables, stats = read_fixed_width(path, SIMPLE)

    assert tables["thing"].to_pylist()[0]["end_date"] is None
    assert stats.null_coercions["thing.end_date"] == 1


def test_comments_blanks_ignored_and_unknown_records_counted(tmp_path):
    path = write(
        tmp_path,
        "a.TST",
        [
            b"/!! Start of file",
            make_line(20, p0="A", p1="EUS"),
            make_line(20, p0="Z"),  # declared as ignorable
            make_line(20, p0="Q"),  # genuinely unexpected
            b"",
            b"/!! End of file",
        ],
    )
    tables, stats = read_fixed_width(path, SIMPLE)

    assert tables["thing"].num_rows == 1
    assert stats.comment_lines == 2
    assert stats.blank_lines == 1
    assert stats.ignored_records["Z"] == 1
    assert stats.unknown_records["Q"] == 1


# --- time and date conventions ----------------------------------------------

TIMES = FileSpec(
    extension="TIM",
    feed="test",
    single=RecordSpec(
        "times",
        fields(
            ("public", 0, 4, Kind.PUBLIC_TIME),
            ("working", 4, 5, Kind.WORKING_TIME),
            ("short", 9, 6, Kind.SHORT_DATE),
        ),
    ),
)


def test_public_time_is_minutes_and_0000_means_no_public_time(tmp_path):
    path = write(tmp_path, "a.TIM", [make_line(20, p0="0730"), make_line(20, p0="0000")])
    rows = read_fixed_width(path, TIMES)[0]["times"].to_pylist()

    assert rows[0]["public"] == 7 * 60 + 30
    # CIF uses 0000 for "no public time" - a passing point, not midnight.
    assert rows[1]["public"] is None


def test_working_time_keeps_the_half_minute(tmp_path):
    path = write(
        tmp_path,
        "a.TIM",
        [
            make_line(20, p4="0730H"),
            make_line(20, p4="0730 "),
            make_line(20, p4="0000H"),
            make_line(20, p4="0000 "),
        ],
    )
    rows = read_fixed_width(path, TIMES)[0]["times"].to_pylist()

    assert rows[0]["working"] == 7 * 3600 + 30 * 60 + 30
    assert rows[1]["working"] == 7 * 3600 + 30 * 60
    assert rows[2]["working"] == 30
    assert rows[3]["working"] is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("260115", dt.date(2026, 1, 15)),
        ("591231", dt.date(2059, 12, 31)),  # window ends at 2059
        ("600101", dt.date(1960, 1, 1)),  # and wraps to 1960
    ],
)
def test_cif_short_date_century_window(tmp_path, raw, expected):
    path = write(tmp_path, "a.TIM", [make_line(20, p9=raw)])
    assert read_fixed_width(path, TIMES)[0]["times"].to_pylist()[0]["short"] == expected


def test_ztr_open_end_is_normalised_without_changing_mca_dates(tmp_path):
    from rail.layouts.timetable import MCA, ZTR

    line = make_line(
        80, p0="BS", p3="Z00001", p9="260625", p15="991231",
        p21="1111100", p29="B", p79="P",
    )

    ztr = read_fixed_width(write(tmp_path, "a.ZTR", [line]), ZTR)[0]
    mca = read_fixed_width(write(tmp_path, "a.MCA", [line]), MCA)[0]

    assert ztr["z_schedule"].to_pylist()[0]["runs_to"] == dt.date(2999, 12, 31)
    assert mca["schedule"].to_pylist()[0]["runs_to"] == dt.date(1999, 12, 31)


# --- multi-record dispatch ---------------------------------------------------


def test_differing_record_layouts_union_into_one_table(tmp_path):
    """LO/LI/LT have different fields but share the stop_time table."""
    from rail.layouts.timetable import MCA

    path = write(
        tmp_path,
        "a.MCA",
        [
            make_line(80, p0="LO", p2="EUSTON ", p10="0730 ", p15="0730", p29="TB"),
            make_line(80, p0="LI", p2="WATFDJ ", p25="0745", p29="0746", p42="T "),
            make_line(80, p0="LT", p2="BHAMNWS", p15="0900", p25="TF"),
        ],
    )
    tables, stats = read_fixed_width(path, MCA)
    stops = tables["stop_time"].to_pylist()

    assert [s["record_type"] for s in stops] == ["LO", "LI", "LT"]
    # The origin has no arrival; the terminus has no departure.
    assert stops[0]["public_departure"] == 450 and stops[0]["public_arrival"] is None
    assert stops[1]["public_arrival"] == 465 and stops[1]["public_departure"] == 466
    assert stops[2]["public_arrival"] == 540 and stops[2]["public_departure"] is None
    assert stops[0]["location"] == "EUSTON"
    assert stats.records_by_type["LI"] == 1


def test_line_numbers_let_stops_be_traced_back_to_their_schedule(tmp_path):
    """CIF is positional; bucketing by record type would otherwise lose that.

    Each BS owns the LO/LI/LT lines that follow it until the next BS. Without a
    file position on every row there is nothing joining 7M stops to a train.
    """
    from rail.layouts.timetable import MCA

    path = write(
        tmp_path,
        "a.MCA",
        [
            make_line(80, p0="BS", p3="AAAAAA", p9="260504", p15="261212"),
            make_line(80, p0="LO", p2="EUSTON ", p15="0730"),
            make_line(80, p0="LT", p2="BHAMNWS", p15="0900"),
            make_line(80, p0="BS", p3="BBBBBB", p9="260504", p15="261212"),
            make_line(80, p0="LO", p2="KNGX   ", p15="1000"),
            make_line(80, p0="LT", p2="YORK   ", p15="1200"),
        ],
    )
    tables, _ = read_fixed_width(path, MCA)
    schedules = tables["schedule"].to_pylist()

    # Rows come out grouped by record type, not in file order - LO and LT are
    # separate specs. Consumers must sort by line_no, which is the point of it.
    stops = sorted(tables["stop_time"].to_pylist(), key=lambda s: s["line_no"])

    def owner(stop):
        earlier = [s for s in schedules if s["line_no"] < stop["line_no"]]
        return max(earlier, key=lambda s: s["line_no"])["train_uid"]

    assert [s["line_no"] for s in stops] == [1, 2, 4, 5]
    assert [owner(s) for s in stops] == ["AAAAAA", "AAAAAA", "BBBBBB", "BBBBBB"]
    assert [s["location"] for s in stops] == ["EUSTON", "BHAMNWS", "KNGX", "YORK"]


def test_mca_schedule_fields_land_at_documented_offsets(tmp_path):
    from rail.layouts.timetable import MCA

    path = write(
        tmp_path,
        "a.MCA",
        [
            make_line(
                80,
                p0="BS",
                p2="N",
                p3="C12345",
                p9="260504",
                p15="261212",
                p21="1111100",
                p29="P",
                p30="OO",
                p79="O",
            )
        ],
    )
    row = read_fixed_width(path, MCA)[0]["schedule"].to_pylist()[0]

    assert row["train_uid"] == "C12345"
    assert row["runs_from"] == dt.date(2026, 5, 4)
    assert row["runs_to"] == dt.date(2026, 12, 12)
    assert [row["monday"], row["friday"], row["saturday"], row["sunday"]] == [
        True,
        True,
        False,
        False,
    ]
    assert row["train_status"] == "P"
    assert row["train_category"] == "OO"
    assert row["stp_indicator"] == "O"


def test_ffl_flow_and_fare_records_split_into_two_tables(tmp_path):
    """Byte 0 is the update marker and byte 1 the record type - not the reverse.

    The flow line below is taken verbatim from RJFAF833, which is what settled
    the question; a synthetic fixture had happily encoded the wrong order.
    """
    from rail.layouts.fares import FFL

    path = write(
        tmp_path,
        "a.FFL",
        [
            b"RF0027003201000000AS3112299902032025ATO01Y0000020",
            make_line(22, p0="R", p1="T", p2="0000020", p9="SDS", p12="00003450"),
        ],
    )
    tables, _ = read_fixed_width(path, FFL)
    flow = tables["flow"].to_pylist()[0]
    fare = tables["fare"].to_pylist()[0]

    assert flow["update_marker"] == "R"  # full refresh
    assert flow["origin_code"] == "0027"
    assert flow["destination_code"] == "0032"
    assert flow["route_code"] == "01000"
    assert flow["direction"] == "S"  # single direction, not reversible
    assert flow["end_date"] == dt.date(2999, 12, 31)
    assert flow["start_date"] == dt.date(2025, 3, 2)
    assert flow["toc"] == "ATO"
    assert flow["flow_id"] == 20

    assert fare["flow_id"] == 20
    assert fare["ticket_code"] == "SDS"
    assert fare["fare"] == 3450  # pence, kept as the feed stores it


def test_loc_record_types_are_read_from_byte_one(tmp_path):
    """LOC has five record types (L/G/M/S/R). Lines are real RJFAF833 records."""
    from rail.layouts.fares import LOC

    path = write(
        tmp_path,
        "a.LOC",
        [
            b"RG7000320311229991508202410102022LONDON ZONES 1-2     ",
            b"RM7000320311229997014440EUS",
            b"RR7000270LUR01092025",
            b"RS70025403112299904032022COLCHESTER (2)  ",
        ],
    )
    tables, stats = read_fixed_width(path, LOC)

    assert tables["location_group"].to_pylist()[0]["description"] == "LONDON ZONES 1-2"
    member = tables["location_group_member"].to_pylist()[0]
    assert member["uic"] == "7000320" and member["member_crs"] == "EUS"
    assert tables["location_railcard"].to_pylist()[0]["railcard_code"] == "LUR"
    assert tables["location_synonym"].to_pylist()[0]["description"] == "COLCHESTER (2)"
    assert not stats.unknown_records
