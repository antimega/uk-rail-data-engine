"""Going and coming back, priced both ways.

Everything else here prices one direction. That is the right shape for "where
can I get for £20", but it cannot answer the ordinary question — *I am going on
Tuesday and back on Thursday, what should I buy* — because the answer is a
choice between two products:

* a **return fare**, one ticket covering both legs, whose validity has to permit
  the return date and whose restrictions apply to *both* journeys; or
* **two singles**, each priced and restricted on its own leg.

Neither is reliably cheaper. A Super Off-Peak Return is often barely more than
a single, and just as often two Advance singles undercut everything. Quoting one
without the other is the misleading part, so this returns both and names the
winner.

**The return leg has to be routed for either arm to be right.** 13,803 of the
restriction bands in force on a weekday carry `out_ret = 'R'`, and until now
none was ever evaluated — there was no time to test them against. Routing B→A
on the return date supplies both: when you leave the destination and when you
get back.

**The two legs run opposite ways, and the bands follow the journey, not the
ticket.** On the way home a *departure* band bites where the journey home
starts — the outward destination — and an *arrival* band bites back at the
origin. Swapping those silently applies London's morning arrival bans to a
train leaving London, which is a restriction that reads plausible and is
backwards. That mapping lives in the pricing SQL, tested directly.

**What this does not do.** It prices one outward journey and one return
journey, both the earliest after the times given. It is not a search for the
cheapest pair of departure times — an Advance ladder would make that a
different and much larger question. And it says nothing about seat
availability, which is not in the feed at all.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import duckdb

from .fares import cheapest_from, fare_options


@dataclass
class Leg:
    """One direction, routed."""

    origin: str
    destination: str
    date: dt.date
    depart: int
    arrive: int
    path: list[str]
    operators: set[str]
    modes: set[str]
    #: Times the journey changes train, from `ScanResult.changes_to`.
    changes: int = 0

    @property
    def minutes(self) -> int:
        return self.arrive - self.depart


@dataclass
class Quote:
    """One way of buying the round trip."""

    kind: str                       #: 'return' or 'two singles'
    pence: int
    #: (ticket code, description, pence, route code) per ticket bought — one
    #: for a return, two for a pair of singles.
    tickets: list[tuple[str, str, int, str | None]]

    def as_sentence(self) -> str:
        parts = " + ".join(
            f"£{pence / 100:,.2f} {code} {description.strip()}"
            for code, description, pence, _ in self.tickets
        )
        return f"{self.kind}: {parts} = £{self.pence / 100:,.2f}"


@dataclass
class RoundTrip:
    out: Leg
    back: Leg
    #: Cheapest of each kind, absent when nothing valid was found.
    single_ticket: Quote | None
    two_singles: Quote | None

    @property
    def best(self) -> Quote | None:
        quotes = [q for q in (self.single_ticket, self.two_singles) if q]
        return min(quotes, key=lambda q: q.pence) if quotes else None

    @property
    def saving(self) -> int | None:
        """What the cheaper option saves, or None if only one was priced."""
        if not (self.single_ticket and self.two_singles):
            return None
        return abs(self.single_ticket.pence - self.two_singles.pence)


def price_round_trip(
    connection: duckdb.DuckDBPyConnection,
    fares_dir: Path,
    out: Leg,
    back: Leg,
    *,
    ticket_class: int = 2,
    railcard: str | None = None,
    include_advance: bool = False,
    check_routes: bool = False,
    break_out: bool = False,
    break_in: bool = False,
) -> RoundTrip:
    """Price `out` and `back` as one return ticket and as two singles.

    Both legs must already be routed — see `Leg`. The restrictions on each are
    evaluated against that leg's own times, which is the whole reason the router
    has to run twice.
    """
    common = dict(ticket_class=ticket_class, railcard=railcard,
                  include_advance=include_advance)
    # TVL fields 12 and 13. A return ticket has to permit a break on whichever
    # leg is broken, and the two are separate permissions — `break_in` is the
    # one nothing could enforce until a return leg was routed through a chosen
    # stop, which is what `--return-via` does.
    breaks = {"break_of_journey": break_out, "break_returning": break_in}

    # --- one ticket, covering both legs -------------------------------------
    # A return's outward restrictions are judged on the outward journey and its
    # return bands on the way home, so both sets of times go in together. Its
    # validity must also permit the return date, which `return_on` enforces.
    #
    # Route conditions see the whole round trip, as they do for a stopover: one
    # ticket, so one journey to judge.
    #
    # `fare_options` rather than `cheapest_from`: the latter keeps only the
    # single cheapest fare per destination, which is almost always a single, so
    # asking it for a return finds nothing. York to King's Cross has a £130.40
    # Off-Peak Return sitting behind a £70.70 single, and the first version of
    # this reported "no return fare is valid" for every pair.
    whole_path = out.path + list(reversed(back.path))[1:]
    returns = fare_options(
        connection, fares_dir, out.origin, out.date,
        depart_minutes=out.depart,
        arrivals={out.destination: out.arrive},
        return_on=back.date,
        return_depart_minutes=back.depart % 1440,
        return_arrival_minutes=back.arrive % 1440,
        paths={out.destination: whole_path} if check_routes else None,
        operators={out.destination: out.operators | back.operators}
                  if check_routes else None,
        modes={out.destination: out.modes | back.modes} if check_routes else None,
        # A return ticket has to cover both journeys, so a restriction barring a
        # change is broken by a change on either one.
        changes={out.destination: out.changes + back.changes},
        **breaks,
        **common,
    )
    # Ordered by destination then price, so the first return is the cheapest.
    single_ticket = next(
        (Quote("return", row[3], [(row[1], row[2], row[3], row[5])])
         for row in returns
         if row[0] == out.destination and row[6] == "R"),
        None,
    )

    # --- two singles, each on its own leg ------------------------------------
    def single(leg: Leg) -> tuple | None:
        priced = cheapest_from(
            connection, fares_dir, leg.origin, leg.date,
            depart_minutes=leg.depart,
            arrivals={leg.destination: leg.arrive},
            include_returns=False,
            paths={leg.destination: leg.path} if check_routes else None,
            operators={leg.destination: leg.operators} if check_routes else None,
            modes={leg.destination: leg.modes} if check_routes else None,
            changes={leg.destination: leg.changes},
            # Each single covers one leg, so it needs only that leg's
            # permission — and it needs it *outward*, since a single has no
            # return side whichever direction it is travelling.
            break_of_journey=(break_out if leg is out else break_in),
            **common,
        )
        return next((r for r in priced if r[0] == leg.destination), None)

    outward, homeward = single(out), single(back)
    two_singles = None
    if outward and homeward:
        two_singles = Quote(
            "two singles",
            outward[3] + homeward[3],
            [(outward[1], outward[2], outward[3], outward[5]),
             (homeward[1], homeward[2], homeward[3], homeward[5])],
        )

    return RoundTrip(out=out, back=back, single_ticket=single_ticket,
                     two_singles=two_singles)
