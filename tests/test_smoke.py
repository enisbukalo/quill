"""Scaffold smoke tests: package imports + version present."""

import quill
from quill_api.state import RunState, RunStatus, RunStore


def test_version() -> None:
    assert quill.__version__


def test_runstore_tracks_runs_by_id() -> None:
    store = RunStore()
    assert store.active is None

    queued = RunState(run_id="r1", ticket=42)
    store.add(queued)

    # Queued is not active: execution is serialised, so "active" means the one actually running.
    assert store.get("r1") is queued
    assert store.queued() == [queued]
    assert store.active is None

    queued.mark_started()
    assert store.active is queued


def test_runstore_trims_finished_runs() -> None:
    """An always-on service would otherwise accumulate every run it ever executed; SQLite is the
    durable record, this is only the live view."""
    store = RunStore()
    for index in range(RunStore.MAX_FINISHED + 10):
        store.add(RunState(run_id=f"r{index}", ticket=1, status=RunStatus.DONE))

    assert len(store.all()) == RunStore.MAX_FINISHED
