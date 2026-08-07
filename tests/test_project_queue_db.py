"""Durable GitHub Project ticket-queue persistence tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from quill_api.db import History, ProjectQueueItemSpec


def _item(ticket: int, *, title: str | None = None) -> ProjectQueueItemSpec:
    return ProjectQueueItemSpec(
        ticket=ticket,
        project_owner="owner",
        project_title="Board",
        project_item_id=f"PVTI_{ticket}",
        title=title or f"Ticket {ticket}",
        branch=f"enhancement/ticket-{ticket}_{ticket}",
        epic_number=4,
        epic_title="First epic",
    )


def _batch(
    history: History,
    batch_id: str,
    tickets: list[int],
    *,
    created_at: float,
    repo: str = "owner/repo",
) -> None:
    history.create_project_queue_batch(
        batch_id=batch_id,
        repo=repo,
        source="ui",
        items=[_item(ticket) for ticket in tickets],
        created_at=created_at,
    )


def test_batch_creation_is_numeric_fifo_and_idempotent() -> None:
    history = History()
    _batch(history, "batch-a", [16, 3, 100], created_at=10.0)
    _batch(history, "batch-b", [2], created_at=1.0)

    repeated = history.create_project_queue_batch(
        batch_id="batch-a",
        repo="owner/repo",
        source="ui",
        items=[_item(100), _item(3), _item(16)],
        created_at=99.0,
    )

    assert repeated.batch_id == "batch-a"
    assert [(batch.batch_id, batch.position) for batch in history.list_project_queue_batches()] == [
        ("batch-a", 1),
        ("batch-b", 2),
    ]
    assert [item.ticket for item in history.list_project_queue_items(batch_id="batch-a")] == [
        3,
        16,
        100,
    ]


def test_batch_retry_rejects_changed_data_and_active_ticket_duplicate() -> None:
    history = History()
    _batch(history, "batch-a", [3], created_at=1.0)

    with pytest.raises(ValueError, match="different data"):
        history.create_project_queue_batch(
            batch_id="batch-a",
            repo="owner/repo",
            source="ui",
            items=[_item(3, title="Changed")],
        )
    with pytest.raises(ValueError, match="already active"):
        _batch(history, "batch-b", [3], created_at=2.0)


@pytest.mark.parametrize(
    ("batch_id", "repo", "source", "items", "message"),
    [
        ("", "owner/repo", "ui", [_item(1)], "batch_id"),
        ("batch", "", "ui", [_item(1)], "repo"),
        ("batch", "owner/repo", "manual", [_item(1)], "source"),
        ("batch", "owner/repo", "ui", [_item(0)], "positive"),
        ("batch", "owner/repo", "ui", [_item(1), _item(1)], "duplicate"),
    ],
)
def test_batch_validation(
    batch_id: str,
    repo: str,
    source: str,
    items: list[ProjectQueueItemSpec],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        History().create_project_queue_batch(
            batch_id=batch_id,
            repo=repo,
            source=source,
            items=items,
        )


def test_strict_fifo_claim_never_interleaves_batches() -> None:
    history = History()
    _batch(history, "older", [8, 2], created_at=10.0)
    _batch(history, "later", [1], created_at=20.0)
    history.stabilize_project_queue_batch("older", stabilized_at=11.0)
    history.stabilize_project_queue_batch("later", stabilized_at=21.0)

    first = history.claim_project_queue_head(started_at=12.0)
    assert first is not None and (first.batch_id, first.ticket) == ("older", 2)
    assert history.claim_project_queue_head(started_at=13.0) is None
    assert history.attach_project_queue_run(first.item_id, "root-2", root=True, updated_at=14.0)
    assert history.attach_project_queue_pr(first.item_id, 52, updated_at=15.0)
    assert history.claim_project_queue_head(started_at=16.0) is None
    assert history.complete_project_queue_item(first.item_id, pr_number=52, completed_at=17.0)

    second = history.claim_project_queue_head(started_at=18.0)
    assert second is not None and (second.batch_id, second.ticket) == ("older", 8)
    assert history.attach_project_queue_pr(second.item_id, 58, updated_at=19.0)
    assert history.complete_project_queue_item(second.item_id, pr_number=58, completed_at=20.0)

    third = history.claim_project_queue_head(started_at=22.0)
    assert third is not None and (third.batch_id, third.ticket) == ("later", 1)
    assert [batch.state for batch in history.list_project_queue_batches(active_only=False)] == [
        "completed",
        "running",
    ]


def test_unstable_oldest_batch_blocks_stable_later_batch() -> None:
    history = History()
    _batch(history, "older", [2], created_at=1.0)
    _batch(history, "later", [1], created_at=2.0)
    history.stabilize_project_queue_batch("later", stabilized_at=3.0)

    assert history.claim_project_queue_head() is None
    assert history.stabilize_project_queue_batch("older") is not None
    claimed = history.claim_project_queue_head()
    assert claimed is not None and claimed.batch_id == "older"


def test_board_observation_restarts_stabilization_timestamp() -> None:
    history = History()
    _batch(history, "batch", [1], created_at=1.0)

    first = history.observe_project_queue_batch("batch", observed_at=5.0)
    second = history.observe_project_queue_batch("batch", observed_at=8.0)
    stable = history.stabilize_project_queue_batch("batch", stabilized_at=13.0)

    assert first is not None and first.observed_at == 5.0
    assert second is not None and second.observed_at == 8.0
    assert stable is not None
    assert (stable.state, stable.observed_at, stable.stabilized_at) == ("pending", 8.0, 13.0)


def test_pause_blocks_all_later_work_until_same_item_is_reattached() -> None:
    history = History()
    _batch(history, "older", [1, 2], created_at=1.0)
    _batch(history, "later", [3], created_at=2.0)
    history.stabilize_project_queue_batch("older")
    history.stabilize_project_queue_batch("later")
    head = history.claim_project_queue_head()
    assert head is not None

    assert history.pause_project_queue_item(head.item_id, error="run failed", run_id="failed")
    assert history.claim_project_queue_head() is None
    paused = history.find_active_project_queue_item("owner/repo", 1)
    assert paused is not None and paused.state == "paused"
    assert history.attach_project_queue_run(head.item_id, "restart", updated_at=5.0)
    resumed = history.find_active_project_queue_item("owner/repo", 1)
    assert resumed is not None
    assert (resumed.state, resumed.current_run_id, resumed.error) == ("running", "restart", None)


def test_run_and_pr_attachment_are_exact_and_idempotent() -> None:
    history = History()
    _batch(history, "batch", [1], created_at=1.0)
    history.stabilize_project_queue_batch("batch")
    item = history.claim_project_queue_head()
    assert item is not None

    assert history.attach_project_queue_run(item.item_id, "root", root=True)
    assert history.attach_project_queue_run(item.item_id, "root", root=True)
    assert not history.attach_project_queue_run(item.item_id, "other-root", root=True)
    assert history.project_queue_item_for_run("root") is not None
    assert history.attach_project_queue_pr(item.item_id, 12)
    assert history.attach_project_queue_pr(item.item_id, 12)
    assert not history.attach_project_queue_pr(item.item_id, 13)
    assert not history.complete_project_queue_item(item.item_id, pr_number=13)
    assert history.complete_project_queue_item(item.item_id, pr_number=12)
    assert history.complete_project_queue_item(item.item_id, pr_number=12)


def test_remove_accepts_nonexecuting_work_and_empty_batch_completes() -> None:
    history = History()
    _batch(history, "empty", [1], created_at=1.0)
    item = history.list_project_queue_items(batch_id="empty")[0]

    assert history.remove_project_queue_item(item.item_id, removed_at=2.0)
    assert history.remove_project_queue_item(item.item_id, removed_at=3.0)
    batch = history.list_project_queue_batches(active_only=False)[0]
    assert (batch.state, batch.completed_at) == ("completed", 2.0)
    assert history.list_project_queue_batches() == []
    assert history.list_project_queue_items() == []

    _batch(history, "running", [2], created_at=4.0)
    history.stabilize_project_queue_batch("running")
    running = history.claim_project_queue_head()
    assert running is not None
    assert not history.remove_project_queue_item(running.item_id)
    assert history.pause_project_queue_item(running.item_id, error="run failed")
    assert history.remove_project_queue_item(running.item_id)


def test_empty_created_batch_is_completed_without_claim() -> None:
    history = History()
    batch = history.create_project_queue_batch(
        batch_id="empty", repo="owner/repo", source="board", items=[], created_at=1.0
    )

    assert (batch.state, batch.completed_at) == ("completed", 1.0)
    assert history.claim_project_queue_head() is None


def test_restart_recovers_order_and_preserves_inflight_ownership(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'quill.db'}"
    first = History(url)
    _batch(first, "older", [2, 1], created_at=1.0)
    _batch(first, "later", [3], created_at=2.0)
    first.stabilize_project_queue_batch("older")
    first.stabilize_project_queue_batch("later")
    claimed = first.claim_project_queue_head()
    assert claimed is not None and claimed.ticket == 1
    first.attach_project_queue_run(claimed.item_id, "run-1", root=True)

    restarted = History(url)
    recovered = restarted.recover_project_queue()

    assert [(item.batch_id, item.ticket, item.state) for item in recovered] == [
        ("older", 1, "running"),
        ("older", 2, "pending"),
        ("later", 3, "pending"),
    ]
    assert restarted.claim_project_queue_head() is None
    owner = restarted.project_queue_item_for_run("run-1")
    assert owner is not None and owner.ticket == 1


def test_atomic_head_claim_has_one_winner(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'quill.db'}"
    history = History(url)
    _batch(history, "batch", [1, 2], created_at=1.0)
    history.stabilize_project_queue_batch("batch")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: history.claim_project_queue_head(), range(4)))

    claimed = [item for item in results if item is not None]
    assert len(claimed) == 1
    assert claimed[0].ticket == 1
    assert [item.state for item in history.list_project_queue_items()] == ["running", "pending"]


def test_concurrent_identical_batch_creation_is_idempotent(tmp_path: Path) -> None:
    history = History(f"sqlite+pysqlite:///{tmp_path / 'quill.db'}")

    def create(_index: int) -> int:
        return history.create_project_queue_batch(
            batch_id="same",
            repo="owner/repo",
            source="ui",
            items=[_item(1)],
            created_at=1.0,
        ).position

    with ThreadPoolExecutor(max_workers=2) as pool:
        positions = list(pool.map(create, range(2)))

    assert positions == [1, 1]
    assert len(history.list_project_queue_batches()) == 1
    assert len(history.list_project_queue_items()) == 1


def test_completed_ticket_can_be_queued_again_but_paused_ticket_cannot() -> None:
    history = History()
    _batch(history, "first", [1], created_at=1.0)
    history.stabilize_project_queue_batch("first")
    first = history.claim_project_queue_head()
    assert first is not None
    history.attach_project_queue_pr(first.item_id, 10)
    history.complete_project_queue_item(first.item_id, pr_number=10)

    _batch(history, "second", [1], created_at=2.0)
    history.stabilize_project_queue_batch("second")
    second = history.claim_project_queue_head()
    assert second is not None
    history.pause_project_queue_item(second.item_id, error="halted")

    with pytest.raises(ValueError, match="already active"):
        _batch(history, "third", [1], created_at=3.0)


def test_compare_and_set_transition_rejects_stale_state() -> None:
    history = History()
    _batch(history, "batch", [1], created_at=1.0)
    history.stabilize_project_queue_batch("batch")
    item = history.claim_project_queue_head()
    assert item is not None

    assert not history.transition_project_queue_item(
        item.item_id, state="waiting_pr", expected_states=("paused",)
    )
    assert history.transition_project_queue_item(
        item.item_id, state="waiting_pr", expected_states=("running",)
    )
    with pytest.raises(ValueError, match="terminal"):
        history.transition_project_queue_item(
            item.item_id, state="completed", expected_states=("waiting_pr",)
        )
