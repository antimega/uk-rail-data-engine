"""End-to-end: a snapshot ZIP becomes Parquet, with an honest report."""

from __future__ import annotations

import json
import zipfile

import pyarrow.parquet as pq

from rail.acquire.snapshots import Manifest, SnapshotStore, parse_sequence
from rail.parse import ingest_snapshot

from test_fixed_width import make_line


def build_snapshot(tmp_path):
    zip_path = tmp_path / "RJTTF0123.ZIP"
    mca = b"\r\n".join(
        [
            b"/!! Start of file",
            make_line(80, p0="BS", p3="C12345", p9="260504", p15="261212", p21="1111100", p29="P", p30="OO", p79="P"),
            make_line(80, p0="LO", p2="EUSTON ", p15="0730", p29="TB"),
            make_line(80, p0="LT", p2="BHAMNWS", p15="0900", p25="TF"),
            make_line(80, p0="TI", p2="EUSTON ", p53="EUS", p18="LONDON EUSTON"),
            b"ZZ",
        ]
    )
    msn = b"\r\n".join(
        [
            make_line(70, p0="A", p5="LONDON EUSTON", p36="EUSTON ", p49="EUS", p52="15296", p58="61836", p63="15"),
            make_line(70, p0="L", p5="LONDON EUSTON", p36="EUSTON"),
        ]
    )
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("RJTTF0123.MCA", mca)
        archive.writestr("RJTTF0123.MSN", msn)
        archive.writestr("RJTTF0123.DAT", b"RJTTF0123.MCA\r\nRJTTF0123.MSN\r\n")
        archive.writestr("RJTTF0123.FLF", b"ADDITIONAL LINK: TUBE BETWEEN EUS AND KXX IN 5 MINUTES\r\n")
        archive.writestr("RJTTF0123.XYZ", b"unexpected\r\n")

    manifest = Manifest(
        feed="timetable",
        filename="RJTTF0123.ZIP",
        url="https://example.invalid/timetable",
        source="test",
        fetched_at="2026-07-24T00:00:00+00:00",
        last_modified=None,
        size=zip_path.stat().st_size,
        sha256="0" * 64,
        sequence=123,
    )
    return zip_path, manifest


def test_ingest_writes_parquet_per_table(tmp_path):
    zip_path, manifest = build_snapshot(tmp_path)
    parquet_dir = tmp_path / "parquet"

    report = ingest_snapshot(zip_path, manifest, parquet_dir)
    out = parquet_dir / "timetable" / "RJTTF0123"

    assert {p.name for p in out.glob("*.parquet")} == {
        "schedule.parquet",
        "stop_time.parquet",
        "tiploc.parquet",
        "physical_station.parquet",
        "station_alias.parquet",
        "fixed_link.parquet",
    }

    stops = pq.read_table(out / "stop_time.parquet")
    assert stops.num_rows == 2
    # Every row carries the snapshot it came from, so unioned vintages stay traceable.
    assert set(stops.column("snapshot_id").to_pylist()) == {"RJTTF0123"}

    station = pq.read_table(out / "physical_station.parquet").to_pylist()[0]
    assert station["crs_code"] == "EUS"
    assert station["tiploc_code"] == "EUSTON"
    assert station["minimum_change_time"] == 15

    # 2 stops, 1 schedule, 1 tiploc, 1 station, 1 alias, 1 fixed link
    assert report.total_rows == 7


def test_ingest_reports_unparsed_members_rather_than_hiding_them(tmp_path):
    zip_path, manifest = build_snapshot(tmp_path)
    report = ingest_snapshot(zip_path, manifest, tmp_path / "parquet")
    status = {entry.extension: entry.status for entry in report.files}

    assert status["MCA"] == "parsed"
    assert status["DAT"] == "index"
    assert status["FLF"] == "parsed"  # sentence format, via a dedicated parser
    assert status["XYZ"] == "unrecognised"


def test_ingest_report_is_written_next_to_the_data(tmp_path):
    zip_path, manifest = build_snapshot(tmp_path)
    ingest_snapshot(zip_path, manifest, tmp_path / "parquet")

    written = json.loads(
        (tmp_path / "parquet" / "timetable" / "RJTTF0123" / "_ingest_report.json").read_text()
    )
    assert written["feed"] == "timetable"
    assert written["snapshot"] == "RJTTF0123"


def test_a_feed_with_no_layouts_says_so_rather_than_reporting_zero_rows(tmp_path):
    """The routeing guide's files are comma-separated and have no layouts, so
    ingesting one writes nothing. That is the normal outcome, not a failed
    parse, and `writes_nothing` is what lets the callers say which it is."""
    zip_path = tmp_path / "RJRG1075.ZIP"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("RJRG1075.RGD", b"/ station links\r\nKDG,ASG,2.34\r\n")
        archive.writestr("RJRG1075.RGI", b"RJRG1075.RGD\r\n")

    manifest = Manifest(
        feed="routeing",
        filename="RJRG1075.ZIP",
        url="https://example.invalid/routeing",
        source="test",
        fetched_at="2026-09-04T00:00:00+00:00",
        last_modified=None,
        size=zip_path.stat().st_size,
        sha256="0" * 64,
        sequence=1075,
    )

    report = ingest_snapshot(zip_path, manifest, tmp_path / "parquet")

    assert report.total_rows == 0
    assert report.writes_nothing

    timetable_zip, timetable_manifest = build_snapshot(tmp_path)
    parsed = ingest_snapshot(
        timetable_zip, timetable_manifest, tmp_path / "parquet"
    )
    assert not parsed.writes_nothing


def test_only_filter_restricts_which_members_are_parsed(tmp_path):
    zip_path, manifest = build_snapshot(tmp_path)
    report = ingest_snapshot(zip_path, manifest, tmp_path / "parquet", only={"MSN"})

    assert [entry.extension for entry in report.files] == ["MSN"]


def test_sequence_number_is_read_from_the_filename():
    assert parse_sequence("RJFAF0123.ZIP") == 123
    assert parse_sequence("RJTTF9999.zip") == 9999
    assert parse_sequence("no-digits.zip") is None


def test_snapshot_store_is_idempotent_for_identical_content(tmp_path):
    store = SnapshotStore(tmp_path / "raw")
    from rail.acquire.source import Feed

    for _ in range(2):
        temp = store.feed_dir(Feed.FARES) / "incoming.tmp"
        temp.write_bytes(b"identical bytes")
        store.store(
            Feed.FARES,
            "RJFAF0001.ZIP",
            temp,
            url="https://example.invalid/fares",
            source="test",
            last_modified="Wed, 01 Jul 2026 00:00:00 GMT",
        )

    assert len(store.manifests(Feed.FARES)) == 1
