from pathlib import Path

from ..acquire import Feed, SnapshotStore
from ..config import Config
from .associations import AssociationCounts, build_associations, links_for
from .plusbus import (
    PlusBusCounts,
    add_ons_from,
    build_plusbus,
    may_sell_add_on,
    zone_for,
)
from .distance import SHORTEST_ROUTE_MARGIN_MILES, Distances
from .fares import (
    RETURN_TYPE,
    FaresCounts,
    build_fares_reference,
    cheapest_from,
    fare_options,
)
from .geo import LatLon, compare_with_naptan, grid_to_latlon, separation_metres
from .railcards import RailcardCounts, build_railcards, eligible_railcards
from .reference import ReferenceCounts, build_reference, classify_locations
from .routeing import RouteingCounts, RouteingGuide, build_routeing
from .restrictions import RestrictionCounts, applicable_bands, build_restrictions
from .returns import (
    ReturnWindow,
    ValidityCounts,
    build_ticket_validity,
    return_window,
    return_windows,
    returnable_on,
)
from .timetable import TimetableCounts, build_timetable
from .validate import Check, run_checks

__all__ = [
    "AssociationCounts",
    "Check",
    "Distances",
    "FaresCounts",
    "LatLon",
    "RETURN_TYPE",
    "SCHEMA_VERSION",
    "SHORTEST_ROUTE_MARGIN_MILES",
    "compare_with_naptan",
    "grid_to_latlon",
    "separation_metres",
    "RailcardCounts",
    "ReferenceCounts",
    "RouteingCounts",
    "RouteingGuide",
    "RestrictionCounts",
    "TimetableCounts",
    "build_associations",
    "build_fares_reference",
    "classify_locations",
    "applicable_bands",
    "build_railcards",
    "build_reference",
    "build_restrictions",
    "build_ticket_validity",
    "ReturnWindow",
    "ValidityCounts",
    "return_window",
    "return_windows",
    "returnable_on",
    "build_routeing",
    "build_timetable",
    "add_ons_from",
    "build_plusbus",
    "may_sell_add_on",
    "zone_for",
    "cheapest_from",
    "fare_options",
    "run_checks",
    "eligible_railcards",
    "links_for",
    "snapshot_parquet_dir",
]

# The contract with anything that queries the database directly.
#
# The tables this builds are a public interface: consumers issue their own SQL
# against `station`, `station_nlc`, `schedule_stop` and the rest rather than
# going through Python, so a renamed column breaks them with no import to
# catch it and no error until a query returns nothing. Bump this whenever a
# table or column that a consumer could reasonably read is renamed, dropped, or
# changes meaning — adding one is not a break. A consumer that pins it fails
# loudly on a mismatch, which is the only cheap way to make a silent breakage
# noisy.
SCHEMA_VERSION = 1


def snapshot_parquet_dir(config: Config, feed: Feed) -> Path:
    """Where the latest snapshot of `feed` was ingested to."""
    manifest = SnapshotStore(config.raw_dir).latest(feed)
    if manifest is None:
        raise RuntimeError(f"no {feed.value} snapshot — run `rail fetch`")
    path = config.parquet_dir / feed.value / Path(manifest.filename).stem
    if not path.exists():
        raise RuntimeError(f"{feed.value} snapshot not ingested — run `rail ingest`")
    return path
