"""The station reference crosswalk.

This is the only place the timetable and fares halves meet, and it is the
highest-consequence table in the project: the timetable is keyed on TIPLOC, the
fares feed on NLC, and a person asks questions using CRS. Get the mapping wrong
and every downstream answer is quietly wrong rather than obviously broken.

Three shapes matter:

* **CRS to TIPLOC is one-to-many.** York is both YORK and YORKYSJ; Birmingham
  New Street is BHAMNWS and STADJN. Journey planning must accept a stop at any
  of them as a stop at the station.
* **The fares LOC file is versioned, not current.** London Euston appears many
  times with different validity windows. Only the record valid on the day being
  priced may be used.
* **Some reference rows are not stations at all** — an MSN header line, Irish
  CIE entries with no coordinates, and TIPLOCs that are timing points rather
  than places a passenger can board.

Everything excluded lands in ``reference_reject`` with a reason, so nothing
disappears silently and ``rail validate`` can report on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from .plusbus import ZONE_MARKER

# MSN encodes grid references as (true_easting / 100) + 10000 and
# (true_northing / 100) + 60000. A stored zero means "no coordinates", not the
# origin of the grid, and must not be decoded.
_EASTING = "case when easting = 0 then null else (easting - 10000) * 100 end"
_NORTHING = "case when northing = 0 then null else (northing - 60000) * 100 end"

_CRS_SHAPE = "^[A-Z]{3}$"


@dataclass
class ReferenceCounts:
    stations: int
    tiplocs: int
    priced: int
    clusters: int
    rejects: int


def _add_rail_station_flag(
    connection: duckdb.DuckDBPyConnection, supplementary_dir: Path | None
) -> None:
    """Mark which CRS codes RSPS5052 says are GB rail stations.

    MSN carries bus and ferry interchange points alongside stations — they are
    legitimate DTD data, reachable only by fixed link, and there is no field in
    MSN that tells them apart. This list does.

    RSPS5052 7.1.2 is explicit that the data is informational and must not
    affect journey planning or ticket selection, so it is a column to label
    output with, never a filter on the network: a journey may perfectly well
    route *through* a bus interchange. Null, not false, when the file is
    absent — "not fetched" and "not a station" are different answers.
    """
    connection.execute("alter table station add column is_rail_station boolean")
    listing = (
        None if supplementary_dir is None
        else supplementary_dir / "rail_station.parquet"
    )
    if listing is None or not listing.exists():
        return

    connection.execute(f"""
        update station set is_rail_station = crs in (
            select crs from read_parquet('{listing.as_posix()}')
        )
    """)


def build_reference(
    connection: duckdb.DuckDBPyConnection,
    timetable_dir: Path,
    fares_dir: Path,
    supplementary_dir: Path | None = None,
    geography_dir: Path | None = None,
    naptan_dir: Path | None = None,
) -> ReferenceCounts:
    """Build station, station_tiploc, station_nlc and station_cluster."""
    msn = (timetable_dir / "physical_station.parquet").as_posix()
    tiploc = (timetable_dir / "tiploc.parquet").as_posix()
    location = (fares_dir / "location.parquet").as_posix()
    cluster = (fares_dir / "station_cluster.parquet").as_posix()

    connection.execute("create or replace table reference_reject (source varchar, key varchar, reason varchar)")

    # --- what MSN offers, minus what isn't a station -------------------------
    connection.execute(f"""
        create or replace temp table msn_raw as
        select station_name, tiploc_code, crs_code, cate_interchange_status,
               {_EASTING} as easting, {_NORTHING} as northing,
               minimum_change_time
        from read_parquet('{msn}')
    """)

    # The MSN header line is itself an "A" record and parses into nonsense.
    connection.execute("""
        insert into reference_reject
        select 'msn', coalesce(tiploc_code, station_name), 'crs is not three letters'
        from msn_raw where crs_code is null or not regexp_matches(crs_code, '^[A-Z]{3}$')
    """)
    # One row per CRS. Prefer an entry that actually has coordinates, then take
    # the lowest TIPLOC so the choice is deterministic across rebuilds.
    connection.execute("""
        create or replace table station as
        with valid as (
            select * from msn_raw where regexp_matches(crs_code, '^[A-Z]{3}$')
        ), ranked as (
            select *, row_number() over (
                partition by crs_code
                order by (easting is null), tiploc_code
            ) as rn
            from valid
        )
        select crs_code as crs,
               station_name as name,
               easting, northing,
               minimum_change_time as interchange_minutes
        from ranked where rn = 1
        order by crs
    """)

    # Flag the station, not each MSN row: a station whose alternative TIPLOC
    # happens to lack coordinates is fine, one with none at all is not.
    connection.execute("""
        insert into reference_reject
        select 'msn', crs, 'no grid reference' from station where easting is null
    """)
    connection.execute(
        "alter table station add column grid_source varchar default 'msn'")

    _add_rail_station_flag(connection, supplementary_dir)

    # Every TIPLOC that belongs to a station, from MSN and from the timetable's
    # own TIPLOC records, so a stop at either York TIPLOC resolves to York.
    #
    # A TIPLOC must map to exactly one CRS or it duplicates rows downstream and
    # corrupts the lead()/lag() that builds connections. Around 38 TIPLOCs carry
    # both a real station CRS and an alias one that is not a station at all
    # (ABDARAR is both ABA Aberdare and XCB), so MSN wins.
    connection.execute(f"""
        create or replace temp table tiploc_ranked as
        with both_sources as (
            select crs_code as crs, tiploc_code as tiploc, 0 as source_rank
            from msn_raw where regexp_matches(crs_code, '^[A-Z]{{3}}$')
            union all
            select crs_code, tiploc_code, 1
            from read_parquet('{tiploc}')
            where crs_code is not null and regexp_matches(crs_code, '^[A-Z]{{3}}$')
        ),
        -- Most TIPLOCs appear in both files with the same CRS. Collapse those
        -- first, so only a genuinely conflicting CRS counts as ambiguous.
        candidates as (
            select crs, tiploc, min(source_rank) as source_rank
            from both_sources group by crs, tiploc
        )
        select crs, tiploc, row_number() over (
            partition by tiploc order by source_rank, crs
        ) as rn
        from candidates
    """)
    connection.execute("""
        create or replace table station_tiploc as
        select crs, tiploc from tiploc_ranked where rn = 1 order by crs, tiploc
    """)
    connection.execute("""
        insert into reference_reject
        select 'tiploc', tiploc || ' → ' || crs, 'ambiguous TIPLOC, kept the MSN station'
        from tiploc_ranked where rn > 1
    """)
    connection.execute("drop table tiploc_ranked")

    # --- the fares side: current NLC per station -----------------------------
    # LOC is a versioned history. Keep only records whose validity window covers
    # today, then the most recently started of those.
    connection.execute(f"""
        create or replace table station_nlc as
        with current_records as (
            select crs, nlc, uic, fare_group, start_date, end_date
            from read_parquet('{location}')
            where crs is not null and nlc is not null
              and regexp_matches(crs, '^[A-Z]{{3}}$')
              -- A PlusBus zone is not a place you can travel to. They used to
              -- carry no CRS at all, which is what the notes recorded as the
              -- reason they could never leak; the feed generation valid from
              -- 2026-06-30 gave four of them one — `QAB` BATH+BUS, `QAA`
              -- WESTON-S-M+BUS, `QAC` BRISTOLPWY+BUS, `QAD` BRISTOL TM+BUS —
              -- and Bristol Temple Meads promptly gained a £5.40 "destination"
              -- called BRISTOL TM+BUS. The zone marker is the same one
              -- `rail plusbus` matches on.
              and coalesce(description, '') not like '{ZONE_MARKER}'
              and current_date between start_date and end_date
        ), ranked as (
            select *, row_number() over (
                partition by crs order by start_date desc, uic
            ) as rn
            from current_records
        )
        select crs, nlc, uic, fare_group
        from ranked where rn = 1
        order by crs
    """)

    connection.execute(f"""
        create or replace table station_cluster as
        select distinct cluster_id, cluster_nlc
        from read_parquet('{cluster}')
        where current_date between start_date and end_date
    """)

    # A station in the timetable with no fares NLC cannot be priced. That is a
    # real limitation of an answer, not a parse failure, so it is recorded.
    connection.execute("""
        insert into reference_reject
        select 'fares', s.crs, 'no current fares NLC'
        from station s left join station_nlc n using (crs)
        where n.crs is null
    """)

    _refine_grid_references(connection, geography_dir, naptan_dir)

    scalar = lambda sql: connection.execute(sql).fetchone()[0]
    return ReferenceCounts(
        stations=scalar("select count(*) from station"),
        tiplocs=scalar("select count(*) from station_tiploc"),
        priced=scalar("select count(*) from station_nlc"),
        clusters=scalar("select count(*) from station_cluster"),
        rejects=scalar("select count(*) from reference_reject"),
    )


#: MSN rounds grid references to 100 m and the observed 90th-percentile
#: disagreement with the precise sources is 156 m, so a difference past a
#: kilometre is two sources naming different places rather than disagreeing on
#: detail. It is the corroboration threshold as well as the conflict one.
GRID_AGREEMENT_METRES = 1000


def _refine_grid_references(
    connection: duckdb.DuckDBPyConnection,
    geography_dir: Path | None,
    naptan_dir: Path | None = None,
) -> None:
    """Resolve a station's position from up to three independent sources.

    * **MSN**, from the timetable feed — about a kilometre accurate.
    * The **Network Rail FOI spreadsheet** — exact, but frozen and with errors
      of its own: it places Highbury & Islington 58 km away, in Kent.
    * **NaPTAN**, from DfT — exact and maintained.

    **Corroboration, not hierarchy.** No source is trusted on its own say-so; a
    position is taken when a second source agrees with it within a kilometre.
    That rule is what the evidence supports: NaPTAN differs from the FOI file by
    a median of 33 m over 2,488 stations and never by more than a kilometre, so
    where they agree either will do and the question is only which to prefer for
    precision. Where MSN and the FOI file *disagreed*, the earlier two-source
    merge kept MSN — and NaPTAN shows that was wrong in 16 of the 30 cases it
    can settle, Stansted Airport and Kirk Sandall among them.

    **Corroboration decides which position is right; precision decides which
    copy of it to keep.** Among corroborated candidates the FOI file wins, then
    NaPTAN, then MSN — because NaPTAN rounds 393 of its 2,765 rail stops to
    100 m where the FOI file rounds 1 of 9,397. So NaPTAN's job is to adjudicate
    and to cover what the FOI file misses, not to supply the final digits.

    Two subtleties that produced wrong answers first time round:

    * A station has several TIPLOCs and some are junctions. Pollokshaws West
      carries `BUSBYJ` as well as `PLKSHWW`, and taking the first match put it
      5 km away. The **nearest** candidate is the right one.
    * A station with only one source has nothing to check against, so its
      position is taken unchecked and `grid_source` says so.
    """
    sources: dict[str, str] = {}
    if geography_dir is not None and (geography_dir / "tiploc_grid.parquet").exists():
        sources["tiploc"] = (geography_dir / "tiploc_grid.parquet").as_posix()
    if naptan_dir is not None and (naptan_dir / "naptan_rail.parquet").exists():
        sources["naptan"] = (naptan_dir / "naptan_rail.parquet").as_posix()
    if not sources:
        return

    # One row per (station, source): the candidate nearest MSN, since MSN
    # already localises the station to within a kilometre and that is what
    # rejects a junction TIPLOC miles away.
    candidates = " union all ".join(
        f"""
        select st.crs, '{name}' as source, g.easting, g.northing,
               case when s.easting is null then null
                    else sqrt((g.easting - s.easting) ^ 2
                              + (g.northing - s.northing) ^ 2) end as from_msn
        from station_tiploc st
        join read_parquet('{path}') g on g.tiploc = st.tiploc
        join station s on s.crs = st.crs
        """
        for name, path in sources.items()
    )
    connection.execute(f"""
        create or replace temp table _grid_candidate as
        select * exclude (rn) from (
            select *, row_number() over (
                partition by crs, source order by from_msn nulls last
            ) as rn
            from ({candidates})
        ) where rn = 1
        union all
        -- MSN itself is a candidate, and on the conflicts it is right as often
        -- as not, so it stands with the others rather than merely anchoring.
        select crs, 'msn', easting, northing, 0.0
        from station where easting is not null
    """)

    # A candidate is corroborated when another source agrees within the
    # threshold. `supporters` counts the others, so 0 means uncorroborated.
    connection.execute(f"""
        create or replace temp table _grid_resolved as
        select * exclude (rn) from (
            select c.crs, c.source, c.easting, c.northing,
                   (select count(*) from _grid_candidate o
                    where o.crs = c.crs and o.source <> c.source
                      and sqrt((o.easting - c.easting) ^ 2
                               + (o.northing - c.northing) ^ 2)
                          <= {GRID_AGREEMENT_METRES}) as supporters,
                   row_number() over (
                       partition by c.crs
                       order by (select count(*) from _grid_candidate o
                                 where o.crs = c.crs and o.source <> c.source
                                   and sqrt((o.easting - c.easting) ^ 2
                                            + (o.northing - c.northing) ^ 2)
                                       <= {GRID_AGREEMENT_METRES}) desc,
                                -- Corroboration decides *which* position is
                                -- right; precision decides which copy of it to
                                -- keep. NaPTAN rounds 393 of its 2,765 rail
                                -- stops to 100 m where the FOI file rounds 1 of
                                -- 9,397, so the FOI value wins wherever a
                                -- second source has vouched for it.
                                case c.source when 'tiploc' then 0
                                              when 'naptan' then 1 else 2 end
                   ) as rn
            from _grid_candidate c
        ) where rn = 1
    """)

    connection.execute("""
        update station s
        set easting = r.easting,
            northing = r.northing,
            grid_source = case when r.supporters > 0 then r.source
                               else r.source || ' (uncorroborated)' end
        from _grid_resolved r
        where r.crs = s.crs
    """)

    # Stations where the sources disagree and none is corroborated: the position
    # used is a guess between them, so it is recorded rather than buried.
    connection.execute("""
        create or replace table station_grid_conflict as
        select r.crs, r.source as chosen_source, r.easting, r.northing,
               (select max(round(sqrt((o.easting - r.easting) ^ 2
                                      + (o.northing - r.northing) ^ 2)))
                from _grid_candidate o where o.crs = r.crs) as max_metres_apart
        from _grid_resolved r
        where r.supporters = 0
          and (select count(*) from _grid_candidate o where o.crs = r.crs) > 1
        order by max_metres_apart desc
    """)
    connection.execute("""
        insert into reference_reject
        select 'geography', crs,
               'grid references disagree and none is corroborated; '
               || 'see station_grid_conflict'
        from station_grid_conflict
    """)
    connection.execute("drop table if exists _grid_candidate")
    connection.execute("drop table if exists _grid_resolved")


#: Operators that run in the CIF timetable but are not National Rail. Curated,
#: like `NON_PUBLIC_MARKERS`, because the feeds do not draw this line anywhere:
#: the fares feed's TOC file lists all 86 operators alike, Tyne & Wear Metro
#: beside GWR. Each entry earns its place by evidence, and the list is short.
NON_NATIONAL_RAIL_OPERATORS: dict[str, str] = {
    # 21 stations reachable only by Metro — Fellgate, Stadium of Light, St
    # Peters, Seaburn. They are in CIF because Metro shares the network, not
    # because a National Rail train calls.
    "TW": "Tyne & Wear Metro",
    # Underground services on sections shared with National Rail.
    "LT": "London Underground",
}

#: CIF train status, RSPS5046. `B`/`5` is a bus and `S`/`4` a ship; everything
#: else is a train. The same mapping the router uses for RGK's mode conditions,
#: because a location's character and a fare's validity are asking the same
#: question of the same field.
_MODE_SQL = """
    case when sc.train_status in ('B', '5') then 'bus'
         when sc.train_status in ('S', '4') then 'ferry'
         else 'train' end
"""


def classify_locations(connection: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Say what each location *is*, from what actually calls there.

    MSN carries bus stops, ferry terminals and Metro stations alongside National
    Rail stations, and RSPS5052's list answers only "is this a rail station" —
    one boolean, which made a Metrolink stop, a coach bay and a ferry pier
    indistinguishable. The timetable answers it better, because it says what
    kind of service calls and who runs it.

    Requires `train_schedule` and `schedule_stop`, so it runs after the
    timetable is built rather than with the rest of the reference layer.

    **It agrees with RSPS5052 on every station RSPS5052 calls a rail station** —
    all 2,579 — which is the check worth having, since the two are derived from
    different files by different means. It then adds **30 more**: stations too
    new for the supplementary list, among them the whole Northumberland Line
    (Ashington, Bedlington, Blyth Bebside, Newsham, Seaton Delaval), Cambridge
    South, Beaulieu Park and the Camp Hill stations.

    Two of those 30 deserve a second look rather than trust: `WPK` Wimbledon
    Park and `ZPU` East Putney are Underground stations at which South Western
    Railway trains call on shared track. "A National Rail train stops here" is
    true of them and "this is a National Rail station" is not, and the timetable
    cannot tell the difference.

    RSPS5052 §7.1.2 forbids its own station list from affecting journey planning
    or ticket selection. This classification is derived from the timetable
    instead, so that restriction does not reach it — but the same discipline is
    kept anyway: `kind` labels output and filters nothing.
    """
    excluded = ", ".join(f"'{code}'" for code in NON_NATIONAL_RAIL_OPERATORS)
    connection.execute(f"""
        create or replace table station_service as
        select ss.crs, {_MODE_SQL} as mode, sc.atoc_code, count(*) as calls
        from train_schedule sc
        join schedule_stop ss using (schedule_id)
        where ss.crs is not null
        group by 1, 2, 3
    """)

    # Strict precedence: a place a train calls at is a station whatever else
    # also stops there, and a ferry terminal with a connecting bus is still a
    # ferry terminal.
    connection.execute(f"""
        create or replace table station_kind as
        select crs,
               case
                   when count(*) filter (
                       where mode = 'train' and atoc_code not in ({excluded})
                   ) > 0 then 'rail'
                   when count(*) filter (where mode = 'train') > 0 then 'metro'
                   when count(*) filter (where mode = 'ferry') > 0 then 'ferry'
                   when count(*) filter (where mode = 'bus') > 0 then 'bus'
                   else 'unserved'
               end as kind
        from station_service
        group by crs
    """)

    connection.execute("alter table station drop column if exists kind")
    connection.execute("alter table station add column kind varchar default 'unserved'")
    connection.execute("""
        update station s set kind = k.kind
        from station_kind k where k.crs = s.crs
    """)

    return dict(connection.execute(
        "select kind, count(*) from station group by 1 order by 2 desc"
    ).fetchall())
