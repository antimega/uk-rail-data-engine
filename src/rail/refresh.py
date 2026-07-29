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
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .acquire import Feed, NRDPSource, PollTooSoon, SnapshotStore
from .config import Config

STATUS_FILE = "refresh-status.json"

#: NRDP deletes accounts after about 30 days of no consumption. Warn from here.
ACCOUNT_WARNING_DAYS = 21
ACCOUNT_EXPIRY_DAYS = 30


@dataclass
class RefreshResult:
    started_at: str
    finished_at: str | None = None
    ok: bool = False
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


def read_status(config: Config) -> dict | None:
    path = status_path(config)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def days_since_last_success(config: Config) -> float | None:
    """How long since a fetch actually reached the portal."""
    status = read_status(config)
    stamp = (status or {}).get("last_success")
    if not stamp:
        return None
    last = datetime.fromisoformat(stamp)
    return (datetime.now(timezone.utc) - last).total_seconds() / 86400


def _record(config: Config, result: RefreshResult) -> None:
    previous = read_status(config) or {}
    payload = {
        "last_run": result.started_at,
        # A poll that reached the portal counts as consumption even when nothing
        # was downloaded - that is what keeps the account alive.
        "last_success": (
            result.finished_at if result.ok else previous.get("last_success")
        ),
        "last_result": asdict(result),
    }
    status_path(config).write_text(json.dumps(payload, indent=2) + "\n")


def refresh(
    config: Config,
    *,
    force: bool = False,
    rebuild_anyway: bool = False,
    horizon_days: int = 90,
    log=print,
    source=None,
) -> RefreshResult:
    """Run the whole pipeline. Safe to call on a schedule.

    `source` exists so tests can supply a fake feed source; leave it unset and
    the real portal client is used.
    """
    import duckdb

    from .model import build_all
    from .parse import ingest_snapshot

    result = RefreshResult(
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds")
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
