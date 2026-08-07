"""Run-history persistence tests (WI-10)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import text

from quill_api.db import History
from quill_api.state import RunState, RunStatus


def _state(run_id: str, ticket: int, status: RunStatus) -> RunState:
    return RunState(run_id=run_id, ticket=ticket, status=status, pr_url=f"url-{run_id}")


def test_record_and_get() -> None:
    h = History()
    h.record(_state("a", 1, RunStatus.DONE))
    row = h.get("a")
    assert row is not None
    assert row.ticket == 1
    assert row.status == "done"
    assert row.pr_url == "url-a"


def test_get_missing_returns_none() -> None:
    assert History().get("nope") is None


def test_exact_pr_head_review_is_deduplicated() -> None:
    history = History()
    state = _state("review", 14, RunStatus.QUEUED)
    state.repo = "me/game"
    state.mode = "review"
    state.pr_number = 31
    state.pr_head_sha = "abc123"
    history.record(state)

    assert history.has_pr_review("me/game", 31, "abc123")
    assert not history.has_pr_review("me/game", 31, "new456")


def test_pr_feedback_pass_completes_without_dispatch() -> None:
    history = History()

    cycle = history.record_pr_feedback_result(
        review_run_id="review-pass",
        repo="me/game",
        pr_number=31,
        ticket=14,
        branch="feature",
        reviewed_head_sha="abc123",
        findings_digest="digest",
        verdict="PASS",
        max_cycles=5,
    )

    assert cycle.status == "pass_complete"
    assert history.recoverable_pr_feedback_cycles() == []


def test_pr_feedback_block_is_claimed_exactly_once() -> None:
    history = History()
    cycle = history.record_pr_feedback_result(
        review_run_id="review-block",
        repo="me/game",
        pr_number=31,
        ticket=14,
        branch="feature",
        reviewed_head_sha="abc123",
        findings_digest="digest",
        verdict="BLOCK",
        max_cycles=5,
    )

    duplicate = history.record_pr_feedback_result(
        review_run_id="different-run",
        repo="me/game",
        pr_number=31,
        ticket=14,
        branch="feature",
        reviewed_head_sha="abc123",
        findings_digest="digest",
        verdict="BLOCK",
        max_cycles=5,
    )

    assert duplicate.review_run_id == cycle.review_run_id
    assert history.attach_pr_feedback_update(cycle.review_run_id, "update-1")
    assert not history.attach_pr_feedback_update(cycle.review_run_id, "update-2")


def test_pr_feedback_cycle_limit_stops_another_update() -> None:
    history = History()
    first = history.record_pr_feedback_result(
        review_run_id="review-1",
        repo="me/game",
        pr_number=31,
        ticket=14,
        branch="feature",
        reviewed_head_sha="head-1",
        findings_digest="digest-1",
        verdict="BLOCK",
        max_cycles=1,
    )
    second = history.record_pr_feedback_result(
        review_run_id="review-2",
        repo="me/game",
        pr_number=31,
        ticket=14,
        branch="feature",
        reviewed_head_sha="head-2",
        findings_digest="digest-2",
        verdict="BLOCK",
        max_cycles=1,
    )

    assert first.status == "update_pending"
    assert second.status == "cycle_limit_reached"


def test_pr_feedback_lost_dispatch_replays_only_once() -> None:
    history = History()
    cycle = history.record_pr_feedback_result(
        review_run_id="review",
        repo="me/game",
        pr_number=31,
        ticket=14,
        branch="feature",
        reviewed_head_sha="head",
        findings_digest="digest",
        verdict="BLOCK",
        max_cycles=5,
    )
    assert history.attach_pr_feedback_update(cycle.review_run_id, "lost-1")

    replay = history.recoverable_pr_feedback_cycles()
    assert len(replay) == 1
    assert replay[0].dispatch_attempts == 1
    assert history.attach_pr_feedback_update(cycle.review_run_id, "lost-2")

    assert history.recoverable_pr_feedback_cycles() == []


def test_completed_feedback_update_remains_reconcilable_after_transient_failure() -> None:
    history = History()
    cycle = history.record_pr_feedback_result(
        review_run_id="review",
        repo="me/game",
        pr_number=31,
        ticket=14,
        branch="feature",
        reviewed_head_sha="head",
        findings_digest="digest",
        verdict="BLOCK",
        max_cycles=5,
    )
    assert history.attach_pr_feedback_update(cycle.review_run_id, "update")
    update_state = _state("update", 14, RunStatus.DONE)
    update_state.mode = "update"
    update_state.repo = "me/game"
    history.record(update_state)

    rows = history.unreconciled_completed_pr_feedback_updates()

    assert [row.run_id for row in rows] == ["update"]


def test_list_orders_recent_first() -> None:
    h = History()
    h.record(_state("a", 1, RunStatus.DONE))
    h.record(_state("b", 2, RunStatus.FAILED))
    ids = [r.run_id for r in h.recent()]
    assert set(ids) == {"a", "b"}


def test_record_replaces_same_run() -> None:
    h = History()
    h.record(_state("a", 1, RunStatus.RUNNING))
    h.record(_state("a", 1, RunStatus.DONE))  # same run_id -> merge/replace
    rows = h.recent()
    assert len(rows) == 1
    assert rows[0].status == "done"


def test_record_persists_last_phase_and_bulk_delete_keeps_lifetime_accounting() -> None:
    h = History()
    state = _state("a", 1, RunStatus.DONE)
    state.phase = "build"
    state.phase_label = "Build executables"
    state.failure_code = "build_failed"
    state.failure_label = "Local build failed"
    state.workflow = "pr_review"
    h.record(state)
    h.record_breakdown("a", {"schema_version": 1}, 1)

    row = h.get("a")
    assert row is not None
    assert (row.last_phase, row.last_phase_label) == ("build", "Build executables")
    assert (row.failure_code, row.failure_label) == ("build_failed", "Local build failed")

    h.delete_many(["a"])
    assert h.get("a") is None
    assert h.get_breakdown("a") is None
    lifetime = h.lifetime_rows()
    assert [row.run_id for row in lifetime] == ["a"]
    assert (lifetime[0].failure_code, lifetime[0].failure_label) == (
        "build_failed",
        "Local build failed",
    )
    assert lifetime[0].workflow == "pr_review"
    assert h.lifetime_breakdowns() == [{"schema_version": 1}]


def test_lifetime_migration_backfills_workflow_from_retained_run(tmp_path: Path) -> None:
    database = tmp_path / "quill.db"
    history = History(f"sqlite+pysqlite:///{database}")
    state = _state("review", 7, RunStatus.DONE)
    state.workflow = "pr_review"
    history.record(state)
    with history._engine.begin() as connection:
        connection.execute(text("ALTER TABLE lifetime_runs DROP COLUMN workflow"))
    history._engine.dispose()

    migrated = History(f"sqlite+pysqlite:///{database}")

    assert migrated.lifetime_rows()[0].workflow == "pr_review"


def test_application_setting_round_trip_and_replace() -> None:
    history = History()
    assert history.get_setting("telemetry_display") is None

    history.set_setting("telemetry_display", {"cpu_temperature_min_c": 20.0})
    assert history.get_setting("telemetry_display") == {"cpu_temperature_min_c": 20.0}

    history.set_setting("telemetry_display", {"cpu_temperature_min_c": 25.0})
    assert history.get_setting("telemetry_display") == {"cpu_temperature_min_c": 25.0}


def test_system_power_average_is_time_weighted_and_persistent(tmp_path: Path) -> None:
    database = tmp_path / "power.db"
    history = History(f"sqlite+pysqlite:///{database}")
    history.record_system_power_sample(100.0, 10.0)
    history.record_system_power_sample(200.0, 11.0)
    history.record_system_power_sample(300.0, 12.0)
    assert history.average_system_power_w() == 200.0
    history.flush_system_power_samples()

    restored = History(f"sqlite+pysqlite:///{database}")
    assert restored.average_system_power_w() == 200.0


def test_repo_filter_finds_legacy_url_shaped_history() -> None:
    h = History()
    state = _state("legacy", 1, RunStatus.DONE)
    state.repo = "https://github.com/me/proj.git"
    h.record(state)

    assert [row.run_id for row in h.recent(repo="me/proj")] == ["legacy"]


def test_file_history_uses_distinct_connections_for_concurrent_requests(tmp_path: Path) -> None:
    history = History(f"sqlite+pysqlite:///{tmp_path / 'quill.db'}")
    barrier = threading.Barrier(2)

    def concurrent_query() -> tuple[int, int]:
        with history._engine.connect() as connection:
            dbapi_id = id(connection.connection.dbapi_connection)
            barrier.wait(timeout=2)
            value = connection.scalar(text("SELECT 1"))
            return dbapi_id, value

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: concurrent_query(), range(2)))

    assert len({dbapi_id for dbapi_id, _value in results}) == 2
    assert [value for _dbapi_id, value in results] == [1, 1]
