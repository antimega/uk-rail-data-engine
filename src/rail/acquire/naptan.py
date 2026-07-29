"""NaPTAN - the Department for Transport's gazetteer of public transport stops.

**Licensing.** Published under the **Open Government Licence v3**, Crown
copyright, and stated as such on the service's own terms page. Copying,
publishing and adapting are permitted provided the source is acknowledged and
the licence named - attribute the **Department for Transport**. That is the same
licence family as the Network Rail FOI grid file but a *different* attribution,
and both sit alongside the National Rail attribution the DTD feeds require.

No account, no key, no rate limit stated: an ordinary public API. Unlike the FOI
spreadsheet this is a **maintained dataset** with a live endpoint, so it can be
refetched, which is the whole reason it is worth having.

## Why it earns its place

The join is `ATCOCode` - rail stations sit in the `9100` namespace and the rest
of the code **is the TIPLOC**: `9100ABDARE` is `ABDARE`. So NaPTAN attaches to
the existing crosswalk with no new identifier and no fuzzy matching.

It arrived to settle a question two other sources could not. MSN's grid
references are about a kilometre accurate and the FOI spreadsheet is exact but
frozen, and where they disagreed there was no way to tell which was right - so
the merge kept MSN and recorded 31 conflicts. NaPTAN adjudicates 30 of them, and
**the conservative choice turns out to have been wrong more often than right**:

```
backs the FOI file   16   Stansted Airport 2.8 km, Kirk Sandall 3.2 km - MSN wrong
backs MSN            14   Highbury & Islington 58 km, Inverness 5.1 km - the file wrong
neither               0
```

Elsewhere it corroborates rather than corrects: against the 2,488 positions the
FOI file had already refined, the median difference is 33 m and **not one
exceeds a kilometre**. It also covers all 103 rail stations the FOI file missed.

So three sources, and the rule is **corroboration rather than hierarchy**: a
position is taken when a second source agrees within a kilometre. See
`model/reference.py` for the resolution.

## What it does not do

The `9100` namespace is rail. Metrolink, bus and ferry locations live under
other prefixes keyed on ATCO rather than TIPLOC, so **458 of the 530 non-rail
locations have no entry here** and NaPTAN cannot improve on RSPS5052's
rail-station flag for them. That was the hoped-for second benefit and it did not
materialise.

The national file is ~100 MB of which the rail namespace is ~2,700 rows, so only
the filtered rows are kept. The SHA-256 recorded is of the **whole download**,
so a figure remains traceable to the exact input even though the input is not
retained.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import duckdb

#: The service's own documented bulk endpoint. CSV rather than XML because the
#: fields wanted are flat and the XML carries a large schema for the rest.
NATIONAL_CSV = "https://naptan.api.dft.gov.uk/v1/access-nodes?dataFormat=csv"

#: ATCO prefix for the rail namespace. The remainder of the code is the TIPLOC.
RAIL_PREFIX = "9100"


@dataclass
class NaptanResult:
    rows: int
    active: int
    downloaded_bytes: int
    sha256: str


def fetch_naptan(
    parquet_dir: Path,
    *,
    timeout: int = 300,
    opener=urllib.request.urlopen,
    source_url: str = NATIONAL_CSV,
) -> NaptanResult:
    """Download NaPTAN and keep the rail namespace as `naptan_rail.parquet`.

    Streamed to a temporary file and hashed on the way past: the national CSV is
    around 100 MB and only about 2,700 rows of it are in the `9100` rail
    namespace, so retaining the whole thing would cost a hundredfold for nothing.
    The checksum is of the full download regardless, so the derived rows stay
    traceable to an exact input.
    """
    target = parquet_dir / "naptan"
    target.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(suffix=".csv") as scratch:
        with opener(source_url, timeout=timeout) as response:
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
                scratch.write(chunk)
        scratch.flush()

        connection = duckdb.connect()
        # all_varchar: several columns are blank for rail stops, and letting
        # DuckDB infer types from a 435,000-row file mostly about bus stops
        # produces surprises. The casts below are the only ones needed.
        connection.execute(f"""
            copy (
                select substr(ATCOCode, {len(RAIL_PREFIX) + 1}) as tiploc,
                       ATCOCode as atco_code,
                       CommonName as name,
                       StopType as stop_type,
                       lower(Status) = 'active' as is_active,
                       try_cast(Easting as bigint) as easting,
                       try_cast(Northing as bigint) as northing,
                       try_cast(Latitude as double) as latitude,
                       try_cast(Longitude as double) as longitude
                from read_csv('{scratch.name}', header=true, all_varchar=true)
                where ATCOCode like '{RAIL_PREFIX}%'
                  and try_cast(Easting as bigint) is not null
            ) to '{(target / "naptan_rail.parquet").as_posix()}' (format parquet)
        """)
        counts = connection.execute(
            f"select count(*), count(*) filter (where is_active) "
            f"from read_parquet('{(target / 'naptan_rail.parquet').as_posix()}')"
        ).fetchone()
        connection.close()

    (target / "manifest.json").write_text(json.dumps({
        "source_url": source_url,
        "sha256": digest.hexdigest(),
        "downloaded_bytes": size,
        "rail_rows": counts[0],
        "active_rows": counts[1],
        "licence": "Open Government Licence v3.0",
        "provenance": "Department for Transport, NaPTAN",
        "note": "Not a DTD feed. Attribution under OGL - naming the Department "
                "for Transport and the OGL - is required separately from the "
                "National Rail attribution the DTD feeds carry. Only the 9100 "
                "rail namespace is retained; the checksum is of the whole "
                "download.",
    }, indent=2))

    return NaptanResult(rows=counts[0], active=counts[1],
                        downloaded_bytes=size, sha256=digest.hexdigest())
