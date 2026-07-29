"""Walk-up fare derivation.

Two things are being pinned down. First the lookup itself: a fare hangs off a
*flow* between two codes, and a station is represented by its own NLC, its
group's NLC, and every cluster it belongs to. Second the filtering, because the
feed ships a great deal that is not an adult walk-up fare — Advance products
priced in the reservation system, flat-rate child and promotional tickets,
family products covering three people, complimentary staff tickets, and records
described "FOR TEST USE ONLY".
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rail.model import build_fares_reference, cheapest_from, fare_options
from rail.model.fares import fares_between
from rail.model.railcards import build_railcards
from rail.model.restrictions import build_restrictions
from rail.model.returns import build_ticket_validity

TODAY = dt.date.today()
FOREVER = dt.date(2999, 12, 31)
PAST = dt.date(2000, 1, 1)
TRAVEL = TODAY + dt.timedelta(days=7)

FLOW_SCHEMA = pa.schema([
    ("flow_id", pa.int64()), ("origin_code", pa.string()),
    ("destination_code", pa.string()), ("route_code", pa.string()),
    ("direction", pa.string()),
    # 0 and 2 take the standard discount; 1 and 3 send the discount through FNS.
    ("ns_disc_ind", pa.int64()),
    ("start_date", pa.date32()), ("end_date", pa.date32()),
])
FARE_SCHEMA = pa.schema([
    ("flow_id", pa.int64()), ("ticket_code", pa.string()), ("fare", pa.int64()),
    ("restriction_code", pa.string()),
])
NFO_SCHEMA = pa.schema([
    ("origin_code", pa.string()), ("destination_code", pa.string()),
    ("route_code", pa.string()), ("railcard_code", pa.string()),
    ("ticket_code", pa.string()), ("adult_fare", pa.int64()),
    ("restriction_code", pa.string()),
    ("suppress_mkr", pa.bool_()), ("composite_indicator", pa.string()),
    ("start_date", pa.date32()), ("end_date", pa.date32()),
])
TTY_SCHEMA = pa.schema([
    ("ticket_code", pa.string()), ("description", pa.string()),
    ("tkt_type", pa.string()), ("tkt_class", pa.int64()), ("tkt_group", pa.string()),
    ("validity_code", pa.string()), ("max_passengers", pa.int64()),
    ("min_passengers", pa.int64()), ("restricted_by_date", pa.bool_()),
    ("restricted_by_train", pa.bool_()), ("discount_category", pa.int64()),
    # RSPS5045 4.6.2 fields 23 and 29. `N` on both is the ordinary fare, which
    # is what every ticket here is unless it says otherwise.
    ("reservation_required", pa.string()), ("package_mkr", pa.string()),
    ("start_date", pa.date32()), ("end_date", pa.date32()),
])
TAP_SCHEMA = pa.schema([
    ("ticket_code", pa.string()), ("start_date", pa.date32()), ("end_date", pa.date32()),
])


def ticket(code, description, *, kind="S", cls=2, group="S", passengers=1,
           category=1, validity="01", reservation="N", package="N"):
    return {"ticket_code": code, "description": description, "tkt_type": kind,
            "tkt_class": cls, "tkt_group": group, "validity_code": validity,
            "max_passengers": passengers, "min_passengers": 1,
            "restricted_by_date": False, "restricted_by_train": False,
            "discount_category": category,
            "reservation_required": reservation, "package_mkr": package,
            "start_date": PAST, "end_date": FOREVER}


def flow(flow_id, origin, destination, *, direction="S", start=PAST, end=FOREVER,
         ns_disc=0):
    return {"flow_id": flow_id, "origin_code": origin, "destination_code": destination,
            "route_code": "00000", "direction": direction, "ns_disc_ind": ns_disc,
            "start_date": start, "end_date": end}


def fare(flow_id, code, pence, restriction=None):
    return {"flow_id": flow_id, "ticket_code": code, "fare": pence,
            "restriction_code": restriction}


@pytest.fixture
def fares(tmp_path):
    """Build a tiny fares world and return a query helper."""
    directory = tmp_path / "fares"
    directory.mkdir()

    def _build(*, flows, fare_records, tickets, nfo=(), advance=(),
               stations=None, clusters=None, bands=(), railcards=(), minimums=(),
               route_locations=(), flexi=(), rgk_rules=(), london_marker=None,
               london_terminals=(), fns=(), toc_rules=(), validities=(),
               headers=(),
               rounding=((99999997, 5), (99999999, 1)), geography=(),
               railcard_rules=(), counties=None):
        pq.write_table(pa.Table.from_pylist(list(flows), schema=FLOW_SCHEMA),
                       directory / "flow.parquet")
        pq.write_table(pa.Table.from_pylist(list(fare_records), schema=FARE_SCHEMA),
                       directory / "fare.parquet")
        pq.write_table(pa.Table.from_pylist(list(nfo), schema=NFO_SCHEMA),
                       directory / "non_derivable_fare_override.parquet")
        pq.write_table(pa.Table.from_pylist(list(tickets), schema=TTY_SCHEMA),
                       directory / "ticket_type.parquet")
        pq.write_table(
            pa.Table.from_pylist(
                [{"ticket_code": c, "start_date": PAST, "end_date": FOREVER}
                 for c in advance],
                schema=TAP_SCHEMA),
            directory / "advance_ticket.parquet")

        # `LOC` carries the county code, which is a legitimate flow endpoint —
        # RSPS5045 4.1.2 says "NLC code, county code, zone code" — and the only
        # way the Isle of Man's fare bands can be reached.
        # A station is (crs, nlc, fare_group), or (crs, nlc, fare_group,
        # description) where the description matters — which is PlusBus, whose
        # zones name themselves "BATH+BUS" and must never become destinations.
        rows = stations or [("AAA", "1111", "1111"), ("BBB", "2222", "2222")]
        rows = [r if len(r) == 4 else (*r, r[0]) for r in rows]
        counted = list(zip(rows, counties)) if counties else [(r, None) for r in rows]
        pq.write_table(
            pa.Table.from_pylist(
                [{"crs": crs, "nlc": nlc, "county": county, "description": desc,
                  "start_date": PAST, "end_date": FOREVER}
                 for (crs, nlc, _group, desc), county in counted],
                schema=pa.schema([("crs", pa.string()), ("nlc", pa.string()),
                                  ("county", pa.string()),
                                  ("description", pa.string()),
                                  ("start_date", pa.date32()),
                                  ("end_date", pa.date32())])),
            directory / "location.parquet")

        connection = duckdb.connect()
        connection.execute("create table station_nlc (crs varchar, nlc varchar, uic varchar, fare_group varchar)")
        # `build_fares_reference` reads this, and the real one already excludes
        # PlusBus zones — see `reference.py`. Mirror that here so a zone reaches
        # `fare_alias` only if the county arm lets it through, which is the
        # path the fix has to close.
        for crs, nlc, group, desc in rows:
            if desc.endswith("+BUS"):
                continue
            connection.execute("insert into station_nlc values (?, ?, ?, ?)", [crs, nlc, nlc, group])
        connection.execute("create table station_cluster (cluster_id varchar, cluster_nlc varchar)")
        for cluster_id, nlc in clusters or []:
            connection.execute("insert into station_cluster values (?, ?)", [cluster_id, nlc])

        pq.write_table(
            pa.Table.from_pylist(
                [{"route_code": r, "crs_code": crs, "nlc_code": None,
                  "incl_excl": sense} for r, crs, sense in route_locations],
                schema=pa.schema([("route_code", pa.string()), ("crs_code", pa.string()),
                                  ("nlc_code", pa.string()), ("incl_excl", pa.string())])),
            directory / "route_location.parquet")
        _write_descriptions(directory, validities, headers)
        _write_routeing(connection, rgk_rules, london_marker, london_terminals,
                        toc_rules)
        _write_restrictions(directory, bands)
        _write_railcards(directory, railcards, minimums, fns, rounding,
                         geography, stations or [('AAA','1111','1111'),
                                                 ('BBB','2222','2222')],
                         railcard_rules)
        build_fares_reference(connection, directory,
                              _write_flexi(directory, flexi))
        build_restrictions(connection, directory)
        build_ticket_validity(connection, directory)
        build_railcards(connection, directory)
        return connection, directory

    return _build


def prices(connection, directory, origin="AAA"):
    return {row[0]: row[3] for row in cheapest_from(connection, directory, origin, TRAVEL)}


# --- the lookup --------------------------------------------------------------


def test_a_direct_flow_between_two_station_nlcs(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
    )
    assert prices(connection, directory) == {"BBB": 1510}


def test_a_flow_from_a_cluster_covers_its_member_stations(fares):
    """Clusters are how the feed avoids storing every station pair."""
    connection, directory = fares(
        flows=[flow(1, "C001", "2222")],
        fare_records=[fare(1, "SDS", 900)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        clusters=[("C001", "1111")],  # AAA belongs to cluster C001
    )
    assert prices(connection, directory) == {"BBB": 900}


def test_a_flow_can_be_priced_by_county_band(fares):
    """**A flow endpoint is not always an NLC.** RSPS5045 4.1.2 allows "4 digit
    NLC code, county code, zone code", and the county form is really used: the
    Isle of Man Steam Packet sets five fare bands by county, £97.30 to £187.20
    across 48 of them, because the rail leg can start anywhere.

    The chain is three deep — station → its county code → the cluster holding
    that county → the flow — and expanding only NLCs missed it entirely. Douglas
    had no fare from anywhere, and the honest-looking answer "the Steam Packet
    is not a National Rail through-fare" was simply wrong.
    """
    connection, directory = fares(
        # The flow names the *band*, not the station.
        flows=[flow(1, "Q797", "2222")],
        fare_records=[fare(1, "SDS", 14540)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        stations=[("AAA", "1111", "1111"), ("BBB", "2222", "2222")],
        counties=["01", "44"],
        clusters=[("Q797", "CC01")],   # band Q797 covers county 01
    )

    assert prices(connection, directory) == {"BBB": 14540}


def test_a_station_with_no_county_is_not_given_one(fares):
    """`LOC` leaves the field blank for some locations, and `'CC' || ''` would
    be a code that matches whatever cluster happens to hold `CC`."""
    connection, directory = fares(
        flows=[flow(1, "Q797", "2222")],
        fare_records=[fare(1, "SDS", 14540)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        counties=["", None],
        clusters=[("Q797", "CC")],
    )

    assert prices(connection, directory) == {}


def test_a_flow_to_the_group_covers_its_members(fares):
    """Euston sits in group 1072, London Terminals."""
    connection, directory = fares(
        flows=[flow(1, "1111", "9999")],
        fare_records=[fare(1, "SDS", 7070)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        stations=[("AAA", "1111", "1111"), ("EUS", "1444", "9999")],
    )
    assert prices(connection, directory) == {"EUS": 7070}


def test_a_reversible_flow_is_usable_in_both_directions(fares):
    connection, directory = fares(
        flows=[flow(1, "2222", "1111", direction="R")],
        fare_records=[fare(1, "SDS", 1200)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
    )
    assert prices(connection, directory) == {"BBB": 1200}


def test_a_one_way_flow_is_not_usable_backwards(fares):
    connection, directory = fares(
        flows=[flow(1, "2222", "1111", direction="S")],
        fare_records=[fare(1, "SDS", 1200)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
    )
    assert prices(connection, directory) == {}


def test_a_flow_outside_its_validity_dates_is_ignored(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222", start=PAST, end=TODAY - dt.timedelta(days=1))],
        fare_records=[fare(1, "SDS", 1510)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
    )
    assert prices(connection, directory) == {}


def test_the_cheapest_ticket_wins(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "CDS", 990)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("CDS", "OFF-PEAK DAY S")],
    )
    assert prices(connection, directory) == {"BBB": 990}


def test_two_tickets_at_the_same_price_pick_the_same_winner_every_time(fares):
    """A price can be sold by more than one product, and the tie has to break
    the same way twice.

    `_CHEAPEST_SQL` groups by `(dest_crs, fare)`, so `fare` is constant inside a
    group and cannot order anything: `min_by(ticket_code, fare)` was choosing
    arbitrarily among every ticket at that price. With parallel aggregation the
    choice was not even stable between two runs on one database — building the
    same map origin twice produced payloads naming different tickets at
    identical prices, which made a payload rebuild fail a byte-comparison for a
    reason that had nothing to do with the data.

    Only the displayed ticket moved; the price was always right. The ordering
    key is `(ticket_code, route_code)` now, so the alphabetically first ticket
    wins and does so repeatably.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 990), fare(1, "CDS", 990)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("CDS", "OFF-PEAK DAY S")],
    )

    winners = {
        tuple(row[1] for row in cheapest_from(connection, directory, "AAA", TRAVEL))
        for _ in range(8)
    }

    assert winners == {("CDS",)}, f"tie broke inconsistently: {winners}"


# --- non-derivable fares -----------------------------------------------------


def test_a_non_derivable_fare_overrides_the_flow_price(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        nfo=[{"origin_code": "1111", "destination_code": "2222", "route_code": "00000",
              "railcard_code": None, "ticket_code": "SDS", "adult_fare": 1200, "restriction_code": None,
              "composite_indicator": "Y", "suppress_mkr": False, "start_date": PAST, "end_date": FOREVER}],
    )
    assert prices(connection, directory) == {"BBB": 1200}


def test_an_override_wins_even_when_it_is_dearer(fares):
    """Precedence, not price: the NDF is the authoritative fare."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1000)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        nfo=[{"origin_code": "1111", "destination_code": "2222", "route_code": "00000",
              "railcard_code": None, "ticket_code": "SDS", "adult_fare": 1800, "restriction_code": None,
              "composite_indicator": "Y", "suppress_mkr": False, "start_date": PAST, "end_date": FOREVER}],
    )
    assert prices(connection, directory) == {"BBB": 1800}


def test_the_no_fare_sentinel_withdraws_the_flow_price(fares):
    """99999999 is not a £999,999.99 fare.

    RSPS5045 4.4.3 field 12: it means no adult fare is available for the
    ticket. The record still overrides, so the flow price goes with it — but
    the sentinel itself must never surface as a price.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        nfo=[{"origin_code": "1111", "destination_code": "2222", "route_code": "00000",
              "railcard_code": None, "ticket_code": "SDS", "adult_fare": 99999999,
              "restriction_code": None,
              "composite_indicator": "Y", "suppress_mkr": False,
              "start_date": PAST, "end_date": FOREVER}],
    )
    assert prices(connection, directory) == {}


def test_a_composite_record_the_flow_file_already_holds_is_ignored(fares):
    """composite_indicator 'N' means the fare is already in the flow file.

    Reading it the other way round — as "this is an aggregate, drop it" — would
    discard all 249,917 override records in RJFAF833, every one of which is 'Y'.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        nfo=[{"origin_code": "1111", "destination_code": "2222", "route_code": "00000",
              "railcard_code": None, "ticket_code": "SDS", "adult_fare": 9900,
              "restriction_code": None,
              "composite_indicator": "N", "suppress_mkr": False,
              "start_date": PAST, "end_date": FOREVER}],
    )
    assert prices(connection, directory) == {"BBB": 1510}


def test_a_railcard_override_does_not_affect_the_adult_fare(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        nfo=[{"origin_code": "1111", "destination_code": "2222", "route_code": "00000",
              "railcard_code": "YNG", "ticket_code": "SDS", "adult_fare": 500, "restriction_code": None,
              "composite_indicator": "Y", "suppress_mkr": False, "start_date": PAST, "end_date": FOREVER}],
    )
    assert prices(connection, directory) == {"BBB": 1510}


# --- what is not a walk-up fare ---------------------------------------------


def test_advance_tickets_are_excluded(fares):
    """Their price here is a placeholder; the real one is in the reservation system."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "DG0", 50)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("DG0", "ADVANCE")],
        advance=["DG0"],
    )
    assert prices(connection, directory) == {"BBB": 1510}


def test_a_plusbus_zone_is_never_a_destination(fares):
    """A PlusBus zone is an add-on to a journey, not a place you travel to.

    They used to carry no CRS, which is what made this safe without anyone
    writing it down. The feed generation valid from 2026-06-30 gave four of them
    one — `QAB` BATH+BUS and friends — and Bristol Temple Meads gained a £5.40
    "destination" called BRISTOL TM+BUS. The zone here carries a county code
    too, because that is the arm that reads `LOC` directly and would let it back
    in on its own.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222"), flow(2, "1111", "3333")],
        fare_records=[fare(1, "SDS", 1510), fare(2, "PBD", 540)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("PBD", "PLUSBUS DAY")],
        stations=[("AAA", "1111", "1111", "ANYTOWN"),
                  ("BBB", "2222", "2222", "SOMEWHERE"),
                  ("QAB", "3333", "3333", "ANYTOWN+BUS")],
        counties=["01", "01", "01"],
    )
    assert prices(connection, directory) == {"BBB": 1510}
    assert connection.execute(
        "select count(*) from fare_alias where crs = 'QAB'").fetchone()[0] == 0


def test_a_fare_requiring_a_reservation_is_not_a_walk_up_fare(fares):
    """RSPS5045 4.6.2 field 23. `AO2 AIRPORT ADV STD` names no train anywhere —
    not in its description, its validity or its restriction — so the reservation
    flag is the only thing in the feed that catches it. It was the cheapest
    "walk-up" to Manchester Airport from every origin tested.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 2545), fare(1, "AO2", 1370)],
        tickets=[ticket("SDS", "ANYTIME DAY S"),
                 ticket("AO2", "AIRPORT ADV STD", reservation="B")],
        advance=[],
    )
    assert prices(connection, directory) == {"BBB": 2545}


def test_every_reservation_value_but_N_bars_a_walk_up_fare(fares):
    """`N` is the only value meaning no reservation; `O`, `R`, `B` and `E` all
    require one. `O` is the one worth pinning: the spec's text for it duplicates
    `R`'s, so it reads like a slip, and reading it as "optional" would let the
    ten-deep `Day-Flex` price ladder back in as a walk-up fare.
    """
    for value in ("O", "R", "B", "E"):
        connection, directory = fares(
            flows=[flow(1, "1111", "2222")],
            fare_records=[fare(1, "SDS", 1880), fare(1, "FE0", 550)],
            tickets=[ticket("SDS", "ANYTIME DAY S"),
                     ticket("FE0", "Day-Flex", reservation=value)],
        )
        assert prices(connection, directory) == {"BBB": 1880}, value


def test_a_reserved_fare_is_reclassified_rather_than_discarded(fares):
    """The booked-train family is offered by `--advance`, not thrown away."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 2545), fare(1, "AO2", 1370)],
        tickets=[ticket("SDS", "ANYTIME DAY S"),
                 ticket("AO2", "AIRPORT ADV STD", reservation="B")],
    )
    rows = cheapest_from(connection, directory, "AAA", TRAVEL, include_advance=True)
    assert {row[0]: row[3] for row in rows} == {"BBB": 1370}


def test_a_package_is_not_a_walk_up_fare(fares):
    """RSPS5045 4.6.2 field 29. The `8A*` series is described exactly like the
    ordinary fare of the same name — `8AB` is "ANYTIME DAY S" at £5.10 — so no
    description marker could ever have found it.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "8AB", 510)],
        tickets=[ticket("SDS", "ANYTIME DAY S"),
                 ticket("8AB", "ANYTIME DAY S", package="S")],
    )
    assert prices(connection, directory) == {"BBB": 1510}


def test_a_package_is_not_offered_as_an_advance_fare_either(fares):
    """A package buys parking or admission alongside travel, so unlike the
    reserved fares it is rejected outright rather than reclassified.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "8AB", 510)],
        tickets=[ticket("SDS", "ANYTIME DAY S"),
                 ticket("8AB", "ANYTIME DAY S", package="S")],
    )
    rows = cheapest_from(connection, directory, "AAA", TRAVEL, include_advance=True)
    assert {row[0]: row[3] for row in rows} == {"BBB": 1510}
    rejected = connection.execute(
        "select reason from fare_reject where ticket_code = '8AB'").fetchone()
    assert rejected and "package" in rejected[0]


def test_a_product_described_as_advance_is_excluded_even_if_tap_misses_it(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "DGA", 50)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("DGA", "SALE ADVANCE")],
        advance=[],  # TAP does not list it
    )
    assert prices(connection, directory) == {"BBB": 1510}


def test_test_data_ticket_types_are_excluded(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "NAP", 230)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("NAP", "FOR TEST USE ONLY")],
    )
    assert prices(connection, directory) == {"BBB": 1510}


def test_not_for_travel_products_are_excluded(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "XXX", 10)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("XXX", "SOMETHING", group="E")],
    )
    assert prices(connection, directory) == {"BBB": 1510}


def test_family_and_group_products_are_excluded(fares):
    """"NTH FAM S 1A2C" prices three people and would undercut the adult fare."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "NF2", 1260)],
        tickets=[ticket("SDS", "ANYTIME DAY S"),
                 ticket("NF2", "NTH FAM S 1A2C", passengers=3)],
    )
    assert prices(connection, directory) == {"BBB": 1510}


def test_complimentary_tickets_are_excluded(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "X2C", 5)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("X2C", "XC COMP STD")],
    )
    assert prices(connection, directory) == {"BBB": 1510}


def test_a_flat_rate_product_is_excluded_by_its_price_spread(fares):
    """A real fare varies with distance; "Kid with Adult" is £2 everywhere."""
    flows = [flow(i, "1111", f"{i:04d}") for i in range(1, 41)]
    stations = [("AAA", "1111", "1111")] + [
        (f"S{i:02d}", f"{i:04d}", f"{i:04d}") for i in range(1, 41)
    ]
    fare_records = (
        [fare(i, "KWA", 200) for i in range(1, 41)]        # identical everywhere
        + [fare(i, "SDS", 1000 + i * 10) for i in range(1, 41)]  # varies
    )
    connection, directory = fares(
        flows=flows, fare_records=fare_records,
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("KWA", "Kid with Adult")],
        stations=stations,
    )
    found = prices(connection, directory)

    assert found["S01"] == 1010  # the varying fare, not the flat £2
    assert min(found.values()) == 1010

    reasons = dict(connection.execute(
        "select ticket_code, reason from fare_reject"
    ).fetchall())
    assert reasons["KWA"] == "flat rate, not a distance-based fare"


def test_first_class_is_available_on_request(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "FOS", 4000)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("FOS", "ANYTIME 1S", cls=1)],
    )
    standard = cheapest_from(connection, directory, "AAA", TRAVEL)
    first = cheapest_from(connection, directory, "AAA", TRAVEL, ticket_class=1)

    assert standard[0][3] == 1510
    assert first[0][3] == 4000


# --- restrictions ------------------------------------------------------------

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TUESDAY = dt.date(2026, 8, 4)
SATURDAY = dt.date(2026, 8, 1)


def _write_restrictions(directory, bands):
    """Minimal RST tables: the bands supplied, in force every weekday."""
    weekdays = {d: d not in ("saturday", "sunday") for d in DAYS}
    pq.write_table(
        pa.Table.from_pylist(
            [{"cf_mkr": "C", "start_date": PAST, "end_date": FOREVER}],
            schema=pa.schema([("cf_mkr", pa.string()), ("start_date", pa.date32()),
                              ("end_date", pa.date32())])),
        directory / "restriction_dates.parquet")
    pq.write_table(
        pa.Table.from_pylist(
            # A band is (code, from, to, sense, location) plus, optionally,
            # min_fare_flag and out_ret — 'O' for the outward leg, 'R' for the
            # journey home. Sequences are distinct so each band keeps its own
            # date window rather than collapsing into one.
            [{"cf_mkr": "C", "restriction_code": c, "sequence_no": f"{i:04d}",
              "out_ret": rest[1] if len(rest) > 1 else "O",
              "time_from": f, "time_to": t,
              "arr_dep_via": sense, "location": loc,
              # 'Y' means a minimum fare rather than a bar.
              "min_fare_flag": bool(rest and rest[0])}
             for i, (c, f, t, sense, loc, *rest) in enumerate(bands, start=1)],
            schema=pa.schema([
                ("cf_mkr", pa.string()), ("restriction_code", pa.string()),
                ("sequence_no", pa.string()), ("out_ret", pa.string()),
                ("time_from", pa.int32()), ("time_to", pa.int32()),
                ("arr_dep_via", pa.string()), ("location", pa.string()),
                ("min_fare_flag", pa.bool_())])),
        directory / "restriction_time.parquet")
    pq.write_table(
        pa.Table.from_pylist([], schema=pa.schema([
            ("cf_mkr", pa.string()), ("restriction_code", pa.string()),
            ("sequence_no", pa.string()), ("out_ret", pa.string()),
            ("date_from", pa.string()), ("date_to", pa.string()),
            *[(d, pa.bool_()) for d in DAYS]])),
        directory / "restriction_time_date.parquet")
    pq.write_table(
        pa.Table.from_pylist(
            [{"cf_mkr": "C", "restriction_code": c, "date_from": "0101",
              "date_to": "1231", **weekdays} for c, *_ in bands],
            schema=pa.schema([
                ("cf_mkr", pa.string()), ("restriction_code", pa.string()),
                ("date_from", pa.string()), ("date_to", pa.string()),
                *[(d, pa.bool_()) for d in DAYS]])),
        directory / "restriction_header_date.parquet")


def test_a_restricted_fare_disappears_at_a_banned_departure_time(fares):
    """Off-Peak at £9.90 is banned leaving AAA in the morning peak."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "CDS", 990, restriction="0W")],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("CDS", "OFF-PEAK DAY S")],
        bands=[("0W", 270, 565, "D", "AAA")],  # 04:30-09:25 departing AAA
    )
    cheapest = lambda **kw: {
        r[0]: (r[1], r[3]) for r in cheapest_from(connection, directory, "AAA", TUESDAY, **kw)
    }["BBB"]

    assert cheapest() == ("CDS", 990)                          # unchecked
    assert cheapest(depart_minutes=660) == ("CDS", 990)        # 11:00, valid
    assert cheapest(depart_minutes=450) == ("SDS", 1510)       # 07:30, falls back


def test_the_same_departure_is_fine_at_the_weekend(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "CDS", 990, restriction="0W")],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("CDS", "OFF-PEAK DAY S")],
        bands=[("0W", 270, 565, "D", "AAA")],
    )
    rows = {r[0]: (r[1], r[3]) for r in cheapest_from(
        connection, directory, "AAA", SATURDAY, depart_minutes=450)}

    assert rows["BBB"] == ("CDS", 990)  # peak restrictions are weekdays only


def test_an_arrival_side_restriction_uses_the_arrival_time(fares):
    """1C bans arriving into BBB before 10:00, so the journey time matters."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "CDS", 990, restriction="1C")],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("CDS", "OFF-PEAK DAY S")],
        bands=[("1C", 270, 599, "A", "BBB")],
    )
    cheapest = lambda **kw: {
        r[0]: (r[1], r[3]) for r in cheapest_from(connection, directory, "AAA", TUESDAY, **kw)
    }["BBB"]

    assert cheapest(depart_minutes=360, arrivals={"BBB": 480}) == ("SDS", 1510)  # arrives 08:00
    assert cheapest(depart_minutes=660, arrivals={"BBB": 780}) == ("CDS", 990)   # arrives 13:00


def test_only_a_via_band_looks_at_the_middle_of_the_journey(fares):
    """**`V` is the only marker that means a station in the middle.**

    RSPS5045 4.19.8 says it twice: field 9 is "arrivals at, departures from or
    *changing at* the location", and field 10 calls the location "a journey
    origin/destination or via location". So an `A` or `D` band naming a station
    is about a journey that ends or starts there.

    Reading them as "any journey through here" instead made 1,648 fares dearer
    than any retailer sells. Restriction `LK` is why it cannot be right: band
    0018 bars departing Euston before 10:29 while band 0006 bars departing
    Leighton Buzzard before 12:33, and one train cannot satisfy both — they are
    per-origin rules, not a way of naming trains.
    """
    world = dict(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 29080), fare(1, "SSS", 15050, restriction="PB")],
        tickets=[ticket("SDS", "ANYTIME S"), ticket("SSS", "SUPER OFFPEAK S")],
        stations=[("AAA", "1111", "1111"), ("BBB", "2222", "2222"),
                  ("MID", "3333", "3333")],
    )
    passing = {"BBB": [("AAA", 484, 484, False), ("MID", 603, 603, False),
                       ("BBB", 972, 972, False)]}
    changing = {"BBB": [("AAA", 484, 484, False), ("MID", 603, 603, True),
                        ("BBB", 972, 972, False)]}
    ask = lambda w, **kw: {
        r[0]: (r[1], r[3]) for r in cheapest_from(connection, directory, "AAA",
                                                  TUESDAY, **kw)}["BBB"]

    # An arrival band at MID: MID is not an end of this journey, so it says
    # nothing about it however the journey passes through.
    connection, directory = fares(**world, bands=[("PB", 270, 677, "A", "MID")])
    assert ask(world, depart_minutes=480, arrivals={"BBB": 972},
               calls=passing) == ("SSS", 15050)

    # A *via* band at MID bites only where the journey changes there.
    connection, directory = fares(**world, bands=[("PB", 270, 677, "V", "MID")])
    assert ask(world, depart_minutes=480, arrivals={"BBB": 972},
               calls=passing) == ("SSS", 15050)
    assert ask(world, depart_minutes=480, arrivals={"BBB": 972},
               calls=changing) == ("SDS", 29080)


def test_calling_times_the_caller_does_not_supply_bar_nothing(fares):
    """The same guard the return leg and the TOC conditions use: not knowing
    where a journey went is not a reason to withdraw a fare."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 29080), fare(1, "SSS", 15050, restriction="PB")],
        tickets=[ticket("SDS", "ANYTIME S"), ticket("SSS", "SUPER OFFPEAK S")],
        bands=[("PB", 270, 677, "V", "MID")],
        stations=[("AAA", "1111", "1111"), ("BBB", "2222", "2222"),
                  ("MID", "3333", "3333")],
    )
    rows = {r[0]: (r[1], r[3]) for r in cheapest_from(
        connection, directory, "AAA", TUESDAY, depart_minutes=480,
        arrivals={"BBB": 972})}

    assert rows["BBB"] == ("SSS", 15050)


def test_an_unrestricted_fare_is_never_removed(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        bands=[("0W", 270, 565, "D", "AAA")],
    )
    rows = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TUESDAY, depart_minutes=450)}

    assert rows == {"BBB": 1510}


# --- railcards ---------------------------------------------------------------

RLC_SCHEMA = pa.schema([
    ("railcard_code", pa.string()), ("description", pa.string()),
    ("adult_status", pa.string()), ("child_status", pa.string()),
    ("min_adults", pa.int64()), ("max_adults", pa.int64()),
    ("min_children", pa.int64()), ("max_children", pa.int64()),
    ("min_passengers", pa.int64()), ("max_passengers", pa.int64()),
    ("restricted_by_train", pa.bool_()), ("restricted_by_date", pa.bool_()),
    ("restricted_by_area", pa.bool_()), ("display_flag", pa.string()),
    ("start_date", pa.date32()), ("end_date", pa.date32()),
])
DIS_SCHEMA = pa.schema([
    ("status_code", pa.string()), ("discount_category", pa.int64()),
    ("discount_indicator", pa.string()), ("discount_percentage", pa.int64()),
])
RCM_SCHEMA = pa.schema([
    ("railcard_code", pa.string()), ("ticket_code", pa.string()),
    ("minimum_fare", pa.int64()), ("start_date", pa.date32()), ("end_date", pa.date32()),
])
FNS_SCHEMA = pa.schema([
    ("origin_code", pa.string()), ("destination_code", pa.string()),
    ("route_code", pa.string()), ("railcard_code", pa.string()),
    ("ticket_code", pa.string()), ("adult_nodis_flag", pa.string()),
    ("use_nlc", pa.string()), ("adult_add_on_amount", pa.int64()),
    ("adult_rebook_flag", pa.string()),
    ("start_date", pa.date32()), ("end_date", pa.date32()),
])
FRR_SCHEMA = pa.schema([
    ("rule_id", pa.string()), ("sequence_no", pa.string()),
    ("upper_limit", pa.int64()), ("round_to", pa.int64()),
    ("start_date", pa.date32()), ("end_date", pa.date32()),
])


def railcard(code, description, status, *, per_mille, category=1,
             min_adults=1, max_adults=1, min_children=0, max_children=0,
             area=False):
    return ({"railcard_code": code, "description": description,
             "adult_status": status, "child_status": "XXX",
             "min_adults": min_adults, "max_adults": max_adults,
             "min_children": min_children, "max_children": max_children,
             "min_passengers": 1, "max_passengers": 1,
             "restricted_by_train": False, "restricted_by_date": False,
             "restricted_by_area": area, "display_flag": "Y",
             "start_date": PAST, "end_date": FOREVER},
            {"status_code": status, "discount_category": category,
             "discount_indicator": "0", "discount_percentage": per_mille})


def _write_descriptions(directory, validities=(), headers=()):
    """The tables `fares_between` joins out to for its explanations. Empty is
    fine — every join is a left one, so a fare with no route or validity record
    still appears, just without the words."""
    for name, schema in (
        ("route", pa.schema([
            ("route_code", pa.string()), ("description", pa.string()),
            ("start_date", pa.date32()), ("end_date", pa.date32())])),
        # The full TVL shape, since `build_ticket_validity` reads the return
        # side too. A validity entry is (code, breaks) or (code, breaks, kwargs)
        # where kwargs sets ret_days, ret_months and the rest.
        ("ticket_validity", pa.schema([
            ("validity_code", pa.string()), ("description", pa.string()),
            ("out_days", pa.int64()), ("out_months", pa.int64()),
            ("ret_days", pa.int64()), ("ret_months", pa.int64()),
            ("ret_after_days", pa.int64()), ("ret_after_months", pa.int64()),
            ("ret_after_day", pa.string()),
            ("break_out", pa.bool_()), ("break_in", pa.bool_()),
            ("out_description", pa.string()), ("rtn_description", pa.string()),
            ("start_date", pa.date32()), ("end_date", pa.date32())])),
        # `change_ind` is 4.19.3 field 10 — whether a change of trains is
        # allowed at all. Absent from a fixture world means "allowed", which is
        # what 803 of the 839 current restrictions say.
        ("restriction_header", pa.schema([
            ("cf_mkr", pa.string()), ("restriction_code", pa.string()),
            ("description", pa.string()), ("desc_out", pa.string()),
            ("change_ind", pa.bool_())])),
    ):
        rows = [
            {"validity_code": code, "description": "ON DATE SHOWN",
             "out_days": 1, "out_months": 0,
             "ret_days": 0, "ret_months": 0,
             "ret_after_days": 0, "ret_after_months": 0, "ret_after_day": None,
             "break_out": breaks, "break_in": breaks,
             "out_description": "ON DATE SHOWN", "rtn_description": "ON DATE SHOWN",
             "start_date": PAST, "end_date": FOREVER,
             **(extra[0] if extra else {})}
            for code, breaks, *extra in validities
        ] if name == "ticket_validity" else [
            # (code, description, change_ind) — whether a change of trains is
            # allowed at all, which no time band can express.
            {"cf_mkr": "C", "restriction_code": code, "description": description,
             "desc_out": description, "change_ind": change_allowed}
            for code, description, change_allowed in headers
        ] if name == "restriction_header" else []
        pq.write_table(pa.Table.from_pylist(rows, schema=schema),
                       directory / f"{name}.parquet")


def _write_routeing(connection, rgk_rules, london_marker, london_terminals,
                    toc_rules=()):
    """The routeing guide's own route conditions, which `rail build` loads from
    RGK. Empty here unless a test asks for them, so the fares feed's RTE
    records stay in charge exactly as they do for a route RGK never mentions."""
    connection.execute(
        "create table route_rule (route_code varchar, entry_type varchar, crs varchar)")
    for route_code, entry_type, crs in rgk_rules:
        connection.execute("insert into route_rule values (?, ?, ?)",
                           [route_code, entry_type, crs])
    # RGK's TOC conditions live on the raw table, not the expanded location one.
    connection.execute("""create table route_condition (
        route_code varchar, entry_type varchar, crs varchar,
        is_group boolean, mode_code varchar, toc_id varchar)""")
    for route_code, entry_type, code in toc_rules:
        # 'T'/'X' name a TOC, 'L'/'N' a transport mode.
        is_mode = entry_type in ('L', 'N')
        connection.execute(
            "insert into route_condition values (?, ?, null, false, ?, ?)",
            [route_code, entry_type,
             code if is_mode else None, None if is_mode else code])
    connection.execute("create table route_london (route_code varchar, london_marker varchar)")
    if london_marker is not None:
        connection.execute("insert into route_london values ('00000', ?)",
                           [london_marker])
    connection.execute("create table london_station (crs varchar, is_terminal boolean)")
    for crs in london_terminals:
        connection.execute("insert into london_station values (?, true)", [crs])
    connection.execute("""
        create table route_rgk_covered as
        select distinct route_code from route_rule
        union
        select distinct route_code from route_london where london_marker in ('0','1')
    """)


FLEXI_SCHEMA = pa.schema([
    ("ticket_code", pa.string()), ("start_date", pa.date32()),
    ("end_date", pa.date32()), ("bundle_size", pa.int32()),
    ("bi_directional", pa.bool_()), ("transferable", pa.bool_()),
])


def _write_flexi(directory, flexi):
    """RSPS5052 bundle sizes. Returns None when nothing was asked for, which is
    how the build behaves before `rail fetch --supplementary` has ever run."""
    if not flexi:
        return None
    supplementary = directory.parent / "supplementary"
    supplementary.mkdir(exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(
            [{"ticket_code": code, "start_date": PAST, "end_date": FOREVER,
              "bundle_size": size, "bi_directional": True, "transferable": False}
             for code, size in flexi],
            schema=FLEXI_SCHEMA),
        supplementary / "flexi_product.parquet")
    return supplementary


def _write_railcards(directory, railcards, minimums, fns=(), rounding=(),
                     geography=(), stations=(), railcard_rules=()):
    cards = [c for c, _ in railcards]
    discounts = [d for _, d in railcards]
    pq.write_table(pa.Table.from_pylist(cards, schema=RLC_SCHEMA),
                   directory / "railcard.parquet")
    pq.write_table(pa.Table.from_pylist(discounts, schema=DIS_SCHEMA),
                   directory / "status_discount.parquet")
    pq.write_table(
        pa.Table.from_pylist(
            [{"railcard_code": r, "ticket_code": t, "minimum_fare": m,
              "start_date": PAST, "end_date": FOREVER} for r, t, m in minimums],
            schema=RCM_SCHEMA),
        directory / "railcard_minimum_fare.parquet")
    pq.write_table(
        pa.Table.from_pylist(
            [{"origin_code": o, "destination_code": d, "route_code": r,
              "railcard_code": rc, "ticket_code": tk, "adult_nodis_flag": flag,
              "use_nlc": None, "adult_add_on_amount": addon,
              "adult_rebook_flag": rebook,
              "start_date": PAST, "end_date": FOREVER}
             for o, d, r, rc, tk, flag, addon, rebook in fns],
            schema=FNS_SCHEMA),
        directory / "non_standard_discount.parquet")
    # RSPS5045 4.19.18: a railcard banned outright for a ticket or route, or
    # subject to a restriction code's time bands.
    pq.write_table(
        pa.Table.from_pylist(
            [{"cf_mkr": "C", "railcard_code": code, "ticket_code": tkt,
              "route_code": route, "location": None,
              "restriction_code": restriction, "total_ban": restriction is None}
             for code, tkt, route, restriction in railcard_rules],
            schema=pa.schema([
                ("cf_mkr", pa.string()), ("railcard_code", pa.string()),
                ("ticket_code", pa.string()), ("route_code", pa.string()),
                ("location", pa.string()), ("restriction_code", pa.string()),
                ("total_ban", pa.bool_())])),
        directory / "restriction_railcard.parquet")
    # RSPS5045 4.20.4: which locations an area-restricted railcard covers.
    by_crs = {row[0]: row[1] for row in stations}
    pq.write_table(
        pa.Table.from_pylist(
            [{"uic": by_crs.get(crs, crs), "railcard_code": code,
              "end_date": FOREVER}
             for code, crs in geography],
            schema=pa.schema([("uic", pa.string()), ("railcard_code", pa.string()),
                              ("end_date", pa.date32())])),
        directory / "location_railcard.parquet")
    # FRR rule 01 by default: 5p across every band, which is what the observed
    # prices say. A test wanting a banded rule passes its own.
    pq.write_table(
        pa.Table.from_pylist(
            [{"rule_id": "01", "sequence_no": f"{i:02d}",
              "upper_limit": upto, "round_to": to,
              "start_date": PAST, "end_date": FOREVER}
             for i, (upto, to) in enumerate(rounding)],
            schema=FRR_SCHEMA),
        directory / "rounding_rule.parquet")


def test_a_railcard_takes_its_percentage_off(fares):
    """334 per mille is 33.4% off, not 334%. £60.00 becomes £39.95 after rounding."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 6000)],
        tickets=[ticket("SDS", "ANYTIME DAY S", category=1)],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
    )
    full = {r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL)}
    with_card = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="YNG")}

    assert full["BBB"] == 6000
    # 6000 * 0.666 = 3996, rounded down to the nearest 5p.
    assert with_card["BBB"] == 3995


def test_fares_round_down_to_the_nearest_five_pence(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1000)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
    )
    rows = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="YNG")}

    # 1000 * 0.666 = 666, which rounds down to 665.
    assert rows["BBB"] == 665


def minimum_fare_world(fares, fare_pence, minimum):
    """A railcard whose restriction charges a minimum in the morning peak.

    The 16-25's real one is restriction R1: "MINIMUM FARES MAY APPLY BEFORE
    1000 MONDAY-FRIDAY", a single band 04:30-09:59 with min_fare_flag set.
    """
    return fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", fare_pence)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
        minimums=[("YNG", "SDS", minimum)],
        railcard_rules=[("YNG", None, None, "R1")],
        bands=[("R1", 270, 599, "D", None, True)],
    )


def test_a_railcard_minimum_fare_lifts_a_cheap_discount(fares):
    connection, directory = minimum_fare_world(fares, 1000, 800)
    cheapest = lambda dep: {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TUESDAY, railcard="YNG",
        depart_minutes=dep)}["BBB"]

    assert cheapest(8 * 60) == 800    # inside the band: the minimum, not £6.65
    assert cheapest(11 * 60) == 665   # outside it: the plain discount


def test_a_minimum_fare_only_applies_when_the_restriction_says_so(fares):
    """RSPS5045 4.16.1.1: minimum fares apply "when railcards are used on
    certain trains (determined by the train restriction)". Charging one all day
    overprices every off-peak journey on a ticket that has a minimum at all —
    it moved 84 of Euston's Sunday fares."""
    connection, directory = minimum_fare_world(fares, 1000, 800)
    weekend = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", dt.date(2026, 8, 2), railcard="YNG",
        depart_minutes=8 * 60)}

    assert weekend["BBB"] == 665  # R1 is Mon-Fri, so no minimum on a Sunday


def test_a_minimum_fare_never_exceeds_the_undiscounted_price(fares):
    connection, directory = minimum_fare_world(fares, 500, 900)
    rows = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TUESDAY, railcard="YNG",
        depart_minutes=8 * 60)}

    assert rows["BBB"] == 500  # a railcard never makes a fare dearer


def test_a_ticket_outside_the_discount_category_is_not_discounted(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 6000)],
        tickets=[ticket("SDS", "ANYTIME DAY S", category=7)],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003",
                            per_mille=334, category=1)],
    )
    rows = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="YNG")}

    assert rows["BBB"] == 6000


def test_a_railcard_specific_non_derivable_fare_is_used_as_stated(fares):
    """Those prices are already discounted; taking a percentage off again is wrong."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 6000)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
        nfo=[{"origin_code": "1111", "destination_code": "2222", "route_code": "00000",
              "railcard_code": "YNG", "ticket_code": "SDS", "adult_fare": 4200,
              "restriction_code": None, "composite_indicator": "Y", "suppress_mkr": False,
              "start_date": PAST, "end_date": FOREVER}],
    )
    rows = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="YNG")}

    assert rows["BBB"] == 4200


def test_railcard_eligibility_depends_on_the_party(fares):
    from rail.model import eligible_railcards

    connection, _ = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 6000)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        railcards=[
            railcard("YNG", "16-25 RAILCARD", "003", per_mille=334),
            railcard("2TR", "TWO TOGETHER", "042", per_mille=334,
                     min_adults=2, max_adults=2),
        ],
    )

    solo = {code for code, _ in eligible_railcards(connection, adults=1)}
    pair = {code for code, _ in eligible_railcards(connection, adults=2)}

    assert solo == {"YNG"}          # Two Together needs two adults
    assert pair == {"2TR"}


# --- Advance fares -----------------------------------------------------------
#
# Advance prices in this feed are real and vary with distance: York to London
# ranges £22.00 to £73.00 across the price ladder against a £70.70 walk-up
# Off-Peak. What the feed does not carry is quota, so nothing says a given price
# point is on sale for a given train. They are therefore opt-in, not excluded.


def test_advance_fares_are_left_out_by_default(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 7070), fare(1, "NAA", 2200)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("NAA", "ADVANCE")],
    )
    assert prices(connection, directory) == {"BBB": 7070}


def test_advance_fares_are_used_when_asked_for(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 7070), fare(1, "NAA", 2200)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("NAA", "ADVANCE")],
    )
    row = cheapest_from(
        connection, directory, "AAA", TRAVEL, include_advance=True
    )[0]

    assert row[3] == 2200
    assert row[4] is True  # flagged as Advance, not a walk-up fare


def test_a_bundle_whose_name_does_not_say_so_needs_the_supplementary_file(fares):
    """"Multiflex" is a bundle and reads like a ticket.

    Most carnets announce themselves — CARNET, FLXIPASS, DAYSAVE — and
    `NON_PUBLIC_MARKERS` catches those by name. This is the case that needs
    RSPS5052, because nothing in the description or in RSPS5045 gives it away:
    min and max passengers are both 1 and the price varies with distance.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "MFX", 900)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("MFX", "Multiflex")],
        flexi=[("MFX", 12)],
    )

    assert prices(connection, directory) == {"BBB": 1510}
    assert connection.execute(
        "select reason from fare_reject where ticket_code = 'MFX'"
    ).fetchone() == ("flexi bundle, priced for several journeys",)


def test_without_the_bundle_list_a_multiflex_looks_like_a_cheap_single(fares):
    """The state before `rail fetch --supplementary`, recorded deliberately.

    900 is cheaper than 1510 and wins, which is exactly the failure the
    supplementary file exists to prevent. A carnet that *says* carnet is caught
    by name whether or not the file has been fetched — see the test below.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "MFX", 900)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("MFX", "Multiflex")],
    )
    assert prices(connection, directory) == {"BBB": 900}


def test_a_carnet_that_names_itself_needs_no_supplementary_file(fares):
    """Euston was quoting `CO5 CARNET OFFPK 5` — five journeys — to 13 stations.

    `%FLEXI%` would have been the obvious marker and would have been wrong:
    "FLEXI ADVANCE" is a real single fare, a changeable Advance. The bundles are
    named precisely so that one survives.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "CO5", 570),
                      fare(1, "3LS", 800)],
        tickets=[ticket("SDS", "ANYTIME DAY S"),
                 ticket("CO5", "CARNET OFFPK 5"),
                 ticket("3LS", "FLEXI ADVANCE")],
        advance=["3LS"],
    )

    assert prices(connection, directory) == {"BBB": 1510}
    assert connection.execute(
        "select reason from fare_reject where ticket_code = 'CO5'"
    ).fetchone() == ("bundle of journeys, not a single fare",)
    # The Advance survives: it is a fare, just a flexible one.
    assert connection.execute(
        "select is_advance_fare from ticket_type_current where ticket_code = '3LS'"
    ).fetchone() == (True,)


def test_a_walk_up_fare_is_not_flagged_as_advance(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 7070)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
    )
    assert cheapest_from(connection, directory, "AAA", TRAVEL)[0][4] is False


def test_walk_up_and_advance_are_disjoint_halves_of_what_is_sellable(fares):
    connection, _ = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 7070), fare(1, "NAA", 2200)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("NAA", "ADVANCE")],
    )
    counts = connection.execute("""
        select count(*) filter (where is_sellable),
               count(*) filter (where is_walk_up),
               count(*) filter (where is_advance_fare),
               count(*) filter (where is_walk_up and is_advance_fare)
        from ticket_type_current
    """).fetchone()

    assert counts == (2, 1, 1, 0)


def test_inclusive_tour_rates_are_excluded_even_with_advance(fares):
    """ITX is sold to tour operators; the 5p here is a nominal figure."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 7070), fare(1, "GPH", 5)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("GPH", "ITX STD HIGH RT")],
        advance=["GPH"],  # TAP does list it, which is why the flag is not enough
    )
    rows = cheapest_from(connection, directory, "AAA", TRAVEL, include_advance=True)

    assert rows[0][3] == 7070
    reasons = dict(connection.execute(
        "select ticket_code, reason from fare_reject").fetchall())
    assert reasons["GPH"] == "inclusive tour rate, priced inside a package"


def test_the_flat_rate_advance_placeholders_stay_excluded(fares):
    """"SALE ADVANCE" is 50p on every flow — a placeholder, not a price."""
    flows = [flow(i, "1111", f"{i:04d}") for i in range(1, 41)]
    stations = [("AAA", "1111", "1111")] + [
        (f"S{i:02d}", f"{i:04d}", f"{i:04d}") for i in range(1, 41)
    ]
    records = (
        [fare(i, "DGA", 50) for i in range(1, 41)]
        + [fare(i, "NAA", 2000 + i * 10) for i in range(1, 41)]
    )
    connection, directory = fares(
        flows=flows, fare_records=records,
        tickets=[ticket("DGA", "SALE ADVANCE"), ticket("NAA", "ADVANCE")],
        stations=stations,
    )
    found = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, include_advance=True)}

    assert found["S01"] == 2010  # the ladder, not the 50p placeholder


def test_a_railcard_still_discounts_an_advance_fare(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "NAA", 3000)],
        tickets=[ticket("NAA", "ADVANCE")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
    )
    rows = cheapest_from(connection, directory, "AAA", TRAVEL,
                         include_advance=True, railcard="YNG")

    assert rows[0][3] == 1995  # 3000 * 0.666, rounded down to 5p


# --- route conditions --------------------------------------------------------
#
# Most fares are not "any permitted": a route may require the journey to pass
# through a station ("VIA APPLEBY") or forbid it ("NOT VIA CHELTNHM"). Route-
# restricted fares are usually the cheaper ones, so failing to check them errs
# in one direction — quoting a price for a journey it is not valid on.


def routed(flow_id, origin, destination, route, *, ns_disc=0):
    return {"flow_id": flow_id, "origin_code": origin, "destination_code": destination,
            "route_code": route, "direction": "S", "ns_disc_ind": ns_disc,
            "start_date": PAST, "end_date": FOREVER}


def test_a_not_via_fare_is_refused_when_the_journey_goes_that_way(fares):
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00007"),   # NOT VIA CCC
               routed(2, "1111", "2222", "00000")],  # any permitted
        fare_records=[fare(1, "SDS", 900), fare(2, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        route_locations=[("00007", "CCC", "E")],
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }["BBB"]

    assert cheapest() == 900  # unchecked: the restricted fare wins
    assert cheapest(paths={"BBB": ["AAA", "CCC", "BBB"]}) == 1500  # via CCC, so refused
    assert cheapest(paths={"BBB": ["AAA", "DDD", "BBB"]}) == 900   # not via CCC, allowed


def test_a_via_fare_requires_the_journey_to_pass_through(fares):
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00010"),   # VIA CCC
               routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 900), fare(2, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        route_locations=[("00010", "CCC", "I")],
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }["BBB"]

    assert cheapest(paths={"BBB": ["AAA", "CCC", "BBB"]}) == 900   # went via CCC
    assert cheapest(paths={"BBB": ["AAA", "DDD", "BBB"]}) == 1500  # did not, so refused


def test_any_of_several_included_stations_will_do(fares):
    """"STRATFORD/LONDON" lists Euston, Liverpool Street and Stratford."""
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00003")],
        fare_records=[fare(1, "SDS", 900)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        route_locations=[("00003", "CCC", "I"), ("00003", "DDD", "I")],
    )
    cheapest = lambda path: {
        r[0]: r[3] for r in cheapest_from(
            connection, directory, "AAA", TRAVEL, paths={"BBB": path})
    }.get("BBB")

    assert cheapest(["AAA", "DDD", "BBB"]) == 900  # one of the two is enough
    assert cheapest(["AAA", "EEE", "BBB"]) is None  # neither


def test_a_route_with_no_location_records_is_left_alone(fares):
    """851 of 1,478 routes state their condition in prose only."""
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "09999")],
        fare_records=[fare(1, "SDS", 900)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        route_locations=[],
    )
    rows = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, paths={"BBB": ["AAA", "BBB"]})}

    assert rows["BBB"] == 900


def test_route_conditions_are_not_applied_without_a_path(fares):
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00010")],
        fare_records=[fare(1, "SDS", 900)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        route_locations=[("00010", "CCC", "I")],
    )
    rows = {r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL)}

    assert rows["BBB"] == 900  # no path supplied, so nothing to check against


# --- the routeing guide's own route conditions (RGK) --------------------------
#
# The fares feed's RTE records carry one thing per location: include or exclude.
# RGK carries the distinctions that make a condition enforceable, and each of
# these tests is a case RTE gets wrong.


def test_all_of_the_named_stations_must_be_on_the_journey(fares):
    """'A' is ALL-of, where RTE's 'I' cannot say whether it means all or any."""
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00007"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 900), fare(2, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        rgk_rules=[("00007", "A", "CCC"), ("00007", "A", "DDD")],
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }["BBB"]

    assert cheapest(paths={"BBB": ["AAA", "CCC", "DDD", "BBB"]}) == 900
    # Only one of the two: an ANY-of reading would wrongly allow this.
    assert cheapest(paths={"BBB": ["AAA", "CCC", "BBB"]}) == 1500


def test_any_one_of_the_named_stations_is_enough(fares):
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00007"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 900), fare(2, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        rgk_rules=[("00007", "I", "CCC"), ("00007", "I", "DDD")],
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }["BBB"]

    assert cheapest(paths={"BBB": ["AAA", "CCC", "BBB"]}) == 900
    assert cheapest(paths={"BBB": ["AAA", "EEE", "BBB"]}) == 1500


def test_not_via_london_means_every_terminal(fares):
    """The case the notes recorded as broken.

    Route 00700 NOT VIA LONDON is encoded in the fares feed as excluding Euston
    alone, so a journey via King's Cross passed a check it should fail. RGK
    carries it as a marker on the route, judged against all 19 London terminals.
    """
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00000"), routed(2, "1111", "2222", "00009")],
        fare_records=[fare(1, "SDS", 900), fare(2, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        london_marker="0",
        london_terminals=["EUS", "KGX", "PAD"],
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }["BBB"]

    assert cheapest(paths={"BBB": ["AAA", "KGX", "BBB"]}) == 1500  # not Euston, still London
    assert cheapest(paths={"BBB": ["AAA", "YRK", "BBB"]}) == 900


def test_a_route_that_must_go_via_london_is_refused_when_it_does_not(fares):
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00000"), routed(2, "1111", "2222", "00009")],
        fare_records=[fare(1, "SDS", 900), fare(2, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        london_marker="1",
        london_terminals=["EUS", "KGX"],
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }["BBB"]

    assert cheapest(paths={"BBB": ["AAA", "EUS", "BBB"]}) == 900
    assert cheapest(paths={"BBB": ["AAA", "YRK", "BBB"]}) == 1500


def test_rgk_wins_over_the_fares_feed_for_a_route_it_covers(fares):
    """RTE says exclude CCC; RGK says exclude DDD. RGK is the better source."""
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00007"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 900), fare(2, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        route_locations=[("00007", "CCC", "E")],
        rgk_rules=[("00007", "E", "DDD")],
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }["BBB"]

    assert cheapest(paths={"BBB": ["AAA", "DDD", "BBB"]}) == 1500
    assert cheapest(paths={"BBB": ["AAA", "CCC", "BBB"]}) == 900


def test_the_fares_feed_still_governs_a_route_rgk_never_mentions(fares):
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00007"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 900), fare(2, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        route_locations=[("00007", "CCC", "E")],
        rgk_rules=[("00123", "E", "DDD")],  # a different route entirely
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }["BBB"]

    assert cheapest(paths={"BBB": ["AAA", "CCC", "BBB"]}) == 1500


# --- all the prices, not just the cheapest ------------------------------------


def test_fare_options_returns_every_price_cheapest_first(fares):
    """What `--check-guide` walks down when it refuses the cheapest fare."""
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00700"),   # NOT VIA LONDON
               routed(2, "1111", "2222", "00000")],  # any permitted
        fare_records=[fare(1, "SDS", 7450), fare(2, "SDS", 23160)],
        tickets=[ticket("SDS", "ANYTIME S")],
    )
    rows = [r for r in fare_options(connection, directory, "AAA", TRAVEL)
            if r[0] == "BBB"]

    assert [r[3] for r in rows] == [7450, 23160]
    assert [r[5] for r in rows] == ["00700", "00000"]


def test_cheapest_from_keeps_the_head_of_each_group(fares):
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00700"),
               routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 7450), fare(2, "SDS", 23160)],
        tickets=[ticket("SDS", "ANYTIME S")],
    )
    rows = cheapest_from(connection, directory, "AAA", TRAVEL)

    assert [(r[0], r[3]) for r in rows] == [("BBB", 7450)]


def test_tickets_at_the_same_price_collapse_to_one_option(fares):
    """One row per distinct price: a caller stepping up the list wants the next
    price, not another ticket costing what the last one did."""
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 900), fare(1, "SOR", 900)],
        tickets=[ticket("SDS", "ANYTIME S"), ticket("SOR", "OFF-PEAK S")],
    )
    rows = [r for r in fare_options(connection, directory, "AAA", TRAVEL)
            if r[0] == "BBB"]

    assert len(rows) == 1


# --- explaining one pair's fares ---------------------------------------------


def test_fares_between_returns_every_ticket_with_its_governing_records(fares):
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 1510, restriction="0W"),
                      fare(1, "SOR", 900)],
        tickets=[ticket("SDS", "ANYTIME S"), ticket("SOR", "OFF-PEAK S")],
    )
    rows = fares_between(connection, directory, "AAA", "BBB", TRAVEL)

    assert [r["ticket_code"] for r in rows] == ["SOR", "SDS"]
    assert rows[1]["restriction_code"] == "0W"
    assert rows[0]["restriction_code"] is None


def test_fares_between_keeps_a_time_restricted_fare_in_the_answer(fares):
    """The question is "what fares exist and when may each be used", so a fare
    barred at this hour belongs in the list with its restriction named. It is
    `cheapest_from` that prices a particular journey."""
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 900, restriction="0W")],
        tickets=[ticket("SDS", "ANYTIME S")],
        bands=[("0W", 0, 1439, "D", "AAA")],
    )

    assert cheapest_from(connection, directory, "AAA", TUESDAY,
                         depart_minutes=600) == []
    assert len(fares_between(connection, directory, "AAA", "BBB", TUESDAY)) == 1


def test_both_classes_are_returned_when_none_is_asked_for(fares):
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 900), fare(1, "FST", 1800)],
        tickets=[ticket("SDS", "ANYTIME S"), ticket("FST", "ANYTIME 1S", cls=1)],
    )
    rows = fares_between(connection, directory, "AAA", "BBB", TRAVEL)

    assert sorted(r["tkt_class"] for r in rows) == [1, 2]
    first = fares_between(connection, directory, "AAA", "BBB", TRAVEL,
                          ticket_class=1)
    assert [r["ticket_code"] for r in first] == ["FST"]


def test_a_truncated_upgrade_is_not_a_fare(fares):
    """The description field is 15 characters, so "UPGRADE" arrives truncated.

    Matching the full word alone left 17 upgrade products classed as walk-up
    fares, and `--first-class` quoted a £7.50 weekend upgrade as the cheapest
    first-class fare from York to Darlington.
    """
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00000")],
        fare_records=[fare(1, "WET", 750), fare(1, "FST", 3720)],
        tickets=[ticket("WET", "WEEKEND 1ST UPG", cls=1),
                 ticket("FST", "OFF-PEAK 1S", cls=1)],
    )
    rows = cheapest_from(connection, directory, "AAA", TRAVEL, ticket_class=1)

    assert [(r[1], r[3]) for r in rows] == [("FST", 3720)]
    assert connection.execute(
        "select reason from fare_reject where ticket_code = 'WET'"
    ).fetchone() == ("supplement, not a fare on its own",)


def test_an_upgrade_naming_neither_upgrade_nor_supplement(fares):
    """`25Q STDPREM ONBOARD` is Avanti's on-board Standard Premium upgrade,
    bought from the crew on a ticket you already hold.

    Nothing structural gives it away: the price varies with distance so the
    flat-rate test cannot see it, the validity is the ordinary "on date shown"
    so the booked-train rule does not catch it, and it declares one passenger.
    It was the cheapest standard walk-up to 44 destinations from Euston and 52
    from Liverpool — every Avanti West Coast origin — quoting £26.50 to
    Birmingham where a retailer's cheapest walk-up is £20.90.
    """
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00000"),
               routed(2, "1111", "3333", "00000")],
        # Two flows at two prices, so the flat-rate test has nothing to say.
        fare_records=[fare(1, "25Q", 2650), fare(1, "OPS", 2090),
                      fare(2, "25Q", 800), fare(2, "OPS", 1200)],
        tickets=[ticket("25Q", "STDPREM ONBOARD"), ticket("OPS", "SUPER OFFPEAK S")],
        stations=[("AAA", "1111", "1111"), ("BBB", "2222", "2222"),
                  ("CCC", "3333", "3333")],
    )
    rows = cheapest_from(connection, directory, "AAA", TRAVEL)

    assert {r[0]: (r[1], r[3]) for r in rows} == {
        "BBB": ("OPS", 2090), "CCC": ("OPS", 1200)}
    assert connection.execute(
        "select reason from fare_reject where ticket_code = '25Q'"
    ).fetchone() == ("upgrade bought on board, not a fare on its own",)


def test_an_age_restricted_fare_is_not_an_adult_fare(fares):
    """The same shape as a concession — a condition the passenger must meet,
    written as a ticket type rather than as a discount — so nothing structural
    sees it. `TRQ TrainLinkC16-18` was quoting 75p from Headbolt Lane to
    Skelmersdale Bus Link, on a single flow where the flat-rate test cannot
    judge it because a modal share over one flow is trivially 1.0. The adult
    `TRP TrainLink C` on the same flow is £1.50 — exactly double, which is what
    a half fare should be.
    """
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00000")],
        fare_records=[fare(1, "TRQ", 75), fare(1, "TRP", 150)],
        tickets=[ticket("TRQ", "TrainLinkC16-18"), ticket("TRP", "TrainLink C")],
    )
    rows = cheapest_from(connection, directory, "AAA", TRAVEL)

    assert [(r[1], r[3]) for r in rows] == [("TRP", 150)]
    assert connection.execute(
        "select reason from fare_reject where ticket_code = 'TRQ'"
    ).fetchone() == ("age-restricted fare, not an adult fare",)


def test_one_fare_reached_by_two_codes_is_listed_once(fares):
    """A station is named by its own NLC, its fare group and every cluster
    holding it, and a flow may exist under more than one. Birmingham New Street
    is reached from Euston as `1127` and as cluster `T120`, both carrying the
    same ticket on the same route at the same price — one fare, printed twice.

    Rows differing only in price are *not* collapsed: RSPS5045 4.2.2 ranks the
    codes nowhere, so choosing between them would invent a precedence.
    """
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00000"),
               routed(2, "1111", "CLUS", "00000"),
               # A third flow elsewhere, so OPS varies with distance and the
               # flat-rate test has nothing to condemn.
               routed(3, "1111", "3333", "00000")],
        fare_records=[fare(1, "OPS", 2090), fare(2, "OPS", 2090),
                      fare(2, "SVS", 3140), fare(3, "OPS", 1200)],
        tickets=[ticket("OPS", "SUPER OFFPEAK S"), ticket("SVS", "OFF-PEAK S")],
        clusters=[("CLUS", "2222")],
        stations=[("AAA", "1111", "1111"), ("BBB", "2222", "2222"),
                  ("CCC", "3333", "3333")],
    )
    rows = fares_between(connection, directory, "AAA", "BBB", TRAVEL)

    assert [(r["ticket_code"], r["fare"]) for r in rows] == [
        ("OPS", 2090), ("SVS", 3140)]


# --- non-standard discounts (FNS) --------------------------------------------
#
# A flow whose ns_disc_ind is 1 or 3 does not take the standard percentage.
# RSPS5045 4.5.1.1 is explicit that the file is not used for undiscounted adult
# fares, so every test here needs a railcard to show anything at all.


def fns(origin="****", dest="****", route="*****", railcard="***",
        ticket="***", flag=None, addon=None, rebook="N"):
    return (origin, dest, route, railcard, ticket, flag, addon, rebook)


def discounted(connection, directory, railcard="YNG"):
    return {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard=railcard)}


def test_an_add_on_is_charged_on_top_of_the_discount(fares):
    """The add-on covers the part of the journey the railcard does not."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222", ns_disc=1)],
        fare_records=[fare(1, "SDS", 1000)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
        fns=[fns(dest="2222", railcard="YNG", addon=120)],
    )
    # 1000 less 33.4% is 666, rounded down to 665, plus the £1.20 add-on.
    assert discounted(connection, directory) == {"BBB": 785}


def test_flag_d_leaves_the_undiscounted_price_standing(fares):
    """'D' says a *discounted* adult fare cannot be calculated."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222", ns_disc=1)],
        fare_records=[fare(1, "SDS", 1000)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
        fns=[fns(dest="2222", railcard="YNG", flag="D")],
    )
    assert discounted(connection, directory) == {"BBB": 1000}


def test_flag_x_withdraws_the_fare_altogether(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222", ns_disc=1)],
        fare_records=[fare(1, "SDS", 1000)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
        fns=[fns(dest="2222", railcard="YNG", flag="X")],
    )
    assert discounted(connection, directory) == {}


def test_a_rebook_flag_means_no_fare_can_be_calculated(fares):
    """'Y' issue to the interchange and rebook; 'S' issue separate tickets."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222", ns_disc=1)],
        fare_records=[fare(1, "SDS", 1000)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
        fns=[fns(dest="2222", railcard="YNG", rebook="Y")],
    )
    assert discounted(connection, directory) == {}


def test_a_flow_not_marked_for_non_standard_discounts_is_untouched(fares):
    """ns_disc_ind 0 takes the ordinary percentage whatever FNS says."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222", ns_disc=0)],
        fare_records=[fare(1, "SDS", 1000)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
        fns=[fns(dest="2222", railcard="YNG", flag="X")],
    )
    assert discounted(connection, directory) == {"BBB": 665}


def test_the_undiscounted_adult_fare_ignores_fns_entirely(fares):
    """4.5.1.1: not used for non-discounted adult fares."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222", ns_disc=1)],
        fare_records=[fare(1, "SDS", 1000)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        fns=[fns(dest="2222", flag="X")],
    )
    assert prices(connection, directory) == {"BBB": 1000}


def test_an_explicit_record_beats_a_wildcard_one(fares):
    """"except where a record exists for an explicit destination"."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222", ns_disc=1)],
        fare_records=[fare(1, "SDS", 1000)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
        fns=[fns(railcard="YNG", flag="X"),
             fns(dest="2222", railcard="YNG", addon=120)],
    )
    assert discounted(connection, directory) == {"BBB": 785}


def test_a_record_for_another_railcard_does_not_apply(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222", ns_disc=1)],
        fare_records=[fare(1, "SDS", 1000)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
        fns=[fns(dest="2222", railcard="SRN", flag="X")],
    )
    assert discounted(connection, directory) == {"BBB": 665}


def test_a_band_naming_no_station_still_bites(fares):
    """RSPS5045 4.19.8 field 10: three spaces means the band is not station
    specific, so it applies at whichever end its arrive/depart marker names.

    Requiring a station dropped 2,010 bands, restriction 3V among them — "VALID
    ON ANY TRAIN 0930 OR LATER M-F" — so York offered its Off-Peak Single on the
    09:06, which no retailer will sell. `--depart` moved one destination from
    York before this; it moves 2,087 after.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "CDS", 990, restriction="3V")],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("CDS", "OFF-PEAK S")],
        bands=[("3V", 180, 569, "D", None)],  # 03:00-09:29, no station named
    )
    cheapest = lambda **kw: {
        r[0]: (r[1], r[3]) for r in cheapest_from(connection, directory, "AAA", TUESDAY, **kw)
    }["BBB"]

    assert cheapest(depart_minutes=9 * 60 + 6) == ("SDS", 1510)   # 09:06, barred
    assert cheapest(depart_minutes=9 * 60 + 30) == ("CDS", 990)   # 09:30, valid


# --- TOC conditions (RGK entry types T and X) --------------------------------
#
# The gap hand-checking found three times in five journeys: York to Newcastle
# quoted £28.20 on route 00085 "TPE ONLY" against an LNER train, and York to
# Manchester and Leeds quoted "NORTHERN ONLY" fares against a TransPennine one.


def test_a_toc_only_fare_is_refused_on_another_operator(fares):
    """`T:TP` — at least one leg must be TransPennine."""
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00085"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 2820), fare(2, "SDS", 4490)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        toc_rules=[("00085", "T", "TP")],
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }["BBB"]
    path = {"BBB": ["AAA", "BBB"]}

    assert cheapest(paths=path, operators={"BBB": {"TP"}}) == 2820
    assert cheapest(paths=path, operators={"BBB": {"GR"}}) == 4490


def test_a_barred_operator_refuses_the_fare(fares):
    """`X:GR` — no leg may be LNER."""
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00085"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 2820), fare(2, "SDS", 4490)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        toc_rules=[("00085", "X", "GR")],
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }["BBB"]
    path = {"BBB": ["AAA", "BBB"]}

    assert cheapest(paths=path, operators={"BBB": {"TP"}}) == 2820
    assert cheapest(paths=path, operators={"BBB": {"GR"}}) == 4490


def test_one_barred_leg_is_enough_to_refuse(fares):
    """`X` bars the operator from *any* leg, not just the first."""
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00085"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 2820), fare(2, "SDS", 4490)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        toc_rules=[("00085", "X", "GR")],
    )
    cheapest = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL,
        paths={"BBB": ["AAA", "CCC", "BBB"]}, operators={"BBB": {"TP", "GR"}})}
    assert cheapest["BBB"] == 4490


def test_without_operators_a_toc_condition_gives_no_verdict(fares):
    """Not knowing who runs the train is not a reason to refuse the fare.

    Without this the check silently refused every fare on a TOC-restricted
    route as soon as paths were supplied, which is how it was caught.
    """
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00085"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 2820), fare(2, "SDS", 4490)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        toc_rules=[("00085", "T", "TP")],
    )
    cheapest = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, paths={"BBB": ["AAA", "BBB"]})}
    assert cheapest["BBB"] == 2820


def test_a_route_requiring_a_mode_is_refused_without_it(fares):
    """Route 00002 requires an Underground leg. Mode 4 is the Underground."""
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00002"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 900), fare(2, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        toc_rules=[("00002", "L", "4")],
    )
    cheapest = lambda modes: {
        r[0]: r[3] for r in cheapest_from(
            connection, directory, "AAA", TRAVEL,
            paths={"BBB": ["AAA", "BBB"]}, modes={"BBB": modes})
    }["BBB"]

    assert cheapest({"0", "4"}) == 900    # a train and a tube hop
    assert cheapest({"0"}) == 1500        # trains only


def test_a_route_barring_a_mode_is_refused_when_it_is_used(fares):
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00002"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 900), fare(2, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        toc_rules=[("00002", "N", "4")],
    )
    cheapest = lambda modes: {
        r[0]: r[3] for r in cheapest_from(
            connection, directory, "AAA", TRAVEL,
            paths={"BBB": ["AAA", "BBB"]}, modes={"BBB": modes})
    }["BBB"]

    assert cheapest({"0"}) == 900
    assert cheapest({"0", "4"}) == 1500


def test_without_modes_a_mode_condition_gives_no_verdict(fares):
    """Same rule as the TOC conditions: not knowing is not refusing."""
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00002"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 900), fare(2, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        toc_rules=[("00002", "L", "4")],
    )
    cheapest = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, paths={"BBB": ["AAA", "BBB"]})}
    assert cheapest["BBB"] == 900


# --- break of journey --------------------------------------------------------


def test_a_fare_barring_a_break_is_not_offered_for_a_broken_journey(fares):
    """TVL field 12. 41 of the 104 validity codes say no, covering 651 of the
    walk-up ticket types — so this is not a corner case."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 900), fare(1, "SDS", 1510)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S"), ticket("SDS", "ANYTIME DAY S")],
        validities=[("01", False)],
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }

    assert cheapest() == {"BBB": 900}
    assert cheapest(break_of_journey=True) == {}


def test_a_fare_permitting_a_break_still_is(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 900)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S")],
        validities=[("01", True)],
    )
    assert {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, break_of_journey=True)} == {"BBB": 900}


def test_a_validity_the_feed_says_nothing_about_is_not_assumed_permissive(fares):
    """Silence is not permission when the question is whether you may stop off."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 900)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S")],
        validities=[],  # no TVL record for validity code 01
    )
    assert cheapest_from(connection, directory, "AAA", TRAVEL,
                         break_of_journey=True) == []


# --- FRR rounding ------------------------------------------------------------


def test_the_discount_rounds_down_to_the_band(fares):
    """£20.10 less 33.4% is £13.3866. Down to 5p is £13.35, which is what a
    retailer quotes; nearest would be £13.40 and up £13.40."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 2010)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
    )
    assert {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="YNG")} == {"BBB": 1335}


def test_a_banded_rule_picks_its_band_from_the_discounted_fare(fares):
    """RSPS5045 4.18.1.1: the *discounted* fare selects the band. Rule 01 is 5p
    throughout so the choice is invisible in the real feed; a banded rule makes
    it visible, and a large fare is the only thing that would tell them apart.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 15050)],
        tickets=[ticket("CDS", "SUPER OFFPEAK S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
        # 5p up to £99.99, then £1 — the shape of FRR rule Z0.
        rounding=((9999, 5), (99999997, 100), (99999999, 1)),
    )
    # £150.50 less 33.4% is £100.233, which lands in the £1 band, not the 5p one.
    assert {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="YNG")} == {"BBB": 10000}


def test_the_same_fare_under_the_flat_five_pence_rule(fares):
    """The contrast, and the prediction worth checking against a retailer."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 15050)],
        tickets=[ticket("CDS", "SUPER OFFPEAK S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
    )
    assert {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="YNG")} == {"BBB": 10020}


# --- railcard geography ------------------------------------------------------
#
# RSPS5045 4.15.2 field 8: an area-restricted railcard "can only be used in
# areas denoted by the Railcard Geography held in the Locations file". 87
# railcards are so flagged. The Network Railcard covers 1,029 stations and the
# Annual Gold Card 1,206 — genuinely different areas, and Birmingham is in the
# second but not the first.


def area_railcard(code="NEW"):
    return railcard(code, "NETWORK RAILCARD", "003", per_mille=334, area=True)


def test_an_area_railcard_does_not_discount_outside_its_area(fares):
    """York to Leeds with a Network Railcard was £13.00 and should be £15.10;
    only the card's own minimum fare kept the number from looking absurd."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 1510)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S")],
        railcards=[area_railcard()],
        stations=[("AAA", "1111", "1111"), ("BBB", "2222", "2222"),
                  ("CCC", "3333", "3333")],
        geography=[("NEW", "CCC")],  # a real area, just not this journey's
    )
    assert {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="NEW")} == {"BBB": 1510}


def test_it_does_discount_inside_it(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 1510)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S")],
        railcards=[area_railcard()],
        geography=[("NEW", "AAA"), ("NEW", "BBB")],
    )
    assert {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="NEW")} == {"BBB": 1005}


def test_both_ends_must_be_inside(fares):
    """Half a journey in the area is not a journey in the area."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 1510)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S")],
        railcards=[area_railcard()],
        geography=[("NEW", "AAA")],  # origin only
    )
    assert {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="NEW")} == {"BBB": 1510}


def test_a_railcard_with_no_area_flag_is_untouched(fares):
    """The 16-25 Railcard is valid everywhere, and has no geography at all."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 1510)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S")],
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
    )
    assert {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="YNG")} == {"BBB": 1005}


def test_a_flagged_railcard_with_no_geography_is_left_alone(fares):
    """Not knowing the area is not knowing it is empty — the same rule the TOC
    and mode conditions follow."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 1510)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S")],
        railcards=[area_railcard()],
        geography=[],
    )
    assert {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="NEW")} == {"BBB": 1005}


# --- railcard bans and the railcard's own restriction -------------------------


def test_a_railcard_banned_on_a_route_does_not_discount_it(fares):
    """The Network Railcard carries 103 route bans, GATWICK EXP ONLY among
    them — which is how "that operator does not accept it" is expressed."""
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00724")],
        fare_records=[fare(1, "CDS", 1510)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S")],
        railcards=[railcard("NEW", "NETWORK RAILCARD", "003", per_mille=334)],
        railcard_rules=[("NEW", None, "00724", None)],
    )
    assert {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="NEW")} == {"BBB": 1510}


def test_a_railcard_banned_on_a_ticket_does_not_discount_it(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 1510)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S")],
        railcards=[railcard("NEW", "NETWORK RAILCARD", "003", per_mille=334)],
        railcard_rules=[("NEW", "CDS", None, None)],
    )
    assert {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TRAVEL, railcard="NEW")} == {"BBB": 1510}


def test_the_railcards_own_restriction_bars_it_in_the_peak(fares):
    """Restriction RN bars the Network Railcard 04:30-09:59 Mon-Fri, on top of
    whatever the fare's own restriction says."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 1510)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S")],
        railcards=[railcard("NEW", "NETWORK RAILCARD", "003", per_mille=334)],
        railcard_rules=[("NEW", None, None, "RN")],
        bands=[("RN", 270, 599, "D", None)],
    )
    cheapest = lambda dep: {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TUESDAY, railcard="NEW",
        depart_minutes=dep)}["BBB"]

    assert cheapest(8 * 60 + 30) == 1510   # inside the ban
    assert cheapest(11 * 60) == 1005       # after it


def test_a_minimum_fare_band_is_not_a_bar(fares):
    """RSPS5045 4.19.8 field 13. Only 19 of 33,216 current bands set it, and
    one is the Network Railcard's own — spanning the whole day, so reading it
    as a bar withdraws the railcard entirely."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 1510)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S")],
        railcards=[railcard("NEW", "NETWORK RAILCARD", "003", per_mille=334)],
        railcard_rules=[("NEW", None, None, "RN")],
        bands=[("RN", 1, 1439, "D", None, True)],
    )
    assert {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TUESDAY, railcard="NEW",
        depart_minutes=11 * 60)} == {"BBB": 1005}


# --- returns -----------------------------------------------------------------


def test_a_return_can_win_on_price_and_the_caller_is_told_which(fares):
    """`cheapest_from` has always priced returns alongside singles and a return
    sometimes wins — 4 of the 2,760 cheapest fares from York are returns, and
    James Cook University Hospital's £23.30 Off-Peak Day Return beats its
    £35.50 single. The price alone does not say you bought a round trip, so the
    ticket type comes back with it."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDR", 2330), fare(1, "CDS", 3550)],
        tickets=[ticket("CDR", "OFF-PEAK DAY R", kind="R", validity="06"),
                 ticket("CDS", "OFF-PEAK DAY S", validity="01")],
        validities=[("01", True), ("06", True, {"ret_days": 1})],
    )
    won = {row[0]: row for row in cheapest_from(connection, directory, "AAA", TRAVEL)}

    assert won["BBB"][3] == 2330
    assert won["BBB"][6] == "R"   # a return, not a cheap single


def test_a_return_date_withdraws_a_return_that_cannot_come_back_then(fares):
    """A Day Return is valid on the date shown, so asking to come back a week
    later has to fall through to the single — at a higher price. Singles are
    kept rather than filtered: a single is not made invalid by the question."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDR", 2330), fare(1, "CDS", 3550)],
        tickets=[ticket("CDR", "OFF-PEAK DAY R", kind="R", validity="06"),
                 ticket("CDS", "OFF-PEAK DAY S", validity="01")],
        validities=[("01", True), ("06", True, {"ret_days": 1})],
    )
    cheapest = lambda back: {
        row[0]: row for row in cheapest_from(
            connection, directory, "AAA", TRAVEL, return_on=back)}["BBB"]

    assert cheapest(TRAVEL)[3] == 2330                       # same day: fine
    assert cheapest(TRAVEL + dt.timedelta(days=7))[3] == 3550  # falls to the single
    assert cheapest(TRAVEL + dt.timedelta(days=7))[6] == "S"


def test_fares_between_states_the_return_window_of_every_return(fares):
    """The gap this closes: a return price used to appear with nothing saying
    it was a return or by when you had to travel back."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SVR", 4140), fare(1, "CDS", 3550)],
        tickets=[ticket("SVR", "OFF-PEAK R", kind="R", validity="13"),
                 ticket("CDS", "OFF-PEAK DAY S", validity="01")],
        validities=[("01", True), ("13", True, {"ret_months": 1})],
    )
    rows = {r["ticket_code"]: r for r in
            fares_between(connection, directory, "AAA", "BBB", TRAVEL)}

    assert rows["CDS"]["return_window"] is None
    window = rows["SVR"]["return_window"]
    assert window.kind == "period"
    # A calendar month, whose exact arithmetic is pinned in test_returns.py.
    assert window.earliest == TRAVEL
    assert 28 <= (window.latest - TRAVEL).days <= 31


# --- the return leg ----------------------------------------------------------


def test_a_return_leg_band_bars_a_fare_on_the_way_home(fares):
    """13,803 of the bands in force on a weekday carry out_ret = 'R', and none
    was ever evaluated because nothing routed the journey back."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SVR", 2010, restriction="R1")],
        tickets=[ticket("SVR", "OFF-PEAK R", kind="R", validity="13")],
        validities=[("01", True), ("13", True, {"ret_months": 1})],
        # Not valid leaving the destination before 09:30 on the way home.
        bands=[("R1", 270, 569, "D", "BBB", False, "R")],
    )
    priced = lambda back_depart: fare_options(
        connection, directory, "AAA", TUESDAY, depart_minutes=11 * 60,
        return_on=TUESDAY + dt.timedelta(days=2),
        return_depart_minutes=back_depart, return_arrival_minutes=back_depart + 60)

    assert not [r for r in priced(8 * 60) if r[0] == "BBB"]     # inside the band
    assert [r for r in priced(11 * 60) if r[0] == "BBB"]        # after it


def test_a_return_departure_band_bites_at_the_destination_not_the_origin(fares):
    """The two legs run opposite ways and the band follows the journey.

    On the way home a departure band bites where the journey home *starts* —
    the outward destination — and an arrival band bites back at the origin.
    Swapping them applies London's morning arrival bans to a train leaving
    London, which reads entirely plausible and is backwards. A band naming AAA
    must therefore do nothing to a return departure, and one naming BBB must.
    """
    def world(location):
        return fares(
            flows=[flow(1, "1111", "2222")],
            fare_records=[fare(1, "SVR", 2010, restriction="R1")],
            tickets=[ticket("SVR", "OFF-PEAK R", kind="R", validity="13")],
            validities=[("01", True), ("13", True, {"ret_months": 1})],
            bands=[("R1", 270, 569, "D", location, False, "R")],
        )

    def survives(location):
        connection, directory = world(location)
        return bool([
            r for r in fare_options(
                connection, directory, "AAA", TUESDAY, depart_minutes=11 * 60,
                return_on=TUESDAY + dt.timedelta(days=2),
                return_depart_minutes=8 * 60, return_arrival_minutes=9 * 60)
            if r[0] == "BBB"])

    assert not survives("BBB")   # where the journey home departs
    assert survives("AAA")       # where it ends: a departure band cannot bite


def test_a_return_arrival_band_bites_back_at_the_origin(fares):
    """The mirror of the above, and the half that catches a swap in either
    direction."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SVR", 2010, restriction="R1")],
        tickets=[ticket("SVR", "OFF-PEAK R", kind="R", validity="13")],
        validities=[("01", True), ("13", True, {"ret_months": 1})],
        # Not valid arriving back at AAA between 16:00 and 19:00.
        bands=[("R1", 960, 1140, "A", "AAA", False, "R")],
    )
    priced = lambda back_arrive: [
        r for r in fare_options(
            connection, directory, "AAA", TUESDAY, depart_minutes=11 * 60,
            return_on=TUESDAY + dt.timedelta(days=2),
            return_depart_minutes=11 * 60, return_arrival_minutes=back_arrive)
        if r[0] == "BBB"]

    assert not priced(17 * 60)   # arrives back inside the band
    assert priced(20 * 60)


def test_return_bands_are_ignored_when_the_way_back_is_not_routed(fares):
    """A sweep prices thousands of destinations and cannot route a return leg
    for each. Not knowing the time is not a reason to refuse the fare — the
    same guard the TOC conditions needed."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SVR", 2010, restriction="R1")],
        tickets=[ticket("SVR", "OFF-PEAK R", kind="R", validity="13")],
        validities=[("01", True), ("13", True, {"ret_months": 1})],
        bands=[("R1", 0, 1439, "D", "BBB", False, "R")],   # all day, every day
    )
    priced = [r for r in fare_options(connection, directory, "AAA", TUESDAY,
                                      depart_minutes=11 * 60) if r[0] == "BBB"]

    assert priced and priced[0][3] == 2010


def test_an_outward_band_still_only_looks_at_the_outward_journey(fares):
    """The regression that would follow from folding the two legs together."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SVR", 2010, restriction="R1")],
        tickets=[ticket("SVR", "OFF-PEAK R", kind="R", validity="13")],
        validities=[("01", True), ("13", True, {"ret_months": 1})],
        bands=[("R1", 270, 569, "D", "AAA", False, "O")],
    )
    priced = lambda out_depart: [
        r for r in fare_options(
            connection, directory, "AAA", TUESDAY, depart_minutes=out_depart,
            return_on=TUESDAY + dt.timedelta(days=2),
            return_depart_minutes=8 * 60, return_arrival_minutes=9 * 60)
        if r[0] == "BBB"]

    assert not priced(8 * 60)    # outward inside the band
    # The return leg is inside the same clock window, and must not matter.
    assert priced(11 * 60)


# --- a restriction that bars changing trains ---------------------------------


def change_barred_world(fares, *, change_allowed):
    """One fare on a restriction that does or does not permit a change."""
    return fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "QFR", 1090, restriction="ME"),
                      fare(1, "SVS", 4900)],
        tickets=[ticket("QFR", "LUMOFIXED", validity="01"),
                 ticket("SVS", "ANYTIME S", validity="01")],
        validities=[("01", True)],
        headers=[("ME", "LUMO STIRLING & CONNECTIONS", change_allowed)],
    )


def test_a_restriction_barring_a_change_withdraws_the_fare(fares):
    """RSPS5045 4.19.3 field 10, and the bands cannot express it — it is a
    property of the whole restriction. From Euston this moves 199 destinations,
    median +£67.10: `QFR` LUMOFIXED at £10.90 was being quoted to Nantwich, on a
    journey changing at Crewe, for a fare valid only on Lumo services.
    """
    connection, directory = change_barred_world(fares, change_allowed=False)
    priced = lambda changes: {
        r[0]: r for r in cheapest_from(connection, directory, "AAA", TUESDAY,
                                       depart_minutes=9 * 60, changes=changes)}

    assert priced({"BBB": 0})["BBB"][3] == 1090   # direct, so the fare stands
    assert priced({"BBB": 1})["BBB"][3] == 4900   # a change, so it does not


def test_a_restriction_that_allows_changes_is_untouched(fares):
    """803 of the 839 current restrictions allow one, so the common case must
    not move."""
    connection, directory = change_barred_world(fares, change_allowed=True)
    rows = {r[0]: r for r in cheapest_from(connection, directory, "AAA", TUESDAY,
                                           depart_minutes=9 * 60,
                                           changes={"BBB": 2})}

    assert rows["BBB"][3] == 1090


def test_not_routing_the_journey_is_not_a_refusal(fares):
    """The same guard the TOC and return-leg conditions needed: with no journey
    supplied the restriction gives no verdict rather than withdrawing the fare.
    Without it, every caller that prices without routing loses these fares."""
    connection, directory = change_barred_world(fares, change_allowed=False)
    rows = {r[0]: r for r in cheapest_from(connection, directory, "AAA", TUESDAY,
                                           depart_minutes=9 * 60)}

    assert rows["BBB"][3] == 1090
