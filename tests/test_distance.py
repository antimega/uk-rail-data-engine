"""Rail miles and straight lines.

The rail figures are a *rule*: RSPS5047 writes its shortest-route conditions
against the station-link file, and section 7.1 permits a journey outright on the
strength of them. The straight-line figures decide nothing and are here to ask
questions of the data. Keeping the two apart is the point.
"""

from __future__ import annotations

import duckdb
import pytest

from rail.model.distance import SHORTEST_ROUTE_MARGIN_MILES, Distances


@pytest.fixture
def world():
    """A short main line with a longer loop, plus grid references.

        A --2-- B --3-- C --2-- D          9 miles the direct way
         \\-------- E --------/            12 miles round the loop
    """

    def _build(links=None, grid=None):
        c = duckdb.connect()
        c.execute("create table station_link "
                  "(from_crs varchar, to_crs varchar, miles double)")
        default = [("A", "B", 2.0), ("B", "C", 3.0), ("C", "D", 2.0),
                   ("A", "E", 6.0), ("E", "D", 6.0)]
        for source, target, miles in (default if links is None else links):
            # RGD carries both directions and never disagrees with itself.
            c.execute("insert into station_link values (?, ?, ?)",
                      [source, target, miles])
            c.execute("insert into station_link values (?, ?, ?)",
                      [target, source, miles])
        c.execute("create table station (crs varchar, easting bigint, northing bigint)")
        # A and D are 8,046.72 m apart — exactly five miles.
        for crs, easting, northing in (grid if grid is not None else
                                       [("A", 0, 0), ("D", 8047, 0)]):
            c.execute("insert into station values (?, ?, ?)", [crs, easting, northing])
        return Distances.load(c)

    return _build


# --- rail distance -----------------------------------------------------------


def test_the_shortest_route_is_found_over_the_links(world):
    distances = world()

    assert distances.shortest_miles("A", "D") == pytest.approx(7.0)
    assert distances.shortest_miles("A", "A") == 0.0


def test_a_station_with_no_rail_path_returns_none(world):
    distances = world()

    assert distances.shortest_miles("A", "ZZZ") is None
    assert distances.shortest_miles("ZZZ", "A") is None


def test_one_scan_answers_every_destination(world):
    """A one-to-all sweep needs 2,600 distances, and Dijkstra gives them all at
    once — asking pair by pair would rescan the graph each time."""
    distances = world()

    assert distances.shortest_from("A") == pytest.approx(
        {"A": 0.0, "B": 2.0, "C": 5.0, "D": 7.0, "E": 6.0})


def test_a_journey_is_measured_between_its_calling_points(world):
    """The guide's links run between *adjacent* stations, but a journey calls at
    few of them — York to Newcastle calls at Darlington, not everywhere. So each
    consecutive pair is measured by its own shortest distance."""
    distances = world()

    # A fast train calling only at C on the way to D.
    assert distances.journey_miles(["A", "C", "D"]) == pytest.approx(7.0)
    # The slow way round, which is genuinely longer.
    assert distances.journey_miles(["A", "E", "D"]) == pytest.approx(12.0)


def test_an_unmeasurable_leg_makes_the_whole_journey_unmeasurable(world):
    """RSPS5047 6.1.6.2: bus, ferry and the Elizabeth Line stations carry no
    station links. A total that silently omits a leg would understate the
    journey, and the rule it feeds is a permission — so None, not a partial
    sum."""
    distances = world()

    assert distances.journey_miles(["A", "ZZZ", "D"]) is None


# --- the rule it exists for --------------------------------------------------


def test_the_shortest_route_is_permitted_outright(world):
    """RSPS5047 7.1.2, and it says so in as many words: "No further checks are
    required." Judging every journey by the maps alone is strictly harsher than
    the guide."""
    distances = world()

    assert distances.within_shortest_margin("A", "D", ["A", "C", "D"]) is True


def test_a_journey_within_three_miles_of_the_shortest_is_permitted(world):
    """7.1.3: "Currently the allowed margin is 3 miles." Stated as a current
    value rather than a constant, hence the named default."""
    distances = world()

    # 12 miles round the loop against a 7-mile shortest: five miles over.
    assert distances.within_shortest_margin("A", "D", ["A", "E", "D"]) is False
    # The same journey passes once the margin is wide enough to cover it.
    assert distances.within_shortest_margin(
        "A", "D", ["A", "E", "D"], margin=5.0) is True
    assert SHORTEST_ROUTE_MARGIN_MILES == 3.0


def test_a_journey_calling_twice_at_one_station_cannot_be_the_shortest(world):
    """7.2.4.2, stated separately from the arithmetic: "If the Local Journey
    includes the same location twice, the shortest route condition will not be
    satisfied." A doubleback is refused however short it is."""
    distances = world()

    assert distances.within_shortest_margin("A", "D", ["A", "B", "A", "B", "C", "D"]) is False


def test_an_unmeasurable_journey_gives_no_verdict(world):
    """None is not False. The caller falls through to the maps, and reading this
    as a refusal would withdraw journeys the guide says nothing against."""
    distances = world()

    assert distances.within_shortest_margin("A", "ZZZ", ["A", "ZZZ"]) is None
    assert distances.within_shortest_margin("A", "D", ["A", "ZZZ", "D"]) is None


# --- straight lines, which are not a rule ------------------------------------


def test_straight_line_distance_comes_from_the_grid(world):
    distances = world()

    assert distances.crow_flies_miles("A", "D") == pytest.approx(5.0, abs=0.01)


def test_a_station_with_no_grid_reference_has_no_straight_line(world):
    distances = world()

    assert distances.crow_flies_miles("A", "B") is None


def test_directness_compares_the_two(world):
    """1.0 would be a railway built along the straight line. Barton-on-Humber
    from York is 2.62, because the Humber is in the way."""
    distances = world()

    assert distances.directness("A", "D") == pytest.approx(7.0 / 5.0, abs=0.01)


def test_directness_is_absent_without_a_grid_reference(world):
    distances = world()

    assert distances.directness("A", "B") is None


def test_an_empty_link_table_is_falsy_rather_than_broken(world):
    """No routeing snapshot means no station links, and the shortest-route rule
    then simply never fires — which is the behaviour before RGD was parsed."""
    distances = world(links=[])

    assert not distances
    assert distances.shortest_miles("A", "D") is None
