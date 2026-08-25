"""The unattended refresh: fetch, ingest what changed, rebuild, validate.

Two things make this more than a convenience wrapper.

**The account expires.** NRDP deletes accounts after roughly 30 days without
feed consumption, and a deleted account has to be re-registered by hand. A
fortnightly schedule leaves comfortable margin, but only while it is actually
running - two consecutive silent failures put the gap at a month. So every run
records its outcome, and ``rail status`` reports how long it has been since the
last successful fetch and starts warning well before the deadline.

**Rebuilding is only worth it when something changed.** Fares refresh about
three times a year and the timetable rarely, so most runs download nothing. The
fetch result decides whether to re-ingest, and the ingest decides whether to
rebuild.

**A run records what triggered it, and that is not a detail.** The status file
used to say a run had succeeded and not what ran it, so a person typing
``rail refresh`` and a launchd job were the same event in the only file anyone
checks. That is not hypothetical: the agent was unloaded from 29 July to
24 August 2026 - the 1 and 15 August runs never happened - and nothing noticed,
because a hand-fetch on 17 August had reset the clock to a healthy seven days.
The outcome was fine and the schedule was dead, and no single number could say
both. So ``last_scheduled_success`` is recorded beside ``last_success``, and a
fortnightly job with no *scheduled* run in three weeks is broken whoever has
been fetching in the meantime.

**Every run appends to a ledger, whoever ran it.** ``refresh.log`` is the
launchd job's ``StandardOutPath``, so it captures scheduled runs and nothing
else - which is exactly how the two clocks came to disagree by nineteen days
with neither being wrong. ``refresh-history.jsonl`` is written by this module
instead, so it holds every run. It is deliberately a *different file*: a second
writer to ``refresh.log`` would double every scheduled line, which is a bug
this project has already shipped once.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .acquire import Feed, NRDPSource, PollTooSoon, SnapshotStore
from .config import Config

STATUS_FILE = "refresh-status.json"

#: Append-only, one JSON object per run. Every run, whoever started it.
HISTORY_FILE = "refresh-history.jsonl"

#: The plist sets this to "scheduled". Anything else - a person at a terminal,
#: a deploy script, a test - is "manual".
#:
#: Deliberately explicit rather than sniffed. The tempting signal is launchd's
#: own ``XPC_SERVICE_NAME``, which is unset or "0" under a plain shell and a
#: real service name under a launchd job - and it is *also* a real service name
#: under anything else launchd started, including the terminal you are reading
#: this in. So it answers "was some ancestor launchd-managed", which is not the
#: question. The label cannot be hardcoded either: it is the installer's to
#: choose, and this file ships in a public repository.
TRIGGER_ENV = "RAIL_REFRESH_TRIGGER"
SCHEDULED = "scheduled"
MANUAL = "manual"

#: NRDP deletes accounts after about 30 days of no consumption. Warn from here.
ACCOUNT_WARNING_DAYS = 21
ACCOUNT_EXPIRY_DAYS = 30


def current_trigger() -> str:
    """What started this run.

    Unset means manual, which is the safe default in both directions: a
    scheduled run wrongly called manual makes the schedule *look* stale and
    prompts somebody to check, while the reverse hides a dead agent behind
    somebody else's typing - the failure this exists to catch.
    """
    return SCHEDULED if os.environ.get(TRIGGER_ENV) == SCHEDULED else MANUAL


@dataclass
class RefreshResult:
    started_at: str
    finished_at: str | None = None
    ok: bool = False
    #: "scheduled" or "manual" - see `current_trigger`.
    trigger: str = MANUAL
    #: feed -> what happened.
    fetched: dict[str, str] = field(default_factory=dict)
    ingested: list[str] = field(default_factory=list)
    rebuilt: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.ingested)


def status_path(config: Config) -> Path:
    return config.data_dir / STATUS_FILE


def history_path(config: Config) -> Path:
    return config.log_dir / HISTORY_FILE


def read_status(config: Config) -> dict | None:
    path = status_path(config)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def days_since_last_success(config: Config, *, scheduled_only: bool = False) -> float | None:
    """How long since a fetch actually reached the portal.

    `scheduled_only` asks the narrower question - how long since the *schedule*
    last worked - which is the one a hand-fetch cannot answer for you. Both
    matter and they mean different things: the first is whether the account is
    safe, the second whether anything will keep it that way.
    """
    status = read_status(config) or {}
    stamp = status.get("last_scheduled_success" if scheduled_only else "last_success")
    if not stamp:
        return None
    last = datetime.fromisoformat(stamp)
    return (datetime.now(timezone.utc) - last).total_seconds() / 86400


def _record(config: Config, result: RefreshResult) -> None:
    previous = read_status(config) or {}
    scheduled = result.ok and result.trigger == SCHEDULED
    payload = {
        "last_run": result.started_at,
        # A poll that reached the portal counts as consumption even when nothing
        # was downloaded - that is what keeps the account alive.
        "last_success": (
            result.finished_at if result.ok else previous.get("last_success")
        ),
        # The same clock, restricted to runs the schedule started. A manual
        # fetch renews the account and says nothing about whether the agent is
        # still loaded, so it must not touch this one.
        "last_scheduled_success": (
            result.finished_at if scheduled
            else previous.get("last_scheduled_success")
        ),
        "last_result": asdict(result),
    }
    status_path(config).write_text(json.dumps(payload, indent=2) + "\n")
    _append_history(config, result)


def _append_history(config: Config, result: RefreshResult) -> None:
    """One line per run, appended, never rewritten.

    Best-effort by design. A ledger that can abort a refresh by failing to
    write is worse than no ledger - the same rule `notify()` follows over in
    the visualisation repo, and for the same reason: the thing being recorded
    matters more than the record of it.
    """
    try:
        config.log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "at": result.finished_at or result.started_at,
            "trigger": result.trigger,
            "ok": result.ok,
            "fetched": result.fetched,
            "ingested": result.ingested,
            "rebuilt": result.rebuilt,
            "errors": result.errors,
        }, separators=(",", ":"))
        with history_path(config).open("a") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def refresh(
    config: Config,
    *,
    force: bool = False,
    rebuild_anyway: bool = False,
    horizon_days: int = 90,
    log=print,
    source=None,
    trigger: str | None = None,
) -> RefreshResult:
    """Run the whole pipeline. Safe to call on a schedule.

    `source` exists so tests can supply a fake feed source; leave it unset and
    the real portal client is used. `trigger` likewise overrides what the
    environment says started this run.
    """
    import duckdb

    from .model import build_all
    from .parse import ingest_snapshot

    result = RefreshResult(
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        trigger=trigger or current_trigger(),
    )
    store = SnapshotStore(config.raw_dir)
    if source is None:
        username, password = config.require_credentials()
        source = NRDPSource(store, username, password, state_dir=config.data_dir)

    reached_portal = False
    for feed in Feed:
        try:
            fetched = source.fetch(feed, force=force)
            reached_portal = True
        except PollTooSoon as exc:
            result.fetched[feed.value] = f"skipped: {exc}"
            log(f"{feed.value}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the rest
            result.errors.append(f"{feed.value} fetch: {exc}")
            log(f"{feed.value}: FAILED - {exc}")
            continue

        result.fetched[feed.value] = fetched.reason
        log(f"{feed.value}: {fetched.filename} - {fetched.reason}")
        if not fetched.downloaded:
            continue

        try:
            manifest = store.latest(feed)
            report = ingest_snapshot(
                store.path_for(manifest), manifest, config.parquet_dir
            )
            result.ingested.append(feed.value)
            log(f"{feed.value}: ingested {report.total_rows:,} rows")
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{feed.value} ingest: {exc}")
            log(f"{feed.value}: ingest FAILED - {exc}")

    if result.ingested or rebuild_anyway:
        try:
            connection = duckdb.connect(str(config.db_path))
            # The same sequence `rail build` runs. Listing the stages here
            # instead is what let an unattended refresh write a database that
            # was quietly missing half of them.
            build_all(connection, config, horizon_days=horizon_days)
            connection.close()
            result.rebuilt = True
            log("rebuilt the database")
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"build: {exc}")
            log(f"build FAILED - {exc}")
    else:
        log("nothing changed; database left alone")

    # Reaching the portal is what matters for the account, so a run that found
    # no new data is still a success.
    result.ok = reached_portal and not result.errors
    result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _record(config, result)
    return result
