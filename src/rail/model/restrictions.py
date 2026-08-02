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
    #: Time-band/operator relationships retained from TT records.
    toc_qualifiers: int = 0


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
        select cf_mkr, restriction_code, description, change_ind as change_allowed
        from read_parquet('{path("restriction_header")}')
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

    # TT qualifies one time-band sequence to one or more operators. It is a
    # relationship, not a standalone restriction: 1L/0001 is an XC morning
    # band and therefore says nothing about a Grand Central train at the same
    # time. Keep the full key because sequence numbers repeat across codes,
    # directions and current/future marker sets.
    toc_path = fares_dir / "restriction_time_toc.parquet"
    if toc_path.exists():
        connection.execute(f"""
            create or replace table restriction_band_toc as
            select cf_mkr, restriction_code, sequence_no, out_ret, toc_code
            from read_parquet('{toc_path.as_posix()}')
        """)
    else:
        connection.execute("""
            create or replace table restriction_band_toc (
                cf_mkr varchar,
                restriction_code varchar,
                sequence_no varchar,
                out_ret varchar,
                toc_code varchar
            )
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
select distinct b.restriction_code, b.out_ret, b.time_from, b.time_to,
       b.arr_dep_via, b.location, b.min_fare_flag
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
"""


APPLICABLE_BAND_RECORDS_SQL = """
select distinct b.restriction_code, b.out_ret, b.time_from, b.time_to,
       b.arr_dep_via, b.location, b.min_fare_flag,
       b.cf_mkr, b.sequence_no,
       (select list(t.toc_code order by t.toc_code)
        from restriction_band_toc t
        where t.cf_mkr = b.cf_mkr
          and t.restriction_code = b.restriction_code
          and t.sequence_no = b.sequence_no
          and t.out_ret = b.out_ret) as toc_codes
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
"""


def applicable_bands(
    connection: duckdb.DuckDBPyConnection,
    travel_date: dt.date,
) -> list[tuple[str, str, int, int, str, str]]:
    """Every restriction band in force on `travel_date`.

    ``min_fare_flag`` comes back with them and changes what a band means:
    RSPS5045 4.19.8 field 13 says `Y` leaves the fare valid but charges a
    minimum, where `N` bars it outright. Only 19 of the 33,216 current bands are
    `Y`, but one of them is the Network Railcard's, and it spans the whole day -
    read as a bar it withdraws the railcard entirely.
    """
    return connection.execute(
        APPLICABLE_BANDS_SQL,
        {
            "marker": marker_for(connection, travel_date),
            "mmdd": travel_date.month * 100 + travel_date.day,
            "weekday": travel_date.weekday(),
        },
    ).fetchall()


def applicable_band_records(
    connection: duckdb.DuckDBPyConnection,
    travel_date: dt.date,
) -> list[tuple]:
    """Applicable bands with their stable key and allowed operators.

    The shorter :func:`applicable_bands` tuple is retained as the public
    reporting API. Pricing uses these records so a TT qualifier stays attached
    to the exact sequence it qualifies.
    """
    return connection.execute(
        APPLICABLE_BAND_RECORDS_SQL,
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


def _days(flags: tuple[bool, ...]) -> str:
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    on = [name for name, flag in zip(names, flags) if flag]
    if len(on) == 7:
        return "daily"
    if on == list(names[:5]):
        return "Mon-Fri"
    if on == list(names[5:]):
        return "weekends"
    return ", ".join(on) or "no days"


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
