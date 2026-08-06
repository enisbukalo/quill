"""Run-history persistence (WI-10) — SQLite via SQLAlchemy 2.0.

Live `RunState` stays in memory; this stores a compact **summary row per run** so `/runs` survives
a restart. No live state, no raw logs.

The default URL is still ``:memory:`` for tests, but the service points it at a **file**: an
always-on process gets restarted — for a deploy, a reboot, a crash — and an in-memory table
silently discards the history this module exists to keep.

Restarts also strand runs: one that was executing when the process died is still marked ``running``
in its row, and nothing will ever finish it. :meth:`History.reconcile_orphans` closes those out at
startup so the list never claims a run is live when no thread is behind it.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    JSON,
    Engine,
    Index,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    func,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.engine import CursorResult, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from quill_api.state import RunState, RunStatus

#: Stable names for constraints and indexes. Set before the first table is created: without it
#: SQLite invents names, and a later migration cannot reliably drop what it cannot name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class RunRow(Base):
    """One run's summary."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticket: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(20))
    #: Which repo/branch the run shipped. A run id alone no longer identifies the work now that
    #: one service serves every repo.
    repo: Mapped[str | None] = mapped_column(String(255), default=None)
    branch: Mapped[str | None] = mapped_column(String(255), default=None)
    mode: Mapped[str] = mapped_column(String(20), default="create")
    workflow: Mapped[str] = mapped_column(String(100), default="ticket")
    pr_number: Mapped[int | None] = mapped_column(default=None)
    pr_head_sha: Mapped[str | None] = mapped_column(String(64), default=None)
    feedback_digest: Mapped[str | None] = mapped_column(String(64), default=None)
    source_run_id: Mapped[str | None] = mapped_column(String(64), default=None)
    start_phase: Mapped[str | None] = mapped_column(String(100), default=None)
    pr_url: Mapped[str | None] = mapped_column(String(512), default=None)
    error: Mapped[str | None] = mapped_column(String(2000), default=None)
    failure_code: Mapped[str | None] = mapped_column(String(100), default=None)
    failure_label: Mapped[str | None] = mapped_column(String(255), default=None)
    started_at: Mapped[float] = mapped_column(default=0.0)
    finished_at: Mapped[float] = mapped_column(default=0.0)
    clear_prefix_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    last_phase: Mapped[str | None] = mapped_column(String(100), default=None)
    last_phase_label: Mapped[str | None] = mapped_column(String(255), default=None)

    __table_args__ = (Index("ix_runs_repo_finished_at", "repo", "finished_at"),)


class BreakdownRow(Base):
    """Complete normalized telemetry projection; transcript files remain the raw source."""

    __tablename__ = "run_breakdowns"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[float] = mapped_column(default=0.0)


class LifetimeRunRow(Base):
    """Permanent accounting record, independent of retained run details and artifacts."""

    __tablename__ = "lifetime_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticket: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(20))
    repo: Mapped[str | None] = mapped_column(String(255), default=None)
    workflow: Mapped[str] = mapped_column(String(100), default="ticket")
    failure_code: Mapped[str | None] = mapped_column(String(100), default=None)
    failure_label: Mapped[str | None] = mapped_column(String(255), default=None)
    started_at: Mapped[float] = mapped_column(default=0.0)
    finished_at: Mapped[float] = mapped_column(default=0.0)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[float] = mapped_column(default=0.0)


class AppSettingRow(Base):
    """One persisted application setting document."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[float] = mapped_column(default=0.0)


class PrFeedbackCycleRow(Base):
    """Persistent outbox and audit record for one reviewed PR head."""

    __tablename__ = "pr_feedback_cycles"

    review_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repo: Mapped[str] = mapped_column(String(255))
    pr_number: Mapped[int] = mapped_column()
    ticket: Mapped[int] = mapped_column()
    branch: Mapped[str] = mapped_column(String(255))
    reviewed_head_sha: Mapped[str] = mapped_column(String(64))
    findings_digest: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(10))
    cycle_number: Mapped[int] = mapped_column(Integer)
    max_cycles: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    update_run_id: Mapped[str | None] = mapped_column(String(64), default=None)
    resulting_head_sha: Mapped[str | None] = mapped_column(String(64), default=None)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(2000), default=None)
    created_at: Mapped[float] = mapped_column(default=0.0)
    updated_at: Mapped[float] = mapped_column(default=0.0)

    __table_args__ = (
        UniqueConstraint(
            "repo", "pr_number", "reviewed_head_sha", name="uq_pr_feedback_cycles_revision"
        ),
        Index("ix_pr_feedback_cycles_status", "status"),
    )


PROJECT_QUEUE_STATES = (
    "stabilizing",
    "pending",
    "running",
    "waiting_pr",
    "paused",
    "completed",
    "removed",
)
PROJECT_QUEUE_TERMINAL_STATES = ("completed", "removed")
PROJECT_QUEUE_ACTIVE_STATES = tuple(
    state for state in PROJECT_QUEUE_STATES if state not in PROJECT_QUEUE_TERMINAL_STATES
)


class ProjectQueueBatchRow(Base):
    """One durable FIFO submission to the GitHub Project ticket queue."""

    __tablename__ = "project_queue_batches"

    #: SQLite assigns this monotonically increasing value. It is the authoritative FIFO key;
    #: timestamps are retained for display and audit, not ordering.
    position: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), unique=True)
    repo: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20), default="stabilizing")
    created_at: Mapped[float] = mapped_column(default=0.0)
    observed_at: Mapped[float | None] = mapped_column(default=None)
    stabilized_at: Mapped[float | None] = mapped_column(default=None)
    started_at: Mapped[float | None] = mapped_column(default=None)
    completed_at: Mapped[float | None] = mapped_column(default=None)
    updated_at: Mapped[float] = mapped_column(default=0.0)
    error: Mapped[str | None] = mapped_column(String(2000), default=None)

    __table_args__ = (
        CheckConstraint(
            f"state IN ({', '.join(repr(state) for state in PROJECT_QUEUE_STATES)})",
            name="project_queue_batch_state",
        ),
        CheckConstraint("source IN ('ui', 'board')", name="project_queue_batch_source"),
        Index("ix_project_queue_batches_state_position", "state", "position"),
        {"sqlite_autoincrement": True},
    )


class ProjectQueueItemRow(Base):
    """One ticket in a durable project-queue batch."""

    __tablename__ = "project_queue_items"

    item_id: Mapped[str] = mapped_column(String(130), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("project_queue_batches.batch_id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    repo: Mapped[str] = mapped_column(String(255))
    ticket: Mapped[int] = mapped_column(Integer)
    project_owner: Mapped[str] = mapped_column(String(255))
    project_title: Mapped[str] = mapped_column(String(255))
    project_item_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(1000))
    epic_number: Mapped[int | None] = mapped_column(Integer, default=None)
    epic_title: Mapped[str | None] = mapped_column(String(1000), default=None)
    state: Mapped[str] = mapped_column(String(20), default="stabilizing")
    branch: Mapped[str] = mapped_column(String(255))
    workflow: Mapped[str] = mapped_column(String(100), default="ticket")
    root_run_id: Mapped[str | None] = mapped_column(String(64), default=None)
    current_run_id: Mapped[str | None] = mapped_column(String(64), default=None)
    pr_number: Mapped[int | None] = mapped_column(Integer, default=None)
    last_board_status: Mapped[str | None] = mapped_column(String(100), default=None)
    error: Mapped[str | None] = mapped_column(String(2000), default=None)
    created_at: Mapped[float] = mapped_column(default=0.0)
    updated_at: Mapped[float] = mapped_column(default=0.0)
    started_at: Mapped[float | None] = mapped_column(default=None)
    completed_at: Mapped[float | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint(
            f"state IN ({', '.join(repr(state) for state in PROJECT_QUEUE_STATES)})",
            name="project_queue_item_state",
        ),
        UniqueConstraint("batch_id", "position", name="uq_project_queue_items_batch_position"),
        UniqueConstraint("batch_id", "ticket", name="uq_project_queue_items_batch_ticket"),
        # A ticket may be queued again after completion/removal, but it can have only one live
        # owner. This closes the race between UI submission and board reconciliation.
        Index(
            "uq_project_queue_items_active_repo_ticket",
            "repo",
            "ticket",
            unique=True,
            sqlite_where=text(
                "state IN ('stabilizing', 'pending', 'running', 'waiting_pr', 'paused')"
            ),
        ),
        Index("ix_project_queue_items_batch_state_position", "batch_id", "state", "position"),
        Index("ix_project_queue_items_current_run", "current_run_id"),
    )


@dataclass(frozen=True, slots=True)
class ProjectQueueItemSpec:
    """Validated ticket metadata used to create one durable queue item."""

    ticket: int
    project_owner: str
    project_title: str
    project_item_id: str
    title: str
    branch: str
    epic_number: int | None = None
    epic_title: str | None = None
    workflow: str = "ticket"
    last_board_status: str | None = "Queue"


@dataclass(frozen=True, slots=True)
class ProjectQueueBatch:
    """Detached batch value safe to use after its database session closes."""

    batch_id: str
    position: int
    repo: str
    source: str
    state: str
    created_at: float
    observed_at: float | None
    stabilized_at: float | None
    started_at: float | None
    completed_at: float | None
    updated_at: float
    error: str | None


@dataclass(frozen=True, slots=True)
class ProjectQueueItem:
    """Detached ticket value safe to use after its database session closes."""

    item_id: str
    batch_id: str
    position: int
    repo: str
    ticket: int
    project_owner: str
    project_title: str
    project_item_id: str
    title: str
    epic_number: int | None
    epic_title: str | None
    state: str
    branch: str
    workflow: str
    root_run_id: str | None
    current_run_id: str | None
    pr_number: int | None
    last_board_status: str | None
    error: str | None
    created_at: float
    updated_at: float
    started_at: float | None
    completed_at: float | None


@dataclass(frozen=True, slots=True)
class PrFeedbackCycle:
    """Detached cycle value safe to use after its database session closes."""

    review_run_id: str
    repo: str
    pr_number: int
    ticket: int
    branch: str
    reviewed_head_sha: str
    findings_digest: str
    verdict: str
    cycle_number: int
    max_cycles: int
    status: str
    update_run_id: str | None
    resulting_head_sha: str | None
    dispatch_attempts: int
    error: str | None


def _project_queue_batch(row: ProjectQueueBatchRow) -> ProjectQueueBatch:
    return ProjectQueueBatch(
        batch_id=row.batch_id,
        position=row.position,
        repo=row.repo,
        source=row.source,
        state=row.state,
        created_at=row.created_at,
        observed_at=row.observed_at,
        stabilized_at=row.stabilized_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        updated_at=row.updated_at,
        error=row.error,
    )


def _project_queue_item(row: ProjectQueueItemRow) -> ProjectQueueItem:
    return ProjectQueueItem(
        item_id=row.item_id,
        batch_id=row.batch_id,
        position=row.position,
        repo=row.repo,
        ticket=row.ticket,
        project_owner=row.project_owner,
        project_title=row.project_title,
        project_item_id=row.project_item_id,
        title=row.title,
        epic_number=row.epic_number,
        epic_title=row.epic_title,
        state=row.state,
        branch=row.branch,
        workflow=row.workflow,
        root_run_id=row.root_run_id,
        current_run_id=row.current_run_id,
        pr_number=row.pr_number,
        last_board_status=row.last_board_status,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _feedback_cycle(row: PrFeedbackCycleRow) -> PrFeedbackCycle:
    return PrFeedbackCycle(
        review_run_id=row.review_run_id,
        repo=row.repo,
        pr_number=row.pr_number,
        ticket=row.ticket,
        branch=row.branch,
        reviewed_head_sha=row.reviewed_head_sha,
        findings_digest=row.findings_digest,
        verdict=row.verdict,
        cycle_number=row.cycle_number,
        max_cycles=row.max_cycles,
        status=row.status,
        update_run_id=row.update_run_id,
        resulting_head_sha=row.resulting_head_sha,
        dispatch_attempts=row.dispatch_attempts,
        error=row.error,
    )


def _enable_sqlite_fk(engine: Engine) -> None:
    """Enable foreign key enforcement for SQLite connections."""

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_conn: Any, conn_record: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def default_db_url(state_dir: Path) -> str:
    """``sqlite+pysqlite:///<state_dir>/quill.db``, creating the directory if needed."""
    state_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{state_dir / 'quill.db'}"


class History:
    """Thin store over a SQLite engine. ``:memory:`` by default (tests); a file in production."""

    def __init__(self, url: str = "sqlite+pysqlite:///:memory:") -> None:
        # An in-memory SQLite database exists per connection, so its sessions must share the one
        # connection held by StaticPool. A file-backed database is different: FastAPI runs these
        # synchronous methods in worker threads, and sharing one sqlite3 connection across those
        # threads can produce intermittent ``InterfaceError: bad parameter or other API misuse``.
        # Let SQLAlchemy use its normal SQLite pool for files so overlapping requests check out
        # independent connections.
        engine_options: dict[str, Any] = {
            "connect_args": {"check_same_thread": False},
        }
        if make_url(url).database in {None, "", ":memory:"}:
            engine_options["poolclass"] = StaticPool
        self._engine = create_engine(url, **engine_options)
        _enable_sqlite_fk(self._engine)
        Base.metadata.create_all(self._engine)
        self._migrate()
        self._backfill_lifetime()

    def _migrate(self) -> None:
        """Apply small, idempotent SQLite additions that ``create_all`` cannot add."""
        columns = {column["name"] for column in inspect(self._engine).get_columns("runs")}
        if "clear_prefix_cache" not in columns:
            with self._engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE runs ADD COLUMN clear_prefix_cache BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
        additions = {
            "last_phase": "ALTER TABLE runs ADD COLUMN last_phase VARCHAR(100)",
            "last_phase_label": "ALTER TABLE runs ADD COLUMN last_phase_label VARCHAR(255)",
            "mode": "ALTER TABLE runs ADD COLUMN mode VARCHAR(20) NOT NULL DEFAULT 'create'",
            "workflow": "ALTER TABLE runs ADD COLUMN workflow VARCHAR(100) NOT NULL DEFAULT 'ticket'",
            "pr_number": "ALTER TABLE runs ADD COLUMN pr_number INTEGER",
            "pr_head_sha": "ALTER TABLE runs ADD COLUMN pr_head_sha VARCHAR(64)",
            "feedback_digest": "ALTER TABLE runs ADD COLUMN feedback_digest VARCHAR(64)",
            "source_run_id": "ALTER TABLE runs ADD COLUMN source_run_id VARCHAR(64)",
            "start_phase": "ALTER TABLE runs ADD COLUMN start_phase VARCHAR(100)",
            "failure_code": "ALTER TABLE runs ADD COLUMN failure_code VARCHAR(100)",
            "failure_label": "ALTER TABLE runs ADD COLUMN failure_label VARCHAR(255)",
        }
        for name, statement in additions.items():
            if name not in columns:
                with self._engine.begin() as connection:
                    connection.execute(text(statement))
        lifetime_columns = {
            column["name"] for column in inspect(self._engine).get_columns("lifetime_runs")
        }
        if "workflow" not in lifetime_columns:
            with self._engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE lifetime_runs ADD COLUMN workflow VARCHAR(100) "
                        "NOT NULL DEFAULT 'ticket'"
                    )
                )
                connection.execute(
                    text(
                        "UPDATE lifetime_runs SET workflow = COALESCE("
                        "(SELECT runs.workflow FROM runs "
                        "WHERE runs.run_id = lifetime_runs.run_id), 'ticket')"
                    )
                )
        for name, statement in {
            "failure_code": "ALTER TABLE lifetime_runs ADD COLUMN failure_code VARCHAR(100)",
            "failure_label": "ALTER TABLE lifetime_runs ADD COLUMN failure_label VARCHAR(255)",
        }.items():
            if name not in lifetime_columns:
                with self._engine.begin() as connection:
                    connection.execute(text(statement))

    def record(self, state: RunState) -> None:
        """Persist (or replace) the summary row for a run."""
        row = RunRow(
            run_id=state.run_id,
            ticket=state.ticket,
            status=state.status.value,
            repo=state.repo or None,
            branch=state.branch,
            mode=state.mode,
            workflow=state.workflow,
            pr_number=state.pr_number,
            pr_head_sha=state.pr_head_sha,
            feedback_digest=state.feedback_digest,
            source_run_id=state.source_run_id,
            start_phase=state.start_phase,
            pr_url=state.pr_url,
            error=state.error,
            failure_code=state.failure_code,
            failure_label=state.failure_label,
            started_at=state.started_at or state.queued_at,
            # 0.0 while the run is still going, so ordering by finish time keeps live runs on top.
            finished_at=0.0 if state.is_active else time.time(),
            clear_prefix_cache=state.clear_prefix_cache,
            last_phase=state.phase,
            last_phase_label=state.phase_label,
        )
        with Session(self._engine) as session:
            session.merge(row)
            if not state.is_active:
                existing = session.get(LifetimeRunRow, state.run_id)
                session.merge(
                    LifetimeRunRow(
                        run_id=state.run_id,
                        ticket=state.ticket,
                        status=state.status.value,
                        repo=state.repo or None,
                        workflow=state.workflow,
                        failure_code=state.failure_code,
                        failure_label=state.failure_label,
                        started_at=row.started_at,
                        finished_at=row.finished_at,
                        breakdown=dict(existing.breakdown) if existing is not None else {},
                        updated_at=time.time(),
                    )
                )
            session.commit()

    def reconcile_orphans(self) -> list[str]:
        """Close out runs left non-terminal by a restart; return their run IDs.

        Nothing is driving them anymore — their thread died with the process — so leaving them
        ``running`` would have ``/runs`` reporting live work that cannot possibly progress.
        """
        stale = [s.value for s in (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.NEEDS_DECISION)]
        with Session(self._engine) as session:
            run_ids = list(session.scalars(select(RunRow.run_id).where(RunRow.status.in_(stale))))
            result = session.execute(
                update(RunRow)
                .where(RunRow.status.in_(stale))
                .values(
                    status=RunStatus.FAILED.value,
                    error="interrupted: the service restarted while this run was in flight",
                    finished_at=time.time(),
                )
            )
            session.commit()
            if isinstance(result, CursorResult) and result.rowcount != len(run_ids):
                return []
            return run_ids

    def recent(
        self,
        limit: int = 50,
        *,
        repo: str | None = None,
        ticket: int | None = None,
        run_status: str | None = None,
    ) -> list[RunRow]:
        with Session(self._engine) as session:
            stmt = select(RunRow).order_by(RunRow.started_at.desc())
            if repo is not None:
                # Older server versions let a run_started event replace owner/name with the
                # configured GitHub URL. Keep those rows discoverable without rewriting the DB.
                stmt = stmt.where(
                    or_(
                        RunRow.repo == repo,
                        RunRow.repo == f"https://github.com/{repo}",
                        RunRow.repo == f"https://github.com/{repo}.git",
                        RunRow.repo == f"git@github.com:{repo}.git",
                    )
                )
            if ticket is not None:
                stmt = stmt.where(RunRow.ticket == ticket)
            if run_status is not None:
                stmt = stmt.where(RunRow.status == run_status)
            return list(session.scalars(stmt.limit(limit)))

    def get(self, run_id: str) -> RunRow | None:
        with Session(self._engine) as session:
            return session.get(RunRow, run_id)

    def has_pr_review(self, repo: str, pr_number: int, head_sha: str) -> bool:
        """Whether this exact PR revision has already been admitted for review."""
        with Session(self._engine) as session:
            return (
                session.scalar(
                    select(RunRow.run_id)
                    .where(
                        RunRow.repo == repo,
                        RunRow.mode == "review",
                        RunRow.pr_number == pr_number,
                        RunRow.pr_head_sha == head_sha,
                    )
                    .limit(1)
                )
                is not None
            )

    def record_pr_feedback_result(
        self,
        *,
        review_run_id: str,
        repo: str,
        pr_number: int,
        ticket: int,
        branch: str,
        reviewed_head_sha: str,
        findings_digest: str,
        verdict: str,
        max_cycles: int,
    ) -> PrFeedbackCycle:
        """Idempotently persist one validated review result before any update is queued."""
        with Session(self._engine) as session:
            existing = session.scalar(
                select(PrFeedbackCycleRow).where(
                    PrFeedbackCycleRow.repo == repo,
                    PrFeedbackCycleRow.pr_number == pr_number,
                    PrFeedbackCycleRow.reviewed_head_sha == reviewed_head_sha,
                )
            )
            if existing is not None:
                return _feedback_cycle(existing)
            prior_blocks = int(
                session.scalar(
                    select(func.count())
                    .select_from(PrFeedbackCycleRow)
                    .where(
                        PrFeedbackCycleRow.repo == repo,
                        PrFeedbackCycleRow.pr_number == pr_number,
                        PrFeedbackCycleRow.verdict == "BLOCK",
                    )
                )
                or 0
            )
            cycle_number = prior_blocks + 1
            status = (
                "pass_complete"
                if verdict == "PASS"
                else "cycle_limit_reached"
                if cycle_number > max_cycles
                else "update_pending"
            )
            now = time.time()
            row = PrFeedbackCycleRow(
                review_run_id=review_run_id,
                repo=repo,
                pr_number=pr_number,
                ticket=ticket,
                branch=branch,
                reviewed_head_sha=reviewed_head_sha,
                findings_digest=findings_digest,
                verdict=verdict,
                cycle_number=cycle_number,
                max_cycles=max_cycles,
                status=status,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return _feedback_cycle(row)

    def attach_pr_feedback_update(self, review_run_id: str, update_run_id: str) -> bool:
        """Claim one pending cycle for an update run; duplicate dispatches lose the claim."""
        with Session(self._engine) as session:
            row = session.get(PrFeedbackCycleRow, review_run_id)
            if row is None or row.status != "update_pending" or row.update_run_id is not None:
                return False
            row.update_run_id = update_run_id
            row.status = "update_queued"
            row.dispatch_attempts += 1
            row.updated_at = time.time()
            session.commit()
            return True

    def feedback_cycle_for_update(self, update_run_id: str) -> PrFeedbackCycle | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(PrFeedbackCycleRow).where(PrFeedbackCycleRow.update_run_id == update_run_id)
            )
            return _feedback_cycle(row) if row is not None else None

    def finish_pr_feedback_update(
        self,
        update_run_id: str,
        *,
        status: str,
        resulting_head_sha: str | None = None,
        error: str | None = None,
    ) -> None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(PrFeedbackCycleRow).where(PrFeedbackCycleRow.update_run_id == update_run_id)
            )
            if row is None:
                return
            row.status = status
            row.resulting_head_sha = resulting_head_sha
            row.error = error
            row.updated_at = time.time()
            session.commit()

    def finish_pr_feedback_cycle(
        self,
        review_run_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        """Close a cycle that cannot or must not dispatch an update."""
        with Session(self._engine) as session:
            row = session.get(PrFeedbackCycleRow, review_run_id)
            if row is None:
                return
            row.status = status
            row.error = error
            row.updated_at = time.time()
            session.commit()

    def recoverable_pr_feedback_cycles(self) -> list[PrFeedbackCycle]:
        """Return pending cycles and reset one deployment-interrupted dispatch for replay."""
        with Session(self._engine) as session:
            queued = list(
                session.scalars(
                    select(PrFeedbackCycleRow).where(PrFeedbackCycleRow.status == "update_queued")
                )
            )
            for cycle in queued:
                run = session.get(RunRow, cycle.update_run_id) if cycle.update_run_id else None
                if (
                    run is None
                    or (
                        run.status == RunStatus.FAILED.value
                        and (run.error or "").startswith("interrupted:")
                    )
                ) and cycle.dispatch_attempts < 2:
                    cycle.status = "update_pending"
                    cycle.update_run_id = None
                    cycle.error = "replaying once after service interruption"
                    cycle.updated_at = time.time()
                elif run is None:
                    cycle.status = "update_failed"
                    cycle.error = "automatic update dispatch was lost after its replay allowance"
                    cycle.updated_at = time.time()
                elif run is not None and run.status in {
                    RunStatus.FAILED.value,
                    RunStatus.HALTED.value,
                }:
                    cycle.status = "update_failed"
                    cycle.error = run.error
                    cycle.updated_at = time.time()
            session.commit()
            pending = list(
                session.scalars(
                    select(PrFeedbackCycleRow).where(PrFeedbackCycleRow.status == "update_pending")
                )
            )
            return [_feedback_cycle(row) for row in pending]

    def create_project_queue_batch(
        self,
        *,
        batch_id: str,
        repo: str,
        source: str,
        items: list[ProjectQueueItemSpec],
        created_at: float | None = None,
    ) -> ProjectQueueBatch:
        """Create one FIFO batch, returning the existing batch for an identical retry.

        Ticket order is canonicalized numerically. A nonterminal ``(repo, ticket)`` may belong to
        only one batch. Completed and removed tickets may be submitted again in a later batch.
        """
        batch_id = batch_id.strip()
        repo = repo.strip()
        if not batch_id or len(batch_id) > 64:
            raise ValueError("project queue batch_id must contain 1-64 characters")
        if not repo:
            raise ValueError("project queue repo must not be blank")
        if source not in {"ui", "board"}:
            raise ValueError("project queue source must be 'ui' or 'board'")
        ordered = sorted(items, key=lambda item: item.ticket)
        if any(item.ticket <= 0 for item in ordered):
            raise ValueError("project queue ticket numbers must be positive")
        if len({item.ticket for item in ordered}) != len(ordered):
            raise ValueError("project queue batch contains duplicate tickets")
        for item in ordered:
            if not all(
                value.strip()
                for value in (
                    item.project_owner,
                    item.project_title,
                    item.project_item_id,
                    item.title,
                    item.branch,
                    item.workflow,
                )
            ):
                raise ValueError("project queue item metadata must not contain blank identifiers")
        expected = [
            (
                spec.ticket,
                spec.project_owner,
                spec.project_title,
                spec.project_item_id,
                spec.title,
                spec.branch,
                spec.epic_number,
                spec.epic_title,
                spec.workflow,
            )
            for spec in ordered
        ]

        now = time.time() if created_at is None else created_at
        with Session(self._engine) as session:

            def existing_batch() -> ProjectQueueBatch | None:
                existing = session.scalar(
                    select(ProjectQueueBatchRow).where(ProjectQueueBatchRow.batch_id == batch_id)
                )
                if existing is None:
                    return None
                existing_items = list(
                    session.scalars(
                        select(ProjectQueueItemRow)
                        .where(ProjectQueueItemRow.batch_id == batch_id)
                        .order_by(ProjectQueueItemRow.position)
                    )
                )
                actual = [
                    (
                        item.ticket,
                        item.project_owner,
                        item.project_title,
                        item.project_item_id,
                        item.title,
                        item.branch,
                        item.epic_number,
                        item.epic_title,
                        item.workflow,
                    )
                    for item in existing_items
                ]
                if existing.repo != repo or existing.source != source or actual != expected:
                    raise ValueError(f"project queue batch {batch_id!r} already has different data")
                return _project_queue_batch(existing)

            if existing := existing_batch():
                return existing

            duplicate = session.scalar(
                select(ProjectQueueItemRow).where(
                    ProjectQueueItemRow.repo == repo,
                    ProjectQueueItemRow.ticket.in_([item.ticket for item in ordered]),
                    ProjectQueueItemRow.state.in_(PROJECT_QUEUE_ACTIVE_STATES),
                )
            )
            if duplicate is not None:
                if duplicate.batch_id == batch_id:
                    # Another connection committed this identical request between our initial
                    # batch lookup and active-ticket lookup. End the stale read transaction and
                    # validate the winner using a fresh SQLite snapshot.
                    session.rollback()
                    if existing := existing_batch():
                        return existing
                raise ValueError(
                    f"{repo}#{duplicate.ticket} is already active in project queue batch "
                    f"{duplicate.batch_id}"
                )

            batch = ProjectQueueBatchRow(
                batch_id=batch_id,
                repo=repo,
                source=source,
                state="stabilizing" if ordered else "completed",
                created_at=now,
                completed_at=None if ordered else now,
                updated_at=now,
            )
            session.add(batch)
            session.add_all(
                [
                    ProjectQueueItemRow(
                        item_id=f"{batch_id}:{position}",
                        batch_id=batch_id,
                        position=position,
                        repo=repo,
                        ticket=spec.ticket,
                        project_owner=spec.project_owner,
                        project_title=spec.project_title,
                        project_item_id=spec.project_item_id,
                        title=spec.title,
                        epic_number=spec.epic_number,
                        epic_title=spec.epic_title,
                        state="stabilizing",
                        branch=spec.branch,
                        workflow=spec.workflow,
                        last_board_status=spec.last_board_status,
                        created_at=now,
                        updated_at=now,
                    )
                    for position, spec in enumerate(ordered, start=1)
                ]
            )
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if existing := existing_batch():
                    return existing
                raise ValueError("project queue batch conflicts with active durable state") from exc
            return _project_queue_batch(batch)

    def list_project_queue_batches(self, *, active_only: bool = True) -> list[ProjectQueueBatch]:
        """Return batches in durable FIFO order."""
        with Session(self._engine) as session:
            stmt = select(ProjectQueueBatchRow).order_by(ProjectQueueBatchRow.position)
            if active_only:
                stmt = stmt.where(ProjectQueueBatchRow.state.in_(PROJECT_QUEUE_ACTIVE_STATES))
            return [_project_queue_batch(row) for row in session.scalars(stmt)]

    def list_project_queue_items(
        self,
        *,
        batch_id: str | None = None,
        active_only: bool = True,
    ) -> list[ProjectQueueItem]:
        """Return tickets by batch FIFO position, then numeric in-batch position."""
        with Session(self._engine) as session:
            stmt = (
                select(ProjectQueueItemRow)
                .join(
                    ProjectQueueBatchRow,
                    ProjectQueueBatchRow.batch_id == ProjectQueueItemRow.batch_id,
                )
                .order_by(ProjectQueueBatchRow.position, ProjectQueueItemRow.position)
            )
            if batch_id is not None:
                stmt = stmt.where(ProjectQueueItemRow.batch_id == batch_id)
            if active_only:
                stmt = stmt.where(ProjectQueueItemRow.state.in_(PROJECT_QUEUE_ACTIVE_STATES))
            return [_project_queue_item(row) for row in session.scalars(stmt)]

    def find_active_project_queue_item(self, repo: str, ticket: int) -> ProjectQueueItem | None:
        """Return the queue owner for an active repository/ticket identity."""
        with Session(self._engine) as session:
            row = session.scalar(
                select(ProjectQueueItemRow).where(
                    ProjectQueueItemRow.repo == repo,
                    ProjectQueueItemRow.ticket == ticket,
                    ProjectQueueItemRow.state.in_(PROJECT_QUEUE_ACTIVE_STATES),
                )
            )
            return _project_queue_item(row) if row is not None else None

    def project_queue_item_for_run(self, run_id: str) -> ProjectQueueItem | None:
        """Return the active queue item currently attached to ``run_id``."""
        with Session(self._engine) as session:
            row = session.scalar(
                select(ProjectQueueItemRow).where(
                    or_(
                        ProjectQueueItemRow.current_run_id == run_id,
                        ProjectQueueItemRow.root_run_id == run_id,
                    ),
                    ProjectQueueItemRow.state.in_(PROJECT_QUEUE_ACTIVE_STATES),
                )
            )
            return _project_queue_item(row) if row is not None else None

    @staticmethod
    def _refresh_project_queue_batch(
        session: Session, batch_id: str, *, now: float
    ) -> ProjectQueueBatchRow | None:
        batch = session.scalar(
            select(ProjectQueueBatchRow).where(ProjectQueueBatchRow.batch_id == batch_id)
        )
        if batch is None:
            return None
        items = list(
            session.scalars(
                select(ProjectQueueItemRow).where(ProjectQueueItemRow.batch_id == batch_id)
            )
        )
        states = {item.state for item in items}
        if not items or states.issubset(set(PROJECT_QUEUE_TERMINAL_STATES)):
            batch.state = "completed"
            batch.completed_at = batch.completed_at or now
            batch.error = None
        elif "paused" in states:
            batch.state = "paused"
            batch.error = next((item.error for item in items if item.state == "paused"), None)
        elif "running" in states:
            batch.state = "running"
            batch.error = None
        elif "waiting_pr" in states:
            batch.state = "waiting_pr"
            batch.error = None
        elif "stabilizing" in states:
            batch.state = "stabilizing"
            batch.error = None
        else:
            batch.state = "pending"
            batch.error = None
        batch.updated_at = now
        return batch

    def stabilize_project_queue_batch(
        self, batch_id: str, *, stabilized_at: float | None = None
    ) -> ProjectQueueBatch | None:
        """Mark a stable observed batch eligible for FIFO claiming; repeated calls are safe."""
        now = time.time() if stabilized_at is None else stabilized_at
        with Session(self._engine) as session:
            batch = session.scalar(
                select(ProjectQueueBatchRow).where(ProjectQueueBatchRow.batch_id == batch_id)
            )
            if batch is None:
                return None
            if batch.state == "stabilizing":
                session.execute(
                    update(ProjectQueueItemRow)
                    .where(
                        ProjectQueueItemRow.batch_id == batch_id,
                        ProjectQueueItemRow.state == "stabilizing",
                    )
                    .values(state="pending", updated_at=now)
                )
                batch.observed_at = batch.observed_at or now
                batch.stabilized_at = now
                batch.updated_at = now
                session.flush()
                batch = self._refresh_project_queue_batch(session, batch_id, now=now) or batch
            session.commit()
            return _project_queue_batch(batch)

    def observe_project_queue_batch(
        self, batch_id: str, *, observed_at: float | None = None
    ) -> ProjectQueueBatch | None:
        """Record the latest changed board observation that restarts stabilization."""
        now = time.time() if observed_at is None else observed_at
        with Session(self._engine) as session:
            batch = session.scalar(
                select(ProjectQueueBatchRow).where(ProjectQueueBatchRow.batch_id == batch_id)
            )
            if batch is None:
                return None
            if batch.state == "stabilizing":
                batch.observed_at = now
                batch.updated_at = now
                session.commit()
            return _project_queue_batch(batch)

    def claim_project_queue_head(
        self, *, started_at: float | None = None
    ) -> ProjectQueueItem | None:
        """Atomically claim only the first ticket in the oldest unfinished FIFO batch."""
        now = time.time() if started_at is None else started_at
        with Session(self._engine) as session:
            batch = session.scalar(
                select(ProjectQueueBatchRow)
                .where(ProjectQueueBatchRow.state.in_(PROJECT_QUEUE_ACTIVE_STATES))
                .order_by(ProjectQueueBatchRow.position)
                .limit(1)
            )
            if batch is None:
                return None
            batch = self._refresh_project_queue_batch(session, batch.batch_id, now=now) or batch
            session.flush()
            if batch.state != "pending":
                session.commit()
                return None
            item = session.scalar(
                select(ProjectQueueItemRow)
                .where(
                    ProjectQueueItemRow.batch_id == batch.batch_id,
                    ProjectQueueItemRow.state == "pending",
                )
                .order_by(ProjectQueueItemRow.position)
                .limit(1)
            )
            if item is None:
                self._refresh_project_queue_batch(session, batch.batch_id, now=now)
                session.commit()
                return None
            result = session.execute(
                update(ProjectQueueItemRow)
                .where(
                    ProjectQueueItemRow.item_id == item.item_id,
                    ProjectQueueItemRow.state == "pending",
                    select(ProjectQueueBatchRow.batch_id)
                    .where(
                        ProjectQueueBatchRow.batch_id == item.batch_id,
                        ProjectQueueBatchRow.state == "pending",
                    )
                    .exists(),
                )
                .values(state="running", started_at=now, updated_at=now, error=None)
            )
            if not isinstance(result, CursorResult) or result.rowcount != 1:
                session.rollback()
                return None
            batch.state = "running"
            batch.started_at = batch.started_at or now
            batch.updated_at = now
            session.commit()
            claimed = session.get(ProjectQueueItemRow, item.item_id)
            return _project_queue_item(claimed) if claimed is not None else None

    def attach_project_queue_run(
        self,
        item_id: str,
        run_id: str,
        *,
        root: bool = False,
        updated_at: float | None = None,
    ) -> bool:
        """Attach a root, review, update, or restart run and mark the ticket running."""
        now = time.time() if updated_at is None else updated_at
        with Session(self._engine) as session:
            item = session.get(ProjectQueueItemRow, item_id)
            if item is None or item.state not in {"running", "waiting_pr", "paused"}:
                return False
            if root and item.root_run_id not in {None, run_id}:
                return False
            if root:
                item.root_run_id = run_id
            item.current_run_id = run_id
            item.state = "running"
            item.started_at = item.started_at or now
            item.updated_at = now
            item.error = None
            self._refresh_project_queue_batch(session, item.batch_id, now=now)
            session.commit()
            return True

    def attach_project_queue_pr(
        self,
        item_id: str,
        pr_number: int,
        *,
        updated_at: float | None = None,
    ) -> bool:
        """Record the exact PR and mark the ticket as waiting for its merge chain."""
        if pr_number <= 0:
            raise ValueError("project queue PR number must be positive")
        now = time.time() if updated_at is None else updated_at
        with Session(self._engine) as session:
            item = session.get(ProjectQueueItemRow, item_id)
            if item is None or item.state not in {"running", "waiting_pr", "paused"}:
                return False
            if item.pr_number not in {None, pr_number}:
                return False
            item.pr_number = pr_number
            item.state = "waiting_pr"
            item.updated_at = now
            item.error = None
            self._refresh_project_queue_batch(session, item.batch_id, now=now)
            session.commit()
            return True

    def transition_project_queue_item(
        self,
        item_id: str,
        *,
        state: str,
        expected_states: tuple[str, ...],
        error: str | None = None,
        updated_at: float | None = None,
    ) -> bool:
        """Compare-and-set a nonterminal item state for scheduler reconciliation."""
        if state not in PROJECT_QUEUE_ACTIVE_STATES:
            raise ValueError("use complete/remove methods for terminal project queue states")
        if not expected_states or any(
            value not in PROJECT_QUEUE_ACTIVE_STATES for value in expected_states
        ):
            raise ValueError("expected project queue states must be nonterminal")
        now = time.time() if updated_at is None else updated_at
        with Session(self._engine) as session:
            item = session.get(ProjectQueueItemRow, item_id)
            if item is None or item.state not in expected_states:
                return False
            item.state = state
            item.error = error
            item.updated_at = now
            self._refresh_project_queue_batch(session, item.batch_id, now=now)
            session.commit()
            return True

    def pause_project_queue_item(
        self,
        item_id: str,
        *,
        error: str,
        run_id: str | None = None,
        updated_at: float | None = None,
    ) -> bool:
        """Pause the queue head after a failed, halted, or unsafe terminal condition."""
        now = time.time() if updated_at is None else updated_at
        with Session(self._engine) as session:
            item = session.get(ProjectQueueItemRow, item_id)
            if item is None or item.state not in {"running", "waiting_pr", "paused"}:
                return False
            item.state = "paused"
            item.error = error
            item.current_run_id = run_id or item.current_run_id
            item.updated_at = now
            self._refresh_project_queue_batch(session, item.batch_id, now=now)
            session.commit()
            return True

    def complete_project_queue_item(
        self,
        item_id: str,
        *,
        pr_number: int,
        completed_at: float | None = None,
    ) -> bool:
        """Complete a ticket after its caller independently verifies the exact PR merge."""
        if pr_number <= 0:
            raise ValueError("project queue PR number must be positive")
        now = time.time() if completed_at is None else completed_at
        with Session(self._engine) as session:
            item = session.get(ProjectQueueItemRow, item_id)
            if item is None:
                return False
            if item.state == "completed":
                return item.pr_number == pr_number
            if item.state not in {"running", "waiting_pr", "paused"}:
                return False
            if item.pr_number not in {None, pr_number}:
                return False
            item.pr_number = pr_number
            item.state = "completed"
            item.error = None
            item.updated_at = now
            item.completed_at = now
            self._refresh_project_queue_batch(session, item.batch_id, now=now)
            session.commit()
            return True

    def remove_pending_project_queue_item(
        self, item_id: str, *, removed_at: float | None = None
    ) -> bool:
        """Remove only work that has not started; active execution is never cancelled here."""
        now = time.time() if removed_at is None else removed_at
        with Session(self._engine) as session:
            item = session.get(ProjectQueueItemRow, item_id)
            if item is None:
                return False
            if item.state == "removed":
                return True
            if item.state not in {"stabilizing", "pending"}:
                return False
            item.state = "removed"
            item.updated_at = now
            item.completed_at = now
            self._refresh_project_queue_batch(session, item.batch_id, now=now)
            session.commit()
            return True

    def recover_project_queue(self) -> list[ProjectQueueItem]:
        """Normalize batch summaries after restart without unlocking in-flight work."""
        now = time.time()
        with Session(self._engine) as session:
            batch_ids = list(
                session.scalars(
                    select(ProjectQueueBatchRow.batch_id).where(
                        ProjectQueueBatchRow.state.in_(PROJECT_QUEUE_ACTIVE_STATES)
                    )
                )
            )
            for batch_id in batch_ids:
                self._refresh_project_queue_batch(session, batch_id, now=now)
            session.commit()
        return self.list_project_queue_items(active_only=True)

    def untracked_completed_pr_reviews(self) -> list[RunRow]:
        """Completed review runs whose validated result has not entered the outbox yet."""
        with Session(self._engine) as session:
            tracked = select(PrFeedbackCycleRow.review_run_id)
            return list(
                session.scalars(
                    select(RunRow).where(
                        RunRow.mode == "review",
                        RunRow.workflow == "pr_review",
                        RunRow.status == RunStatus.DONE.value,
                        RunRow.run_id.not_in(tracked),
                    )
                )
            )

    def unreconciled_completed_pr_feedback_updates(self) -> list[RunRow]:
        """Completed automatic updates whose resulting remote head is not recorded yet."""
        with Session(self._engine) as session:
            return list(
                session.scalars(
                    select(RunRow)
                    .join(
                        PrFeedbackCycleRow,
                        PrFeedbackCycleRow.update_run_id == RunRow.run_id,
                    )
                    .where(
                        PrFeedbackCycleRow.status == "update_queued",
                        RunRow.status == RunStatus.DONE.value,
                    )
                )
            )

    def get_breakdown(self, run_id: str) -> dict[str, Any] | None:
        with Session(self._engine) as session:
            row = session.get(BreakdownRow, run_id)
            return dict(row.data) if row is not None else None

    def delete_many(self, run_ids: list[str]) -> None:
        """Delete retained details while intentionally preserving permanent accounting."""
        with Session(self._engine) as session:
            session.execute(delete(BreakdownRow).where(BreakdownRow.run_id.in_(run_ids)))
            session.execute(delete(RunRow).where(RunRow.run_id.in_(run_ids)))
            session.commit()

    def record_breakdown(self, run_id: str, data: dict[str, Any], schema_version: int) -> None:
        with Session(self._engine) as session:
            session.merge(
                BreakdownRow(
                    run_id=run_id,
                    schema_version=schema_version,
                    data=data,
                    updated_at=time.time(),
                )
            )
            run = session.get(RunRow, run_id)
            if run is not None and run.status in {
                RunStatus.DONE.value,
                RunStatus.FAILED.value,
                RunStatus.HALTED.value,
            }:
                session.merge(
                    LifetimeRunRow(
                        run_id=run.run_id,
                        ticket=run.ticket,
                        status=run.status,
                        repo=run.repo,
                        workflow=run.workflow,
                        failure_code=run.failure_code,
                        failure_label=run.failure_label,
                        started_at=run.started_at,
                        finished_at=run.finished_at,
                        breakdown=data,
                        updated_at=time.time(),
                    )
                )
            session.commit()

    def _backfill_lifetime(self) -> None:
        """Seed the permanent ledger from terminal details retained before this table existed."""
        terminal = [RunStatus.DONE.value, RunStatus.FAILED.value, RunStatus.HALTED.value]
        with Session(self._engine) as session:
            existing = set(session.scalars(select(LifetimeRunRow.run_id)))
            runs = list(session.scalars(select(RunRow).where(RunRow.status.in_(terminal))))
            for run in runs:
                if run.run_id in existing:
                    continue
                cached = session.get(BreakdownRow, run.run_id)
                session.add(
                    LifetimeRunRow(
                        run_id=run.run_id,
                        ticket=run.ticket,
                        status=run.status,
                        repo=run.repo,
                        workflow=run.workflow,
                        failure_code=run.failure_code,
                        failure_label=run.failure_label,
                        started_at=run.started_at,
                        finished_at=run.finished_at,
                        breakdown=dict(cached.data) if cached is not None else {},
                        updated_at=time.time(),
                    )
                )
            session.commit()

    def iter_all(self) -> Iterator[RunRow]:
        yield from self.recent(limit=1000)

    def lifetime_rows(self) -> list[LifetimeRunRow]:
        """Permanent run accounting; retained-detail deletion never removes these rows."""
        with Session(self._engine) as session:
            return list(session.scalars(select(LifetimeRunRow)))

    def lifetime_breakdowns(self) -> list[dict[str, Any]]:
        """Permanent per-run telemetry snapshots used by lifetime usage aggregation."""
        with Session(self._engine) as session:
            return [dict(row.breakdown) for row in session.scalars(select(LifetimeRunRow))]

    def get_setting(self, key: str) -> dict[str, Any] | None:
        """Return one application setting document."""
        with Session(self._engine) as session:
            row = session.get(AppSettingRow, key)
            return dict(row.value) if row is not None else None

    def set_setting(self, key: str, value: dict[str, Any]) -> None:
        """Persist one complete application setting document."""
        with Session(self._engine) as session:
            session.merge(AppSettingRow(key=key, value=value, updated_at=time.time()))
            session.commit()
