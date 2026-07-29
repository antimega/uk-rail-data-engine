"""RDG Supplementary Reference Data (RSPS5052).

**A different source with different terms.** These files are published on a
public S3 bucket with no authentication and are not DTD feeds, so the NRDP
Developer Terms you accepted do not cover them. Check RSPS5052's own licensing
before publishing anything derived from them. That boundary is why this lives
apart from :mod:`rail.acquire.nrdp` and why `rail fetch --supplementary` is a
separate switch rather than another value of ``--feed``.

**The URLs are http, not https**, exactly as the spec prints them. The bucket
name contains dots, so it cannot be addressed virtual-host style under Amazon's
wildcard certificate, and path-style addressing is refused with a permanent
redirect. Nothing here is authenticated and every file is public reference data,
so the exposure is that a middlebox could serve you a wrong station list —
which is what the recorded SHA-256 is for.

Two files are used, both chosen for a specific job:

* ``RailStations`` — which CRS codes are GB rail stations. MSN mixes in bus and
  ferry interchange points (`HELSTON BUS`, `ASHURST BALD FACED STAG`) that are
  legitimate data but not stations. 530 of our 3,109 are not on this list.
  Note §7.1.2: the data is informational and **must not** affect journey
  planning or ticket selection, so it lands as a flag on `station` and is used
  to label output, never to prune the network.
* ``FlexiProducts`` — bundle sizes for flexi season and carnet tickets. A
  carnet prices 12 or 50 journeys as one ticket, and nothing in RSPS5045 says
  so: min and max passengers are both 1, so every walk-up filter passes it.
* ``PlusbusExcludedStationPairs`` — pairs an add-on may not be sold for, both
  ends being in one zone. Like everything else in these feeds it is a version
  history: the file carries two annual generations and half of it has expired,
  so it must be filtered on the travel date.
* ``PlusBusWebPages`` — the scheme page per zone, which is where the map and
  the list of operators actually live.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .snapshots import sha256_file

#: Spec section 2.2 and 6.2. http for the reason given in the module docstring.
BASE_URL = "http://datafeeds.rdg.s3.amazonaws.com/RSPS5052/"

#: Version numbers are part of the filename, so a new edition is a new file
#: rather than a silent change of content.
RAIL_STATIONS = "RailStations02-00.csv"
FLEXI_PRODUCTS = "FlexiProducts02-01.csv"
#: Station pairs a PlusBus add-on may not be sold for, because both ends sit in
#: the same zone — Buxton and Matlock, not Derby and Buxton. Reversible: a
#: record from A to B applies from B to A.
PLUSBUS_EXCLUDED = "PlusbusExcludedStationPairs02-00.csv"
PLUSBUS_PAGES = "PlusBusWebPages01-00.csv"
FILES = (RAIL_STATIONS, FLEXI_PRODUCTS, PLUSBUS_EXCLUDED, PLUSBUS_PAGES)

RAIL_STATION_SCHEMA = pa.schema([("crs", pa.string())])
PLUSBUS_EXCLUDED_SCHEMA = pa.schema([
    ("start_date", pa.date32()), ("end_date", pa.date32()),
    ("from_nlc", pa.string()), ("to_nlc", pa.string()),
])
PLUSBUS_PAGE_SCHEMA = pa.schema([("nlc", pa.string()), ("url", pa.string())])
FLEXI_SCHEMA = pa.schema([
    ("ticket_code", pa.string()),
    ("start_date", pa.date32()),
    ("end_date", pa.date32()),
    ("bundle_size", pa.int32()),
    ("bi_directional", pa.bool_()),
    ("transferable", pa.bool_()),
])


@dataclass(frozen=True)
class SupplementaryResult:
    filename: str
    path: Path
    size: int
    sha256: str
    last_modified: str | None
    rows: int


def _read_date(value: str) -> dt.date | None:
    """Parse a date the file's way, not the spec's.

    §6.4 documents YYYY/MM/DD; the published file uses YYYY-MM-DD. The version
    history shows the date format in this document has been changed and changed
    back, so accept both rather than pick a side.
    """
    text = (value or "").strip().replace("/", "-")
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def fetch_supplementary(
    raw_dir: Path, *, timeout: int = 60, opener=urllib.request.urlopen
) -> list[SupplementaryResult]:
    """Download the RSPS5052 files used here into ``raw_dir/supplementary``."""
    target = raw_dir / "supplementary"
    target.mkdir(parents=True, exist_ok=True)

    results = []
    for name in FILES:
        with opener(BASE_URL + name, timeout=timeout) as response:
            body = response.read()
            last_modified = response.headers.get("Last-Modified")
        path = target / name
        path.write_bytes(body)
        results.append(
            SupplementaryResult(
                filename=name,
                path=path,
                size=len(body),
                sha256=sha256_file(path),
                last_modified=last_modified,
                rows=sum(1 for line in body.splitlines() if line.strip()),
            )
        )
    return results


def ingest_supplementary(raw_dir: Path, parquet_dir: Path) -> dict[str, int]:
    """Parse the downloaded CSVs into Parquet. Returns rows written per table."""
    source = raw_dir / "supplementary"
    target = parquet_dir / "supplementary"
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}

    stations_csv = source / RAIL_STATIONS
    if stations_csv.exists():
        # One quoted CRS code per line, no header.
        codes = [
            {"crs": row[0].strip()}
            for row in csv.reader(io.StringIO(stations_csv.read_text()))
            if row and row[0].strip()
        ]
        pq.write_table(
            pa.Table.from_pylist(codes, schema=RAIL_STATION_SCHEMA),
            target / "rail_station.parquet",
        )
        written["rail_station"] = len(codes)

    flexi_csv = source / FLEXI_PRODUCTS
    if flexi_csv.exists():
        products = []
        for row in csv.reader(io.StringIO(flexi_csv.read_text())):
            if len(row) < 6 or not row[0].strip():
                continue
            products.append({
                "ticket_code": row[0].strip(),
                "start_date": _read_date(row[1]),
                "end_date": _read_date(row[2]),
                "bundle_size": int(row[3]) if row[3].strip().isdigit() else None,
                "bi_directional": row[4].strip() == "1",
                "transferable": row[5].strip() == "1",
            })
        pq.write_table(
            pa.Table.from_pylist(products, schema=FLEXI_SCHEMA),
            target / "flexi_product.parquet",
        )
        written["flexi_product"] = len(products)

    excluded_csv = source / PLUSBUS_EXCLUDED
    if excluded_csv.exists():
        pairs = []
        for row in csv.reader(io.StringIO(excluded_csv.read_text())):
            if len(row) < 4:
                continue
            pairs.append({
                "start_date": _read_date(row[0]), "end_date": _read_date(row[1]),
                "from_nlc": row[2].strip(), "to_nlc": row[3].strip(),
            })
        pq.write_table(
            pa.Table.from_pylist(pairs, schema=PLUSBUS_EXCLUDED_SCHEMA),
            target / "plusbus_excluded_pair.parquet",
        )
        written["plusbus_excluded_pair"] = len(pairs)

    pages_csv = source / PLUSBUS_PAGES
    if pages_csv.exists():
        pages = [
            {"nlc": row[0].strip(), "url": row[1].strip()}
            for row in csv.reader(io.StringIO(pages_csv.read_text()))
            if len(row) >= 2 and row[0].strip()
        ]
        pq.write_table(
            pa.Table.from_pylist(pages, schema=PLUSBUS_PAGE_SCHEMA),
            target / "plusbus_web_page.parquet",
        )
        written["plusbus_web_page"] = len(pages)

    return written
