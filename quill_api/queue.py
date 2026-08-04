"""FIFO run queue with a single worker thread (server milestone B3).

Runs execute one at a time because the GPU is genuinely exclusive: the llama.cpp router holds one
preset, vLLM one resident model. That constraint is real and stays.

What changes is what happens to the *second* request. The old service raised a conflict and
rejected it, which is wrong for a shared service — a client that asks while something is running
should be told "you are second in line", not "no". So submissions are accepted into a queue and a
single worker drains it in order.

The queue owns only scheduling. Preparing a workspace and driving the pipeline belong to the
executor passed in, so this module stays testable without git, models, or GitHub.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class QueuedRun:
    """Everything needed to execute a run, captured when it was submitted."""

    run_id: str
    repo: str
    branch: str
    ticket: int
    mode: str
    workflow: str = "ticket"
    pr_number: int | None = None
    pr_head_sha: str | None = None
    feedback_digest: str | None = None
    clear_prefix_cache: bool = False
    model_overrides: tuple[tuple[str, str], ...] = ()
    source_run_id: str | None = None
    start_phase: str | None = None
    checkpoint_commit: str | None = None


#: Executes one run to completion. Exceptions are caught by the worker, never killing the thread.
type RunExecutor = Callable[[QueuedRun], None]
type QueueChanged = Callable[[], None]


@dataclass(slots=True)
class _Worker:
    thread: threading.Thread | None = None
    stopping: threading.Event = field(default_factory=threading.Event)


class RunQueue:
    """Accepts runs, executes them one at a time, in submission order."""

    def __init__(self, execute: RunExecutor, on_change: QueueChanged | None = None) -> None:
        self._execute = execute
        self._queue: queue.Queue[QueuedRun | None] = queue.Queue()
        self._pending: list[QueuedRun] = []  # mirrors the queue, for position/introspection
        self._cancelled: set[str] = set()
        self._active_id: str | None = None
        self._lock = threading.Lock()
        self._worker = _Worker()
        self._on_change = on_change

    def set_on_change(self, callback: QueueChanged) -> None:
        self._on_change = callback

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()

    def start(self) -> None:
        """Begin draining. Idempotent, so app startup can call it unconditionally."""
        if self._worker.thread is not None and self._worker.thread.is_alive():
            return
        self._worker.stopping.clear()
        self._worker.thread = threading.Thread(target=self._drain, name="quill-queue", daemon=True)
        self._worker.thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop draining after the current run finishes.

        Does not interrupt a run in flight — cancelling mid-phase would leave a half-written branch
        and a loaded model. Callers wanting that ask the run itself to stop first.
        """
        self._worker.stopping.set()
        self._queue.put(None)  # wake the worker so it notices the flag
        thread = self._worker.thread
        if thread is not None:
            thread.join(timeout=timeout)

    def submit(self, run: QueuedRun) -> int:
        """Enqueue ``run``; return how many runs are ahead of it (0 = starts immediately)."""
        with self._lock:
            position = len(self._pending)
            self._pending.append(run)
        self._queue.put(run)
        self._changed()
        return position

    def position(self, run_id: str) -> int | None:
        """How many runs are ahead of ``run_id``, or None if it is not waiting."""
        with self._lock:
            for index, run in enumerate(self._pending):
                if run.run_id == run_id:
                    return index
        return None

    def pending(self) -> list[QueuedRun]:
        with self._lock:
            return list(self._pending)

    def cancel(self, run_id: str) -> bool:
        """Remove a waiting run from the visible queue and ensure the worker skips it.

        ``queue.Queue`` cannot remove an arbitrary item, so cancellation uses a tombstone. The
        backing item is consumed normally but never executed.
        """
        with self._lock:
            if self._active_id == run_id:
                return False
            if not any(run.run_id == run_id for run in self._pending):
                return False
            self._pending = [run for run in self._pending if run.run_id != run_id]
            self._cancelled.add(run_id)
        self._changed()
        return True

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._pending)

    # -- worker -------------------------------------------------------------------

    def _drain(self) -> None:
        while not self._worker.stopping.is_set():
            item = self._queue.get()
            if item is None:  # wake-up sentinel from stop()
                self._queue.task_done()
                continue
            with self._lock:
                self._active_id = item.run_id
                cancelled = item.run_id in self._cancelled
                self._cancelled.discard(item.run_id)
            self._changed()
            try:
                if not cancelled:
                    self._execute(item)
            except Exception:  # noqa: BLE001,S110 - a bad run must never kill the queue
                # The executor is responsible for recording the failure on the RunState; swallowing
                # here only protects the worker thread, which every later run depends on.
                pass
            finally:
                with self._lock:
                    self._pending = [r for r in self._pending if r.run_id != item.run_id]
                    self._active_id = None
                self._changed()
                self._queue.task_done()
