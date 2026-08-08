"""Validated reviewer findings and deterministic gate decisions."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from quill.phases import Outcome, PhaseResult

BLOCKING_SEVERITIES = frozenset({"CRITICAL", "MAJOR"})
SEVERITIES = BLOCKING_SEVERITIES | {"MINOR", "NIT"}
STATUSES = frozenset({"OPEN", "RESOLVED"})

#: How a gate treats blocking severities on a *retry* round.
#:
#: ``same``        — a retry blocks exactly like the initial review (historic behavior).
#: ``repeat-only`` — on a retry, a non-CRITICAL blocker must already have been reported in an
#:                   earlier round. Late discovery is advisory instead of consuming another retry.
RETRY_MODES = frozenset({"same", "repeat-only"})


@dataclass(frozen=True, slots=True)
class Finding:
    """One schema-validated finding."""

    id: str
    severity: str
    status: str
    title: str
    requirement: str
    evidence: str
    failure_scenario: str
    required_outcome: str
    owner: str | None = None
    introduced_by_revision: str | None = None
    escalation_reason: str | None = None  # set by gate when escalating a decision to planning

    @property
    def blocks(self) -> bool:
        """Severity-only blocking test, independent of any round.

        This is the identity question ("is this the kind of finding that stops a gate?") used by
        contract merging, prompt assembly, and blocker memory. Gate routing uses
        :meth:`BlockingPolicy.blocks_at`, which additionally accounts for the round.
        """
        return self.status == "OPEN" and self.severity in BLOCKING_SEVERITIES


@dataclass(frozen=True, slots=True)
class BlockingPolicy:
    """Which findings stop a gate, as a function of the revise round.

    A gate that applies one fixed severity set to every round cannot be guaranteed to terminate:
    reviewers re-reading revised code always find *something* new, so each round can replace the
    blockers the producer just fixed with fresh ones of the same severity. Observed directly on
    ticket #19, where four consecutive rounds resolved every prior blocker and were each blocked by
    three brand-new MAJOR findings whose IDs never repeated.

    ``repeat-only`` closes that loop by making late discovery cost a round before it can spend one.
    After the initial review a non-CRITICAL finding may block only when it was already reported in
    an earlier round, so a reviewer cannot replace the blockers just fixed with fresh ones and
    extend the loop indefinitely. It is *deferral*, not exclusion: a finding raised mid-loop is
    recorded and carried, and once the producer has been shown it and has not resolved it, it
    blocks like any other known defect. That is deliberate — ticket #20's round-1 side observations
    included a stated requirement with no test seam and an undefined integration contract, and both
    were fixed only because the following round could block on them. CRITICAL is exempt at every
    round, so a revision that breaks the build or crashes still stops the run.

    Termination therefore rests on ``final``, not on a shrinking blocking set: the last round the
    budget allows blocks on CRITICAL alone, so the loop always drains.

    The default reproduces historic behavior exactly; a repository opts in through ``[gates]``.
    """

    #: Severities that block the initial review (round 0).
    initial: frozenset[str] = BLOCKING_SEVERITIES
    #: How retry rounds treat those severities (see :data:`RETRY_MODES`).
    retry_mode: str = "same"
    #: Severities that block the last available round, when the budget is known.
    final: frozenset[str] = BLOCKING_SEVERITIES

    def blocks_at(
        self,
        finding: Finding,
        *,
        round_index: int = 0,
        carried_ids: frozenset[str] = frozenset(),
        final_round: bool = False,
    ) -> bool:
        """Whether ``finding`` stops the gate on this round.

        ``carried_ids`` are the finding IDs the gate already held *before* this round ran — not the
        IDs produced during it. Reviewers re-audited inside a retry route write fresh findings that
        are merged into the verification contract before the gate evaluates, so deriving the repeat
        test from the contract itself would let every new finding trivially qualify as a repeat.
        """
        if finding.status != "OPEN":
            return False
        if round_index <= 0:
            return finding.severity in self.initial
        allowed = self.final if final_round else self.initial
        if finding.severity not in allowed:
            return False
        if self.retry_mode == "repeat-only" and finding.severity != "CRITICAL":
            return _matches_any_id(finding.id, carried_ids)
        return True


#: Historic behavior: CRITICAL/MAJOR block every round. Repositories opt into a converging gate.
DEFAULT_BLOCKING_POLICY = BlockingPolicy()


def load_findings(path: Path) -> tuple[Finding, ...]:
    """Load the strict findings contract or raise ``ValueError`` with an actionable reason."""
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid findings JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("findings must be an object with schema_version 1")
    rows = raw.get("findings")
    if not isinstance(rows, list):
        raise ValueError("findings must be an array")
    findings: list[Finding] = []
    seen: set[str] = set()
    required = (
        "id",
        "severity",
        "status",
        "title",
        "requirement",
        "evidence",
        "failure_scenario",
        "required_outcome",
    )
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"finding #{index + 1} must be an object")
        values: dict[str, str] = {}
        for field in required:
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"finding #{index + 1} has invalid {field}")
            values[field] = value.strip()
        values["severity"] = values["severity"].upper()
        values["status"] = values["status"].upper()
        if values["severity"] not in SEVERITIES:
            raise ValueError(f"finding {values['id']} has invalid severity")
        if values["status"] not in STATUSES:
            raise ValueError(f"finding {values['id']} has invalid status")
        if values["id"] in seen:
            raise ValueError(f"duplicate finding id {values['id']}")
        seen.add(values["id"])
        optional: dict[str, str | None] = {}
        for field in ("owner", "introduced_by_revision", "escalation_reason"):
            value = row.get(field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"finding #{index + 1} has invalid {field}")
            optional[field] = value.strip() if isinstance(value, str) else None
        findings.append(
            Finding(
                **values,
                owner=optional["owner"],
                introduced_by_revision=optional["introduced_by_revision"],
                escalation_reason=optional["escalation_reason"],
            )
        )
    return tuple(findings)


#: Keys that mark a verification artifact as the status-delta shape rather than a full array.
_DELTA_KEYS = ("dispositions", "new_findings")


def _is_verification_delta(raw: object) -> bool:
    return isinstance(raw, dict) and any(key in raw for key in _DELTA_KEYS)


def materialize_verification_delta(
    path: Path,
    prior: tuple[Finding, ...],
    *,
    require_delta: bool = False,
) -> None:
    """Rewrite a delta-shaped reconciliation artifact into the canonical findings array.

    A verification pass used to be required to re-emit every prior finding verbatim — nine string
    fields per finding, byte-identical, or the gate returned GARBAGE and discarded the run. That is
    a transcription task, and it is where small models actually fail: six of ticket #13-#19's hard
    failures were ``changed identity field(s)``/``omitted prior blocking finding(s)``, none of which
    described a real defect in the code under review.

    Quill already holds the prior findings in memory, so the model is asked only for what it
    genuinely knows — a status plus evidence per prior ID, and any genuinely new finding — and Quill
    reconstructs the authoritative artifact itself. Identity is never re-emitted, so it cannot drift.

    A full-array artifact passes through untouched for legacy direct callers unless
    ``require_delta`` is set. Contract projection paths require the delta so a model cannot reopen
    the immutable-field transcription failure by ignoring its requested schema. Raises
    :class:`ValueError` with an actionable reason.
    """
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid findings JSON: {exc}") from exc
    if not _is_verification_delta(raw):
        if require_delta:
            raise ValueError(
                "findings projection must use dispositions/new_findings delta; "
                "do not re-emit prior findings"
            )
        return
    if raw.get("schema_version") != 1:
        raise ValueError("findings must be an object with schema_version 1")

    carried = {finding.id: finding for finding in prior}
    dispositions = raw.get("dispositions", [])
    if not isinstance(dispositions, list):
        raise ValueError("dispositions must be an array")
    seen_dispositions: set[str] = set()
    for index, row in enumerate(dispositions):
        if not isinstance(row, dict):
            raise ValueError(f"disposition #{index + 1} must be an object")
        finding_id = row.get("id")
        if not isinstance(finding_id, str) or finding_id.strip() not in carried:
            raise ValueError(f"disposition #{index + 1} names unknown finding {finding_id!r}")
        finding_id = finding_id.strip()
        if finding_id in seen_dispositions:
            raise ValueError(f"duplicate disposition for finding {finding_id}")
        seen_dispositions.add(finding_id)
        status = row.get("status")
        if not isinstance(status, str) or status.strip().upper() not in STATUSES:
            raise ValueError(f"disposition for {finding_id} has invalid status {status!r}")
        evidence = row.get("evidence")
        target = carried[finding_id]
        carried[finding_id] = replace(
            target,
            status=status.strip().upper(),
            evidence=(
                evidence.strip()
                if isinstance(evidence, str) and evidence.strip()
                else target.evidence
            ),
        )

    new_rows = raw.get("new_findings", [])
    if not isinstance(new_rows, list):
        raise ValueError("new_findings must be an array")
    # A prior finding with no disposition keeps the status it carried in. Quill holds the record, so
    # silence is not loss of information — it simply means the reviewer made no claim about it.
    payload = {
        "schema_version": 1,
        "findings": [_finding_payload(carried[finding.id]) for finding in prior] + new_rows,
    }
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def normalize_findings_namespace(path: Path, namespace: str) -> tuple[Finding, ...]:
    """Load findings and persist globally stable IDs for one reviewer lane.

    Reviewers naturally reuse local IDs such as ``F1``. A finalizer cannot reconcile three such
    files deterministically, so Quill—not the model—prefixes each ID with the configured lane or
    model namespace before the files become finalizer inputs.
    """
    findings = load_findings(path)
    prefix = f"{namespace}:"
    normalized = tuple(
        finding if finding.id.startswith(prefix) else replace(finding, id=f"{prefix}{finding.id}")
        for finding in findings
    )
    seen: set[str] = set()
    for finding in normalized:
        if finding.id in seen:
            raise ValueError(
                f"namespace {namespace!r} produces duplicate finding id {finding.id}; "
                "use unique lane-local IDs"
            )
        seen.add(finding.id)
    if normalized != findings:
        payload = {
            "schema_version": 1,
            "findings": [_finding_payload(finding) for finding in normalized],
        }
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return normalized


def _finding_payload(finding: Finding) -> dict[str, str]:
    """Serialize one finding without optional nulls rejected by the contract schema."""
    return {key: value for key, value in asdict(finding).items() if value is not None}


def deterministic_review_result(
    path: Path,
    receipt: PhaseResult,
    *,
    namespace: str | None = None,
) -> PhaseResult:
    """Validate an informational review artifact without treating its findings as a gate."""
    if receipt.outcome in (Outcome.CRASH, Outcome.FAILED, Outcome.NEEDS_DECISION):
        return receipt
    try:
        findings = (
            normalize_findings_namespace(path, namespace) if namespace else load_findings(path)
        )
    except (OSError, ValueError) as exc:
        return PhaseResult(Outcome.GARBAGE, str(exc), raw_receipt=receipt.raw_receipt)
    return PhaseResult(
        Outcome.DONE,
        f"validated {len(findings)} structured finding(s)",
        raw_receipt=receipt.raw_receipt,
    )


def deterministic_gate_result(
    path: Path,
    receipt: PhaseResult,
    *,
    prior: tuple[Finding, ...] = (),
    allowed_owners: tuple[str, ...] = (),
    policy: BlockingPolicy = DEFAULT_BLOCKING_POLICY,
    round_index: int = 0,
    carried_ids: frozenset[str] | None = None,
    final_round: bool = False,
    allow_escalation: bool = True,
) -> PhaseResult:
    """Compute PASS/BLOCK from findings; reject malformed or incomplete reconciliation.

    ``round_index`` is 0 for the initial review and 1-based for each revise round.
    ``carried_ids`` are the blocker IDs the gate held before this round; when omitted they are
    derived from ``prior`` (correct for the initial review, where nothing has been merged in yet).
    """
    # Process failures and explicit inability to complete remain authoritative. For every normal
    # completion shape—including a missing/malformed receipt—the persisted artifact is the gate's
    # source of truth. Small models routinely get the final line wrong after writing valid JSON;
    # routing must not depend on that prose.
    if receipt.outcome in (Outcome.CRASH, Outcome.FAILED, Outcome.NEEDS_DECISION):
        return receipt
    try:
        current = load_findings(path)
    except ValueError as exc:
        return PhaseResult(Outcome.GARBAGE, str(exc), raw_receipt=receipt.raw_receipt)

    known_ids = (
        carried_ids if carried_ids is not None else frozenset(finding.id for finding in prior)
    )

    def gates(finding: Finding) -> bool:
        return policy.blocks_at(
            finding,
            round_index=round_index,
            carried_ids=known_ids,
            final_round=final_round,
        )

    current_by_id = {finding.id: finding for finding in current}
    missing: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    for finding in prior:
        # Identity preservation is demanded only of findings that actually gate this round. A
        # finding the policy has already made advisory must not still be a mandatory prior
        # identity, or relaxing the gate would simply re-enter through the reconciliation check.
        if not gates(finding):
            continue
        exact = current_by_id.get(finding.id)
        if exact is not None:
            if changed := _changed_identity_fields(finding, exact):
                return PhaseResult(
                    Outcome.GARBAGE,
                    (
                        f"verification changed identity field(s) for prior blocking finding "
                        f"{finding.id}: {', '.join(changed)}"
                    ),
                    raw_receipt=receipt.raw_receipt,
                )
            continue
        aliases = [
            candidate
            for candidate in current
            if candidate.severity == finding.severity
            and _has_finding_id_suffix(candidate.id, finding.id)
        ]
        if len(aliases) == 1:
            if changed := _changed_identity_fields(finding, aliases[0]):
                return PhaseResult(
                    Outcome.GARBAGE,
                    (
                        f"verification changed identity field(s) for prior blocking finding "
                        f"{finding.id} via {aliases[0].id}: {', '.join(changed)}"
                    ),
                    raw_receipt=receipt.raw_receipt,
                )
            continue
        if aliases:
            ambiguous[finding.id] = [candidate.id for candidate in aliases]
        else:
            missing.append(finding.id)
    if ambiguous:
        details = "; ".join(
            f"{finding_id}: {', '.join(alias_ids)}" for finding_id, alias_ids in ambiguous.items()
        )
        return PhaseResult(
            Outcome.GARBAGE,
            f"verification ambiguously renamed prior blocking finding(s): {details}",
            raw_receipt=receipt.raw_receipt,
        )
    if missing:
        return PhaseResult(
            Outcome.GARBAGE,
            f"verification omitted prior blocking finding(s): {', '.join(missing)}",
            raw_receipt=receipt.raw_receipt,
        )

    prior_ids = {finding.id for finding in prior}
    for finding in current:
        if not gates(finding):
            continue
        if allowed_owners and finding.owner not in allowed_owners:
            allowed = ", ".join(allowed_owners)
            return PhaseResult(
                Outcome.GARBAGE,
                f"blocking finding {finding.id} must name owner from: {allowed}",
                raw_receipt=receipt.raw_receipt,
            )
        matches_prior = finding.id in prior_ids or any(
            _has_finding_id_suffix(finding.id, prior_id) for prior_id in prior_ids
        )
        if prior and not matches_prior and not finding.introduced_by_revision:
            return PhaseResult(
                Outcome.GARBAGE,
                (
                    f"new verification blocker {finding.id} must identify the exact revision "
                    "change that introduced it in introduced_by_revision"
                ),
                raw_receipt=receipt.raw_receipt,
            )

    blockers = [finding for finding in current if gates(finding)]
    if blockers:
        # Escalation: if every blocker carries an escalation_reason, the gate has determined
        # these are decision-points rather than defects. Route directly to planning.
        all_escalated = all(b.escalation_reason for b in blockers)
        if allow_escalation and all_escalated:
            summary = "; ".join(
                f"{finding.id} ({finding.severity}): {finding.title}" for finding in blockers
            )
            return PhaseResult(
                Outcome.ESCALATE,
                f"escalated to planning — {summary}",
                raw_receipt=receipt.raw_receipt,
            )
        summary = "; ".join(
            f"{finding.id} ({finding.severity}): {finding.title}" for finding in blockers
        )
        return PhaseResult(Outcome.BLOCK, summary, raw_receipt=receipt.raw_receipt)

    # Escalation: all prior blockers are now RESOLVED with an escalation_reason.
    # The gate has determined these are decision-points for planning, not defects for research.
    prior_blocks = {f.id for f in prior if f.blocks}
    if allow_escalation and prior_blocks:
        escalated_ids = {
            f.id
            for f in current
            if f.id in prior_blocks and f.status == "RESOLVED" and f.escalation_reason
        }
        if escalated_ids == prior_blocks:
            summary = "; ".join(
                f"{finding.id} ({finding.severity}): {finding.title}"
                for finding in current
                if finding.id in prior_blocks
            )
            return PhaseResult(
                Outcome.ESCALATE,
                f"escalated to planning — {summary}",
                raw_receipt=receipt.raw_receipt,
            )

    advisory = sum(finding.status == "OPEN" for finding in current)
    return PhaseResult(
        Outcome.PASS,
        f"no open CRITICAL/MAJOR findings ({advisory} advisory)",
        raw_receipt=receipt.raw_receipt,
    )


def merge_verification_findings(
    prior: tuple[Finding, ...], current: tuple[Finding, ...]
) -> tuple[Finding, ...]:
    """Build one collision-free verification contract from old and newly audited findings.

    A reviewer may reuse a stable ID for a substantively different defect on a later pass. The
    original identity must remain immutable, but dropping the new defect would make the gate
    unsound. Preserve the first identity and assign every conflicting identity a deterministic
    revision ID. Exact duplicates collapse to one OPEN contract until the finalizer independently
    verifies resolution.
    """
    merged: list[Finding] = []
    indexes: dict[str, int] = {}
    for finding in (*prior, *current):
        candidate = finding
        while (index := indexes.get(candidate.id)) is not None:
            existing = merged[index]
            if not _changed_identity_fields(existing, candidate):
                status = "OPEN" if existing.blocks or candidate.blocks else candidate.status
                merged[index] = replace(candidate, status=status)
                break
            candidate = replace(candidate, id=_revision_finding_id(finding, indexes))
        else:
            indexes[candidate.id] = len(merged)
            merged.append(candidate)
    return tuple(merged)


def _matches_any_id(candidate_id: str, known_ids: frozenset[str]) -> bool:
    """Whether ``candidate_id`` names a finding already in ``known_ids``.

    Lane namespacing (:func:`normalize_findings_namespace`) rewrites ``F1`` to ``tests:F1`` between
    rounds, so an exact-match test alone would report a carried finding as newly discovered.

    A ``:revision-`` ID from :func:`merge_verification_findings` deliberately does *not* match its
    base: that suffix is only ever minted when a later audit reused an existing ID for a defect with
    different identity fields, which is a new finding rather than a repeat of the original.
    """
    if candidate_id in known_ids:
        return True
    return any(_has_finding_id_suffix(candidate_id, known) for known in known_ids)


def _has_finding_id_suffix(candidate_id: str, prior_id: str) -> bool:
    """Whether ``candidate_id`` safely namespaces ``prior_id`` as its final segment."""
    if candidate_id == prior_id or not candidate_id.endswith(prior_id):
        return False
    prefix = candidate_id[: -len(prior_id)]
    return bool(prefix) and prefix[-1] in "-_:./"


def _changed_identity_fields(prior: Finding, current: Finding) -> tuple[str, ...]:
    """Immutable fields that make a finding ID refer to the same defect across verification."""
    fields = (
        "severity",
        "title",
        "requirement",
        "failure_scenario",
        "required_outcome",
        "owner",
    )
    return tuple(field for field in fields if getattr(prior, field) != getattr(current, field))


def _revision_finding_id(finding: Finding, indexes: dict[str, int]) -> str:
    """Return a stable unused ID for one reused-ID identity."""
    identity = "\0".join(
        (
            finding.id,
            finding.severity,
            finding.title,
            finding.requirement,
            finding.failure_scenario,
            finding.required_outcome,
        )
    )
    digest = sha256(identity.encode()).hexdigest()
    for length in range(10, len(digest) + 1, 2):
        candidate = f"{finding.id}:revision-{digest[:length]}"
        if candidate not in indexes:
            return candidate
    raise RuntimeError(f"could not allocate revision ID for finding {finding.id}")
