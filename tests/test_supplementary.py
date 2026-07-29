"""RDG Supplementary Reference Data (RSPS5052).

Four files, each doing a job the DTD feeds cannot: which CRS codes are actually
GB rail stations, which ticket codes price a bundle of journeys rather than one,
which station pairs a PlusBus add-on may not be sold for, and where each zone's
scheme page lives. All are optional - the build has to work without them, and
has to say "unknown" rather than "no" when they are missing.
"""

from __future__ import annotations

import datetime as dt
import io

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rail.acquire.supplementary import (
    FLEXI_PRODUCTS,
    PLUSBUS_EXCLUDED,
    PLUSBUS_PAGES,
    RAIL_STATIONS,
    _read_date,
    fetch_supplementary,
    ingest_supplementary,
)
from rail.model.reference import _add_rail_station_flag
from rail.model.fares import _load_flexi_products

STATIONS_CSV = '"AAP"\n"AAT"\n"ABD"\n'
EXCLUDED_CSV = (
    '"2026-01-02","2027-01-01","1234","5678"\n'
    '"2025-01-02","2026-01-01","1234","9012"\n'
)
PAGES_CSV = '"H001","https://www.plusbus.info/buxton"\n'
FLEXI_CSV = (
    "FFL,2021-10-05,2999-12-31,50,1,0\n"
    "FL1,2021-06-21,2999-12-31,8,1,0\n"
    "SFX,2024-09-23,2025-07-01,12,1,0\n"
)


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes):
        super().__init__(body)
        self.headers = {"Last-Modified": "Mon, 06 Jan 2025 09:46:07 GMT"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture
def downloaded(tmp_path):
    """Fetch both files from a stub bucket."""
    bodies = {
        RAIL_STATIONS: STATIONS_CSV.encode(),
        FLEXI_PRODUCTS: FLEXI_CSV.encode(),
        PLUSBUS_EXCLUDED: EXCLUDED_CSV.encode(),
        PLUSBUS_PAGES: PAGES_CSV.encode(),
    }

    def opener(url, timeout=None):
        return FakeResponse(bodies[url.rsplit("/", 1)[-1]])

    results = fetch_supplementary(tmp_path / "raw", opener=opener)
    return tmp_path, results


def test_both_files_are_stored_with_a_checksum(downloaded):
    tmp_path, results = downloaded
    names = {r.filename for r in results}

    assert names == {RAIL_STATIONS, FLEXI_PRODUCTS,
                     PLUSBUS_EXCLUDED, PLUSBUS_PAGES}
    for result in results:
        assert result.path.exists()
        assert len(result.sha256) == 64
        assert result.last_modified == "Mon, 06 Jan 2025 09:46:07 GMT"


def test_the_station_list_is_one_quoted_crs_per_line(downloaded):
    tmp_path, _ = downloaded
    written = ingest_supplementary(tmp_path / "raw", tmp_path / "parquet")

    assert written["rail_station"] == 3
    table = pq.read_table(tmp_path / "parquet" / "supplementary" / "rail_station.parquet")
    assert table.column("crs").to_pylist() == ["AAP", "AAT", "ABD"]


def test_flexi_products_carry_the_bundle_size(downloaded):
    tmp_path, _ = downloaded
    ingest_supplementary(tmp_path / "raw", tmp_path / "parquet")

    table = pq.read_table(
        tmp_path / "parquet" / "supplementary" / "flexi_product.parquet"
    ).to_pylist()
    by_code = {row["ticket_code"]: row for row in table}

    assert by_code["FFL"]["bundle_size"] == 50
    assert by_code["FL1"]["bundle_size"] == 8
    assert by_code["FFL"]["bi_directional"] is True
    assert by_code["FFL"]["transferable"] is False
    assert by_code["SFX"]["end_date"] == dt.date(2025, 7, 1)


def test_dates_are_read_the_way_the_file_writes_them():
    """The spec says YYYY/MM/DD, the file says YYYY-MM-DD, and the version
    history shows this changed and changed back. Accept both."""
    assert _read_date("2021-10-05") == dt.date(2021, 10, 5)
    assert _read_date("2021/10/05") == dt.date(2021, 10, 5)
    assert _read_date("") is None
    assert _read_date("not a date") is None


# --- how the flags reach the model -------------------------------------------


def station_table(connection):
    connection.execute("create table station (crs varchar, name varchar)")
    connection.execute(
        "insert into station values ('AAP', 'ALEXANDRA PALACE'), "
        "('HEB', 'HELSTON BUS')"
    )


def test_an_absent_station_list_leaves_the_flag_unknown(tmp_path):
    """Null, not false: "not fetched" is not the same as "not a station"."""
    connection = duckdb.connect()
    station_table(connection)
    _add_rail_station_flag(connection, None)

    flags = connection.execute("select crs, is_rail_station from station").fetchall()
    assert flags == [("AAP", None), ("HEB", None)]


def test_the_station_list_marks_non_rail_interchange_points(tmp_path):
    directory = tmp_path / "supplementary"
    directory.mkdir()
    pq.write_table(
        pa.Table.from_pylist([{"crs": "AAP"}], schema=pa.schema([("crs", pa.string())])),
        directory / "rail_station.parquet",
    )
    connection = duckdb.connect()
    station_table(connection)
    _add_rail_station_flag(connection, directory)

    flags = dict(connection.execute("select crs, is_rail_station from station").fetchall())
    assert flags == {"AAP": True, "HEB": False}


def test_flexi_products_are_empty_rather_than_missing_when_not_fetched():
    """The fares SQL joins to this table unconditionally, so it must exist."""
    connection = duckdb.connect()
    _load_flexi_products(connection, None)

    assert connection.execute("select count(*) from flexi_product").fetchone() == (0,)


def test_an_expired_flexi_product_is_not_loaded(tmp_path):
    directory = tmp_path / "supplementary"
    directory.mkdir()
    schema = pa.schema([
        ("ticket_code", pa.string()), ("start_date", pa.date32()),
        ("end_date", pa.date32()), ("bundle_size", pa.int32()),
        ("bi_directional", pa.bool_()), ("transferable", pa.bool_()),
    ])
    pq.write_table(
        pa.Table.from_pylist([
            {"ticket_code": "FFL", "start_date": dt.date(2021, 10, 5),
             "end_date": dt.date(2999, 12, 31), "bundle_size": 50,
             "bi_directional": True, "transferable": False},
            {"ticket_code": "OLD", "start_date": dt.date(2020, 1, 1),
             "end_date": dt.date(2021, 1, 1), "bundle_size": 12,
             "bi_directional": True, "transferable": False},
        ], schema=schema),
        directory / "flexi_product.parquet",
    )
    connection = duckdb.connect()
    _load_flexi_products(connection, directory)

    codes = connection.execute("select ticket_code from flexi_product").fetchall()
    assert codes == [("FFL",)]


def test_the_excluded_pairs_keep_their_validity_dates(downloaded):
    """The file ships two annual generations and half of it has expired, so the
    dates are the difference between a live rule and a dead one."""
    tmp_path, _ = downloaded
    written = ingest_supplementary(tmp_path / "raw", tmp_path / "parquet")

    assert written["plusbus_excluded_pair"] == 2
    rows = pq.read_table(
        tmp_path / "parquet" / "supplementary" / "plusbus_excluded_pair.parquet"
    ).to_pylist()
    assert rows[0]["end_date"] == dt.date(2027, 1, 1)
    assert rows[1]["end_date"] == dt.date(2026, 1, 1)


def test_the_scheme_pages_are_read(downloaded):
    tmp_path, _ = downloaded
    ingest_supplementary(tmp_path / "raw", tmp_path / "parquet")

    rows = pq.read_table(
        tmp_path / "parquet" / "supplementary" / "plusbus_web_page.parquet"
    ).to_pylist()
    assert rows == [{"nlc": "H001", "url": "https://www.plusbus.info/buxton"}]
