"""Which ticket types have been looked at, and what changed since.

**The feed ships new ticket types with every generation, and a misclassified one
is silent.** Every bug in this area has had the same shape: a product nobody had
seen before lands in the wrong class and immediately wins, because the wrong
class is nearly always the cheaper one. `SCR GROUP 05` at 80p was the cheapest
fare from Glasgow Central to 358 destinations; `ILF DUMY-DO NOT USE` was the
winning walk-up on every one of its eight flows; `Secret Fare` undercut the real
Advance on 92 destinations from Euston. None of them raised anything.

So the register: a checked-in record of every ticket code and the class it was
given when somebody last looked. `rail tickets --review` diffs the current build
against it and shows what is new and what has moved; `rail tickets --accept`
writes the current state back once the answers have been checked.

## What the register is not

**It is not an override.** There is no way to say in this file "GTS is really a
walk-up" - reviewing a code means either agreeing with the classification or
changing the rules in `model/fares.py` that produced it, and then accepting.
A data file that could contradict the rules would be a second source of truth,
and the first thing to go stale.

That is deliberate friction. The rules carry their reasons in comments beside
them and `fare_reject` / `advance_reject` publish those reasons per code, which
is what makes the classification arguable. An override would move a decision out
of that record and into a file nothing explains.

## Why it is checked in

`data/` is git-ignored, so anything kept there vanishes on a fresh clone and
cannot be reviewed in a diff. This is a human judgement about 3,425 products and
belongs in the repository next to the rules it vouches for - a review is a
commit, and `git log` on this file is the history of who decided what.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

#: Ships with the package, beside the layouts, for the reason above.
REGISTER = Path(__file__).resolve().parents[1] / "reviewed_tickets.json"

#: The three classes a sellable ticket type can be in, and the fourth state.
#: These are the strings the register stores, so renaming one rewrites the file.
WALK_UP = "walk-up"
ADVANCE = "advance"
#: Sellable, tied to a booked train, and still not an Advance anyone can buy -
#: see `is_real_advance`. Held apart from `rejected` because the two mean
#: different things: this one is sold, just not as an Advance.
NOT_A_REAL_ADVANCE = "not-a-real-advance"
REJECTED = "rejected"


def classify(connection: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    """Every ticket code with the class this build gives it, and why.

    One row per code, so the register is a plain mapping and a diff of it reads
    as "this code moved from here to there".
    """
    rows = connection.execute("""
        select t.ticket_code, t.description,
               case
                   when t.is_walk_up then ?
                   when t.is_real_advance then ?
                   when t.is_advance_fare then ?
                   else ?
               end as class,
               coalesce(a.reason, f.reason) as reason
        from ticket_type_current t
        left join fare_reject f using (ticket_code)
        left join advance_reject a using (ticket_code)
        order by t.ticket_code
    """, [WALK_UP, ADVANCE, NOT_A_REAL_ADVANCE, REJECTED]).fetchall()
    return {
        code: {"description": description, "class": kind,
               **({"reason": reason} if reason else {})}
        for code, description, kind, reason in rows
    }


def priced(connection: duckdb.DuckDBPyConnection, fares_dir: Path) -> dict[str, int]:
    """How many fares each ticket code carries.

    The register's whole priority order: a new code with no fares can wait, and
    a new code that is already the cheapest thing on a flow cannot.
    """
    return dict(connection.execute(f"""
        select ticket_code, count(*)
        from read_parquet('{fares_dir / "fare.parquet"}')
        where fare is not null and fare > 0 group by 1
    """).fetchall())


@dataclass
class Review:
    """What has changed since the register was last written."""

    #: Codes the register has never seen.
    added: list[str] = field(default_factory=list)
    #: Codes whose class has moved, as (code, was, now).
    moved: list[tuple[str, str, str]] = field(default_factory=list)
    #: Codes the register knows and the feed no longer ships.
    withdrawn: list[str] = field(default_factory=list)
    #: The current classification, for writing back.
    current: dict[str, dict] = field(default_factory=dict)
    #: Fares per code, so a caller can rank by what actually matters.
    fares: dict[str, int] = field(default_factory=dict)
    #: The snapshot this was measured against.
    snapshot: str = ""

    @property
    def settled(self) -> bool:
        return not (self.added or self.moved or self.withdrawn)

    def carrying_fares(self) -> list[str]:
        """Unreviewed codes that are already pricing journeys.

        These are the ones worth stopping for. A new code with no fares is a
        product an operator has registered and not yet filed prices for, and it
        can be accepted whenever somebody next looks.
        """
        touched = set(self.added) | {code for code, _, _ in self.moved}
        return sorted((c for c in touched if self.fares.get(c)),
                      key=lambda c: -self.fares[c])


def load_register(path: Path = REGISTER) -> dict[str, dict]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("tickets", {})


def review(
    connection: duckdb.DuckDBPyConnection,
    fares_dir: Path,
    *,
    snapshot: str = "",
    path: Path = REGISTER,
) -> Review:
    """Diff this build's classification against the register."""
    known = load_register(path)
    current = classify(connection)
    return Review(
        added=sorted(set(current) - set(known)),
        moved=sorted(
            (code, known[code]["class"], current[code]["class"])
            for code in set(current) & set(known)
            if known[code]["class"] != current[code]["class"]
        ),
        withdrawn=sorted(set(known) - set(current)),
        current=current,
        fares=priced(connection, fares_dir),
        snapshot=snapshot,
    )


def accept(result: Review, *, path: Path = REGISTER) -> int:
    """Write the current classification back as reviewed. Returns the count.

    Sorted keys and an indent, because the point of the file is the diff: a
    review should read as the two or three lines that moved, not as a rewrite.
    """
    path.write_text(
        json.dumps(
            {
                "note": ("What each ticket code was classified as when somebody "
                         "last looked. Written by `rail tickets --accept`; see "
                         "rail/model/tickets.py for why this is not an override."),
                # **The file carries its own attribution**, because it is the
                # one thing here that is a standalone extract of the feed: 3,425
                # ticket codes and descriptions, in a format somebody could lift
                # on its own. The NRE Developer Terms permit publishing the data
                # and require acknowledgement "wherever the data or anything
                # derived from it is published", and a JSON file lifted out of a
                # repository takes the repository's README with it precisely
                # never. See docs/DATA-SOURCES.md.
                "source": ("Ticket codes and descriptions from the RDG fares "
                           "feed. Contains information from National Rail "
                           "Enquiries, licensed under the NRE Developer Terms "
                           "and Conditions v3.0 - "
                           "https://opendata.nationalrail.co.uk/terms. The "
                           "`class` field is this project's own classification, "
                           "not part of the feed."),
                "snapshot": result.snapshot,
                "tickets": dict(sorted(result.current.items())),
            },
            indent=1, sort_keys=False, ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    return len(result.current)
