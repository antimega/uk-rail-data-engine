"""Snapshot ZIP → Parquet.

Members are streamed straight out of the ZIP, so a 1 GB MCA never lands on disk
uncompressed. Output goes to ``parquet/<feed>/<snapshot>/<table>.parquet`` —
one directory per snapshot, so several feed vintages sit side by side and any
result can be traced back to the exact input that produced it.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import asdict, dataclass, field as dc_field
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ..acquire.snapshots import Manifest
from ..layouts import ALL_FILES
from .fixed_width import ParseStats, read_fixed_width
from .special import SPECIAL_READERS

#: Index/manifest members, kept for validation but holding no records.
INDEX_FILES = {"DAT", "RGI"}

#: Files with no spec yet, distinguished from genuinely unrecognised ones so the
#: report says "not written yet" rather than "we have no idea what this is".
#: RST/FNS/FRR arrive with the restriction and railcard phases; the routeing
#: guide (RG*) is out of scope until route validity is in scope.
DEFERRED = {
    "RST", "FNS", "FRR", "SUP", "TAP", "TSP", "TPK", "TRR",
    "TCL", "TJS", "TPB", "TPN", "REJ", "SET", "CFA", "NDF",
} | {f"RG{letter}" for letter in "ABCDEFGHKLMNPRSVXY"}


@dataclass
class FileReport:
    member: str
    extension: str
    status: str
    rows: dict[str, int] = dc_field(default_factory=dict)
    lines: int = 0
    unknown_records: dict[str, int] = dc_field(default_factory=dict)
    null_coercions: dict[str, int] = dc_field(default_factory=dict)


@dataclass
class IngestReport:
    feed: str
    snapshot: str
    output_dir: str
    files: list[FileReport] = dc_field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(sum(f.rows.values()) for f in self.files)

    @property
    def parsed(self) -> list[FileReport]:
        return [f for f in self.files if f.status == "parsed"]


def _write_tables(
    tables: dict[str, pa.Table],
    out_dir: Path,
    snapshot: str,
    report: FileReport,
) -> None:
    for name, table in tables.items():
        table = table.append_column(
            "snapshot_id", pa.array([snapshot] * table.num_rows, type=pa.string())
        )
        pq.write_table(table, out_dir / f"{name}.parquet", compression="zstd")
        report.rows[name] = table.num_rows


def _stats_to_report(report: FileReport, stats: ParseStats) -> None:
    report.lines = stats.lines
    report.unknown_records = dict(stats.unknown_records)
    report.null_coercions = dict(stats.null_coercions)


def ingest_snapshot(
    zip_path: Path,
    manifest: Manifest,
    parquet_dir: Path,
    *,
    only: set[str] | None = None,
) -> IngestReport:
    """Parse every recognised member of a snapshot ZIP into Parquet."""
    snapshot = Path(manifest.filename).stem
    out_dir = parquet_dir / manifest.feed / snapshot
    out_dir.mkdir(parents=True, exist_ok=True)

    report = IngestReport(feed=manifest.feed, snapshot=snapshot, output_dir=str(out_dir))

    with zipfile.ZipFile(zip_path) as archive:
        for member in sorted(archive.namelist()):
            if member.endswith("/"):
                continue
            extension = Path(member).suffix.lstrip(".").upper()

            if only and extension not in only:
                continue

            if extension in SPECIAL_READERS:
                table_name, reader = SPECIAL_READERS[extension]
                file_report = FileReport(member, extension, "parsed")
                with archive.open(member) as handle:
                    tables = {table_name: reader(handle)}
                _write_tables(tables, out_dir, snapshot, file_report)
                report.files.append(file_report)
                continue

            spec = ALL_FILES.get(extension)
            if spec is None:
                status = (
                    "index"
                    if extension in INDEX_FILES
                    else "spec-pending"
                    if extension in DEFERRED
                    else "unrecognised"
                )
                report.files.append(FileReport(member, extension, status))
                continue

            file_report = FileReport(member, extension, "parsed")
            with archive.open(member) as handle:
                tables, stats = read_fixed_width(handle, spec)
            _stats_to_report(file_report, stats)

            _write_tables(tables, out_dir, snapshot, file_report)
            report.files.append(file_report)

    (out_dir / "_ingest_report.json").write_text(json.dumps(asdict(report), indent=2) + "\n")
    return report
