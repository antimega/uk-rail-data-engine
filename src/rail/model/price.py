"""What counts as a price in a fare record.

Its own module because every reader of `fare.parquet` needs the same answer -
the pricing CTEs, the ticket classification, PlusBus, the review register and
`rail validate` - and `fares` already imports `plusbus`, so the rule cannot
live there without a cycle.
"""

from __future__ import annotations

#: **£9,998.00 and up is not a price either**, and unlike `99999999` the spec
#: never says so. 60,229 fare records in RJFAF952 sit at £9,998.00, £9,999.98 or
#: £9,999.99 across 170 ticket types - every `FOR TEST USE ON` code, and Avanti's
#: A and B ladders on the London zone flows, where 56A-56Y carry 1,675 of them
#: against 44 real prices of £40-£64. The dearest genuine fare in the feed is
#: £5,810.00, a season, so the gap is wide and this sits well clear of it.
#:
#: Read as prices they cost nothing at the head of a cheapest-fare query and
#: plenty everywhere else: `rail fares` lists them, and they **dilute the
#: flat-rate test** - `56A` came out an Advance only because 67 placeholders
#: pulled its modal share below the threshold. Treated exactly as the `99999999`
#: sentinel is: no fare, and on a non-derivable record one that withdraws the
#: flow fare it overrides.
PLACEHOLDER_FARE = 999_800


def is_price(column: str) -> str:
    """SQL: this fare column holds a price, not an absence of one."""
    return (f"({column} is not null and {column} > 0 "
            f"and {column} < {PLACEHOLDER_FARE})")
