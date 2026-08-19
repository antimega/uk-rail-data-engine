from dataclasses import dataclass
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
    SINGLE_TYPE,
    FaresCounts,
    build_fares_reference,
    cheapest_from,
    fare_options,
    travelcard_zone_codes,
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
from .tickets import (
    REGISTER,
    Review,
    accept as accept_tickets,
    classify as classify_tickets,
    review as review_tickets,
)
from .timetable import TimetableCounts, build_timetable
from .validate import Check, run_checks

__all__ = [
    "AssociationCounts",
    "BuildCounts",
    "build_all",
    "Check",
    "Distances",
    "FaresCounts",
    "LatLon",
    "RETURN_TYPE",
    "SINGLE_TYPE",
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
    "REGISTER",
    "Review",
    "accept_tickets",
    "classify_tickets",
    "fare_options",
    "travelcard_zone_codes",
    "review_tickets",
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
# changes meaning - adding one is not a break. A consumer that pins it fails
# loudly on a mismatch, which is the only cheap way to make a silent breakage
# noisy.
SCHEMA_VERSION = 2


@dataclass(frozen=True)
class BuildCounts:
    """What one full build produced, per stage."""

    reference: ReferenceCounts
    timetable: TimetableCounts
    kinds: dict
    fares: FaresCounts
    restrictions: RestrictionCounts
    validities: ValidityCounts
    railcards: RailcardCounts
    associations: AssociationCounts
    plusbus: PlusBusCounts
    routeing: RouteingCounts | None


def build_all(connection, config: Config, *, horizon_days: int = 90) -> BuildCounts:
    """Build every table, in the one order that satisfies the dependencies.

    **There is exactly one build sequence, and this is it.** `rail build` and
    `rail refresh` both call this rather than each listing the stages, because
    the second list drifted from the first and the failure was silent: refresh
    ran five of the ten stages, so an unattended run left `ticket_validity_current`
    with the six-column shape `build_fares_reference` writes as an intermediate,
    and `station.kind`, the associations, PlusBus and the whole routeing guide
    frozen at whatever the last manual `rail build` produced.

    Nothing errored, because every one of those tables still existed. It only
    surfaced when a query wanted a column that the losing writer does not
    produce - and by then the database had been wrong for as long as nobody had
    run `rail build` by hand.

    Order matters twice: `classify_locations` needs the timetable, since what a
    location *is* comes from what calls there, and `build_ticket_validity` must
    run after `build_fares_reference`, which writes the same table name as a
    narrower intermediate.
    """
    timetable_dir = snapshot_parquet_dir(config, Feed.TIMETABLE)
    fares_dir = snapshot_parquet_dir(config, Feed.FARES)

    # The three optional sources. Each is absent unless its own command has been
    # run, and each must be passed through on every build - dropping them is how
    # a refresh used to silently discard the corroborated station positions.
    def _optional(name: str) -> Path | None:
        path = config.parquet_dir / name
        return path if path.exists() else None

    supplementary_dir = _optional("supplementary")
    geography_dir = _optional("geography")
    naptan_dir = _optional("naptan")

    reference = build_reference(connection, timetable_dir, fares_dir,
                                supplementary_dir, geography_dir, naptan_dir)
    timetable = build_timetable(connection, timetable_dir,
                                horizon_days=horizon_days)
    kinds = classify_locations(connection)
    fares = build_fares_reference(connection, fares_dir, supplementary_dir)
    restrictions = build_restrictions(connection, fares_dir)
    validities = build_ticket_validity(connection, fares_dir)
    railcards = build_railcards(connection, fares_dir)
    associations = build_associations(connection, timetable_dir)
    plusbus = build_plusbus(connection, fares_dir, supplementary_dir)

    # The routeing guide is read from its ZIP rather than from Parquet - the
    # ingest marks its files spec-pending and writes none - so it is skipped
    # rather than failed when no snapshot has been fetched.
    store = SnapshotStore(config.raw_dir)
    manifest = store.latest(Feed.ROUTEING)
    routeing = (
        build_routeing(connection, store.path_for(manifest))
        if manifest is not None else None
    )

    return BuildCounts(
        reference=reference, timetable=timetable, kinds=kinds, fares=fares,
        restrictions=restrictions, validities=validities, railcards=railcards,
        associations=associations, plusbus=plusbus, routeing=routeing,
    )


def snapshot_parquet_dir(config: Config, feed: Feed) -> Path:
    """Where the latest snapshot of `feed` was ingested to."""
    manifest = SnapshotStore(config.raw_dir).latest(feed)
    if manifest is None:
        raise RuntimeError(f"no {feed.value} snapshot - run `rail fetch`")
    path = config.parquet_dir / feed.value / Path(manifest.filename).stem
    if not path.exists():
        raise RuntimeError(f"{feed.value} snapshot not ingested - run `rail ingest`")
    return path
