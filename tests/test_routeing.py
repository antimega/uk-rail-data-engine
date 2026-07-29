"""Permitted routes from the National Routeing Guide.

Three things about the guide are easy to get wrong and each was got wrong first
time here, so each has a test:

* Field 6 of a station record is the station *group*, not a fifth routeing
  point, and a station with no listed points routes via its group.
* Maps are graphs of routeing points that trains pass *through* without calling,
  so a journey's calling points are a subsequence of a path across the map, not
  a chain of direct links.
* Journeys through London are validated as two halves with a transfer between,
  not as one continuous route.
"""

from __future__ import annotations

import duckdb
import pytest

from rail.model.distance import Distances
from rail.model.routeing import RouteingGuide


def guide(*, points, nodes=None, station_points=None, groups=None,
          routes=None, links=None, cross_london=(), station_links=()):
    built = RouteingGuide(
        points=set(points),
        nodes=set(nodes if nodes is not None else points),
        station_points=station_points or {},
        station_group=groups or {},
        routes=routes or {},
        map_links={code: set(pairs) for code, pairs in (links or {}).items()},
    )
    built.cross_london = set(cross_london)
    if station_links:
        # RGD mileages, which sections 7.1.2 and 7.2.4 are written against.
        adjacent = {}
        for source, target, miles in station_links:
            adjacent.setdefault(source, []).append((target, miles))
            adjacent.setdefault(target, []).append((source, miles))
        built.distances = Distances(adjacent=adjacent)
    return built


def test_a_station_routes_via_its_listed_points():
    g = guide(points=["DIN"], station_points={"AAT": ["DIN"]})
    assert g.points_for("AAT") == ["DIN"]


def test_a_routeing_point_stands_for_itself():
    g = guide(points=["ABD"])
    assert g.points_for("ABD") == ["ABD"]


def test_a_station_in_a_group_routes_via_the_group():
    """Aston is not a routeing point; Birmingham Group is, and contains it."""
    g = guide(points=["G02"], groups={"AST": "G02", "BHM": "G02"})

    assert g.points_for("AST") == ["G02"]
    assert g.node_of("BHM") == "G02"


def test_a_journey_along_a_map_is_permitted():
    g = guide(
        points=["YRK", "NCL"], nodes=["YRK", "NTR", "DAR", "DHM", "NCL"],
        routes={("YRK", "NCL"): [("YA",)]},
        links={"YA": [("YRK", "NTR"), ("NTR", "DAR"), ("DAR", "DHM"), ("DHM", "NCL")]},
    )
    assert g.permits("YRK", "NCL", ["YRK", "DAR", "NCL"]) is True


def test_calling_points_need_not_be_directly_linked():
    """Trains pass through map nodes without stopping.

    On the real map YA, York links to Northallerton rather than Darlington, yet
    York to Newcastle calling only at Darlington is plainly permitted.
    """
    g = guide(
        points=["YRK", "NCL"], nodes=["YRK", "NTR", "DAR", "NCL"],
        routes={("YRK", "NCL"): [("YA",)]},
        links={"YA": [("YRK", "NTR"), ("NTR", "DAR"), ("DAR", "NCL")]},
    )
    # Darlington is reachable from York across the map even though no single
    # link joins them.
    assert g.permits("YRK", "NCL", ["YRK", "DAR", "NCL"]) is True


def test_a_journey_off_the_map_is_refused():
    g = guide(
        points=["YRK", "NCL"], nodes=["YRK", "NTR", "NCL", "CAR"],
        routes={("YRK", "NCL"): [("YA",)]},
        links={"YA": [("YRK", "NTR"), ("NTR", "NCL")]},
    )
    # Carlisle is on no permitted map for this pair.
    assert g.permits("YRK", "NCL", ["YRK", "CAR", "NCL"]) is False


def test_links_are_directional():
    """The file carries the reverse record wherever the reverse is valid."""
    g = guide(
        points=["AAA", "BBB"], nodes=["AAA", "MID", "BBB"],
        routes={("AAA", "BBB"): [("M1",)], ("BBB", "AAA"): [("M1",)]},
        links={"M1": [("AAA", "MID"), ("MID", "BBB")]},
    )
    assert g.permits("AAA", "BBB", ["AAA", "MID", "BBB"]) is True
    assert g.permits("BBB", "AAA", ["BBB", "MID", "AAA"]) is False


def test_a_chain_of_maps_is_walked_in_order():
    g = guide(
        points=["AAA", "CCC"], nodes=["AAA", "BBB", "CCC"],
        routes={("AAA", "CCC"): [("M1", "M2")]},
        links={"M1": [("AAA", "BBB")], "M2": [("BBB", "CCC")]},
    )
    assert g.permits("AAA", "CCC", ["AAA", "BBB", "CCC"]) is True


def test_a_pair_the_guide_does_not_list_gives_no_verdict():
    """None is "nothing to say", and must not be read as a refusal."""
    g = guide(points=["AAA", "BBB"], routes={})
    assert g.permits("AAA", "BBB", ["AAA", "BBB"]) is None


def test_an_unknown_station_gives_no_verdict():
    g = guide(points=["AAA"])
    assert g.permits("ZZZ", "AAA", ["ZZZ", "AAA"]) is None


def test_a_journey_through_london_is_checked_as_two_halves():
    """No single route covers York to Penzance; the guide splits at London."""
    g = guide(
        points=["YRK", "PNZ", "KGX", "PAD"],
        nodes=["YRK", "KGX", "PAD", "PNZ"],
        routes={
            ("YRK", "KGX"): [("EC",)],
            ("PAD", "PNZ"): [("WC",)],
            # Nothing joins York to Penzance directly.
        },
        links={"EC": [("YRK", "KGX")], "WC": [("PAD", "PNZ")]},
        cross_london=["KGX", "PAD"],
    )
    assert g.permits("YRK", "PNZ", ["YRK", "KGX", "PAD", "PNZ"]) is True


def test_a_london_transfer_is_only_allowed_at_permitted_stations():
    g = guide(
        points=["YRK", "PNZ", "KGX", "PAD"],
        nodes=["YRK", "KGX", "PAD", "PNZ"],
        routes={("YRK", "KGX"): [("EC",)], ("PAD", "PNZ"): [("WC",)]},
        links={"EC": [("YRK", "KGX")], "WC": [("PAD", "PNZ")]},
        cross_london=[],  # neither terminal permits the transfer
    )
    # With nothing listed for the pair and no transfer available, the guide has
    # no opinion — which must not be read as a refusal.
    assert g.permits("YRK", "PNZ", ["YRK", "KGX", "PAD", "PNZ"]) is None


# --- easements ---------------------------------------------------------------
#
# The published exceptions. A positive easement grants a route the maps refuse;
# a negative one withdraws a route they allow. Not applying them is not the
# conservative choice it looks like — it errs both ways.

import datetime as dt

from rail.model.routeing import Easement

MONDAY = dt.date(2026, 8, 3)
SUNDAY = dt.date(2026, 8, 2)
ALL_DAYS = (True,) * 7


def easement(ref, *, grants, unsettleable=False, route_codes=(), ticket_codes=(),
             origins=(), destinations=(), applicable=(), via=(), excluded=(),
             doubleback=(), tocs=(), days=ALL_DAYS, start=None, end=None):
    return Easement(
        ref=ref, grants=grants, unsettleable=unsettleable,
        route_codes=frozenset(route_codes), ticket_codes=frozenset(ticket_codes),
        start_date=start, end_date=end, days=days,
        origins=frozenset(origins), destinations=frozenset(destinations),
        applicable=frozenset(applicable), via=frozenset(via),
        excluded=frozenset(excluded), doubleback=frozenset(doubleback),
        tocs=frozenset(tocs),
    )


def refusing_guide(**kwargs):
    """A guide that refuses AAA to BBB via CCC or EEE on the maps alone.

    Both are nodes but neither is on map M1, so a journey calling at either is
    off the permitted route. A station that is not a node at all — DDD — would
    simply be ignored, and the maps would permit the journey.
    """
    return guide(
        points=["AAA", "BBB"], nodes=["AAA", "MID", "BBB", "CCC", "EEE"],
        routes={("AAA", "BBB"): [("M1",)]},
        links={"M1": [("AAA", "MID"), ("MID", "BBB")]},
    )


def permitting_guide():
    return guide(
        points=["AAA", "BBB"], nodes=["AAA", "MID", "BBB"],
        routes={("AAA", "BBB"): [("M1",)]},
        links={"M1": [("AAA", "MID"), ("MID", "BBB")]},
    )


def test_a_positive_easement_grants_a_route_the_maps_refuse():
    g = refusing_guide()
    path = ["AAA", "CCC", "BBB"]
    assert g.permits("AAA", "BBB", path) is False

    g.easements = [easement("1", grants=True, origins=["AAA"], destinations=["BBB"])]
    assert g.permits("AAA", "BBB", path, date=MONDAY) is True


def test_a_negative_easement_withdraws_a_route_the_maps_allow():
    """This is why not applying easements was never the safe option."""
    g = permitting_guide()
    path = ["AAA", "MID", "BBB"]
    assert g.permits("AAA", "BBB", path) is True

    g.easements = [easement("1", grants=False, origins=["AAA"], destinations=["BBB"])]
    assert g.permits("AAA", "BBB", path, date=MONDAY) is False


def test_easements_are_ignored_without_a_date():
    """Every easement carries validity dates and days, so a dateless question
    cannot be answered by one."""
    g = permitting_guide()
    g.easements = [easement("1", grants=False, origins=["AAA"], destinations=["BBB"])]

    assert g.permits("AAA", "BBB", ["AAA", "MID", "BBB"]) is True


def test_an_easement_out_of_season_does_not_apply():
    g = permitting_guide()
    g.easements = [easement(
        "1", grants=False, origins=["AAA"], destinations=["BBB"],
        start=dt.date(2020, 1, 1), end=dt.date(2020, 12, 31),
    )]
    assert g.permits("AAA", "BBB", ["AAA", "MID", "BBB"], date=MONDAY) is True


def test_an_easement_that_does_not_run_today_does_not_apply():
    sundays_only = (False,) * 6 + (True,)
    g = permitting_guide()
    g.easements = [easement("1", grants=False, origins=["AAA"],
                            destinations=["BBB"], days=sundays_only)]

    assert g.permits("AAA", "BBB", ["AAA", "MID", "BBB"], date=MONDAY) is True
    assert g.permits("AAA", "BBB", ["AAA", "MID", "BBB"], date=SUNDAY) is False


def test_a_via_location_must_be_on_the_journey():
    g = refusing_guide()
    g.easements = [easement("1", grants=True, origins=["AAA"],
                            destinations=["BBB"], via=["CCC"])]

    assert g.permits("AAA", "BBB", ["AAA", "CCC", "BBB"], date=MONDAY) is True
    # Off the map by way of EEE instead, which the easement does not cover.
    assert g.permits("AAA", "BBB", ["AAA", "EEE", "BBB"], date=MONDAY) is False


def test_an_excluded_location_stops_the_easement_applying():
    g = refusing_guide()
    g.easements = [easement("1", grants=True, origins=["AAA"],
                            destinations=["BBB"], excluded=["CCC"])]

    assert g.permits("AAA", "BBB", ["AAA", "CCC", "BBB"], date=MONDAY) is False


def test_several_origins_are_alternatives_not_requirements():
    """An easement listing six origins applies to a journey from any of them."""
    g = refusing_guide()
    g.easements = [easement("1", grants=True, origins=["AAA", "ZZZ"],
                            destinations=["BBB"])]

    assert g.permits("AAA", "BBB", ["AAA", "CCC", "BBB"], date=MONDAY) is True


def test_a_negative_easement_beats_a_positive_one():
    """The guide does not say which wins. Refusing is the answer that cannot
    sell someone a ticket they may not use."""
    g = refusing_guide()
    g.easements = [
        easement("1", grants=True, origins=["AAA"], destinations=["BBB"]),
        easement("2", grants=False, origins=["AAA"], destinations=["BBB"]),
    ]
    assert g.permits("AAA", "BBB", ["AAA", "CCC", "BBB"], date=MONDAY) is False


def test_a_conditional_easement_leaves_the_verdict_unknown():
    """Its condition is the ticket, the train or the traveller — none of which a
    list of calling points can settle. Applying it would be a guess; ignoring it
    would let a published exception silently do nothing."""
    g = refusing_guide()
    g.easements = [easement("1", grants=True, unsettleable=True,
                            origins=["AAA"], destinations=["BBB"])]

    assert g.permits("AAA", "BBB", ["AAA", "CCC", "BBB"], date=MONDAY) is None


def test_an_easement_tied_to_an_operator_is_judged_on_the_trains_caught():
    """**RGH is where the operator conditions live**, and it was read by
    nothing. RGF's own `D` records give eight easements an operator; RGH gives
    942, one of which is in both — so the guide was deciding on eight easements
    where the feed describes 624 of the ones held here, and applying the other
    616 to every journey regardless of who ran the trains.

    The router already collects the operator of every leg for RGK's own `T`/`X`
    conditions, so this is a question the engine can answer, and a question it
    can answer is not an unknown. Only the train UID stays unsettleable."""
    g = refusing_guide()
    g.easements = [easement("1", grants=True, tocs=["TP"],
                            origins=["AAA"], destinations=["BBB"])]
    path = ["AAA", "CCC", "BBB"]

    # Caught the operator it names: the easement applies and grants the route.
    assert g.permits("AAA", "BBB", path, date=MONDAY, operators={"TP"}) is True
    # One of several counts, exactly as its station lists do.
    assert g.permits("AAA", "BBB", path, date=MONDAY,
                     operators={"XC", "TP"}) is True
    # A different operator: it does not apply, so the maps stand and refuse.
    assert g.permits("AAA", "BBB", path, date=MONDAY, operators={"XC"}) is False


def test_not_knowing_the_operators_leaves_an_operator_easement_open():
    """The same guard RGK's TOC conditions needed. A caller with a path and no
    trains would otherwise have every RGH easement silently withdrawn from it —
    which for a positive easement means a refusal nobody asked for."""
    g = refusing_guide()
    g.easements = [easement("1", grants=True, tocs=["TP"],
                            origins=["AAA"], destinations=["BBB"])]

    assert g.permits("AAA", "BBB", ["AAA", "CCC", "BBB"], date=MONDAY) is None


def test_a_conditional_positive_easement_does_not_unsettle_a_permitted_route():
    """It can only ever add permission, so it says nothing new about a journey
    the maps already allow. Treating it as doubt made 1,059 of York's 2,828
    destinations unknown for no reason."""
    g = permitting_guide()
    g.easements = [easement("1", grants=True, unsettleable=True,
                            origins=["AAA"], destinations=["BBB"])]

    assert g.permits("AAA", "BBB", ["AAA", "MID", "BBB"], date=MONDAY) is True


def test_a_conditional_negative_easement_does_unsettle_a_permitted_route():
    g = permitting_guide()
    g.easements = [easement("1", grants=False, unsettleable=True,
                            origins=["AAA"], destinations=["BBB"])]

    assert g.permits("AAA", "BBB", ["AAA", "MID", "BBB"], date=MONDAY) is None


def test_a_conditional_negative_easement_cannot_make_a_refusal_worse():
    g = refusing_guide()
    g.easements = [easement("1", grants=False, unsettleable=True,
                            origins=["AAA"], destinations=["BBB"])]

    assert g.permits("AAA", "BBB", ["AAA", "CCC", "BBB"], date=MONDAY) is False


def test_an_easement_mentioning_no_station_on_the_journey_is_skipped():
    """The index that keeps the sweep fast must not change any answer."""
    g = permitting_guide()
    g.easements = [easement("1", grants=False, origins=["ZZZ"],
                            destinations=["YYY"])]

    assert g.permits("AAA", "BBB", ["AAA", "MID", "BBB"], date=MONDAY) is True


# --- knowing which fare is being asked about ----------------------------------


def test_a_ticket_route_condition_is_settled_by_the_fare(fares_route="00000"):
    """Most easements left open are open only because they say "customers with
    tickets routed X". York to Glasgow Queen Street is the real case: the fastest
    journey runs via Haymarket, easement 701719 forbids exactly that on tickets
    routed 00000, and the cheapest fare is routed 00000."""
    g = permitting_guide()
    g.easements = [easement("1", grants=False, route_codes=["00000", "00353"],
                            origins=["AAA"], destinations=["BBB"])]
    path = ["AAA", "MID", "BBB"]

    assert g.permits("AAA", "BBB", path, date=MONDAY) is None
    assert g.permits("AAA", "BBB", path, date=MONDAY, route_code="00000") is False


def test_a_fare_on_another_route_is_not_touched_by_the_easement():
    """The route code settles it in both directions: an easement naming ticket
    routes does not apply to a ticket routed otherwise."""
    g = permitting_guide()
    g.easements = [easement("1", grants=False, route_codes=["00000"],
                            origins=["AAA"], destinations=["BBB"])]

    assert g.permits("AAA", "BBB", ["AAA", "MID", "BBB"],
                     date=MONDAY, route_code="00049") is True


def test_a_ticket_code_condition_is_settled_the_same_way():
    g = refusing_guide()
    g.easements = [easement("1", grants=True, ticket_codes=["SDS"],
                            origins=["AAA"], destinations=["BBB"])]
    path = ["AAA", "CCC", "BBB"]

    assert g.permits("AAA", "BBB", path, date=MONDAY) is None
    assert g.permits("AAA", "BBB", path, date=MONDAY, ticket_code="SDS") is True
    assert g.permits("AAA", "BBB", path, date=MONDAY, ticket_code="SOR") is False


def test_a_train_condition_stays_unsettleable_whatever_the_fare():
    """Detail types 1 and 2 name a train UID and an operator. Neither is
    knowable from a fare, and the router records neither per leg."""
    g = permitting_guide()
    g.easements = [easement("1", grants=False, unsettleable=True,
                            origins=["AAA"], destinations=["BBB"])]

    assert g.permits("AAA", "BBB", ["AAA", "MID", "BBB"],
                     date=MONDAY, route_code="00000", ticket_code="SDS") is None


# --- enumerating routings ----------------------------------------------------


def test_routings_lists_the_points_a_chain_passes_through():
    g = guide(
        points=["AAA", "CCC"], nodes=["AAA", "BBB", "CCC"],
        routes={("AAA", "CCC"): [("M1", "M2")]},
        links={"M1": [("AAA", "BBB")], "M2": [("BBB", "CCC")]},
    )
    found = g.routings("AAA", "CCC")

    assert [r.maps for r in found] == [("M1", "M2")]
    assert found[0].points == ["AAA", "BBB", "CCC"]


def test_a_london_routing_has_no_single_path():
    """RSPS5047 4.8.1.3: a route shown as LONDON is the single map LO, which
    carries only six Thameslink links and cannot be walked end to end. The
    journey is validated as two halves with a transfer between."""
    g = guide(points=["AAA", "CCC"], routes={("AAA", "CCC"): [("LO",)]})
    found = g.routings("AAA", "CCC")

    assert found[0].via_london
    assert found[0].points == []


def test_the_shortest_walk_is_taken_not_every_path():
    """The busiest maps carry 180 links; enumerating every simple path would
    not terminate usefully."""
    g = guide(
        points=["AAA", "DDD"], nodes=["AAA", "BBB", "CCC", "DDD"],
        routes={("AAA", "DDD"): [("M1",)]},
        links={"M1": [("AAA", "BBB"), ("BBB", "DDD"),
                      ("AAA", "CCC"), ("CCC", "BBB")]},
    )
    assert g.routings("AAA", "DDD")[0].points == ["AAA", "BBB", "DDD"]


def test_a_pair_the_guide_does_not_list_has_no_routings():
    g = guide(points=["AAA", "BBB"], routes={})
    assert g.routings("AAA", "BBB") == []


def test_a_group_routeing_point_is_named_by_its_main_station():
    """G02 means nothing to anyone; RGG says Birmingham New Street."""
    g = guide(points=["G02"], groups={"AST": "G02"})
    g.group_main = {"G02": "BHM"}

    assert g.main_station("G02") == "BHM"
    assert g.main_station("YRK") == "YRK"


# --- section 7.1: permitted before any map is consulted ----------------------


def test_a_through_train_is_permitted_outright():
    """RSPS5047 7.1.1: "If there is no change of train at any intermediate
    location on the journey, then the journey is on a through train and is
    permitted. No further checks are required."

    This was not implemented, so every journey was judged by the maps alone —
    which is strictly harsher than the guide. From Manchester it settles 85
    destinations the maps had no opinion on.
    """
    # A pair the maps refuse outright.
    g = guide(points=["AAA", "BBB"], routes={}, links={})

    assert g.permits("AAA", "BBB", ["AAA", "XXX", "BBB"]) is None
    assert g.permits("AAA", "BBB", ["AAA", "XXX", "BBB"], changes=0) is True


def test_a_journey_with_a_change_is_not_permitted_by_that_rule_alone():
    g = guide(points=["AAA", "BBB"], routes={}, links={})

    assert g.permits("AAA", "BBB", ["AAA", "XXX", "BBB"], changes=1) is None


def test_the_shortest_route_is_permitted_outright():
    """7.1.2/7.1.3: the shortest distance by rail, or within 3 miles of it, is
    permitted with no further checks. This is why RGD had to be parsed."""
    links = [("AAA", "MID", 2.0), ("MID", "BBB", 3.0), ("AAA", "LNG", 20.0),
             ("LNG", "BBB", 20.0)]
    g = guide(points=["AAA", "BBB"], routes={}, links={}, station_links=links)

    # Five miles by the direct line, and the journey takes it.
    assert g.permits("AAA", "BBB", ["AAA", "MID", "BBB"], changes=2) is True
    # Forty miles the long way round is well outside the margin, so the maps
    # decide — and here they say nothing.
    assert g.permits("AAA", "BBB", ["AAA", "LNG", "BBB"], changes=2) is None


def test_without_station_links_the_shortest_route_rule_never_fires():
    """The behaviour before RGD was parsed, and what a build with no routeing
    snapshot still does."""
    g = guide(points=["AAA", "BBB"], routes={}, links={})

    assert g.permits("AAA", "BBB", ["AAA", "MID", "BBB"], changes=2) is None


def test_the_blanket_permissions_never_turn_a_permission_into_a_refusal():
    """Section 7.1 only ever adds permissions, so being unable to answer it
    costs nothing — and a journey the maps permit stays permitted."""
    links = [("AAA", "LNG", 50.0), ("LNG", "BBB", 50.0)]
    g = guide(
        points=["AAA", "BBB"],
        routes={("AAA", "BBB"): [("M1",)]},
        links={"M1": [("AAA", "LNG"), ("LNG", "BBB")]},
        station_links=links,
    )

    assert g.permits("AAA", "BBB", ["AAA", "LNG", "BBB"], changes=1) is True


# --- RGX and RGY, the last two routeing files --------------------------------


def test_a_new_station_routes_as_the_station_rgx_names():
    """RSPS5047 4.14: a station built since NFM64 has no fares of its own in the
    guide's world, and the New Stations file names the older station to use —
    `LUT,LTN` means Luton Airport Parkway checks against Luton."""
    g = guide(points=["LUT", "BBB"], routes={("LUT", "BBB"): [("M1",)]},
              links={"M1": [("LUT", "BBB")]})
    g.equivalent_station = {"LTN": "LUT"}

    assert g._for_fares("LTN") == "LUT"
    # Without the substitution LTN has no routeing point and the maps are
    # silent; with it the journey is judged as if from Luton.
    assert g.permits("LTN", "BBB", ["LUT", "BBB"]) is True
    g.equivalent_station = {}
    assert g.permits("LTN", "BBB", ["LUT", "BBB"]) is None


def test_the_guides_own_mapping_beats_the_equivalence():
    """25 of the 30 stations new enough to be in RGX already have a routeing
    point, and that is the better answer where it exists — the equivalence is
    the fallback, not the rule."""
    g = guide(points=["AAA", "LUT"], station_points={"LTN": ["AAA"]})
    g.equivalent_station = {"LTN": "LUT"}

    assert g._for_fares("LTN") == "LTN"
    assert g.points_for("LTN") == ["AAA"]


def test_a_station_in_neither_stands_for_itself():
    g = guide(points=["AAA"])
    g.equivalent_station = {"LTN": "LUT"}

    assert g._for_fares("ZZZ") == "ZZZ"


def test_a_doubleback_target_counts_as_a_station_the_easement_names():
    """RSPS5047 4.10.3 modifier 6 names "the station to which a doubleback is
    allowed", with a NOTE promising a matching modifier-4 via record for the
    same station "for backwards compatibility".

    **That promise does not hold in this feed**: 83 of the 322 doubleback
    records have no via record for the same station. Easement 701612 permits a
    doubleback through Wimbledon and names Wimbledon nowhere else, so trusting
    the note drops the easement from every journey it governs — the index is
    built from the stations an easement names, and it would name none of them.
    """
    g = refusing_guide()
    g.easements = [easement("E1", grants=True, doubleback=["CCC"])]

    assert g._easements_touching("AAA", "BBB", ["AAA", "CCC", "BBB"]) == g.easements


def test_an_easement_naming_no_station_on_the_journey_is_not_considered():
    g = refusing_guide()
    g.easements = [easement("E1", grants=True, doubleback=["ZZZ"])]

    assert g._easements_touching("AAA", "BBB", ["AAA", "CCC", "BBB"]) == []
