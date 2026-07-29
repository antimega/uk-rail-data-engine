"""Train joins and splits.

A single physical train can carry two schedules. The Highland service divides at
Crianlarich for Oban and Fort William; portions join at Dundee. In CIF these are
``AA`` records: ``VV`` divides, ``JJ`` joins, both keyed on a base train, an
associated train and the location where it happens.

Without them a through passenger appears to change trains, paying the station's
minimum interchange time for a move they never make — and sometimes missing the
connection entirely, because the two portions are booked to depart within a
minute or two of each other.

Associations carry their own STP indicators and resolve exactly like schedules,
``C > N > O > P``, so a cancelled association has to stop applying on the days it
is cancelled.

**Bound to the location, not the train.** The obvious shortcut — mark the partner
train boardable once you have ridden its counterpart — is wrong. It holds for the
common shape, where a divide's associated train originates at the split and a
join's terminates there, but roughly one association in seven has the partner
calling at other stations too, and the shortcut would let a passenger board it
somewhere they have never been. Each link therefore unlocks boarding at exactly
one station.

**The associated train may run on a different day.** ``assoc_date_ind`` is `S`
for the ordinary case, `N` for "over next midnight" and `P` for "over previous
midnight", and the offset applies to the *associated* schedule's own date. All
234 `N` records in this feed are Caledonian Sleeper divides at Edinburgh and
Carstairs: the base leaves Euston at 21:15 and the portion is a separate
schedule dated the next day, departing 04:28. There is no same-day overlap
between the two at all — 13 next-day ones — so resolving them on the base date
found nothing, and the link simply did not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

#: Categories a passenger can stay aboard through.
PASSENGER_CATEGORIES = ("JJ", "VV")

#: RSPS5046 5.5.8.2 field 9. `S` is the ordinary case, the associated train
#: running on the same day as the base. `N` is "over next midnight" — the
#: associated train's schedule is dated the *following* day — and `P` "over
#: previous midnight". All 234 of the `N` records here are Caledonian Sleeper
#: divides at Edinburgh and Carstairs, where the portion runs on into the
#: morning as a schedule of its own.
DAY_OFFSETS = {"S": 0, "N": 1, "P": -1}
SAME_DAY = "S"

#: "P" marks an association a passenger may use. "O" and blank are operating
#: associations, which RSPS5046 5.5.8.2 says journey planners must ignore.
PASSENGER_USE = "P"


@dataclass
class AssociationCounts:
    resolved: int
    links: int
    #: Links whose associated train runs on a different day from the base.
    next_day: int
    dates: int


def build_associations(
    connection: duckdb.DuckDBPyConnection,
    timetable_dir: Path,
) -> AssociationCounts:
    """Build association_link: which trains are joined, where, on which dates."""
    associations = (timetable_dir / "association.parquet").as_posix()

    connection.execute(f"""
        create or replace temp table association_raw as
        select base_uid, assoc_uid, assoc_location, assoc_cat, assoc_date_ind,
               association_type, stp_indicator, start_date, end_date,
               monday, tuesday, wednesday, thursday, friday, saturday, sunday
        from read_parquet('{associations}')
        where base_uid is not null and assoc_uid is not null
          and assoc_location is not null
    """)

    # Same C > N > O > P resolution as schedules, per association and date. A
    # cancellation winning means the trains are not joined that day.
    connection.execute("""
        create or replace table association_date as
        with spine as (select distinct date from service_date),
        candidates as (
            select a.base_uid, a.assoc_uid, a.assoc_location, a.assoc_cat,
                   a.assoc_date_ind, a.association_type, a.stp_indicator, d.date,
                   case a.stp_indicator
                       when 'C' then 4 when 'N' then 3
                       when 'O' then 2 when 'P' then 1 else 0
                   end as priority
            from association_raw a
            join spine d on d.date between a.start_date and a.end_date
            where case dayofweek(d.date)
                      when 1 then a.monday    when 2 then a.tuesday
                      when 3 then a.wednesday when 4 then a.thursday
                      when 5 then a.friday    when 6 then a.saturday
                      when 0 then a.sunday
                  end
        ),
        ranked as (
            select *, row_number() over (
                partition by base_uid, assoc_uid, assoc_location, date
                order by priority desc
            ) as rn
            from candidates
        )
        select base_uid, assoc_uid, assoc_location, assoc_cat,
               assoc_date_ind, association_type, date
        from ranked
        where rn = 1 and stp_indicator <> 'C'
    """)

    # Resolve both UIDs to the schedules actually running that day, and the
    # location to a station. A link is only usable if both trains run and both
    # call publicly at the association point.
    connection.execute(f"""
        create or replace table association_link as
        with split as (
            select a.date, a.base_uid, a.assoc_uid, a.assoc_cat,
                   case a.assoc_date_ind
                       when 'N' then 1 when 'P' then -1 else 0 end as assoc_day_offset,
                   base.schedule_id as base_schedule_id,
                   assoc.schedule_id as assoc_schedule_id,
                   bs.seq as base_seq,
                   as_.seq as assoc_seq,
                   t.crs
            from association_date a
            join service_date base
              on base.train_uid = a.base_uid and base.date = a.date
            -- The offset applies to the associated schedule, not the base: a
            -- next-day divide is the same journey, continuing past midnight on
            -- a schedule the feed dates tomorrow.
            join service_date assoc
              on assoc.train_uid = a.assoc_uid
             and assoc.date = a.date + coalesce(
                     case a.assoc_date_ind
                         when 'N' then 1 when 'P' then -1 else 0 end, 0)
            join station_tiploc t on t.tiploc = a.assoc_location
            -- **Not required to be a public call.** A train is split where the
            -- operation happens, which is routinely an operational stop: the
            -- Highland sleeper divides at Edinburgh and the Inverness portion's
            -- Edinburgh entry has no times and `is_public` false, because
            -- nobody boards or alights — they stay aboard. Demanding a public
            -- call dropped every Aberdeen and Fort William portion, so Euston
            -- to Aberdeen came out 53 minutes late with two changes against a
            -- through train.
            join schedule_stop bs
              on bs.schedule_id = base.schedule_id
             and bs.location = a.assoc_location
            join schedule_stop as_
              on as_.schedule_id = assoc.schedule_id
             and as_.location = a.assoc_location
            where a.assoc_cat in {PASSENGER_CATEGORIES}
              and a.assoc_date_ind in {tuple(DAY_OFFSETS)}
              -- RSPS5046 5.5.8.2 field 14: 'O' or blank means the association
              -- is for operating purposes and journey planners must ignore it.
              -- All 3,432 JJ/VV records in RJTTF904 are 'P', so this changes
              -- nothing today and stops an operating join being sold as a
              -- through train the day one appears.
              and a.association_type = '{PASSENGER_USE}'
        )
        select distinct
               s.date,
               s.base_schedule_id,
               s.assoc_schedule_id,
               s.assoc_day_offset,
               s.crs,
               s.assoc_cat,
               -- Where the passenger must already be aboard the base: its last
               -- public call at or before the split. Usually the split itself;
               -- for the sleeper it is Preston, because the Edinburgh stop is
               -- operational and the Aberdeen portion leaves before the
               -- Inverness portion's next public call at Stirling.
               (select bt.crs
                from schedule_stop b2
                join station_tiploc bt on bt.tiploc = b2.location
                where b2.schedule_id = s.base_schedule_id
                  and b2.seq <= s.base_seq and b2.is_public
                order by b2.seq desc limit 1) as base_unlock_crs,
               -- And where they carry on: the portion's first public call at or
               -- after the split.
               (select at2.crs
                from schedule_stop a2
                join station_tiploc at2 on at2.tiploc = a2.location
                where a2.schedule_id = s.assoc_schedule_id
                  and a2.seq >= s.assoc_seq and a2.is_public
                order by a2.seq limit 1) as assoc_board_crs
        from split s
        where base_unlock_crs is not null and assoc_board_crs is not null
    """)

    scalar = lambda sql: connection.execute(sql).fetchone()[0]
    return AssociationCounts(
        resolved=scalar("select count(*) from association_date"),
        links=scalar("select count(*) from association_link"),
        next_day=scalar(
            "select count(*) from association_link where assoc_day_offset <> 0"
        ),
        dates=scalar("select count(distinct date) from association_link"),
    )


def links_for(
    connection: duckdb.DuckDBPyConnection,
    date,
) -> list[tuple[int, int, str]]:
    """(base schedule, associated schedule, CRS) joined on this date."""
    return connection.execute(
        """
        select base_schedule_id, assoc_schedule_id, crs
        from association_link where date = $date
        """,
        {"date": date},
    ).fetchall()
