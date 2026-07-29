"""Going and coming back, priced both ways.

The question these answer is the one nothing else here could: *I am going on
Tuesday and back on Thursday, what should I buy* - where the answer is a choice
between one return ticket and two singles, and neither is reliably cheaper.

The fares world is built by `test_fares.fares`, which is where the fixture and
its helpers live; importing them keeps one definition of what a fare looks like.
"""

from __future__ import annotations

import datetime as dt

import pytest

from rail.model.roundtrip import Leg, price_round_trip
from test_fares import TUESDAY, fare, fares, flow, ticket  # noqa: F401

THURSDAY = TUESDAY + dt.timedelta(days=2)

RETURN_VALIDITIES = [("01", True), ("13", True, {"ret_months": 1}),
                     ("06", True, {"ret_days": 1})]

#: The two directions are priced a little differently on purpose. A ticket type
#: charging one price on every one of its flows is a flat-rate product to the
#: classifier - that is what keeps "Kid with Adult" out of the walk-up set - so
#: pricing both directions identically would withdraw the single entirely.
OUT, BACK = 1700, 1650


def legs(*, out_depart=11 * 60, out_arrive=12 * 60,
         back_depart=17 * 60, back_arrive=18 * 60):
    """One journey each way, already routed - which is what `Leg` means."""
    return (
        Leg(origin="AAA", destination="BBB", date=TUESDAY,
            depart=out_depart, arrive=out_arrive, path=["AAA", "BBB"],
            operators={"XX"}, modes={"0"}),
        Leg(origin="BBB", destination="AAA", date=THURSDAY,
            depart=back_depart, arrive=back_arrive, path=["BBB", "AAA"],
            operators={"XX"}, modes={"0"}),
    )


def test_a_return_ticket_and_two_singles_are_both_priced(fares):
    """Quoting one without the other is the misleading part."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222"), flow(2, "2222", "1111")],
        fare_records=[fare(1, "SVR", 2010), fare(1, "SDS", OUT),
                      fare(2, "SDS", BACK)],
        tickets=[ticket("SVR", "OFF-PEAK R", kind="R", validity="13"),
                 ticket("SDS", "ANYTIME DAY S", validity="01")],
        validities=RETURN_VALIDITIES,
    )
    trip = price_round_trip(connection, directory, *legs())

    assert trip.single_ticket.pence == 2010
    assert trip.two_singles.pence == OUT + BACK
    assert trip.best.kind == "return"
    assert trip.saving == OUT + BACK - 2010


def test_two_singles_win_when_they_are_cheaper(fares):
    """A Super Off-Peak Return is often barely more than a single, and just as
    often a pair of singles undercuts everything. The winner is measured, not
    assumed."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222"), flow(2, "2222", "1111")],
        fare_records=[fare(1, "SVR", 5000), fare(1, "SDS", OUT),
                      fare(2, "SDS", BACK)],
        tickets=[ticket("SVR", "OFF-PEAK R", kind="R", validity="13"),
                 ticket("SDS", "ANYTIME DAY S", validity="01")],
        validities=RETURN_VALIDITIES,
    )
    trip = price_round_trip(connection, directory, *legs())

    assert trip.best.kind == "two singles"
    assert trip.best.pence == OUT + BACK


def test_the_cheapest_return_is_found_behind_a_cheaper_single(fares):
    """`cheapest_from` keeps only the cheapest fare per destination, which is
    almost always a single - so asking it for a return finds nothing. York to
    King's Cross has a £130.40 Off-Peak Return sitting behind a £70.70 single,
    and the first version of this reported no return for every pair.
    """
    connection, directory = fares(
        flows=[flow(1, "1111", "2222"), flow(2, "2222", "1111")],
        fare_records=[fare(1, "SVR", 13040), fare(1, "SDS", 7070),
                      fare(2, "SDS", 7000)],
        tickets=[ticket("SVR", "OFF-PEAK R", kind="R", validity="13"),
                 ticket("SDS", "ANYTIME DAY S", validity="01")],
        validities=RETURN_VALIDITIES,
    )
    trip = price_round_trip(connection, directory, *legs())

    assert trip.single_ticket is not None
    assert trip.single_ticket.pence == 13040
    assert trip.best.kind == "return"   # £130.40 against £140.70


def test_a_return_whose_validity_cannot_reach_the_return_date_is_not_offered(fares):
    """A Day Return is valid on the date shown, so it cannot cover a Thursday
    journey home from a Tuesday outward."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222"), flow(2, "2222", "1111")],
        fare_records=[fare(1, "CDR", 1520), fare(1, "SDS", OUT),
                      fare(2, "SDS", BACK)],
        tickets=[ticket("CDR", "OFF-PEAK DAY R", kind="R", validity="06"),
                 ticket("SDS", "ANYTIME DAY S", validity="01")],
        validities=RETURN_VALIDITIES,
    )
    two_days_apart = price_round_trip(connection, directory, *legs())
    same_day = price_round_trip(
        connection, directory,
        *[Leg(**{**leg.__dict__, "date": TUESDAY}) for leg in legs()])

    assert two_days_apart.single_ticket is None
    assert two_days_apart.best.kind == "two singles"
    # Back the same day, and the Day Return is both valid and cheapest.
    assert same_day.single_ticket.pence == 1520


def test_a_return_leg_restriction_withdraws_the_return_ticket(fares):
    """The point of routing the way back at all: 13,803 bands in force on a
    weekday govern the return leg, and until the journey home had times to test
    against, not one of them could be evaluated."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222"), flow(2, "2222", "1111")],
        fare_records=[fare(1, "SVR", 2010, restriction="R1"),
                      fare(1, "SDS", OUT), fare(2, "SDS", BACK)],
        tickets=[ticket("SVR", "OFF-PEAK R", kind="R", validity="13"),
                 ticket("SDS", "ANYTIME DAY S", validity="01")],
        validities=RETURN_VALIDITIES,
        # Not valid leaving BBB before 09:30 on the way home.
        bands=[("R1", 270, 569, "D", "BBB", False, "R")],
    )
    peak = price_round_trip(connection, directory, *legs(back_depart=8 * 60,
                                                         back_arrive=9 * 60))
    later = price_round_trip(connection, directory, *legs())

    assert peak.single_ticket is None
    assert later.single_ticket.pence == 2010


def test_each_single_is_restricted_on_its_own_leg(fares):
    """The two singles are two journeys, and an Off-Peak barred in the morning
    peak outward is perfectly usable coming home at five."""
    connection, directory = fares(
        flows=[flow(1, "1111", "2222"), flow(2, "2222", "1111")],
        fare_records=[fare(1, "CDS", 1510, restriction="R1"),
                      fare(1, "SDS", OUT),
                      fare(2, "CDS", 1490, restriction="R1"),
                      fare(2, "SDS", BACK)],
        tickets=[ticket("CDS", "OFF-PEAK DAY S", validity="01"),
                 ticket("SDS", "ANYTIME DAY S", validity="01")],
        validities=RETURN_VALIDITIES,
        # A plain outward-leg peak bar, which applies to each single's own
        # journey - the outward one leaves AAA, the homeward one leaves BBB.
        bands=[("R1", 270, 569, "D", None, False, "O")],
    )
    trip = price_round_trip(
        connection, directory, *legs(out_depart=8 * 60, out_arrive=9 * 60))

    # Outward is in the peak so pays the Anytime; the way home is not.
    assert [t[2] for t in trip.two_singles.tickets] == [OUT, 1490]
    assert trip.two_singles.pence == OUT + 1490


def test_nothing_priced_leaves_both_quotes_absent(fares):
    connection, directory = fares(
        flows=[flow(1, "1111", "2222")],
        fare_records=[fare(1, "SDS", 1700)],
        tickets=[ticket("SDS", "ANYTIME DAY S", validity="01")],
        validities=RETURN_VALIDITIES,
    )
    # No flow the other way, so the journey home cannot be priced at all.
    trip = price_round_trip(connection, directory, *legs())

    assert trip.single_ticket is None
    assert trip.two_singles is None
    assert trip.best is None
    assert trip.saving is None


def test_a_leg_reports_its_own_duration(fares):
    out, back = legs()

    assert out.minutes == 60
    assert back.minutes == 60


# --- breaking the journey ----------------------------------------------------


def breakable(fares, *, out_ok, home_ok):
    """A return whose validity permits a break outward, homeward, or neither."""
    return fares(
        flows=[flow(1, "1111", "2222"), flow(2, "2222", "1111")],
        fare_records=[fare(1, "SVR", 2010), fare(1, "SDS", OUT),
                      fare(2, "SDS", BACK)],
        tickets=[ticket("SVR", "OFF-PEAK R", kind="R", validity="13"),
                 ticket("SDS", "ANYTIME DAY S", validity="01")],
        validities=[("01", True),
                    ("13", True, {"ret_months": 1,
                                  "break_out": out_ok, "break_in": home_ok})],
    )


def test_a_break_on_the_way_home_needs_its_own_permission(fares):
    """TVL field 13, and the reason it could not be enforced before: nothing
    routed a return leg through a chosen stop, so there was no journey for the
    permission to be about. 32 of the 444 walk-up returns bar one."""
    connection, directory = breakable(fares, out_ok=True, home_ok=False)

    assert price_round_trip(connection, directory, *legs()).single_ticket
    assert price_round_trip(
        connection, directory, *legs(), break_in=True).single_ticket is None


def test_the_two_directions_are_separate_permissions(fares):
    """A ticket may allow a break coming home and not going out. 109 walk-up
    returns are the other way round - no break outward, one permitted home."""
    connection, directory = breakable(fares, out_ok=False, home_ok=True)
    priced = lambda **kw: price_round_trip(
        connection, directory, *legs(), **kw).single_ticket

    assert priced(break_in=True) is not None
    assert priced(break_out=True) is None


def test_an_unbroken_journey_asks_for_no_permission(fares):
    connection, directory = breakable(fares, out_ok=False, home_ok=False)

    assert price_round_trip(connection, directory, *legs()).single_ticket
