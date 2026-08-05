"""Vectorised reader for the DTD fixed-width flat files.

Lines are bucketed by their record-type discriminator, then each bucket is
turned into a byte matrix so every column is a numpy slice rather than a Python
loop. This matters: the timetable MCA runs to millions of records.

Nothing here raises on malformed input. Bad values become nulls and are counted,
because the whole point of the ingest is to find out what the feed actually
contains rather than to assume it is clean.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import IO, Iterator

import numpy as np
import pyarrow as pa

from ..layouts.spec import Field, FileSpec, Kind, RecordSpec

DEFAULT_BATCH_SIZE = 250_000

ARROW_TYPES = {
    Kind.TEXT: pa.string(),
    Kind.INT: pa.int64(),
    Kind.DATE: pa.date32(),
    Kind.SHORT_DATE: pa.date32(),
    Kind.ZTR_END_DATE: pa.date32(),
    Kind.PUBLIC_TIME: pa.int32(),
    Kind.WORKING_TIME: pa.int32(),
    Kind.BOOL: pa.bool_(),
}

TRUE_BYTES = (b"Y", b"X", b"1", b"T")

_MONTH_LENGTHS = np.array([31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31], dtype=np.int64)


@dataclass
class ParseStats:
    """What the reader saw, so `rail validate` can report on it."""

    lines: int = 0
    blank_lines: int = 0
    comment_lines: int = 0
    #: Record-type keys present in the file but not in the spec.
    unknown_records: Counter = dc_field(default_factory=Counter)
    #: Record-type keys the spec deliberately ignores.
    ignored_records: Counter = dc_field(default_factory=Counter)
    records_by_type: Counter = dc_field(default_factory=Counter)
    #: Values that failed conversion, keyed "table.column".
    null_coercions: Counter = dc_field(default_factory=Counter)


# ---------------------------------------------------------------------------
# column extraction
# ---------------------------------------------------------------------------


def _column(matrix: np.ndarray, start: int, length: int) -> np.ndarray:
    """Extract a fixed-width byte column as a 1-D S{length} array."""
    sub = np.ascontiguousarray(matrix[:, start : start + length])
    return sub.view(f"S{length}").reshape(-1)


def _digits_to_int(col: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (values, valid_mask). Non-numeric entries yield 0 and False."""
    stripped = np.char.strip(col)
    valid = (stripped != b"") & np.char.isdigit(stripped)
    safe = np.where(valid, stripped, b"0")
    return safe.astype(np.int64), valid


def _days_from_civil(year: np.ndarray, month: np.ndarray, day: np.ndarray) -> np.ndarray:
    """Days since 1970-01-01, by pure integer arithmetic.

    Deliberately never raises: an impossible date such as 31 February yields a
    nearby wrong day rather than blowing up the ingest. Range checks upstream
    mask the values that matter.
    """
    year = year - (month <= 2)
    era = np.where(year >= 0, year, year - 399) // 400
    year_of_era = year - era * 400
    month_shift = np.where(month > 2, -3, 9)
    day_of_year = (153 * (month + month_shift) + 2) // 5 + day - 1
    day_of_era = (
        year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year
    )
    return era * 146097 + day_of_era - 719468


def _date_column(matrix: np.ndarray, field: Field) -> tuple[np.ndarray, np.ndarray]:
    if field.kind is Kind.DATE:  # DDMMYYYY
        day, day_ok = _digits_to_int(_column(matrix, field.start, 2))
        month, month_ok = _digits_to_int(_column(matrix, field.start + 2, 2))
        year, year_ok = _digits_to_int(_column(matrix, field.start + 4, 4))
    else:  # YYMMDD, CIF short date
        short_year, year_ok = _digits_to_int(_column(matrix, field.start, 2))
        month, month_ok = _digits_to_int(_column(matrix, field.start + 2, 2))
        day, day_ok = _digits_to_int(_column(matrix, field.start + 4, 2))
        # CIF convention: the window is 1960-2059.
        year = np.where(short_year >= 60, 1900 + short_year, 2000 + short_year)
        if field.kind is Kind.ZTR_END_DATE:
            open_ended = (short_year == 99) & (month == 12) & (day == 31)
            year = np.where(open_ended, 2999, year)

    valid = (
        day_ok
        & month_ok
        & year_ok
        & (month >= 1)
        & (month <= 12)
        & (day >= 1)
        & (year >= 1900)
    )
    # Reject impossible days rather than letting them roll over: 31 February
    # would otherwise become 3 March, which is worse than a null because it
    # looks plausible.
    safe_month = np.clip(month, 1, 12)
    leap = ((year % 4 == 0) & (year % 100 != 0)) | (year % 400 == 0)
    days_in_month = _MONTH_LENGTHS[safe_month - 1] + ((safe_month == 2) & leap)
    valid &= day <= days_in_month

    return _days_from_civil(year, month, day).astype(np.int32), valid


def _to_arrow(matrix: np.ndarray, field: Field) -> tuple[pa.Array, int]:
    """Convert one field to an Arrow array. Returns (array, null_count_from_bad_input)."""
    if field.kind is Kind.TEXT:
        stripped = np.char.strip(_column(matrix, field.start, field.length))
        mask = stripped == b""
        return pa.array(stripped.astype("U"), mask=mask, type=pa.string()), 0

    if field.kind is Kind.INT:
        col = _column(matrix, field.start, field.length)
        values, valid = _digits_to_int(col)
        blank = np.char.strip(col) == b""
        bad = int(np.count_nonzero(~valid & ~blank))
        return pa.array(values, mask=~valid, type=pa.int64()), bad

    if field.kind in (Kind.DATE, Kind.SHORT_DATE, Kind.ZTR_END_DATE):
        col = _column(matrix, field.start, field.length)
        values, valid = _date_column(matrix, field)
        blank = np.char.strip(col) == b""
        bad = int(np.count_nonzero(~valid & ~blank))
        return pa.array(values, mask=~valid, type=pa.date32()), bad

    if field.kind is Kind.PUBLIC_TIME:
        hours, hours_ok = _digits_to_int(_column(matrix, field.start, 2))
        minutes, minutes_ok = _digits_to_int(_column(matrix, field.start + 2, 2))
        # CIF uses 0000 to mean "no public time", not midnight.
        valid = hours_ok & minutes_ok & (hours < 24) & (minutes < 60)
        valid &= ~((hours == 0) & (minutes == 0))
        return pa.array((hours * 60 + minutes).astype(np.int32), mask=~valid, type=pa.int32()), 0

    if field.kind is Kind.WORKING_TIME:
        hours, hours_ok = _digits_to_int(_column(matrix, field.start, 2))
        minutes, minutes_ok = _digits_to_int(_column(matrix, field.start + 2, 2))
        half = _column(matrix, field.start + 4, 1) == b"H"
        valid = hours_ok & minutes_ok & (hours < 24) & (minutes < 60)
        # Bare 0000 is the absent-time sentinel. 0000H is a real working
        # time—thirty seconds after midnight—and occurs in the live MCA feed.
        valid &= ~((hours == 0) & (minutes == 0) & ~half)
        seconds = hours * 3600 + minutes * 60 + np.where(half, 30, 0)
        return pa.array(seconds.astype(np.int32), mask=~valid, type=pa.int32()), 0

    if field.kind is Kind.BOOL:
        col = _column(matrix, field.start, field.length)
        truthy = np.isin(np.char.upper(np.char.strip(col)), TRUE_BYTES)
        return pa.array(truthy, type=pa.bool_()), 0

    raise ValueError(f"unhandled field kind: {field.kind}")


# ---------------------------------------------------------------------------
# file reading
# ---------------------------------------------------------------------------


def _batches(handle: IO[bytes], batch_size: int) -> Iterator[list[bytes]]:
    batch: list[bytes] = []
    for line in handle:
        batch.append(line.rstrip(b"\r\n"))
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _matrix(lines: list[bytes], width: int) -> np.ndarray:
    width = max(width, max(len(line) for line in lines))
    joined = b"".join(line.ljust(width)[:width] for line in lines)
    return np.frombuffer(joined, dtype="S1").reshape(len(lines), width)


def _union_schema(spec: FileSpec) -> dict[str, pa.Schema]:
    """One schema per output table, unioning the fields of every record type."""
    columns: dict[str, dict[str, pa.DataType]] = defaultdict(dict)
    for record in spec.all_records:
        table = columns[record.name]
        # CIF is positional: a BS schedule owns the LO/LI/LT stops that follow
        # it. Bucketing by record type loses that, so every row carries its
        # position in the file and an as-of join rebuilds the hierarchy.
        table.setdefault("line_no", pa.int64())
        table.setdefault("record_type", pa.string())
        for field in record.fields:
            table[field.name] = ARROW_TYPES[field.kind]
        for name in record.constants:
            table.setdefault(name, pa.string())
    return {
        name: pa.schema([pa.field(k, v) for k, v in cols.items()])
        for name, cols in columns.items()
    }


def _record_batch(
    numbered: list[tuple[int, bytes]],
    key: str,
    record: RecordSpec,
    schema: pa.Schema,
    stats: ParseStats,
) -> pa.RecordBatch:
    line_numbers = [n for n, _ in numbered]
    lines = [line for _, line in numbered]

    matrix = _matrix(lines, record.width)
    built: dict[str, pa.Array] = {}
    for field in record.fields:
        array, bad = _to_arrow(matrix, field)
        built[field.name] = array
        if bad:
            stats.null_coercions[f"{record.name}.{field.name}"] += bad
    built["line_no"] = pa.array(line_numbers, type=pa.int64())
    built["record_type"] = pa.array([key] * len(lines), type=pa.string())
    for name, value in record.constants.items():
        built[name] = pa.array([value] * len(lines), type=pa.string())

    arrays = [
        built.get(name) or pa.nulls(len(lines), type=schema.field(name).type)
        for name in schema.names
    ]
    return pa.RecordBatch.from_arrays(arrays, schema=schema)


def read_fixed_width(
    source: Path | IO[bytes],
    spec: FileSpec,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[dict[str, pa.Table], ParseStats]:
    """Parse one DTD file into a table per record group.

    Accepts a path or an open binary handle, so members can be streamed
    straight out of the feed ZIP without unpacking to disk first.
    """
    schemas = _union_schema(spec)
    collected: dict[str, list[pa.RecordBatch]] = defaultdict(list)
    stats = ParseStats()

    handle = source.open("rb") if isinstance(source, Path) else source
    try:
        for batch in _batches(handle, batch_size):
            # Grouped as (line_no, line) so record order survives bucketing.
            grouped: dict[str, list[tuple[int, bytes]]] = defaultdict(list)
            for line in batch:
                line_no = stats.lines
                stats.lines += 1
                if not line.strip():
                    stats.blank_lines += 1
                    continue
                # Every DTD file brackets its content with /!! comment lines.
                if line.startswith(b"/!!"):
                    stats.comment_lines += 1
                    continue
                if spec.single is not None:
                    grouped[""].append((line_no, line))
                    continue
                key = line[spec.key_start : spec.key_start + spec.key_length].decode(
                    "latin-1"
                )
                grouped[key].append((line_no, line))

            for key, numbered in grouped.items():
                record = spec.record_for(key)
                if record is None:
                    if key in spec.ignore:
                        stats.ignored_records[key] += len(numbered)
                    else:
                        stats.unknown_records[key] += len(numbered)
                    continue
                stats.records_by_type[key] += len(numbered)
                collected[record.name].append(
                    _record_batch(numbered, key, record, schemas[record.name], stats)
                )
    finally:
        if isinstance(source, Path):
            handle.close()

    tables = {
        name: pa.Table.from_batches(batches, schema=schemas[name])
        for name, batches in collected.items()
    }
    return tables, stats
