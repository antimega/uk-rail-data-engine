"""Walk-up fare derivation.

Two things are being pinned down. First the lookup itself: a fare hangs off a
*flow* between two codes, and a station is represented by its own NLC, its
group's NLC, and every cluster it belongs to. Second the filtering, because the
feed ships a great deal that is not an adult walk-up fare - Advance products
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
    # The operator that *set* the fare, which is not the same question as whose
    # trains it is valid on - that is the route's job. Null in most fixtures,
    # which is what a feed naming none looks like.
    ("toc", pa.string()),
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
         ns_disc=0, route="00000", toc=None):
    return {"flow_id": flow_id, "origin_code": origin, "destination_code": destination,
            "route_code": route, "direction": direction, "ns_disc_ind": ns_disc,
            "toc": toc, "start_date": start, "end_date": end}


def nfo(origin, destination, code, pence, *, route="00000", railcard=None):
    """A non-derivable override, which states a price outright and names no
    operator - there is no `toc` field on the record at all."""
    return {"origin_code": origin, "destination_code": destination,
            "route_code": route, "railcard_code": railcard, "ticket_code": code,
            "adult_fare": pence, "restriction_code": None,
            "composite_indicator": "Y", "suppress_mkr": False,
            "start_date": PAST, "end_date": FOREVER}


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
               railcard_rules=(), counties=None, tocs=(), calendars=()):
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

        # `LOC` carries the county code, which is a legitimate flow endpoint -
        # RSPS5045 4.1.2 says "NLC code, county code, zone code" - and the only
        # way the Isle of Man's fare bands can be reached.
        # A station is (crs, nlc, fare_group), or (crs, nlc, fare_group,
        # description) where the description matters - which is PlusBus, whose
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
        # PlusBus zones - see `reference.py`. Mirror that here so a zone reaches
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
        _write_descriptions(directory, validities, headers, tocs)
        _write_routeing(connection, rgk_rules, london_marker, london_terminals,
                        toc_rules)
        _write_restrictions(directory, bands, calendars)
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

    The chain is three deep - station → its county code → the cluster holding
    that county → the flow - and expanding only NLCs missed it entirely. Douglas
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
    choice was not even stable between two runs on one database - building the
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


def test_one_ticket_under_two_restrictions_also_breaks_the_tie_repeatably(fares):
    """**The same bug came back through a column added after the fix above.**

    That tie-break is `(smart, ticket_code, route_code)`, which was total when
    it was written and stopped being so the moment `restriction_code` was
    selected beside it: one ticket, one route, one price, two restrictions, and
    `min_by` with two equal keys takes either.

    **It is the origin expansion showing through**, which is the same mechanism
    this file already records for one fare listed twice. An origin is several
    codes - its own NLC, its cluster, its group - and two of them can match
    different flows selling the same ticket at the same price on the same
    route. Real: `CDR OFF-PEAK DAY R` to Congleton is £13.20 on route `00325`
    from `Q126` under `B3` and from `Q235` under `B1`, and three builds of one
    fare group gave three different payloads - every price identical, the
    restriction beside them flipping.

    A tie-break is total against the columns that existed when it was written,
    and adding a column is what makes it partial again.

    **This asserts the rule rather than the behaviour, and that is a
    concession worth stating.** The shape needs one ticket sold on *two* flows,
    and this fixture cannot express it - two flows carrying the same ticket
    code return no rows at all, with or without restrictions, which is a limit
    of the tiny world here and nothing to do with the fix. Verified on the real
    feed instead, three ways: the Congleton rows above exist; `fare_options`
    returned a different row order on three consecutive runs before this and
    the same order on three after; and a fare group built twice went from
    differing to byte-identical.
    """
    from pathlib import Path

    from rail.model import fares as fares_module

    source = Path(fares_module.__file__).read_text(encoding="utf-8")

    # Every tie-break that picks a column off a tied row carries the
    # restriction, so none of them can be decided by which row arrives first.
    picked = source.count("(description like 'SMART %', ticket_code, route_code,")
    assert picked >= 7, f"only {picked} min_by tie-breaks name the restriction"
    assert "coalesce(restriction_code, '')" in source
    # And the partial form is gone rather than merely joined by a total one.
    assert "(description like 'SMART %', ticket_code, route_code)" not in source


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
    ticket. The record still overrides, so the flow price goes with it - but
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

    Reading it the other way round - as "this is an aggregate, drop it" - would
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


def test_advance_only_returns_advances_instead_of_walk_ups(fares):
    """Three states, not two: walk-up only, both, or Advance alone.

    `include_advance` *adds* Advance prices to the walk-ups, which answers "what
    is the cheapest fare". A caller asking "what is the cheapest Advance" needs
    the walk-ups gone rather than outranked - otherwise every destination where
    a walk-up happens to be cheaper reports the walk-up, and the answer silently
    stops being about Advances at all.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "DG0", 900)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("DG0", "ADVANCE")],
        advance=["DG0"],
    )

    walk_up = fare_options(connection, directory, "AAA", TRAVEL)
    both = fare_options(connection, directory, "AAA", TRAVEL, include_advance=True)
    only = fare_options(connection, directory, "AAA", TRAVEL, advance_only=True)

    assert [(r[1], r[3]) for r in walk_up] == [("SDS", 1510)]
    assert [(r[1], r[3]) for r in both] == [("DG0", 900), ("SDS", 1510)]
    assert [(r[1], r[3]) for r in only] == [("DG0", 900)]
    assert all(row[4] for row in only), "advance_only returned a walk-up fare"


def test_advance_only_shows_an_advance_a_walk_up_would_have_masked(fares):
    """One row per *distinct price*, so a walk-up at the same price as an Advance
    absorbs it - the cheapest-named ticket stands for the group.

    That is right when the question is "what does this cost" and wrong when it
    is "what is the cheapest Advance": the Advance exists, at that price, and
    reporting nothing would be a lie of omission. Four real destinations from
    York are in exactly this position.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "CDS", 900), fare(1, "DG0", 900)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S"), ticket("DG0", "ADVANCE")],
        advance=["DG0"],
    )

    both = fare_options(connection, directory, "AAA", TRAVEL, include_advance=True)
    only = fare_options(connection, directory, "AAA", TRAVEL, advance_only=True)

    # The walk-up wins the tie and the Advance is not reported at all...
    assert [(r[1], r[3], r[4]) for r in both] == [("CDS", 900, False)]
    # ...but it is there, and asking for Advances finds it.
    assert [(r[1], r[3], r[4]) for r in only] == [("DG0", 900, True)]


def test_a_plusbus_zone_is_never_a_destination(fares):
    """A PlusBus zone is an add-on to a journey, not a place you travel to.

    They used to carry no CRS, which is what made this safe without anyone
    writing it down. The feed generation valid from 2026-06-30 gave four of them
    one - `QAB` BATH+BUS and friends - and Bristol Temple Meads gained a £5.40
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
    """RSPS5045 4.6.2 field 23. `AO2 AIRPORT ADV STD` names no train anywhere -
    not in its description, its validity or its restriction - so the reservation
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
    ordinary fare of the same name - `8AB` is "ANYTIME DAY S" at £5.10 - so no
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


def _write_restrictions(directory, bands, calendars=()):
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
            # min_fare_flag, out_ret - 'O' for the outward leg, 'R' for the
            # journey home - and the operators the band is qualified to.
            # Sequences are distinct so each band keeps its own date window
            # rather than collapsing into one.
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
    # RSPS5045 4.19.10 field 7. A band with no TT rows applies to every
    # operator's trains, which is all but 2,565 of the real ones - so the usual
    # fixture writes an empty table and behaves exactly as it did before.
    pq.write_table(
        pa.Table.from_pylist(
            [{"cf_mkr": "C", "restriction_code": c, "sequence_no": f"{i:04d}",
              "out_ret": rest[1] if len(rest) > 1 else "O", "toc_code": toc}
             for i, (c, _f, _t, _sense, _loc, *rest) in enumerate(bands, start=1)
             for toc in ((len(rest) > 2 and rest[2]) or ())],
            schema=pa.schema([
                ("cf_mkr", pa.string()), ("restriction_code", pa.string()),
                ("sequence_no", pa.string()), ("out_ret", pa.string()),
                ("toc_code", pa.string())])),
        directory / "restriction_time_toc.parquet")
    _write_calendars(directory, calendars)


def _write_calendars(directory, calendars):
    """CA, the ticket calendars. A calendar is (ticket, days) or
    (ticket, days, route) or (ticket, days, route, cal_type), where `days` is
    the spec's own seven-character mask starting on Monday - "YYYYYNN" for the
    weekdays.

    `cal_type` defaults to 'I', the only kind this feed ships and the only one
    read: the days the ticket is *not* available.
    """
    pq.write_table(
        pa.Table.from_pylist(
            [{"cf_mkr": "C", "ticket_code": code,
              "cal_type": rest[1] if len(rest) > 1 else "I",
              "route_code": rest[0] if rest else None, "country_code": None,
              "date_from": "0101", "date_to": "1231",
              **{d: days[n] == "Y" for n, d in enumerate(DAYS)}}
             for code, days, *rest in calendars],
            schema=pa.schema([
                ("cf_mkr", pa.string()), ("ticket_code", pa.string()),
                ("cal_type", pa.string()), ("route_code", pa.string()),
                ("country_code", pa.string()),
                ("date_from", pa.string()), ("date_to", pa.string()),
                *[(d, pa.bool_()) for d in DAYS]])),
        directory / "restriction_ticket_calendar.parquet")


def test_a_ticket_calendar_withdraws_a_fare_on_the_days_it_names(fares):
    """RSPS5045 4.19.20: an `I` calendar names the days a ticket is *not*
    available. `WKF Weekend Return` carries one covering Monday to Friday, and
    was the cheapest walk-up on 96 of 21,805 fares priced on a Tuesday.

    Unlike a restriction band this needs no journey - it is a fact about the
    ticket and the date - so it applies whether or not the caller has routed
    anything.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "WKF", 990)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("WKF", "Weekend Return")],
        calendars=[("WKF", "YYYYYNN")],
    )
    cheapest = lambda day: {
        r[0]: (r[1], r[3]) for r in cheapest_from(connection, directory, "AAA", day)
    }["BBB"]

    assert cheapest(TUESDAY) == ("SDS", 1510)
    assert cheapest(SATURDAY) == ("WKF", 990)


def test_reading_an_I_calendar_as_the_days_it_is_available_inverts_it(fares):
    """The trap this rule exists to avoid, pinned as a test.

    `I` looks like "included" and means the opposite. `SUA Sunday Single`
    carries one record covering Monday to Saturday: read correctly it is a
    Sunday ticket, and read as availability it is a Sunday ticket that cannot
    be used on a Sunday. Both readings are internally consistent, so only the
    spec - or this test - tells them apart.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "SUA", 990)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("SUA", "Sunday Single")],
        calendars=[("SUA", "YYYYYYN")],
    )
    sunday = dt.date(2026, 8, 2)
    priced = lambda day: sorted(
        (r[1], r[3]) for r in fare_options(connection, directory, "AAA", day)
        if r[0] == "BBB")

    assert priced(sunday) == [("SDS", 1510), ("SUA", 990)]
    assert priced(TUESDAY) == [("SDS", 1510)]


def test_a_calendar_naming_a_route_bars_only_that_route(fares):
    """`SOS ANYTIME S` carries one all-year, all-days record scoped to route
    `00041`. Ignoring the scope withdraws it from every route in the feed - and
    it is the retailer-verified £193.00 King's Cross to Manchester, so that
    error is not a small one.
    """
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00041"),
               routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SOS", 990), fare(2, "SOS", 1930)],
        tickets=[ticket("SOS", "ANYTIME S")],
        calendars=[("SOS", "YYYYYYY", "00041")],
    )
    rows = [(r[3], r[5]) for r in fare_options(connection, directory, "AAA", TUESDAY)
            if r[0] == "BBB"]

    assert rows == [(1930, "00000")]


def test_a_supplement_calendar_is_not_a_bar(fares):
    """`'S'` is a supplement calendar and `'D'` means the ticket is *restricted*
    on those dates - neither says the ticket is unavailable, and this feed
    ships 58 of the first and none of the second. Only `'I'` is read.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "GCW", 990)],
        tickets=[ticket("GCW", "OFF-PEAK DAY S")],
        calendars=[("GCW", "YYYYYYY", None, "S")],
    )
    rows = [(r[1], r[3]) for r in cheapest_from(connection, directory, "AAA", TUESDAY)]

    assert rows == [("GCW", 990)]


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


def test_a_band_bites_where_the_passenger_boards_not_only_at_the_ends(fares):
    """**A station band bites where you board or alight, changes included.**

    RSPS5045 4.19.8 field 10 calls the location "a journey origin/destination
    or via location", and reading that as "the ends only" was wrong. A retailer
    settled it: Stratford to Cardiff boards the Cardiff train at Paddington,
    and `WW` band 0011 bars departures from Paddington before 09:04 - leaving
    Stratford at 08:11 you catch the 08:48 from Paddington and only Anytime
    fares are offered, at 08:41 you catch the 09:18 and the Off-Peak Return is
    back. Woking to Cardiff shows the same at *Reading*, so it is about
    boarding rather than about London terminals.

    What the old reading got right is passing through, and that still holds
    below: `LK` band 0018 bars departing Euston before 10:29 while band 0006
    bars departing Leighton Buzzard before 12:33, and one train cannot satisfy
    both - but that passenger passes Leighton Buzzard without boarding, so the
    band never spoke to them.
    """
    world = dict(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 29080), fare(1, "SSS", 15050, restriction="PB")],
        tickets=[ticket("SDS", "ANYTIME S"), ticket("SSS", "SUPER OFFPEAK S")],
        stations=[("AAA", "1111", "1111"), ("BBB", "2222", "2222"),
                  ("MID", "3333", "3333")],
    )
    # Arrive at MID 10:03, leave on the connection at 11:40 - far enough apart
    # that a band can catch one and not the other.
    passing = {"BBB": [("AAA", 484, 484, False), ("MID", 603, 700, False),
                       ("BBB", 972, 972, False)]}
    changing = {"BBB": [("AAA", 484, 484, False), ("MID", 603, 700, True),
                        ("BBB", 972, 972, False)]}
    ask = lambda **kw: {
        r[0]: (r[1], r[3]) for r in cheapest_from(connection, directory, "AAA",
                                                  TUESDAY, **kw)}["BBB"]
    run = lambda calls: ask(depart_minutes=480, arrivals={"BBB": 972}, calls=calls)

    # Passing through MID says nothing, whichever marker the band carries.
    for marker in ("A", "D", "V"):
        connection, directory = fares(**world, bands=[("PB", 270, 720, marker, "MID")])
        assert run(passing) == ("SSS", 15050), marker

    # Changing at MID: an arrival band is judged on when you got there…
    connection, directory = fares(**world, bands=[("PB", 270, 677, "A", "MID")])
    assert run(changing) == ("SDS", 29080)      # arrived 603, inside
    connection, directory = fares(**world, bands=[("PB", 690, 720, "A", "MID")])
    assert run(changing) == ("SSS", 15050)      # arrived 603, outside

    # …and a departure band on when you left, which is the other number.
    connection, directory = fares(**world, bands=[("PB", 690, 720, "D", "MID")])
    assert run(changing) == ("SDS", 29080)      # left 700, inside
    connection, directory = fares(**world, bands=[("PB", 270, 677, "D", "MID")])
    assert run(changing) == ("SSS", 15050)      # left 700, outside

    # `V` says "changing at" outright and needs no end test.
    connection, directory = fares(**world, bands=[("PB", 270, 677, "V", "MID")])
    assert run(changing) == ("SDS", 29080)


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


def _write_descriptions(directory, validities=(), headers=(), tocs=()):
    """The tables `fares_between` joins out to for its explanations. Empty is
    fine - every join is a left one, so a fare with no route or validity record
    still appears, just without the words."""
    # The operator crossref, which `build_fares_reference` turns into
    # `fare_toc`. Empty in a fixture world: `flow.toc` is null there, so every
    # fare reports no operator, which is exactly what a feed that named none
    # would do.
    pq.write_table(
        pa.Table.from_pylist(
            [{"fare_toc_id": fid, "toc_id": atoc, "fare_toc_name": name}
             for fid, atoc, name in tocs],
            schema=pa.schema([("fare_toc_id", pa.string()), ("toc_id", pa.string()),
                              ("fare_toc_name", pa.string())])),
        directory / "toc_fare.parquet")
    pq.write_table(
        pa.Table.from_pylist(
            [{"toc_id": atoc, "toc_name": name} for _fid, atoc, name in tocs if atoc],
            schema=pa.schema([("toc_id", pa.string()), ("toc_name", pa.string())])),
        directory / "toc.parquet")
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
        # `change_ind` is 4.19.3 field 10 - whether a change of trains is
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
            # (code, description, change_ind) - whether a change of trains is
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
        "create table route_rule (route_code varchar, entry_type varchar, "
        "condition_crs varchar, crs varchar)")
    for rule in rgk_rules:
        # A three-tuple is one station standing for its own condition, which is
        # every case here bar the group tests: `_build_route_rules` sets
        # `condition_crs` to the condition's own CRS and the expansion to its
        # members, and an `A` group is satisfied by any one of them.
        route_code, entry_type, crs = rule[0], rule[1], rule[-1]
        condition_crs = rule[2] if len(rule) == 4 else crs
        connection.execute("insert into route_rule values (?, ?, ?, ?)",
                           [route_code, entry_type, condition_crs, crs])
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
    overprices every off-peak journey on a ticket that has a minimum at all -
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


def toc_qualified_railcard(fares):
    """A railcard barred 00:01-23:59 every day - but only on GR and VT trains.

    The shape of the Annual Gold Card's `RD` and the 16-17 Saver's `R5`. Read
    without RSPS5045 4.19.10 field 7 this is not a peak restriction at all: it
    is the railcard not existing, at every hour of every day of the year.
    """
    return fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        railcards=[railcard("NGC", "ANNUAL GOLD CARD", "003", per_mille=334)],
        railcard_rules=[("NGC", None, None, "RD")],
        bands=[("RD", 1, 1439, "D", None, False, "O", ["GR", "VT"])],
    )


def test_a_toc_qualified_railcard_band_spares_another_operators_train(fares):
    connection, directory = toc_qualified_railcard(fares)
    priced = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TUESDAY, railcard="NGC",
        depart_minutes=660, operators={"BBB": {"SW"}})}

    # £15.00 less 33.4% is £9.99, and the rounding rule takes it down to 5p.
    assert priced["BBB"] == 995


def test_a_toc_qualified_railcard_band_bites_on_a_train_it_names(fares):
    connection, directory = toc_qualified_railcard(fares)
    # Any one of the named operators is enough, exactly as an easement's
    # station list works - this journey used a GR train for one of its legs.
    priced = {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TUESDAY, railcard="NGC",
        depart_minutes=660, operators={"BBB": {"SW", "GR"}})}

    assert priced["BBB"] == 1500  # barred, so no discount


def test_not_knowing_the_operators_keeps_a_railcard_band_applying(fares):
    """The conservative half, and why `rail fares` and unrouted sweeps do not
    move. A bar lifted on a guess sells a ticket that may not be valid.

    This is the opposite guard from the `T` route conditions, and deliberately
    so: there a missing answer must not become a refusal, because the question
    is whether to *withdraw* a fare. Here it is whether to *restore* one.
    """
    connection, directory = toc_qualified_railcard(fares)
    priced = lambda **kw: {r[0]: r[3] for r in cheapest_from(
        connection, directory, "AAA", TUESDAY, railcard="NGC",
        depart_minutes=660, **kw)}["BBB"]

    assert priced() == 1500
    assert priced(operators={}) == 1500
    # Another destination's operators say nothing about this one.
    assert priced(operators={"CCC": {"SW"}}) == 1500


def test_a_toc_qualifier_applies_to_a_fares_own_bands(fares):
    """York to Penzance in miniature - and why this needed the change-station
    fix first.

    Restriction `1L` bars departures 04:30-09:29 on CrossCountry (band 0001)
    *and* arrivals into King's Cross before 11:16 (band 0038, unqualified). The
    via-London journey uses no CrossCountry train, so the qualifier rightly
    lifts 0001 - and if 0038 is skipped for naming a station in the middle,
    nothing is left and £290.80 collapses to a £150.50 no retailer sells.

    A retailer prices both itineraries with each band carrying one: the
    via-London journey barred at the change, the not-via-London one - which is
    CrossCountry throughout - by the qualified band.
    """
    world = dict(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1500), fare(1, "SSS", 990, restriction="1L")],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("SSS", "SUPER OFFPEAK S")],
        stations=[("AAA", "1111", "1111"), ("BBB", "2222", "2222"),
                  ("MID", "3333", "3333")],
    )
    # Changes at MID at 10:03, on nobody's CrossCountry train.
    via_mid = {"BBB": [("AAA", 484, 484, False), ("MID", 603, 620, True),
                       ("BBB", 972, 972, False)]}
    ask = lambda **kw: {r[0]: (r[1], r[3]) for r in cheapest_from(
        connection, directory, "AAA", TUESDAY, depart_minutes=450,
        arrivals={"BBB": 972}, **kw)}["BBB"]

    # Both bands, as the real restriction carries them.
    connection, directory = fares(**world, bands=[
        ("1L", 270, 569, "D", None, False, "O", ["XC"]),
        ("1L", 270, 676, "A", "MID", False, "O", None)])
    assert ask(operators={"BBB": {"GR", "GW"}}, calls=via_mid) == ("SDS", 1500)

    # The qualified band alone: it lifts, because no CrossCountry train is used.
    connection, directory = fares(**world, bands=[
        ("1L", 270, 569, "D", None, False, "O", ["XC"])])
    assert ask(operators={"BBB": {"GR", "GW"}}, calls=via_mid) == ("SSS", 990)

    # …and bites on the itinerary that is CrossCountry throughout.
    assert ask(operators={"BBB": {"XC"}}, calls={}) == ("SDS", 1500)


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

    Most carnets announce themselves - CARNET, FLXIPASS, DAYSAVE - and
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
    by name whether or not the file has been fetched - see the test below.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510), fare(1, "MFX", 900)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("MFX", "Multiflex")],
    )
    assert prices(connection, directory) == {"BBB": 900}


def test_a_carnet_that_names_itself_needs_no_supplementary_file(fares):
    """Euston was quoting `CO5 CARNET OFFPK 5` - five journeys - to 13 stations.

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
    """"SALE ADVANCE" is 50p on every flow - a placeholder, not a price."""
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
# in one direction - quoting a price for a journey it is not valid on.


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


def test_an_all_of_condition_on_a_group_takes_any_one_member(fares):
    """**`A` is all-of over *conditions*, not over rows.**

    A routeing group expands to one row per member, so a flat all-of demands the
    journey call at every station in the group. Route 00312 `VIA MANCHESTER` is
    a single `A MAN (group)` condition covering Piccadilly, Victoria, Oxford
    Road, Deansgate and Salford Central; 00311 `VIA LIVERPOOL` covers eight. No
    journey calls at all of them, so every fare on those 30 routes was withdrawn
    from every itinerary - and a retailer sells Llanelli to Huddersfield at
    £122.40 on 00312 where we quoted £204.50.

    Both halves are asserted, because only the pair tells the fix apart from
    deleting the rule: any one member satisfies the group, **and** a second
    condition still has to be met.
    """
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00007"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 900), fare(2, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        # One condition on a two-member group, and one standing alone. The
        # four-tuple is what `_build_route_rules` writes: the condition's own
        # CRS, then the member it expanded to.
        rgk_rules=[("00007", "A", "CCC", "CCC"), ("00007", "A", "CCC", "DDD"),
                   ("00007", "A", "EEE", "EEE")],
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }["BBB"]

    # Either member satisfies the group, with the separate condition also met.
    assert cheapest(paths={"BBB": ["AAA", "CCC", "EEE", "BBB"]}) == 900
    assert cheapest(paths={"BBB": ["AAA", "DDD", "EEE", "BBB"]}) == 900
    # The group is met and the second condition is not.
    assert cheapest(paths={"BBB": ["AAA", "CCC", "DDD", "BBB"]}) == 1500
    # The second is met and the group is not.
    assert cheapest(paths={"BBB": ["AAA", "EEE", "BBB"]}) == 1500


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
    from Liverpool - every Avanti West Coast origin - quoting £26.50 to
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
    """The same shape as a concession - a condition the passenger must meet,
    written as a ticket type rather than as a discount - so nothing structural
    sees it. `TRQ TrainLinkC16-18` was quoting 75p from Headbolt Lane to
    Skelmersdale Bus Link, on a single flow where the flat-rate test cannot
    judge it because a modal share over one flow is trivially 1.0. The adult
    `TRP TrainLink C` on the same flow is £1.50 - exactly double, which is what
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


def test_a_negotiated_scheme_is_not_a_fare_the_public_can_buy(fares):
    """Corporate, business and privilege rates are the same argument as a
    concession: a condition on *who you are*, written as a ticket type rather
    than as a discount, so nothing structural sees it - min and max passengers
    are both 1 and the price varies with distance.

    `CSF BUSINESS SINGLE` was £39.10 Manchester to Sheffield against a public
    £78.20, and `FTS FCCTFL_PRIV` is the industry's own word for a staff rate,
    which `%STAFF%` cannot see. The privilege one carries no fare in the live
    feed and so moves no price; it is excluded because `is_walk_up` should mean
    what it says whether or not a wrong answer happens to follow.
    """
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00000")],
        fare_records=[fare(1, "CSF", 3910), fare(1, "FTS", 500),
                      fare(1, "SDS", 7820)],
        tickets=[ticket("CSF", "BUSINESS SINGLE"), ticket("FTS", "FCCTFL_PRIV"),
                 ticket("SDS", "ANYTIME DAY S")],
    )
    rows = cheapest_from(connection, directory, "AAA", TRAVEL)

    assert [(r[1], r[3]) for r in rows] == [("SDS", 7820)]
    assert dict(connection.execute(
        "select ticket_code, reason from fare_reject"
        " where ticket_code in ('CSF', 'FTS')"
    ).fetchall()) == {
        "CSF": "corporate scheme, not sold to the public",
        "FTS": "privilege rate, not sold to the public",
    }


def test_one_fare_reached_by_two_codes_is_listed_once(fares):
    """A station is named by its own NLC, its fare group and every cluster
    holding it, and a flow may exist under more than one. Birmingham New Street
    is reached from Euston as `1127` and as cluster `T120`, both carrying the
    same ticket on the same route at the same price - one fare, printed twice.

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

    Requiring a station dropped 2,010 bands, restriction 3V among them - "VALID
    ON ANY TRAIN 0930 OR LATER M-F" - so York offered its Off-Peak Single on the
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
    """`T:TP` - at least one leg must be TransPennine."""
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
    """`X:GR` - no leg may be LNER."""
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


def test_operators_without_a_path_do_not_withdraw_a_barred_fare(fares):
    """`--check-routes` is the flag, and supplying operators is not it.

    The `X` clause used to be inert only because `journey_operator` was empty,
    which is a fact about which callers pass `operators=` rather than a rule.
    Operators are evidence about the journey rather than a policy choice - the
    same reasoning under which `changes` and `calls` are passed unconditionally
    - so somebody will eventually pass them here too, and a bare `rail
    reachable` must not start withdrawing fares when they do.
    """
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00085"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 2820), fare(2, "SDS", 4490)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        toc_rules=[("00085", "X", "GR")],
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }["BBB"]

    # The bar bites on a routed journey, and only there.
    assert cheapest(paths={"BBB": ["AAA", "BBB"]}, operators={"BBB": {"GR"}}) == 4490
    assert cheapest(operators={"BBB": {"GR"}}) == 2820


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


def test_modes_without_a_path_do_not_withdraw_a_barred_fare(fares):
    """The `X` guard's twin, and the one that could actually have been tripped.

    `check_routes` is `bool(paths)`, so the 'E' station bar and the RTE
    fallback cannot fire without the flag - supplying a path *is* the flag.
    `journey_mode` is filled from `modes=`, which is passed independently, so
    a caller handing over the modes of a journey and nothing else would have
    had mode bars applied without asking for route conditions at all.
    """
    connection, directory = fares(
        flows=[routed(1, "1111", "2222", "00002"), routed(2, "1111", "2222", "00000")],
        fare_records=[fare(1, "SDS", 900), fare(2, "SDS", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        toc_rules=[("00002", "N", "4")],
    )
    cheapest = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)
    }["BBB"]

    # The bar bites on a routed journey, and only there.
    assert cheapest(paths={"BBB": ["AAA", "BBB"]}, modes={"BBB": {"0", "4"}}) == 1500
    assert cheapest(modes={"BBB": {"0", "4"}}) == 900


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
    walk-up ticket types - so this is not a corner case."""
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
        # 5p up to £99.99, then £1 - the shape of FRR rule Z0.
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
# Annual Gold Card 1,206 - genuinely different areas, and Birmingham is in the
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
    """Not knowing the area is not knowing it is empty - the same rule the TOC
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
    them - which is how "that operator does not accept it" is expressed."""
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
    one is the Network Railcard's own - spanning the whole day, so reading it
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
    sometimes wins - 4 of the 2,760 cheapest fares from York are returns, and
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
    later has to fall through to the single - at a higher price. Singles are
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

    On the way home a departure band bites where the journey home *starts* -
    the outward destination - and an arrival band bites back at the origin.
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
    for each. Not knowing the time is not a reason to refuse the fare - the
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
    """RSPS5045 4.19.3 field 10, and the bands cannot express it - it is a
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


# --- the narrow Advance class -----------------------------------------------


def test_a_retailer_scheme_is_not_a_real_advance(fares):
    """`is_advance_fare` is a residual - sellable and not a walk-up - so it
    collects fares that are tied to a booked train and still are not an Advance
    anybody can buy. A retailer's own scheme is the case that costs money:
    `Secret Fare` sits at 0.79 of the real Advance on its flows, so it wins, and
    Euston to Cardiff came out £15.00 against a real cheapest of £29.00."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "NAA", 2900), fare(1, "GW4", 1500)],
        tickets=[ticket("NAA", "ADVANCE", reservation="B"),
                 ticket("GW4", "Secret Fare", reservation="B")],
    )
    # Both are Advances in the broad sense; only one is real.
    broad, real = connection.execute("""
        select count(*) filter (where is_advance_fare),
               count(*) filter (where is_real_advance)
        from ticket_type_current
    """).fetchone()
    assert (broad, real) == (2, 1)

    # And the price the narrow class quotes is the real Advance, not the
    # retailer's.
    priced = cheapest_from(connection, directory, "AAA", TRAVEL, advance_only=True)
    assert [(row[0], row[3]) for row in priced] == [("BBB", 2900)]

    reason = connection.execute(
        "select reason from advance_reject where ticket_code = 'GW4'").fetchone()
    assert reason == ("sold through one retailer scheme, not published",)


def test_a_walk_up_on_a_booked_train_validity_is_not_a_real_advance(fares):
    """**Validity code `11` is why this class exists.** It is *described* "AS
    ADVERTISED" and its `out_description` reads `BOOKDTRAINONLY`, so Grand
    Central's `GTS ANYTIME S` - 205 fares, not one carrying a restriction,
    `reservation_required = 'N'` - came out an Advance and duly won as the
    cheapest one to Hartlepool and Thirsk.

    The rule is that the other two signals outvote the validity: a fare needing
    no reservation, with no booked-train restriction on any of its prices, and
    not calling itself an Advance is not tied to a booked train."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "GTS", 2080), fare(1, "NAA", 2560)],
        tickets=[ticket("GTS", "ANYTIME S", validity="11", reservation="N"),
                 ticket("NAA", "ADVANCE", reservation="B")],
        validities=[("11", False, {"description": "AS ADVERTISED",
                                   "out_description": "BOOKDTRAINONLY"})],
    )
    kinds = dict(connection.execute(
        "select ticket_code, is_real_advance from ticket_type_current").fetchall())
    assert kinds == {"GTS": False, "NAA": True}

    priced = cheapest_from(connection, directory, "AAA", TRAVEL, advance_only=True)
    assert [(row[0], row[3]) for row in priced] == [("BBB", 2560)]


def test_the_narrow_class_never_widens_the_broad_one(fares):
    """`is_real_advance` is a subset, and `include_advance` still reads the
    residual. Two switches on two columns is the one thing here that could go
    quietly wrong, and this is what says it has not."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 7070), fare(1, "GW4", 1500)],
        tickets=[ticket("SDS", "ANYTIME DAY S"),
                 ticket("GW4", "Secret Fare", reservation="B")],
    )
    contradictions = connection.execute("""
        select count(*) from ticket_type_current
        where is_real_advance and not is_advance_fare
    """).fetchone()[0]
    assert contradictions == 0

    # Widening still offers it - nothing existing moves.
    both = cheapest_from(connection, directory, "AAA", TRAVEL, include_advance=True)
    assert [(row[0], row[3]) for row in both] == [("BBB", 1500)]
    # Asking for Advances alone does not, there being no real one here.
    assert cheapest_from(connection, directory, "AAA", TRAVEL,
                         advance_only=True) == []


def test_a_dummy_ticket_type_is_not_sellable_at_all(fares):
    """`ILF DUMY-DO NOT USE` carried 8 fares from £18.90 to £26.90 and was the
    winning cheapest **walk-up** on every one of its flows. `rail validate` had
    been counting these under "ticket types naming themselves test data" and
    passing, which is a check that noticed and did nothing.

    The marker is `%DUM%` rather than `%DUMMY%` because the description field is
    15 characters and the feed ships `Z12 NR SDS DUMM` and `Z123 NR SDS DUM`."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "ILF", 1890), fare(1, "SDS", 7070)],
        tickets=[ticket("ILF", "DUMY-DO NOT USE"), ticket("SDS", "ANYTIME DAY S")],
    )
    assert [(row[0], row[3]) for row in
            cheapest_from(connection, directory, "AAA", TRAVEL)] == [("BBB", 7070)]
    assert connection.execute(
        "select reason from fare_reject where ticket_code = 'ILF'"
    ).fetchone() == ("dummy record, the feed says do not use",)


# --- reviewing new ticket types ---------------------------------------------


def test_the_register_notices_a_new_ticket_type(fares, tmp_path):
    """**The failure this exists for is silent.** A generation ships a product
    nobody has seen, it lands in the wrong class, and it wins immediately -
    because the wrong class is nearly always the cheaper one. `SCR GROUP 05` at
    80p was the cheapest fare from Glasgow Central to 358 destinations."""
    from rail.model import review_tickets, accept_tickets

    register = tmp_path / "reviewed.json"
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 7070)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
    )
    first = review_tickets(connection, directory, path=register)
    assert first.added == ["SDS"]
    assert not first.settled
    accept_tickets(first, path=register)
    assert review_tickets(connection, directory, path=register).settled

    # The next generation brings one more.
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 7070), fare(1, "NEW", 500)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("NEW", "MYSTERY FARE")],
    )
    second = review_tickets(connection, directory, path=register)
    assert second.added == ["NEW"]
    assert second.moved == []
    # And it is already pricing a journey, which is what makes it pressing.
    assert second.carrying_fares() == ["NEW"]


def test_the_register_notices_a_type_changing_class(fares, tmp_path):
    """A code that quietly moves between classes is the other half. `GTS ANYTIME
    S` did not arrive new - it was reclassified by a validity record that had
    always been there, and nothing said so."""
    from rail.model import review_tickets, accept_tickets

    register = tmp_path / "reviewed.json"
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 7070)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
    )
    accept_tickets(review_tickets(connection, directory, path=register),
                   path=register)

    # Same code, now requiring a reservation - so it is an Advance.
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 7070)],
        tickets=[ticket("SDS", "ANYTIME DAY S", reservation="B")],
    )
    moved = review_tickets(connection, directory, path=register)
    assert moved.added == []
    assert moved.moved == [("SDS", "walk-up", "advance")]


def test_a_withdrawn_type_is_reported_and_not_treated_as_new(fares, tmp_path):
    """The register outlives the feed, so a code can leave. Worth saying, and
    worth not confusing with an arrival - a withdrawal cannot misprice
    anything."""
    from rail.model import review_tickets, accept_tickets

    register = tmp_path / "reviewed.json"
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 7070), fare(1, "OLD", 100)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("OLD", "GOING AWAY")],
    )
    accept_tickets(review_tickets(connection, directory, path=register),
                   path=register)

    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 7070)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
    )
    gone = review_tickets(connection, directory, path=register)
    assert gone.withdrawn == ["OLD"]
    assert gone.added == [] and gone.carrying_fares() == []


def test_the_register_records_the_class_and_not_an_override(fares, tmp_path):
    """**The register vouches for a classification; it cannot impose one.**
    Editing a class here and re-reviewing reports the code as *moved*, which is
    the whole design: reviewing means agreeing with the rules or changing them,
    never patching their output in a file nothing explains."""
    import json

    from rail.model import review_tickets, accept_tickets

    register = tmp_path / "reviewed.json"
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 7070)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
    )
    accept_tickets(review_tickets(connection, directory, path=register),
                   path=register)

    written = json.loads(register.read_text())
    written["tickets"]["SDS"]["class"] = "advance"
    register.write_text(json.dumps(written))

    after = review_tickets(connection, directory, path=register)
    assert after.moved == [("SDS", "advance", "walk-up")]


def test_the_shipped_register_covers_every_type_the_rules_produce():
    """The checked-in register, against the classes the code can emit. A class
    renamed in `tickets.py` and not in the file would make every ticket read as
    having moved, which is a rewrite pretending to be a review."""
    import json

    from rail.model import REGISTER
    from rail.model.tickets import ADVANCE, NOT_A_REAL_ADVANCE, REJECTED, WALK_UP

    shipped = json.loads(REGISTER.read_text(encoding="utf-8"))
    classes = {entry["class"] for entry in shipped["tickets"].values()}
    assert classes <= {WALK_UP, ADVANCE, NOT_A_REAL_ADVANCE, REJECTED}
    assert shipped["snapshot"], "the register should say what it was reviewed against"
    assert len(shipped["tickets"]) > 3000


def test_a_fare_says_which_operator_set_it(fares):
    """**RSPS5045's flow record has carried this all along and nothing read
    it.** It surfaced when an Advance ladder turned out to be three operators
    interleaved rather than one operator's quota selling out: York to King's
    Cross climbs £11.00 Grand Central, £18.00 Grand Central, £18.90 LNER,
    £19.60 Grand Central, £22.00 Hull Trains.

    Reported as an ATOC code where `TOC_FARE` gives one, so it can be compared
    with `ScanResult.operators_to`, and as the feed's own id otherwise - 7 of
    the 36 ids that price a flow are historic sector codes with no modern
    equivalent, and their own id is the most honest thing to return."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222", toc="GCR"),
               flow(2, "1111", "2222", toc="ZZZ", route="00027")],
        fare_records=[fare(1, "NAA", 1100), fare(2, "NAA", 1890)],
        tickets=[ticket("NAA", "ADVANCE", reservation="B")],
        tocs=[("GCR", "GC", "GRAND CENTRAL RAILWAY")],
    )
    priced = {row[3]: row[7] for row in fare_options(
        connection, directory, "AAA", TRAVEL, advance_only=True)}
    # Mapped to its ATOC code where the crossref has one, its own id otherwise.
    assert priced == {1100: "GC", 1890: "ZZZ"}


def test_a_non_derivable_fare_names_no_operator(fares):
    """NFO states a price against a code pair outright and has no operator field
    at all. Null is the honest answer - "the feed does not say", never "no
    operator" - and a caller grouping by operator has to keep it apart from a
    named one rather than folding it into a blank."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222", toc="GCR")],
        fare_records=[fare(1, "NAA", 5000)],
        tickets=[ticket("NAA", "ADVANCE", reservation="B")],
        nfo=[nfo("1111", "2222", "NAA", 1100)],
        tocs=[("GCR", "GC", "GRAND CENTRAL RAILWAY")],
    )
    priced = {row[3]: row[7] for row in fare_options(
        connection, directory, "AAA", TRAVEL, advance_only=True)}
    assert priced == {1100: None}


def test_two_routes_at_one_price_are_two_rows_when_asked(fares):
    """**`fare_options` returns one row per distinct price, and that hides a
    route.** York to Edinburgh offers £54.80 on both `XC ONLY` and `LNER &
    CONNECTNS`; the group-by collapsed them and the tie-break named whichever
    route sorted first, so a caller listing what each route sells lost the
    other. A retailer lists it under both, which is how it was found.

    501 of the 95,404 route-price pairs from York are hidden this way. The
    default is unchanged, because collapsing is right for "what is the cheapest
    fare" and only wrong for "what does this route sell"."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222", route="00024"),
               flow(2, "1111", "2222", route="00430")],
        fare_records=[fare(1, "NAA", 5480), fare(2, "NAB", 5480)],
        tickets=[ticket("NAA", "ADVANCE", reservation="B"),
                 ticket("NAB", "ADVANCE", reservation="B")],
    )
    collapsed = fare_options(connection, directory, "AAA", TRAVEL, advance_only=True)
    assert len(collapsed) == 1, "one row per distinct price, as before"

    split = fare_options(connection, directory, "AAA", TRAVEL,
                         advance_only=True, per_route=True)
    assert sorted(row[5] for row in split) == ["00024", "00430"]
    assert {row[3] for row in split} == {5480}


def test_fare_options_says_which_restriction_governs_a_fare(fares):
    """**Null is the useful value.** A fare with no restriction code is usable
    on any train, which is what an Anytime ticket is - and a caller comparing
    against a booked-train Advance has to be able to name it.

    Inferring it instead - "the fare that survives a peak departure" - answers a
    different question: a peak-valid fare can still be restricted in other ways.

    It is appended rather than put beside `tkt_type` where it belongs by
    meaning, because every consumer reads this tuple positionally and inserting
    would shift `operator` by one. An operator code read as a restriction is the
    kind of wrong that still looks like a string."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1510, restriction="1A"),
                      fare(1, "SOS", 2010)],
        tickets=[ticket("SDS", "OFF-PEAK DAY S"), ticket("SOS", "ANYTIME S")],
    )
    rows = fare_options(connection, directory, "AAA", TRAVEL)
    assert rows, "the fixture prices nothing"
    # The dearer one carries no restriction, which is what makes it the
    # anytime fare - and the cheaper one names the restriction that governs it.
    assert {row[1]: row[8] for row in rows} == {"SDS": "1A", "SOS": None}
    assert all(len(row) == 9 for row in rows)
    # The operator has not moved: it is still the eighth.
    assert all(row[7] is None or isinstance(row[7], str) for row in rows)
    assert all(row[8] is None or isinstance(row[8], str) for row in rows)


def test_an_add_on_applies_when_the_flow_priced_through_a_cluster(fares):
    """**The destination is expanded like the origin, and for years it was not.**

    Fares are not point-to-point: a station is named by its own NLC, its fare
    group, every cluster holding it and its county code. Which of those a flow
    happened to be *found* by has nothing to do with which one an FNS record
    chose to *name* - and the join compared the FNS destination against the one
    the flow matched on, so the two only ever met by luck.

    Here the flow prices `BBB` through cluster `C002` and the FNS record names
    `2222`, its own NLC. Both are `BBB`. Before the fix the add-on never
    applied and the fare came out 120 light.

    This is the real shape of it. Stratford to Shanklin prices through cluster
    `Q262` while the record names `5529`, and every walk-up fare to the Isle of
    Wight was 25-45p under what a retailer sells. Confirmed on four pairs and
    three railcards: 14 of 14 retailer-quoted prices reproduce with the
    destination expanded and 0 of 14 without.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "C002", ns_disc=1)],
        fare_records=[fare(1, "SDS", 1000)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        clusters=[("C002", "2222")],          # BBB belongs to cluster C002
        railcards=[railcard("YNG", "16-25 RAILCARD", "003", per_mille=334)],
        fns=[fns(dest="2222", railcard="YNG", addon=120)],
    )
    # 1000 less 33.4% is 666, rounded down to 665, plus the £1.20 add-on.
    assert discounted(connection, directory) == {"BBB": 785}


def test_a_contactless_tap_is_not_a_walk_up_fare(fares):
    """**The same product was answered two ways, and that was the fault.**

    TfL's `PAYG PEAK INFO` and `PAYG OFFPK INFO` were already excluded, because
    the feed names those informational and `%INFO%` caught them. None of the
    other 28 pay-as-you-go types matched anything, so Transport for Wales'
    `TFW PAYG Single` was a walk-up fare and **won as the cheapest on about 90
    destinations from every South Wales origin** - Cardiff to Abergavenny came
    out £4.20 against a £17.70 Anytime Day Single.

    Nothing is lost by excluding it: every one of those had an ordinary fare
    behind it. But the map prices tickets, and a tap is not one.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1770), fare(1, "TFW", 420)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("TFW", "PAYG Single")],
    )

    assert [(r[1], r[3]) for r in fare_options(connection, directory, "AAA", TRAVEL)] \
        == [("SDS", 1770)]


def test_pay_as_you_go_is_kept_rather_than_discarded(fares):
    """A tap is a price somebody really pays, so it is a *third* question -
    never mixed into the walk-ups, which is the only honest way round when the
    two are different products bought different ways.

    Reaches TfL's records as well: their descriptions carry `PAYG` too, so the
    50,907 fares behind `PAYG PEAK INFO` and `PAYG OFFPK INFO` come back here
    even though `%INFO%` is what excludes them from the walk-ups.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1770), fare(1, "TFW", 420),
                      fare(1, "POP", 310)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("TFW", "PAYG Single"),
                 ticket("POP", "PAYG OFFPK INFO")],
    )

    tap = fare_options(connection, directory, "AAA", TRAVEL, payg_only=True)

    assert sorted((r[1], r[3]) for r in tap) == [("POP", 310), ("TFW", 420)]
    # And it is only the taps - a walk-up must not arrive through this door.
    assert "SDS" not in {row[1] for row in tap}


def test_a_daily_cap_is_not_a_fare(fares):
    """`PAYG Daily Cap`, `PAYG HERE-HERE`, `PAYG UNSTARTED` and `PAYG
    INCOMPLETE` are a ceiling on a day's spending, touching in and out at one
    station, and two ways of not touching out. None is the price of a journey,
    and all four were classified as walk-up fares."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1770), fare(1, "TFX", 370),
                      fare(1, "PTH", 1000)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("TFX", "PAYG Daily Cap"),
                 ticket("PTH", "PAYG HERE-HERE")],
    )

    assert [r[1] for r in fare_options(connection, directory, "AAA", TRAVEL)] == ["SDS"]


def test_an_ordinary_ticket_beats_a_smartcard_one_at_the_same_price(fares):
    """`0AE SMART SDR` and `SDR ANYTIME DAY R` are the same product in two
    media - same price, route, restriction, validity, ticket group and discount
    category, differing only in how many flows carry them, 2,395 against
    275,483.

    A price group names one ticket, and ordering by code alone gave the
    smartcard one every time because digits sort before letters. Euston to
    Shepherd's Bush read "SMART SDR" with an identically priced paper ticket
    sitting beside it, which tells a reader they need a smartcard they do not.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "0AE", 840), fare(1, "SDR", 840)],
        tickets=[ticket("0AE", "SMART SDR"), ticket("SDR", "ANYTIME DAY R")],
    )

    priced = fare_options(connection, directory, "AAA", TRAVEL)

    # One row - they are one price - and it is the one anybody can buy.
    assert [(r[1], r[2], r[3]) for r in priced] == [("SDR", "ANYTIME DAY R", 840)]


def test_a_smartcard_only_fare_keeps_its_name(fares):
    """65 of the 5,462 price groups a SMART ticket wins have no ordinary twin.
    Renaming those would be inventing a ticket that is not sold."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "0AK", 2200)],
        tickets=[ticket("0AK", "SMART CDR")],
    )

    assert [r[2] for r in fare_options(connection, directory, "AAA", TRAVEL)] \
        == ["SMART CDR"]


def test_the_tie_break_does_not_reach_for_the_commoner_ticket(fares):
    """**The tempting general rule, measured and discarded.** Preferring
    whichever ticket sits on more flows would rename 63,028 groups, and the
    biggest families are 25,560 `OFF-PEAK DAY R` becoming `ANYTIME DAY R` and
    14,477 `ANYTIME R` becoming `OFF-PEAK R` - different products that happen
    to cost the same, whose restrictions the panel would then describe wrongly.

    So two ordinary tickets at one price still tie on the code, as they always
    did, and only the smartcard test is applied.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SVR", 3350), fare(1, "SOR", 3350)],
        tickets=[ticket("SOR", "ANYTIME R"), ticket("SVR", "OFF-PEAK R")],
    )

    # `SOR` sorts first and wins, exactly as before - no reaching for the
    # off-peak one because it happens to be the commoner product.
    assert [r[1] for r in fare_options(connection, directory, "AAA", TRAVEL)] == ["SOR"]


def test_a_zone_code_prices_a_journey_that_uses_the_underground(fares):
    """RSPS5045 4.1.2's third endpoint form, and the last one to be reached.

    A `ZONE U1` fare is a *through* fare from a London Underground zone - it
    includes the hop the station's own fare does not. `ZONE U1 -> KINGS LYNN`
    is £50.70 against London Terminals' £47.70, and £50.70 is what a retailer
    quotes for a Euston journey; the whole Euston to Claygate ladder is the
    same, singles £3.00 apart and returns £6.00.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222"), flow(2, "0785", "2222")],
        fare_records=[fare(1, "SDS", 4770), fare(2, "SDS", 5070)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
    )
    price = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)}

    assert price() == {"BBB": 4770}
    assert price(origin_zone="0785") == {"BBB": 5070}


def test_a_zone_replaces_the_station_codes_rather_than_joining_them(fares):
    """**The reason this is a parameter and not another `fare_alias` row.**

    A zone fare is usually dearer, being the same journey plus the
    Underground - 850 of 859 comparable pairs from Euston. On 9 it is
    *cheaper*, by up to £108.80, so a union would quote a fare including the
    Underground to a passenger whose journey never touches it, and quote it as
    the cheapest. Asked for, it is the only answer; unasked, it is absent.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222"), flow(2, "0785", "2222")],
        fare_records=[fare(1, "SDS", 4770), fare(2, "SDS", 1830)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
    )
    price = lambda **kw: {
        r[0]: r[3] for r in cheapest_from(connection, directory, "AAA", TRAVEL, **kw)}

    assert price() == {"BBB": 4770}
    assert price(origin_zone="0785") == {"BBB": 1830}


def test_a_zone_priced_fare_still_departs_from_the_station(fares):
    """`$origin` is unchanged: the passenger stands at Euston whichever code
    prices the ticket, so a band naming the station still bites.
    """
    connection, directory = fares(
        flows=[flow(1, "0785", "2222")],
        fare_records=[fare(1, "SDS", 5070), fare(1, "CDS", 3000, restriction="0W")],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("CDS", "OFF-PEAK DAY S")],
        bands=[("0W", 270, 565, "D", "AAA")],  # 04:30-09:25 departing AAA
    )
    price = lambda **kw: {
        r[0]: (r[1], r[3])
        for r in cheapest_from(connection, directory, "AAA", TUESDAY,
                               origin_zone="0785", **kw)}

    assert price(depart_minutes=660) == {"BBB": ("CDS", 3000)}
    assert price(depart_minutes=450) == {"BBB": ("SDS", 5070)}


def test_the_stations_own_nlc_beats_a_cheaper_cluster_fare(fares):
    """RSPS5045 ranks the two nowhere and this file used to take the lower,
    which is not what is sold: Aldermaston to Overton sells a £136.60
    first-class return against a £91.20 cluster fare."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222"), flow(2, "C111", "C222")],
        fare_records=[fare(1, "SDS", 3880), fare(2, "SDS", 2650)],
        tickets=[ticket("SDS", "ANYTIME DAY S")],
        clusters=[("C111", "1111"), ("C222", "2222")],
    )

    rows = cheapest_from(connection, directory, "AAA", TUESDAY)
    assert {row[0]: row[3] for row in rows}["BBB"] == 3880


def test_a_cluster_still_prices_a_ticket_the_own_nlc_flow_has_not_got(fares):
    """Brighton to London Bridge prices Super Off-Peak from its own NLC and
    every other ticket from a cluster, and a retailer sells all of them - so
    the precedence is per ticket, not "ignore clusters"."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222"), flow(2, "C111", "C222")],
        fare_records=[fare(1, "SDS", 3880), fare(2, "CDS", 2650)],
        tickets=[ticket("SDS", "ANYTIME DAY S"), ticket("CDS", "OFF-PEAK DAY S")],
        clusters=[("C111", "1111"), ("C222", "2222")],
    )

    rows = cheapest_from(connection, directory, "AAA", TUESDAY)
    assert {row[0]: row[3] for row in rows}["BBB"] == 2650


def test_cpay_is_pay_as_you_go_under_another_name(fares):
    """Project Oval extends contactless beyond the Oyster area and names its
    records CPAY, not PAYG. Reading only PAYG answered TfL and left 191
    stations with no tap at all - the reject table called them a
    "pay-as-you-go information record" while is_payg said false."""
    connection, _ = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "PAC", 880), fare(1, "PAT", 600),
                      fare(1, "SDS", 900)],
        tickets=[ticket("PAC", "CPAY PEAK INFO"),
                 ticket("PAT", "CPAY PEAK TEST"),
                 ticket("SDS", "ANYTIME DAY S")],
    )

    classified = dict(connection.execute(
        "select ticket_code, is_payg from ticket_type_current"
    ).fetchall())
    assert classified["PAC"] is True
    assert classified["PAT"] is False, "TEST is a pilot set, not a price"
    assert classified["SDS"] is False


def test_the_code_family_says_which_cards_are_accepted(fares):
    """Checked against RDG's contactless map: a station whose own NLC carries
    PAYG is drawn yellow, "Oyster and contactless"; CPAY alone is light or dark
    green, "Oyster is NOT valid". OTU is a top-up ladder, not a journey."""
    connection, _ = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "PAP", 300), fare(1, "PAC", 400),
                      fare(1, "PAT", 500), fare(1, "OTU", 600),
                      fare(1, "SDS", 900)],
        tickets=[ticket("PAP", "PAYG PEAK INFO"), ticket("PAC", "CPAY PEAK INFO"),
                 ticket("PAT", "CPAY PEAK TEST"), ticket("OTU", "OYSTER  PREPAY"),
                 ticket("SDS", "ANYTIME DAY S")],
    )

    rows = dict(connection.execute(
        "select ticket_code, payg_family from ticket_type_current"
    ).fetchall())
    assert rows["PAP"] == "oyster"
    assert rows["PAC"] == "contactless"
    assert rows["PAT"] is None, "not enabled yet, so not a price"
    assert rows["OTU"] == "topup", "the value loaded on a card, not a journey"
    assert rows["SDS"] is None

    payg = dict(connection.execute(
        "select ticket_code, is_payg from ticket_type_current"
    ).fetchall())
    assert [payg[t] for t in ("PAP", "PAC", "PAT", "OTU", "SDS")] == [
        True, True, False, False, False]


def test_a_departure_band_needs_a_train_boarded_there_not_merely_a_change(fares):
    """**Changing onto a walk is not departing on a train.**

    `is_change` is true wherever the journey changes, and that includes
    changing onto a fixed link - a tube hop or a walk between stations. A
    departure band bars *trains*, so a station the passenger leaves on foot is
    not somewhere it can bite.

    Canary Wharf to York reaches Liverpool Street at 09:07 on the Elizabeth
    line and walks to King's Cross for the 10:03. `4R` band 0077 bars
    departures from Liverpool Street before 09:29; applied there it withdrew
    the £78.50 Super Off-Peak a retailer sells and left £176.00. Custom House
    to Aberdeen and Brighton to Banbury confirm the fix at £119.20 and £60.80.

    **Judged on the operator boarded rather than on whether one was, and it is
    refuted twice**: Brighton to Witley boards South Western at Havant where
    the band names Southern, and York to Cambridge boards LNER at York where
    the band names CrossCountry - a retailer keeps both bands. So the test is
    "was a train boarded here", and the TOC qualifier stays journey-wide.
    """
    world = dict(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 17600), fare(1, "SSS", 7850, restriction="PB")],
        tickets=[ticket("SDS", "ANYTIME S"), ticket("SSS", "SUPER OFFPEAK S")],
        stations=[("AAA", "1111", "1111"), ("BBB", "2222", "2222"),
                  ("MID", "3333", "3333")],
    )
    # Changes at MID, leaving there at 11:40 - inside the band below.
    changing = {"BBB": [("AAA", 484, 484, False), ("MID", 603, 700, True),
                        ("BBB", 972, 972, False)]}
    ask = lambda **kw: {
        r[0]: (r[1], r[3]) for r in cheapest_from(connection, directory, "AAA",
                                                  TUESDAY, **kw)}["BBB"]
    run = lambda **kw: ask(depart_minutes=480, arrivals={"BBB": 972},
                           calls=changing, **kw)

    connection, directory = fares(**world, bands=[("PB", 690, 720, "D", "MID")])

    # No boardings supplied - unchanged, so no existing caller moves.
    assert run() == ("SDS", 17600)

    # A train is boarded at MID: the band still bites.
    assert run(boardings={"BBB": [("AAA", "GR"), ("MID", "GW")]}) == ("SDS", 17600)

    # The passenger leaves MID on foot - the fixed link carries no operator, so
    # there is no boarding there and the band has nothing to speak to.
    assert run(boardings={"BBB": [("AAA", "GR"), ("MID", "")]}) == ("SSS", 7850)

    # And the operator boarded is *not* the test: a band naming nobody in
    # particular still bites where a train is caught.
    assert run(boardings={"BBB": [("AAA", "GR"), ("MID", "XC")]}) == ("SDS", 17600)
