"""Service configuration, read from the environment (server milestone D2).

Everything the service needs to know about *this machine* — where checkouts, personas, skills and
run artifacts live, and what to bind — comes from here. Nothing is per-repo: that arrives with each
request.

Read at startup and passed around explicitly rather than consulted through the environment at use
sites, so a test can construct a `Settings` pointing anywhere without mutating global state.
"""

from __future__ import annotations

import os
import math
from dataclasses import dataclass
from pathlib import Path

from quill.config import default_personas_root, default_state_dir

#: Fallback port when ``QUILL_PORT`` is unset. Deliberately not 8000, which collides with the
#: vLLM default. Any real deployment sets ``QUILL_PORT`` in its environment file rather than
#: relying on this — machine-specific ports do not belong in source or in documentation.
DEFAULT_PORT = 8002
DEFAULT_HOST = "0.0.0.0"
#: Fallback commit authorship for agent-made commits. Deliberately generic: a real deployment
#: sets ``QUILL_GIT_AUTHOR_NAME`` / ``QUILL_GIT_AUTHOR_EMAIL`` to its own automation account.
#: A GitHub ``ID+login@users.noreply.github.com`` address attributes commits to that account
#: without publishing a real mailbox.
DEFAULT_GIT_AUTHOR_NAME = "quill"
DEFAULT_GIT_AUTHOR_EMAIL = "quill@users.noreply.github.com"
#: Where skills are discovered. Defaults to pi's own directory so the service sees exactly the
#: skills the agent CLI would load if you ran it by hand.
DEFAULT_SKILLS_DIR = Path.home() / ".pi" / "agent" / "skills"
PACKAGED_WEB_DIR = Path(__file__).with_name("web")


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved machine-level configuration."""

    state_dir: Path
    workspace_root: Path
    runs_root: Path
    personas_root: Path
    skills_root: Path
    db_url: str
    vllm_url: str
    web_root: Path = PACKAGED_WEB_DIR
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    #: Authorship stamped on every commit an agent phase makes, set repo-locally on each
    #: workspace checkout. Without this a checkout inherits the service user's global git
    #: identity, which publishes a human's personal address on machine-generated commits.
    #: Point it at a dedicated automation account.
    git_author_name: str = DEFAULT_GIT_AUTHOR_NAME
    git_author_email: str = DEFAULT_GIT_AUTHOR_EMAIL
    telemetry_interval_s: float = 0.125
    pr_watch_enabled: bool = True
    pr_watch_interval_s: float = 15.0
    pr_feedback_loop_enabled: bool = True
    pr_feedback_loop_max_cycles: int = 5
    project_queue_watch_enabled: bool = True
    project_queue_watch_interval_s: float = 5.0
    #: USD per 1M tokens for a **local** model server (llamacpp/vllm), where the real cost is
    #: electricity, not an API bill. Derived from this machine's wall power, throughput, and local
    #: electricity rate (default: ~$0.043/1M). Hosted models ignore this and use the CLI's reported
    #: cost instead.
    usd_per_1m_tokens: float = 0.043
    #: Machine-level launcher for an interactive model switch. Deliberately server config, not a
    #: repository's `[runner.vllm]` command — which model is resident is a property of this box,
    #: not of whichever repo happens to be selected. Each unit declares `Conflicts=` against its
    #: siblings, so starting one stops the rest. Unloading the resident model uses the matching
    #: machine-level stop command.
    vllm_switch_command: tuple[str, ...] = ("sudo", "systemctl", "start")
    vllm_stop_command: tuple[str, ...] = ("sudo", "systemctl", "stop")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        """Build settings from ``$QUILL_*``, falling back to the machine defaults."""
        source = env if env is not None else dict(os.environ)
        state_dir = Path(source.get("QUILL_STATE_DIR") or default_state_dir())
        vllm_url = (source.get("QUILL_VLLM_URL") or "").strip()
        if not vllm_url:
            raise ValueError(
                "QUILL_VLLM_URL must be set to the machine's vLLM base URL "
                '(for example, "http://vllm.example:8000")'
            )
        return cls(
            state_dir=state_dir,
            workspace_root=Path(source.get("QUILL_WORKSPACE_ROOT") or state_dir / "workspaces"),
            runs_root=Path(source.get("QUILL_RUNS_ROOT") or state_dir / "runs"),
            personas_root=Path(source.get("QUILL_PERSONAS_DIR") or default_personas_root()),
            skills_root=Path(source.get("QUILL_SKILLS_DIR") or DEFAULT_SKILLS_DIR),
            db_url=source.get("QUILL_DB_URL") or f"sqlite+pysqlite:///{state_dir / 'quill.db'}",
            vllm_url=vllm_url,
            web_root=Path(source.get("QUILL_WEB_ROOT") or PACKAGED_WEB_DIR),
            host=source.get("QUILL_HOST") or DEFAULT_HOST,
            port=_int(source.get("QUILL_PORT"), DEFAULT_PORT),
            git_author_name=(source.get("QUILL_GIT_AUTHOR_NAME") or "").strip()
            or DEFAULT_GIT_AUTHOR_NAME,
            git_author_email=(source.get("QUILL_GIT_AUTHOR_EMAIL") or "").strip()
            or DEFAULT_GIT_AUTHOR_EMAIL,
            telemetry_interval_s=_float(
                source.get("QUILL_TELEMETRY_INTERVAL_SECONDS"), 0.125, 0.125, 10.0
            ),
            pr_watch_enabled=_bool(source.get("QUILL_PR_WATCH_ENABLED"), True),
            pr_watch_interval_s=_float(
                source.get("QUILL_PR_WATCH_INTERVAL_SECONDS"), 15.0, 5.0, 3600.0
            ),
            pr_feedback_loop_enabled=_bool(source.get("QUILL_PR_FEEDBACK_LOOP_ENABLED"), True),
            pr_feedback_loop_max_cycles=_bounded_int(
                source.get("QUILL_PR_FEEDBACK_LOOP_MAX_CYCLES"), 5, 1, 20
            ),
            project_queue_watch_enabled=_bool(
                source.get("QUILL_PROJECT_QUEUE_WATCH_ENABLED"), True
            ),
            project_queue_watch_interval_s=_float(
                source.get("QUILL_PROJECT_QUEUE_WATCH_INTERVAL_SECONDS"), 5.0, 1.0, 300.0
            ),
            vllm_switch_command=tuple(
                (source.get("QUILL_VLLM_SWITCH_COMMAND") or "sudo systemctl start").split()
            ),
            vllm_stop_command=tuple(
                (source.get("QUILL_VLLM_STOP_COMMAND") or "sudo systemctl stop").split()
            ),
            usd_per_1m_tokens=_float(source.get("QUILL_USD_PER_1M_TOKENS"), 0.043, 0.0, 1000.0),
        )

    def ensure_dirs(self) -> None:
        """Create the directories the service writes to. Read-only roots are left alone: a missing
        persona library is a setup problem to report, not something to paper over with an empty
        directory that makes every config fail validation instead."""
        for path in (self.state_dir, self.workspace_root, self.runs_root):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def memory_root(self) -> Path:
        """Machine-wide repository blocker-memory root."""
        return self.state_dir / "memory"


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _bounded_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, _int(value, default)))


def _float(value: str | None, default: float, minimum: float, maximum: float) -> float:
    try:
        result = float(value) if value else default
    except ValueError:
        return default
    if not math.isfinite(result):
        return default
    return max(minimum, min(maximum, result))


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}
