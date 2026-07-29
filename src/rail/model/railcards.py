"""Railcard discounts.

The chain is indirect. A railcard (RLC) names a passenger *status* — the 16-25
Railcard is status 003. A ticket type carries a *discount category*. The two
together index into DIS, which gives a percentage: status 003 against any of the
common categories yields 334, and a child yields 500.

**That percentage is per mille, not per cent.** 334 is 33.4% off, which is the
familiar railcard third; 500 is half fare. Reading it as a percentage would
price a £60 fare at £39.80 instead of £39.96 — close enough to look right and
wrong on every single fare.

Three things then modify the discounted price:

* **Railcard minimum fares** (RCM), per railcard and ticket type. A discount
  that falls below the minimum is lifted back up to it.
* **Rounding.** See ``ROUNDING_UNIT`` below.
* **Non-standard discounts** (FNS), where a particular flow does not attract the
  standard discount at all — 39,242 of the 326,231 records say so.

Eligibility matters too: a railcard states how many adults and children it
covers. Two Together requires two adults, so a solo traveller cannot use it.

**Known-weak data.** RSP's own guidance and long-standing community experience
is that the railcard fields in this feed contain errors. Treat discounted fares
as indicative and spot-check anything that matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

#: Discount percentages are per mille: 334 means 33.4% off.
PER_MILLE = 1000

#: Which of FRR's 36 rule sets applies. **Nothing in the feed says.** No field
#: in TTY, DIS or the status records carries a rule id, so the mapping the spec
#: implies cannot be read from the data — it is established by measurement
#: instead, against six fares checked with a retailer (see ROUNDING_UNIT).
#:
#: Only two rules reproduce all six, `01` and `M0`, and both are 5p across every
#: band, so they are indistinguishable on this evidence and either will do. Only
#: a *banded* rule would tell them apart, and only on a fare large enough to
#: cross a band — which is the test worth running if more examples turn up.
SELECTED_ROUNDING_RULE = "01"

#: Fares round *down* to the nearest 5p. The feed ships 36 rounding rule sets in
#: FRR, but nothing in TTY, DIS or the status records carries a rule id, so
#: which rule applies to a given discount cannot be determined from the data.
#: Rounding down to 5p is the documented industry convention. The parsed rules
#: are in `rounding_rule` for whenever the mapping is established.
#:
#: Kept as documentation of the observed behaviour; the arithmetic now walks
#: the bands of `SELECTED_ROUNDING_RULE`, which is 5p throughout.
#:
#: RSPS5045 4.18.1.1 says the discounted fare is rounded *up* to the rounding
#: amount. That is not what happens, and neither is "to the nearest": of the
#: three directions tested against the six known answers, only *down* fits, and
#: it fits under no rule but `01` and `M0`. Checked against a retailer for York to
#: Leeds, six fares with a 16-25 Railcard, all six rounding down and none up:
#: £20.10 -> £13.35 (up would be £13.40), £20.40 -> £13.55, £23.00 -> £15.30,
#: £24.10 -> £16.05, £32.30 -> £21.50, £20.20 -> £13.45. The same six confirm
#: the per-mille reading, since a 0.666 multiplier reproduces every one exactly.
ROUNDING_UNIT = 5

#: RSPS5045 4.17.3 field 5. The only discount_indicator meaning "take the
#: percentage off"; see `railcard_discount` for the six others.
PERCENTAGE_DISCOUNT = "0"

#: A railcard whose status is this covers nobody — the field is unused.
NO_STATUS = "XXX"


@dataclass
class RailcardCounts:
    railcards: int
    discounts: int
    minimum_fares: int
    no_discount_flows: int


def build_railcards(
    connection: duckdb.DuckDBPyConnection,
    fares_dir: Path,
) -> RailcardCounts:
    """Build railcard_current, railcard_discount and railcard_minimum."""

    def path(name: str) -> str:
        return (fares_dir / f"{name}.parquet").as_posix()

    # RLC is a version history like the rest of the feed.
    connection.execute(f"""
        create or replace table railcard_current as
        with ranked as (
            select *, row_number() over (
                partition by railcard_code order by start_date desc
            ) as rn
            from read_parquet('{path("railcard")}')
            where current_date between start_date and end_date
        )
        select railcard_code, description, adult_status, child_status,
               min_adults, max_adults, min_children, max_children,
               min_passengers, max_passengers,
               restricted_by_train, restricted_by_date, restricted_by_area,
               -- 100 of the 330 are internal TOC codes rather than railcards a
               -- passenger can hold.
               display_flag = 'Y' as is_public
        from ranked
        where rn = 1 and adult_status is not null and adult_status <> '{NO_STATUS}'
    """)

    # Where an area-restricted railcard may be used. RSPS5045 4.15.2 field 8:
    # "it can only be used in areas denoted by the Railcard Geography held in
    # the Locations file". 87 railcards are so flagged and the Locations file
    # carries 165,674 records across 88 of them — the Network Railcard has
    # 15,300 and the Annual Gold Card 17,301, two genuinely different areas.
    #
    # Without this a Network Railcard discounted York to Leeds, where it is not
    # valid at either end, and only its £13.00 minimum fare stopped the number
    # looking obviously wrong.
    connection.execute(f"""
        create or replace table railcard_geography as
        select distinct l.railcard_code, n.crs
        from read_parquet('{path("location_railcard")}') l
        join station_nlc n on n.uic = l.uic
        where current_date <= l.end_date
    """)

    # RSPS5045 4.19.18. A railcard restriction either bans the card outright for
    # a ticket, route or origin, or names a restriction code whose time bands
    # apply on top of the fare's own. The Network Railcard has both: 103 route
    # bans including GATWICK EXP ONLY and VIA UNDERGRD/DLR, and restriction RN,
    # which bars it 04:30-09:59 Mon-Fri and charges a minimum fare the rest of
    # the day.
    connection.execute(f"""
        create or replace table railcard_ban as
        select distinct railcard_code, ticket_code, route_code, location
        from read_parquet('{path("restriction_railcard")}')
        where total_ban and cf_mkr = 'C'
    """)
    connection.execute(f"""
        create or replace table railcard_restriction as
        select distinct railcard_code, restriction_code, ticket_code, route_code
        from read_parquet('{path("restriction_railcard")}')
        where not total_ban and restriction_code is not null and cf_mkr = 'C'
    """)

    # railcard x ticket discount category -> percentage off.
    #
    # RSPS5045 4.17.3 gives discount_indicator seven values and only '0' means
    # "take the percentage off". 'F' substitutes a flat fare, 'M' caps the
    # result, 'H' and 'L' floor it against the status record's own minima, and
    # 'X'/'N' mean no discount at all. Applying a percentage regardless would
    # price 'X' rows as a discount that does not exist.
    #
    # In RJFAF833 every 'X' and 'F' row carries a zero percentage, so the old
    # `> 0` test happened to exclude them and every surviving row is a '0'.
    # That is luck, not design: an 'L' row on a live railcard's status would
    # sail through and come out too cheap. Filter on the indicator itself and
    # let `unhandled_discount_rule` say what was set aside.
    connection.execute(f"""
        create or replace table railcard_discount as
        select r.railcard_code, d.discount_category,
               d.discount_indicator, d.discount_percentage
        from railcard_current r
        join read_parquet('{path("status_discount")}') d
          on d.status_code = r.adult_status
        where d.discount_percentage is not null
          and d.discount_percentage > 0
          and d.discount_indicator = '{PERCENTAGE_DISCOUNT}'
    """)

    # The flat/cap/floor rules we do not implement, kept visible rather than
    # dropped on the floor. Empty in RJFAF833.
    connection.execute(f"""
        create or replace table unhandled_discount_rule as
        select r.railcard_code, d.status_code, d.discount_category,
               d.discount_indicator, d.discount_percentage
        from railcard_current r
        join read_parquet('{path("status_discount")}') d
          on d.status_code = r.adult_status
        where d.discount_indicator <> '{PERCENTAGE_DISCOUNT}'
          and coalesce(d.discount_percentage, 0) > 0
    """)

    connection.execute(f"""
        create or replace table railcard_minimum as
        select railcard_code, ticket_code, min(minimum_fare) as minimum_fare
        from read_parquet('{path("railcard_minimum_fare")}')
        where current_date between start_date and end_date
        group by railcard_code, ticket_code
    """)

    # Flows where the standard discount does not simply apply.
    #
    # RSPS5045 4.5.2 field 11 reads the opposite way round to its name: 'N'
    # means the adult fare is calculated as normal and *this record can be
    # ignored*. The records that matter are the other three — a space (price
    # via use_nlc and add the add-on), 'X' (no adult fare at all) and 'D' (no
    # discounted adult fare). Selecting 'N' collected precisely the 39,242
    # rows with nothing to say.
    connection.execute(f"""
        create or replace table no_standard_discount as
        select distinct origin_code, destination_code, route_code,
               railcard_code, ticket_code, adult_nodis_flag,
               use_nlc, adult_add_on_amount, adult_rebook_flag
        from read_parquet('{path("non_standard_discount")}')
        where adult_nodis_flag is distinct from 'N'
          and current_date between start_date and end_date
    """)

    connection.execute(f"""
        create or replace table rounding_rule as
        select rule_id, sequence_no, upper_limit, round_to
        from read_parquet('{path("rounding_rule")}')
        where current_date between start_date and end_date
    """)

    # The bands actually applied. Nothing in the feed selects a rule, so it is
    # selected by measurement instead — see SELECTED_ROUNDING_RULE.
    connection.execute(f"""
        create or replace table rounding_band as
        select upper_limit, min(round_to) as round_to
        from rounding_rule
        where rule_id = '{SELECTED_ROUNDING_RULE}'
        group by upper_limit
    """)

    scalar = lambda sql: connection.execute(sql).fetchone()[0]
    return RailcardCounts(
        railcards=scalar("select count(*) from railcard_current"),
        discounts=scalar("select count(*) from railcard_discount"),
        minimum_fares=scalar("select count(*) from railcard_minimum"),
        no_discount_flows=scalar("select count(*) from no_standard_discount"),
    )


def eligible_railcards(
    connection: duckdb.DuckDBPyConnection,
    *,
    adults: int = 1,
    children: int = 0,
    public_only: bool = True,
) -> list[tuple[str, str]]:
    """Railcards a party of this shape may actually use."""
    return connection.execute(
        """
        select railcard_code, description
        from railcard_current
        where coalesce(min_adults, 0) <= $adults
          and coalesce(max_adults, 9) >= $adults
          and coalesce(min_children, 0) <= $children
          and coalesce(max_children, 9) >= $children
          and (not $public_only or is_public)
        order by railcard_code
        """,
        {"adults": adults, "children": children, "public_only": public_only},
    ).fetchall()


def describe(connection: duckdb.DuckDBPyConnection, railcard: str) -> str | None:
    row = connection.execute(
        "select description from railcard_current where railcard_code = $code",
        {"code": railcard},
    ).fetchone()
    return row[0] if row else None
