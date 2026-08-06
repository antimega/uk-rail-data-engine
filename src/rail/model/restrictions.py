"""Time restrictions on fares.

A fare carries a two-character restriction code - 65% of them do - and that code
names a set of *time bands during which the fare may not be used*. Both header
types in the feed (`N` and `P`) behave that way: restriction 0W bans departing
Euston 04:30–09:25 and 15:01–18:44, and 1C bans arriving into London termini
before 10:00. Without this, an Off-Peak fare gets quoted for the 07:30 out of
Euston, which is exactly the fare you cannot buy.

Three things govern whether a band bites on a given day:

* ``cf_mkr`` - the file carries the restrictions in force now ("C") *and* the
  next version ("F"), each with its own validity window in the RD records. The
  travel date decides which set to read.
* header dates (HD) - which dates and weekdays the whole restriction applies to.
* band dates (TD) - the same, per time band, overriding the header.

Those date ranges are **MMDD, not DDMM**. Restriction 0W runs 0104–0402,
0407–0501, 0505–0522, 0526–0828, 0901–1223: read as MMDD the gaps are Easter,
both May bank holidays, the August bank holiday and Christmas, which is exactly
when peak restrictions lift. Read as DDMM it is nonsense.

**What this does not do.** A band names a location. Bands at the journey's
origin (departing) and destination (arriving) can be evaluated from a journey
time alone, and those are the overwhelming majority. A band at an intermediate
station needs the actual itinerary, not just an arrival time, so it is counted
and reported rather than silently ignored.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import duckdb

_DAY_COLUMNS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)


@dataclass
class RestrictionCounts:
    codes: int
    bands: int
    windows: int
    #: Bands naming a station that is neither origin nor destination, so they
    #: cannot be judged from a journey time alone.
    intermediate_bands: int
    #: Current restrictions barring a change of trains outright.
    no_change_allowed: int = 0
    #: Band/operator relationships from TT. Counted because the failure is
    #: silent: with none of them every qualified band bars every operator, and
    #: the 16-17 Saver quietly discounts nothing anywhere.
    toc_qualifiers: int = 0
    #: SR train-list rows retained for dated fare decisions.
    trains: int = 0
    #: SD date windows attached to SR train-list rows.
    train_windows: int = 0
    #: SQ location exceptions attached to negative train-list rows.
    train_exceptions: int = 0


def build_restrictions(
    connection: duckdb.DuckDBPyConnection,
    fares_dir: Path,
) -> RestrictionCounts:
    """Build restriction_window, restriction_band and restriction_band_window."""

    def path(name: str) -> str:
        return (fares_dir / f"{name}.parquet").as_posix()

    # Which set of restrictions applies to a given travel date.
    connection.execute(f"""
        create or replace table restriction_window as
        select cf_mkr, start_date, end_date
        from read_parquet('{path("restriction_dates")}')
    """)

    # The header carries one thing the bands do not: whether a change of trains
    # is allowed at all (4.19.3 field 10, `CHANGE_IND`, position 139). `N` bars
    # it - 36 of the 839 current restrictions, among them the Avanti "Valid on
    # booked service only" fares and TfW's "BOOKDTRAINONLY" Advance Flex.
    #
    # RSPS5045 4.19.6 defines an `HC` record naming stations where a change *is*
    # allowed despite the bar. **This feed ships none** - not one HC record in
    # RJFAF833 - so a bar here has no exceptions. If HC ever appears, refusing
    # every change becomes too strict and this is the place to fix it.
    connection.execute(f"""
        create or replace table restriction_current as
        select cf_mkr, restriction_code, description,
               type_out, type_ret, change_ind as change_allowed
        from read_parquet('{path("restriction_header")}')
    """)

    train_path = fares_dir / "restriction_train.parquet"
    if train_path.exists():
        connection.execute(f"""
            create or replace table restriction_train_current as
            select cf_mkr, restriction_code, train_no, out_ret,
                   quota_ind, sleeper_ind
            from read_parquet('{train_path.as_posix()}')
        """)
    else:
        connection.execute("""
            create or replace table restriction_train_current (
                cf_mkr varchar,
                restriction_code varchar,
                train_no varchar,
                out_ret varchar,
                quota_ind varchar,
                sleeper_ind varchar
            )
        """)

    train_date_path = fares_dir / "restriction_train_date.parquet"
    if train_date_path.exists():
        connection.execute(f"""
            create or replace table restriction_train_window as
            select cf_mkr, restriction_code, train_no, out_ret,
                   try_cast(date_from as integer) as from_mmdd,
                   try_cast(date_to as integer) as to_mmdd,
                   {", ".join(_DAY_COLUMNS)}
            from read_parquet('{train_date_path.as_posix()}')
        """)
    else:
        connection.execute("""
            create or replace table restriction_train_window (
                cf_mkr varchar,
                restriction_code varchar,
                train_no varchar,
                out_ret varchar,
                from_mmdd integer,
                to_mmdd integer,
                monday boolean,
                tuesday boolean,
                wednesday boolean,
                thursday boolean,
                friday boolean,
                saturday boolean,
                sunday boolean
            )
        """)

    exception_path = fares_dir / "restriction_train_quota.parquet"
    if exception_path.exists():
        connection.execute(f"""
            create or replace table restriction_train_exception_current as
            select cf_mkr, restriction_code, train_no, out_ret,
                   location, quota_ind, arr_dep
            from read_parquet('{exception_path.as_posix()}')
        """)
    else:
        connection.execute("""
            create or replace table restriction_train_exception_current (
                cf_mkr varchar,
                restriction_code varchar,
                train_no varchar,
                out_ret varchar,
                location varchar,
                quota_ind varchar,
                arr_dep varchar
            )
        """)

    # A blank time is an **open end**, not a missing band. 30 of the 66,432
    # records leave one side empty - `FL` and `FK` four each, then `XG`, `8Z`,
    # `OF` and `S1` - and they read as "any time up to 23:59" or "from 00:00
    # onwards". Dropping them, which is what this did, silently discarded a bar
    # and made the fare look valid all day.
    #
    # Coalescing to the ends of the day is both the natural reading and the
    # conservative one: a band is a *prohibition*, so widening it can only
    # withdraw a fare that should not have been offered, never invent one.
    connection.execute(f"""
        create or replace table restriction_band as
        select cf_mkr, restriction_code, sequence_no, out_ret,
               coalesce(time_from, 0) as time_from,
               coalesce(time_to, 1439) as time_to,
               arr_dep_via, location, min_fare_flag
        from read_parquet('{path("restriction_time")}')
        where time_from is not null or time_to is not null
    """)

    # RSPS5045 4.19.10 field 7: "The time restriction only applies to trains
    # provided by this TOC." 2,565 of the current bands carry one, and applying
    # them to every operator is how the 16-17 Saver came to be withdrawn from
    # the entire network - its restriction `R5` is a single band barring travel
    # 00:01-23:59, every day of the year, at any station, qualified to
    # ScotRail and Caledonian Sleeper alone. Read without the qualifier that is
    # not a peak restriction, it is the railcard not existing.
    #
    # The band is keyed here exactly as `restriction_band` keys it, so a band
    # with no TT rows joins to nothing and keeps applying unconditionally.
    connection.execute(f"""
        create or replace table restriction_band_toc as
        select distinct cf_mkr, restriction_code, sequence_no, out_ret, toc_code
        from read_parquet('{path("restriction_time_toc")}')
    """)

    # A band's dates come from its own TD records where it has any, and from the
    # restriction's header dates otherwise. MMDD is stored as an integer so the
    # range test is a plain comparison.
    day_columns = ", ".join(_DAY_COLUMNS)
    connection.execute(f"""
        create or replace table restriction_band_window as
        with band_dates as (
            select cf_mkr, restriction_code, sequence_no, out_ret,
                   try_cast(date_from as integer) as from_mmdd,
                   try_cast(date_to as integer) as to_mmdd,
                   {day_columns}
            from read_parquet('{path("restriction_time_date")}')
        ),
        header_dates as (
            select cf_mkr, restriction_code,
                   try_cast(date_from as integer) as from_mmdd,
                   try_cast(date_to as integer) as to_mmdd,
                   {day_columns}
            from read_parquet('{path("restriction_header_date")}')
        )
        select b.cf_mkr, b.restriction_code, b.sequence_no, b.out_ret,
               d.from_mmdd, d.to_mmdd, {", ".join("d." + c for c in _DAY_COLUMNS)}
        from restriction_band b
        join band_dates d
          on d.cf_mkr = b.cf_mkr
         and d.restriction_code = b.restriction_code
         and d.sequence_no = b.sequence_no
         and d.out_ret = b.out_ret
        union all
        select b.cf_mkr, b.restriction_code, b.sequence_no, b.out_ret,
               h.from_mmdd, h.to_mmdd, {", ".join("h." + c for c in _DAY_COLUMNS)}
        from restriction_band b
        join header_dates h
          on h.cf_mkr = b.cf_mkr and h.restriction_code = b.restriction_code
        where not exists (
            select 1 from band_dates d
            where d.cf_mkr = b.cf_mkr
              and d.restriction_code = b.restriction_code
              and d.sequence_no = b.sequence_no
              and d.out_ret = b.out_ret
        )
    """)

    scalar = lambda sql: connection.execute(sql).fetchone()[0]
    return RestrictionCounts(
        codes=scalar("select count(distinct restriction_code) from restriction_band"),
        bands=scalar("select count(*) from restriction_band"),
        windows=scalar("select count(*) from restriction_band_window"),
        intermediate_bands=scalar(
            "select count(*) from restriction_band where arr_dep_via = 'V'"
        ),
        no_change_allowed=scalar(
            "select count(*) from restriction_current "
            "where cf_mkr = 'C' and not change_allowed"
        ),
        toc_qualifiers=scalar("select count(*) from restriction_band_toc"),
        trains=scalar("select count(*) from restriction_train_current"),
        train_windows=scalar("select count(*) from restriction_train_window"),
        train_exceptions=scalar(
            "select count(*) from restriction_train_exception_current"
        ),
    )


def marker_for(connection: duckdb.DuckDBPyConnection, travel_date: dt.date) -> str:
    """Whether a travel date reads the current or the future restriction set."""
    row = connection.execute(
        """
        select cf_mkr from restriction_window
        where $travel_date between start_date and end_date
        order by cf_mkr limit 1
        """,
        {"travel_date": travel_date},
    ).fetchone()
    # Outside both windows, the future set is the better guess.
    return row[0] if row else "F"


#: Bands that bite on a given date, for a given restriction set. `mmdd` and the
#: weekday are supplied by the caller so this stays a plain lookup.
APPLICABLE_BANDS_SQL = """
with in_force as (
    select b.restriction_code, b.out_ret, b.time_from, b.time_to,
           b.arr_dep_via, b.location, b.min_fare_flag,
           b.cf_mkr, b.sequence_no
    from restriction_band b
    join restriction_band_window w
      on w.cf_mkr = b.cf_mkr
     and w.restriction_code = b.restriction_code
     and w.sequence_no = b.sequence_no
     and w.out_ret = b.out_ret
    where b.cf_mkr = $marker
      and $mmdd between w.from_mmdd and w.to_mmdd
      and case $weekday
          when 0 then w.monday when 1 then w.tuesday when 2 then w.wednesday
          when 3 then w.thursday when 4 then w.friday when 5 then w.saturday
          else w.sunday end
)
-- The TOC qualifier rides along with the band rather than being looked up
-- later, because `distinct` collapses bands to their shape: two bands of
-- identical times and location differing only in which operators they name
-- must stay two rows, or one's qualifier would silently govern the other.
-- `list_sort` keeps that key stable so identical sets do still collapse.
select distinct i.restriction_code, i.out_ret, i.time_from, i.time_to,
       i.arr_dep_via, i.location, i.min_fare_flag,
       (select list_sort(list(t.toc_code))
        from restriction_band_toc t
        where t.cf_mkr = i.cf_mkr
          and t.restriction_code = i.restriction_code
          and t.sequence_no = i.sequence_no
          and t.out_ret = i.out_ret) as tocs
from in_force i
"""


def applicable_bands(
    connection: duckdb.DuckDBPyConnection,
    travel_date: dt.date,
) -> list[tuple[str, str, int, int, str, str, bool, list[str] | None]]:
    """Every restriction band in force on `travel_date`.

    ``min_fare_flag`` comes back with them and changes what a band means:
    RSPS5045 4.19.8 field 13 says `Y` leaves the fare valid but charges a
    minimum, where `N` bars it outright. Only 19 of the 33,216 current bands are
    `Y`, but one of them is the Network Railcard's, and it spans the whole day -
    read as a bar it withdraws the railcard entirely.

    The last element is the band's **TOC qualifier** (4.19.10 field 7): the
    operators whose trains it applies to, or ``None`` where it applies to all of
    them. It is null for the great majority - only 2,565 current bands name any
    - and a caller that cannot say which operators a journey used must go on
    applying the band, since a bar wrongly lifted sells a ticket that is not
    valid. See `_register_journey_tables` for where that guard lives.
    """
    return connection.execute(
        APPLICABLE_BANDS_SQL,
        {
            "marker": marker_for(connection, travel_date),
            "mmdd": travel_date.month * 100 + travel_date.day,
            "weekday": travel_date.weekday(),
        },
    ).fetchall()


APPLICABLE_TRAINS_SQL = """
select r.cf_mkr, r.restriction_code, r.train_no, r.out_ret,
       r.quota_ind, r.sleeper_ind
from restriction_train_current r
where r.cf_mkr = $marker
  and (
      not exists (
          select 1 from restriction_train_window w
          where w.cf_mkr = r.cf_mkr
            and w.restriction_code = r.restriction_code
            and w.train_no = r.train_no
            and w.out_ret = r.out_ret
      )
      or exists (
          select 1 from restriction_train_window w
          where w.cf_mkr = r.cf_mkr
            and w.restriction_code = r.restriction_code
            and w.train_no = r.train_no
            and w.out_ret = r.out_ret
            and $mmdd between w.from_mmdd and w.to_mmdd
            and case $weekday
                when 0 then w.monday when 1 then w.tuesday when 2 then w.wednesday
                when 3 then w.thursday when 4 then w.friday when 5 then w.saturday
                else w.sunday end
      )
  )
"""


def applicable_trains(
    connection: duckdb.DuckDBPyConnection,
    travel_date: dt.date,
) -> list[tuple[str, str, str, str, str, str]]:
    """SR train-list rows in force on ``travel_date`` after SD windows."""
    return connection.execute(
        APPLICABLE_TRAINS_SQL,
        {
            "marker": marker_for(connection, travel_date),
            "mmdd": travel_date.month * 100 + travel_date.day,
            "weekday": travel_date.weekday(),
        },
    ).fetchall()


#: MMDD, so 0104 is 1 April. See the module docstring for why this bites.
def _mmdd(value: int | None) -> str:
    if not value:
        return ""
    month, day = divmod(int(value), 100)
    months = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{day} {months[month]}" if 1 <= month <= 12 else str(value)


#: What `_days` says when no day is set - a band with neither its own date
#: records nor the restriction header's, which RSPS5045 means as "never
#: applies". Named so that reading it is a comparison rather than a string
#: literal repeated in two files.
_NO_DAYS = "no days"


def _days(flags: tuple[bool, ...]) -> str:
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    on = [name for name, flag in zip(names, flags) if flag]
    if len(on) == 7:
        return "daily"
    if on == list(names[:5]):
        return "Mon-Fri"
    if on == list(names[5:]):
        return "weekends"
    return ", ".join(on) or _NO_DAYS


#: What the band's location means for the journey.
_SENSE = {"D": "departing", "A": "arriving at", "V": "changing at"}


@dataclass
class RestrictionBand:
    """One time window in which a fare may not be used."""

    sequence_no: str
    out_ret: str
    time_from: int
    time_to: int
    sense: str
    location: str | None
    days: str
    dates: list[str]
    #: RSPS5045 4.19.8 field 13. When set the fare stays valid but a minimum
    #: fare is charged, rather than the fare being barred outright.
    minimum_fare_instead: bool

    def as_sentence(self) -> str:
        where = f" {_SENSE.get(self.sense, self.sense)} {self.location}" if self.location else ""
        window = f"{_hhmm(self.time_from)}-{_hhmm(self.time_to)}"
        leg = "outward" if self.out_ret == "O" else "return"
        effect = ("a minimum fare applies" if self.minimum_fare_instead
                  else "not valid")
        seasons = f" ({'; '.join(self.dates)})" if self.dates else ""
        return f"{leg}: {effect}{where} {window}, {self.days}{seasons}"


def _hhmm(minutes: int | None) -> str:
    if minutes is None:
        return "?"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def describe_restriction(
    connection: duckdb.DuckDBPyConnection,
    code: str,
    travel_date: dt.date,
    fares_dir: Path,
) -> dict:
    """A restriction code in words: what it is called and when it bites.

    Bands with their own date records (TD) use those; the rest inherit the
    restriction's header dates (HD). A band with neither never applies, and is
    returned with an empty ``dates`` so that shows rather than hides.
    """
    marker = marker_for(connection, travel_date)
    header = connection.execute(
        f"""
        select description, desc_out, desc_ret, type_out, type_ret, change_ind
        from read_parquet('{(fares_dir / "restriction_header.parquet").as_posix()}')
        where restriction_code = $code and cf_mkr = $marker
        limit 1
        """,
        {"code": code, "marker": marker},
    ).fetchone()

    rows = connection.execute(
        f"""
        select t.sequence_no, t.out_ret,
               -- Open ends, coalesced exactly as `restriction_band` does: a
               -- blank side means "from the start of the day" or "until the end
               -- of it". Reading them as missing crashed this command on `FL`.
               coalesce(t.time_from, 0) as time_from,
               coalesce(t.time_to, 1439) as time_to,
               t.arr_dep_via, t.location, t.min_fare_flag,
               list({{
                   'from_mmdd': w.from_mmdd, 'to_mmdd': w.to_mmdd,
                   'mo': w.monday, 'tu': w.tuesday, 'we': w.wednesday,
                   'th': w.thursday, 'fr': w.friday, 'sa': w.saturday,
                   'su': w.sunday
               }}) filter (where w.from_mmdd is not null) as windows
        from read_parquet('{(fares_dir / "restriction_time.parquet").as_posix()}') t
        left join restriction_band_window w
          on w.cf_mkr = t.cf_mkr and w.restriction_code = t.restriction_code
         and w.sequence_no = t.sequence_no and w.out_ret = t.out_ret
        where t.restriction_code = $code and t.cf_mkr = $marker
        group by all
        order by t.out_ret, t.sequence_no
        """,
        {"code": code, "marker": marker},
    ).fetchall()

    bands = []
    for seq, out_ret, start, end, sense, location, min_fare, windows in rows:
        dates, days = [], "daily"
        for window in windows or ():
            flags = tuple(window[k] for k in ("mo", "tu", "we", "th", "fr", "sa", "su"))
            days = _days(flags)
            dates.append(f"{_mmdd(window['from_mmdd'])} to {_mmdd(window['to_mmdd'])}")
        bands.append(RestrictionBand(
            sequence_no=seq, out_ret=out_ret, time_from=start, time_to=end,
            sense=sense, location=location, days=days, dates=dates,
            minimum_fare_instead=bool(min_fare),
        ))

    return {
        "code": code,
        "marker": marker,
        "description": header[0] if header else None,
        "note_out": header[1] if header else None,
        "note_return": header[2] if header else None,
        "change_allowed": bool(header[5]) if header else None,
        "bands": bands,
    }


@dataclass(frozen=True)
class RestrictionWindow:
    """One band of a restriction, flattened for a consumer to render.

    `RestrictionBand` above is the same thing with its date ranges and sequence
    number attached, which is what `rail restrictions 0W` prints. This is what
    is left once a caller only wants to say *when*.
    """

    out_ret: str
    sense: str
    #: `None` means the band is not station-specific (RSPS5045 4.19.8 field 10,
    #: three spaces), so it bites at whichever end its sense names - which for a
    #: return leg is the destination for `D` and the origin for `A`. A consumer
    #: that renders it as "somewhere on the journey" is throwing that away.
    location: str | None
    time_from: int
    time_to: int
    days: str
    #: RSPS5045 4.19.10 field 7 - the band applies only to these operators'
    #: trains, and empty means everybody's. 2,565 of the 33,219 current bands
    #: carry one.
    #:
    #: **Rendering a qualified band as unconditional is the mistake this file
    #: records at length**: `R5` and `RD` are each a single band spanning
    #: 00:01-23:59 every day at every station, and read without the qualifier
    #: that is not a peak restriction, it is the railcard withdrawn from the
    #: whole network. The same trap on a fare's own bands is quieter and no
    #: more correct - all five of `YX`'s Paddington windows are GW-only.
    tocs: tuple[str, ...] = ()


@dataclass(frozen=True)
class RestrictionNote:
    """What a restriction is called and, in the operator's words, what it does.

    `describe_restriction` renders every band for one code, which is the right
    answer for `rail restrictions 0W` and far too much for a consumer that
    wants to put a line of text beside a price - 18 KB for a single code, most
    of it band structure. This is the header alone, for every code at once.
    """

    code: str
    #: The restriction's name - "OFF-PEAK", "SUPER OFF-PEAK". A label, not a
    #: rule: `1B` is called "EIF Advance" and does no more than bar arrivals
    #: into London before 11:26.
    description: str
    #: RSPS5045 4.19.3 `DESC_OUT` / `DESC_RTN` - the operator's own prose, and
    #: the only place the rule is stated in a form a passenger could read:
    #: "NO ARR IN LDN EUS PRE 1130 Mon-Thurs". Free text, not parsed.
    note_out: str
    note_return: str
    #: Whether any current band governs the **return** leg. Read from the bands
    #: rather than from the prose, because the prose is not a rule.
    bars_return: bool
    #: Every current band, outward and return. **This is where the prose stops
    #: being good enough**: `YX` says "PEAK TRAVEL RESTRICTIONS APPLY MON-FRI"
    #: in both notes and carries 42 bands, and what a passenger going to
    #: Lostwithiel needs is the one that says no train back before 07:20.
    #:
    #: Checked against National Rail's own page for the code, which publishes
    #: the same windows to the minute - "06:16 from Penzance" against our
    #: `departing PNZ 04:30-06:15`, "arrive London Waterloo before 11:48"
    #: against `arriving at WAT 02:30-11:47`.
    bands: tuple[RestrictionWindow, ...] = ()


def restriction_notes(
    connection: duckdb.DuckDBPyConnection,
    travel_date: dt.date,
    fares_dir: Path,
) -> dict[str, RestrictionNote]:
    """Every restriction in force on `travel_date`, as a lookup by code.

    **`bars_return` is what makes a fare's conditionality answerable**, and it
    has to come from the bands. The tempting test is the ticket's own name -
    anything called "ANYTIME" is unrestricted - and it is wrong twice over: it
    misses 78 of Euston's 2,108 destinations, and the description field is 15
    characters, which is how "WEEKEND 1ST UPGRADE" arrived as `WEEKEND 1ST UPG`
    and cost three separate bugs. A code with no `out_ret = 'R'` band cannot
    bar the way home, whatever it is called.

    It is deliberately a fact about the *code*, not about a journey. A
    restriction whose return bands name stations nowhere near a particular pair
    still counts as barring the return, so a consumer reading this can say "no
    condition on coming back" and be right, but will sometimes fail to say it
    where it happens to be true. That is the safe direction: the cost of the
    first mistake is telling somebody a ticket is unconditional when it is not.
    """
    marker = marker_for(connection, travel_date)
    rows = connection.execute(
        f"""
        with header as (
            select restriction_code, description, desc_out, desc_ret
            from read_parquet('{(fares_dir / "restriction_header.parquet").as_posix()}')
            where cf_mkr = $marker
        ),
        -- One row per code, not per band: `exists` would need a correlated
        -- subquery per header row where this scans the band file once.
        -- Named `return_band` rather than the obvious `returning`, which is a
        -- DuckDB keyword and fails to parse as a CTE name.
        return_band as (
            select distinct restriction_code
            from read_parquet('{(fares_dir / "restriction_time.parquet").as_posix()}')
            where cf_mkr = $marker and out_ret = 'R'
        )
        select h.restriction_code, h.description, h.desc_out, h.desc_ret,
               r.restriction_code is not null as bars_return
        from header h
        left join return_band r using (restriction_code)
        order by h.restriction_code
        """,
        {"marker": marker},
    ).fetchall()

    # Every band for every code in one pass, joined to its day flags exactly as
    # `describe_restriction` joins them for one. `restriction_band_window` has
    # already resolved the precedence - a band with its own TD records uses
    # those and the rest inherit the restriction's header dates - so there is
    # one join here and not two.
    #
    # A band matching nothing has neither, which RSPS5045 says means it never
    # applies; `_days` of no flags gives "no days", which is that said out loud
    # rather than a band with a blank day list.
    windows = connection.execute(
        f"""
        select t.restriction_code, t.out_ret, t.sequence_no, t.arr_dep_via,
               t.location,
               coalesce(t.time_from, 0), coalesce(t.time_to, 1439),
               {", ".join(f"coalesce(max(w.{day}), false)" for day in _DAY_COLUMNS)},
               list(distinct q.toc_code) filter (where q.toc_code is not null)
        from read_parquet('{(fares_dir / "restriction_time.parquet").as_posix()}') t
        left join restriction_band_window w
          on w.cf_mkr = t.cf_mkr and w.restriction_code = t.restriction_code
         and w.sequence_no = t.sequence_no and w.out_ret = t.out_ret
        left join restriction_band_toc q
          on q.cf_mkr = t.cf_mkr and q.restriction_code = t.restriction_code
         and q.sequence_no = t.sequence_no and q.out_ret = t.out_ret
        where t.cf_mkr = $marker
        group by all
        order by t.restriction_code, t.out_ret, t.sequence_no
        """,
        {"marker": marker},
    ).fetchall()
    banded: dict[str, list[RestrictionWindow]] = {}
    for code, out_ret, _seq, sense, location, start, end, *rest in windows:
        *flags, tocs = rest
        days = _days(tuple(bool(f) for f in flags))
        # **A band with neither its own dates nor the header's never applies**,
        # and 20 of the 33,219 are in that position. `describe_restriction`
        # returns them with empty dates so they show, which is right for
        # `rail restrictions 0W` - the question there is what the file says.
        # The question here is what to tell a passenger, and "no trains on no
        # days" is not an answer.
        if days == _NO_DAYS:
            continue
        banded.setdefault(code, []).append(RestrictionWindow(
            out_ret=out_ret, sense=sense, location=location,
            time_from=start, time_to=end, days=days,
            tocs=tuple(sorted(tocs or ())),
        ))

    # The header file carries one row per code per marker, but say so by
    # construction rather than trusting it - a duplicate would otherwise pick
    # whichever row sorted last.
    notes: dict[str, RestrictionNote] = {}
    for code, description, out, ret, bars_return in rows:
        notes.setdefault(code, RestrictionNote(
            code=code,
            description=(description or "").strip(),
            note_out=(out or "").strip(),
            note_return=(ret or "").strip(),
            bars_return=bool(bars_return),
            bands=tuple(banded.get(code, ())),
        ))
    return notes
