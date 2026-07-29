"""Return tickets: which kind, and when you may come back.

`cheapest_from` has always priced returns alongside singles, and a return
sometimes wins - an Off-Peak Day Return can undercut two singles. What was
missing was any statement of *what you had bought*: a price appeared with no
note that it was a return, and nothing said by when you had to travel back.

Three sources have to agree before that question can be answered.

**`tkt_type` (TTY field 9) decides the shape**, not the validity record. `S` is a
single, `R` a return, `N` a season. This matters because validity codes are
shared: 185 current single ticket types point at a validity code carrying a
return period, because that same code also serves returns. Reading the validity
alone would call `1U2` "OFF PK - 1ST PK" a return. Of the walk-up types, 742 are
singles, 444 returns and 193 seasons.

**`TVL` says how long each leg lasts**, and the arithmetic is pinned by two
codes rather than assumed:

* `06` "ON DATE SHOWN" is an ordinary Day Return with ``ret_days = 1``. A day
  return comes back the same day, so **``ret_days`` counts days inclusive of
  the outward day** and the last day is ``outward + ret_days - 1``.
* `49` "FIVE DAY RTN" carries ``ret_days = 5``, ``ret_after_days = 4`` and, in
  prose, ``out_description = 'OUT ON WED'`` / ``rtn_description = 'RTN ON SUN'``.
  Wednesday to Sunday is four days, and it is both the earliest and the latest
  return. That fixes **``ret_after_days`` as a plain offset** - earliest is
  ``outward + ret_after_days`` - and confirms the inclusive reading above, since
  ``outward + 5 - 1`` is also Sunday. Neither rule can be moved by a day without
  breaking one of the two.

Months are a calendar offset rather than a count of days, so a one-month return
runs to the same day of the following month. The asymmetry with days is real and
follows from `06`: there is no "day zero" for days, and no natural inclusive
reading for months.

**`ret_after_day` (4.7.2 field 11) is what makes a weekend return a weekend
return**, and it is set on **three codes out of a hundred** - `58` and `59` say
`SU`, `98` says `SA`. The spec's wording is that return travel is not permitted
*until the day specified has passed*, so the earliest return is the day after
that weekday, not the weekday itself. `98` is the old must-stay-a-Saturday-night
rule and reproduces exactly under that reading: travel out midweek and you may
return from Sunday. Miss the field entirely and a Long Weekend Return looks like
an ordinary four-day return.

**The trap that turns out not to bite.** Twelve validity codes carry zero in
every numeric field and state their period only in the description - `01` is
"THREE DAYS", `02` "THREE MONTHS", `03` "ONE MONTH" - so reading the numbers
alone would make those look like zero-day tickets. Checked against the current
feed: **no walk-up return uses one of them.** They cover seasons (182 tickets on
`00` "(USE SEASON)") and eight singles. So the prose is worth recording and is
not worth parsing, and `unstated_period` marks the rows rather than guessing.

The same caution does apply to two codes that *are* returns: `49` and `29` give
only a bound numerically, and the actual rule - "OUT ON WED / RTN ON SUN",
"BEFORE 1200" - lives in `out_description`/`rtn_description` and nowhere else.
Those are carried through to the caller verbatim for that reason.

**What this does not do.** It answers "may I come back on this date, on this
ticket", not "what is the cheapest way to make a round trip". Two singles are
often cheaper and are a different query.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import duckdb

#: TTY field 9. `N` is a season, which this module treats as "not a return"
#: rather than as a return with an unusual window.
SINGLE, RETURN, SEASON = "S", "R", "N"

#: TVL field 11, and the only place the weekend-return rule is encoded.
_WEEKDAYS = {
    "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6,
}

#: Descriptions that state the ticket is not valid in that direction. A single's
#: validity record often shares a code with a return, so this is a corroborating
#: signal rather than the deciding one - `tkt_type` decides.
_INVALID = "INVALID"


@dataclass
class ValidityCounts:
    codes: int
    singles: int
    returns: int
    seasons: int
    #: Return ticket types by the shape of their return window.
    same_day: int
    multi_day: int
    period: int
    #: Returns whose validity record gives no return period at all.
    unstated: int
    #: Return types carrying the weekend rule, which is the shape most easily
    #: mistaken for an ordinary multi-day return.
    weekend: int


def build_ticket_validity(
    connection: duckdb.DuckDBPyConnection,
    fares_dir: Path,
) -> ValidityCounts:
    """Build `ticket_validity_current`, one row per currently-valid TVL code.

    Requires `ticket_type_current`, since the return *kind* comes from the
    ticket type and only the *window* comes from the validity record.
    """
    validity = (fares_dir / "ticket_validity.parquet").as_posix()

    connection.execute(f"""
        create or replace table ticket_validity_current as
        with current_records as (
            select *, row_number() over (
                partition by validity_code order by start_date desc
            ) as rn
            from read_parquet('{validity}')
            where current_date between start_date and end_date
        )
        select validity_code,
               description,
               out_days, out_months,
               ret_days, ret_months,
               ret_after_days, ret_after_months,
               -- Three codes out of a hundred, and the whole weekend rule.
               nullif(trim(coalesce(ret_after_day, '')), '') as ret_after_day,
               break_out, break_in,
               out_description, rtn_description,
               -- Every numeric field zero: the period, if any, is stated only
               -- in the description. No walk-up return uses one of these.
               (coalesce(out_days, 0) = 0 and coalesce(out_months, 0) = 0
                and coalesce(ret_days, 0) = 0 and coalesce(ret_months, 0) = 0)
                   as unstated_period,
               upper(trim(coalesce(rtn_description, ''))) = '{_INVALID}'
                   as return_marked_invalid
        from current_records
        where rn = 1
    """)

    # The kind is a property of the ticket, not of the validity code, because a
    # validity code is shared between singles and returns.
    connection.execute("""
        create or replace table ticket_return_kind as
        select t.ticket_code,
               t.tkt_type,
               v.validity_code,
               case
                   when t.tkt_type <> 'R' then 'none'
                   when coalesce(v.ret_months, 0) > 0 then 'period'
                   when coalesce(v.ret_days, 0) >= 2 then 'multi_day'
                   when coalesce(v.ret_days, 0) = 1 then 'same_day'
                   else 'unstated'
               end as return_kind,
               v.ret_after_day is not null as is_weekend_return
        from ticket_type_current t
        left join ticket_validity_current v on v.validity_code = t.validity_code
    """)

    scalar = lambda sql: connection.execute(sql).fetchone()[0]
    kind = lambda k: scalar(
        "select count(*) from ticket_return_kind k "
        "join ticket_type_current t using (ticket_code) "
        f"where t.is_walk_up and k.return_kind = '{k}'"
    )
    walk_up = lambda tkt: scalar(
        "select count(*) from ticket_return_kind k "
        "join ticket_type_current t using (ticket_code) "
        f"where t.is_walk_up and k.tkt_type = '{tkt}'"
    )
    return ValidityCounts(
        codes=scalar("select count(*) from ticket_validity_current"),
        singles=walk_up(SINGLE),
        returns=walk_up(RETURN),
        seasons=walk_up(SEASON),
        same_day=kind("same_day"),
        multi_day=kind("multi_day"),
        period=kind("period"),
        unstated=kind("unstated"),
        weekend=scalar(
            "select count(*) from ticket_return_kind k "
            "join ticket_type_current t using (ticket_code) "
            "where t.is_walk_up and k.is_weekend_return"
        ),
    )


def _add_months(date: dt.date, months: int) -> dt.date:
    """The same day of the month, `months` later, clamped to the month's end."""
    if not months:
        return date
    total = date.month - 1 + months
    year, month = date.year + total // 12, total % 12 + 1
    return dt.date(year, month, min(date.day, calendar.monthrange(year, month)[1]))


def _after_weekday(date: dt.date, code: str) -> dt.date:
    """The day after the next occurrence of `code` on or after `date`.

    RSPS5045 4.7.2 field 11 says return travel is not permitted until the day
    specified has *passed*, so a `SA` ticket may return from the Sunday. That
    reading is what reproduces the old must-stay-a-Saturday-night rule, which is
    the only one of the three codes whose real-world behaviour is documented
    outside the feed.
    """
    target = _WEEKDAYS.get(code.upper())
    if target is None:
        return date
    ahead = (target - date.weekday()) % 7
    return date + dt.timedelta(days=ahead + 1)


@dataclass
class ReturnWindow:
    """When a return ticket permits the journey back."""

    ticket_code: str
    kind: str
    earliest: dt.date
    latest: dt.date
    #: Set when the window comes from the outward validity because the record
    #: gives no return period - one walk-up ticket, `OG8` "Open Golf 8 Day".
    inferred: bool
    #: TVL field 11, the weekend rule. `SA` or `SU` where present.
    after_weekday: str | None
    #: RSPS5045 TVL field 12. Silence is not permission - see `fares.py`.
    break_permitted: bool
    #: The feed's own prose, which on `49` and `29` is the only complete
    #: statement of the rule. Carried verbatim rather than parsed.
    note: str | None

    @property
    def is_empty(self) -> bool:
        """No date satisfies both ends of the rule, so the ticket is unusable.

        This is the weekend rule doing its job rather than a defect. A `WKND 3
        Days` return bought for a Wednesday must be back within three days -
        by the Friday - and may not travel until Sunday has passed. Nothing
        satisfies both, which is precisely why it is a *weekend* return. Reading
        the days alone makes it look like an ordinary three-day ticket valid any
        day of the week, and clamping the window to keep it non-empty would sell
        exactly that.
        """
        return self.latest < self.earliest

    def covers(self, date: dt.date) -> bool:
        return not self.is_empty and self.earliest <= date <= self.latest

    def as_sentence(self) -> str:
        kinds = {"same_day": "same-day return", "multi_day": "return",
                 "period": "open return", "unstated": "return"}
        kind = kinds.get(self.kind, "return")
        day = {"SA": "Saturday", "SU": "Sunday"}.get(
            self.after_weekday, self.after_weekday)

        if self.is_empty:
            return (f"{kind}, but not for this outward date - must be back by "
                    f"{self.latest:%a %-d %b} and may not travel until "
                    f"{day} has passed")

        span = (f"on {self.earliest:%a %-d %b}"
                if self.earliest == self.latest
                else f"between {self.earliest:%a %-d %b} and {self.latest:%a %-d %b}")
        sentence = f"{kind}, back {span}"
        if day:
            sentence += f" (not until {day} has passed)"
        # TVL field 13, the return-leg counterpart of `break_out`. Silence is
        # not permission here either: a validity the feed says nothing about is
        # not assumed to allow stopping off on the way home.
        if not self.break_permitted:
            sentence += ", no break of journey returning"
        if self.note:
            sentence += f" - feed says {self.note!r}"
        return sentence


def return_window(
    connection: duckdb.DuckDBPyConnection,
    ticket_code: str,
    outward_date: dt.date,
) -> ReturnWindow | None:
    """When `ticket_code` permits the return leg, or None if it is not a return.

    None means "this is a single or a season", which is a different answer from
    a return whose window happens to exclude the date - that comes back as a
    window `covers()` rejects.
    """
    row = connection.execute(
        """
        select k.return_kind, v.ret_days, v.ret_months,
               v.ret_after_days, v.ret_after_months, v.ret_after_day,
               v.break_in, v.out_days, v.rtn_description, v.description
        from ticket_return_kind k
        left join ticket_validity_current v on v.validity_code = k.validity_code
        where k.ticket_code = $ticket_code
        """,
        {"ticket_code": ticket_code},
    ).fetchone()
    if row is None or row[0] == "none":
        return None
    return _window(ticket_code, *row[:1], outward_date, *row[1:])


def _window(ticket_code, kind, outward_date, ret_days, ret_months, after_days,
            after_months, after_day, break_in, out_days, rtn_description,
            description) -> ReturnWindow:
    """The arithmetic, in one place. See the module docstring for the evidence."""
    earliest = _add_months(
        outward_date + dt.timedelta(days=after_days or 0), after_months or 0)
    if after_day:
        earliest = _after_weekday(earliest, after_day)

    inferred = False
    if ret_months:
        latest = _add_months(outward_date, ret_months)
    elif ret_days:
        # Inclusive of the outward day: a Day Return carries ret_days = 1.
        latest = outward_date + dt.timedelta(days=ret_days - 1)
    else:
        # No return period stated. `OG8` is the only walk-up case: an eight-day
        # ticket whose single window covers both legs.
        inferred = True
        latest = outward_date + dt.timedelta(days=max(out_days or 1, 1) - 1)

    # A rule stated only in prose is worth surfacing; one that merely repeats
    # the code's own description is noise.
    note = (rtn_description or "").strip() or None
    if note and note.upper() in {(description or "").strip().upper(), _INVALID}:
        note = None

    return ReturnWindow(
        ticket_code=ticket_code,
        kind=kind,
        earliest=earliest,
        # Deliberately *not* clamped to `earliest`. A weekend rule can push the
        # earliest past the nominal last day, and that empty window is the
        # answer: see `ReturnWindow.is_empty`.
        latest=latest,
        inferred=inferred,
        after_weekday=after_day,
        break_permitted=bool(break_in),
        note=note,
    )


def return_windows(
    connection: duckdb.DuckDBPyConnection,
    outward_date: dt.date,
) -> dict[str, ReturnWindow]:
    """Every return ticket's window for one outward date, keyed by ticket code.

    Deliberately the same arithmetic as `return_window`, driven off one query
    rather than reimplemented in SQL. Expressing the rules a second time in the
    pricing CTEs would be faster and would eventually disagree with this - and
    the weekday rule cannot be written there at all without a calendar join.
    There are only a few hundred return types, so the loop costs nothing.
    """
    rows = connection.execute(
        """
        select k.ticket_code, k.return_kind, v.ret_days, v.ret_months,
               v.ret_after_days, v.ret_after_months, v.ret_after_day,
               v.break_in, v.out_days, v.rtn_description, v.description
        from ticket_return_kind k
        left join ticket_validity_current v on v.validity_code = k.validity_code
        where k.return_kind <> 'none'
        """
    ).fetchall()

    windows = {}
    for row in rows:
        (code, kind, ret_days, ret_months, after_days, after_months, after_day,
         break_in, out_days, rtn_description, description) = row
        windows[code] = _window(
            code, kind, outward_date, ret_days, ret_months, after_days,
            after_months, after_day, break_in, out_days, rtn_description,
            description)

    return windows


def returnable_on(
    connection: duckdb.DuckDBPyConnection,
    outward_date: dt.date,
    return_date: dt.date,
) -> set[str]:
    """Return ticket codes whose validity permits coming back on `return_date`.

    Singles are absent rather than excluded: a single is not made invalid by the
    question, it answers a different one, and the caller decides what to do with
    that. `cheapest_from` keeps them and labels them.
    """
    return {
        code for code, window in return_windows(connection, outward_date).items()
        if window.covers(return_date)
    }
