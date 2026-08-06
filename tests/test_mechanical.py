"""Unit tests for the built-in mechanical steps (ticket #33)."""

from __future__ import annotations

import json
import shlex
import sys
import threading
import time
from pathlib import Path

from quill.config import PhaseDef, QuillfolioConfig
from quill.git_ops import GitOps
from quill.mechanical import (
    MECHANICAL_STEPS,
    body_text_from,
    build_test_runner,
    run_mechanical,
    step_collect_pr_evidence,
    step_build_test,
    step_ci_check,
    step_acknowledge_pr_feedback,
    step_publish_pr_review,
    step_pr_head_guard,
)
from quill.phases import Outcome, PhaseResult
from quill.runctx import BuildTest, PipelineDeps, RunContext, VerificationResult


class _FakeRunner:
    """Records git/gh command sequences; returns canned stdout per command."""

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._responses = responses or {}

    def __call__(self, args):  # type: ignore[no-untyped-def]
        args = list(args)
        self.calls.append(args)
        key = " ".join(args)
        for prefix, out in self._responses.items():
            if key.startswith(prefix):
                return out
        if key.startswith("gh pr view 7 --json body,closingIssuesReferences"):
            return json.dumps({"body": "Closes #33", "closingIssuesReferences": [{"number": 33}]})
        return ""


def _config(
    tmp_path: Path, *, build_command: str = "make", test_command: str = "make test"
) -> QuillfolioConfig:
    return QuillfolioConfig(
        directory=tmp_path,
        repo="me/proj",
        pr_base="main",
        runner="opencode",
        build_command=build_command,
        test_command=test_command,
        log_dir="logs",
        phases=[],
    )


def _ctx(tmp_path: Path, deps: PipelineDeps) -> RunContext:
    run_dir = tmp_path / "quillvault" / "run1"
    run_dir.mkdir(parents=True, exist_ok=True)
    return RunContext(
        config=_config(tmp_path),
        deps=deps,
        ticket=33,
        run_id="run1",
        run_dir=run_dir,
        on_event=lambda _e: None,
        should_stop=lambda: False,
        answer_decision=lambda _q: None,
    )


class _FakeLoader:
    def load(self, preset: str, timeout: float = 180) -> None: ...
    def unload_all(self) -> None: ...


def _spawn(
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
    return ""


def _deps(*, git: GitOps | None = None, build_test: BuildTest | None = None) -> PipelineDeps:
    return PipelineDeps(
        loader=_FakeLoader(),
        spawn=_spawn,
        git=git,
        build_test=build_test,
    )


def _noop_spawn(ctx: RunContext, phase: PhaseDef) -> PhaseResult:
    return PhaseResult(Outcome.DONE, "spawned")


# -- registry ---------------------------------------------------------------------


def test_registry_holds_only_the_verification_gates() -> None:
    # branch + commit are agent phases; mechanical steps verify locally or on GitHub's runners.
    assert set(MECHANICAL_STEPS) == {
        "build",
        "test",
        "build_test",
        "ci_check",
        "pr_head_guard",
        "acknowledge_pr_feedback",
        "collect_pr_evidence",
        "publish_pr_review",
    }


def test_update_head_guard_blocks_a_concurrent_push(tmp_path: Path) -> None:
    runner = _FakeRunner({"gh pr view 7 --json headRefOid": '{"headRefOid":"new"}'})
    ctx = _ctx(tmp_path, _deps(git=GitOps(runner)))
    ctx.pr_number = 7
    ctx.pr_head_sha = "old"

    result = step_pr_head_guard(
        ctx, PhaseDef(id="guard", type="mechanical", step="pr_head_guard"), spawn=_noop_spawn
    )

    assert result.outcome is Outcome.NEEDS_DECISION
    assert "moved" in result.message


def test_feedback_acknowledgement_is_idempotent(tmp_path: Path) -> None:
    runner = _FakeRunner(
        {
            "git rev-parse HEAD": "abc123",
            "gh pr view 7 --json headRefOid": '{"headRefOid":"abc123"}',
            "gh pr view 7 --json comments": '{"comments":[]}',
        }
    )
    ctx = _ctx(tmp_path, _deps(git=GitOps(runner)))
    ctx.pr_number = 7
    ctx.feedback_ids = ("C1",)
    phase = PhaseDef(id="ack", type="mechanical", step="acknowledge_pr_feedback")

    first = step_acknowledge_pr_feedback(ctx, phase, spawn=_noop_spawn)
    second = step_acknowledge_pr_feedback(ctx, phase, spawn=_noop_spawn)

    assert first.outcome is second.outcome is Outcome.PASS
    assert sum(call[:3] == ["gh", "pr", "comment"] for call in runner.calls) == 1
    assert "already acknowledged" in second.message


def test_pr_evidence_records_test_and_build_without_blocking_review(tmp_path: Path) -> None:
    selections: list[str] = []

    def verify(_config: QuillfolioConfig, selection: str) -> tuple[bool, str]:
        selections.append(selection)
        return (selection == "build", f"{selection} output")

    ctx = _ctx(tmp_path, _deps(build_test=verify))
    phase = PhaseDef(
        id="evidence",
        type="mechanical",
        step="collect_pr_evidence",
        artifact="pr-verification.md",
    )

    result = step_collect_pr_evidence(ctx, phase, spawn=_noop_spawn)

    assert result.outcome is Outcome.PASS
    assert selections == ["test", "build"]
    artifact = (ctx.run_dir / "pr-verification.md").read_text(encoding="utf-8")
    assert "Test — FAIL" in artifact
    assert "Build — PASS" in artifact


def test_pr_review_publishes_only_valid_blocking_findings(tmp_path: Path) -> None:
    runner = _FakeRunner(
        {
            "gh pr view 7 --json headRefOid": '{"headRefOid":"abc123"}',
            "git status --porcelain": "",
            "gh pr view 7 --json comments": '{"comments":[]}',
        }
    )
    ctx = _ctx(tmp_path, _deps(git=GitOps(runner)))
    ctx.pr_number = 7
    ctx.pr_head_sha = "abc123"
    (ctx.run_dir / "pr-review.json").write_text(
        json.dumps(
            {
                "verdict": "BLOCK",
                "summary": "One blocker.",
                "findings": [
                    {
                        "id": "PRR-001",
                        "severity": "MAJOR",
                        "title": "Primary path fails",
                        "requirement": "Ticket requires the primary path",
                        "evidence": "src/app.py:42 returns false",
                        "failure_scenario": "A normal request reaches this branch",
                        "impact": "The requested behavior is unavailable",
                        "required_outcome": "The normal request must succeed",
                    },
                    {"severity": "MINOR", "title": "advisory"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = step_publish_pr_review(
        ctx,
        PhaseDef(id="publish", type="mechanical", step="publish_pr_review"),
        spawn=_noop_spawn,
    )

    assert result.outcome is Outcome.PASS
    comment = next(call for call in runner.calls if call[:3] == ["gh", "pr", "comment"])
    assert "PRR-001" in comment[-1]
    assert "advisory" not in comment[-1]
    assert "quill-pr-review-result:v1 head=abc123 verdict=BLOCK digest=" in comment[-1]


def test_pr_review_rejects_repository_changes_before_commenting(tmp_path: Path) -> None:
    runner = _FakeRunner(
        {
            "gh pr view 7 --json headRefOid": '{"headRefOid":"abc123"}',
            "git status --porcelain": " M src/app.py",
        }
    )
    ctx = _ctx(tmp_path, _deps(git=GitOps(runner)))
    ctx.pr_number = 7
    ctx.pr_head_sha = "abc123"

    result = step_publish_pr_review(
        ctx,
        PhaseDef(id="publish", type="mechanical", step="publish_pr_review"),
        spawn=_noop_spawn,
    )

    assert result.outcome is Outcome.FAILED
    assert "read-only workspace" in result.message
    assert not any(call[:3] == ["gh", "pr", "comment"] for call in runner.calls)


def test_clean_pr_review_uses_repository_check_policy(tmp_path: Path) -> None:
    runner = _FakeRunner(
        {
            "gh pr view 7 --json headRefOid": '{"headRefOid":"abc123"}',
            "git status --porcelain": "",
            "gh pr view 7 --json comments": '{"comments":[]}',
            "gh pr view 7 --json number,state,isDraft": json.dumps(
                {
                    "number": 7,
                    "state": "OPEN",
                    "isDraft": False,
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "headRefName": "feature/ticket-33",
                    "headRefOid": "abc123",
                    "baseRefName": "main",
                    "statusCheckRollup": [],
                }
            ),
            "gh pr view 7 --json state,mergedAt,mergeCommit": json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-01T21:00:00Z",
                    "mergeCommit": {"oid": "merge456"},
                    "headRefName": "feature/ticket-33",
                    "headRefOid": "abc123",
                    "baseRefName": "main",
                }
            ),
            "git ls-remote --heads origin": "",
        }
    )
    ctx = _ctx(tmp_path, _deps(git=GitOps(runner)))
    ctx.pr_number = 7
    ctx.pr_head_sha = "abc123"
    ctx.branch = "feature/ticket-33"
    ctx.config.pr_checks_required = False
    (ctx.run_dir / "pr-review.json").write_text(
        json.dumps({"verdict": "PASS", "summary": "Ready.", "findings": []}),
        encoding="utf-8",
    )

    result = step_publish_pr_review(
        ctx,
        PhaseDef(id="publish", type="mechanical", step="publish_pr_review"),
        spawn=_noop_spawn,
    )

    assert result.outcome is Outcome.PASS
    comment = next(call for call in runner.calls if call[:3] == ["gh", "pr", "comment"])
    assert "Checked by Quill Pull Request Reviewer" in comment[-1]
    assert ["gh", "pr", "merge", "7", "--merge", "--match-head-commit", "abc123"] in runner.calls


def test_registry_matches_the_configs_allowed_steps() -> None:
    """A step the config accepts but the registry lacks would KeyError mid-run, after a model has
    already done the expensive work."""
    from quill.config import MECHANICAL_STEPS as ALLOWED_STEPS

    assert set(MECHANICAL_STEPS) == set(ALLOWED_STEPS)


# -- build_test -------------------------------------------------------------------


def test_build_test_no_runner_is_unavailable(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _deps(build_test=None))
    result = step_build_test(
        ctx, PhaseDef(id="bt", type="mechanical", step="build_test"), spawn=_noop_spawn
    )
    assert result.outcome is Outcome.FAILED
    assert ctx.mechanical_evidence["bt"].status.value == "UNAVAILABLE"


def test_build_test_rejects_malformed_typed_runner_result_as_unavailable(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        _deps(build_test=lambda _config, _selection: VerificationResult("test", ())),
    )
    result = step_build_test(
        ctx,
        PhaseDef(id="bt", type="mechanical", step="build_test"),
        spawn=_noop_spawn,
    )
    assert result.outcome is Outcome.FAILED
    assert "could not persist build/test evidence" in result.message
    assert ctx.mechanical_evidence["bt"].status.value == "UNAVAILABLE"


def test_build_test_ok_passes(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _deps(build_test=lambda _c, _selection: (True, "ok")))
    result = step_build_test(
        ctx, PhaseDef(id="bt", type="mechanical", step="build_test"), spawn=_noop_spawn
    )
    assert result.outcome is Outcome.PASS


def test_build_test_failure_blocks(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _deps(build_test=lambda _c, _selection: (False, "boom")))
    result = step_build_test(
        ctx, PhaseDef(id="bt", type="mechanical", step="build_test"), spawn=_noop_spawn
    )
    assert result.outcome is Outcome.BLOCK
    assert "boom" in result.message


def test_build_test_runner_writes_log(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("", encoding="utf-8")
    runner = build_test_runner(str(tmp_path))
    config = _config(tmp_path, build_command="echo hi", test_command="echo bye")
    ok, log = runner(config, "build_test")
    assert ok
    assert "hi" in log
    assert (tmp_path / "logs" / "test-log.txt").exists()


def test_build_test_runner_can_select_test_or_build_independently(tmp_path: Path) -> None:
    runner = build_test_runner(str(tmp_path))
    config = _config(
        tmp_path,
        build_command="echo executable-build",
        test_command="echo unit-tests",
    )

    test_ok, test_log = runner(config, "test")
    build_ok, build_log = runner(config, "build")

    assert test_ok and build_ok
    assert "unit-tests" in test_log
    assert "executable-build" not in test_log
    assert "executable-build" in build_log
    assert "unit-tests" not in build_log


def test_build_test_runner_cancel_terminates_active_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    blocker = tmp_path / "blocker.py"
    blocker.write_text(
        "from pathlib import Path\n"
        "import time\n"
        f"Path({str(marker)!r}).write_text('started', encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    command = shlex.join((sys.executable, str(blocker)))
    runner = build_test_runner(str(tmp_path))
    config = _config(tmp_path, build_command=command, test_command=command)
    result: list[tuple[bool, str]] = []
    worker = threading.Thread(target=lambda: result.append(runner(config, "test")))
    worker.start()

    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "verification command did not start"

    cancel = getattr(runner, "cancel")
    cancel()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert result and result[0][0] is False
    assert "terminated by stop request" in result[0][1]


# -- dispatch ---------------------------------------------------------------------


def test_run_mechanical_dispatches(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _deps(build_test=lambda _c, _selection: (True, "ok")))
    result = run_mechanical(
        ctx, PhaseDef(id="bt", type="mechanical", step="build_test"), spawn=_noop_spawn
    )
    assert result.outcome is Outcome.PASS


def test_local_mechanical_steps_pass_their_selection_to_the_runner(tmp_path: Path) -> None:
    selections: list[str] = []

    def run(_config: QuillfolioConfig, selection: str) -> tuple[bool, str]:
        selections.append(selection)
        return True, selection

    ctx = _ctx(tmp_path, _deps(build_test=run))
    for step in ("test", "build"):
        result = run_mechanical(
            ctx, PhaseDef(id=step, type="mechanical", step=step), spawn=_noop_spawn
        )
        assert result.outcome is Outcome.PASS

    assert selections == ["test", "build"]


# -- ticket body extraction -------------------------------------------------------


def test_body_text_from_extracts_json_body() -> None:
    raw = json.dumps({"title": "T", "body": "the real body"})
    assert body_text_from(raw) == "the real body"


def test_body_text_from_passes_through_non_json() -> None:
    assert body_text_from("  plain text  ") == "plain text"


def test_body_text_from_missing_field_falls_back_to_raw() -> None:
    raw = json.dumps({"title": "T"})  # no body key
    assert body_text_from(raw) == raw


# -- ci_check ---------------------------------------------------------------------


def _rollup(*checks: dict[str, object]) -> str:
    return json.dumps({"statusCheckRollup": list(checks)})


def _check(
    name: str, *, status: str = "COMPLETED", conclusion: str = "SUCCESS"
) -> dict[str, object]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": f"https://github.com/me/proj/actions/runs/4242/job/9{name}",
    }


_PR_LIST = "gh pr list"
_PR_FOUND = json.dumps(
    [{"number": 7, "headRefName": "ticket-33-fix", "title": "Fix #33", "body": "", "url": "u/7"}]
)


def _ci_phase() -> PhaseDef:
    return PhaseDef(id="ci", type="mechanical", step="ci_check", gates=True, on_block=("impl",))


def _ci_ctx(tmp_path: Path, runner: _FakeRunner) -> RunContext:
    ctx = _ctx(tmp_path, _deps(git=GitOps(run=runner)))
    ctx.config.ci_seconds = 120
    return ctx


def test_ci_check_passes_when_every_check_is_green(tmp_path: Path) -> None:
    runner = _FakeRunner(
        {
            _PR_LIST: _PR_FOUND,
            "gh pr view 7 --json statusCheckRollup": _rollup(_check("build"), _check("test")),
        }
    )
    ctx = _ci_ctx(tmp_path, runner)

    result = step_ci_check(ctx, _ci_phase(), spawn=_noop_spawn)

    assert result.outcome is Outcome.PASS
    assert "build, test" in result.message


def test_ci_check_resolves_new_pr_by_exact_branch_before_ticket_search(tmp_path: Path) -> None:
    """GitHub full-text search can lag immediately after `gh pr create`; exact head cannot."""

    class _Runner(_FakeRunner):
        def __call__(self, args):  # type: ignore[no-untyped-def]
            args = list(args)
            self.calls.append(args)
            if "--head" in args:
                return _PR_FOUND
            if "--search" in args:
                return "[]"
            if args[:4] == ["gh", "pr", "view", "7"]:
                if "body,closingIssuesReferences" in args:
                    return json.dumps(
                        {"body": "Closes #33", "closingIssuesReferences": [{"number": 33}]}
                    )
                return _rollup(_check("build"))
            return ""

    runner = _Runner()
    ctx = _ci_ctx(tmp_path, runner)
    ctx.branch = "ticket-33-fix"

    result = step_ci_check(ctx, _ci_phase(), spawn=_noop_spawn)

    assert result.outcome is Outcome.PASS
    list_calls = [call for call in runner.calls if call[:3] == ["gh", "pr", "list"]]
    assert len(list_calls) == 1
    assert "--head" in list_calls[0]
    assert all("--search" not in call for call in list_calls)


def test_ci_check_blocks_and_writes_findings_with_the_failing_log(tmp_path: Path) -> None:
    runner = _FakeRunner(
        {
            _PR_LIST: _PR_FOUND,
            "gh pr view 7 --json statusCheckRollup": _rollup(
                _check("build"), _check("test", conclusion="FAILURE")
            ),
            "gh run view 4242 --log-failed": "FAILED: test_thing\nassert 1 == 2",
        }
    )
    ctx = _ci_ctx(tmp_path, runner)

    result = step_ci_check(ctx, _ci_phase(), spawn=_noop_spawn)

    assert result.outcome is Outcome.BLOCK
    assert "test" in result.message
    findings = (ctx.run_dir / "ci-findings.md").read_text(encoding="utf-8")
    assert "assert 1 == 2" in findings
    # Only the failing check is reported — a green job's log is noise in a revise prompt.
    assert "## test" in findings
    assert "## build" not in findings


def test_ci_check_waits_for_pending_checks_then_settles(tmp_path: Path, monkeypatch) -> None:
    """The gate must not read a verdict off a run that is still going."""
    responses = [
        _rollup(_check("build", status="IN_PROGRESS", conclusion="")),
        _rollup(_check("build", status="IN_PROGRESS", conclusion="")),
        _rollup(_check("build")),
    ]

    class _Runner(_FakeRunner):
        def __call__(self, args):  # type: ignore[no-untyped-def]
            args = list(args)
            self.calls.append(args)
            if args[:4] == ["gh", "pr", "view", "7"]:
                if "body,closingIssuesReferences" in args:
                    return json.dumps(
                        {"body": "Closes #33", "closingIssuesReferences": [{"number": 33}]}
                    )
                return responses.pop(0) if len(responses) > 1 else responses[0]
            if args[:3] == ["gh", "pr", "list"]:
                return _PR_FOUND
            return ""

    slept: list[float] = []
    monkeypatch.setattr("quill.mechanical._sleep", slept.append)
    ctx = _ci_ctx(tmp_path, _Runner())

    result = step_ci_check(ctx, _ci_phase(), spawn=_noop_spawn)

    assert result.outcome is Outcome.PASS
    assert len(slept) == 2  # polled twice while pending, then settled


def test_ci_check_tolerates_the_window_before_checks_register(tmp_path: Path, monkeypatch) -> None:
    """Right after a push GitHub reports no checks at all. Treating that as green would sail
    straight through a gate whose workflows had not started."""
    responses = [_rollup(), _rollup(), _rollup(_check("build"))]

    class _Runner(_FakeRunner):
        def __call__(self, args):  # type: ignore[no-untyped-def]
            args = list(args)
            self.calls.append(args)
            if args[:4] == ["gh", "pr", "view", "7"]:
                if "body,closingIssuesReferences" in args:
                    return json.dumps(
                        {"body": "Closes #33", "closingIssuesReferences": [{"number": 33}]}
                    )
                return responses.pop(0) if len(responses) > 1 else responses[0]
            if args[:3] == ["gh", "pr", "list"]:
                return _PR_FOUND
            return ""

    monkeypatch.setattr("quill.mechanical._sleep", lambda _s: None)
    ctx = _ci_ctx(tmp_path, _Runner())

    assert step_ci_check(ctx, _ci_phase(), spawn=_noop_spawn).outcome is Outcome.PASS


def test_ci_check_fails_when_no_checks_ever_register(tmp_path: Path, monkeypatch) -> None:
    """A repo with no PR workflows would otherwise wait out the whole CI timeout for nothing."""
    runner = _FakeRunner({_PR_LIST: _PR_FOUND, "gh pr view 7 --json statusCheckRollup": _rollup()})
    clock = iter([float(t) for t in range(500)])
    monkeypatch.setattr("quill.mechanical._sleep", lambda _s: None)
    monkeypatch.setattr("quill.mechanical._monotonic", lambda: next(clock))
    ctx = _ci_ctx(tmp_path, runner)
    ctx.config.ci_seconds = 1800  # the real default, comfortably past the no-checks grace

    result = step_ci_check(ctx, _ci_phase(), spawn=_noop_spawn)

    assert result.outcome is Outcome.FAILED
    assert "no CI checks" in result.message


def test_ci_check_times_out_while_still_pending(tmp_path: Path, monkeypatch) -> None:
    runner = _FakeRunner(
        {
            _PR_LIST: _PR_FOUND,
            "gh pr view 7 --json statusCheckRollup": _rollup(
                _check("slow", status="IN_PROGRESS", conclusion="")
            ),
        }
    )
    clock = iter([float(t) for t in range(5000)])
    monkeypatch.setattr("quill.mechanical._sleep", lambda _s: None)
    monkeypatch.setattr("quill.mechanical._monotonic", lambda: next(clock))
    ctx = _ci_ctx(tmp_path, runner)

    result = step_ci_check(ctx, _ci_phase(), spawn=_noop_spawn)

    assert result.outcome is Outcome.FAILED
    assert "did not finish" in result.message
    assert "slow" in result.message


def test_ci_check_fails_when_the_ticket_has_no_open_pr(tmp_path: Path) -> None:
    runner = _FakeRunner({_PR_LIST: "[]"})
    ctx = _ci_ctx(tmp_path, runner)

    result = step_ci_check(ctx, _ci_phase(), spawn=_noop_spawn)

    assert result.outcome is Outcome.FAILED
    assert "must run after the phase that pushes" in result.message


def test_ci_check_resolves_and_caches_the_pr_in_create_mode(tmp_path: Path) -> None:
    """Create mode never populates pr_number — only update mode did — so the run would otherwise
    finish reporting no PR URL at all."""
    runner = _FakeRunner(
        {_PR_LIST: _PR_FOUND, "gh pr view 7 --json statusCheckRollup": _rollup(_check("build"))}
    )
    ctx = _ci_ctx(tmp_path, runner)
    assert ctx.pr_number is None

    step_ci_check(ctx, _ci_phase(), spawn=_noop_spawn)

    assert ctx.pr_number == 7
    assert ctx.pr_url == "u/7"
    assert ctx.branch == "ticket-33-fix"


def test_ci_check_without_a_gh_reader_is_unavailable(tmp_path: Path) -> None:
    result = step_ci_check(_ctx(tmp_path, _deps()), _ci_phase(), spawn=_noop_spawn)
    assert result.outcome is Outcome.FAILED
    assert "unavailable" in result.message
