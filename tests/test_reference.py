"""The station crosswalk - the one table that silently corrupts everything else.

Fixtures reproduce the real shapes found in RJTTF904/RJFAF833: an MSN header
line that parses as a station, Irish entries with sentinel zero coordinates,
stations with several TIPLOCs, and a fares LOC file that is a version history
rather than a current view.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rail.model import build_reference
from rail.model.reference import (
    NON_NATIONAL_RAIL_OPERATORS,
    classify_locations,
)

FAR_FUTURE = dt.date(2999, 12, 31)
LONG_AGO = dt.date(2000, 1, 1)


def write(path, rows, schema):
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


MSN_SCHEMA = pa.schema(
    [
        ("station_name", pa.string()),
        ("tiploc_code", pa.string()),
        ("crs_code", pa.string()),
        ("cate_interchange_status", pa.string()),
        ("easting", pa.int64()),
        ("northing", pa.int64()),
        ("minimum_change_time", pa.int64()),
    ]
)

TIPLOC_SCHEMA = pa.schema([("tiploc_code", pa.string()), ("crs_code", pa.string())])

LOCATION_SCHEMA = pa.schema(
    [
        ("crs", pa.string()),
        ("nlc", pa.string()),
        ("uic", pa.string()),
        ("fare_group", pa.string()),
        # PlusBus zones name themselves "BATH+BUS" and are excluded by it.
        ("description", pa.string()),
        ("start_date", pa.date32()),
        ("end_date", pa.date32()),
    ]
)

CLUSTER_SCHEMA = pa.schema(
    [
        ("cluster_id", pa.string()),
        ("cluster_nlc", pa.string()),
        ("start_date", pa.date32()),
        ("end_date", pa.date32()),
    ]
)


@pytest.fixture
def built(tmp_path):
    timetable = tmp_path / "timetable"
    fares = tmp_path / "fares"
    timetable.mkdir()
    fares.mkdir()

    write(
        timetable / "physical_station.parquet",
        [
            # The MSN header line is itself an "A" record and parses to junk.
            {"station_name": "F", "tiploc_code": None, "crs_code": "1/0",
             "cate_interchange_status": None, "easting": None, "northing": None,
             "minimum_change_time": None},
            {"station_name": "YORK", "tiploc_code": "YORK", "crs_code": "YRK",
             "cate_interchange_status": "9", "easting": 14596, "northing": 64517,
             "minimum_change_time": 8},
            # Same station, second TIPLOC, and this row carries no coordinates.
            {"station_name": "YORK", "tiploc_code": "YORKYSJ", "crs_code": "YRK",
             "cate_interchange_status": "9", "easting": 0, "northing": 0,
             "minimum_change_time": 8},
            # Irish CIE entry: a real station, but no grid reference at all.
            {"station_name": "ATHENRY (CIE)", "tiploc_code": "ATHENRY", "crs_code": "ATR",
             "cate_interchange_status": "0", "easting": 0, "northing": 0,
             "minimum_change_time": 5},
        ],
        MSN_SCHEMA,
    )
    write(
        timetable / "tiploc.parquet",
        [
            {"tiploc_code": "YORK", "crs_code": "YRK"},
            {"tiploc_code": "YRKSDG", "crs_code": None},  # timing point, no CRS
            # A TI reference can describe a location absent from MSN. Existing
            # reachability semantics retain it in station_tiploc for now.
            {"tiploc_code": "MILESPL", "crs_code": "MLP"},
        ],
        TIPLOC_SCHEMA,
    )
    write(
        fares / "location.parquet",
        [
            # A superseded version of York that must not win.
            {"crs": "YRK", "nlc": "0000", "uic": "7000001", "fare_group": "0000",
             "start_date": LONG_AGO, "end_date": dt.date(2020, 1, 1)},
            {"crs": "YRK", "nlc": "8263", "uic": "7008263", "fare_group": "8263",
             "start_date": dt.date(2024, 1, 1), "end_date": FAR_FUTURE},
            {"crs": "ATR", "nlc": "9999", "uic": "7009999", "fare_group": "9999",
             "start_date": LONG_AGO, "end_date": FAR_FUTURE},
        ],
        LOCATION_SCHEMA,
    )
    write(
        fares / "station_cluster.parquet",
        [
            {"cluster_id": "1072", "cluster_nlc": "1444",
             "start_date": LONG_AGO, "end_date": FAR_FUTURE},
            {"cluster_id": "0001", "cluster_nlc": "0002",
             "start_date": LONG_AGO, "end_date": dt.date(2020, 1, 1)},  # expired
        ],
        CLUSTER_SCHEMA,
    )

    connection = duckdb.connect()
    counts = build_reference(connection, timetable, fares)
    return connection, counts


def test_header_line_is_rejected_not_treated_as_a_station(built):
    connection, counts = built

    assert counts.stations == 2  # YRK and ATR, not the header
    reasons = connection.execute(
        "select reason from reference_reject where key = 'F' or key is null"
    ).fetchall()
    assert reasons == [("crs is not three letters",)]


def test_grid_references_decode_and_sentinel_zero_stays_null(built):
    connection, _ = built
    york, athenry = connection.execute(
        "select crs, easting, northing from station order by crs desc"
    ).fetchall()

    # (14596 - 10000) * 100 = 459600 - York, to within about 100 metres.
    assert york == ("YRK", 459600, 451700)
    # A stored zero means "unknown", not the origin of the grid.
    assert athenry == ("ATR", None, None)


def test_station_prefers_the_row_that_has_coordinates(built):
    connection, _ = built
    assert connection.execute(
        "select easting from station where crs = 'YRK'"
    ).fetchone() == (459600,)


def test_all_tiplocs_for_a_station_are_kept(built):
    connection, _ = built
    tiplocs = connection.execute(
        "select tiploc from station_tiploc where crs = 'YRK' order by tiploc"
    ).fetchall()

    # A stop at either TIPLOC has to resolve to York.
    assert tiplocs == [("YORK",), ("YORKYSJ",)]


def test_timing_points_without_a_crs_are_not_stations(built):
    connection, _ = built
    assert connection.execute(
        "select count(*) from station_tiploc where tiploc = 'YRKSDG'"
    ).fetchone() == (0,)


def test_operational_crs_is_additive_and_preserves_existing_mapping(built):
    connection, _ = built

    assert connection.execute(
        "select crs from tiploc_crs where tiploc = 'MILESPL'"
    ).fetchone() == ("MLP",)
    assert connection.execute(
        "select crs from station_tiploc where tiploc = 'MILESPL'"
    ).fetchone() == ("MLP",)


def test_only_the_currently_valid_fares_record_is_used(built):
    connection, _ = built
    assert connection.execute(
        "select nlc, fare_group from station_nlc where crs = 'YRK'"
    ).fetchone() == ("8263", "8263")


def test_expired_clusters_are_excluded(built):
    connection, counts = built
    assert counts.clusters == 1
    assert connection.execute(
        "select cluster_id from station_cluster"
    ).fetchall() == [("1072",)]


def test_stations_without_coordinates_are_recorded_not_dropped(built):
    connection, _ = built
    # Athenry has no grid reference but is still a station you can route to.
    assert connection.execute(
        "select count(*) from station where crs = 'ATR'"
    ).fetchone() == (1,)
    assert connection.execute(
        "select count(*) from reference_reject where reason = 'no grid reference'"
    ).fetchone() == (1,)


# --- what a location actually is ---------------------------------------------


def kinds_world(*, calls):
    """Stations classified by what calls at them.

    `calls` is (crs, train_status, atoc_code) - the two fields that between them
    say what kind of service it was and who ran it - optionally followed by the
    schedule's source file and train category, which is what tells an operator
    marker from a place. They default to an ordinary CIF working.
    """
    c = duckdb.connect()
    c.execute("create table station (crs varchar, name varchar)")
    c.execute("create table station_tiploc (crs varchar, tiploc varchar)")
    c.execute("create table train_schedule "
              "(schedule_id bigint, train_status varchar, atoc_code varchar, "
              " source varchar, train_category varchar)")
    c.execute("create table schedule_stop (schedule_id bigint, location varchar, crs varchar)")
    seen = set()
    for index, call in enumerate(calls, start=1):
        crs, status, toc = call[:3]
        source, category = (call + ("cif", "OO"))[3:5]
        if crs not in seen:
            c.execute("insert into station values (?, ?)", [crs, crs])
            c.execute("insert into station_tiploc values (?, ?)", [crs, crs + "TIP"])
            seen.add(crs)
        c.execute("insert into train_schedule values (?, ?, ?, ?, ?)",
                  [index, status, toc, source, category])
        c.execute("insert into schedule_stop values (?, ?, ?)", [index, crs + "TIP", crs])
    classify_locations(c)
    return dict(c.execute("select crs, kind from station").fetchall())


def test_a_station_a_train_calls_at_is_a_rail_station():
    assert kinds_world(calls=[("AAA", "P", "GW")]) == {"AAA": "rail"}


def test_a_bus_stop_and_a_ferry_pier_are_told_apart():
    """MSN mixes them in with stations and RSPS5052's boolean cannot: it says
    only "not a rail station" for a coach bay, a pier and a Metro platform
    alike. CIF train status says which - `B`/`5` a bus, `S`/`4` a ship."""
    kinds = kinds_world(calls=[("BUS", "B", "AW"), ("SHP", "S", "QC"),
                               ("BU5", "5", "AW"), ("SH4", "4", "QC")])

    assert kinds == {"BUS": "bus", "SHP": "ferry", "BU5": "bus", "SH4": "ferry"}


def test_a_metro_station_is_not_a_rail_station():
    """Tyne & Wear Metro runs in CIF because it shares the network, not because
    a National Rail train calls. 21 stations are reachable only that way -
    Fellgate, Stadium of Light, St Peters, Seaburn."""
    assert kinds_world(calls=[("FEG", "P", "TW")]) == {"FEG": "metro"}


def test_a_national_rail_train_at_a_metro_station_makes_it_rail():
    """The feeds draw this line nowhere - the fares feed's TOC file lists all 86
    operators alike, Tyne & Wear Metro beside GWR - so the operator list is
    curated and deliberately short."""
    kinds = kinds_world(calls=[("XXX", "P", "TW"), ("XXX", "P", "NT")])

    assert kinds == {"XXX": "rail"}
    assert set(NON_NATIONAL_RAIL_OPERATORS) == {"TW", "LT"}


def test_a_train_outranks_everything_else_that_stops_there():
    """A place a train calls at is a station whatever else also stops, and a
    ferry terminal with a connecting bus is still a ferry terminal."""
    kinds = kinds_world(calls=[("AAA", "P", "GW"), ("AAA", "B", "GW"),
                               ("FER", "S", "QC"), ("FER", "B", "AW")])

    assert kinds == {"AAA": "rail", "FER": "ferry"}


def test_a_location_nothing_calls_at_is_unserved():
    """368 of them, reachable only by fixed link if at all - which is why they
    show up in a sweep with no fare."""
    c = duckdb.connect()
    c.execute("create table station (crs varchar, name varchar)")
    c.execute("insert into station values ('ZZZ', 'NOWHERE')")
    c.execute("create table station_tiploc (crs varchar, tiploc varchar)")
    c.execute("create table train_schedule "
              "(schedule_id bigint, train_status varchar, atoc_code varchar, "
              " source varchar, train_category varchar)")
    c.execute("create table schedule_stop (schedule_id bigint, location varchar, crs varchar)")

    classify_locations(c)

    assert c.execute("select kind from station").fetchone() == ("unserved",)


def test_the_evidence_is_kept_not_just_the_verdict():
    """`station_service` holds the calls the classification rests on, so a
    surprising verdict can be argued with rather than taken on trust."""
    c = duckdb.connect()
    c.execute("create table station (crs varchar, name varchar)")
    c.execute("insert into station values ('AAA', 'A')")
    c.execute("create table station_tiploc (crs varchar, tiploc varchar)")
    c.execute("insert into station_tiploc values ('AAA', 'AAATIP')")
    c.execute("create table train_schedule "
              "(schedule_id bigint, train_status varchar, atoc_code varchar, "
              " source varchar, train_category varchar)")
    c.execute("insert into train_schedule values "
              "(1, 'P', 'GW', 'cif', 'OO'), (2, 'B', 'GW', 'cif', 'BS')")
    c.execute("create table schedule_stop (schedule_id bigint, location varchar, crs varchar)")
    c.execute("insert into schedule_stop values "
              "(1, 'AAATIP', 'AAA'), (2, 'AAATIP', 'AAA')")

    classify_locations(c)

    assert sorted(c.execute(
        "select mode, atoc_code, calls from station_service").fetchall()) == [
        ("bus", "GW", 1), ("train", "GW", 1)]


def test_an_operator_marker_is_not_a_place():
    """**MSN carries locations that are not places.** Twelve of them are named
    for an operator and a direction - `CH ORIGIN`, `EMR DESTINATION`, `SWR
    ORIGIN`, `TRANSPENNINE DESTINATION` - and they are how a rail-replacement
    working names an endpoint it does not have. Every one was classified `rail`
    on two to six calls, and counted among the stations "too new for the
    RSPS5052 list", which was a claim nothing checked.

    The test is structural, not by name: `ZTR` is the file for the services CIF
    cannot express, and an unspecified category there is exactly that kind of
    working."""
    assert kinds_world(calls=[
        ("QXO", "P", "XC", "ztr", "XX"),
        ("AAA", "P", "XC", "cif", "OO"),
    ]) == {"QXO": "marker", "AAA": "rail"}


def test_a_real_station_a_replacement_bus_also_reaches_is_still_a_station():
    """The rule is "every call is an unspecified ZTR working", not "any". A
    station a rail-replacement service calls at as well as a train is a station,
    and the marker locations are the ones with nothing else at all.

    Stratford International is why this matters and why the obvious weaker rule
    was rejected: it is served *only* by unspecified workings, 4,100 stops of
    them, as are Elgin, Forres, Nairn and fifteen more real stations. Category
    alone would take out all of them."""
    assert kinds_world(calls=[
        ("BBB", "P", "GW", "ztr", "XX"),
        ("BBB", "P", "GW", "cif", "XX"),
    ]) == {"BBB": "rail"}
    # And an unspecified CIF working on its own is not a marker either: the
    # source is half the test.
    assert kinds_world(calls=[("CCC", "P", "GW", "cif", "XX")]) == {"CCC": "rail"}


def test_a_marker_keeps_its_evidence():
    """`station_service` is the evidence behind `kind`, so the rows stay: a
    reader has to be able to see *why* a location was set aside, and deleting
    them would leave `marker` an assertion with nothing behind it."""
    c = duckdb.connect()
    c.execute("create table station (crs varchar, name varchar)")
    c.execute("insert into station values ('QXO', 'XC ORIGIN')")
    c.execute("create table station_tiploc (crs varchar, tiploc varchar)")
    c.execute("insert into station_tiploc values ('QXO', 'QXOTIP')")
    c.execute("create table train_schedule "
              "(schedule_id bigint, train_status varchar, atoc_code varchar, "
              " source varchar, train_category varchar)")
    c.execute("insert into train_schedule values (1, 'P', 'XC', 'ztr', 'XX')")
    c.execute("create table schedule_stop (schedule_id bigint, location varchar, crs varchar)")
    c.execute("insert into schedule_stop values (1, 'QXOTIP', 'QXO')")
    classify_locations(c)
    assert c.execute("select kind from station").fetchone() == ("marker",)
    assert c.execute(
        "select mode, calls from station_service where crs = 'QXO'"
    ).fetchone() == ("train", 1)


ALIAS_SCHEMA = pa.schema(
    [("station_name", pa.string()), ("station_alias", pa.string())]
)


@pytest.fixture
def named(tmp_path):
    """A station with two MSN records, one of them subsidiary.

    Paddington's real shape: `PADTLL` sorts before `PADTON`, so the TIPLOC
    tie-break alone hands the station the Elizabeth line box's name.
    """
    timetable = tmp_path / "timetable"
    fares = tmp_path / "fares"
    timetable.mkdir()
    fares.mkdir()

    write(
        timetable / "physical_station.parquet",
        [
            {"station_name": "PADDINGTON EL", "tiploc_code": "PADTLL",
             "crs_code": "PAD", "cate_interchange_status": "9",
             "easting": 15295, "northing": 61827, "minimum_change_time": 15},
            {"station_name": "LONDON PADDINGTON", "tiploc_code": "PADTON",
             "crs_code": "PAD", "cate_interchange_status": "3",
             "easting": 15295, "northing": 61827, "minimum_change_time": 15},
            {"station_name": "SWANSEA", "tiploc_code": "SWANSEA",
             "crs_code": "SWA", "cate_interchange_status": "1",
             "easting": 12657, "northing": 61932, "minimum_change_time": 5},
        ],
        MSN_SCHEMA,
    )
    write(timetable / "tiploc.parquet", [], TIPLOC_SCHEMA)
    write(
        timetable / "station_alias.parquet",
        [
            # Named by the record the ranking does *not* keep. Joining on
            # `station.name` would lose this one entirely.
            {"station_name": "LONDON PADDINGTON", "station_alias": "PADDINGTON"},
            {"station_name": "SWANSEA", "station_alias": "ABERTAWE"},
            # An alias the station already answers to under another TIPLOC.
            {"station_name": "LONDON PADDINGTON", "station_alias": "PADDINGTON EL"},
            {"station_name": "SWANSEA", "station_alias": "   "},
        ],
        ALIAS_SCHEMA,
    )
    write(fares / "location.parquet", [], LOCATION_SCHEMA)
    write(fares / "station_cluster.parquet", [], CLUSTER_SCHEMA)

    connection = duckdb.connect()
    counts = build_reference(connection, timetable, fares)
    return connection, counts


def test_a_subsidiary_msn_record_does_not_name_the_station(named):
    """MSN's own `9` is what separates a platform-level record from the station.

    Without it the TIPLOC tie-break chose alphabetically and PAD came out
    `PADDINGTON EL`, which no passenger would type and no ticket carries.
    """
    connection, _ = named
    assert connection.execute(
        "select name from station where crs = 'PAD'"
    ).fetchone() == ("LONDON PADDINGTON",)


def test_an_alias_is_resolved_against_every_name_the_station_carries(named):
    """The alias file names a station by whichever of its MSN records it likes.

    It calls PAD `LONDON PADDINGTON`; before the fix above that was the record
    `station` discarded, so a join on the kept name lost the alias. Joining on
    the whole MSN set is what makes the two files independent of each other.
    """
    connection, counts = named
    assert connection.execute(
        "select crs, alias from station_alias order by crs, alias"
    ).fetchall() == [("PAD", "PADDINGTON"), ("SWA", "ABERTAWE")]
    assert counts.aliases == 2


def test_an_alias_repeating_a_name_the_station_has_is_dropped(named):
    """`PADDINGTON EL` is a name PAD already carries on its other TIPLOC.

    Keeping it would offer the same station twice in a search, the second time
    under the very name this change exists to stop showing.
    """
    connection, _ = named
    assert "PADDINGTON EL" not in [
        row[0] for row in connection.execute("select alias from station_alias").fetchall()
    ]


def test_station_alias_exists_even_when_msn_carried_no_l_records(tmp_path):
    """A consumer's SQL should not have to ask whether the table is there."""
    timetable = tmp_path / "timetable"
    fares = tmp_path / "fares"
    timetable.mkdir()
    fares.mkdir()
    write(timetable / "physical_station.parquet", [], MSN_SCHEMA)
    write(timetable / "tiploc.parquet", [], TIPLOC_SCHEMA)
    write(fares / "location.parquet", [], LOCATION_SCHEMA)
    write(fares / "station_cluster.parquet", [], CLUSTER_SCHEMA)

    connection = duckdb.connect()
    counts = build_reference(connection, timetable, fares)
    assert counts.aliases == 0
    assert connection.execute("select count(*) from station_alias").fetchone() == (0,)
