"""The scheduled refresh.

The subtle part is what counts as success. Reaching the portal is what stops
NRDP deleting the account, so a run that downloads nothing is still a good run.
A run where the daily guard skipped every feed is not - nothing reached the
portal, and treating it as success would let the account quietly lapse.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from rail.acquire import Feed, FetchResult, PollTooSoon
from rail.config import Config
from rail.refresh import (
    MANUAL,
    SCHEDULED,
    TRIGGER_ENV,
    current_trigger,
    days_since_last_success,
    history_path,
    read_status,
    refresh,
)


class FakeSource:
    """Stands in for the portal client."""

    name = "fake"

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.polled: list[Feed] = []

    def fetch(self, feed: Feed, force: bool = False) -> FetchResult:
        self.polled.append(feed)
        outcome = self.behaviour.get(feed, "unchanged")
        if outcome == "guarded":
            raise PollTooSoon(f"{feed.value} was polled recently")
        if outcome == "error":
            raise RuntimeError("the portal fell over")
        return FetchResult(
            feed=feed, path=None, filename=f"{feed.prefix}0001.ZIP",
            last_modified="Tue, 21 Jul 2026 19:44:07 GMT",
            downloaded=False, reason="unchanged",
        )


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path, nrdp_username="u", nrdp_password="p")


def run(config, behaviour, trigger=MANUAL):
    return refresh(
        config, source=FakeSource(behaviour), log=lambda _: None, trigger=trigger
    )


def test_reaching_the_portal_is_success_even_with_nothing_to_download(config):
    """Fares change three times a year; most runs find nothing new."""
    result = run(config, {})

    assert result.ok
    assert not result.changed
    assert not result.rebuilt  # no point rebuilding unchanged data


def test_a_run_blocked_by_the_daily_guard_is_not_success(config):
    """Nothing reached the portal, so it does not renew the account."""
    result = run(config, {feed: "guarded" for feed in Feed})

    assert not result.ok
    assert not result.errors  # not an error, just a no-op


def test_a_failing_feed_marks_the_run_failed_but_others_still_run(config):
    source = FakeSource({Feed.FARES: "error"})
    result = refresh(config, source=source, log=lambda _: None)

    assert not result.ok
    assert any("fares" in e for e in result.errors)
    assert set(source.polled) == set(Feed)  # one bad feed did not stop the rest


def test_success_is_recorded_and_reported_in_days(config):
    run(config, {})

    assert read_status(config)["last_success"]
    elapsed = days_since_last_success(config)
    assert elapsed is not None and elapsed < 1


def test_a_later_failure_does_not_erase_the_last_success(config):
    """`rail status` must keep counting from the last run that actually worked."""
    run(config, {})
    first = read_status(config)["last_success"]

    run(config, {feed: "error" for feed in Feed})
    status = read_status(config)

    assert status["last_success"] == first
    assert status["last_run"] > first or status["last_run"] >= first


def test_no_status_file_means_no_elapsed_time(config):
    assert days_since_last_success(config) is None


def test_every_feed_is_polled(config):
    source = FakeSource({})
    refresh(config, source=source, log=lambda _: None)

    assert set(source.polled) == {Feed.TIMETABLE, Feed.FARES, Feed.ROUTEING}


def test_the_result_records_what_happened_per_feed(config):
    result = run(config, {Feed.ROUTEING: "guarded"})

    assert result.fetched["timetable"] == "unchanged"
    assert "skipped" in result.fetched["routeing"]


def test_status_survives_a_corrupt_file(config):
    (config.data_dir / "refresh-status.json").write_text("{ not json")

    assert read_status(config) is None
    assert days_since_last_success(config) is None


def test_dates_in_the_status_file_are_utc_isoformat(config):
    run(config, {})
    stamp = read_status(config)["last_success"]

    parsed = dt.datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None


# --- what started the run, which is a different question from whether it worked


def test_an_unset_environment_means_manual(monkeypatch):
    """The safe default: a scheduled run wrongly called manual makes somebody
    look, where the reverse hides a dead agent behind their own typing."""
    monkeypatch.delenv(TRIGGER_ENV, raising=False)
    assert current_trigger() == MANUAL


def test_only_the_exact_value_counts_as_scheduled(monkeypatch):
    monkeypatch.setenv(TRIGGER_ENV, SCHEDULED)
    assert current_trigger() == SCHEDULED

    for near_miss in ["Scheduled", "1", "true", "", "launchd"]:
        monkeypatch.setenv(TRIGGER_ENV, near_miss)
        assert current_trigger() == MANUAL, near_miss


def test_a_manual_fetch_does_not_renew_the_scheduled_clock(config):
    """The whole point. The agent was unloaded for 26 days while the account
    stayed healthy, because somebody had fetched by hand in the middle."""
    first = run(config, {}, trigger=SCHEDULED)
    scheduled = read_status(config)["last_scheduled_success"]
    assert scheduled == first.finished_at

    second = run(config, {}, trigger=MANUAL)
    status = read_status(config)

    assert status["last_success"] == second.finished_at   # the account is fine
    assert status["last_scheduled_success"] == scheduled  # the schedule is not


def test_a_failed_scheduled_run_does_not_renew_it_either(config):
    run(config, {}, trigger=SCHEDULED)
    first = read_status(config)["last_scheduled_success"]

    run(config, {feed: "error" for feed in Feed}, trigger=SCHEDULED)

    assert read_status(config)["last_scheduled_success"] == first


def test_the_two_clocks_are_asked_separately(config):
    run(config, {}, trigger=MANUAL)

    assert days_since_last_success(config) is not None
    assert days_since_last_success(config, scheduled_only=True) is None


def test_a_status_file_predating_this_reads_as_never_scheduled(config):
    """An older engine wrote no such key. Absent must not read as fresh."""
    run(config, {})
    status = read_status(config)
    del status["last_scheduled_success"]
    (config.data_dir / "refresh-status.json").write_text(json.dumps(status))

    assert days_since_last_success(config, scheduled_only=True) is None


# --- the ledger, which is every run rather than the launchd ones -------------


def test_every_run_is_appended_whoever_started_it(config):
    run(config, {}, trigger=SCHEDULED)
    run(config, {}, trigger=MANUAL)
    run(config, {feed: "error" for feed in Feed}, trigger=MANUAL)

    lines = history_path(config).read_text().strip().splitlines()
    entries = [json.loads(line) for line in lines]

    assert [entry["trigger"] for entry in entries] == [SCHEDULED, MANUAL, MANUAL]
    assert [entry["ok"] for entry in entries] == [True, True, False]


def test_the_ledger_is_not_the_launchd_log(config):
    """`refresh.log` is the agent's StandardOutPath. A second writer to it
    would double every scheduled line - a bug this project has shipped once."""
    run(config, {})

    assert history_path(config).name != "refresh.log"
    assert not (config.log_dir / "refresh.log").exists()


def test_a_ledger_that_cannot_be_written_does_not_break_the_refresh(config):
    """Recording the run matters less than the run."""
    config.log_dir.parent.mkdir(parents=True, exist_ok=True)
    config.log_dir.write_text("not a directory")

    result = run(config, {})

    assert result.ok
    assert read_status(config)["last_success"]


# --- one build sequence, not two ---------------------------------------------


def test_the_refresh_does_not_keep_its_own_list_of_build_stages():
    """`rail build` and `rail refresh` must run the *same* stages.

    They used to each name their own, and the lists drifted: refresh ran five
    of the ten. Nothing errored, because the missing stages leave tables that a
    previous manual build had already written - so the database stayed
    plausible and went stale in place. `ticket_validity_current` was the tell:
    `build_fares_reference` writes a six-column intermediate of it and
    `build_ticket_validity` replaces that with the real fifteen-column table, so
    skipping the second leaves a table that exists, has the right row count, and
    is missing every return-window column.

    Pinned at the source level because the failure is a *missing* call, which no
    amount of exercising the code that is still there will catch.
    """
    import inspect

    from rail import cli, refresh as refresh_module
    from rail.model import build_all

    for module in (cli, refresh_module):
        source = inspect.getsource(module)
        assert "build_all(" in source, f"{module.__name__} must delegate"
        for stage in ("build_reference(", "build_timetable(", "build_railcards(",
                      "build_fares_reference(", "build_restrictions("):
            assert stage not in source, (
                f"{module.__name__} names {stage} itself - that is the "
                "duplication that drifted"
            )

    # And the one sequence really does run every stage the model exports.
    body = inspect.getsource(build_all)
    for stage in ("build_reference(", "build_timetable(", "classify_locations(",
                  "build_fares_reference(", "build_restrictions(",
                  "build_ticket_validity(", "build_railcards(",
                  "build_associations(", "build_plusbus(", "build_routeing("):
        assert stage in body, f"build_all is missing {stage}"


def test_the_optional_sources_survive_a_build():
    """Positions and the supplementary station list are passed on every build.

    A refresh that dropped them rebuilt `station` with no corroborated grid
    references at all - which looks like nothing at the row-count level and
    moves every station on a map.
    """
    import inspect

    from rail.model import build_all

    body = inspect.getsource(build_all)
    for optional in ("supplementary", "geography", "naptan"):
        assert f'_optional("{optional}")' in body
    assert "supplementary_dir, geography_dir, naptan_dir" in body
