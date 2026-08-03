"""Turning CIF schedules into concrete running dates.

CIF does not say "this train runs on these dates". It gives a base schedule and
then *amends* it: a permanent schedule (``P``), short-term-plan variations that
overlay it (``O``), wholly new short-term schedules (``N``), and cancellations
(``C``). Several can cover the same date, and the winner is decided by priority:

    C > N > O > P

A cancellation winning means the train does not run that day at all.

Resolving this correctly is the single most error-prone step in the pipeline.
Get it wrong and the timetable looks plausible while quietly running trains that
were cancelled, or missing diverted ones.

Stops attach to schedules by file position (see ``line_no`` in the parser):
a ``BS`` record owns every ``LO``/``LI``/``LT`` line until the next ``BS``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import duckdb

#: CIF train status codes that carry passengers. P/B/S are permanent passenger,
#: bus and ship; 1/4/5 are their short-term-plan equivalents. Everything else
#: (freight, empty stock, trips) moves no passengers and is excluded from
#: journey planning.
PASSENGER_STATUSES = ("P", "B", "S", "1", "4", "5")

#: Activity codes that mean a passenger may board or alight. "T" is stop to take
#: up and set down, "U" pick up only, "D" set down only, "R" request stop.
PUBLIC_ACTIVITIES = ("T", "U", "D", "R")

# Activity is a compact sequence of one- and multi-character CIF codes, not a
# bag of letters. Remove the known multi-character codes before inspecting the
# remaining one-character atoms; otherwise TB is mistaken for T and -D for D.
MULTI_CHARACTER_ACTIVITIES = (
    "-D", "-T", "-U", "AE", "AX", "BL", "HH", "KC", "KE", "KF", "KS",
    "OP", "OR", "PR", "RM", "RR", "TB", "TF", "TS", "TW",
)
ACTIVITY_MULTI_PATTERN = "(" + "|".join(MULTI_CHARACTER_ACTIVITIES) + ")"

DEFAULT_HORIZON_DAYS = 90

#: **The feed ships two schedule files and `ZTR` is the second one.** It carries
#: the services the main CIF cannot express - Hovertravel's 223 crossings of the
#: Solent, First Group and Arriva rail-replacement coaches, Red Funnel, the
#: Ffestiniog, and the Metropolitan line beyond Harrow. 5,309 schedules and
#: 20,740 stops, parsed since the layouts were written and read by nothing until
#: now.
#:
#: **They are disjoint from the main file, checked rather than assumed.** No
#: `train_uid` appears in both. The overlap that looked most likely was London
#: Underground, which has 5,610 schedules in the main CIF and 1,764 here - and
#: they are different railways: the CIF ones are the Bakerloo and District
#: shared sections (Willesden Junction, Kew Gardens, Queen's Park), these are the
#: Metropolitan line (Amersham, Chalfont & Latimer, Chorleywood, Chesham).
#:
#: **Line numbers do collide**, because the two files are numbered separately -
#: CIF runs 16,949 to 7,901,571 and ZTR 1 to 31,355. So a ZTR schedule's id is
#: its line number plus this offset, chosen an order of magnitude above anything
#: the main file can reach. `source` says which file a row came from, so the two
#: can always be told apart afterwards.
ZTR_SCHEDULE_OFFSET = 100_000_000


@dataclass
class TimetableCounts:
    schedules: int
    stops: int
    service_dates: int
    cancelled_dates: int
    horizon_start: dt.date
    horizon_end: dt.date


def build_timetable(
    connection: duckdb.DuckDBPyConnection,
    timetable_dir: Path,
    *,
    start: dt.date | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> TimetableCounts:
    """Build train_schedule, schedule_stop and service_date."""
    schedule_path = (timetable_dir / "schedule.parquet").as_posix()
    extra_path = (timetable_dir / "schedule_extra.parquet").as_posix()
    stop_path = (timetable_dir / "stop_time.parquet").as_posix()
    z_schedule_path = (timetable_dir / "z_schedule.parquet").as_posix()
    z_extra_path = (timetable_dir / "z_schedule_extra.parquet").as_posix()
    z_stop_path = (timetable_dir / "z_stop_time.parquet").as_posix()
    has_ztr = (timetable_dir / "z_schedule.parquet").exists()

    start = start or dt.date.today()
    end = start + dt.timedelta(days=horizon_days)

    # line_no is unique within the file and orders the records, so it doubles as
    # the schedule's identity without inventing a surrogate key. ZTR is numbered
    # from its own file, hence the offset - see `ZTR_SCHEDULE_OFFSET`.
    ztr_schedules = f"""
        union all
        select s.line_no + {ZTR_SCHEDULE_OFFSET} as schedule_id,
               s.train_uid, s.runs_from, s.runs_to,
               s.monday, s.tuesday, s.wednesday, s.thursday,
               s.friday, s.saturday, s.sunday,
               s.bank_holiday_running,
               s.train_status, s.train_category, s.train_identity,
               s.stp_indicator,
               s.train_status in {PASSENGER_STATUSES} as is_passenger,
               x.atoc_code, null as retail_train_id,
               'ztr' as source
        from read_parquet('{z_schedule_path}') s
        asof left join read_parquet('{z_extra_path}') x
          on x.line_no >= s.line_no
    """ if has_ztr else ""

    connection.execute(f"""
        create or replace table train_schedule as
        select s.line_no as schedule_id,
               s.train_uid, s.runs_from, s.runs_to,
               s.monday, s.tuesday, s.wednesday, s.thursday,
               s.friday, s.saturday, s.sunday,
               s.bank_holiday_running,
               s.train_status, s.train_category, s.train_identity,
               s.stp_indicator,
               s.train_status in {PASSENGER_STATUSES} as is_passenger,
               x.atoc_code, x.retail_train_id,
               'cif' as source
        from read_parquet('{schedule_path}') s
        asof left join read_parquet('{extra_path}') x
          on x.line_no >= s.line_no
        {ztr_schedules}
    """)

    # **`crs` is resolved here, not by every consumer.** The two files name
    # locations differently - CIF by TIPLOC (`RYDEHOV`), ZTR by CRS (`XRD`) -
    # and that difference must be spent once, at the join, rather than left for
    # the network and `classify_locations` to trip over separately. A naive
    # union of the two would have matched no ZTR stop against `station_tiploc`
    # and silently dropped every one rather than failing.
    #
    # Left join, so a TIPLOC with no station survives as a row with a null
    # `crs`; consumers filter on it. An inner join here would lose the stop
    # entirely and take the through-connection with it.
    ztr_stops = f"""
        union all
        select z.line_no + {ZTR_SCHEDULE_OFFSET} as schedule_id, st.line_no,
               st.record_type, st.location, st.location as crs,
               st.public_arrival, st.public_departure,
               st.scheduled_arrival, st.scheduled_departure,
               st.platform, st.activity
        from read_parquet('{z_stop_path}') st
        asof join read_parquet('{z_schedule_path}') z on st.line_no >= z.line_no
    """ if has_ztr else ""

    # A BS owns the stops that follow it, up to the next BS.
    connection.execute(f"""
        create or replace temp table stop_raw as
        with joined as (
            select sch.schedule_id, st.line_no,
                   st.record_type, st.location, t.crs,
                   st.public_arrival, st.public_departure,
                   st.scheduled_arrival, st.scheduled_departure,
                   st.platform, st.activity
            from read_parquet('{stop_path}') st
            asof join (select * from train_schedule where source = 'cif') sch
              on st.line_no >= sch.schedule_id
            left join station_tiploc t on t.tiploc = st.location
            {ztr_stops}
        )
        , classified as (
            select *,
                   replace(coalesce(activity, ''), ' ', '') as compact_activity,
                   regexp_replace(
                       replace(coalesce(activity, ''), ' ', ''),
                       '{ACTIVITY_MULTI_PATTERN}', '', 'g'
                   ) as activity_atoms
            from joined
        )
        select schedule_id,
               row_number() over (
                   partition by schedule_id order by line_no
               ) as seq,
               record_type, location, crs,
               public_arrival, public_departure,
               scheduled_arrival, scheduled_departure,
               platform, activity,
               -- A public time plus a boarding activity is what makes a stop
               -- usable by a passenger; everything else is a passing point.
               (coalesce(public_arrival, public_departure) is not null
                -- N is an explicit non-advertised instruction and wins even
                -- when another token, such as origin TB, would permit a call.
                and not contains(activity_atoms, 'N')
                and (
                    regexp_matches(
                        activity_atoms, '[{"".join(PUBLIC_ACTIVITIES)}]'
                    )
                    or (record_type = 'LO' and contains(compact_activity, 'TB'))
                    or (record_type = 'LT' and contains(compact_activity, 'TF'))
                )) as is_public
        from classified
    """)

    # Public times are minutes after midnight and wrap on overnight services -
    # 4.6% of schedules in RJTTF904 do. Left as-is they produce negative journey
    # times, so unwrap them into a monotonic timeline: day_offset counts how
    # many midnights the train has crossed, and *_minutes are absolute from the
    # origin day. Only public calls take part; passing points have no times and
    # must not break the chain.
    connection.execute("""
        create or replace table schedule_stop as
        with public_only as (
            select schedule_id, seq,
                   coalesce(public_arrival, public_departure) as at_stop,
                   lag(coalesce(public_departure, public_arrival)) over (
                       partition by schedule_id order by seq
                   ) as previous,
                   -- A stop can straddle midnight by itself: arrive 23:59,
                   -- leave 00:05. That rollover has to carry to later stops.
                   case when public_departure < public_arrival then 1 else 0 end
                       as within_stop
            from stop_raw
            where is_public
        ),
        offsets as (
            select schedule_id, seq, within_stop,
                   sum(case when at_stop < previous then 1 else 0 end) over w
                   + coalesce(sum(within_stop) over w_before, 0) as arrival_offset
            from public_only
            window
                w as (partition by schedule_id order by seq
                      rows between unbounded preceding and current row),
                w_before as (partition by schedule_id order by seq
                             rows between unbounded preceding and 1 preceding)
        )
        select r.*,
               coalesce(o.arrival_offset, 0) as day_offset,
               r.public_arrival + 1440 * coalesce(o.arrival_offset, 0)
                   as arrival_minutes,
               r.public_departure + 1440 * (
                   coalesce(o.arrival_offset, 0) + coalesce(o.within_stop, 0)
               ) as departure_minutes
        from stop_raw r
        left join offsets o using (schedule_id, seq)
    """)
    connection.execute("drop table stop_raw")

    # The date spine, then every schedule that could apply on each date.
    connection.execute(f"""
        create or replace table service_date as
        with spine as (
            select unnest(generate_series(
                date '{start}', date '{end}', interval 1 day
            ))::date as date
        ),
        candidates as (
            select s.schedule_id, s.train_uid, s.stp_indicator, d.date,
                   case s.stp_indicator
                       when 'C' then 4 when 'N' then 3
                       when 'O' then 2 when 'P' then 1 else 0
                   end as priority
            from train_schedule s
            join spine d on d.date between s.runs_from and s.runs_to
            where case dayofweek(d.date)
                      when 1 then s.monday    when 2 then s.tuesday
                      when 3 then s.wednesday when 4 then s.thursday
                      when 5 then s.friday    when 6 then s.saturday
                      when 0 then s.sunday
                  end
        ),
        ranked as (
            select *, row_number() over (
                partition by train_uid, date
                order by priority desc, schedule_id desc
            ) as rn
            from candidates
        )
        select schedule_id, train_uid, date, stp_indicator
        from ranked
        where rn = 1 and stp_indicator <> 'C'
    """)

    # Kept for reporting: how much of the timetable is cancelled, which is a
    # useful smell test that overlay resolution is doing something.
    cancelled = connection.execute(f"""
        with spine as (
            select unnest(generate_series(
                date '{start}', date '{end}', interval 1 day
            ))::date as date
        ),
        candidates as (
            select s.train_uid, s.stp_indicator, d.date,
                   case s.stp_indicator
                       when 'C' then 4 when 'N' then 3
                       when 'O' then 2 when 'P' then 1 else 0
                   end as priority
            from train_schedule s
            join spine d on d.date between s.runs_from and s.runs_to
            where case dayofweek(d.date)
                      when 1 then s.monday    when 2 then s.tuesday
                      when 3 then s.wednesday when 4 then s.thursday
                      when 5 then s.friday    when 6 then s.saturday
                      when 0 then s.sunday
                  end
        ),
        ranked as (
            select *, row_number() over (
                partition by train_uid, date order by priority desc
            ) as rn
            from candidates
        )
        select count(*) from ranked where rn = 1 and stp_indicator = 'C'
    """).fetchone()[0]

    scalar = lambda sql: connection.execute(sql).fetchone()[0]
    return TimetableCounts(
        schedules=scalar("select count(*) from train_schedule"),
        stops=scalar("select count(*) from schedule_stop"),
        service_dates=scalar("select count(*) from service_date"),
        cancelled_dates=cancelled,
        horizon_start=start,
        horizon_end=end,
    )
