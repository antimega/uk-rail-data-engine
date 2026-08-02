"""Standing data-quality report.

Every number here was checked by hand once, while building the thing it
measures. The point of the command is that they stay checked: the feeds refresh,
RSP changes things, and a silent shift in the data is far more dangerous than a
parse error. A weekday that stops looking busier than a Sunday means STP
resolution has broken, and nothing else in the pipeline would say so.

Checks come in three severities:

* ``fail`` - something is wrong and results cannot be trusted.
* ``warn`` - outside the expected band; worth a look, not necessarily broken.
* ``ok``   - as expected.

Bands are deliberately loose. They exist to catch a pipeline that has broken,
not to fail every time a train operator adds a service.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import duckdb

Severity = Literal["ok", "warn", "fail"]


@dataclass
class Check:
    category: str
    name: str
    status: Severity
    detail: str

    @property
    def failed(self) -> bool:
        return self.status == "fail"


def _band(value: float, low: float, high: float) -> Severity:
    return "ok" if low <= value <= high else "warn"


def run_checks(
    connection: duckdb.DuckDBPyConnection,
    timetable_dir: Path,
    fares_dir: Path,
    naptan_dir: Path | None = None,
) -> list[Check]:
    checks: list[Check] = []
    scalar = lambda sql, **kw: connection.execute(sql, kw).fetchone()[0]

    def add(category: str, name: str, status: Severity, detail: str) -> None:
        checks.append(Check(category, name, status, detail))

    def path(directory: Path, name: str) -> str:
        return (directory / f"{name}.parquet").as_posix()

    # --- referential integrity ----------------------------------------------
    # These are the checks that proved the field offsets were right in the first
    # place. If any starts failing, the parse has drifted.

    orphan_stops = scalar(f"""
        select count(*) from (
            select distinct location from read_parquet('{path(timetable_dir, "stop_time")}')
        ) s
        left join read_parquet('{path(timetable_dir, "tiploc")}') t
          on t.tiploc_code = s.location
        where t.tiploc_code is null
    """)
    add("integrity", "stop locations resolve to a TIPLOC",
        "ok" if orphan_stops == 0 else "fail",
        f"{orphan_stops:,} orphans")

    # **The second schedule file, and the way it fails is silent.** `ZTR` names
    # locations by CRS where the main file uses TIPLOCs, so a resolution that
    # drifts does not error - it produces stops with no station, which are then
    # filtered out, and the hovercraft, Red Funnel and the Metropolitan line
    # beyond Harrow simply stop existing. Nobody would notice: they were absent
    # for the whole life of the project before anyone asked.
    ztr_schedules = scalar(
        "select count(*) from train_schedule where source = 'ztr'")
    add("integrity", "the ZTR schedule file is loaded",
        "ok" if ztr_schedules > 0 else "fail",
        f"{ztr_schedules:,} schedules")

    ztr_unresolved = scalar("""
        select count(*) from schedule_stop ss
        join train_schedule s using (schedule_id)
        where s.source = 'ztr' and ss.is_public and ss.crs is null
    """)
    add("integrity", "every public ZTR stop resolves to a station",
        "ok" if ztr_unresolved == 0 else "fail",
        f"{ztr_unresolved:,} unresolved")

    # The crossing itself, named outright. It is the case that found the file,
    # and a check on the outcome rather than on the mechanism.
    hovercraft = scalar("""
        select count(*) from station_service
        where crs in ('XRD', 'SHV') and mode = 'ferry'
    """)
    add("integrity", "the Solent hovercraft is in the network",
        "ok" if hovercraft > 0 else "fail",
        f"{hovercraft} hoverport services")

    # Against every code the feed defines, not just the priceable stations:
    # fare_alias covers stations we can sell a ticket to, which is deliberately
    # narrower than the set of codes a flow may name.
    orphan_flows = scalar(f"""
        with known as (
            select distinct nlc as code from read_parquet('{path(fares_dir, "location")}')
            where nlc is not null
            union
            select distinct fare_group from read_parquet('{path(fares_dir, "location")}')
            where fare_group is not null
            union
            select distinct cluster_id
            from read_parquet('{path(fares_dir, "station_cluster")}')
        )
        select count(*) from read_parquet('{path(fares_dir, "flow")}') f
        where f.origin_code not in (select code from known)
           or f.destination_code not in (select code from known)
    """)
    add("integrity", "flow endpoints resolve to a code the feed defines",
        "ok" if orphan_flows == 0 else "fail",
        f"{orphan_flows:,} unresolvable")

    unpriceable = scalar(f"""
        select count(*) from read_parquet('{path(fares_dir, "flow")}') f
        where f.origin_code not in (select code from fare_alias)
           or f.destination_code not in (select code from fare_alias)
    """)
    total_flows = scalar(f"select count(*) from read_parquet('{path(fares_dir, 'flow')}')")
    add("integrity", "flows reaching a priceable station",
        _band(1 - unpriceable / total_flows, 0.7, 1.0),
        f"{total_flows - unpriceable:,} of {total_flows:,}; the rest name "
        "locations that are not National Rail stations")

    # The crosswalk between the two feeds is joined on CRS, and getting it
    # wrong corrupts everything downstream in silence. This tests it against
    # something CRS has nothing to do with: each feed's own location number.
    #
    # A full NLC is six digits (Swindon is 333300); booking offices drop the
    # trailing zeroes and quote 3333, which is the form the fares feed carries.
    # The timetable's TI records carry the full six. So for any station the two
    # feeds describe, the fares NLC should be the first four digits of the
    # timetable's NALCO - derived independently, from different files, by
    # different systems.
    #
    # Bus stops, Metrolink stops, airports and ferry terminals are excluded:
    # the fares feed gives those an alphanumeric NLC (Bowker Vale is G206)
    # against the timetable's numeric one, and they are not rail stations.
    # Without the RSPS5052 list there is no way to exclude them, so the check
    # loosens to a band rather than demanding the exact agreement it can get.
    known_kind = scalar("select count(*) from station where is_rail_station")
    compared, agreeing = connection.execute(f"""
        with ti as (
            select crs_code as crs, nalco,
                   row_number() over (
                       partition by crs_code
                       order by (right(nalco, 2) = '00') desc, nalco
                   ) as rn
            from read_parquet('{path(timetable_dir, "tiploc")}')
            where crs_code is not null and nalco is not null and nalco <> '000000'
        )
        select count(*), count(*) filter (where left(ti.nalco, 4) = n.nlc)
        from ti
        join station_nlc n using (crs)
        join station s using (crs)
        where ti.rn = 1 and s.is_rail_station is not false
    """).fetchone()
    if not compared:
        add("integrity", "fares NLC matches the timetable's own location number",
            "warn", "nothing to compare")
    elif known_kind:
        add("integrity", "fares NLC matches the timetable's own location number",
            "ok" if agreeing == compared else "fail",
            f"{agreeing:,} of {compared:,} rail stations agree")
    else:
        add("integrity", "fares NLC matches the timetable's own location number",
            _band(agreeing / compared, 0.9, 1.0),
            f"{agreeing:,} of {compared:,} agree; non-rail locations are not "
            "excluded - run `rail fetch --supplementary` to tighten this")

    # Three separate bugs have come from the same cause: the description field
    # is 15 characters, so the word the classifier matches on arrives truncated.
    # "UPGRADE" became "UPG" and 17 supplements were priced as first-class
    # fares; "CARNET OFFPK 5" was quoted as a single. This asserts the outcome
    # rather than the rule - no walk-up fare should describe itself with a stem
    # that means a bundle or a supplement.
    # Every stem here earns its place from a product that slipped through and
    # priced a journey absurdly. The last three came from hand-checking Euston
    # to Birmingham against a retailer: 48 group products declaring one
    # passenger, 13 concessionary fares, and `SF3 SUPERFARE` at £9.00 against a
    # cheapest real Advance of £31.00.
    # "ONBOARD" is the fourth way of naming a supplement without naming one:
    # `25Q STDPREM ONBOARD` is bought from the crew on a ticket you already
    # hold, and neither the flat-rate test nor the booked-train rule can see it.
    bundle_stems = ("UPG", "CARNET", "FLXIPASS", "DAYSAVE", "FLEXIDAY",
                    "SUPPLEMENT", "SUPPLMNT", "GROUP", "GRP", "CONC", "ONBOARD",
                    # Age conditions, written as a ticket type rather than as a
                    # discount, so nothing else in the classifier sees them.
                    "YOUTH", "CHILD", "16-18", "SCHOL")
    leaked = connection.execute(f"""
        select list(distinct ticket_code)
        from ticket_type_current
        where is_walk_up and ({
            " or ".join(f"upper(description) like '%{stem}%'"
                        for stem in bundle_stems)
        })
    """).fetchone()[0] or []
    # The restriction says what the validity does not. LNER's Simpler Fares
    # `70min Flex` carries validity `61` "ON DATE SHOWN" and restriction `FL`,
    # "LNER FLEX ON SET TIME"; Greater Anglia's Seatfrog Secret Fare carries
    # `OA`, "LER ADVANCE". Both were quoted as walk-up fares, and from King's
    # Cross a `70min Flex` single made two singles look £100 cheaper than the
    # return. Operators keep inventing these, so this asserts the outcome: no
    # walk-up type may have *every* one of its fares tied to a booked train.
    from .fares import _BOOKED_TRAIN_SQL

    bound = connection.execute(f"""
        with booked as (
            select distinct restriction_code
            from read_parquet('{path(fares_dir, "restriction_header")}')
            where cf_mkr = 'C' and ({_BOOKED_TRAIN_SQL})
        )
        select list(distinct ticket_code) from (
            select f.ticket_code
            from read_parquet('{path(fares_dir, "fare")}') f
            join ticket_type_current t using (ticket_code)
            where t.is_walk_up and f.fare > 0
            group by f.ticket_code
            having count(*) = count(*) filter (
                where f.restriction_code in (select restriction_code from booked))
        )
    """).fetchone()[0] or []
    add("fares", "no walk-up fare is tied to a booked train on every flow",
        "ok" if not bound else "fail",
        "none" if not bound
        else f"{len(bound)}: {', '.join(sorted(bound)[:8])}")

    add("fares", "no bundle, supplement, group or concession as a walk-up fare",
        "ok" if not leaked else "fail",
        "none" if not leaked else f"{len(leaked)}: {', '.join(sorted(leaked)[:8])}")

    # The two structural flags, asserted as an outcome for the same reason as
    # the stems above: the classifier reads them, and nothing else would notice
    # if a build stopped. Unlike the stems these are the feed's own statements
    # of fact, so a failure here means the parse has drifted rather than that an
    # operator has invented a new product name.
    from .fares import NOT_A_PACKAGE, NO_RESERVATION

    structural = connection.execute(f"""
        select list(distinct ticket_code) from ticket_type_current
        where is_walk_up
          and (coalesce(reservation_required, '{NO_RESERVATION}')
                   <> '{NO_RESERVATION}'
               or coalesce(package_mkr, '{NOT_A_PACKAGE}') <> '{NOT_A_PACKAGE}')
    """).fetchone()[0] or []
    add("fares", "no walk-up fare requires a reservation or is a package",
        "ok" if not structural else "fail",
        "none" if not structural
        else f"{len(structural)}: {', '.join(sorted(structural)[:8])}")

    # The narrow Advance class, asserted as an outcome. `is_advance_fare` is a
    # residual - sellable and not a walk-up - so it collects things that are not
    # Advance tickets at all, and `is_real_advance` is what `--advance-only`
    # prices against. Both halves of that are worth guarding.
    #
    # The first is the structural rule restated: a fare needing no reservation,
    # carrying no booked-train restriction and not calling itself an Advance is
    # not one. `GTS ANYTIME S` reached the map through exactly that gap - 205
    # fares, not one with a restriction, on a validity described "AS ADVERTISED"
    # whose `out_description` reads `BOOKDTRAINONLY`.
    loose = connection.execute(f"""
        select list(distinct ticket_code) from ticket_type_current
        where is_real_advance
          and coalesce(reservation_required, '{NO_RESERVATION}')
              = '{NO_RESERVATION}'
          and upper(description) not like '%ADV%'
    """).fetchone()[0] or []
    add("fares", "no real Advance is sellable without a reservation",
        "ok" if not loose else "fail",
        "none" if not loose else f"{len(loose)}: {', '.join(sorted(loose)[:8])}")

    # And the second: the marker list, guarded the way the walk-up stems are.
    # A failure here means an operator has named a product in a new way, not
    # that the parse has drifted - so it is the list that wants arguing with.
    from .fares import PSEUDO_ADVANCE_MARKERS

    stems = " or ".join(f"upper(description) like '{pattern}'"
                        for pattern, _ in PSEUDO_ADVANCE_MARKERS)
    pseudo = connection.execute(f"""
        select list(distinct ticket_code) from ticket_type_current
        where is_real_advance and ({stems})
    """).fetchone()[0] or []
    add("fares", "no retailer scheme, rover or swap counts as a real Advance",
        "ok" if not pseudo else "fail",
        "none" if not pseudo else f"{len(pseudo)}: {', '.join(sorted(pseudo)[:8])}")

    # The classes must partition what is sellable, and the narrow one must sit
    # inside the broad one. Cheap to check and it would catch a fourth state
    # appearing by accident, which is how a residual class goes wrong.
    overlap = connection.execute("""
        select count(*) from ticket_type_current
        where (is_walk_up and is_advance_fare)
           or (is_real_advance and not is_advance_fare)
           or (is_sellable <> (is_walk_up or is_advance_fare))
    """).fetchone()[0]
    add("fares", "walk-up and Advance partition the sellable types",
        "ok" if not overlap else "fail",
        "clean" if not overlap else f"{overlap} contradictory rows")

    # Ticket types nobody has looked at since the last generation.
    #
    # **A warn, not a fail**, and the distinction matters here more than
    # anywhere else in this file. A new generation legitimately ships new
    # products, so failing would stop the scheduled refresh on an ordinary
    # Tuesday; but a new product lands in the *wrong* class silently and
    # immediately wins, the wrong class being nearly always the cheaper one.
    # `rail tickets --review` is where it gets acted on, and that exits 1.
    #
    # Only the ones already carrying fares are counted. A code an operator has
    # registered without filing prices cannot be wrong about anything yet.
    from .tickets import review as review_tickets

    unreviewed = review_tickets(connection, fares_dir).carrying_fares()
    add("fares", "no unreviewed ticket type is already pricing journeys",
        "ok" if not unreviewed else "warn",
        "none" if not unreviewed
        else f"{len(unreviewed)}: {', '.join(unreviewed[:8])}"
             " - run `rail tickets --review`")

    # A PlusBus zone is an add-on to a journey, not a place you can travel to.
    # The original note recorded that they carry no CRS and so could never be
    # named as a destination - true when written, and the feed generation valid
    # from 2026-06-30 gave four of them one, at which point Bristol Temple Meads
    # gained a £5.40 "destination" called BRISTOL TM+BUS. The exclusion is now
    # explicit in `station_nlc` and in `fare_alias`; this asserts the outcome,
    # because the failure is silent and looks like an ordinary cheap fare.
    from .plusbus import ZONE_MARKER

    zones = connection.execute(f"""
        select list(distinct a.crs) from fare_alias a
        where a.crs in (
            select crs from read_parquet('{path(fares_dir, "location")}')
            where crs is not null and description like '{ZONE_MARKER}'
              and current_date between start_date and end_date)
    """).fetchone()[0] or []
    add("fares", "no PlusBus zone is priceable as a destination",
        "ok" if not zones else "fail",
        "none" if not zones else f"{len(zones)}: {', '.join(sorted(zones)[:8])}")

    # The rule is selected by measurement, not by the feed, so its content is
    # worth watching: if RSP changes rule 01 away from 5p, every discounted
    # fare moves and nothing else here would notice.
    bands = connection.execute(
        "select distinct round_to from rounding_band where upper_limit > 1"
    ).fetchall()
    units = sorted(row[0] for row in bands)
    add("fares", "the selected rounding rule still rounds to 5p",
        "ok" if units == [1, 5] else "warn",
        f"bands round to {', '.join(str(u) for u in units)}p"
        if units else "no rounding bands loaded")

    # A Day Return that stopped being a same-day ticket, or a weekend return
    # that lost its weekday rule, would quietly widen what the tool says you may
    # do with a ticket you have bought. Both are single fields in TVL.
    day_returns = scalar("""
        select count(*) from ticket_return_kind k
        join ticket_type_current t using (ticket_code)
        where t.is_walk_up and k.return_kind = 'same_day'
    """)
    add("fares", "day returns still come back the same day",
        "ok" if day_returns > 0 else "fail",
        f"{day_returns:,} walk-up same-day returns")

    # Three codes out of a hundred carry it, and it is the only thing that makes
    # a weekend return a weekend return.
    weekend = connection.execute("""
        select distinct v.validity_code, v.ret_after_day
        from ticket_validity_current v
        where v.ret_after_day is not null
        order by 1
    """).fetchall()
    add("fares", "the weekend-return day rule is still carried",
        "ok" if weekend else "warn",
        ", ".join(f"{code} {day}" for code, day in weekend) or "no code carries one")

    # The polarity of CHANGE_IND is worth watching: read backwards it would
    # withdraw fares on 803 restrictions instead of 36, and every multi-leg
    # journey would price as an Anytime. A handful barring a change is the
    # shape the feed has always had.
    barred = scalar("""
        select count(*) from restriction_current
        where cf_mkr = 'C' and not change_allowed
    """)
    allowed = scalar("""
        select count(*) from restriction_current
        where cf_mkr = 'C' and change_allowed
    """)
    # 36 of 839 is 4.3%; inverted it would be 95.7%, so any threshold between
    # the two separates them. 15% leaves room for the feed to change without
    # crying wolf.
    share = barred / (barred + allowed) if barred + allowed else 0
    add("fares", "few restrictions bar a change of trains",
        "ok" if 0 < share < 0.15 else "warn",
        f"{barred:,} bar it, {allowed:,} allow it ({share:.1%})")

    # RSPS5045 4.19.6 lets an HC record name stations where a change is allowed
    # despite the bar. This feed ships none, so a bar has no exceptions - if any
    # appear, refusing every change becomes too strict.
    exceptions = 0
    if (fares_dir / "restriction_header_change.parquet").exists():
        exceptions = scalar(
            f"select count(*) from read_parquet("
            f"'{path(fares_dir, 'restriction_header_change')}')")
    add("fares", "no restriction names an allowed change station",
        "ok" if exceptions == 0 else "warn",
        "none, so a change bar has no exceptions"
        if exceptions == 0 else f"{exceptions:,} HC records now present")

    # Positions come from up to three independent sources and are taken only
    # when a second corroborates. A rise in uncorroborated positions means a
    # source has gone missing or drifted - the FOI grid file in particular is a
    # frozen snapshot that `rail refresh` drops.
    corroborated, positioned = connection.execute("""
        select count(*) filter (where grid_source not like '%uncorroborated%'),
               count(*)
        from station where easting is not null
    """).fetchone()
    add("integrity", "station positions are corroborated by a second source",
        _band(corroborated / positioned, 0.7, 1.0) if positioned else "warn",
        f"{corroborated:,} of {positioned:,}")

    unresolved = scalar("""
        select count(*) from station_grid_conflict
    """) if connection.execute(
        "select count(*) from duckdb_tables() where table_name = "
        "'station_grid_conflict'").fetchone()[0] else 0
    add("integrity", "sources agree on where each station is",
        "ok" if unresolved <= 10 else "warn",
        f"{unresolved} unresolved" if unresolved else "no disagreement left")

    # Everything here stores positions as OS grid metres; the map draws latitude
    # and longitude. NaPTAN publishes both for the same stop, so converting its
    # grid reference and comparing with its own lat/lon measures the transform
    # and nothing else - one source, one position, two representations.
    #
    # The band is a tripwire rather than a tolerance: the real figure is 0.19 m,
    # and the failure this catches is the datum shift silently going missing,
    # which takes the median to 113 m and would still return coordinates that
    # look entirely plausible.
    from .geo import (
        CONVERSION_MEDIAN_LIMIT_METRES,
        NAPTAN_SELF_AGREEMENT_METRES,
        compare_with_naptan,
    )

    conversion = compare_with_naptan(naptan_dir)
    if not conversion:
        add("integrity", "grid references convert to latitude and longitude",
            "warn", "NaPTAN not fetched - run `rail naptan` to check the transform")
    else:
        add("integrity", "grid references convert to latitude and longitude",
            "ok" if conversion.median_metres < CONVERSION_MEDIAN_LIMIT_METRES
            else "fail",
            f"median {conversion.median_metres:.2f} m over {conversion.stops:,} "
            f"NaPTAN stops")
        # Two stops disagree with themselves by more than 5 m, which is NaPTAN's
        # own inconsistency and not something the transform can fix. Reported so
        # a jump in the count is visible.
        add("suspicious", "NaPTAN stops whose own grid and lat/lon disagree",
            "ok" if len(conversion.outliers) <= 10 else "warn",
            f"{len(conversion.outliers)} past {NAPTAN_SELF_AGREEMENT_METRES:.0f} m"
            + (f" - worst {conversion.outliers[0][0]} at "
               f"{conversion.outliers[0][1]:.0f} m" if conversion.outliers else ""))

    # Two independent answers to "is this a rail station": RSPS5052's list, and
    # what actually calls there. They are derived from different files by
    # different means, so agreement is worth asserting - and disagreement in one
    # direction only is the expected shape, since a new station reaches the
    # timetable before it reaches the supplementary list.
    contradicted, extra = connection.execute("""
        select count(*) filter (where is_rail_station and kind <> 'rail'),
               count(*) filter (where is_rail_station = false and kind = 'rail')
        from station where is_rail_station is not null
    """).fetchone()
    # RGX independently names the stations built since NFM64, so it can vouch
    # for most of the extras - and its silence is informative too: Wimbledon
    # Park and East Putney are absent from it, which fits their being
    # Underground stations rather than new ones.
    vouched = connection.execute("""
        select count(*) from station s
        where s.is_rail_station = false and s.kind = 'rail'
          and exists (select 1 from routeing_new_station x where x.crs = s.crs)
    """).fetchone()[0] if connection.execute(
        "select count(*) from duckdb_tables() where table_name = "
        "'routeing_new_station'").fetchone()[0] else 0
    add("integrity", "the timetable agrees a rail station is a rail station",
        "ok" if contradicted == 0 else "fail",
        f"{contradicted:,} contradicted; {extra:,} rail by the timetable and "
        f"not on the RSPS5052 list, {vouched:,} of them named by RGX")

    # **A location named for an operator and a direction is not a station.**
    # MSN carries `CH ORIGIN`, `SWR DESTINATION` and ten more like them, which
    # is how a rail-replacement working names an endpoint it does not have.
    # Every one was classified `rail` on two to six calls until `marker` was
    # added, and they were counted among the stations "too new for the RSPS5052
    # list" - a claim this file printed and nothing checked.
    #
    # The outcome is asserted rather than the rule, as elsewhere: a failure
    # here means one has slipped back into a real class, not that an operator
    # has invented a new name. Names rather than the ZTR test, deliberately -
    # two independent signals, and agreement between them is the check.
    named = connection.execute(
        "select count(*) from duckdb_columns() where table_name = 'station' "
        "and column_name = 'name'").fetchone()[0]
    misnamed = connection.execute("""
        select count(*) from station
        where kind not in ('marker', 'unserved')
          and (name like '% ORIGIN' or name like '% DESTINATION')
    """).fetchone()[0] if named else 0
    markers = scalar("select count(*) from station where kind = 'marker'")
    add("integrity", "no operator marker is classified as a place",
        "ok" if misnamed == 0 else "fail",
        f"{markers:,} markers set aside; {misnamed:,} named for an operator "
        f"and a direction but classified as somewhere you can go")

    # A third opinion on the crosswalk, from the routeing feed. The fares NLC
    # is already checked against the timetable's NALCO; RGY states CRS against
    # NLC directly, from a third file produced by a third process. All 3,430
    # agree today, on the fare group as well as the NLC.
    xref = connection.execute("""
        select count(*), count(*) filter (where x.nlc = n.nlc),
               count(*) filter (where x.fare_group = n.fare_group)
        from routeing_location x
        join station_nlc n using (crs)
        where x.crs is not null
          and current_date between x.start_date and x.end_date
    """).fetchone() if connection.execute(
        "select count(*) from duckdb_tables() where table_name = "
        "'routeing_location'").fetchone()[0] else (0, 0, 0)
    compared, same_nlc, same_group = xref
    if not compared:
        add("integrity", "the routeing feed agrees on CRS to NLC",
            "warn", "no routeing snapshot loaded")
    else:
        add("integrity", "the routeing feed agrees on CRS to NLC",
            "ok" if same_nlc == compared and same_group == compared else "fail",
            f"{same_nlc:,} of {compared:,} on the NLC, {same_group:,} on the "
            "fare group")

    orphan_tickets = scalar(f"""
        select count(*) from read_parquet('{path(fares_dir, "fare")}') f
        where f.ticket_code not in (select ticket_code from ticket_type_current)
    """)
    add("integrity", "fares carry a known ticket code",
        "ok" if orphan_tickets == 0 else "fail",
        f"{orphan_tickets:,} unknown")

    orphan_dates = scalar("""
        select count(*) from service_date d
        left join train_schedule s using (schedule_id)
        where s.schedule_id is null
    """)
    add("integrity", "running dates resolve to a schedule",
        "ok" if orphan_dates == 0 else "fail", f"{orphan_dates:,} orphans")

    tiploc_dupes = scalar("""
        select count(*) from (
            select tiploc from station_tiploc group by tiploc having count(*) > 1
        )
    """)
    add("integrity", "each TIPLOC maps to one station",
        "ok" if tiploc_dupes == 0 else "fail",
        f"{tiploc_dupes:,} ambiguous - duplicates corrupt the connection set")

    # Two independent grouping systems that answer different questions and are
    # not expected to agree - see "Two grouping systems" in
    # docs/INTERPRETING-THE-FEEDS.md. What must hold
    # is that neither is ambiguous and their code spaces stay apart: a fares
    # group is a 4-character NLC, a routeing group is Gnn, and a code leaking
    # from one into the other would expand ticket validity or permitted routes
    # without anything else noticing.
    group_dupes = scalar("""
        select
            (select count(*) from (
                select crs from station_nlc group by crs
                having count(distinct fare_group) > 1))
          + (select count(*) from (
                select crs from station_group_member group by crs
                having count(distinct group_code) > 1))
    """)
    add("integrity", "a station belongs to at most one group of each kind",
        "ok" if group_dupes == 0 else "fail",
        f"{group_dupes:,} stations in two groups of the same kind")

    leaked = scalar("""
        select
            (select count(*) from fare_alias
             where regexp_matches(code, '^G[0-9]{2}$'))
          + (select count(*) from station_group_member
             where not regexp_matches(group_code, '^G[0-9]{2}$'))
    """)
    add("integrity", "fares and routeing group codes stay in their own space",
        "ok" if leaked == 0 else "fail",
        f"{leaked:,} codes in the wrong namespace")

    # --- timetable ----------------------------------------------------------

    backwards = scalar("""
        with d as (
            select schedule_id,
                   coalesce(arrival_minutes, departure_minutes) as at_stop,
                   lag(coalesce(departure_minutes, arrival_minutes)) over (
                       partition by schedule_id order by seq
                   ) as previous
            from schedule_stop where is_public
        )
        select count(distinct schedule_id) from d where at_stop < previous
    """)
    add("timetable", "journeys move forwards in time",
        "ok" if backwards == 0 else "fail",
        f"{backwards:,} schedules go backwards - check the overnight unwrapping")

    cancelled_with_stops = scalar("""
        select count(*) from train_schedule s
        where s.stp_indicator = 'C'
          and exists (select 1 from schedule_stop ss where ss.schedule_id = s.schedule_id)
    """)
    add("timetable", "cancellations carry no stops",
        "ok" if cancelled_with_stops == 0 else "warn",
        f"{cancelled_with_stops:,} with stops")

    by_day = dict(connection.execute("""
        select case when dayofweek(sd.date) = 0 then 'sunday'
                    when dayofweek(sd.date) = 6 then 'saturday'
                    else 'weekday' end as kind,
               count(*) / count(distinct sd.date) as per_day
        from service_date sd join train_schedule s using (schedule_id)
        where s.is_passenger group by 1
    """).fetchall())
    weekday = by_day.get("weekday", 0)
    saturday = by_day.get("saturday", 0)
    sunday = by_day.get("sunday", 0)

    add("timetable", "weekday passenger trains per day",
        _band(weekday, 20_000, 30_000), f"{weekday:,.0f}")
    add("timetable", "Sunday is quieter than a weekday",
        "ok" if 0 < sunday < weekday else "fail",
        f"Sunday {sunday:,.0f} vs weekday {weekday:,.0f}")
    add("timetable", "Saturday sits between the two",
        "ok" if sunday < saturday <= weekday else "warn",
        f"Saturday {saturday:,.0f}")

    joined = scalar("""
        select count(*) from association_link
    """) if connection.execute(
        "select count(*) from duckdb_tables() where table_name = 'association_link'"
    ).fetchone()[0] else 0
    add("timetable", "trains joined or split",
        _band(joined, 1, 200_000) if joined else "warn",
        f"{joined:,} links" if joined else "none - associations not built")

    # RSPS5046 5.4.12: a location may occur up to nine times on one schedule,
    # distinguished by a suffix, and 5.5.8.1 fields 11-12 let an association
    # name which occurrence it happens at. `association_link` joins on the
    # TIPLOC alone, so a suffixed association is ambiguous whenever the
    # schedule really does call there more than once.
    #
    # **It is safe today, and by luck rather than by design.** All 53 suffixed
    # records are the sleeper at Edinburgh, whose two calls are adjacent with
    # the first non-public - and the unlock/board station is the nearest
    # *public* call, so both candidates resolve to the same answer. Two public
    # calls at one TIPLOC would not, and would cross-product the links. This
    # watches for that rather than carrying the suffix through the build for a
    # case that does not yet exist.
    ambiguous = scalar(f"""
        with suffixed as (
            select distinct assoc_uid, assoc_location
            from read_parquet('{path(timetable_dir, "association")}')
            where assoc_location_suffix is not null
        )
        select count(*) from (
            select s.schedule_id, x.assoc_location
            from suffixed x
            join train_schedule s on s.train_uid = x.assoc_uid
            join schedule_stop ss on ss.schedule_id = s.schedule_id
                                 and ss.location = x.assoc_location
            where ss.is_public
            group by 1, 2 having count(*) > 1
        )
    """) if (
        (timetable_dir / "association.parquet").exists()
        and connection.execute(
            "select count(*) from duckdb_tables() "
            "where table_name in ('train_schedule', 'schedule_stop')"
        ).fetchone()[0] == 2
    ) else None
    if ambiguous is not None:
        add("timetable", "no association is ambiguous about which call it joins at",
            "ok" if ambiguous == 0 else "warn",
            "none" if ambiguous == 0
            else f"{ambiguous:,} schedules call publicly twice "
                 "where an association names a suffix")

    horizon = connection.execute(
        "select min(date), max(date), count(distinct date) from service_date"
    ).fetchone()
    add("timetable", "running dates cover the horizon",
        "ok" if horizon[2] and horizon[2] > 30 else "warn",
        f"{horizon[0]} → {horizon[1]} ({horizon[2]:,} days)")

    # --- fares --------------------------------------------------------------

    walk_up = scalar("select count(*) from ticket_type_current where is_walk_up")
    total_tickets = scalar("select count(*) from ticket_type_current")
    share = walk_up / total_tickets if total_tickets else 0
    # The band moved down when booked-train-only fares were reclassified as
    # Advance: 1,379 walk-up types became 1,020, which is 30%. Then again when
    # the feed's own `RESERVATION_REQUIRED` and `PACKAGE_MKR` flags were read,
    # taking 951 to 850 - 25%. It is a floor against the classification quietly
    # collapsing, not a target, and every step down so far has been a batch of
    # products that were never walk-up fares. Expect to lower it again; what
    # would be alarming is a *jump*, which no single exclusion has ever caused.
    add("fares", "walk-up share of ticket types",
        _band(share, 0.20, 0.6), f"{walk_up:,} of {total_tickets:,} ({share:.0%})")

    median_fare = scalar(f"""
        select median(f.fare) from read_parquet('{path(fares_dir, "fare")}') f
        join ticket_type_current t using (ticket_code)
        where t.is_walk_up and t.tkt_class = 2 and f.fare > 0
    """) or 0
    add("fares", "median standard walk-up fare",
        _band(median_fare, 1_000, 20_000), f"£{median_fare / 100:,.2f}")

    zero_fares = scalar(f"""
        select count(*) from read_parquet('{path(fares_dir, "fare")}') f
        join ticket_type_current t using (ticket_code)
        where t.is_walk_up and f.fare = 0
    """)
    add("fares", "no zero-priced walk-up fares",
        "ok" if zero_fares == 0 else "warn", f"{zero_fares:,}")

    advance = scalar("select count(*) from ticket_type_current where is_advance_fare")
    add("fares", "Advance price points exist",
        _band(advance, 100, 3_000), f"{advance:,} ticket types")

    overlap = scalar("""
        select count(*) from ticket_type_current
        where is_walk_up and is_advance_fare
    """)
    add("fares", "walk-up and Advance do not overlap",
        "ok" if overlap == 0 else "fail", f"{overlap:,} in both")

    # The bug this guards: ITX and tour-operator rates are nominal 5p package
    # prices, and single-flow ones slip past the flat-rate test because with one
    # flow the modal share is trivially 1.0.
    implausible = scalar(f"""
        select count(distinct t.ticket_code)
        from read_parquet('{path(fares_dir, "fare")}') f
        join ticket_type_current t using (ticket_code)
        where t.is_advance_fare and t.tkt_class = 2 and f.fare between 1 and 99
    """)
    add("fares", "no sub-pound Advance fares",
        "ok" if implausible == 0 else "warn",
        f"{implausible:,} ticket types priced under £1 - check ITX and placeholders")

    cheapest_advance = scalar(f"""
        select min(f.fare) from read_parquet('{path(fares_dir, "fare")}') f
        join ticket_type_current t using (ticket_code)
        where t.is_advance_fare and t.tkt_class = 2 and f.fare > 0
    """) or 0
    add("fares", "cheapest Advance fare is plausible",
        _band(cheapest_advance, 100, 1_000), f"£{cheapest_advance / 100:,.2f}")

    railcard_pct = connection.execute("""
        select min(discount_percentage), max(discount_percentage)
        from railcard_discount
    """).fetchone()
    sane = railcard_pct[0] is not None and 0 < railcard_pct[0] and railcard_pct[1] <= 1000
    add("fares", "railcard discounts are per mille",
        "ok" if sane else "fail",
        f"{railcard_pct[0]}–{railcard_pct[1]} per mille "
        f"({(railcard_pct[1] or 0) / 10:.1f}% maximum)")

    # A railcard band barring travel all day, every day, at every station and
    # naming no operator is not a restriction - it is the railcard not
    # existing. Two such bands do exist and both name operators: `RD` (Annual
    # Gold Card) says LNER and Avanti, `R5` (16-17 Saver) says ScotRail and
    # Caledonian Sleeper. The outcome is asserted rather than the rule, because
    # losing the TT join withdraws a railcard from the entire network in
    # silence and the symptom is an ordinary-looking undiscounted fare. See
    # `_band_toc_applies` in model/fares.py.
    unqualified = scalar("""
        select count(*)
        from restriction_band b
        join railcard_restriction rr using (restriction_code)
        where b.cf_mkr = 'C' and b.out_ret = 'O'
          and not b.min_fare_flag and b.location is null
          and b.time_from <= 1 and b.time_to >= 1439
          and not exists (
              select 1 from restriction_band_toc t
              where t.cf_mkr = b.cf_mkr
                and t.restriction_code = b.restriction_code
                and t.sequence_no = b.sequence_no
                and t.out_ret = b.out_ret
          )
    """)
    add("fares", "no railcard is barred all day with no operator named",
        "ok" if unqualified == 0 else "fail",
        f"{unqualified:,} all-day bars carrying no TOC qualifier")

    # --- what was excluded, and why -----------------------------------------

    for reason, count in connection.execute(
        "select reason, count(*) from reference_reject group by 1 order by 2 desc"
    ).fetchall():
        add("excluded", f"reference: {reason}", "ok", f"{count:,}")

    for reason, count in connection.execute(
        "select reason, count(*) from fare_reject group by 1 order by 2 desc"
    ).fetchall():
        add("excluded", f"ticket types: {reason}", "ok", f"{count:,}")

    # --- things that look like test data ------------------------------------

    suspicious = connection.execute("""
        select count(*) filter (where upper(description) like '%TEST%'),
               count(*) filter (where upper(description) like '%DUMMY%'),
               count(*) filter (where upper(description) like '%DO NOT USE%')
        from ticket_type_current
    """).fetchone()
    add("suspicious", "ticket types naming themselves test data", "ok",
        f"{suspicious[0]:,} TEST, {suspicious[1]:,} DUMMY, {suspicious[2]:,} DO NOT USE")

    no_coordinates = scalar("select count(*) from station where easting is null")
    add("suspicious", "stations without a grid reference",
        _band(no_coordinates, 0, 200), f"{no_coordinates:,}")

    open_ended = scalar(f"""
        select count(*) from read_parquet('{path(fares_dir, "flow")}')
        where end_date = date '2999-12-31'
    """)
    add("suspicious", "flows with the open-ended date sentinel", "ok",
        f"{open_ended:,} - expected, 31122999 means no end date")

    return checks
