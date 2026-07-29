"""Precise grid references for TIPLOCs - a third source, under a third licence.

**Where it comes from, and why that matters.** This is neither a DTD feed nor
RSPS5052. It is a spreadsheet of TIPLOC eastings and northings released by
**Network Rail in response to a Freedom of Information request**, under the
**Open Government Licence v3** - which permits copying, publishing and adapting
provided the source is acknowledged and the OGL named. That obligation is
*different* from the National Rail attribution the DTD feeds carry, and anything
published from a mixture of the two owes both. Name Network Rail and the FOI
release, not "the routeing guide".

**An FOI disclosure is a snapshot, not a feed.** There is no publication
schedule, no versioning and no URL to poll, so this is imported by path and
never fetched. Two consequences worth keeping in mind:

* it **goes stale silently** as stations open, close and move, and nothing here
  will notice - the DTD feeds refresh around it while this does not;
* `rail refresh` rebuilds the database, which drops the refinement, so
  re-running `rail geography` afterwards is a manual step.

`grid_source` on `station` records which positions came from it, so the staleness
is at least visible. `data/` is git-ignored, so nothing here is redistributed by
the repository.

**Why it is worth having.** MSN's own grid references are about a kilometre
accurate - the working notes have said so from the start, and the numbers bear it
out: against this file the median disagreement is 59 m, which is MSN rounding to
the nearest 100 m. That is fine for labelling a station and useless for measuring
a distance.

**Neither source is authoritative, and that is the finding.** Of 2,535 stations
present in both, 31 disagree by more than a kilometre, and the disagreements are
not all one way:

* `HHY` Highbury & Islington - the spreadsheet places it 58 km away, in Kent.
  MSN is right.
* `PWW` Pollokshaws West - resolved by matching the *nearest* TIPLOC rather than
  the first, because the station also carries `BUSBYJ`, a junction 5 km off.

So the merge refines rather than overrides: where the two agree within a
kilometre the precise value wins on precision, and where they do not, MSN stands
and the conflict is recorded in `reference_reject`. Using each source to check
the other is the only check available, since there is no third.

**Parsing.** xlsx is a zip of XML, so this reads it with the standard library
rather than adding a spreadsheet dependency for one file. Gzipped or plain, both
are accepted - the copy this was built against arrived `.xlsx.gz`.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

import pyarrow as pa
import pyarrow.parquet as pq

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

#: MSN rounds to 100 m and the observed 90th-percentile disagreement is 156 m, so
#: anything past a kilometre is not a difference of precision - it is the two
#: sources naming different places, and neither can be trusted over the other.
AGREEMENT_METRES = 1000

TIPLOC_GRID = pa.schema([
    ("tiploc", pa.string()),
    ("name", pa.string()),
    ("easting", pa.int64()),
    ("northing", pa.int64()),
])


@dataclass
class GeographyCounts:
    tiplocs: int
    source: str
    sha256: str


def _cells(sheet: ET.Element, strings: list[str]):
    """Row by row, as {column letter: value}."""
    for row in sheet.iter(f"{_NS}row"):
        values: dict[str, str] = {}
        for cell in row.iter(f"{_NS}c"):
            column = re.match(r"[A-Z]+", cell.get("r") or "")
            value = cell.find(f"{_NS}v")
            if column is None or value is None or value.text is None:
                continue
            text = value.text
            if cell.get("t") == "s":
                index = int(text)
                text = strings[index] if index < len(strings) else ""
            values[column.group(0)] = text
        yield values


def read_tiploc_grid(path: Path) -> list[dict]:
    """TIPLOC, name, easting, northing from the FOI spreadsheet.

    Accepts the file gzipped or not. Rows without a numeric easting are skipped,
    which drops the header and any blank line without needing to know where they
    are.
    """
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)

    with zipfile.ZipFile(io.BytesIO(raw)) as book:
        strings = [
            node.text or ""
            for node in ET.fromstring(book.read("xl/sharedStrings.xml")).iter(f"{_NS}t")
        ] if "xl/sharedStrings.xml" in book.namelist() else []
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))

        found: list[dict] = []
        for values in _cells(sheet, strings):
            tiploc = (values.get("A") or "").strip()
            easting, northing = values.get("C", ""), values.get("D", "")
            if not tiploc or not easting.isdigit() or not northing.isdigit():
                continue
            found.append({
                "tiploc": tiploc,
                "name": (values.get("B") or "").strip() or None,
                "easting": int(easting),
                "northing": int(northing),
            })
    return found


def ingest_geography(path: Path, parquet_dir: Path) -> GeographyCounts:
    """Import the spreadsheet to `tiploc_grid.parquet`, with its checksum.

    The checksum is recorded for the same reason the DTD snapshots carry one:
    every figure derived from it should be traceable to the exact input, and this
    file arrives by hand rather than from a versioned feed.
    """
    rows = read_tiploc_grid(path)
    target = parquet_dir / "geography"
    target.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=TIPLOC_GRID),
                   target / "tiploc_grid.parquet")

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (target / "manifest.json").write_text(json.dumps({
        "source_file": path.name,
        "sha256": digest,
        "tiplocs": len(rows),
        "licence": "Open Government Licence v3.0",
        "provenance": "Network Rail, released under FOI",
        "note": "Not a DTD feed, and not a maintained publication: an FOI "
                "disclosure is a point-in-time snapshot with no refresh. "
                "Attribution under OGL - naming Network Rail and the OGL - is "
                "required separately from the National Rail attribution the DTD "
                "feeds carry.",
    }, indent=2))
    return GeographyCounts(tiplocs=len(rows), source=path.name, sha256=digest)
