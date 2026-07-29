"""PlusBus: bus travel around a station, sold as an add-on to a rail ticket.

Two feeds meet here, and neither is sufficient alone.

The **prices** are ordinary flows in the fares feed, from a station's own NLC to
a pseudo-location for its bus zone - Hucknall 1862 to `HUCKNALL+BUS` L102, a
PLUSBUS DAY at £5.40. 346 of those zone locations exist and 464 flows carry a
PlusBus fare. They never leak into ordinary pricing because the zone locations
carry no CRS, so `fare_alias` never names one as a destination.

The **rules** are in RSPS5052, under different licensing - see
:mod:`rail.acquire.supplementary`. A PlusBus add-on is only valid for a journey
*to or from* a zone, so it must not be sold when both ends of the rail journey
sit in the same one: Derby and Buxton are fine, Buxton and Matlock are not.
That list is a version history like everything else in these feeds - the file
ships two annual generations and half of it has expired - so it is filtered on
the travel date.

**The exclusion is reversible.** A record from A to B applies from B to A, and
the file does not carry both directions.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import duckdb

#: The zone locations describe themselves this way - "DERBY+BUS".
ZONE_MARKER = "%+BUS"


@dataclass
class PlusBusCounts:
    zones: int
    fares: int
    excluded_pairs: int
    web_pages: int


def build_plusbus(
    connection: duckdb.DuckDBPyConnection,
    fares_dir: Path,
    supplementary_dir: Path | None = None,
) -> PlusBusCounts:
    """Build plusbus_zone, plusbus_fare and the RSPS5052 rules beside them."""

    def path(name: str) -> str:
        return (fares_dir / f"{name}.parquet").as_posix()

    # A PlusBus flow runs from the station to its own bus zone, so the flow's
    # destination names the zone and its origin names the station.
    connection.execute(f"""
        create or replace table plusbus_fare as
        select n.crs,
               fl.origin_code as station_nlc,
               fl.destination_code as zone_nlc,
               trim(z.description) as zone_name,
               fa.ticket_code,
               trim(t.description) as description,
               fa.fare
        from read_parquet('{path("fare")}') fa
        join read_parquet('{path("flow")}') fl using (flow_id)
        join (
            select distinct nlc, description
            from read_parquet('{path("location")}')
            where upper(description) like '{ZONE_MARKER}'
              and current_date between start_date and end_date
        ) z on z.nlc = fl.destination_code
        join station_nlc n on n.nlc = fl.origin_code
        join ticket_type_current t on t.ticket_code = fa.ticket_code
        where current_date between fl.start_date and fl.end_date
          and fa.fare is not null and fa.fare > 0
    """)

    connection.execute("""
        create or replace table plusbus_zone as
        select crs, station_nlc, zone_nlc, min(zone_name) as zone_name,
               min(fare) filter (where description ilike 'PLUSBUS DAY') as day_fare
        from plusbus_fare
        group by crs, station_nlc, zone_nlc
    """)

    _load_rules(connection, supplementary_dir)

    scalar = lambda sql: connection.execute(sql).fetchone()[0]
    return PlusBusCounts(
        zones=scalar("select count(*) from plusbus_zone"),
        fares=scalar("select count(*) from plusbus_fare"),
        excluded_pairs=scalar("select count(*) from plusbus_excluded_pair"),
        web_pages=scalar("select count(*) from plusbus_web_page"),
    )


def _load_rules(
    connection: duckdb.DuckDBPyConnection, supplementary_dir: Path | None
) -> None:
    """The RSPS5052 half. Empty tables when it has not been fetched, so the
    queries below need no branch - and an empty exclusion list means nothing is
    excluded, which is the honest reading of "we do not know"."""
    for name, columns in (
        ("plusbus_excluded_pair",
         "start_date date, end_date date, from_nlc varchar, to_nlc varchar"),
        ("plusbus_web_page", "nlc varchar, url varchar"),
    ):
        source = (
            None if supplementary_dir is None
            else supplementary_dir / f"{name}.parquet"
        )
        if source is not None and source.exists():
            connection.execute(
                f"create or replace table {name} as "
                f"select * from read_parquet('{source.as_posix()}')"
            )
        else:
            connection.execute(f"create or replace table {name} ({columns})")


def zone_for(
    connection: duckdb.DuckDBPyConnection, crs: str
) -> dict | None:
    """The PlusBus zone at a station, with its fares and scheme page."""
    row = connection.execute(
        """
        select z.crs, z.zone_nlc, z.zone_name, z.station_nlc, p.url
        from plusbus_zone z
        left join plusbus_web_page p on p.nlc = z.zone_nlc
        where z.crs = $crs
        """,
        {"crs": crs},
    ).fetchone()
    if row is None:
        return None
    fares = connection.execute(
        """
        select ticket_code, description, fare
        from plusbus_fare where crs = $crs order by fare
        """,
        {"crs": crs},
    ).fetchall()
    return {
        "crs": row[0], "zone_nlc": row[1], "zone_name": row[2],
        "station_nlc": row[3], "url": row[4],
        "fares": [
            {"ticket_code": t, "description": d, "pence": f} for t, d, f in fares
        ],
    }


def may_sell_add_on(
    connection: duckdb.DuckDBPyConnection,
    origin: str,
    destination: str,
    travel_date: dt.date,
) -> bool | None:
    """May a PlusBus add-on be sold for a journey between these two?

    False when both ends sit in the same zone - the add-on would buy travel
    within one zone, which the product does not do. None when neither end has a
    zone at all, which is not a refusal but an absence.

    The exclusion list is checked in both directions: a record from A to B
    applies from B to A and the file carries only one of them.
    """
    zones = dict(connection.execute(
        "select crs, station_nlc from plusbus_zone where crs in ($a, $b)",
        {"a": origin, "b": destination},
    ).fetchall())
    if not zones:
        return None

    excluded = connection.execute(
        """
        select count(*) from plusbus_excluded_pair
        where $date between start_date and end_date
          and ((from_nlc = $a and to_nlc = $b)
               or (from_nlc = $b and to_nlc = $a))
        """,
        {"a": zones.get(origin), "b": zones.get(destination), "date": travel_date},
    ).fetchone()[0]
    return excluded == 0


def add_ons_from(
    connection: duckdb.DuckDBPyConnection,
    origin: str,
    travel_date: dt.date,
    *,
    ticket_code: str = "PBD",
) -> dict[str, int]:
    """The PlusBus add-on buyable at each destination, for one origin.

    Batched deliberately: `rail reachable` prices thousands of destinations at
    once and asking per station would dominate the query.

    Excludes anywhere the add-on may not be sold - the origin's own zone, and
    every pair RSPS5052 lists - so a destination missing from the result is one
    with no zone *or* one where the product does not apply. `PBD` is the day
    ticket, the only one that makes sense alongside a single journey.
    """
    rows = connection.execute(
        """
        with here as (
            select station_nlc, zone_nlc from plusbus_zone where crs = $origin
        ),
        barred as (
            select case when from_nlc = (select station_nlc from here)
                        then to_nlc else from_nlc end as station_nlc
            from plusbus_excluded_pair
            where $date between start_date and end_date
              and (from_nlc = (select station_nlc from here)
                   or to_nlc = (select station_nlc from here))
        )
        select f.crs, min(f.fare)
        from plusbus_fare f
        join plusbus_zone z on z.crs = f.crs
        where f.ticket_code = $ticket
          and f.crs <> $origin
          and z.station_nlc not in (select station_nlc from barred)
          -- Both ends in one zone buys travel within it, which is not the
          -- product. The exclusion list covers most of these, but a pair
          -- sharing a zone outright needs no list to be obvious.
          and (not exists (select 1 from here)
               or z.zone_nlc <> (select zone_nlc from here))
        group by f.crs
        """,
        {"origin": origin, "date": travel_date, "ticket": ticket_code},
    ).fetchall()
    return {crs: fare for crs, fare in rows}
