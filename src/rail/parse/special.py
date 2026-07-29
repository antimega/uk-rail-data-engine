"""Parsers for the timetable files that aren't fixed-width.

Three files carry the data the routing engine needs for transfers, and each has
its own idiosyncratic format:

* ``FLF`` — fixed links, written as English sentences.
* ``ALF`` — additional fixed links, comma-separated ``KEY=VALUE`` pairs.
* ``TSI`` — TOC-specific interchange times, plain CSV.

Fixed links are how a journey gets from Euston to King's Cross on foot or by
Underground; without them, one-to-all routing silently misses everything that
needs a cross-London transfer.
"""

from __future__ import annotations

import re
from typing import IO

import pyarrow as pa

FLF_PATTERN = re.compile(
    rb"ADDITIONAL LINK:\s*(?P<mode>\S+)\s+BETWEEN\s+(?P<origin>\S{3})\s+AND\s+"
    rb"(?P<destination>\S{3})\s+IN\s+(?P<duration>\d+)\s+MINUTES",
    re.IGNORECASE,
)

FLF_SCHEMA = pa.schema(
    [
        pa.field("mode", pa.string()),
        pa.field("origin", pa.string()),
        pa.field("destination", pa.string()),
        pa.field("duration", pa.int32()),
    ]
)

ALF_SCHEMA = pa.schema(
    [
        pa.field("mode", pa.string()),
        pa.field("origin", pa.string()),
        pa.field("destination", pa.string()),
        pa.field("duration", pa.int32()),
        pa.field("start_time", pa.int32()),
        pa.field("end_time", pa.int32()),
        pa.field("priority", pa.int32()),
        pa.field("start_date", pa.string()),
        pa.field("end_date", pa.string()),
        pa.field("monday", pa.bool_()),
        pa.field("tuesday", pa.bool_()),
        pa.field("wednesday", pa.bool_()),
        pa.field("thursday", pa.bool_()),
        pa.field("friday", pa.bool_()),
        pa.field("saturday", pa.bool_()),
        pa.field("sunday", pa.bool_()),
    ]
)

TSI_SCHEMA = pa.schema(
    [
        pa.field("crs", pa.string()),
        pa.field("from_toc", pa.string()),
        pa.field("to_toc", pa.string()),
        pa.field("minutes", pa.int32()),
    ]
)

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _minutes(value: str | None) -> int | None:
    """HHMM to minutes after midnight."""
    if not value or len(value) != 4 or not value.isdigit():
        return None
    return int(value[:2]) * 60 + int(value[2:])


def read_flf(handle: IO[bytes]) -> pa.Table:
    rows = []
    for line in handle:
        match = FLF_PATTERN.search(line)
        if match is None:
            continue  # header, the trailing "END", or anything unexpected
        rows.append(
            {
                "mode": match["mode"].decode("latin-1").upper(),
                "origin": match["origin"].decode("latin-1"),
                "destination": match["destination"].decode("latin-1"),
                "duration": int(match["duration"]),
            }
        )
    return pa.Table.from_pylist(rows, schema=FLF_SCHEMA)


def read_alf(handle: IO[bytes]) -> pa.Table:
    rows = []
    for raw in handle:
        line = raw.strip().decode("latin-1")
        if not line or "=" not in line:
            continue
        pairs = {}
        for part in line.split(","):
            key, _, value = part.partition("=")
            pairs[key.strip().upper()] = value.strip()
        if not {"M", "O", "D"} <= pairs.keys():
            continue

        # R is a seven-character bitmap, Monday first: "1111110" is Mon-Sat and
        # "0000001" Sunday only. The same link often appears twice with
        # different day sets and different durations, so both must survive.
        runs = pairs.get("R", "")
        row = {
            "mode": pairs["M"].upper(),
            "origin": pairs["O"],
            "destination": pairs["D"],
            "duration": int(pairs["T"]) if pairs.get("T", "").isdigit() else None,
            "start_time": _minutes(pairs.get("S")),
            "end_time": _minutes(pairs.get("E")),
            "priority": int(pairs["P"]) if pairs.get("P", "").isdigit() else None,
            "start_date": pairs.get("F") or None,
            "end_date": pairs.get("U") or None,
        }
        for index, day in enumerate(_DAYS):
            # An absent or malformed R field means the link runs every day.
            row[day] = runs[index] == "1" if len(runs) == 7 else True
        rows.append(row)
    return pa.Table.from_pylist(rows, schema=ALF_SCHEMA)


def read_tsi(handle: IO[bytes]) -> pa.Table:
    rows = []
    for raw in handle:
        parts = raw.strip().decode("latin-1").split(",")
        if len(parts) < 4 or not parts[3].strip().isdigit():
            continue
        rows.append(
            {
                "crs": parts[0].strip(),
                "from_toc": parts[1].strip(),
                "to_toc": parts[2].strip(),
                "minutes": int(parts[3].strip()),
            }
        )
    return pa.Table.from_pylist(rows, schema=TSI_SCHEMA)


SPECIAL_READERS = {
    "FLF": ("fixed_link", read_flf),
    "ALF": ("additional_fixed_link", read_alf),
    "TSI": ("toc_interchange", read_tsi),
}
