"""`rail` command line."""

from __future__ import annotations

import datetime as dt

import typer
from rich.console import Console
from rich.table import Table

from .acquire import Feed, NRDPSource, PollTooSoon, SnapshotStore
from .config import load_config
from .model import (
    add_ons_from,
    build_all,
    cheapest_from,
    fare_options,
    eligible_railcards,
    run_checks,
    snapshot_parquet_dir,
)
from .parse import ingest_snapshot

app = typer.Typer(
    help="UK rail schedule and fares analysis (RDG DTD feeds).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


#: Tables show everything by default. `--limit` is for when you want less, and
#: the notice below fires only when it has actually held something back - a
#: truncated table with no marker reads as the whole answer, which is how you
#: conclude a station is unreachable when it was row 21.
SHOW_EVERYTHING = 0


def _shown(displayed: int, total: int, *, what: str = "rows") -> None:
    """Say so when a table is only part of the answer."""
    if displayed >= total:
        return
    console.print(
        f"[yellow]Showing {displayed:,} of {total:,} {what}.[/yellow]"
        " [dim]Omit --limit for all of them.[/dim]"
    )


def _store() -> SnapshotStore:
    return SnapshotStore(load_config().raw_dir)


@app.command()
def fetch(
    feed: str = typer.Option(
        "all", "--feed", help="timetable, fares, routeing, or all."
    ),
    force: bool = typer.Option(
        False, "--force", help="Override the once-daily poll guard."
    ),
    supplementary: bool = typer.Option(
        False,
        "--supplementary",
        help="Fetch RSPS5052 reference data instead. Different source, "
             "different licence - see docs/DATA-SOURCES.md.",
    ),
) -> None:
    """Download feed ZIPs from the National Rail Data Portal.

    Skips the body when Last-Modified is unchanged, but still counts as feed
    consumption - which is what keeps the NRDP account from being deleted after
    ~30 days of inactivity.
    """
    config = load_config()

    if supplementary:
        _fetch_supplementary(config)
        return

    try:
        username, password = config.require_credentials()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    feeds = list(Feed) if feed == "all" else [Feed(feed)]
    store = SnapshotStore(config.raw_dir)
    source = NRDPSource(store, username, password, state_dir=config.data_dir)

    failures = 0
    for target in feeds:
        try:
            result = source.fetch(target, force=force)
        except PollTooSoon as exc:
            console.print(f"[yellow]{target.value}: skipped[/yellow] - {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - surface any feed failure, keep going
            console.print(f"[red]{target.value}: failed[/red] - {exc}")
            failures += 1
            continue

        colour = "green" if result.downloaded else "cyan"
        console.print(
            f"[{colour}]{target.value}: {result.filename}[/{colour}] - {result.reason}"
        )

    if failures:
        raise typer.Exit(1)


def _fetch_supplementary(config) -> None:
    """RSPS5052, which is a different source under different terms.

    No credentials are involved and none should be: this is a public bucket,
    not the licensee-personal NRDP. It also has no poll guard, because the
    High Volume Usage charging that guard exists for is an NRDP term and does
    not apply here.
    """
    from .acquire.supplementary import fetch_supplementary, ingest_supplementary

    try:
        results = fetch_supplementary(config.raw_dir)
    except Exception as exc:  # noqa: BLE001 - a third-party bucket, report and stop
        console.print(f"[red]supplementary: failed[/red] - {exc}")
        raise typer.Exit(1)

    for result in results:
        console.print(
            f"[green]{result.filename}[/green] - {result.rows:,} records, "
            f"{result.size:,} bytes, modified {result.last_modified or 'unknown'}"
        )
    written = ingest_supplementary(config.raw_dir, config.parquet_dir)
    for name, rows in sorted(written.items()):
        console.print(f"  [cyan]{name}[/cyan] {rows:,} rows")
    console.print(
        "[yellow]RSPS5052 is not a DTD feed[/yellow] - the NRDP terms do not "
        "cover it. Check its own licensing before publishing anything derived "
        "from it."
    )


@app.command()
def ingest(
    feed: str = typer.Option("all", "--feed", help="timetable, fares, routeing, or all."),
    only: str = typer.Option(
        "", "--only", help="Comma-separated file extensions, e.g. MCA,MSN."
    ),
) -> None:
    """Parse the latest snapshot of each feed into Parquet."""
    config = load_config()
    store = SnapshotStore(config.raw_dir)
    feeds = list(Feed) if feed == "all" else [Feed(feed)]
    extensions = {e.strip().upper() for e in only.split(",") if e.strip()} or None

    for target in feeds:
        manifest = store.latest(target)
        if manifest is None:
            console.print(f"[yellow]{target.value}: no snapshot - run `rail fetch`.[/yellow]")
            continue

        console.print(f"[bold]{target.value}[/bold] - parsing {manifest.filename}")
        report = ingest_snapshot(
            store.path_for(manifest), manifest, config.parquet_dir, only=extensions
        )

        table = Table("file", "status", "lines", "tables (rows)")
        for entry in report.files:
            rows = ", ".join(f"{k} {v:,}" for k, v in sorted(entry.rows.items()))
            table.add_row(entry.member, entry.status, f"{entry.lines:,}", rows or "-")
        console.print(table)

        unknown = {
            key: count
            for entry in report.files
            for key, count in entry.unknown_records.items()
        }
        if unknown:
            console.print(f"[yellow]Unrecognised record types: {unknown}[/yellow]")
        console.print(f"[green]{report.total_rows:,} rows → {report.output_dir}[/green]")


@app.command()
def build(
    horizon: int = typer.Option(
        90, "--horizon", help="Days of running dates to materialise."
    ),
) -> None:
    """Build the DuckDB query surface from the ingested Parquet."""
    import duckdb

    config = load_config()
    timetable_dir = snapshot_parquet_dir(config, Feed.TIMETABLE)
    fares_dir = snapshot_parquet_dir(config, Feed.FARES)

    # Optional: absent unless `rail fetch --supplementary` has been run.
    supplementary_dir = config.parquet_dir / "supplementary"
    if not supplementary_dir.exists():
        supplementary_dir = None

    config.data_dir.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(config.db_path))
    built = build_all(connection, config, horizon_days=horizon)
    counts = built.reference
    timetable = built.timetable
    kinds = built.kinds
    fares = built.fares
    restrictions = built.restrictions
    validities = built.validities
    railcards = built.railcards
    associations = built.associations
    plusbus = built.plusbus
    routeing = built.routeing

    table = Table("table", "rows")
    table.add_row("station", f"{counts.stations:,}")
    table.add_row("station_tiploc", f"{counts.tiplocs:,}")
    table.add_row(
        "station by kind",
        ", ".join(f"{n:,} {kind}" for kind, n in kinds.items()),
    )
    table.add_row("station_nlc (priceable)", f"{counts.priced:,}")
    table.add_row("station_cluster", f"{counts.clusters:,}")
    table.add_row("station_alias", f"{counts.aliases:,}")
    table.add_row("reference_reject", f"{counts.rejects:,}")
    table.add_row("train_schedule", f"{timetable.schedules:,}")
    table.add_row("schedule_stop", f"{timetable.stops:,}")
    table.add_row("service_date", f"{timetable.service_dates:,}")
    table.add_row("fare_alias", f"{fares.aliases:,}")
    table.add_row(
        "ticket_calendar_current",
        f"{fares.calendar_bars:,}"
        + (f" ({fares.calendar_unsettled:,} not judged)"
           if fares.calendar_unsettled else ""),
    )
    table.add_row(
        "ticket_type_current",
        f"{fares.ticket_types:,} ({fares.walk_up:,} walk-up)",
    )
    table.add_row(
        "restriction_band",
        f"{restrictions.bands:,} ({restrictions.toc_qualifiers:,} TOC-qualified)",
    )
    table.add_row(
        "ticket_validity_current",
        f"{validities.codes:,} ({validities.returns:,} walk-up returns)",
    )
    table.add_row("railcard_current", f"{railcards.railcards:,}")
    table.add_row("railcard_discount", f"{railcards.discounts:,}")
    table.add_row("association_link", f"{associations.links:,}")
    table.add_row("plusbus_zone", f"{plusbus.zones:,}")
    if routeing is not None:
        table.add_row("permitted_route", f"{routeing.routes:,}")
    console.print(table)
    for reason, count in fares.rejected:
        console.print(f"  [yellow]not walk-up[/yellow] {count:,} - {reason}")
    console.print(
        f"Running dates {timetable.horizon_start} → {timetable.horizon_end}; "
        f"[yellow]{timetable.cancelled_dates:,}[/yellow] train-days cancelled by STP."
    )
    console.print(f"[green]{config.db_path}[/green]")

    rejects = connection.execute(
        "select reason, count(*) n from reference_reject group by 1 order by 2 desc"
    ).fetchall()
    for reason, count in rejects:
        console.print(f"  [yellow]excluded[/yellow] {count:,} - {reason}")
    connection.close()


@app.command()
def stations(
    search: str = typer.Argument("", help="CRS code or part of a station name."),
    limit: int = typer.Option(
        SHOW_EVERYTHING, "--limit", help="Rows to show; 0 or unset for all."
    ),
) -> None:
    """Look up stations in the crosswalk."""
    import duckdb

    config = load_config()
    if not config.db_path.exists():
        console.print("[red]No database yet - run `rail build`.[/red]")
        raise typer.Exit(1)

    connection = duckdb.connect(str(config.db_path), read_only=True)
    rows = connection.execute(
        """
        select s.crs, s.name, n.nlc, n.fare_group, s.interchange_minutes,
               (select count(*) from station_tiploc t where t.crs = s.crs) as tiplocs,
               s.kind, s.is_rail_station
        from station s left join station_nlc n using (crs)
        where upper(s.crs) = upper($term) or s.name ilike '%' || $term || '%'
        order by (upper(s.crs) = upper($term)) desc, s.name
        """,
        {"term": search},
    ).fetchall()
    connection.close()

    if not rows:
        console.print(f"[yellow]Nothing matching {search!r}.[/yellow]")
        return

    shown = rows[:limit] if limit else rows
    table = Table("crs", "name", "nlc", "fare group", "interchange", "tiplocs",
                  "kind")
    for crs, name, nlc, group, interchange, tiploc_count, kind, is_rail in shown:
        # MSN mixes bus stops, ferry piers and Metro stations in with stations.
        # `kind` comes from what actually calls there; RSPS5052's own answer is
        # noted only where the two differ, which is how new stations show up.
        label = kind or "?"
        if kind == "rail" and is_rail is False:
            label = "rail [dim](new)[/dim]"
        table.add_row(
            crs, name, nlc or "-", group or "-",
            f"{interchange} min" if interchange else "-", str(tiploc_count), label,
        )
    console.print(table)
    _shown(len(shown), len(rows), what="matches")


@app.command(name="journey-times")
def journey_times(
    origin: str = typer.Option(..., "--from", help="Origin CRS code, e.g. YRK."),
    date: str = typer.Option(..., "--date", help="Travel date, YYYY-MM-DD."),
    depart: str = typer.Option("09:00", "--depart", help="Earliest departure, HH:MM."),
    profile: bool = typer.Option(
        False, "--profile",
        help="Sweep departures across the day and keep the best per station.",
    ),
    until: str = typer.Option("20:00", "--until", help="Last departure when profiling."),
    step: int = typer.Option(30, "--step", help="Profile interval in minutes."),
    limit: int = typer.Option(
        SHOW_EVERYTHING, "--limit", help="Rows to show; 0 or unset for all."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Journey time from one station to every other station on a date."""
    import datetime as dt
    import json as jsonlib

    import duckdb

    from .engine import best_over_window, earliest_arrival, load_network

    config = load_config()
    if not config.db_path.exists():
        console.print("[red]No database yet - run `rail build`.[/red]")
        raise typer.Exit(1)

    travel_date = dt.date.fromisoformat(date)
    connection = duckdb.connect(str(config.db_path), read_only=True)
    timetable_dir = snapshot_parquet_dir(config, Feed.TIMETABLE)
    network = load_network(connection, travel_date, timetable_dir=timetable_dir)

    # (crs, name, journey, arrival, elapsed). Under `--profile` the arrival and
    # the elapsed time are both None on purpose: each belongs to one departure,
    # and the window is an answer across many of them.
    if profile:
        minutes_by_crs = best_over_window(
            network, origin.upper(),
            first_departure=_hhmm(depart), last_departure=_hhmm(until), step=step,
        )
        rows = sorted(
            ((crs, network.names[network.index[crs]], minutes, None, None)
             for crs, minutes in minutes_by_crs.items()),
            key=lambda row: row[2],
        )
    else:
        result = earliest_arrival(network, origin.upper(), _hhmm(depart))
        rows = [(j.crs, j.name, result.journey_minutes_to(j.crs), j.arrival,
                 j.minutes)
                for j in result.reached()]

    connection.close()

    if as_json:
        print(jsonlib.dumps(
            {
                "origin": origin.upper(),
                "date": str(travel_date),
                "depart": depart,
                "profile": profile,
                "dayname": travel_date.strftime("%A"),
                "reached": len(rows),
                "stations": [
                    # `journey` is the travelling time and `elapsed` counts from
                    # the query. `minutes` is kept as an alias of `elapsed` so a
                    # consumer written against the old shape still reads.
                    {"crs": crs, "name": name,
                     "journey": journey,
                     "elapsed": elapsed,
                     "minutes": elapsed,
                     "arrival": _fmt(arrival) if arrival is not None else None}
                    for crs, name, journey, arrival, elapsed in rows
                ],
            },
            indent=2,
        ))
        return

    console.print(
        f"[bold]{origin.upper()}[/bold] on {travel_date} ({travel_date.strftime('%A')}), "
        f"{'departures ' + depart + '–' + until if profile else 'departing from ' + depart}"
        f" - reached [green]{len(rows):,}[/green] stations"
    )
    hm = lambda n: "-" if n is None else f"{n // 60}h{n % 60:02d}"
    # Journey time leads because it is the number a timetable would show.
    # `elapsed` includes the wait for the first train, so the two differ by
    # however long you stood on the platform - which is worth seeing, not hiding.
    table = Table("crs", "station", "journey", "arrive", "elapsed")
    shown = rows[:limit] if limit else rows
    for crs, name, journey, arrival, elapsed in shown:
        table.add_row(crs, name, hm(journey),
                      _fmt(arrival) if arrival is not None else "-", hm(elapsed))
    console.print(table)
    _shown(len(shown), len(rows), what="stations")
    if profile:
        console.print("[dim]Journey time is the shortest across the window. "
                      "There is no single arrival or elapsed time for a "
                      "sweep.[/dim]")


@app.command()
def reachable(
    origin: str = typer.Option(..., "--from", help="Origin CRS code, e.g. YRK."),
    date: str = typer.Option(..., "--date", help="Travel date, YYYY-MM-DD."),
    max_fare: float = typer.Option(
        ..., "--max-fare", help="Fare ceiling in pounds, e.g. 20."
    ),
    depart: str = typer.Option("09:00", "--depart", help="Departure time, HH:MM."),
    return_on: str = typer.Option(
        "", "--return-on",
        help="Return date, YYYY-MM-DD. Only offers returns whose validity "
             "permits coming back then; singles are unaffected.",
    ),
    ignore_restrictions: bool = typer.Option(
        False, "--ignore-restrictions",
        help="Quote fares without checking they are valid at this time.",
    ),
    plusbus: bool = typer.Option(
        False, "--plusbus",
        help="Add each destination's PlusBus day ticket to the fare.",
    ),
    check_guide: bool = typer.Option(
        False, "--check-guide",
        help="Drop destinations whose journey is not a permitted route under "
             "the National Routeing Guide.",
    ),
    check_routes: bool = typer.Option(
        False, "--check-routes",
        help="Only quote fares valid on the journey found, rather than the "
             "cheapest fare to the destination by any route.",
    ),
    railcard: str = typer.Option("", "--railcard", help="Railcard code, e.g. YNG."),
    advance: bool = typer.Option(
        False, "--advance",
        help="Include Advance price points. Real prices, but the feed carries no "
             "availability, so they are not necessarily bookable.",
    ),
    first_class: bool = typer.Option(False, "--first-class"),
    limit: int = typer.Option(
        SHOW_EVERYTHING, "--limit", help="Rows to show; 0 or unset for all."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Stations reachable from an origin within a fare ceiling.

    By default the fare is the cheapest to that destination by any route, which
    is what "where can I get for £20" means - you would pick a route to suit the
    fare. `--check-routes` instead prices the journey actually found, refusing
    fares whose route conditions it breaks.
    """
    import datetime as dt
    import json as jsonlib

    import duckdb

    from .engine import UNREACHABLE as UNREACHABLE_SENTINEL, earliest_arrival, load_network

    config = load_config()
    if not config.db_path.exists():
        console.print("[red]No database yet - run `rail build`.[/red]")
        raise typer.Exit(1)

    travel_date = dt.date.fromisoformat(date)
    depart_minutes = _hhmm(depart)
    connection = duckdb.connect(str(config.db_path), read_only=True)
    network = load_network(
        connection, travel_date,
        timetable_dir=snapshot_parquet_dir(config, Feed.TIMETABLE),
    )

    # A single departure rather than a sweep, because a fare's validity depends
    # on when the journey is actually made.
    back_on = dt.date.fromisoformat(return_on) if return_on else None
    if back_on is not None and back_on < travel_date:
        console.print("[red]--return-on is before the travel date.[/red]")
        raise typer.Exit(1)
    result = earliest_arrival(network, origin.upper(), depart_minutes)
    journeys = result.reached()
    minutes_by_crs = {j.crs: j.minutes for j in journeys}
    # The travelling time, as against `minutes`, which counts from `--depart`
    # and so includes waiting for the first train. Looked up by CRS at render
    # rather than threaded through the fare rows, which are already wide.
    journey_by_crs = {
        j.crs: result.journey_minutes_to(j.crs) for j in journeys
    }
    arrivals = {j.crs: j.arrival for j in journeys}
    journey_paths = result.paths()

    priced = fare_options(
        connection, snapshot_parquet_dir(config, Feed.FARES),
        origin.upper(), travel_date,
        ticket_class=1 if first_class else 2,
        depart_minutes=None if ignore_restrictions else depart_minutes,
        arrivals=None if ignore_restrictions else arrivals,
        railcard=railcard.upper() or None,
        include_advance=advance,
        paths=journey_paths if check_routes else None,
        operators=result.operators() if check_routes else None,
        modes=result.modes() if check_routes else None,
        return_on=back_on,
        # Unconditional, unlike the route conditions: a restriction barring a
        # change of trains is a property of the fare, not a stricter reading of
        # the journey, and the sweep has already routed every destination.
        changes=None if ignore_restrictions else result.changes(),
        # Same reasoning for the calling points. A band naming a station the
        # journey passes through is an ordinary time restriction - 32,206 of
        # the 33,216 current bands name a station - and the journey is routed
        # either way.
        calls=None if ignore_restrictions else result.calls(),
        # And where each train is boarded, for the same reason again. A
        # departure band bars *trains*, so a station the journey leaves on foot
        # is not one it can bite at - `is_change` alone is true there too.
        # Supplied unconditionally, so this command and any other caller that
        # routes agree about which bands apply.
        boardings=None if ignore_restrictions else {
            crs: [(leg.board, leg.operator or "", leg.alight)
                  for leg in (result.legs_to(crs) or [])]
            for crs in result.paths()},
    )
    options: dict[str, list] = {}
    for row in priced:
        options.setdefault(row[0], []).append(row)

    # Without the guide, the cheapest fare is the answer.
    fares = {crs: rows[0] for crs, rows in options.items()}
    superseded: dict[str, tuple] = {}
    off_route: set[str] = set()
    no_valid_fare: set[str] = set()

    if check_guide:
        from .model import RouteingGuide

        guide = RouteingGuide.load(connection)
        # The guide is asked about a *fare on a journey*, not about the journey
        # alone: most easements left open by a bare origin/destination question
        # say "customers with tickets routed X", and each fare carries its own
        # route. So walk the prices upwards and take the first the guide does
        # not refuse - the cheapest fare is often invalid on the fastest route
        # while a dearer one, priced VIA somewhere, is fine.
        fares = {}
        for crs, candidates in options.items():
            path = result.path_to(crs)
            for candidate in candidates:
                if guide.permits(
                    origin.upper(), crs, path, date=travel_date,
                    route_code=candidate[5], ticket_code=candidate[1],
                    # RSPS5047 7.1.1: a through train is permitted outright.
                    changes=result.changes_to(crs),
                    # RGH ties easements to operators, and the router already
                    # walks the journey to collect them for the route
                    # conditions - the same set, so the same call.
                    operators=result.operators_to(crs),
                ) is not False:
                    fares[crs] = candidate
                    if candidate is not candidates[0]:
                        superseded[crs] = candidates[0]
                    break
            else:
                # Two quite different reasons for having no fare, and lumping
                # them together misleads. Either the journey itself is off the
                # permitted route for this pair, in which case no ticket helps
                # and the answer is "go a different way"; or the maps allow the
                # journey and it is the fares that fail on it, in which case a
                # fare this query never saw may still be valid.
                if guide.permits(
                    origin.upper(), crs, path, date=travel_date,
                    changes=result.changes_to(crs),
                    operators=result.operators_to(crs),
                ) is False:
                    off_route.add(crs)
                else:
                    no_valid_fare.add(crs)
    # The add-on is bought at the destination, so it is part of what the
    # journey costs if you want to get about when you arrive. The origin's own
    # zone and every excluded pair are already filtered out.
    add_ons = (
        add_ons_from(connection, origin.upper(), travel_date) if plusbus else {}
    )
    unpriced_kinds = dict(connection.execute("""
        select kind, count(*) from station
        where crs in (select unnest($crs)) group by 1 order by 2 desc
    """, {"crs": [c for c in minutes_by_crs if c not in options]}).fetchall())
    connection.close()

    ceiling = round(max_fare * 100)
    rows = sorted(
        (
            (crs, fares[crs][2], fares[crs][3] + add_ons.get(crs, 0), minutes,
             fares[crs][4], superseded.get(crs), add_ons.get(crs),
             fares[crs][6])
            for crs, minutes in minutes_by_crs.items()
            if crs in fares and fares[crs][3] + add_ons.get(crs, 0) <= ceiling
        ),
        key=lambda row: row[2],
    )

    unpriced = sum(1 for crs in minutes_by_crs if crs not in options)

    if as_json:
        print(jsonlib.dumps({
            "origin": origin.upper(),
            "date": str(travel_date),
            "dayname": travel_date.strftime("%A"),
            "max_fare_pence": ceiling,
            "return_on": str(back_on) if back_on else None,
            "reachable": len(minutes_by_crs),
            "within_budget": len(rows),
            "unpriced": unpriced,
            # "No walk-up fare" mostly means "not a place you buy a rail ticket
            # to". Saying which kind turns a bare count into an answer.
            "unpriced_by_kind": unpriced_kinds,
            # The journey found is not a permitted route to these at all, so
            # no ticket makes it valid - a different itinerary might.
            "off_permitted_route": len(off_route & minutes_by_crs.keys()),
            # The route is fine; every fare this query saw fails on it.
            "no_valid_fare_on_this_route": len(
                no_valid_fare & minutes_by_crs.keys()
            ),
            "advance_included": advance,
            "stations": [
                {"crs": crs, "ticket": ticket, "pence": pence,
                 "minutes": minutes, "advance": is_advance,
                 # Present only when the cheapest fare was not the one quoted:
                 # the routeing guide refuses it on the journey found here.
                 **({"plusbus_pence": add_on} if add_on else {}),
                 **({"cheaper_but_invalid": {
                        "ticket": beaten[2], "pence": beaten[3],
                        "reason": "the routeing guide does not permit this "
                                  "route for that fare",
                    }} if beaten else {}),
                 # 'S' single, 'R' return, 'N' season. A return can undercut
                 # two singles and win, so the price alone does not say what
                 # was quoted. `rail fares` spells out the return window.
                 "ticket_type": tkt_type,
                 # `minutes` counts from --depart; `journey` is the travelling
                 # time. `minutes` keeps its meaning so existing consumers read.
                 "journey": journey_by_crs.get(crs),
                 }
                for crs, ticket, pence, minutes, is_advance, beaten, add_on,
                    tkt_type in rows
            ],
        }, indent=2))
        return

    console.print(
        f"[bold]{origin.upper()}[/bold] on {travel_date} "
        f"({travel_date.strftime('%A')}) - [green]{len(rows):,}[/green] of "
        f"{len(minutes_by_crs):,} reachable stations cost £{max_fare:,.2f} or less"
        + (f"; {unpriced:,} had no walk-up fare"
           + (" (" + ", ".join(f"{n:,} {kind}" for kind, n in unpriced_kinds.items())
              + ")" if unpriced_kinds else "")
           if unpriced else "")
        + (f"; {len(off_route & minutes_by_crs.keys()):,} are off the "
           "guide's permitted route however you ticket them"
           if off_route & minutes_by_crs.keys() else "")
        + (f"; {len(no_valid_fare & minutes_by_crs.keys()):,} had no fare "
           "valid on the route found"
           if no_valid_fare & minutes_by_crs.keys() else "")
    )
    columns = ["crs", "station", "fare", "journey", "elapsed", "ticket", "type",
               "validity"]
    if plusbus:
        columns.insert(3, "of which bus")
    table = Table(*columns)
    shown = rows[:limit] if limit else rows
    for crs, ticket, pence, minutes, is_advance, beaten, add_on, tkt_type in shown:
        cells = [crs, network.names[network.index[crs]], f"£{pence / 100:,.2f}"]
        if plusbus:
            cells.append(f"£{add_on / 100:,.2f}" if add_on else "-")
        travelling = journey_by_crs.get(crs)
        table.add_row(
            *cells,
            "-" if travelling is None
            else f"{travelling // 60}h{travelling % 60:02d}",
            f"{minutes // 60}h{minutes % 60:02d}", ticket,
            ("[yellow]advance[/yellow]" if is_advance else "walk-up")
            # A return sometimes undercuts two singles and wins here. Saying so
            # is the point: the price is for a round trip, not for getting there.
            + ("[cyan] return[/cyan]" if tkt_type == "R" else ""),
            # Silence here would be the misleading part: the cheapest fare to
            # this station is cheaper than the one quoted, and the reason it is
            # not on offer is the route this journey takes.
            f"[yellow]£{beaten[3] / 100:,.2f} {beaten[1]} not valid this way"
            f"[/yellow]" if beaten else "",
        )
    console.print(table)
    _shown(len(shown), len(rows), what="priced destinations")
    if back_on is not None:
        console.print(
            f"[dim]Returns are limited to those valid for a journey back on "
            f"{back_on}; where none is, the destination falls back to a single, "
            f"which prices the outward leg only. Two singles are a different "
            f"question and often the cheaper answer.[/dim]"
        )
    if check_guide:
        console.print(
            "[dim]Validity here means the routeing guide permits this fare's "
            "route on the journey found - the maps for the pair, plus any "
            "easement in force on the day. Where the cheapest fare fails that, "
            "the next cheapest that passes is quoted instead.[/dim]"
        )
    if advance:
        console.print(
            "[dim]Advance prices are real and vary with distance, but the feed "
            "carries no quota - nothing here says a given price point is on sale "
            "for a given train.[/dim]"
        )


def _hhmm(value: str) -> int:
    hours, _, minutes = value.partition(":")
    return int(hours) * 60 + int(minutes or 0)


def _fmt(minutes: int) -> str:
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}" + (
        " +1d" if minutes >= 1440 else ""
    )


@app.command()
def snapshots() -> None:
    """List stored feed snapshots."""
    store = _store()
    table = Table("feed", "file", "seq", "size", "last-modified", "fetched")
    total = 0
    for feed in Feed:
        for manifest in store.manifests(feed):
            total += 1
            table.add_row(
                manifest.feed,
                manifest.filename,
                str(manifest.sequence or "-"),
                f"{manifest.size / 1e6:.1f} MB",
                manifest.last_modified or "-",
                manifest.fetched_at,
            )
    if not total:
        console.print("[yellow]No snapshots yet. Run `rail fetch`.[/yellow]")
        return
    console.print(table)


if __name__ == "__main__":
    app()


@app.command()
def railcards(
    search: str = typer.Argument("", help="Filter by code or name, e.g. 'senior'."),
    adults: int = typer.Option(1, "--adults"),
    children: int = typer.Option(0, "--children"),
    all_codes: bool = typer.Option(
        False, "--all", help="Include internal TOC codes, not just public railcards."
    ),
    limit: int = typer.Option(
        SHOW_EVERYTHING, "--limit", help="Rows to show; 0 or unset for all."
    ),
) -> None:
    """Railcards a party of this shape can use."""
    import duckdb

    config = load_config()
    if not config.db_path.exists():
        console.print("[red]No database yet - run `rail build`.[/red]")
        raise typer.Exit(1)

    connection = duckdb.connect(str(config.db_path), read_only=True)
    rows = eligible_railcards(
        connection, adults=adults, children=children, public_only=not all_codes
    )
    discounts = dict(connection.execute(
        "select railcard_code, max(discount_percentage) from railcard_discount group by 1"
    ).fetchall())
    connection.close()

    if search:
        term = search.upper()
        rows = [
            r for r in rows
            if term in (r[0] or "").upper() or term in (r[1] or "").upper()
        ]

    shown = rows[:limit] if limit else rows
    table = Table("code", "railcard", "max discount")
    for code, description in shown:
        per_mille = discounts.get(code)
        table.add_row(
            code, description or "-", f"{per_mille / 10:.1f}%" if per_mille else "-"
        )
    console.print(table)
    _shown(len(shown), len(rows), what="railcards")
    console.print(
        f"[dim]{len(rows)} for {adults} adult(s), {children} child(ren). "
        "The list includes corporate and delegate schemes, which RSP models as "
        "railcards too. Railcard fields in this feed are known to contain "
        "errors - spot-check anything that matters.[/dim]"
    )


@app.command()
def tickets(
    search: str = typer.Argument(
        "", help="Filter by ticket code, description or class, e.g. 'advance'."),
    review_only: bool = typer.Option(
        False, "--review",
        help="Show only what is new or has changed class since the register, "
             "and exit 1 if anything unreviewed is already carrying fares."),
    accept_all: bool = typer.Option(
        False, "--accept",
        help="Record the current classification as reviewed. Do this after "
             "checking the answers, not instead of it - it is a commit."),
    limit: int = typer.Option(
        SHOW_EVERYTHING, "--limit", help="Rows to show; 0 or unset for all."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Every ticket type, the class it is in, and what has changed since.

    Three classes, and each is used by something different:

    \b
      walk-up            what `rail reachable` prices by default
      advance            what `--advance` adds and `advance_only` prices alone
      not-a-real-advance sold, but not as an Advance anyone can buy
      rejected           not a fare to somewhere; `reason` says why

    **A new generation brings new ticket types, and a misclassified one is
    silent** - it lands in the wrong class and wins, the wrong class being
    nearly always the cheaper one. `--review` is the prompt to look.
    """
    import duckdb

    from .acquire import Feed, SnapshotStore
    from .model import review_tickets, accept_tickets, snapshot_parquet_dir

    config = load_config()
    if not config.db_path.exists():
        console.print("[red]No database yet - run `rail build`.[/red]")
        raise typer.Exit(1)

    manifest = SnapshotStore(config.raw_dir).latest(Feed.FARES)
    snapshot = manifest.filename.rsplit(".", 1)[0] if manifest else ""
    connection = duckdb.connect(str(config.db_path), read_only=True)
    result = review_tickets(
        connection, snapshot_parquet_dir(config, Feed.FARES), snapshot=snapshot)
    connection.close()

    if accept_all:
        from .model import REGISTER

        written = accept_tickets(result)
        console.print(
            f"[green]{written:,}[/green] ticket types recorded as reviewed "
            f"against {snapshot or 'this build'}.")
        console.print(f"[dim]{REGISTER} - commit it. The diff is the review.[/dim]")
        return

    unreviewed = set(result.added) | {code for code, _, _ in result.moved}
    rows = [
        (code, entry["description"], entry["class"], entry.get("reason", ""),
         result.fares.get(code, 0), code in unreviewed)
        for code, entry in result.current.items()
    ]
    if review_only:
        rows = [r for r in rows if r[5]]
    if search:
        term = search.upper()
        rows = [r for r in rows
                if term in r[0].upper() or term in (r[1] or "").upper()
                or term in r[2].upper()]
    # Unreviewed first, then by how much each one could move a price: a code
    # with no fares cannot be wrong about anything yet.
    rows.sort(key=lambda r: (not r[5], -r[4], r[0]))

    if as_json:
        console.print_json(data={
            "snapshot": snapshot,
            "added": result.added,
            "moved": [{"ticket_code": c, "was": w, "now": n}
                      for c, w, n in result.moved],
            "withdrawn": result.withdrawn,
            "tickets": [
                {"ticket_code": c, "description": d, "class": k,
                 "reason": reason or None, "fares": n, "unreviewed": new}
                for c, d, k, reason, n, new in rows
            ],
        })
        raise typer.Exit(1 if review_only and result.carrying_fares() else 0)

    shown = rows[:limit] if limit else rows
    table = Table("code", "description", "class", "fares", "why", "new")
    for code, description, kind, reason, fares, is_new in shown:
        table.add_row(
            code, description or "-", kind, f"{fares:,}" if fares else "-",
            reason or "-", "yes" if is_new else "")
    console.print(table)
    _shown(len(shown), len(rows), what="ticket types")

    if result.withdrawn:
        console.print(
            f"[dim]{len(result.withdrawn):,} the register knows and this "
            f"generation no longer ships: "
            f"{', '.join(result.withdrawn[:8])}"
            + (", …" if len(result.withdrawn) > 8 else "") + "[/dim]")
    for code, was, now in result.moved[:12]:
        console.print(
            f"[yellow]moved[/yellow] {code} "
            f"{result.current[code]['description']}: {was} -> {now}"
            + (f"  ({result.fares[code]:,} fares)" if result.fares.get(code) else ""))
    if len(result.moved) > 12:
        console.print(f"[dim]… and {len(result.moved) - 12:,} more[/dim]")

    if result.settled:
        console.print("[green]Nothing new since the register.[/green]")
        return

    # A code with no fares is a product an operator has registered and not yet
    # filed prices for; it can wait. One that is already pricing journeys cannot,
    # and that is the only thing here that sets an exit code.
    pressing = result.carrying_fares()
    console.print(
        f"[dim]{len(result.added):,} new, {len(result.moved):,} changed class. "
        f"Check them, change the rules in model/fares.py if any is wrong, then "
        f"`rail tickets --accept`.[/dim]")
    if pressing:
        console.print(
            f"[red]{len(pressing):,} of them already carry fares[/red] "
            f"[dim]- {', '.join(pressing[:8])}"
            + (", …" if len(pressing) > 8 else "") + "[/dim]")
        if review_only:
            raise typer.Exit(1)


@app.command()
def validate(
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Data-quality report for the built database.

    Run it after every refresh. The bands are loose enough not to fire on a
    normal feed update, and tight enough to catch a pipeline that has broken.
    """
    import json as jsonlib

    import duckdb

    config = load_config()
    if not config.db_path.exists():
        console.print("[red]No database yet - run `rail build`.[/red]")
        raise typer.Exit(1)

    connection = duckdb.connect(str(config.db_path), read_only=True)
    checks = run_checks(
        connection,
        snapshot_parquet_dir(config, Feed.TIMETABLE),
        snapshot_parquet_dir(config, Feed.FARES),
        naptan_dir=config.parquet_dir / "naptan",
    )
    connection.close()

    if as_json:
        print(jsonlib.dumps(
            [
                {"category": c.category, "check": c.name,
                 "status": c.status, "detail": c.detail}
                for c in checks
            ],
            indent=2,
        ))
        raise typer.Exit(1 if any(c.failed for c in checks) else 0)

    marks = {"ok": "[green]ok[/green]", "warn": "[yellow]warn[/yellow]",
             "fail": "[red]FAIL[/red]"}
    current = None
    for check in checks:
        if check.category != current:
            current = check.category
            console.print(f"\n[bold]{current}[/bold]")
        console.print(f"  {marks[check.status]:<22} {check.name} - {check.detail}")

    failures = sum(1 for c in checks if c.failed)
    warnings = sum(1 for c in checks if c.status == "warn")
    console.print(
        f"\n{len(checks)} checks: [green]{len(checks) - failures - warnings} ok[/green], "
        f"[yellow]{warnings} warn[/yellow], [red]{failures} fail[/red]"
    )
    if failures:
        raise typer.Exit(1)


@app.command()
def refresh(
    force: bool = typer.Option(False, "--force", help="Override the daily poll guard."),
    rebuild: bool = typer.Option(
        False, "--rebuild", help="Rebuild even when nothing was downloaded."
    ),
    horizon: int = typer.Option(90, "--horizon"),
) -> None:
    """Fetch, ingest what changed, rebuild, and report - for scheduled runs."""
    from .refresh import refresh as run_refresh

    config = load_config()
    try:
        config.require_credentials()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    console.print(f"[bold]refresh[/bold] {stamp}")
    result = run_refresh(
        config, force=force, rebuild_anyway=rebuild, horizon_days=horizon,
        log=lambda message: console.print(f"  {message}"),
    )

    for error in result.errors:
        console.print(f"[red]error[/red] {error}")

    if result.ok:
        console.print(
            "[green]refresh ok[/green]"
            + ("" if result.changed else " - nothing had changed")
        )
        return

    if not result.errors:
        # Every feed hit the daily poll guard, so nothing reached the portal.
        # Harmless when run by hand, but it does not renew the account.
        console.print(
            "[yellow]nothing polled[/yellow] - the daily guard skipped every feed, "
            "so this run does not count towards keeping the account alive. "
            "Use --force to poll anyway."
        )
    raise typer.Exit(1)


@app.command()
def status() -> None:
    """Snapshot ages and how close the NRDP account is to expiring."""
    from .refresh import ACCOUNT_EXPIRY_DAYS, ACCOUNT_WARNING_DAYS, days_since_last_success

    config = load_config()
    store = SnapshotStore(config.raw_dir)

    table = Table("feed", "snapshot", "last modified", "fetched")
    for feed in Feed:
        manifest = store.latest(feed)
        if manifest is None:
            table.add_row(feed.value, "[yellow]none[/yellow]", "-", "-")
            continue
        table.add_row(
            feed.value, manifest.filename,
            manifest.last_modified or "-", manifest.fetched_at,
        )
    console.print(table)

    elapsed = days_since_last_success(config)
    if elapsed is None:
        console.print(
            "[yellow]No successful refresh recorded.[/yellow] Run `rail refresh`."
        )
        return

    remaining = ACCOUNT_EXPIRY_DAYS - elapsed
    if elapsed >= ACCOUNT_WARNING_DAYS:
        console.print(
            f"[red]{elapsed:.0f} days since the last successful fetch.[/red] "
            f"NRDP deletes accounts after about {ACCOUNT_EXPIRY_DAYS} days of no "
            f"consumption - roughly {remaining:.0f} days left. Run `rail refresh`."
        )
    else:
        console.print(
            f"[green]{elapsed:.1f} days[/green] since the last successful fetch; "
            f"about {remaining:.0f} days of account margin."
        )


@app.command()
def restrictions(
    code: str = typer.Argument(..., help="Two-character restriction code, e.g. 0W."),
    date: str = typer.Option("", "--date", help="Travel date, YYYY-MM-DD."),
    limit: int = typer.Option(
        SHOW_EVERYTHING, "--limit", help="Bands to show; 0 or unset for all."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Explain a restriction code: what it is called and when it applies.

    A restriction names time bands during which the fare may *not* be used, so
    every line below is a bar, not a permission.
    """
    import duckdb

    import json as jsonlib
    from .model.restrictions import describe_restriction

    config = load_config()
    if not config.db_path.exists():
        console.print("[red]No database yet - run `rail build`.[/red]")
        raise typer.Exit(1)

    travel_date = dt.date.fromisoformat(date) if date else dt.date.today()
    connection = duckdb.connect(str(config.db_path), read_only=True)
    detail = describe_restriction(
        connection, code.upper(), travel_date,
        snapshot_parquet_dir(config, Feed.FARES),
    )
    connection.close()

    if detail["description"] is None:
        console.print(f"[yellow]No restriction {code.upper()!r} in force on "
                      f"{travel_date}.[/yellow]")
        raise typer.Exit(1)

    if as_json:
        print(jsonlib.dumps({
            **{k: v for k, v in detail.items() if k != "bands"},
            "date": str(travel_date),
            "bands": [
                {"leg": "outward" if b.out_ret == "O" else "return",
                 "from": b.time_from, "to": b.time_to, "sense": b.sense,
                 "location": b.location, "days": b.days, "dates": b.dates,
                 "minimum_fare_instead": b.minimum_fare_instead,
                 "text": b.as_sentence()}
                for b in detail["bands"]
            ],
        }, indent=2))
        return

    console.print(
        f"[bold]{detail['code']}[/bold] {detail['description']} "
        f"[dim](the {'current' if detail['marker'] == 'C' else 'future'} "
        f"set, for travel on {travel_date})[/dim]"
    )
    if detail["note_out"]:
        console.print(f"  [dim]{detail['note_out']}[/dim]")
    if detail["change_allowed"] is False:
        console.print("  [yellow]A change of trains is not allowed.[/yellow]")

    bands = detail["bands"]
    shown = bands[:limit] if limit else bands
    table = Table("leg", "effect", "when", "days", "dates")
    for band in shown:
        where = (f"{band.sense and _SENSE_WORD.get(band.sense, band.sense)} "
                 f"{band.location}" if band.location else "any station")
        table.add_row(
            "outward" if band.out_ret == "O" else "return",
            "[yellow]minimum fare[/yellow]" if band.minimum_fare_instead
            else "[red]not valid[/red]",
            f"{where} {_fmt(band.time_from)}-{_fmt(band.time_to)}",
            band.days,
            "; ".join(band.dates) or "[yellow]no dates - never applies[/yellow]",
        )
    console.print(table)
    _shown(len(shown), len(bands), what="bands")
    console.print(
        "[dim]Only bands at the journey's own origin and destination are "
        "applied when pricing; one naming an intermediate station needs the "
        "itinerary. Return-leg bands are listed but not applied, since only "
        "the outward journey is routed.[/dim]"
    )


_SENSE_WORD = {"D": "departing", "A": "arriving", "V": "changing at"}


@app.command()
def fares(
    origin: str = typer.Option(..., "--from", help="Origin CRS code, e.g. YRK."),
    destination: str = typer.Option(..., "--to", help="Destination CRS code."),
    date: str = typer.Option("", "--date", help="Travel date, YYYY-MM-DD."),
    return_on: str = typer.Option(
        "", "--return-on",
        help="Return date, YYYY-MM-DD. Drops returns that cannot come back then.",
    ),
    railcard: str = typer.Option("", "--railcard", help="Railcard code, e.g. YNG."),
    advance: bool = typer.Option(False, "--advance", help="Include Advance prices."),
    first_class: bool = typer.Option(False, "--first-class", help="First class only."),
    plusbus: bool = typer.Option(
        False, "--plusbus", help="Also show the PlusBus add-on at either end."
    ),
    limit: int = typer.Option(
        SHOW_EVERYTHING, "--limit", help="Fares to show; 0 or unset for all."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Every fare between two stations, and what governs the use of each.

    Deliberately unfiltered by time: a peak-barred fare belongs in the answer
    with its restriction named, not removed from it. `rail reachable` is the
    one that prices a particular journey.
    """
    import duckdb

    import json as jsonlib
    from .model.fares import fares_between

    config = load_config()
    if not config.db_path.exists():
        console.print("[red]No database yet - run `rail build`.[/red]")
        raise typer.Exit(1)

    travel_date = dt.date.fromisoformat(date) if date else dt.date.today()
    back_on = dt.date.fromisoformat(return_on) if return_on else None
    if back_on is not None and back_on < travel_date:
        console.print("[red]--return-on is before the travel date.[/red]")
        raise typer.Exit(1)
    connection = duckdb.connect(str(config.db_path), read_only=True)
    fares_dir = snapshot_parquet_dir(config, Feed.FARES)
    rows = fares_between(
        connection, fares_dir,
        origin.upper(), destination.upper(), travel_date,
        ticket_class=1 if first_class else None,
        railcard=railcard.upper() or None,
        include_advance=advance,
        return_on=back_on,
    )
    # How many returns the return date removed, so a shortened list says why.
    withdrawn = 0
    if back_on is not None:
        withdrawn = sum(
            1 for row in fares_between(
                connection, fares_dir, origin.upper(), destination.upper(),
                travel_date, ticket_class=1 if first_class else None,
                railcard=railcard.upper() or None, include_advance=advance)
            if row["return_window"] is not None
            and not row["return_window"].covers(back_on)
        )
    names = dict(connection.execute(
        "select crs, name from station where crs in ($from, $to)",
        {"from": origin.upper(), "to": destination.upper()},
    ).fetchall())
    # An add-on is bought at one end or the other, and may be bought at both -
    # unless the two share a zone, in which case neither.
    add_ons = None
    if plusbus:
        from .model import may_sell_add_on, zone_for

        allowed = may_sell_add_on(
            connection, origin.upper(), destination.upper(), travel_date
        )
        add_ons = {
            "may_sell": allowed,
            "ends": {} if allowed is False else {
                end: zone
                for end in (origin.upper(), destination.upper())
                if (zone := zone_for(connection, end)) is not None
            },
        }
    # Bands governing the journey home, for every restriction code still on
    # offer. Read on the return date, since the feed carries two versions of the
    # restrictions and a return in November reads the next set.
    return_bands: dict[str, list[str]] = {}
    if back_on is not None:
        from .model.restrictions import applicable_bands, describe_restriction

        # Bands in force on the return date specifically. Without this the
        # Mon-Fri peak bands are listed under a Sunday return, and the heading
        # says they apply.
        in_force = {
            (code, out_ret, frm, to, sense, location)
            for code, out_ret, frm, to, sense, location, *_ in
            applicable_bands(connection, back_on)
        }
        for code in sorted({row["restriction_code"] for row in rows
                            if row["restriction_code"]}):
            spelled = describe_restriction(connection, code, back_on, fares_dir)
            sentences = [
                band.as_sentence() for band in spelled["bands"]
                if band.out_ret == "R"
                and (code, band.out_ret, band.time_from, band.time_to,
                     band.sense, band.location) in in_force
                # Same rule as the pricing: a band naming no station is not
                # station specific and bites at whichever end its marker names,
                # while one naming a station only matters if the journey home
                # touches it. Without this a York-Leeds query lists Paddington.
                and (band.location is None
                     or band.location in (origin.upper(), destination.upper()))
                # A band with no dates never applies; it is shown as such by
                # `rail restrictions` and would only be noise here.
                and band.dates
            ]
            if sentences:
                return_bands[code] = sentences
    connection.close()

    if not rows:
        console.print(
            f"[yellow]No fare from {origin.upper()} to {destination.upper()} "
            f"on {travel_date}.[/yellow]"
        )
        raise typer.Exit(1)

    if as_json:
        print(jsonlib.dumps({
            "origin": origin.upper(), "destination": destination.upper(),
            "date": str(travel_date),
            "return_on": str(back_on) if back_on else None,
            # Listed, not applied: the return leg is not routed.
            "return_leg_restrictions": return_bands,
            "railcard": railcard.upper() or None,
            "fares": [
                {**row, "return_window": (
                    None if row["return_window"] is None else {
                        "kind": row["return_window"].kind,
                        "earliest": str(row["return_window"].earliest),
                        "latest": str(row["return_window"].latest),
                        "usable": not row["return_window"].is_empty,
                        "after_weekday": row["return_window"].after_weekday,
                        "break_permitted": row["return_window"].break_permitted,
                        "note": row["return_window"].note,
                    })}
                for row in rows
            ],
            **({"plusbus": add_ons} if add_ons is not None else {}),
        }, indent=2, default=str))
        return

    console.print(
        f"[bold]{names.get(origin.upper(), origin.upper())}[/bold] to "
        f"[bold]{names.get(destination.upper(), destination.upper())}[/bold] "
        f"on {travel_date} - {len(rows)} fares"
        + (f", back on {back_on}" if back_on else "")
        + (f", discounted with {railcard.upper()}" if railcard else "")
    )
    shown = rows[:limit] if limit else rows
    table = Table("fare", "ticket", "class", "type", "route", "when it may be used")
    for row in shown:
        # The three things that decide whether you may use it: the route it is
        # priced on, the times it is barred, and how long it lasts.
        validity = row["restriction_description"] or "no time restriction"
        if row["restriction_code"]:
            validity = f"[yellow]{row['restriction_code']}[/yellow] {validity}"
        if row["validity_description"]:
            validity += f" · {row['validity_description'].strip()}"
        if row["break_out"] is False:
            validity += " · no break of journey"
        window = row["return_window"]
        if window is not None:
            validity += (f" · [cyan]{window.as_sentence()}[/cyan]"
                         if not window.is_empty
                         else f" · [yellow]{window.as_sentence()}[/yellow]")
        price = f"£{row['fare'] / 100:,.2f}"
        if row["undiscounted"] and row["undiscounted"] != row["fare"]:
            price += f" [dim](£{row['undiscounted'] / 100:,.2f})[/dim]"
        table.add_row(
            price, f"{row['ticket_code']} {row['description'].strip()}",
            "1st" if row["tkt_class"] == 1 else "std",
            {"S": "single", "R": "return", "N": "season"}.get(
                row["tkt_type"], row["tkt_type"]),
            f"{row['route_code']} {(row['route_description'] or '').strip()}",
            validity,
        )
    console.print(table)
    _shown(len(shown), len(rows), what="fares")
    if withdrawn:
        console.print(
            f"[yellow]{withdrawn} return fare{'s' if withdrawn > 1 else ''} "
            f"withdrawn[/yellow] - the validity does not permit coming back on "
            f"{back_on}. Two singles may still work."
        )
    # The return leg is not routed, so these are listed rather than applied -
    # which is what this command is for. `rail reachable` filters the outward
    # journey only, and says so.
    if back_on is not None and return_bands:
        console.print(
            f"[bold]On the way back[/bold], {back_on} "
            f"({back_on.strftime('%A')}) - restrictions on the return leg, "
            f"which nothing here checks against a time:"
        )
        for code, sentences in sorted(return_bands.items()):
            console.print(f"  [yellow]{code}[/yellow]")
            for sentence in sentences:
                console.print(f"    {sentence}")
    if add_ons is not None:
        if add_ons["may_sell"] is False:
            console.print(
                "[yellow]No PlusBus add-on for this pair[/yellow] - both ends "
                "sit in the same zone, and the product buys travel around a "
                "place rather than between two."
            )
        elif not add_ons["ends"]:
            console.print("[dim]Neither end has a PlusBus zone.[/dim]")
        else:
            for end, zone in add_ons["ends"].items():
                day = next((f for f in zone["fares"]
                            if f["description"].upper() == "PLUSBUS DAY"), None)
                console.print(
                    f"PlusBus at [bold]{names.get(end, end)}[/bold] "
                    f"({zone['zone_name']}): "
                    + (f"£{day['pence'] / 100:,.2f} a day" if day
                       else "no day ticket")
                    + f" - add to any fare above."
                )
    console.print(
        "[dim]A restriction code names the times the fare may NOT be used - "
        "`rail restrictions <code>` spells one out. The route is where the "
        "ticket is valid, not the journey you would make; `rail reachable "
        "--check-routes --check-guide` tests a fare against a real itinerary."
        "[/dim]"
    )


@app.command()
def routings(
    origin: str = typer.Option(..., "--from", help="Origin CRS code, e.g. YRK."),
    destination: str = typer.Option(..., "--to", help="Destination CRS code."),
    date: str = typer.Option("", "--date", help="Date, for easements in force."),
    limit: int = typer.Option(
        SHOW_EVERYTHING, "--limit", help="Routings to show; 0 or unset for all."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Every routing the National Routeing Guide permits between two stations.

    The inverse of the check `rail reachable --check-guide` runs: rather than
    judging the journey the router found, this lists the routes on offer.
    """
    import duckdb
    import json as jsonlib

    from .model import RouteingGuide

    config = load_config()
    if not config.db_path.exists():
        console.print("[red]No database yet - run `rail build`.[/red]")
        raise typer.Exit(1)

    travel_date = dt.date.fromisoformat(date) if date else dt.date.today()
    connection = duckdb.connect(str(config.db_path), read_only=True)
    guide = RouteingGuide.load(connection)
    found = guide.routings(origin.upper(), destination.upper())
    names = dict(connection.execute("select crs, name from station").fetchall())

    # Easements that name one of these two stations outright. Deliberately not
    # the full `matches` test, which needs a path this command does not have -
    # and deliberately not counting easements that name no origin at all, since
    # those match every journey and would drown the answer.
    ends = {origin.upper(), destination.upper()}
    relevant = [
        e for e in guide.easements
        if e.runs_on(travel_date) and (e.origins & ends or e.destinations & ends)
    ]
    connection.close()

    label = lambda code: names.get(guide.main_station(code), guide.main_station(code))

    if not found:
        console.print(
            f"[yellow]The guide lists no route from {origin.upper()} to "
            f"{destination.upper()}.[/yellow] That is silence, not a refusal - "
            "a pair it does not list is one it has no opinion on."
        )
        raise typer.Exit(1)

    if as_json:
        print(jsonlib.dumps({
            "origin": origin.upper(), "destination": destination.upper(),
            "date": str(travel_date),
            "routeing_points": {
                "origin": guide.points_for(origin.upper()),
                "destination": guide.points_for(destination.upper()),
            },
            "routings": [
                {"maps": list(r.maps), "via_london": r.via_london,
                 "points": r.points,
                 "stations": [guide.main_station(p) for p in r.points]}
                for r in found
            ],
            "easements": [
                {"ref": e.ref, "grants": e.grants,
                 "conditional": e.unsettleable or bool(e.route_codes or e.ticket_codes)}
                for e in relevant
            ],
        }, indent=2))
        return

    console.print(
        f"[bold]{names.get(origin.upper(), origin.upper())}[/bold] to "
        f"[bold]{names.get(destination.upper(), destination.upper())}[/bold]"
        f" - {len(found)} permitted routings"
        f" [dim](routeing points {', '.join(guide.points_for(origin.upper()))} → "
        f"{', '.join(guide.points_for(destination.upper()))})[/dim]"
    )
    shown = found[:limit] if limit else found
    table = Table("maps", "via")
    for routing in shown:
        if routing.via_london:
            way = ("[yellow]London[/yellow] - validated as two halves with a "
                   "transfer between, so no single path applies")
        elif routing.walkable:
            way = " · ".join(label(p) for p in routing.points)
        else:
            way = "[dim]the chain does not join these points[/dim]"
        table.add_row(" → ".join(routing.maps), way)
    console.print(table)
    _shown(len(shown), len(found), what="routings")
    console.print(
        "[dim]Each row is a chain of the guide's maps; the stations are the "
        "shortest walk across it, not the only one. A train may pass through a "
        "routeing point without calling there.[/dim]"
    )
    if relevant:
        grants = sum(1 for e in relevant if e.grants)
        console.print(
            f"[dim]{len(relevant)} easements name one of these stations and are "
            f"in force on {travel_date}: {grants} grant a route the maps refuse, "
            f"{len(relevant) - grants} withdraw one they allow. Whether any "
            f"applies to a given journey depends on the path and the ticket - "
            f"`rail reachable --check-guide` settles that.[/dim]"
        )


@app.command()
def stopover(
    origin: str = typer.Option(..., "--from", help="Origin CRS code, e.g. YRK."),
    destination: str = typer.Option(..., "--to", help="Destination CRS code."),
    via: str = typer.Option(..., "--via", help="Where to break the journey."),
    date: str = typer.Option(..., "--date", help="Travel date, YYYY-MM-DD."),
    depart: str = typer.Option("09:00", "--depart", help="Earliest departure, HH:MM."),
    dwell: int = typer.Option(60, "--dwell", help="Minutes to spend at the stop."),
    railcard: str = typer.Option("", "--railcard", help="Railcard code, e.g. YNG."),
    first_class: bool = typer.Option(False, "--first-class"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Break a journey deliberately: A to B with a stop at somewhere on the way.

    Priced as **one ticket** from A to B, which is the whole point - and only
    fares whose validity permits a break of journey are offered. Both halves are
    routed, and the guide is asked about the journey as a whole.
    """
    import duckdb
    import json as jsonlib

    from .engine import UNREACHABLE as UNREACHABLE_SENTINEL, earliest_arrival
    from .engine.network import load_network
    from .model import RouteingGuide

    config = load_config()
    if not config.db_path.exists():
        console.print("[red]No database yet - run `rail build`.[/red]")
        raise typer.Exit(1)

    travel_date = dt.date.fromisoformat(date)
    depart_minutes = _hhmm(depart)
    connection = duckdb.connect(str(config.db_path), read_only=True)
    timetable_dir = snapshot_parquet_dir(config, Feed.TIMETABLE)
    network = load_network(connection, travel_date, timetable_dir=timetable_dir)

    stop = via.upper()
    first = earliest_arrival(network, origin.upper(), depart_minutes)
    if first.arrival[network.index.get(stop, -1)] >= UNREACHABLE_SENTINEL:
        console.print(f"[yellow]{stop} is not reachable from {origin.upper()}.[/yellow]")
        raise typer.Exit(1)

    reached_stop = first.arrival[network.index[stop]]
    # The dwell is the point of the exercise, so it is charged before looking
    # for anything onward.
    second = earliest_arrival(network, stop, reached_stop + dwell)
    end = network.index.get(destination.upper(), -1)
    if end < 0 or second.arrival[end] >= UNREACHABLE_SENTINEL:
        console.print(
            f"[yellow]{destination.upper()} is not reachable from {stop} after "
            f"{dwell} minutes there.[/yellow]"
        )
        raise typer.Exit(1)
    arrive = second.arrival[end]

    # One journey, so the guide and the route conditions see the whole path.
    whole_path = first.path_to(stop) + second.path_to(destination.upper())[1:]
    whole_operators = (first.operators_to(stop)
                       | second.operators_to(destination.upper()))
    whole_modes = first.modes_to(stop) | second.modes_to(destination.upper())

    fares = cheapest_from(
        connection, snapshot_parquet_dir(config, Feed.FARES),
        origin.upper(), travel_date,
        ticket_class=1 if first_class else 2,
        railcard=railcard.upper() or None,
        paths={destination.upper(): whole_path},
        operators={destination.upper(): whole_operators},
        modes={destination.upper(): whole_modes},
        # Both halves, and the deliberate break between them is itself a change.
        changes={destination.upper(): first.changes_to(stop)
                 + second.changes_to(destination.upper()) + 1},
        boardings={destination.upper(): [
            (leg.board, leg.operator or "")
            for leg in ((first.legs_to(stop) or [])
                        + (second.legs_to(destination.upper()) or []))]},
        break_of_journey=True,
    )
    fare = next((r for r in fares if r[0] == destination.upper()), None)

    guide = RouteingGuide.load(connection)
    verdict = guide.permits(
        origin.upper(), destination.upper(), whole_path, date=travel_date,
        route_code=fare[5] if fare else None,
        ticket_code=fare[1] if fare else None,
        # The deliberate break is itself a change, so this is never a through
        # train however direct the two halves are.
        changes=first.changes_to(stop) + second.changes_to(destination.upper()) + 1,
        # Both halves, since an easement tied to an operator speaks to the
        # whole journey and the break does not divide it into two questions.
        operators=set(first.operators_to(stop))
                  | set(second.operators_to(destination.upper())),
    )
    names = dict(connection.execute("select crs, name from station").fetchall())
    connection.close()

    payload = {
        "origin": origin.upper(), "destination": destination.upper(), "via": stop,
        "date": str(travel_date), "dwell_minutes": dwell,
        "depart": _fmt(depart_minutes),
        "arrive_at_stop": _fmt(reached_stop),
        "leave_stop": _fmt(reached_stop + dwell),
        "arrive": _fmt(arrive),
        "path": whole_path,
        "operators": sorted(whole_operators),
        "fare": None if fare is None else {
            "ticket": fare[1], "description": fare[2], "pence": fare[3],
            "route_code": fare[5],
        },
        "guide": {True: "permitted", False: "refused", None: "no opinion"}[verdict],
    }
    if as_json:
        print(jsonlib.dumps(payload, indent=2))
        return

    console.print(
        f"[bold]{names.get(origin.upper(), origin.upper())}[/bold] to "
        f"[bold]{names.get(destination.upper(), destination.upper())}[/bold], "
        f"breaking at [bold]{names.get(stop, stop)}[/bold] for {dwell} minutes "
        f"on {travel_date}"
    )
    table = Table("leg", "depart", "arrive", "calls")
    table.add_row("to the stop", _fmt(depart_minutes), _fmt(reached_stop),
                  " · ".join(first.path_to(stop)))
    table.add_row("onward", _fmt(reached_stop + dwell), _fmt(arrive),
                  " · ".join(second.path_to(destination.upper())))
    console.print(table)

    if fare is None:
        console.print(
            "[yellow]No fare covers this.[/yellow] Either nothing is priced "
            "between these stations, or every fare that is bars a break of "
            "journey - TVL field 12 - or fails the route on this path."
        )
    else:
        console.print(
            f"One ticket, [green]£{fare[3] / 100:,.2f}[/green] {fare[2]} "
            f"(route {fare[5]}) - a fare whose validity permits a break of "
            f"journey. The routeing guide says [bold]{payload['guide']}[/bold]."
        )
    console.print(
        "[dim]The stop is charged before looking onward, so `--dwell` is time "
        "you actually get there. Priced as one ticket end to end: two singles "
        "may well be cheaper, and this does not look for them.[/dim]"
    )


@app.command()
def plusbus(
    station: str = typer.Argument(..., help="CRS code, e.g. YRK."),
    other: str = typer.Option(
        "", "--with", help="The other end of the journey, to check the pair."
    ),
    date: str = typer.Option("", "--date", help="Travel date, YYYY-MM-DD."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Bus travel around a station, sold as an add-on to a rail ticket.

    With `--with`, also says whether an add-on may be sold for that journey: it
    may not when both ends sit in the same zone, since the product buys travel
    *around* a place rather than between two.
    """
    import duckdb
    import json as jsonlib

    from .model import may_sell_add_on, zone_for

    config = load_config()
    if not config.db_path.exists():
        console.print("[red]No database yet - run `rail build`.[/red]")
        raise typer.Exit(1)

    travel_date = dt.date.fromisoformat(date) if date else dt.date.today()
    connection = duckdb.connect(str(config.db_path), read_only=True)
    zone = zone_for(connection, station.upper())
    pair = None
    if other:
        pair = {
            "with": other.upper(),
            "zone": zone_for(connection, other.upper()) is not None,
            "may_sell": may_sell_add_on(
                connection, station.upper(), other.upper(), travel_date
            ),
        }
    names = dict(connection.execute(
        "select crs, name from station where crs in ($a, $b)",
        {"a": station.upper(), "b": other.upper() or station.upper()},
    ).fetchall())
    connection.close()

    if zone is None:
        console.print(
            f"[yellow]No PlusBus zone at {station.upper()}.[/yellow] "
            "312 stations have one."
        )
        raise typer.Exit(1)

    if as_json:
        print(jsonlib.dumps({"date": str(travel_date), **zone, "pair": pair}, indent=2))
        return

    console.print(
        f"[bold]{names.get(zone['crs'], zone['crs'])}[/bold] - "
        f"{zone['zone_name']} (zone {zone['zone_nlc']})"
    )
    table = Table("ticket", "price", "code")
    for entry in zone["fares"]:
        table.add_row(entry["description"], f"£{entry['pence'] / 100:,.2f}",
                      entry["ticket_code"])
    console.print(table)
    if zone["url"]:
        console.print(f"[dim]Zone map and operators: {zone['url']}[/dim]")

    if pair is not None:
        name = names.get(pair["with"], pair["with"])
        if pair["may_sell"] is None:
            console.print(f"Neither end has a zone, so there is nothing to add on.")
        elif pair["may_sell"]:
            console.print(
                f"[green]An add-on may be sold[/green] for a journey with {name}."
            )
        else:
            console.print(
                f"[red]No add-on for {station.upper()} to {name}[/red] - both "
                "sit in the same PlusBus zone, and the product buys travel "
                "around a place rather than between two."
            )
    console.print(
        "[dim]PlusBus prices come from the fares feed; which pairs are excluded "
        "comes from RSPS5052, whose licensing is separate - see "
        "docs/DATA-SOURCES.md.[/dim]"
    )


@app.command()
def roundtrip(
    origin: str = typer.Option(..., "--from", help="Origin CRS code, e.g. YRK."),
    destination: str = typer.Option(..., "--to", help="Destination CRS code."),
    date: str = typer.Option(..., "--date", help="Outward date, YYYY-MM-DD."),
    return_on: str = typer.Option(..., "--return-on", help="Return date, YYYY-MM-DD."),
    depart: str = typer.Option("09:00", "--depart", help="Earliest outward departure."),
    return_depart: str = typer.Option(
        "17:00", "--return-depart", help="Earliest departure on the way back."
    ),
    via: str = typer.Option(
        "", "--via", help="Break the outward journey here (needs break_out)."
    ),
    return_via: str = typer.Option(
        "", "--return-via",
        help="Break the journey home here (needs break_in, which nothing else "
             "can enforce).",
    ),
    dwell: int = typer.Option(60, "--dwell", help="Minutes to spend at a break."),
    railcard: str = typer.Option("", "--railcard", help="Railcard code, e.g. YNG."),
    advance: bool = typer.Option(False, "--advance", help="Include Advance prices."),
    check_routes: bool = typer.Option(
        False, "--check-routes",
        help="Only quote fares valid on the journeys actually found.",
    ),
    first_class: bool = typer.Option(False, "--first-class"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Going and coming back, priced as one return and as two singles.

    Everything else here prices one direction. This routes both legs - which is
    what makes the 13,803 return-leg restriction bands evaluable at all - and
    names the cheaper of the two ways to buy it. Neither is reliably cheaper.
    """
    import duckdb
    import json as jsonlib

    from .engine import UNREACHABLE as UNREACHABLE_SENTINEL, earliest_arrival
    from .engine.network import load_network
    from .model.roundtrip import Leg, price_round_trip

    config = load_config()
    if not config.db_path.exists():
        console.print("[red]No database yet - run `rail build`.[/red]")
        raise typer.Exit(1)

    a, b = origin.upper(), destination.upper()
    out_date = dt.date.fromisoformat(date)
    back_date = dt.date.fromisoformat(return_on)
    if back_date < out_date:
        console.print("[red]--return-on is before the outward date.[/red]")
        raise typer.Exit(1)

    connection = duckdb.connect(str(config.db_path), read_only=True)
    timetable_dir = snapshot_parquet_dir(config, Feed.TIMETABLE)

    def route(frm: str, to: str, on: dt.date, after: int,
              stop: str = "") -> Leg | None:
        """One leg, optionally broken at `stop`.

        A break is routed as two halves with the dwell charged between them, so
        `--dwell` is time actually spent there rather than whatever the next
        connection happened to allow. The halves are then treated as one leg:
        one ticket, so the guide and the route conditions see the whole path.
        """
        network = load_network(connection, on, timetable_dir=timetable_dir)
        first = earliest_arrival(network, frm, after)
        if not stop:
            index = network.index.get(to, -1)
            if index < 0 or first.arrival[index] >= UNREACHABLE_SENTINEL:
                return None
            return Leg(origin=frm, destination=to, date=on, depart=after,
                       arrive=first.arrival[index], path=first.path_to(to),
                       operators=first.operators_to(to), modes=first.modes_to(to),
                       changes=first.changes_to(to), calls=first.calls_to(to),
                       boardings=[(leg.board, leg.operator or "", leg.alight)
                                  for leg in (first.legs_to(to) or [])])

        middle = network.index.get(stop, -1)
        if middle < 0 or first.arrival[middle] >= UNREACHABLE_SENTINEL:
            return None
        second = earliest_arrival(network, stop, first.arrival[middle] + dwell)
        end = network.index.get(to, -1)
        if end < 0 or second.arrival[end] >= UNREACHABLE_SENTINEL:
            return None
        # The break station ends one half and begins the other, so it appears
        # in both lists - joined the same way `calls_to` joins a change of
        # trains, keeping the arrival from the first half and the departure
        # from the second. It *is* a change, however direct the halves are, so
        # a band naming it bites here exactly as it would anywhere else.
        halves, onward = first.calls_to(stop), second.calls_to(to)
        if halves and onward and halves[-1][0] == onward[0][0]:
            calls = (halves[:-1]
                     + [(halves[-1][0], halves[-1][1], onward[0][2], True)]
                     + onward[1:])
        else:
            calls = halves + onward

        return Leg(
            origin=frm, destination=to, date=on, depart=after,
            arrive=second.arrival[end],
            path=first.path_to(stop) + second.path_to(to)[1:],
            operators=first.operators_to(stop) | second.operators_to(to),
            modes=first.modes_to(stop) | second.modes_to(to),
            boardings=[(leg.board, leg.operator or "", leg.alight)
                       for leg in ((first.legs_to(stop) or [])
                                   + (second.legs_to(to) or []))],
            # The deliberate break is itself a change, however direct the halves.
            changes=first.changes_to(stop) + second.changes_to(to) + 1,
            calls=calls,
        )

    out_leg = route(a, b, out_date, _hhmm(depart), via.upper())
    if out_leg is None:
        console.print(f"[yellow]{b} is not reachable from {a} on {out_date}.[/yellow]")
        raise typer.Exit(1)
    back_leg = route(b, a, back_date, _hhmm(return_depart), return_via.upper())
    if back_leg is None:
        console.print(
            f"[yellow]{a} is not reachable from {b} on {back_date} "
            f"after {return_depart}.[/yellow]"
        )
        raise typer.Exit(1)

    trip = price_round_trip(
        connection, snapshot_parquet_dir(config, Feed.FARES), out_leg, back_leg,
        ticket_class=1 if first_class else 2,
        railcard=railcard.upper() or None,
        include_advance=advance,
        check_routes=check_routes,
        break_out=bool(via),
        break_in=bool(return_via),
    )
    names = dict(connection.execute("select crs, name from station").fetchall())
    connection.close()

    def quote_json(quote):
        return None if quote is None else {
            "kind": quote.kind, "pence": quote.pence,
            "tickets": [{"ticket": c, "description": d.strip(), "pence": p,
                         "route_code": r} for c, d, p, r in quote.tickets],
        }

    if as_json:
        print(jsonlib.dumps({
            "origin": a, "destination": b,
            "date": str(out_date), "return_on": str(back_date),
            "legs": [
                {"from": leg.origin, "to": leg.destination, "date": str(leg.date),
                 "depart": _fmt(leg.depart), "arrive": _fmt(leg.arrive),
                 "minutes": leg.minutes, "changes": leg.changes, "path": leg.path,
                 "operators": sorted(leg.operators)}
                for leg in (out_leg, back_leg)
            ],
            "break_at": {"outward": via.upper() or None,
                         "homeward": return_via.upper() or None},
            "return_ticket": quote_json(trip.single_ticket),
            "two_singles": quote_json(trip.two_singles),
            "cheapest": None if trip.best is None else trip.best.kind,
            "saving_pence": trip.saving,
        }, indent=2))
        return

    console.print(
        f"[bold]{names.get(a, a)}[/bold] to [bold]{names.get(b, b)}[/bold] "
        f"on {out_date} ({out_date:%a}), back {back_date} ({back_date:%a})"
        + (f", discounted with {railcard.upper()}" if railcard else "")
    )
    table = Table("leg", "date", "depart", "arrive", "changes", "calls")
    for label, leg, stop in (("out", out_leg, via.upper()),
                             ("back", back_leg, return_via.upper())):
        table.add_row(
            label + (f" via {stop}" if stop else ""),
            f"{leg.date:%a %-d %b}", _fmt(leg.depart), _fmt(leg.arrive),
            str(leg.changes), " · ".join(leg.path))
    console.print(table)
    if via or return_via:
        console.print(
            "[dim]A break of journey needs a ticket that permits one, and the "
            "two directions are separate permissions - TVL field 12 outward, 13 "
            "on the way home. 651 of the 1,379 walk-up ticket types bar a break "
            "outward and 32 of the 444 returns bar one homeward; a validity the "
            "feed says nothing about is not assumed permissive either way.[/dim]"
        )

    if trip.best is None:
        console.print(
            "[yellow]Nothing priced.[/yellow] No return is valid for these "
            "dates and no single covers each leg - try `rail fares` for the "
            "pair to see what exists and what governs it."
        )
        raise typer.Exit(1)

    for quote in (trip.single_ticket, trip.two_singles):
        if quote is None:
            missing = ("no return fare is valid for these dates and times"
                       if quote is trip.single_ticket
                       else "no single covers both legs")
            console.print(f"  [dim]{missing}[/dim]")
            continue
        mark = "[green]→[/green]" if quote is trip.best else " "
        console.print(f"{mark} {quote.as_sentence()}")

    if trip.saving:
        console.print(
            f"[green]{trip.best.kind.capitalize()} is cheaper by "
            f"£{trip.saving / 100:,.2f}.[/green]"
        )
    elif trip.saving == 0:
        console.print("[dim]The two cost the same.[/dim]")
    console.print(
        "[dim]Both legs are routed, so the fares are checked against the "
        "restrictions on each - including the return-leg bands, which nothing "
        "else here evaluates. This prices the journeys found, not the cheapest "
        "pair of departure times.[/dim]"
    )


@app.command()
def geography(
    path: str = typer.Argument(..., help="TIPLOC eastings/northings, .xlsx or .xlsx.gz."),
) -> None:
    """Import precise TIPLOC grid references, then rebuild to apply them.

    A third source under a third licence: a Network Rail **FOI disclosure**
    under the **Open Government Licence v3**, not the DTD terms and not
    RSPS5052's. Acknowledging Network Rail and naming the OGL is a condition of
    publishing anything derived from it, alongside the National Rail attribution
    the feeds already require.

    An FOI release is a snapshot with no refresh, so it goes stale silently as
    stations open and close - and `rail refresh` rebuilds the database, dropping
    the refinement, so this has to be re-run afterwards.

    MSN's own grid references are about a kilometre accurate, which is fine for
    labelling a station and useless for measuring a distance. This sharpens them
    - but only where the two sources agree, because neither is authoritative.
    """
    from pathlib import Path

    from .acquire.geography import ingest_geography

    config = load_config()
    source = Path(path).expanduser()
    if not source.exists():
        console.print(f"[red]No such file: {source}[/red]")
        raise typer.Exit(1)

    counts = ingest_geography(source, config.parquet_dir)
    console.print(
        f"[green]{counts.tiplocs:,}[/green] TIPLOC grid references from "
        f"[bold]{counts.source}[/bold]\n"
        f"[dim]sha256 {counts.sha256[:16]}…[/dim]"
    )
    console.print(
        "[yellow]Network Rail FOI disclosure, Open Government Licence v3.[/yellow]"
        "\nNot a DTD feed: acknowledge Network Rail and name the OGL in anything "
        "published from it, as well as National Rail for the feed data."
        "\n[dim]A snapshot, not a feed - it has no refresh and goes stale as "
        "stations change.[/dim]"
    )
    console.print(
        "Run [bold]rail build[/bold] to apply them - and again after any "
        "[bold]rail refresh[/bold], which rebuilds without them."
    )


@app.command()
def distance(
    origin: str = typer.Option(..., "--from", help="Origin CRS code, e.g. YRK."),
    destination: str = typer.Option("", "--to", help="Destination CRS; omit to sweep."),
    date: str = typer.Option("", "--date", help="Travel date, for the journey found."),
    depart: str = typer.Option("09:00", "--depart", help="Departure time, HH:MM."),
    least_direct: bool = typer.Option(
        False, "--least-direct",
        help="Sweeping: rank by how far the rail route exceeds the straight line.",
    ),
    limit: int = typer.Option(
        SHOW_EVERYTHING, "--limit", help="Rows to show; 0 or unset for all."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """How far it is by rail, and as the crow flies.

    Rail miles come from the routeing guide's own station-link file, which is
    what its shortest-route rules are written against - York to King's Cross is
    188.50 miles, the ECML's published figure. Straight-line distance comes from
    grid references and is **not** a routeing rule; the guide never mentions it.

    With `--date` the journey the router actually finds is measured too, and
    compared against the shortest route and the guide's 3-mile margin.
    """
    import duckdb
    import json as jsonlib

    from .model.distance import SHORTEST_ROUTE_MARGIN_MILES, Distances

    config = load_config()
    if not config.db_path.exists():
        console.print("[red]No database yet - run `rail build`.[/red]")
        raise typer.Exit(1)

    connection = duckdb.connect(str(config.db_path), read_only=True)
    distances = Distances.load(connection)
    if not distances:
        console.print(
            "[red]No station links loaded.[/red] The routeing feed's RGD file "
            "carries them - run `rail build` against a routeing snapshot."
        )
        raise typer.Exit(1)
    names = dict(connection.execute("select crs, name from station").fetchall())
    a = origin.upper()

    # --- one pair ------------------------------------------------------------
    if destination:
        b = destination.upper()
        rail = distances.shortest_miles(a, b)
        straight = distances.crow_flies_miles(a, b)
        journey = None
        if date and rail is not None:
            from .engine import UNREACHABLE as GONE, earliest_arrival
            from .engine.network import load_network

            network = load_network(connection, dt.date.fromisoformat(date),
                                   timetable_dir=snapshot_parquet_dir(
                                       config, Feed.TIMETABLE))
            found = earliest_arrival(network, a, _hhmm(depart))
            index = network.index.get(b, -1)
            if index >= 0 and found.arrival[index] < GONE:
                path = found.path_to(b)
                journey = {
                    "miles": distances.journey_miles(path),
                    "changes": found.changes_to(b),
                    "path": path,
                }
        connection.close()

        if rail is None:
            console.print(
                f"[yellow]No rail path from {a} to {b}.[/yellow] The station-link "
                "file covers rail only - bus, ferry and the Elizabeth Line "
                "stations carry no links (RSPS5047 6.1.6.2)."
            )
            raise typer.Exit(1)

        payload = {
            "origin": a, "destination": b,
            "rail_miles": round(rail, 2),
            "crow_flies_miles": None if straight is None else round(straight, 2),
            "directness": None if not straight else round(rail / straight, 3),
            "journey": None if journey is None else {
                **journey,
                "miles": None if journey["miles"] is None else round(journey["miles"], 2),
                "excess_miles": None if journey["miles"] is None
                                else round(journey["miles"] - rail, 2),
                "within_guide_margin": distances.within_shortest_margin(a, b,
                                                                       journey["path"]),
            },
        }
        if as_json:
            print(jsonlib.dumps(payload, indent=2))
            return

        console.print(
            f"[bold]{names.get(a, a)}[/bold] to [bold]{names.get(b, b)}[/bold]")
        table = Table("measure", "miles", "note")
        table.add_row("shortest by rail", f"{rail:,.2f}",
                      "RGD station links - what the guide's rules use")
        if straight is not None:
            table.add_row("straight line", f"{straight:,.2f}",
                          f"×{rail / straight:.2f} - not a routeing rule")
        if journey and journey["miles"] is not None:
            excess = journey["miles"] - rail
            table.add_row(
                "the journey found", f"{journey['miles']:,.2f}",
                f"{excess:+,.2f} vs shortest, {journey['changes']} changes")
        console.print(table)
        if journey and journey["miles"] is not None:
            verdict = distances.within_shortest_margin(a, b, journey["path"])
            console.print(
                f"[green]Permitted outright[/green] - within the guide's "
                f"{SHORTEST_ROUTE_MARGIN_MILES:g}-mile margin of the shortest "
                f"route (RSPS5047 7.1.2)."
                if verdict else
                "[dim]Outside the shortest-route margin, so the guide falls "
                "through to its maps - see `rail reachable --check-guide`.[/dim]"
            )
        return

    # --- one to all ----------------------------------------------------------
    reachable = distances.shortest_from(a)
    rows = []
    for crs, rail in reachable.items():
        if crs == a:
            continue
        straight = distances.crow_flies_miles(a, crs)
        rows.append((crs, names.get(crs, crs), rail, straight,
                     None if not straight else rail / straight))
    connection.close()

    rows.sort(key=lambda r: (-(r[4] or 0), -r[2]) if least_direct else (r[2],))
    if as_json:
        print(jsonlib.dumps({
            "origin": a,
            "reachable": len(rows),
            "stations": [
                {"crs": crs, "name": name, "rail_miles": round(rail, 2),
                 "crow_flies_miles": None if straight is None else round(straight, 2),
                 "directness": None if d is None else round(d, 3)}
                # Not sliced by `--limit`: that flag is about how much fits on
                # a screen, and a machine-readable answer that silently stops at
                # row 20 is a trap rather than a convenience.
                for crs, name, rail, straight, d in rows
            ],
        }, indent=2))
        return

    console.print(
        f"[bold]{names.get(a, a)}[/bold] - {len(rows):,} stations reachable by "
        f"rail" + (", least direct first" if least_direct else ", nearest first"))
    table = Table("crs", "station", "rail miles", "straight", "×")
    shown = rows[:limit] if limit else rows
    for crs, name, rail, straight, d in shown:
        table.add_row(crs, name[:30], f"{rail:,.2f}",
                      "-" if straight is None else f"{straight:,.2f}",
                      "-" if d is None else f"{d:.2f}")
    console.print(table)
    _shown(len(shown), len(rows), what="stations")
    console.print(
        "[dim]Rail miles are RGD station links, which is what the routeing "
        "guide's shortest-route rules use. Straight-line distance is from grid "
        "references and decides nothing - the guide never mentions it.[/dim]"
    )


@app.command()
def naptan() -> None:
    """Fetch NaPTAN's rail stops, then rebuild to resolve station positions.

    The Department for Transport's gazetteer of public transport access nodes,
    under the **Open Government Licence v3** - no account and no key, and unlike
    the FOI grid file it is maintained, so it can be refetched.

    It joins on the ATCO code: rail stations sit in the `9100` namespace and the
    rest of the code is the TIPLOC. What it settles is the disagreement between
    MSN and the FOI spreadsheet, where nothing else could tell which was right.
    """
    from .acquire.naptan import fetch_naptan

    config = load_config()
    console.print("[dim]Downloading the national CSV (~100 MB); the rail "
                  "namespace is about 2,700 rows of it.[/dim]")
    result = fetch_naptan(config.parquet_dir)
    console.print(
        f"[green]{result.rows:,}[/green] rail stops "
        f"({result.active:,} active) from {result.downloaded_bytes / 1e6:,.0f} MB\n"
        f"[dim]sha256 {result.sha256[:16]}… of the whole download[/dim]"
    )
    console.print(
        "[yellow]Department for Transport, Open Government Licence v3.[/yellow]"
        "\nNot a DTD feed: acknowledge DfT and name the OGL in anything "
        "published from it, as well as National Rail for the feed data."
    )
    console.print("Run [bold]rail build[/bold] to apply it.")
