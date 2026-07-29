"""Reader for the National Routeing Guide feed (RSPS5047).

Unlike the fares and timetable feeds these files are comma-separated, with two
kinds of comment: the ``/!!`` header block every DTD file carries, and plain
``/`` lines naming the record that follows, which is a human aid and carries no
data.

The guide answers a different question from the fares feed. Fares say what a
ticket costs and, sometimes, that it is "VIA SHREWSBURY". The guide says which
physical routes a ticket entitles you to take at all, and it is the reason
"ANY PERMITTED" is not the same as "any route".
"""

from __future__ import annotations

import datetime as dt

import zipfile
from pathlib import Path
from typing import Iterator

import pyarrow as pa

#: Files with no records in this export, and the ones that need no parsing.
EMPTY_OR_INDEX = {"RGI", "DAT", "RGA", "RGV"}


def read_records(handle) -> Iterator[list[str]]:
    """Yield comma-separated records, skipping both styles of comment."""
    for raw in handle:
        line = raw.rstrip(b"\r\n").decode("latin-1")
        if not line or line.startswith("/"):
            continue
        yield [field.strip() for field in line.split(",")]


def _routeing_date(value: str | None) -> dt.date | None:
    """ddmmyyyy, as the fares feed uses. `31122999` is the open-ended sentinel
    and is kept as a real date so a `between` needs no special case."""
    value = (value or "").strip()
    if len(value) != 8 or not value.isdigit():
        return None
    try:
        return dt.date(int(value[4:8]), int(value[2:4]), int(value[0:2]))
    except ValueError:
        return None


def _table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    return pa.Table.from_pylist(rows, schema=schema)


ROUTEING_POINT = pa.schema([("crs", pa.string())])
STATION_POINT = pa.schema([
    ("crs", pa.string()), ("routeing_point", pa.string()),
])
STATION_GROUP_MEMBER = pa.schema([
    ("crs", pa.string()), ("group_code", pa.string()),
])
PERMITTED = pa.schema([
    ("origin", pa.string()), ("destination", pa.string()),
    ("route_id", pa.int64()), ("seq", pa.int32()), ("map_code", pa.string()),
])
MAP_LINK = pa.schema([
    ("map_code", pa.string()), ("from_crs", pa.string()), ("to_crs", pa.string()),
])
#: RSPS5047 4.9: a section of line between two *adjacent* stations over which
#: there is a passenger service, with its distance in miles to two decimals.
#: Every record has a reverse — checked, all 5,874 do, and none disagrees on the
#: distance — so the graph is undirected in practice despite being stored twice.
STATION_LINK = pa.schema([
    ("from_crs", pa.string()), ("to_crs", pa.string()),
    ("miles", pa.float64()),
])
#: RSPS5047 4.14. A station created since NFM64, and the older station whose
#: fares stand in for it "when obtaining fares for Routeing Guide Fare
#: checking" — `LUT,LTN` means Luton Airport Parkway uses Luton's.
NEW_STATION = pa.schema([
    ("crs", pa.string()), ("equivalent_crs", pa.string()),
    ("start_date", pa.date32()), ("end_date", pa.date32()),
])
#: RSPS5047 4.15, the routeing feed's own CRS/NLC cross-reference — a third
#: opinion on the crosswalk the whole stack is joined on.
LOCATION_XREF = pa.schema([
    ("nlc", pa.string()), ("fare_group", pa.string()), ("crs", pa.string()),
    ("county_code", pa.string()), ("zone_code", pa.string()),
    ("start_date", pa.date32()), ("end_date", pa.date32()),
])
NAMED = pa.schema([("code", pa.string()), ("name", pa.string())])
GROUP = pa.schema([("group_code", pa.string()), ("crs", pa.string())])
EASEMENT_TEXT = pa.schema([("easement_id", pa.string()), ("description", pa.string())])
EASEMENT = pa.schema([
    ("easement_ref", pa.string()),
    ("start_date", pa.string()), ("end_date", pa.string()),
    ("text_ref", pa.string()),
    #: 1 sleeper, 2 disabled passenger, 3 normal, 4 service variation.
    ("easement_type", pa.string()),
    #: 1 grants a route the maps refuse, 2 withdraws one they allow.
    ("easement_class", pa.string()),
    ("category", pa.string()),
    ("monday", pa.bool_()), ("tuesday", pa.bool_()), ("wednesday", pa.bool_()),
    ("thursday", pa.bool_()), ("friday", pa.bool_()), ("saturday", pa.bool_()),
    ("sunday", pa.bool_()),
    ("start_time", pa.string()), ("end_time", pa.string()),
])
EASEMENT_LOCATION = pa.schema([
    ("easement_ref", pa.string()), ("crs", pa.string()),
    #: 1 applicable, 2 origin, 3 destination, 4 via, 5 exclude, 6 doubleback.
    ("modifier", pa.string()),
])
EASEMENT_DETAIL = pa.schema([
    ("easement_ref", pa.string()),
    #: 1 train UID, 2 TOC, 3 ticket route, 4 ticket code.
    ("detail_ref", pa.string()), ("detail_code", pa.string()),
])
#: RGH, "easement TOC" — one row per easement per operator it is tied to.
#:
#: This is where the operator conditions actually live. RGF's own `D` records
#: carry a `detail_ref = '2'` for the same thing and there are **eight** of
#: them; RGH names 942 easements against 35 operators, and exactly one easement
#: appears in both. Reading only RGF meant `unsettleable` was deciding on 8
#: easements where the feed describes 624 of the ones we hold.
EASEMENT_TOC = pa.schema([
    ("easement_ref", pa.string()), ("toc", pa.string()),
])
ROUTE_LONDON = pa.schema([
    ("route_code", pa.string()), ("london_marker", pa.string()),
])
ROUTE_CONDITION = pa.schema([
    ("route_code", pa.string()), ("entry_type", pa.string()),
    ("crs", pa.string()), ("is_group", pa.bool_()),
    ("mode_code", pa.string()), ("toc_id", pa.string()),
])
LONDON = pa.schema([
    ("crs", pa.string()), ("is_terminal", pa.bool_()), ("cross_london", pa.bool_()),
])


def read_routeing(zip_path: Path) -> dict[str, pa.Table]:
    """Parse the routeing guide ZIP into tables."""
    points: list[dict] = []
    station_points: list[dict] = []
    permitted: list[dict] = []
    links: list[dict] = []
    maps: list[dict] = []
    groups: list[dict] = []
    easements: list[dict] = []
    members_of_group: list[dict] = []
    nodes: list[dict] = []
    london: list[dict] = []
    route_london: list[dict] = []
    conditions: list[dict] = []
    easement_defs: list[dict] = []
    easement_locations: list[dict] = []
    easement_details: list[dict] = []
    easement_tocs: list[dict] = []
    station_links: list[dict] = []
    new_stations: list[dict] = []
    location_xref: list[dict] = []

    with zipfile.ZipFile(zip_path) as archive:
        members = {Path(n).suffix.lstrip(".").upper(): n for n in archive.namelist()}

        with archive.open(members["RGP"]) as fh:
            points = [{"crs": r[0]} for r in read_records(fh) if r[0]]

        # Links run between *nodes* — routeing points and interchange points —
        # so path reduction has to use this list, not the routeing points alone.
        with archive.open(members["RGN"]) as fh:
            nodes = [{"crs": r[0]} for r in read_records(fh) if r[0]]

        # RSPS5047 4.9. This is what the guide's own shortest-route rules are
        # written against (7.2.4), and without it sections 7.1.2 and 7.1.3 —
        # "permitted if it is the shortest distance, or within 3 miles of it" —
        # cannot be evaluated at all.
        with archive.open(members["RGD"]) as fh:
            for record in read_records(fh):
                if len(record) < 3 or not record[0] or not record[1]:
                    continue
                try:
                    miles = float(record[2])
                except ValueError:
                    continue
                station_links.append({"from_crs": record[0], "to_crs": record[1],
                                      "miles": miles})

        # RSPS5047 4.14. Confirms which stations are new from a third file, and
        # names the station whose fares the guide substitutes for them.
        with archive.open(members["RGX"]) as fh:
            for record in read_records(fh):
                if len(record) < 4 or not record[0] or not record[1]:
                    continue
                new_stations.append({
                    "crs": record[1], "equivalent_crs": record[0],
                    "start_date": _routeing_date(record[2]),
                    "end_date": _routeing_date(record[3]),
                })

        # RSPS5047 4.15. Field 1 is the admin area (always '70' here) and is not
        # kept; the point of the file is fields 2-4.
        with archive.open(members["RGY"]) as fh:
            for record in read_records(fh):
                if len(record) < 8 or not record[1]:
                    continue
                location_xref.append({
                    "nlc": record[1], "fare_group": record[2] or None,
                    "crs": record[3] or None, "county_code": record[4] or None,
                    "zone_code": record[5] or None,
                    "start_date": _routeing_date(record[6]),
                    "end_date": _routeing_date(record[7]),
                })

        # Fields 2-5 are up to four routeing points; field 6 is the station
        # group, which is NOT a routeing point reference. Per RSPS5047 4.2.1.2 a
        # station with none listed is itself a routeing point, or belongs to a
        # group which is one — so the group stands in for it.
        with archive.open(members["RGS"]) as fh:
            for record in read_records(fh):
                if not record or not record[0]:
                    continue
                crs = record[0]
                listed = [f for f in record[1:5] if f]
                group = record[5] if len(record) > 5 and record[5] else None
                if group:
                    members_of_group.append({"crs": crs, "group_code": group})
                for point in listed or [group or crs]:
                    station_points.append({"crs": crs, "routeing_point": point})

        # A permitted route is an ordered chain of maps, so each is numbered and
        # its maps kept in sequence: AAP,CNR,AC,CG,EG,FW is one route of four.
        with archive.open(members["RGR"]) as fh:
            for route_id, record in enumerate(read_records(fh)):
                if len(record) < 3:
                    continue
                origin, destination, chain = record[0], record[1], record[2:]
                for position, map_code in enumerate(chain):
                    if map_code:
                        permitted.append({
                            "origin": origin, "destination": destination,
                            "route_id": route_id, "seq": position,
                            "map_code": map_code,
                        })

        # Cross-London journeys are validated in two halves with a transfer
        # between terminals, so the markers say which stations may be used.
        with archive.open(members["RGC"]) as fh:
            for record in read_records(fh):
                if len(record) >= 3 and record[0]:
                    london.append({
                        "crs": record[0],
                        "is_terminal": record[1].upper() == "Y",
                        "cross_london": record[2].upper() == "Y",
                    })

        # RGF is where the published exceptions actually live. RGE, which was
        # all this read before, carries only their prose descriptions.
        with archive.open(members["RGF"]) as fh:
            for record in read_records(fh):
                if len(record) < 2 or not record[1]:
                    continue
                kind, ref = record[0], record[1]
                if kind == "E" and len(record) >= 9:
                    days = record[8]
                    easement_defs.append({
                        "easement_ref": ref,
                        "start_date": record[2] or None,
                        "end_date": record[3] or None,
                        "text_ref": record[4] or None,
                        "easement_type": record[5] or None,
                        "easement_class": record[6] or None,
                        "category": record[7] or None,
                        **{
                            day: len(days) > i and days[i].upper() == "Y"
                            for i, day in enumerate((
                                "monday", "tuesday", "wednesday", "thursday",
                                "friday", "saturday", "sunday",
                            ))
                        },
                        "start_time": (record[9] or None) if len(record) > 9 else None,
                        "end_time": (record[10] or None) if len(record) > 10 else None,
                    })
                elif kind == "L" and len(record) >= 4:
                    easement_locations.append({
                        "easement_ref": ref,
                        "crs": record[2],
                        "modifier": record[3],
                    })
                elif kind == "D" and len(record) >= 4:
                    easement_details.append({
                        "easement_ref": ref,
                        "detail_ref": record[2],
                        "detail_code": record[3],
                    })
                # 'X' exception records: none in this export, and they qualify
                # an easement by train or TOC — neither of which a list of
                # calling points can be judged against anyway.

        # RGH ties easements to operators, and is the file RGF's own `D`
        # records only hint at: eight of those against 993 rows here. Two
        # fields, `easement_ref,TOC`, and no header beyond the `/!!` block.
        with archive.open(members["RGH"]) as fh:
            for record in read_records(fh):
                if len(record) < 2 or not record[0] or not record[1]:
                    continue
                easement_tocs.append({
                    "easement_ref": record[0], "toc": record[1].strip().upper(),
                })

        # RGK says what a fare's route code actually requires. The fares feed's
        # own RTE records carry only include/exclude per location; this carries
        # the distinctions that make the condition enforceable — ALL-of versus
        # ANY-of, a marker saying a CRS stands for its whole routeing group, and
        # London as a marker rather than a list of terminals to guess at.
        with archive.open(members["RGK"]) as fh:
            for record in read_records(fh):
                if len(record) < 3 or not record[0]:
                    continue
                code, kind = record[0], record[1]
                if kind == "L":
                    route_london.append(
                        {"route_code": code, "london_marker": record[2]}
                    )
                elif kind == "D":
                    entry = record[2]
                    conditions.append({
                        "route_code": code,
                        "entry_type": entry,
                        "crs": record[3] or None if len(record) > 3 else None,
                        # "one station in a routeing guide group", meaning the
                        # whole group is included or excluded, not just this
                        # station. 58 records set it.
                        "is_group": len(record) > 4 and record[4].upper() == "Y",
                        "mode_code": record[5] or None if len(record) > 5 else None,
                        "toc_id": record[6] or None if len(record) > 6 else None,
                    })

        with archive.open(members["RGL"]) as fh:
            for record in read_records(fh):
                if len(record) >= 3 and all(record[:3]):
                    links.append({
                        "map_code": record[2],
                        "from_crs": record[0], "to_crs": record[1],
                    })

        for key, target, schema in (
            ("RGM", maps, NAMED), ("RGG", groups, GROUP), ("RGE", easements, EASEMENT_TEXT)
        ):
            with archive.open(members[key]) as fh:
                for record in read_records(fh):
                    if not record or not record[0]:
                        continue
                    if schema is NAMED:
                        target.append({"code": record[0], "name": ""})
                    elif schema is GROUP and len(record) >= 2:
                        target.append({"group_code": record[0], "crs": record[1]})
                    elif schema is EASEMENT_TEXT and len(record) >= 2:
                        target.append({
                            "easement_id": record[0],
                            "description": ",".join(record[1:]),
                        })

    return {
        "routeing_point": _table(points, ROUTEING_POINT),
        "routeing_node": _table(nodes, ROUTEING_POINT),
        "station_routeing_point": _table(station_points, STATION_POINT),
        "permitted_route": _table(permitted, PERMITTED),
        "routeing_map_link": _table(links, MAP_LINK),
        "station_link": _table(station_links, STATION_LINK),
        "routeing_new_station": _table(new_stations, NEW_STATION),
        "routeing_location": _table(location_xref, LOCATION_XREF),
        "routeing_map": _table(maps, NAMED),
        "routeing_group": _table(groups, GROUP),
        "station_group_member": _table(members_of_group, STATION_GROUP_MEMBER),
        "easement_text": _table(easements, EASEMENT_TEXT),
        "london_station": _table(london, LONDON),
        "route_london": _table(route_london, ROUTE_LONDON),
        "route_condition": _table(conditions, ROUTE_CONDITION),
        "easement": _table(easement_defs, EASEMENT),
        "easement_location": _table(easement_locations, EASEMENT_LOCATION),
        "easement_detail": _table(easement_details, EASEMENT_DETAIL),
        "easement_toc": _table(easement_tocs, EASEMENT_TOC),
    }
