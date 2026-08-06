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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quill.config import PhaseDef, QuillfolioConfig
from quill.git_ops import ChecksStatus, GitError, GitOps
from quill.phases import Outcome, PhaseResult
from quill.contracts import ContractError, ContractStatus, file_sha256, repository_identity
from quill.runctx import CommandResult, MechanicalEvidence, RunContext, VerificationResult

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


def _record_mechanical(
    ctx: RunContext,
    phase: PhaseDef,
    status: ContractStatus,
    payload: object,
    *artifacts: str,
) -> None:
    ctx.mechanical_evidence[phase.id] = MechanicalEvidence(status, payload, tuple(artifacts))


def step_build_test(ctx: RunContext, phase: PhaseDef, *, spawn: SpawnPhase) -> PhaseResult:
    """Run build then test; PASS/BLOCK so the engine's gate can revise→verify on failure.

    On BLOCK, the build/test output is also written to ``<run-dir>/build-findings.md`` so the impl
    revise can read exactly what failed (compiler errors, failing test names) instead of retrying
    blind — the same "pass the findings into the retry" contract the reviewer gates have.
    """
    if ctx.deps.build_test is None:
        ctx.mechanical_evidence[phase.id] = MechanicalEvidence(
            ContractStatus.UNAVAILABLE,
            {"selection": phase.step or "build_test", "commands": []},
        )
        return PhaseResult(Outcome.FAILED, "build/test unavailable (no runner configured)")
    try:
        observed = _coerce_verification_result(
            ctx,
            phase.step or "build_test",
            ctx.deps.build_test(ctx.config, phase.step or "build_test"),
        )
        payload, artifacts = _persist_verification_evidence(ctx, phase, observed)
        if not observed.ok:
            _write_build_findings(ctx, observed.combined_log)
            artifacts = (*artifacts, BUILD_FINDINGS_NAME)
    except (OSError, TypeError, ValueError, ContractError) as exc:
        ctx.mechanical_evidence[phase.id] = MechanicalEvidence(
            ContractStatus.UNAVAILABLE,
            {"selection": phase.step or "build_test", "commands": []},
        )
        return PhaseResult(Outcome.FAILED, f"could not persist build/test evidence: {exc}")
    ctx.mechanical_evidence[phase.id] = MechanicalEvidence(
        ContractStatus.COMPLETE,
        payload,
        artifacts,
    )
    return PhaseResult(
        Outcome.PASS if observed.ok else Outcome.BLOCK,
        observed.combined_log,
    )


def _write_build_findings(ctx: RunContext, log: str) -> None:
    """Persist the failing build/test log as the run-dir findings file for the impl revise."""
    path = ctx.run_dir / BUILD_FINDINGS_NAME
    path.write_text(
        "# Build / test failure — fix these before the next build\n\n"
        "The build or test suite failed. Treat every compiler error and failing test below as a "
        "CRITICAL finding: the code must compile and all tests must pass. Fix the root cause in "
        "the source; do not delete or weaken tests to make them pass.\n\n"
        "```\n" + log + "\n```\n",
        encoding="utf-8",
    )


def _coerce_verification_result(
    ctx: RunContext,
    selection: str,
    value: VerificationResult | tuple[bool, str],
) -> VerificationResult:
    """Temporary adapter for injected legacy fakes; production returns typed observations."""
    if isinstance(value, VerificationResult):
        if value.selection != selection:
            raise ValueError(
                f"verification runner returned selection {value.selection!r}, expected {selection!r}"
            )
        return value
    ok, log = value
    now = datetime.now(UTC).isoformat()
    commands = {
        "build": (ctx.config.build_command,),
        "test": (ctx.config.test_command,),
        "build_test": (ctx.config.build_command, ctx.config.test_command),
    }[selection]
    # A flattened legacy fake cannot prove per-command execution. Keep it useful to old callers
    # without fabricating a successful second command after a failure.
    observed = commands if ok else commands[:1]
    return VerificationResult(
        selection,
        tuple(
            CommandResult(command, 0 if ok else 1, False, False, now, now, log)
            for command in observed
        ),
    )


def _persist_verification_evidence(
    ctx: RunContext, phase: PhaseDef, result: VerificationResult
) -> tuple[dict[str, object], tuple[str, ...]]:
    rows: list[dict[str, object]] = []
    artifacts: list[str] = []
    head, fingerprint = repository_identity(ctx.directory)
    for index, command in enumerate(result.commands, 1):
        name = f"{phase.id}-{result.selection}-command-{index}.log"
        path = ctx.run_dir / name
        path.write_text(command.output, encoding="utf-8")
        artifacts.append(name)
        rows.append(
            {
                "command": command.command,
                # JSON contract uses -1 for "no process exit" (cancel/timeout); the accompanying
                # booleans retain the exact reason without requiring a union-typed schema.
                "exit_code": command.exit_code if command.exit_code is not None else -1,
                "cancelled": command.cancelled,
                "timed_out": command.timed_out,
                "started_at": command.started_at,
                "ended_at": command.ended_at,
                "log": name,
                "log_sha256": file_sha256(path),
            }
        )
    payload: dict[str, object] = {
        "selection": result.selection,
        "commands": rows,
    }
    if head is not None:
        payload["source_sha"] = head
    if fingerprint is not None:
        payload["worktree_fingerprint"] = fingerprint
    return payload, tuple(artifacts)


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
        _record_mechanical(
            ctx,
            phase,
            ContractStatus.UNAVAILABLE,
            {"pr": 0, "head_sha": "", "link_action": "unavailable", "checks": []},
        )
        return PhaseResult(Outcome.FAILED, "ci check unavailable (no GitHub reader wired)")

    pr_number = _resolve_pr_number(ctx)
    if pr_number is None:
        _record_mechanical(
            ctx,
            phase,
            ContractStatus.UNAVAILABLE,
            {"pr": 0, "head_sha": "", "link_action": "unavailable", "checks": []},
        )
        return PhaseResult(
            Outcome.FAILED,
            f"no open PR found for ticket {ctx.ticket} — a ci_check phase must run after the "
            "phase that pushes and opens the PR.",
        )

    try:
        link_action = git.ensure_pr_closes_ticket(pr_number, ctx.ticket)
    except GitError as exc:
        _record_mechanical(
            ctx,
            phase,
            ContractStatus.UNAVAILABLE,
            {"pr": pr_number, "head_sha": "", "link_action": "failed", "checks": []},
        )
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
            _record_mechanical(
                ctx,
                phase,
                ContractStatus.UNAVAILABLE,
                {"pr": pr_number, "head_sha": "", "link_action": link_action, "checks": []},
            )
            return PhaseResult(Outcome.CRASH, f"could not read PR #{pr_number} checks: {exc}")

        if status.settled:
            break
        # One reading per iteration: two calls would let the deadlines be compared against
        # different instants, and makes the loop untestable against a scripted clock.
        now = _monotonic()
        if not status.reported and now >= no_checks_deadline:
            _record_mechanical(
                ctx,
                phase,
                ContractStatus.UNAVAILABLE,
                {"pr": pr_number, "head_sha": "", "link_action": link_action, "checks": []},
            )
            return PhaseResult(
                Outcome.FAILED,
                f"PR #{pr_number} reported no CI checks within "
                f"{no_checks_deadline - started:g}s — does this repo run workflows on "
                "pull requests?",
            )
        if now >= deadline:
            waiting = ", ".join(c.name for c in status.pending) or "unknown"
            _record_mechanical(
                ctx,
                phase,
                ContractStatus.UNAVAILABLE,
                {
                    "pr": pr_number,
                    "head_sha": "",
                    "link_action": link_action,
                    "checks": [_check_payload(check) for check in status.checks],
                },
            )
            return PhaseResult(
                Outcome.FAILED,
                f"PR #{pr_number} CI did not finish within {ctx.config.ci_seconds:g}s "
                f"(still running: {waiting}).",
            )
        _sleep(CI_POLL_INTERVAL)

    head_sha = ctx.pr_head_sha
    if phase.produces_contract or head_sha:
        try:
            head_sha = git.pr_head_sha(pr_number)
        except GitError as exc:
            _record_mechanical(
                ctx,
                phase,
                ContractStatus.UNAVAILABLE,
                {"pr": pr_number, "head_sha": "", "link_action": link_action, "checks": []},
            )
            return PhaseResult(Outcome.CRASH, f"could not read PR #{pr_number} head: {exc}")
    captured_at = datetime.now(UTC).isoformat()
    checks = [_check_payload(check) for check in status.checks]
    artifacts: tuple[str, ...] = ()
    if not status.failed:
        _record_mechanical(
            ctx,
            phase,
            ContractStatus.COMPLETE,
            {
                "pr": pr_number,
                "head_sha": head_sha,
                "link_action": link_action,
                "captured_at": captured_at,
                "checks": checks,
            },
        )
        names = ", ".join(c.name for c in status.checks)
        return PhaseResult(Outcome.PASS, f"{link_action}; CI green on PR #{pr_number} ({names})")

    try:
        artifacts = _write_ci_findings(ctx, git, pr_number, status, checks)
    except (OSError, GitError, ContractError) as exc:
        _record_mechanical(
            ctx,
            phase,
            ContractStatus.UNAVAILABLE,
            {
                "pr": pr_number,
                "head_sha": head_sha,
                "link_action": link_action,
                "captured_at": captured_at,
                "checks": checks,
            },
        )
        return PhaseResult(Outcome.FAILED, f"could not persist CI failure evidence: {exc}")
    _record_mechanical(
        ctx,
        phase,
        ContractStatus.COMPLETE,
        {
            "pr": pr_number,
            "head_sha": head_sha,
            "link_action": link_action,
            "captured_at": captured_at,
            "checks": checks,
        },
        *artifacts,
    )
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


def _check_payload(check: Any) -> dict[str, object]:
    return {
        "name": check.name,
        "state": check.state,
        "status": getattr(check, "status", ""),
        "conclusion": getattr(check, "conclusion", ""),
        "url": check.url,
        "run_id": check.run_id or "",
    }


def _write_ci_findings(
    ctx: RunContext,
    git: GitOps,
    pr_number: int,
    status: ChecksStatus,
    checks: list[dict[str, object]],
) -> tuple[str, ...]:
    """Persist the failing checks (and their logs) for the revise sequence to read."""
    sections = [
        "# CI failure — fix these before the next push\n",
        (
            f"The CI run for PR #{pr_number} failed. Treat every failure below as a CRITICAL "
            "finding: fix the root cause in the source. Do not delete, skip, or weaken tests to "
            "make them pass.\n"
        ),
    ]
    artifacts: list[str] = []
    for index, check in enumerate(status.failed, 1):
        sections.append(f"\n## {check.name}\n")
        if check.url:
            sections.append(f"\n{check.url}\n")
        run_id = check.run_id
        log = git.failed_check_log(run_id) if run_id else ""
        log_name = f"ci-failure-{index}.log"
        log_path = ctx.run_dir / log_name
        log_path.write_text(log, encoding="utf-8")
        artifacts.append(log_name)
        for payload in checks:
            if payload["name"] == check.name and payload["url"] == check.url:
                payload["failure_log"] = log_name
                payload["failure_log_sha256"] = file_sha256(log_path)
                break
        sections.append(f"\n```\n{log or '(no log available)'}\n```\n")
    (ctx.run_dir / CI_FINDINGS_NAME).write_text("".join(sections), encoding="utf-8")
    return (CI_FINDINGS_NAME, *artifacts)


def step_pr_head_guard(ctx: RunContext, phase: PhaseDef, *, spawn: SpawnPhase) -> PhaseResult:
    """Stop before push when somebody moved the existing PR head during this run."""
    git = ctx.deps.git
    if git is None or ctx.pr_number is None or not ctx.pr_head_sha:
        _record_mechanical(
            ctx,
            phase,
            ContractStatus.UNAVAILABLE,
            {"pr": ctx.pr_number or 0, "expected": ctx.pr_head_sha, "observed": "", "matches": False},
        )
        return PhaseResult(Outcome.FAILED, "update head guard is missing PR boundary metadata")
    try:
        current = git.pr_head_sha(ctx.pr_number)
    except GitError as exc:
        _record_mechanical(
            ctx,
            phase,
            ContractStatus.UNAVAILABLE,
            {"pr": ctx.pr_number, "expected": ctx.pr_head_sha, "observed": "", "matches": False},
        )
        return PhaseResult(Outcome.CRASH, f"could not verify PR head: {exc}")
    if current != ctx.pr_head_sha:
        _record_mechanical(
            ctx,
            phase,
            ContractStatus.COMPLETE,
            {"pr": ctx.pr_number, "expected": ctx.pr_head_sha, "observed": current, "matches": False},
        )
        return PhaseResult(
            Outcome.NEEDS_DECISION,
            f"PR #{ctx.pr_number} moved from {ctx.pr_head_sha[:12]} to {current[:12]}; "
            "halt and restart the update against the new head.",
            question="The PR changed during this run. Stop and restart from its new head?",
        )
    _record_mechanical(
        ctx,
        phase,
        ContractStatus.COMPLETE,
        {"pr": ctx.pr_number, "expected": ctx.pr_head_sha, "observed": current, "matches": True},
    )
    return PhaseResult(Outcome.PASS, f"PR head unchanged at {current[:12]}")


def step_collect_pr_evidence(ctx: RunContext, phase: PhaseDef, *, spawn: SpawnPhase) -> PhaseResult:
    """Run local test and build commands, preserving both outcomes for semantic reviewers."""
    sections = ["# Pull request mechanical verification"]
    if ctx.deps.build_test is None:
        _record_mechanical(
            ctx,
            phase,
            ContractStatus.UNAVAILABLE,
            {"results": []},
        )
        return PhaseResult(Outcome.FAILED, "local PR verification is unavailable (no runner)")
    else:
        results: list[dict[str, object]] = []
        evidence_artifacts: list[str] = []
        try:
            for selection in ("test", "build"):
                observed = _coerce_verification_result(
                    ctx,
                    selection,
                    ctx.deps.build_test(ctx.config, selection),
                )
                payload, artifacts = _persist_verification_evidence(ctx, phase, observed)
                results.append(payload)
                evidence_artifacts.extend(artifacts)
                log = observed.combined_log
                clipped = log if len(log) <= 30_000 else log[:30_000] + "\n… output truncated …"
                sections.append(
                    f"\n## {selection.title()} — {'PASS' if observed.ok else 'FAIL'}\n\n```text\n"
                    f"{clipped}\n```\n"
                )
        except (OSError, TypeError, ValueError, ContractError) as exc:
            _record_mechanical(ctx, phase, ContractStatus.UNAVAILABLE, {"results": results})
            return PhaseResult(Outcome.FAILED, f"could not capture PR verification evidence: {exc}")
    try:
        (ctx.run_dir / (phase.artifact or PR_EVIDENCE_NAME)).write_text(
            "".join(sections), encoding="utf-8"
        )
    except OSError as exc:
        _record_mechanical(ctx, phase, ContractStatus.UNAVAILABLE, {"results": []})
        return PhaseResult(Outcome.FAILED, f"could not write PR verification evidence: {exc}")
    _record_mechanical(
        ctx,
        phase,
        ContractStatus.COMPLETE,
        {"results": results},
        phase.artifact or PR_EVIDENCE_NAME,
        *evidence_artifacts,
    )
    return PhaseResult(Outcome.PASS, "captured local test and build evidence")


def step_publish_pr_review(ctx: RunContext, phase: PhaseDef, *, spawn: SpawnPhase) -> PhaseResult:
    """Publish the validated review; a clean PASS merges and removes the remote branch."""
    git = ctx.deps.git
    if git is None or ctx.pr_number is None or not ctx.pr_head_sha:
        _record_mechanical(
            ctx,
            phase,
            ContractStatus.UNAVAILABLE,
            {
                "pr": ctx.pr_number or 0,
                "head_sha": ctx.pr_head_sha,
                "review_digest": "",
                "comment_action": "unavailable",
                "merge_action": "unavailable",
                "pr_url": ctx.pr_url or "",
            },
        )
        return PhaseResult(Outcome.FAILED, "PR review publication is missing PR metadata")
    try:
        current = git.pr_head_sha(ctx.pr_number)
        if current != ctx.pr_head_sha:
            _record_mechanical(
                ctx,
                phase,
                ContractStatus.UNAVAILABLE,
                {
                    "pr": ctx.pr_number,
                    "head_sha": current,
                    "review_digest": "",
                    "comment_action": "stale-head",
                    "merge_action": "not-requested",
                    "pr_url": ctx.pr_url or "",
                },
            )
            return PhaseResult(
                Outcome.FAILED,
                f"PR #{ctx.pr_number} moved from {ctx.pr_head_sha[:12]} to {current[:12]}; "
                "discard this stale review and restart",
            )
        dirty = git.workspace_status().strip()
        if dirty:
            _record_mechanical(
                ctx,
                phase,
                ContractStatus.UNAVAILABLE,
                {
                    "pr": ctx.pr_number,
                    "head_sha": current,
                    "review_digest": "",
                    "comment_action": "dirty-workspace",
                    "merge_action": "not-requested",
                    "pr_url": ctx.pr_url or "",
                },
            )
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
                pr_checks_required=ctx.config.pr_checks_required,
            )
    except (GitError, OSError, ValueError, KeyError, TypeError) as exc:
        _record_mechanical(
            ctx,
            phase,
            ContractStatus.UNAVAILABLE,
            {
                "pr": ctx.pr_number,
                "head_sha": ctx.pr_head_sha,
                "review_digest": "",
                "comment_action": "failed",
                "merge_action": "failed",
                "pr_url": ctx.pr_url or "",
            },
        )
        return PhaseResult(Outcome.FAILED, f"could not publish PR review: {exc}")
    _record_mechanical(
        ctx,
        phase,
        ContractStatus.COMPLETE,
        {
            "pr": ctx.pr_number,
            "head_sha": ctx.pr_head_sha,
            "review_digest": pr_review_digest(review),
            "comment_action": action,
            "merge_action": str(merge_action or "not-requested"),
            "pr_url": ctx.pr_url or "",
        },
    )
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
        _record_mechanical(
            ctx,
            phase,
            ContractStatus.UNAVAILABLE,
            {
                "pr": ctx.pr_number or 0,
                "commit": "",
                "acknowledged_ids": [],
                "resolved_threads": [],
                "warnings": ["GitHub integration unavailable"],
                "commented": False,
            },
        )
        return PhaseResult(Outcome.FAILED, "feedback acknowledgement unavailable")
    marker = f"<!-- quill-update:{ctx.run_id} -->"
    result_path = ctx.run_dir / "pr-feedback-result.json"
    try:
        previous = json.loads(result_path.read_text(encoding="utf-8"))
        if previous.get("run_id") == ctx.run_id and not previous.get("warnings"):
            _record_mechanical(
                ctx,
                phase,
                ContractStatus.COMPLETE,
                {
                    "pr": int(previous["pr"]),
                    "commit": str(previous["commit"]),
                    "acknowledged_ids": list(previous.get("acknowledged_ids", [])),
                    "resolved_threads": list(previous.get("resolved_threads", [])),
                    "warnings": [],
                    "commented": bool(previous.get("commented")),
                },
                "pr-feedback-result.json",
            )
            return PhaseResult(Outcome.PASS, "feedback already acknowledged for this run")
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass
    result: dict[str, object] = {"run_id": ctx.run_id, "pr": ctx.pr_number, "items": []}
    pushed = ""
    resolved: list[str] = []
    warnings: list[str] = []
    already = True
    try:
        pushed = git.local_head_sha()
        remote = git.pr_head_sha(ctx.pr_number)
        if pushed != remote:
            _record_mechanical(
                ctx,
                phase,
                ContractStatus.UNAVAILABLE,
                {
                    "pr": ctx.pr_number,
                    "commit": pushed,
                    "acknowledged_ids": [],
                    "resolved_threads": [],
                    "warnings": ["remote head mismatch"],
                    "commented": False,
                },
            )
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
        payload = {
            "pr": ctx.pr_number,
            "commit": pushed,
            "acknowledged_ids": list(ctx.feedback_ids),
            "resolved_threads": resolved,
            "warnings": [str(exc)],
            "commented": bool(result.get("commented")),
        }
        artifacts = ("pr-feedback-result.json",) if result_path.is_file() else ()
        _record_mechanical(ctx, phase, ContractStatus.PARTIAL, payload, *artifacts)
        return PhaseResult(Outcome.PASS, f"code shipped; feedback acknowledgement warning: {exc}")
    payload = {
        "pr": ctx.pr_number,
        "commit": pushed,
        "acknowledged_ids": list(ctx.feedback_ids),
        "resolved_threads": resolved,
        "warnings": warnings,
        "commented": not already,
    }
    _record_mechanical(
        ctx,
        phase,
        ContractStatus.PARTIAL if warnings else ContractStatus.COMPLETE,
        payload,
        "pr-feedback-result.json",
    )
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

    def __call__(self, config: QuillfolioConfig, selection: str) -> VerificationResult:
        build_command = config.build_command
        test_command = config.test_command
        log_dir = config.log_dir
        commands = {
            "build": (build_command,),
            "test": (test_command,),
            "build_test": (build_command, test_command),
        }[selection]
        results: list[CommandResult] = []
        for cmd in commands:
            started = datetime.now(UTC).isoformat()
            if self._cancelled.is_set():
                results.append(
                    CommandResult(
                        cmd,
                        None,
                        True,
                        False,
                        started,
                        datetime.now(UTC).isoformat(),
                        "terminated by stop request",
                    )
                )
                observed = VerificationResult(selection, tuple(results))
                _write_test_log(Path(self._directory), log_dir, observed.combined_log)
                return observed
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
            cancelled = self._cancelled.is_set()
            results.append(
                CommandResult(
                    cmd,
                    proc.returncode,
                    cancelled,
                    False,
                    started,
                    datetime.now(UTC).isoformat(),
                    stdout + stderr + ("\nterminated by stop request" if cancelled else ""),
                )
            )
            if proc.returncode != 0 or self._cancelled.is_set():
                observed = VerificationResult(selection, tuple(results))
                _write_test_log(Path(self._directory), log_dir, observed.combined_log)
                return observed
        observed = VerificationResult(selection, tuple(results))
        _write_test_log(Path(self._directory), log_dir, observed.combined_log)
        return observed

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


def build_test_runner(directory: str) -> Callable[..., VerificationResult]:
    """Run the selected local verification command(s) in ``directory``.

    ``selection`` is ``build``, ``test``, or the compatibility ``build_test`` pair. Returns
    a typed result; captured output is also written to ``<log_dir>/test-log.txt`` for humans.
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
