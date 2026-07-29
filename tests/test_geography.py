"""Precise grid references, and the merge that must not trust either source.

MSN's coordinates are about a kilometre accurate; the OGL TIPLOC spreadsheet is
exact. But the spreadsheet has its own errors — it puts Highbury & Islington 58
km away, in Kent — so the merge refines where the two agree and keeps MSN where
they do not. Using each to check the other is the only check available.
"""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from xml.sax.saxutils import escape

import duckdb
import pytest

import pyarrow as pa
import pyarrow.parquet as pq

from rail.acquire.geography import ingest_geography, read_tiploc_grid
from rail.model.reference import GRID_AGREEMENT_METRES, _refine_grid_references

SHEET = """<?xml version="1.0"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData>{rows}</sheetData></worksheet>"""

STRINGS = """<?xml version="1.0"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{items}</sst>"""


def spreadsheet(rows, *, compress=False):
    """The smallest xlsx the reader will accept: shared strings and a sheet."""
    shared: list[str] = []

    def intern(value):
        if value not in shared:
            shared.append(value)
        return shared.index(value)

    body = []
    for index, (tiploc, name, easting, northing) in enumerate(rows, start=1):
        cells = []
        for column, value in (("A", tiploc), ("B", name)):
            if value is not None:
                cells.append(f'<c r="{column}{index}" t="s">'
                             f"<v>{intern(value)}</v></c>")
        for column, value in (("C", easting), ("D", northing)):
            if value is not None:
                cells.append(f'<c r="{column}{index}"><v>{value}</v></c>')
        body.append(f'<row r="{index}">{"".join(cells)}</row>')

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        # Real station names contain "&" — Highbury & Islington, the very case
        # below — so the writer has to escape even though the reader unescapes.
        book.writestr("xl/sharedStrings.xml", STRINGS.format(
            items="".join(f"<si><t>{escape(s)}</t></si>" for s in shared)))
        book.writestr("xl/worksheets/sheet1.xml",
                      SHEET.format(rows="".join(body)))
    raw = buffer.getvalue()
    return gzip.compress(raw) if compress else raw


@pytest.fixture
def sheet(tmp_path):
    def _write(rows, *, compress=False, name="grid.xlsx"):
        path = tmp_path / (name + (".gz" if compress else ""))
        path.write_bytes(spreadsheet(rows, compress=compress))
        return path

    return _write


# --- reading the file --------------------------------------------------------


def test_tiplocs_and_grid_references_are_read(sheet):
    path = sheet([("TIPLOC", "NAME", None, None),
                  ("YORK", "YORK", 460000, 451800)])

    assert read_tiploc_grid(path) == [
        {"tiploc": "YORK", "name": "YORK", "easting": 460000, "northing": 451800}
    ]


def test_the_header_row_drops_out_without_being_named(sheet):
    """A row with no numeric easting is not a station, which removes the header
    and any blank line without needing to know where they are."""
    path = sheet([("TIPLOC", "NAME", None, None),
                  (None, None, None, None),
                  ("YORK", "YORK", 460000, 451800)])

    assert [row["tiploc"] for row in read_tiploc_grid(path)] == ["YORK"]


def test_the_file_is_accepted_gzipped_or_not(sheet):
    """The published file is distributed .xlsx.gz."""
    rows = [("YORK", "YORK", 460000, 451800)]

    assert read_tiploc_grid(sheet(rows)) == read_tiploc_grid(
        sheet(rows, compress=True))


def test_the_import_records_a_checksum_and_the_licence(sheet, tmp_path):
    """This arrives by hand rather than from a versioned feed, so every figure
    derived from it should still be traceable to the exact input. The licence is
    recorded because it is *not* the DTD licence."""
    path = sheet([("YORK", "YORK", 460000, 451800)])
    counts = ingest_geography(path, tmp_path / "parquet")

    manifest = json.loads(
        (tmp_path / "parquet" / "geography" / "manifest.json").read_text())
    assert counts.tiplocs == 1
    assert manifest["sha256"] == counts.sha256
    assert "Open Government Licence" in manifest["licence"]


# --- the merge ---------------------------------------------------------------


NAPTAN = pa.schema([
    ("tiploc", pa.string()), ("atco_code", pa.string()), ("name", pa.string()),
    ("stop_type", pa.string()), ("is_active", pa.bool_()),
    ("easting", pa.int64()), ("northing", pa.int64()),
    ("latitude", pa.float64()), ("longitude", pa.float64()),
])


@pytest.fixture
def merged(tmp_path, sheet):
    """A station table plus up to two precise sources, resolved against MSN."""

    def _build(stations, tiplocs, rows, naptan=None):
        connection = duckdb.connect()
        connection.execute(
            "create table station (crs varchar, easting bigint, northing bigint, "
            "grid_source varchar default 'msn')")
        for crs, easting, northing in stations:
            connection.execute(
                "insert into station (crs, easting, northing, grid_source) "
                "values (?, ?, ?, 'msn')", [crs, easting, northing])
        connection.execute("create table station_tiploc (crs varchar, tiploc varchar)")
        for crs, tiploc in tiplocs:
            connection.execute("insert into station_tiploc values (?, ?)",
                               [crs, tiploc])
        connection.execute("create table reference_reject "
                           "(source varchar, key varchar, reason varchar)")

        ingest_geography(sheet(rows), tmp_path / "parquet")
        naptan_dir = None
        if naptan is not None:
            naptan_dir = tmp_path / "parquet" / "naptan"
            naptan_dir.mkdir(parents=True, exist_ok=True)
            pq.write_table(
                pa.Table.from_pylist(
                    [{"tiploc": tiploc, "atco_code": "9100" + tiploc,
                      "name": tiploc, "stop_type": "RLY", "is_active": True,
                      "easting": easting, "northing": northing,
                      "latitude": None, "longitude": None}
                     for tiploc, easting, northing in naptan],
                    schema=NAPTAN),
                naptan_dir / "naptan_rail.parquet")
        _refine_grid_references(
            connection, tmp_path / "parquet" / "geography", naptan_dir)
        return connection

    return _build


def test_an_agreeing_position_is_sharpened(merged):
    """MSN rounds to 100 m, so a small disagreement is precision rather than
    conflict and the exact value wins."""
    connection = merged(
        stations=[("YRK", 460000, 451800)],
        tiplocs=[("YRK", "YORK")],
        rows=[("YORK", "YORK", 460041, 451825)],
    )

    assert connection.execute(
        "select easting, northing, grid_source from station").fetchone() == (
        460041, 451825, "tiploc")


def test_two_sources_that_disagree_leave_the_position_unresolved(merged):
    """Highbury & Islington: the FOI spreadsheet places it 58 km away, in Kent,
    and MSN is right. With only those two there is nothing to adjudicate — the
    measured split is 16 to 14 across the real conflicts, which is a coin flip —
    so the more precise value is taken, marked uncorroborated, and recorded.
    What matters is that the guess is visible, not which way it went."""
    connection = merged(
        stations=[("HHY", 531500, 184700)],
        tiplocs=[("HHY", "HIGHBYA")],
        rows=[("HIGHBYA", "HIGHBURY & ISLINGTON", 574355, 145323)],
    )

    assert connection.execute("select grid_source from station").fetchone() == (
        "tiploc (uncorroborated)",)
    assert connection.execute(
        "select count(*) from station_grid_conflict").fetchone() == (1,)


def test_a_third_source_settles_it(merged):
    """Which is exactly why NaPTAN was added. It adjudicates 30 of the 31 real
    conflicts, and backs the FOI file in 16 of them — so the earlier rule of
    always keeping MSN was wrong more often than right."""
    connection = merged(
        stations=[("HHY", 531500, 184700)],
        tiplocs=[("HHY", "HIGHBYA")],
        rows=[("HIGHBYA", "HIGHBURY & ISLINGTON", 574355, 145323)],
        naptan=[("HIGHBYA", 531585, 184725)],
    )

    # NaPTAN agrees with MSN, so the FOI position loses despite being precise.
    assert connection.execute(
        "select easting, northing, grid_source from station").fetchone() == (
        531585, 184725, "naptan")
    assert connection.execute(
        "select count(*) from station_grid_conflict").fetchone() == (0,)


def test_corroboration_picks_the_place_and_precision_picks_the_digits(merged):
    """NaPTAN rounds 393 of its 2,765 rail stops to 100 m; the FOI file rounds 1
    of 9,397. So where a second source vouches for the FOI position, its exact
    value is the one to keep — NaPTAN's job is to say *which* position is right,
    not to supply the final digits."""
    connection = merged(
        stations=[("YRK", 459500, 451700)],
        tiplocs=[("YRK", "YORK")],
        rows=[("YORK", "YORK", 459512, 451648)],
        naptan=[("YORK", 459600, 451700)],
    )

    assert connection.execute(
        "select easting, northing, grid_source from station").fetchone() == (
        459512, 451648, "tiploc")


def test_a_station_with_no_msn_position_takes_what_it_can_get(merged):
    """Nothing to check against, so the value is used and marked unverified
    rather than discarded."""
    connection = merged(
        stations=[("ZZZ", None, None)],
        tiplocs=[("ZZZ", "SOMEWHERE")],
        rows=[("SOMEWHERE", "SOMEWHERE", 400000, 400000)],
    )

    assert connection.execute(
        "select easting, northing, grid_source from station").fetchone() == (
        400000, 400000, "tiploc (uncorroborated)")


def test_a_station_in_neither_precise_source_keeps_its_msn_position(merged):
    connection = merged(
        stations=[("YRK", 460000, 451800)],
        tiplocs=[("YRK", "YORK")],
        rows=[("ELSEWHERE", "ELSEWHERE", 1, 1)],
    )

    assert connection.execute(
        "select easting, northing, grid_source from station").fetchone() == (
        460000, 451800, "msn (uncorroborated)")


def test_the_nearest_tiploc_wins_not_the_first(merged):
    """Pollokshaws West also carries `BUSBYJ`, a junction 5 km off, and taking
    the first match moved the station. MSN already localises it to within a
    kilometre, so the nearest candidate is the right one."""
    connection = merged(
        stations=[("PWW", 255900, 661400)],
        tiplocs=[("PWW", "BUSBYJ"), ("PWW", "PLKSHWW")],
        rows=[("BUSBYJ", "BUSBY JN", 258109, 656692),
              ("PLKSHWW", "POLLOKSHAWS WEST", 255925, 661358)],
    )

    assert connection.execute(
        "select easting, northing, grid_source from station").fetchone() == (
        255925, 661358, "tiploc")


def test_no_geography_import_leaves_everything_untouched(tmp_path):
    """The file is optional, and a build without it must behave exactly as it
    did before the file existed."""
    connection = duckdb.connect()
    connection.execute("create table station (crs varchar, easting bigint, "
                       "northing bigint, grid_source varchar default 'msn')")
    connection.execute("insert into station values ('YRK', 460000, 451800, 'msn')")

    _refine_grid_references(connection, None)
    _refine_grid_references(connection, tmp_path / "nothing-here")

    assert connection.execute("select grid_source from station").fetchone() == ("msn",)


def test_the_agreement_threshold_is_a_kilometre():
    """MSN rounds to 100 m and the 90th-percentile disagreement is 156 m, so a
    kilometre separates precision from conflict with room to spare."""
    assert GRID_AGREEMENT_METRES == 1000
