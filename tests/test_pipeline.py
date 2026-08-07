"""Pipeline orchestration tests (ticket #33) — config-driven engine + git wiring."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from quill import config as cfg
from quill import events
from quill.bootstrap import init_config, seed_personas
from quill.events import Event
from quill.git_ops import GitOps
from quill.phases import Spawner
from quill.pipeline import PipelineDeps, make_run_id, run_pipeline

# The task line names the ABSOLUTE path the worker must write; the spawn fake writes there to
# mirror a real worker. The engine injects absolute paths so the worker can't misresolve them.
_ARTIFACT_RE = re.compile(
    r"[Ww]rite (?:your artifact|your findings|your natural review notes|the reconciled review|"
    r"the reconciled natural review notes) to (\S+\.(?:md|json))"
)
_PROJECTION_RE = re.compile(r"write one JSON payload to (\S+\.json)")


@pytest.fixture(autouse=True)
def _stub_git_detect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "_detect_repo", lambda _d: "me/proj")
    monkeypatch.setattr(cfg, "_detect_default_branch", lambda _d: "main")


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    """A repo whose default config has build/runner filled in, plus a seeded persona library."""
    config_file = init_config(tmp_path)
    seed_personas()
    text = config_file.read_text(encoding="utf-8")
    text = text.replace('command = ""', 'command = "make"')
    text = text.replace('test    = ""', 'test    = "make test"')
    config_file.write_text(text, encoding="utf-8")
    return tmp_path


class FakeLoader:
    def __init__(self) -> None:
        self.loaded: list[str] = []

    def load(self, preset: str, timeout: float = 180) -> None:
        self.loaded.append(preset)

    def unload_all(self) -> None: ...


def _write_artifact_for(repo_dir: Path, prompt: str, receipt: str) -> None:
    """Mirror a real worker: on a DONE/PASS receipt, write the artifact to the absolute path the
    prompt names. The engine verifies this file exists before advancing."""
    if not receipt.startswith(("DONE", "PASS", "BLOCK")):
        return
    art_m = _ARTIFACT_RE.search(prompt)
    if art_m:
        path = Path(art_m.group(1).rstrip("."))
        path.parent.mkdir(parents=True, exist_ok=True)
        if '"schema_version":1' in prompt:
            findings = []
            if receipt.startswith("BLOCK"):
                findings = [
                    {
                        "id": "F1",
                        "severity": "MAJOR",
                        "status": "OPEN",
                        "title": "Scripted blocker",
                        "requirement": "Complete the required behavior",
                        "evidence": "test fixture",
                        "failure_scenario": "The requirement remains unmet",
                        "required_outcome": "Satisfy the requirement",
                    }
                ]
            elif receipt.startswith("PASS"):
                findings = [
                    {
                        "id": "F1",
                        "severity": "MAJOR",
                        "status": "RESOLVED",
                        "title": "Scripted blocker",
                        "requirement": "Complete the required behavior",
                        "evidence": "test fixture confirms resolution",
                        "failure_scenario": "The requirement remains unmet",
                        "required_outcome": "Satisfy the requirement",
                    }
                ]
            path.write_text(
                json.dumps({"schema_version": 1, "findings": findings}), encoding="utf-8"
            )
        else:
            path.write_text("artifact body", encoding="utf-8")


def _spawn_returning(
    repo_dir: Path, receipts: dict[str, str], default: str = "DONE: ok"
) -> Spawner:
    """Spawn fake keyed by agent (= phase id), wrapping the receipt in opencode's JSON shape and
    writing the phase's artifact into the run dir so the engine's existence check passes."""

    latest: dict[str, str] = {}

    def spawn(
        agent: str,
        preset: str,
        prompt: str,
        *,
        timeout: float,
        stream_path: Path,
        on_tool: object = None,
        on_usage: object = None,
        abort_reason: object = None,
    ) -> str:
        receipt = receipts.get(agent, default)
        latest[agent] = receipt
        _write_artifact_for(repo_dir, prompt, receipt)
        return json.dumps({"type": "text", "text": receipt})

    def repair(
        agent: str,
        preset: str,
        prompt: str,
        *,
        timeout: float,
        stream_path: Path,
        on_tool: object = None,
        on_usage: object = None,
        abort_reason: object = None,
    ) -> str:
        receipt = latest.get(agent, receipts.get(agent, default))
        _write_projection_for(agent, prompt, receipt)
        return json.dumps({"type": "text", "text": receipt})

    cast(Any, spawn).repair_session = repair

    return spawn


def _write_projection_for(agent: str, prompt: str, receipt: str) -> None:
    match = _PROJECTION_RE.search(prompt)
    if match is None:
        return
    path = Path(match.group(1))
    if '"dispositions"' in prompt or agent.endswith("_final"):
        dispositions = (
            []
            if agent.endswith("_final")
            else [
                {
                    "id": "F1",
                    "status": "RESOLVED" if receipt.startswith("PASS") else "OPEN",
                    "evidence": "test fixture verification",
                }
            ]
        )
        payload: object = {
            "schema_version": 1,
            "dispositions": dispositions,
            "new_findings": [],
        }
    elif agent.startswith(("research_gate", "review_")):
        findings = []
        if receipt.startswith("BLOCK"):
            finding = {
                "id": "F1",
                "severity": "MAJOR",
                "status": "OPEN",
                "title": "Scripted blocker",
                "requirement": "Complete the required behavior",
                "evidence": "test fixture",
                "failure_scenario": "The requirement remains unmet",
                "required_outcome": "Satisfy the requirement",
            }
            if agent == "research_gate":
                finding["owner"] = "research_technical"
            findings.append(finding)
        payload = {"schema_version": 1, "findings": findings}
    elif agent == "research_requirements":
        payload = {
            "summary": "requirements",
            "requirements": ["R1"],
            "evidence": ["ticket:R1"],
            "unknowns": [],
            "obligations": ["Preserve R1"],
        }
    elif agent == "research_architecture":
        payload = {
            "summary": "architecture",
            "existing_seams": [],
            "proposed_seams": ["Implement R1"],
            "evidence": ["repository fixture"],
            "unknowns": [],
            "obligations": ["Preserve ownership"],
        }
    elif agent == "research_technical":
        payload = {
            "summary": "technical",
            "contracts": ["Local API"],
            "evidence": ["local API fixture"],
            "unknowns": [],
            "obligations": ["Verify behavior"],
        }
    elif agent == "plan":
        payload = {
            "summary": "plan",
            "decisions": ["Implement R1"],
            "phases": ["Implement", "Verify"],
            "evidence": ["research contracts"],
            "verification": ["Run tests"],
            "unknowns": [],
        }
    elif agent == "impl":
        payload = {
            "summary": "implemented",
            "changed_files": [],
            "verification": ["Tests delegated to mechanical gate"],
            "unresolved": [],
        }
    elif agent == "commit":
        payload = {"summary": "delivered", "unresolved": []}
    else:
        raise AssertionError(f"no projection fixture for {agent}")
    path.write_text(json.dumps(payload), encoding="utf-8")


def _with_repair(spawn: Spawner, **kwargs: Any) -> PipelineDeps:
    return PipelineDeps(
        spawn=spawn,
        session_repair=cast(Any, spawn).repair_session,
        **kwargs,
    )


class RecordingRunner:
    def __init__(self, returns: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._returns = returns or {}

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        joined = " ".join(args)
        for key, val in self._returns.items():
            if key in joined:
                return val
        return ""


def _record() -> tuple[list[Event], Callable[[Event], None]]:
    seen: list[Event] = []
    return seen, seen.append


# All reviewers PASS / DONE so the happy path flows to the end. Keyed by the default config's
# phase ids: review_plan gates (PASS), review_impl fans out (DONE), review_impl_final gates (PASS).
_GREEN = {
    "research_gate": "PASS: ok",
    "review_plan": "PASS: ok",
    "review_impl": "DONE: findings",
    "review_impl_final": "PASS: ok",
}


def _git_with_ticket() -> GitOps:
    """A GitOps whose runner answers `gh issue view` so the ticket guard finds a body.

    Real runs (CLI/API) always wire a GitOps. These fixtures mirror that — without it the engine's
    ticket guard fails the run for an empty body.
    """
    return GitOps(run=RecordingRunner(returns=_git_responses("T", "b")))


def _git_responses(title: str, body: str) -> dict[str, str]:
    return {
        "issue view": json.dumps({"title": title, "body": body}),
        "gh pr list": json.dumps(
            [
                {
                    "number": 7,
                    "headRefName": "feature/ticket-7",
                    "title": "Tickets #1 #7 #42",
                    "body": "Closes #1, #7, and #42",
                    "url": "https://example.test/pr/7",
                    "headRefOid": "abc123",
                }
            ]
        ),
        "git rev-parse HEAD": "abc123",
        "headRefOid": json.dumps({"headRefOid": "abc123"}),
        "git status --porcelain": "",
    }


def test_run_drives_all_phases(repo_dir: Path) -> None:
    seen, on_event = _record()
    spawn = _spawn_returning(repo_dir, _GREEN)
    deps = _with_repair(
        loader=FakeLoader(),
        spawn=spawn,
        git=_git_with_ticket(),
        build_test=lambda _c, _selection: (True, "green"),
    )
    final = run_pipeline(7, directory=str(repo_dir), deps=deps, on_event=on_event)

    assert final["type"] == events.RUN_DONE
    started = [e["phase"] for e in seen if e["type"] == events.PHASE_STARTED]
    assert started == [
        "research_requirements",
        "research_architecture",
        "research_technical",
        "research_gate",
        "plan",
        "review_plan",
        "impl",
        "build_test",
        "review_impl.architecture",
        "review_impl.correctness",
        "review_impl.tests",
        "review_impl_final",
        "commit",
    ]


def test_run_dir_created(repo_dir: Path) -> None:
    spawn = _spawn_returning(repo_dir, _GREEN)
    deps = _with_repair(
        loader=FakeLoader(),
        spawn=spawn,
        git=_git_with_ticket(),
        build_test=lambda _c, _selection: (True, ""),
    )
    run_id = make_run_id(7)
    run_pipeline(7, directory=str(repo_dir), run_id=run_id, deps=deps)
    # Run artifacts land in the machine-level runs root, never inside the checkout.
    assert (cfg.default_runs_root() / run_id).is_dir()
    assert not (repo_dir / run_id).exists()


def test_plan_gate_block_with_no_retry_fails(repo_dir: Path) -> None:
    config_file = repo_dir / cfg.CONFIG_FILENAME
    config_file.write_text(
        config_file.read_text().replace("retry_budget = 1", "retry_budget = 0"), encoding="utf-8"
    )
    spawn = _spawn_returning(repo_dir, {**_GREEN, "review_plan": "BLOCK: weak"})
    deps = _with_repair(
        loader=FakeLoader(),
        spawn=spawn,
        git=_git_with_ticket(),
        build_test=lambda _c, _selection: (True, ""),
    )
    final = run_pipeline(1, directory=str(repo_dir), deps=deps)
    assert final["type"] == events.RUN_FAILED
    assert final["phase"] == "review_plan"


def test_plan_gate_revise_then_verify_passes(repo_dir: Path) -> None:
    """BLOCK on first review, then a revise→verify round that PASSes (budget=1)."""
    seen, on_event = _record()
    calls = {"review_plan": 0}
    latest: dict[str, str] = {}

    def spawn(
        agent: str,
        preset: str,
        prompt: str,
        *,
        timeout: float,
        stream_path: Path,
        on_tool: object = None,
        on_usage: object = None,
        abort_reason: object = None,
    ) -> str:
        if agent == "review_plan":
            calls["review_plan"] += 1
            text = "BLOCK: weak" if calls["review_plan"] == 1 else "PASS: fixed"
        else:
            text = _GREEN.get(agent, "DONE: ok")
        latest[agent] = text
        _write_artifact_for(repo_dir, prompt, text)
        return json.dumps({"type": "text", "text": text})

    def repair(
        agent: str,
        preset: str,
        prompt: str,
        *,
        timeout: float,
        stream_path: Path,
        on_tool: object = None,
        on_usage: object = None,
        abort_reason: object = None,
    ) -> str:
        text = latest[agent]
        _write_projection_for(agent, prompt, text)
        return json.dumps({"type": "text", "text": text})

    cast(Any, spawn).repair_session = repair
    deps = _with_repair(
        loader=FakeLoader(),
        spawn=spawn,
        git=_git_with_ticket(),
        build_test=lambda _c, _selection: (True, ""),
    )
    final = run_pipeline(1, directory=str(repo_dir), deps=deps, on_event=on_event)
    assert final["type"] == events.RUN_DONE
    assert any(e["type"] == events.RETRY and e["phase"] == "review_plan" for e in seen)


def test_build_test_fail_blocks(repo_dir: Path) -> None:
    config_file = repo_dir / cfg.CONFIG_FILENAME
    # Zero the build_test retry budget so a BLOCK halts immediately.
    text = config_file.read_text()
    text = text.replace(
        'step         = "build_test"\ngates        = true\nretry_budget = 1',
        'step         = "build_test"\ngates        = true\nretry_budget = 0',
    )
    config_file.write_text(text, encoding="utf-8")
    spawn = _spawn_returning(repo_dir, _GREEN)
    deps = _with_repair(
        loader=FakeLoader(),
        spawn=spawn,
        git=_git_with_ticket(),
        build_test=lambda _c, _selection: (False, "2 failed"),
    )
    final = run_pipeline(1, directory=str(repo_dir), deps=deps)
    assert final["type"] == events.RUN_FAILED
    assert final["phase"] == "build_test"


def test_needs_decision_halts(repo_dir: Path) -> None:
    spawn = _spawn_returning(repo_dir, {"plan": "FAILED: needs decision — which db?"})
    deps = _with_repair(
        loader=FakeLoader(),
        spawn=spawn,
        git=_git_with_ticket(),
    )
    final = run_pipeline(1, directory=str(repo_dir), deps=deps)
    assert final["type"] == events.RUN_HALTED


def test_should_stop_halts(repo_dir: Path) -> None:
    spawn = _spawn_returning(repo_dir, {})
    deps = _with_repair(loader=FakeLoader(), spawn=spawn, git=_git_with_ticket())
    final = run_pipeline(1, directory=str(repo_dir), deps=deps, should_stop=lambda: True)
    assert final["type"] == events.RUN_HALTED


# -- driver git surface (ticket read only; branch + commit are agent phases) ------


def test_driver_only_reads_the_ticket(repo_dir: Path) -> None:
    """The driver's sole git/gh call is `gh issue view`. Branch, commit, push, and PR are performed
    by the branch/commit *agent* phases (here the spawn is faked, so no git runs), never the driver.
    """
    runner = RecordingRunner(returns=_git_responses("Add the thing", "do it"))
    spawn = _spawn_returning(repo_dir, _GREEN)
    deps = _with_repair(
        loader=FakeLoader(),
        spawn=spawn,
        git=GitOps(run=runner),
        build_test=lambda _c, _selection: (True, ""),
    )
    final = run_pipeline(42, directory=str(repo_dir), deps=deps)
    assert final["type"] == events.RUN_DONE

    cmds = [" ".join(c) for c in runner.calls]
    assert any("issue view 42" in c for c in cmds)  # the one read the driver does
    # The driver performs NO git mutations — those are the agents' job.
    assert not any("checkout" in c for c in cmds)
    assert not any("git add" in c for c in cmds)
    assert not any("git commit" in c for c in cmds)
    assert not any("git push" in c for c in cmds)
    assert not any("pr create" in c for c in cmds)
