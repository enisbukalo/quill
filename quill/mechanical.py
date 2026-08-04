"""Built-in mechanical phase steps (ticket #33).

Almost every phase is agent-driven. The exceptions are the verification gates: running a build,
or reading a CI verdict, and turning failure into a BLOCK is deterministic code, never a model's
job. Branch creation and commit/push/PR are **not** mechanical — they are agent (producer) phases
that run git/gh themselves, so the whole flow (including branch-naming convention and PR text)
stays data.

* ``build`` / ``test`` — run one configured local command as its own gate.
* ``build_test`` — compatibility step that runs ``build.command`` then ``build.test``.
* ``ci_check`` — wait for the PR's GitHub Actions checks and BLOCK on failure. The same gate, but
  the work happens on GitHub's runners, so the machine driving the pipeline needs none of the
  repo's toolchain.

Both write their failure output to a findings file in the run dir, so the revise sequence reads
exactly what broke instead of retrying blind.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quill.config import PhaseDef, QuillfolioConfig
from quill.git_ops import ChecksStatus, GitError, GitOps
from quill.phases import Outcome, PhaseResult
from quill.runctx import RunContext

#: How the engine spawns an LLM phase: (ctx, phase) -> PhaseResult. Unused by build_test, but kept
#: in the step signature so every mechanical step has a uniform shape.
SpawnPhase = Callable[[RunContext, PhaseDef], PhaseResult]


#: Findings file a BLOCKing build_test writes into the run dir, for the impl revise to read. Named
#: here so the engine's gate can point the producer at the same path (see engine._gate_findings_artifact).
BUILD_FINDINGS_NAME = "build-findings.md"
PR_EVIDENCE_NAME = "pr-verification.md"
PR_REVIEW_NAME = "pr-review.json"
PR_REVIEW_MARKER = "<!-- quill-pr-review -->"
PR_REVIEW_RESULT_MARKER = "quill-pr-review-result:v1"


def step_build_test(ctx: RunContext, phase: PhaseDef, *, spawn: SpawnPhase) -> PhaseResult:
    """Run build then test; PASS/BLOCK so the engine's gate can revise→verify on failure.

    On BLOCK, the build/test output is also written to ``<run-dir>/build-findings.md`` so the impl
    revise can read exactly what failed (compiler errors, failing test names) instead of retrying
    blind — the same "pass the findings into the retry" contract the reviewer gates have.
    """
    if ctx.deps.build_test is None:
        return PhaseResult(Outcome.PASS, "build/test skipped (no runner)")
    ok, log = ctx.deps.build_test(ctx.config, phase.step or "build_test")
    if not ok:
        _write_build_findings(ctx, log)
    return PhaseResult(Outcome.PASS if ok else Outcome.BLOCK, log)


def _write_build_findings(ctx: RunContext, log: str) -> None:
    """Persist the failing build/test log as the run-dir findings file for the impl revise."""
    try:
        path = ctx.run_dir / BUILD_FINDINGS_NAME
        path.write_text(
            "# Build / test failure — fix these before the next build\n\n"
            "The build or test suite failed. Treat every compiler error and failing test below as a "
            "CRITICAL finding: the code must compile and all tests must pass. Fix the root cause in "
            "the source; do not delete or weaken tests to make them pass.\n\n"
            "```\n" + log + "\n```\n",
            encoding="utf-8",
        )
    except OSError:
        pass  # best-effort; the gate decision already happened, revise just won't have the file


# -- ci_check ---------------------------------------------------------------------

#: Findings file a BLOCKing ci_check writes, for the revise sequence to read.
CI_FINDINGS_NAME = "ci-findings.md"

#: Seconds between polls of the PR's checks.
CI_POLL_INTERVAL = 20.0
#: How long to keep waiting when the PR reports **no checks at all**. A push does not register its
#: workflow runs instantly, so "nothing reported" right after one means "not started yet", not
#: "nothing to run". Past this, a repo with no CI wired up is a config error, not an infinite wait.
CI_NO_CHECKS_GRACE = 180.0

# Indirected so tests drive the polling loop without real time passing.
_sleep = time.sleep
_monotonic = time.monotonic


def step_ci_check(ctx: RunContext, phase: PhaseDef, *, spawn: SpawnPhase) -> PhaseResult:
    """Wait for the PR's CI to settle; PASS if it went green, BLOCK with the failing logs if not.

    This is the gate that lets build/test run **in GitHub Actions** instead of on the machine
    driving the pipeline — which is what frees a quill server from needing every repo's toolchain.
    It is deliberately a mechanical phase, so the engine's existing gate can drive
    ``on_block = ["impl", "commit"]``: fix the code, push the fix, then re-run this check against
    the new commit.
    """
    git = ctx.deps.git
    if git is None:
        return PhaseResult(Outcome.PASS, "ci check skipped (no gh reader wired)")

    pr_number = _resolve_pr_number(ctx)
    if pr_number is None:
        return PhaseResult(
            Outcome.FAILED,
            f"no open PR found for ticket {ctx.ticket} — a ci_check phase must run after the "
            "phase that pushes and opens the PR.",
        )

    try:
        link_action = git.ensure_pr_closes_ticket(pr_number, ctx.ticket)
    except GitError as exc:
        return PhaseResult(
            Outcome.FAILED,
            f"could not establish PR #{pr_number} closing link for ticket #{ctx.ticket}: {exc}",
        )

    started = _monotonic()
    deadline = started + ctx.config.ci_seconds
    no_checks_deadline = started + min(CI_NO_CHECKS_GRACE, float(ctx.config.ci_seconds))

    while True:
        try:
            status = git.pr_checks(pr_number)
        except GitError as exc:
            return PhaseResult(Outcome.CRASH, f"could not read PR #{pr_number} checks: {exc}")

        if status.settled:
            break
        # One reading per iteration: two calls would let the deadlines be compared against
        # different instants, and makes the loop untestable against a scripted clock.
        now = _monotonic()
        if not status.reported and now >= no_checks_deadline:
            return PhaseResult(
                Outcome.FAILED,
                f"PR #{pr_number} reported no CI checks within "
                f"{no_checks_deadline - started:g}s — does this repo run workflows on "
                "pull requests?",
            )
        if now >= deadline:
            waiting = ", ".join(c.name for c in status.pending) or "unknown"
            return PhaseResult(
                Outcome.FAILED,
                f"PR #{pr_number} CI did not finish within {ctx.config.ci_seconds:g}s "
                f"(still running: {waiting}).",
            )
        _sleep(CI_POLL_INTERVAL)

    if not status.failed:
        names = ", ".join(c.name for c in status.checks)
        return PhaseResult(Outcome.PASS, f"{link_action}; CI green on PR #{pr_number} ({names})")

    _write_ci_findings(ctx, git, pr_number, status)
    failed = ", ".join(c.name for c in status.failed)
    return PhaseResult(Outcome.BLOCK, f"CI failed on PR #{pr_number}: {failed}")


def _resolve_pr_number(ctx: RunContext) -> int | None:
    """The PR this run is shipping, resolved once and cached on the context.

    In update mode it is already known. In create mode nothing has resolved it — the commit phase
    ran ``gh pr create`` itself, and the driver only watches — so look it up by ticket. Caching
    also fills ``ctx.pr_url``, which the run's final ``run_done`` event reports.
    """
    if ctx.pr_number is not None:
        return ctx.pr_number
    git = ctx.deps.git
    if git is None:
        return None
    try:
        # A PR created by the immediately preceding commit phase may not be in GitHub's full-text
        # search index yet. The run already knows its exact head branch, so resolve that first;
        # ticket search remains the fallback for older/update flows without a branch.
        pr = git.pr_for_branch(ctx.branch) if ctx.branch else None
        if pr is None:
            pr = git.pr_for_ticket(ctx.ticket)
    except GitError:
        return None
    if pr is None:
        return None
    ctx.pr_number = pr.number
    ctx.pr_url = pr.url or ctx.pr_url
    ctx.branch = ctx.branch or pr.branch
    return pr.number


def _write_ci_findings(ctx: RunContext, git: GitOps, pr_number: int, status: ChecksStatus) -> None:
    """Persist the failing checks (and their logs) for the revise sequence to read."""
    sections = [
        "# CI failure — fix these before the next push\n",
        (
            f"The CI run for PR #{pr_number} failed. Treat every failure below as a CRITICAL "
            "finding: fix the root cause in the source. Do not delete, skip, or weaken tests to "
            "make them pass.\n"
        ),
    ]
    for check in status.failed:
        sections.append(f"\n## {check.name}\n")
        if check.url:
            sections.append(f"\n{check.url}\n")
        run_id = check.run_id
        log = git.failed_check_log(run_id) if run_id else ""
        sections.append(f"\n```\n{log or '(no log available)'}\n```\n")
    try:
        (ctx.run_dir / CI_FINDINGS_NAME).write_text("".join(sections), encoding="utf-8")
    except OSError:
        pass  # best-effort; the gate decision already happened, revise just won't have the file


def step_pr_head_guard(ctx: RunContext, phase: PhaseDef, *, spawn: SpawnPhase) -> PhaseResult:
    """Stop before push when somebody moved the existing PR head during this run."""
    git = ctx.deps.git
    if git is None or ctx.pr_number is None or not ctx.pr_head_sha:
        return PhaseResult(Outcome.FAILED, "update head guard is missing PR boundary metadata")
    try:
        current = git.pr_head_sha(ctx.pr_number)
    except GitError as exc:
        return PhaseResult(Outcome.CRASH, f"could not verify PR head: {exc}")
    if current != ctx.pr_head_sha:
        return PhaseResult(
            Outcome.NEEDS_DECISION,
            f"PR #{ctx.pr_number} moved from {ctx.pr_head_sha[:12]} to {current[:12]}; "
            "halt and restart the update against the new head.",
            question="The PR changed during this run. Stop and restart from its new head?",
        )
    return PhaseResult(Outcome.PASS, f"PR head unchanged at {current[:12]}")


def step_collect_pr_evidence(ctx: RunContext, phase: PhaseDef, *, spawn: SpawnPhase) -> PhaseResult:
    """Run local test and build commands, preserving both outcomes for semantic reviewers."""
    sections = ["# Pull request mechanical verification"]
    if ctx.deps.build_test is None:
        sections.append("\nNo local verification runner is configured.\n")
    else:
        for selection in ("test", "build"):
            ok, log = ctx.deps.build_test(ctx.config, selection)
            clipped = log if len(log) <= 30_000 else log[:30_000] + "\n… output truncated …"
            sections.append(
                f"\n## {selection.title()} — {'PASS' if ok else 'FAIL'}\n\n```text\n"
                f"{clipped}\n```\n"
            )
    try:
        (ctx.run_dir / (phase.artifact or PR_EVIDENCE_NAME)).write_text(
            "".join(sections), encoding="utf-8"
        )
    except OSError as exc:
        return PhaseResult(Outcome.FAILED, f"could not write PR verification evidence: {exc}")
    return PhaseResult(Outcome.PASS, "captured local test and build evidence")


def step_publish_pr_review(ctx: RunContext, phase: PhaseDef, *, spawn: SpawnPhase) -> PhaseResult:
    """Publish the validated review; a clean PASS merges and removes the remote branch."""
    git = ctx.deps.git
    if git is None or ctx.pr_number is None or not ctx.pr_head_sha:
        return PhaseResult(Outcome.FAILED, "PR review publication is missing PR metadata")
    try:
        current = git.pr_head_sha(ctx.pr_number)
        if current != ctx.pr_head_sha:
            return PhaseResult(
                Outcome.FAILED,
                f"PR #{ctx.pr_number} moved from {ctx.pr_head_sha[:12]} to {current[:12]}; "
                "discard this stale review and restart",
            )
        dirty = git.workspace_status().strip()
        if dirty:
            return PhaseResult(
                Outcome.FAILED,
                "PR review modified the read-only workspace: " + dirty.replace("\n", ", "),
            )
        review = _load_pr_review(ctx.run_dir / PR_REVIEW_NAME)
        findings = review["findings"]
        action = git.upsert_pr_comment(
            ctx.pr_number,
            PR_REVIEW_MARKER,
            _render_pr_review_comment(ctx, review),
        )
        merge_action = None
        if not findings:
            if not ctx.branch:
                raise GitError("PR review publication is missing the reviewed branch")
            merge_action = git.merge_reviewed_pr(
                ctx.pr_number,
                expected_head_sha=ctx.pr_head_sha,
                expected_branch=ctx.branch,
                expected_base=ctx.config.pr_base,
            )
    except (GitError, OSError, ValueError, KeyError, TypeError) as exc:
        return PhaseResult(Outcome.FAILED, f"could not publish PR review: {exc}")
    if findings:
        return PhaseResult(
            Outcome.PASS,
            f"{action} PR review comment with {len(findings)} blocking finding(s)",
        )
    return PhaseResult(
        Outcome.PASS,
        f"{action} PR review comment: no blocking findings; {merge_action}",
    )


def _load_pr_review(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("pr-review.json must contain one JSON object")
    findings = raw.get("findings")
    if not isinstance(findings, list):
        raise ValueError("pr-review.json findings must be a list")
    normalized: list[dict[str, str]] = []
    required = (
        "id",
        "severity",
        "title",
        "requirement",
        "evidence",
        "failure_scenario",
        "impact",
        "required_outcome",
    )
    for index, item in enumerate(findings, 1):
        if not isinstance(item, dict):
            raise ValueError(f"finding {index} must be an object")
        severity = str(item.get("severity") or "").upper()
        if severity not in {"CRITICAL", "MAJOR"}:
            continue
        finding = {key: str(item.get(key) or "").strip() for key in required}
        finding["severity"] = severity
        missing = [key for key in required if not finding[key]]
        if missing:
            raise ValueError(f"finding {index} is missing: {', '.join(missing)}")
        normalized.append(finding)
    # Quill already knows the verdict: it is BLOCK exactly when a blocking finding survived
    # reconciliation. Rejecting the artifact because the model's own `verdict` field disagreed threw
    # away a complete, valid review over a single word — one recorded run died on
    # "verdict must be BLOCK for 1 blocking finding(s)". Derive it instead, as the structured gates do.
    verdict = "BLOCK" if normalized else "PASS"
    return {
        "verdict": verdict,
        "summary": str(raw.get("summary") or "").strip(),
        "findings": normalized,
    }


def _render_pr_review_comment(ctx: RunContext, review: dict[str, Any]) -> str:
    findings = review["findings"]
    digest = pr_review_digest(review)
    lines = [
        PR_REVIEW_MARKER,
        (
            f"<!-- {PR_REVIEW_RESULT_MARKER} head={ctx.pr_head_sha} "
            f"verdict={review['verdict']} digest={digest} -->"
        ),
        f"## Quill PR Review — {review['verdict']}",
        "",
    ]
    if review["summary"]:
        lines.extend([review["summary"], ""])
    if not findings:
        lines.append(
            "✅ Checked by Quill Pull Request Reviewer. "
            f"No CRITICAL or MAJOR findings remain at `{ctx.pr_head_sha[:12]}`."
        )
    for finding in findings:
        lines.extend(
            [
                f"### {finding['severity']} · {finding['id']} · {finding['title']}",
                f"- **Requirement:** {finding['requirement']}",
                f"- **Evidence:** {finding['evidence']}",
                f"- **Failure scenario:** {finding['failure_scenario']}",
                f"- **Impact:** {finding['impact']}",
                f"- **Required outcome:** {finding['required_outcome']}",
                "",
            ]
        )
    lines.append(f"Reviewed PR head `{ctx.pr_head_sha[:12]}` by Quill run `{ctx.run_id}`.")
    return "\n".join(lines)


def pr_review_digest(review: dict[str, Any]) -> str:
    """Stable identity for one normalized PR-review decision and its blocking findings."""
    canonical = json.dumps(review, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def step_acknowledge_pr_feedback(
    ctx: RunContext, phase: PhaseDef, *, spawn: SpawnPhase
) -> PhaseResult:
    """Post one idempotent receipt after the updated PR and CI agree on the pushed SHA."""
    git = ctx.deps.git
    if git is None or ctx.pr_number is None:
        return PhaseResult(Outcome.PASS, "feedback acknowledgement skipped")
    marker = f"<!-- quill-update:{ctx.run_id} -->"
    result_path = ctx.run_dir / "pr-feedback-result.json"
    try:
        previous = json.loads(result_path.read_text(encoding="utf-8"))
        if previous.get("run_id") == ctx.run_id and not previous.get("warnings"):
            return PhaseResult(Outcome.PASS, "feedback already acknowledged for this run")
    except (OSError, ValueError, AttributeError):
        pass
    result: dict[str, object] = {"run_id": ctx.run_id, "pr": ctx.pr_number, "items": []}
    try:
        pushed = git.local_head_sha()
        remote = git.pr_head_sha(ctx.pr_number)
        if pushed != remote:
            return PhaseResult(
                Outcome.FAILED,
                f"PR head {remote[:12]} does not match pushed commit {pushed[:12]}",
            )
        already = any(
            marker in str(comment.get("body") or "") for comment in git.pr_comments(ctx.pr_number)
        )
        if not already:
            ids = ", ".join(f"`{item}`" for item in ctx.feedback_ids)
            git.post_pr_comment(
                ctx.pr_number,
                f"{marker}\nQuill addressed feedback {ids} in `{pushed[:12]}`; local gates and CI passed.",
            )
        resolved: list[str] = []
        warnings: list[str] = []
        for thread_id in ctx.feedback_threads:
            try:
                git.reply_review_thread(
                    thread_id, f"Addressed by Quill run {ctx.run_id} in {pushed[:12]}."
                )
                git.resolve_review_thread(thread_id)
                resolved.append(thread_id)
            except GitError as exc:
                warnings.append(f"{thread_id}: {exc}")
        result.update(
            {
                "commit": pushed,
                "acknowledged_ids": list(ctx.feedback_ids),
                "resolved_threads": resolved,
                "warnings": warnings,
                "commented": not already,
            }
        )
        result_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (GitError, OSError) as exc:
        result["warning"] = str(exc)
        try:
            result_path.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError:
            pass
        return PhaseResult(Outcome.PASS, f"code shipped; feedback acknowledgement warning: {exc}")
    if warnings:
        return PhaseResult(
            Outcome.PASS,
            f"code shipped at {pushed[:12]}; feedback acknowledgement warnings: "
            + "; ".join(warnings),
        )
    return PhaseResult(Outcome.PASS, f"feedback acknowledged at {pushed[:12]}")


#: Built-in step registry, keyed by the ``step`` name a mechanical phase references.
MECHANICAL_STEPS: dict[str, Callable[..., PhaseResult]] = {
    "build": step_build_test,
    "test": step_build_test,
    "build_test": step_build_test,
    "ci_check": step_ci_check,
    "pr_head_guard": step_pr_head_guard,
    "acknowledge_pr_feedback": step_acknowledge_pr_feedback,
    "collect_pr_evidence": step_collect_pr_evidence,
    "publish_pr_review": step_publish_pr_review,
}


def run_mechanical(ctx: RunContext, phase: PhaseDef, *, spawn: SpawnPhase) -> PhaseResult:
    """Dispatch a mechanical phase to its built-in step (validated to exist at config load)."""
    step = MECHANICAL_STEPS[phase.step or ""]
    return step(ctx, phase, spawn=spawn)


# -- build/test runner (used by the CLI/API to wire ctx.deps.build_test) ----------


class _BuildTestRunner:
    """Run local verification commands and expose immediate process-tree cancellation."""

    def __init__(self, directory: str) -> None:
        self._directory = directory
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._active: subprocess.Popen[str] | None = None

    def __call__(self, config: QuillfolioConfig, selection: str) -> tuple[bool, str]:
        build_command = config.build_command
        test_command = config.test_command
        log_dir = config.log_dir
        commands = {
            "build": (build_command,),
            "test": (test_command,),
            "build_test": (build_command, test_command),
        }[selection]
        parts: list[str] = []
        for cmd in commands:
            if self._cancelled.is_set():
                parts.append(f"$ {cmd}\nterminated by stop request")
                log = "\n".join(parts)
                _write_test_log(Path(self._directory), log_dir, log)
                return False, log
            proc = subprocess.Popen(
                cmd,
                cwd=self._directory,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=os.name != "nt",
            )
            with self._lock:
                self._active = proc
                cancelled = self._cancelled.is_set()
            if cancelled:
                _terminate_verification_process(proc)
            stdout, stderr = proc.communicate()
            with self._lock:
                if self._active is proc:
                    self._active = None
            parts.append(f"$ {cmd}\n{stdout}{stderr}")
            if proc.returncode != 0 or self._cancelled.is_set():
                if self._cancelled.is_set():
                    parts.append("terminated by stop request")
                log = "\n".join(parts)
                _write_test_log(Path(self._directory), log_dir, log)
                return False, log
        log = "\n".join(parts)
        _write_test_log(Path(self._directory), log_dir, log)
        return True, log

    def cancel(self) -> None:
        """Stop the active shell and every child it launched."""
        self._cancelled.set()
        with self._lock:
            proc = self._active
        if proc is not None:
            _terminate_verification_process(proc)


def _terminate_verification_process(proc: subprocess.Popen[str]) -> None:
    """Terminate a verification process group, then force-kill it if it ignores SIGTERM."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    def force_kill() -> None:
        try:
            proc.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    threading.Thread(target=force_kill, daemon=True).start()


def build_test_runner(directory: str) -> Callable[..., tuple[bool, str]]:
    """Run the selected local verification command(s) in ``directory``.

    ``selection`` is ``build``, ``test``, or the compatibility ``build_test`` pair. Returns
    ``(ok, log)``; the captured output is also written to ``<log_dir>/test-log.txt`` for the revise
    step to read. Any non-zero exit means not ok.
    """
    return _BuildTestRunner(directory)


def _write_test_log(directory: Path, log_dir: str, log: str) -> None:
    out = directory / log_dir
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / "test-log.txt").write_text(log, encoding="utf-8")
    except OSError:
        pass  # logging is best-effort; the gate decision already happened


# -- text + parsing ---------------------------------------------------------------


def _title_from(issue_body: str) -> str | None:
    """Pull a title from `gh issue view --json title,body` output (JSON or first line)."""
    try:
        data = json.loads(issue_body)
        title = data.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    except ValueError:
        pass
    first = issue_body.strip().splitlines()
    return first[0].strip() if first else None


def body_text_from(issue_body: str) -> str:
    """Pull the body text from `gh issue view --json title,body` output.

    ``issue_body`` is the raw JSON `gh` returns; this returns the ``body`` field as prose. Non-JSON
    input (or a missing field) is returned unchanged so callers always get something to inject.
    """
    try:
        data = json.loads(issue_body)
    except ValueError:
        return issue_body.strip()
    body = data.get("body")
    return body.strip() if isinstance(body, str) else issue_body.strip()


__all__ = [
    "MECHANICAL_STEPS",
    "build_test_runner",
    "run_mechanical",
    "step_build_test",
    "step_ci_check",
]
