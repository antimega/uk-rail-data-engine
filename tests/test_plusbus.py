"""PlusBus add-ons.

The prices come from the fares feed and the rules from RSPS5052, and the rule
that matters is the one the spec opens with: an add-on may not be sold when both
ends of the rail journey sit in the same zone, because the product buys travel
*around* a place rather than between two. Derby to Buxton is fine; Buxton to
Matlock is not.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pytest

from rail.model.plusbus import add_ons_from, may_sell_add_on, zone_for

TRAVEL = dt.date(2026, 8, 4)
PAST = dt.date(2000, 1, 1)
FOREVER = dt.date(2999, 12, 31)


@pytest.fixture
def world():
    """Buxton, Matlock and Derby, with Buxton and Matlock sharing a zone."""
    c = duckdb.connect()
    c.execute("""create table plusbus_zone as select * from (values
        ('BUX', '1234', 'H001', 'BUXTON+BUS', 300),
        ('MAT', '5678', 'H002', 'MATLOCK+BUS', 320),
        ('DBY', '9012', 'H003', 'DERBY+BUS', 400)
    ) t(crs, station_nlc, zone_nlc, zone_name, day_fare)""")
    c.execute("""create table plusbus_fare as select * from (values
        ('BUX', 'PBD', 'PLUSBUS DAY', 300),
        ('BUX', 'PB7', 'PLUSBUS 7-DAY', 1200),
        ('MAT', 'PBD', 'PLUSBUS DAY', 320),
        ('DBY', 'PBD', 'PLUSBUS DAY', 400)
    ) t(crs, ticket_code, description, fare)""")
    c.execute("create table plusbus_web_page as select * from (values "
              "('H001', 'https://www.plusbus.info/buxton')) t(nlc, url)")
    c.execute("""create table plusbus_excluded_pair as select * from (values
        (DATE '2026-01-02', DATE '2027-01-01', '1234', '5678'),
        (DATE '2025-01-02', DATE '2026-01-01', '1234', '9012')
    ) t(start_date, end_date, from_nlc, to_nlc)""")
    return c


def test_a_pair_in_the_same_zone_gets_no_add_on(world):
    """RSPS5052 2.1.1's own example."""
    assert may_sell_add_on(world, "BUX", "MAT", TRAVEL) is False


def test_a_pair_in_different_zones_does(world):
    """Also 2.1.1: Derby and Buxton both have zones but not the same one."""
    assert may_sell_add_on(world, "DBY", "BUX", TRAVEL) is True


def test_the_exclusion_is_reversible(world):
    """2.1.6: a record from A to B applies from B to A, and the file carries
    only one of the two."""
    assert may_sell_add_on(world, "MAT", "BUX", TRAVEL) is False


def test_an_expired_exclusion_does_not_apply(world):
    """The file ships two annual generations and half of it has expired, so it
    is a version history like everything else in these feeds."""
    assert may_sell_add_on(world, "BUX", "DBY", TRAVEL) is True
    assert may_sell_add_on(world, "BUX", "DBY", dt.date(2025, 6, 1)) is False


def test_neither_end_having_a_zone_is_an_absence_not_a_refusal(world):
    assert may_sell_add_on(world, "POP", "HMM", TRAVEL) is None


def test_one_end_having_a_zone_is_enough_to_offer_one(world):
    assert may_sell_add_on(world, "BUX", "POP", TRAVEL) is True


def test_a_zone_carries_its_fares_and_its_scheme_page(world):
    zone = zone_for(world, "BUX")

    assert zone["zone_name"] == "BUXTON+BUS"
    assert zone["url"] == "https://www.plusbus.info/buxton"
    assert [f["pence"] for f in zone["fares"]] == [300, 1200]


def test_a_station_with_no_zone_returns_nothing(world):
    assert zone_for(world, "POP") is None


# --- the batched form, for pricing a whole sweep ------------------------------


def test_add_ons_are_offered_at_every_destination_with_a_zone(world):
    """`rail reachable` prices thousands of destinations, so this is batched
    rather than asked per station."""
    found = add_ons_from(world, "DBY", TRAVEL)

    assert found == {"BUX": 300, "MAT": 320}


def test_the_origins_own_zone_is_not_offered_back_to_it(world):
    """An add-on at the station you started from is not part of the journey's
    cost, and buying travel within one zone is not the product."""
    assert "DBY" not in add_ons_from(world, "DBY", TRAVEL)


def test_an_excluded_destination_is_left_out_of_the_batch(world):
    found = add_ons_from(world, "BUX", TRAVEL)

    assert "MAT" not in found       # same zone, excluded by RSPS5052
    assert found["DBY"] == 400      # a different zone, so fine


def test_the_batch_honours_the_validity_dates_too(world):
    """The Buxton–Derby exclusion expired on 2026-01-01."""
    assert "DBY" in add_ons_from(world, "BUX", TRAVEL)
    assert "DBY" not in add_ons_from(world, "BUX", dt.date(2025, 6, 1))


def test_only_the_day_ticket_is_offered_alongside_a_single(world):
    """A 7-day PlusBus beside a single journey is not the question being asked."""
    found = add_ons_from(world, "DBY", TRAVEL)

    assert found["BUX"] == 300  # PBD, not the £12.00 PB7
