"""The standing data-quality report.

A check that never fails is decoration, so each test here breaks one thing and
asserts the report notices. The fixture builds the smallest database the checks
can run against - which makes it a smoke test for the model layer's shape too.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rail.model import run_checks

TODAY = dt.date.today()
FOREVER = dt.date(2999, 12, 31)
PAST = dt.date(2000, 1, 1)

#: An ordinary walk-up fare, an Advance, and a restriction that bars nothing.
DEFAULT_FARES = (
    {"ticket_code": "SDS", "fare": 1510, "restriction_code": None},
    {"ticket_code": "NAA", "fare": 2200, "restriction_code": "AD"},
)

#: `AD` is how most of the feed words a booked-train restriction; `XX` is an
#: ordinary time restriction, there so the check has something to *not* match.
DEFAULT_RESTRICTIONS = (
    {"cf_mkr": "C", "restriction_code": "AD", "description": "TFW ADVANCE",
     "desc_out": "VALID ON DATE&TRAIN SHOWN ONLY.LMTD CHNGE.NO RFND."},
    {"cf_mkr": "C", "restriction_code": "XX", "description": "OFF-PEAK",
     "desc_out": "NOT VALID BEFORE 0930 MON-FRI"},
)

#: Real NaPTAN rows, copied verbatim: that source publishes an easting and
#: northing *and* a latitude and longitude for each stop, and the conversion
#: check compares the two. Values taken from the feed rather than computed,
#: because computing them with the transform under test would prove nothing.
DEFAULT_NAPTAN = (
    ("YORK", 459600, 451700, 53.95796588375, -1.09318208959),
    ("KNGX", 530300, 183000, 51.53088333892, -0.12292591146),
    ("PENZNCE", 147588, 30599, 50.12167398129, -5.53256527555),
)


def write(directory, name, rows, schema):
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), directory / f"{name}.parquet")


@pytest.fixture
def world(tmp_path):
    """A tiny but internally consistent database, plus knobs to break it."""

    def _build(*, stop_locations=("EUSTON",), tiplocs=("EUSTON",),
               flow_codes=(("1111", "2222"),), stop_times=None,
               naptan_stops=None, fare_records=None, restrictions=None):
        timetable = tmp_path / "tt"
        fares = tmp_path / "fa"
        timetable.mkdir(exist_ok=True)
        fares.mkdir(exist_ok=True)

        write(timetable, "stop_time", [{"location": loc} for loc in stop_locations],
              pa.schema([("location", pa.string())]))
        # crs_code and nalco carry the timetable's own view of a location, which
        # the crosswalk check tests the fares NLC against.
        write(timetable, "tiploc",
              [{"tiploc_code": t, "crs_code": "AAA", "nalco": "111100"}
               for t in tiplocs],
              pa.schema([("tiploc_code", pa.string()), ("crs_code", pa.string()),
                         ("nalco", pa.string())]))
        write(fares, "flow",
              [{"origin_code": o, "destination_code": d, "end_date": FOREVER}
               for o, d in flow_codes],
              pa.schema([("origin_code", pa.string()), ("destination_code", pa.string()),
                         ("end_date", pa.date32())]))
        # `crs` and `description` are what the PlusBus-zone check reads: a zone
        # names itself "BATH+BUS", and one that has gained a CRS must not be
        # priceable as a destination.
        write(fares, "location",
              [{"nlc": "1111", "fare_group": "1111", "crs": "AAA",
                "description": "ANYTOWN", "start_date": PAST, "end_date": FOREVER},
               {"nlc": "2222", "fare_group": "2222", "crs": "BBB",
                "description": "SOMEWHERE", "start_date": PAST, "end_date": FOREVER}],
              pa.schema([("nlc", pa.string()), ("fare_group", pa.string()),
                         ("crs", pa.string()), ("description", pa.string()),
                         ("start_date", pa.date32()), ("end_date", pa.date32())]))
        write(fares, "station_cluster", [{"cluster_id": "C001"}],
              pa.schema([("cluster_id", pa.string())]))
        write(fares, "fare",
              list(fare_records or DEFAULT_FARES),
              pa.schema([("ticket_code", pa.string()), ("fare", pa.int64()),
                         ("restriction_code", pa.string())]))
        # The booked-train check reads the restriction's own words: a fare valid
        # only on the train you booked is an Advance product whatever its
        # validity record says. `AD` is the phrasing most of the feed uses.
        write(fares, "restriction_header",
              list(restrictions or DEFAULT_RESTRICTIONS),
              pa.schema([("cf_mkr", pa.string()), ("restriction_code", pa.string()),
                         ("description", pa.string()), ("desc_out", pa.string())]))

        c = duckdb.connect()
        c.execute("create table fare_alias as select * from (values ('AAA','1111'),('BBB','2222')) t(crs, code)")
        # `reservation_required` and `package_mkr` are the feed's own structural
        # flags; `N` on both is an ordinary fare. The Advance row carries `B`,
        # which is what the real `AO2 AIRPORT ADV STD` looks like.
        # `is_real_advance` is the narrow class - an Advance somebody can buy -
        # where `is_advance_fare` is the residual "sellable and not a walk-up".
        # The rows here agree, which is what an undisturbed feed looks like.
        c.execute("""create table ticket_type_current as select * from (values
            ('SDS', 'ANYTIME DAY S',  2, true,  false, false, true,  'N', 'N'),
            ('CDR', 'OFF-PEAK DAY R', 2, true,  false, false, true,  'N', 'N'),
            ('SVR', 'OFF-PEAK R',     2, true,  false, false, true,  'N', 'N'),
            ('NAA', 'ADVANCE',        2, false, true,  true,  true,  'B', 'N')
        ) t(ticket_code, description, tkt_class, is_walk_up, is_advance_fare,
            is_real_advance, is_sellable, reservation_required, package_mkr)""")
        c.execute("create table station_tiploc as select * from (values ('AAA','EUSTON')) t(crs, tiploc)")
        # is_rail_station is null unless `rail fetch --supplementary` has run.
        c.execute("""create table station as select * from
            (values ('AAA', 100, null::boolean, 'tiploc', 'rail'))
            t(crs, easting, is_rail_station, grid_source, kind)""")
        c.execute("create table station_grid_conflict (crs varchar)")
        # RGY: the routeing feed's own CRS/NLC cross-reference.
        c.execute("""create table routeing_location as select * from (values
            ('1111', '1111', 'AAA', DATE '2000-01-01', DATE '2999-12-31')
        ) t(nlc, fare_group, crs, start_date, end_date)""")
        # RGX: stations built since NFM64, and the older station whose fares
        # stand in for them.
        c.execute("create table routeing_new_station (crs varchar, "
                  "equivalent_crs varchar)")
        c.execute("""create table station_nlc as select * from
            (values ('AAA', '1111', '1111')) t(crs, nlc, fare_group)""")
        c.execute("""create table station_group_member as select * from
            (values ('AAA', 'G02')) t(crs, group_code)""")
        # A Day Return and an open return, plus the one field that makes a
        # weekend return a weekend return.
        c.execute("""create table ticket_return_kind as select * from (values
            ('SDS', 'S', '01', 'none',     false),
            ('CDR', 'R', '06', 'same_day', false),
            ('SVR', 'R', '13', 'period',   false)
        ) t(ticket_code, tkt_type, validity_code, return_kind, is_weekend_return)""")
        c.execute("""create table ticket_validity_current as select * from (values
            ('06', 'ON DATE SHOWN',  1, 0, null),
            ('13', '1DYOUT 1MTHRTN', 0, 1, null),
            ('59', 'WKND 3 Days',    3, 0, 'SU')
        ) t(validity_code, description, ret_days, ret_months, ret_after_day)""")
        # 4.19.3 field 10. A handful bar a change of trains; most allow one.
        c.execute("""create table restriction_current as
            select 'C' as cf_mkr, code as restriction_code, code as description,
                   allowed as change_allowed
            from (values ('ME', false), ('GZ', true), ('0W', true), ('1C', true),
                         ('B1', true), ('B3', true), ('I1', true), ('TF', true),
                         ('R1', true), ('R6', true), ('RN', true)) t(code, allowed)""")
        # FRR rule 01: 5p across every band, selected by measurement.
        c.execute("""create table rounding_band as select * from
            (values (1, 1), (99999997, 5), (99999999, 1)) t(upper_limit, round_to)""")
        c.execute("""create table train_schedule as select * from
            (values (1, 'P', true, 'cif'), (100000001, 'P', true, 'ztr'))
            t(schedule_id, stp_indicator, is_passenger, source)""")
        # The Solent hovercraft, which is what the ZTR checks are watching.
        c.execute("""create table station_service as select * from
            (values ('XRD', 'ferry', 'QH', 222), ('SHV', 'ferry', 'QH', 111))
            t(crs, mode, atoc_code, calls)""")
        c.execute("create table reference_reject as select * from (values ('x','y','a reason')) t(source, key, reason)")
        c.execute("create table fare_reject as select * from (values ('ZZZ','desc','a reason')) t(ticket_code, description, reason)")
        c.execute("create table railcard_discount as select * from (values ('YNG', 334)) t(railcard_code, discount_percentage)")

        # A fortnight of service, busier on weekdays than at the weekend.
        c.execute("""
            create table service_date as
            select 1 as schedule_id, d::date as date
            from generate_series(date '2026-08-03', date '2026-08-16', interval 1 day) g(d),
                 generate_series(1, 100) n(i)
            where dayofweek(d::date) not in (0, 6)
            union all
            select 1, d::date
            from generate_series(date '2026-08-03', date '2026-08-16', interval 1 day) g(d),
                 generate_series(1, 40) n(i)
            where dayofweek(d::date) = 6
            union all
            select 1, d::date
            from generate_series(date '2026-08-03', date '2026-08-16', interval 1 day) g(d),
                 generate_series(1, 20) n(i)
            where dayofweek(d::date) = 0
        """)

        default_stops = [(1, 1, 600, True), (1, 2, 700, True)]
        c.execute("create table schedule_stop (schedule_id bigint, seq bigint, "
                  "arrival_minutes bigint, departure_minutes bigint, "
                  "is_public boolean, crs varchar)")
        for schedule_id, seq, minutes, public in (stop_times or default_stops):
            c.execute("insert into schedule_stop values (?, ?, ?, ?, ?, ?)",
                      [schedule_id, seq, minutes, minutes, public, 'AAA'])
        # One resolved ZTR stop, so "every public ZTR stop resolves" has
        # something to be true about rather than being vacuous.
        c.execute("insert into schedule_stop values (100000001, 1, 600, 600, true, 'XRD')")

        # Three real NaPTAN rows, each carrying that source's own grid reference
        # *and* its own latitude and longitude. The conversion check measures one
        # against the other, so real values keep it honest - deriving the lat/lon
        # with the transform under test would make it pass however broken it was.
        naptan = tmp_path / "naptan"
        naptan.mkdir(exist_ok=True)
        write(naptan, "naptan_rail",
              [{"tiploc": t, "easting": e, "northing": n,
                "latitude": la, "longitude": lo}
               for t, e, n, la, lo in (naptan_stops or DEFAULT_NAPTAN)],
              pa.schema([("tiploc", pa.string()), ("easting", pa.int64()),
                         ("northing", pa.int64()), ("latitude", pa.float64()),
                         ("longitude", pa.float64())]))

        return c, timetable, fares, naptan

    return _build


def status_of(checks, fragment):
    return next(c.status for c in checks if fragment in c.name)


def test_a_consistent_world_passes_every_integrity_check(world):
    checks = run_checks(*world())
    integrity = [c for c in checks if c.category == "integrity"]

    assert integrity and all(c.status == "ok" for c in integrity)


def test_a_stop_at_an_unknown_tiploc_fails(world):
    checks = run_checks(*world(stop_locations=("EUSTON", "NOWHERE")))

    assert status_of(checks, "stop locations resolve") == "fail"


def test_a_flow_to_an_undefined_code_fails(world):
    checks = run_checks(*world(flow_codes=(("1111", "9999"),)))

    assert status_of(checks, "flow endpoints resolve") == "fail"


def test_the_two_feeds_agree_on_a_station_location_number(world):
    """An independent test of the crosswalk, which is joined on CRS alone.

    A full NLC is six digits and booking offices quote the first four, so the
    fares feed's 1111 and the timetable's 111100 are the same place - derived
    from different files by different systems. Getting the crosswalk wrong
    corrupts everything downstream in silence, and nothing else here would
    notice.
    """
    checks = run_checks(*world())

    assert status_of(checks, "fares NLC matches") == "ok"


def test_a_disagreement_over_the_location_number_is_caught(world):
    connection, timetable, fares, naptan = world()
    connection.execute("update station_nlc set nlc = '9999'")

    checks = run_checks(connection, timetable, fares, naptan)

    # Warn, not fail: without the RSPS5052 station list the check cannot tell a
    # bus stop from a station, and the fares feed numbers those differently.
    assert status_of(checks, "fares NLC matches") == "warn"


def test_the_station_list_tightens_the_crosswalk_check(world):
    connection, timetable, fares, naptan = world()
    connection.execute("update station set is_rail_station = true")
    connection.execute("update station_nlc set nlc = '9999'")

    checks = run_checks(connection, timetable, fares, naptan)

    assert status_of(checks, "fares NLC matches") == "fail"


def test_a_journey_that_runs_backwards_fails(world):
    """The check that would have caught the overnight-wrap bug."""
    checks = run_checks(*world(stop_times=[(1, 1, 1400, True), (1, 2, 30, True)]))

    assert status_of(checks, "journeys move forwards") == "fail"


def test_an_unwrapped_overnight_journey_passes(world):
    checks = run_checks(*world(stop_times=[(1, 1, 1400, True), (1, 2, 1470, True)]))

    assert status_of(checks, "journeys move forwards") == "ok"


def test_the_weekday_and_sunday_relationship_is_asserted(world):
    checks = run_checks(*world())

    assert status_of(checks, "Sunday is quieter") == "ok"
    assert status_of(checks, "Saturday sits between") == "ok"


def test_exclusions_are_reported_with_their_reasons(world):
    checks = run_checks(*world())
    excluded = [c for c in checks if c.category == "excluded"]

    assert any("a reason" in c.name for c in excluded)


def test_the_report_covers_every_category(world):
    checks = run_checks(*world())

    assert {c.category for c in checks} == {
        "integrity", "timetable", "fares", "excluded", "suspicious"
    }


def test_the_return_shape_checks_report_what_the_feed_carries(world):
    """A Day Return that stopped being same-day, or a weekend return that lost
    its weekday rule, would quietly widen what the tool says a ticket permits.
    Both are single fields in TVL, so both are asserted rather than assumed."""
    checks = run_checks(*world())

    assert status_of(checks, "day returns still come back") == "ok"
    assert status_of(checks, "weekend-return day rule") == "ok"


def test_losing_the_weekend_day_rule_is_caught(world):
    connection, timetable, fares, naptan = world()
    connection.execute("update ticket_validity_current set ret_after_day = null")

    checks = run_checks(connection, timetable, fares, naptan)

    assert status_of(checks, "weekend-return day rule") == "warn"


def test_a_day_return_that_stops_being_same_day_is_caught(world):
    connection, timetable, fares, naptan = world()
    connection.execute(
        "update ticket_return_kind set return_kind = 'multi_day' "
        "where return_kind = 'same_day'")

    checks = run_checks(connection, timetable, fares, naptan)

    assert status_of(checks, "day returns still come back") == "fail"


def test_the_change_indicator_polarity_is_watched(world):
    """Read backwards, CHANGE_IND would withdraw fares on 803 restrictions
    instead of 36, and every journey with a connection would price as an
    Anytime. A handful barring a change is the shape the feed has always had."""
    checks = run_checks(*world())

    assert status_of(checks, "bar a change of trains") == "ok"


def test_an_inverted_change_indicator_is_caught(world):
    connection, timetable, fares, naptan = world()
    connection.execute("update restriction_current set change_allowed = not change_allowed")

    checks = run_checks(connection, timetable, fares, naptan)

    assert status_of(checks, "bar a change of trains") == "warn"


def test_uncorroborated_positions_are_watched(world):
    """Positions come from up to three sources and are taken only when a second
    agrees. A rise here means a source went missing - the FOI grid file is a
    frozen snapshot that `rail refresh` drops."""
    checks = run_checks(*world())

    assert status_of(checks, "corroborated by a second source") == "ok"


def test_losing_the_corroborating_sources_is_caught(world):
    connection, timetable, fares, naptan = world()
    connection.execute(
        "update station set grid_source = 'msn (uncorroborated)'")

    checks = run_checks(connection, timetable, fares, naptan)

    assert status_of(checks, "corroborated by a second source") == "warn"


def test_widespread_disagreement_over_positions_is_caught(world):
    connection, timetable, fares, naptan = world()
    connection.execute(
        "insert into station_grid_conflict select 'X' || i from range(20) t(i)")

    checks = run_checks(connection, timetable, fares, naptan)

    assert status_of(checks, "agree on where each station is") == "warn"


def test_a_walk_up_fare_tied_to_a_booked_train_fails(world):
    """The restriction says what the validity does not.

    LNER's Simpler Fares `70min Flex` carries validity `61` "ON DATE SHOWN" and
    restriction `FL`, "LNER FLEX ON SET TIME"; Greater Anglia's Seatfrog Secret
    Fare carries `OA`, "LER ADVANCE". Both were classified walk-up, and from
    King's Cross a `70min Flex` single made two singles look £100 cheaper than
    the return. Operators keep inventing these, so the report asserts the
    outcome rather than the rule.
    """
    checks = run_checks(*world(fare_records=[
        # Every fare of SDS is now tied to a booked train, which makes it an
        # Advance product wearing a walk-up label.
        {"ticket_code": "SDS", "fare": 1510, "restriction_code": "AD"},
        {"ticket_code": "SDS", "fare": 2010, "restriction_code": "AD"},
    ]))

    assert status_of(checks, "tied to a booked train") == "fail"


def test_one_booked_train_flow_among_many_is_not_enough(world):
    """*Every* fare, not any: a type with a mix is an ordinary fare that happens
    to have an Advance variant on one flow, and withdrawing it would be too
    strict."""
    checks = run_checks(*world(fare_records=[
        {"ticket_code": "SDS", "fare": 1510, "restriction_code": "AD"},
        {"ticket_code": "SDS", "fare": 2010, "restriction_code": "XX"},
    ]))

    assert status_of(checks, "tied to a booked train") == "ok"


def test_a_broken_grid_conversion_fails(world, monkeypatch):
    """The map draws latitude and longitude; everything else stores OS grid
    metres. The failure worth catching is the datum shift going missing, because
    Airy 1830 sits about 100 m from WGS84 over Britain - so the coordinates stay
    entirely plausible and every station moves a street.

    Measured on the real feed the median is 0.19 m with the shift and 113 m
    without, which is why the band is a metre.
    """
    from rail.model import geo

    connection, timetable, fares, naptan = world()
    monkeypatch.setattr(geo, "_helmert", lambda x, y, z: (x, y, z))

    checks = run_checks(connection, timetable, fares, naptan)

    assert status_of(checks, "convert to latitude and longitude") == "fail"


def test_no_naptan_leaves_the_conversion_unchecked(world):
    """`rail refresh` rebuilds without NaPTAN, so this is a real recurring
    state. The transform itself is unaffected - it is pure arithmetic pinned by
    its own tests - but it can no longer be checked against anything, and saying
    so is the point of `grid_source` and of this report."""
    connection, timetable, fares, _ = world()

    checks = run_checks(connection, timetable, fares, naptan_dir=None)

    assert status_of(checks, "convert to latitude and longitude") == "warn"


def test_the_two_answers_to_what_a_station_is_are_compared(world):
    """RSPS5052's list and the timetable are derived from different files by
    different means, so agreement is worth asserting."""
    connection, timetable, fares, naptan = world()
    connection.execute("update station set is_rail_station = true")

    checks = run_checks(connection, timetable, fares, naptan)

    assert status_of(checks, "agrees a rail station is a rail station") == "ok"


def test_a_rail_station_the_timetable_calls_a_bus_stop_fails(world):
    """Disagreement in one direction is expected - a new station reaches the
    timetable before the supplementary list. This is the other direction, which
    would mean the classification or the crosswalk has drifted."""
    connection, timetable, fares, naptan = world()
    connection.execute(
        "update station set is_rail_station = true, kind = 'bus'")

    checks = run_checks(connection, timetable, fares, naptan)

    assert status_of(checks, "agrees a rail station is a rail station") == "fail"


def test_the_routeing_feed_is_a_third_opinion_on_the_crosswalk(world):
    """The fares NLC is already checked against the timetable's NALCO. RGY
    states CRS against NLC directly, from a third file by a third process."""
    checks = run_checks(*world())

    assert status_of(checks, "routeing feed agrees on CRS to NLC") == "ok"


def test_a_routeing_feed_disagreement_over_the_nlc_fails(world):
    connection, timetable, fares, naptan = world()
    connection.execute("update routeing_location set nlc = '9999'")

    checks = run_checks(connection, timetable, fares, naptan)

    assert status_of(checks, "routeing feed agrees on CRS to NLC") == "fail"


def test_a_routeing_feed_disagreement_over_the_fare_group_fails(world):
    """Both dimensions are checked: the group is what decides whether a ticket
    to "Birmingham Stations" is valid at a given station."""
    connection, timetable, fares, naptan = world()
    connection.execute("update routeing_location set fare_group = '9999'")

    checks = run_checks(connection, timetable, fares, naptan)

    assert status_of(checks, "routeing feed agrees on CRS to NLC") == "fail"
