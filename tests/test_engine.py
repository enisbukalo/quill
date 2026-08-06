"""Unit tests for the data-driven phase executor (ticket #33)."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from quill import engine
from quill.blocker_memory import verified_memory_block
from quill.config import AuditDef, PhaseDef, QuillfolioConfig
from quill.contracts import default_catalog, load_contract
from quill.findings import Finding, load_findings, merge_verification_findings
from quill.git_ops import FeedbackItem, FeedbackSnapshot, PullRequest
from quill.loader import ModelLoadError
from quill.live_usage import LiveUsage
from quill.phases import Outcome, PhaseResult
from quill.runctx import CommandResult, PipelineDeps, RunContext, VerificationResult

# The task line names the file the worker must write, e.g. "Write your artifact to plan.md."
# Capture the whole filename token; the sentence's trailing period is stripped below.
_ARTIFACT_RE = re.compile(
    r"[Ww]rite (?:your artifact|your findings|your natural review notes|the reconciled review|"
    r"the reconciled natural review notes) to (\S+\.(?:md|json))"
)

_PROJECTION_RE = re.compile(r"write one JSON payload to (\S+\.json)")


class _FakeLoader:
    def __init__(self) -> None:
        self.loaded: list[str] = []

    def load(self, preset: str, timeout: float = 180) -> None:
        self.loaded.append(preset)

    def unload_all(self) -> None: ...


class _Spawn:
    """Spawn fake: returns a canned receipt per agent (phase id), in sequence if a list given.

    Records every (agent, preset, prompt) call so tests can assert fan-out + path injection.
    """

    def __init__(self, receipts: dict[str, str | list[str]]) -> None:
        self._receipts = receipts
        self.calls: list[tuple[str, str, str]] = []
        self.stream_paths: list[Path] = []
        #: run dir a real worker writes into; set by _ctx. When set, a DONE/PASS spawn writes the
        #: artifact named in its prompt, so the engine's artifact-existence check passes.
        self.run_dir: Path | None = None
        #: agents whose success receipt should NOT write a file (simulate the model claiming
        #: success but skipping the write — the engine must catch this).
        self.skip_write: set[str] = set()

    def __call__(
        self,
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
        self.calls.append((agent, preset, prompt))
        self.stream_paths.append(stream_path)
        value = self._receipts.get(agent, "DONE: ok")
        if isinstance(value, list):
            # pop the next scripted receipt; repeat the last once exhausted.
            receipt = value.pop(0) if len(value) > 1 else value[0]
        else:
            receipt = value
        self._maybe_write_artifact(agent, prompt, receipt)
        return receipt

    def _maybe_write_artifact(self, agent: str, prompt: str, receipt: str) -> None:
        """Mirror a real worker: on a success receipt, write the artifact to the ABSOLUTE path the
        prompt names (the engine now injects absolute paths so the worker can't misresolve them)."""
        if self.run_dir is None or agent in self.skip_write:
            return
        if not receipt.startswith(("DONE", "PASS")):
            return
        m = _ARTIFACT_RE.search(prompt)
        if m:
            Path(m.group(1).rstrip(".")).write_text("artifact body", encoding="utf-8")


def _extract(stdout: str) -> str | None:
    """The fake spawn returns the bare receipt line already."""
    return stdout.strip() or None


def test_usage_counter_accumulates_repeated_spawns_within_a_phase(tmp_path: Path) -> None:
    config = _config(tmp_path, [])
    ctx = _ctx(tmp_path, config, _Spawn({}), _FakeLoader())
    seen: list[LiveUsage] = []
    ctx.deps.on_usage_progress = lambda _phase, usage, _path: seen.append(usage)
    stream = tmp_path / "stream.jsonl"

    first = engine._usage_counter(ctx, "plan", stream)
    first(LiveUsage(100, 2))
    first(LiveUsage(100, 3))
    second = engine._usage_counter(ctx, "plan", stream)
    second(LiveUsage(150, 4))

    assert seen == [LiveUsage(100, 2), LiveUsage(100, 3), LiveUsage(250, 7)]


def test_usage_counter_replaces_context_window_for_same_session_continuation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, [])
    ctx = _ctx(tmp_path, config, _Spawn({}), _FakeLoader())
    seen: list[LiveUsage] = []
    ctx.deps.on_usage_progress = lambda _phase, usage, _path: seen.append(usage)
    stream = tmp_path / "stream.jsonl"

    first = engine._usage_counter(ctx, "research", stream)
    first(LiveUsage(82_682, 25_291, 82_958))
    continuation = engine._usage_counter(ctx, "research", stream, continuation=True)
    continuation(LiveUsage(92_020, 652, 92_672))

    assert seen[-1] == LiveUsage(174_702, 25_943, 92_672)


def _config(tmp_path: Path, phases: list[PhaseDef]) -> QuillfolioConfig:
    return QuillfolioConfig(
        directory=tmp_path,
        repo="me/proj",
        pr_base="main",
        runner="opencode",
        build_command="make",
        test_command="make test",
        log_dir="logs",
        phases=phases,
        # Personas and run artifacts are machine-level roots, not repo subdirectories. Point them
        # inside tmp_path so a test never reads or writes the real ~/.quill.
        personas_root=tmp_path / "personas-lib",
        runs_root=tmp_path / "runs",
    )


# Events captured per RunContext (RunContext is slotted, so we key off id()).
_EVENTS: dict[int, list[dict]] = {}


def _ctx(
    tmp_path: Path, config: QuillfolioConfig, spawn: _Spawn, loader: _FakeLoader
) -> RunContext:
    # Create the personas the phases reference so load_persona works.
    config.personas_root.mkdir(parents=True, exist_ok=True)
    for ph in config.phases:
        if ph.persona:
            persona = config.persona_path(ph.persona)
            persona.parent.mkdir(parents=True, exist_ok=True)
            persona.write_text("persona body", encoding="utf-8")
        for audit in ph.audits:
            persona = config.persona_path(audit.persona)
            persona.parent.mkdir(parents=True, exist_ok=True)
            persona.write_text("persona body", encoding="utf-8")
    run_dir = config.runs_root / "run1"
    run_dir.mkdir(parents=True, exist_ok=True)
    spawn.run_dir = run_dir
    events: list[dict] = []
    ctx = RunContext(
        config=config,
        deps=PipelineDeps(loader=loader, spawn=spawn, extract=_extract),  # type: ignore[arg-type]
        ticket=33,
        run_id="run1",
        run_dir=run_dir,
        on_event=events.append,
        should_stop=lambda: False,
        answer_decision=lambda _q: None,
        title="Ticket 33",
        body="Do the thing described here.",
    )
    _EVENTS[id(ctx)] = events
    return ctx


def _ev_types(ctx: RunContext) -> list[str]:
    return [e["type"] for e in _EVENTS[id(ctx)]]


def _events_of(ctx: RunContext, etype: str) -> list[dict]:
    return [e for e in _EVENTS[id(ctx)] if e["type"] == etype]


def test_concurrent_audits_share_one_load_and_overlap_execution(tmp_path: Path) -> None:
    audits = (
        AuditDef("architecture", "Requirements + architecture", "architecture.md", "qwen"),
        AuditDef("correctness", "Correctness + lifecycle", "correctness.md", "qwen"),
        AuditDef("tests", "Tests + regressions", "tests.md", "qwen"),
    )
    review = PhaseDef(
        id="review_impl",
        type="reviewer",
        against=("impl",),
        audits=audits,
    )
    config = _config(
        tmp_path,
        [
            PhaseDef(id="impl", type="producer", artifact="impl.md"),
            review,
            PhaseDef(
                id="review_final",
                type="finalizer",
                reconciles=("review_impl",),
            ),
        ],
    )
    barrier = threading.Barrier(3)
    lock = threading.Lock()
    active = 0
    max_active = 0

    class ConcurrentSpawn(_Spawn):
        def __call__(
            self,
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
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            barrier.wait(timeout=2)
            try:
                return super().__call__(
                    agent,
                    preset,
                    prompt,
                    timeout=timeout,
                    stream_path=stream_path,
                    on_tool=on_tool,
                    on_usage=on_usage,
                )
            finally:
                with lock:
                    active -= 1

    spawn = ConcurrentSpawn({})
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)
    ctx.deps.session_capacity = lambda _model: 3

    result = engine._run_reviewer(ctx, review)

    assert result.outcome is Outcome.DONE
    assert max_active == 3
    assert loader.loaded == ["qwen"]
    assert {call[0] for call in spawn.calls} == {
        "review_impl.architecture",
        "review_impl.correctness",
        "review_impl.tests",
    }
    assert engine._reconciled_findings(ctx, config.phases[-1]) == [
        "review-review_impl-architecture.md",
        "review-review_impl-correctness.md",
        "review-review_impl-tests.md",
    ]
    assert all(
        (ctx.run_dir / name).is_file()
        for name in engine._reconciled_findings(ctx, config.phases[-1])
    )


def test_parallel_producers_share_model_and_snapshot_latest_artifacts(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id=name,
            type="producer",
            persona=f"{name}.md",
            models=("qwen",),
            artifact=f"{name}.md",
            parallel_group="research",
        )
        for name in ("requirements", "architecture", "technical")
    ]
    config = _config(tmp_path, phases)
    barrier = threading.Barrier(3)

    class ConcurrentProducerSpawn(_Spawn):
        def __call__(
            self,
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
            barrier.wait(timeout=2)
            return super().__call__(
                agent,
                preset,
                prompt,
                timeout=timeout,
                stream_path=stream_path,
                on_tool=on_tool,
                on_usage=on_usage,
                abort_reason=abort_reason,
            )

    spawn = ConcurrentProducerSpawn({})
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)
    ctx.deps.session_capacity = lambda _model: 3

    results = engine._run_parallel_producers(ctx, phases)

    assert all(result.outcome is Outcome.DONE for _phase, result in results)
    assert loader.loaded == ["qwen"]
    assert {call[0] for call in spawn.calls} == {"requirements", "architecture", "technical"}
    manifest = json.loads((ctx.run_dir / "parallel-research-manifest.json").read_text())
    assert set(manifest["lanes"]) == {"requirements", "architecture", "technical"}
    assert all((ctx.run_dir / row["snapshot"]).is_file() for row in manifest["lanes"].values())


def test_parallel_producer_failure_prevents_synthesis(tmp_path: Path) -> None:
    lanes = [
        PhaseDef(
            id=name,
            type="producer",
            persona=f"{name}.md",
            models=("qwen",),
            artifact=f"{name}.md",
            parallel_group="research",
        )
        for name in ("requirements", "architecture", "technical")
    ]
    synthesis = PhaseDef(
        id="research_synthesis",
        type="producer",
        persona="synthesis.md",
        models=("qwen",),
        artifact="research.md",
        synthesizes=tuple(lane.id for lane in lanes),
    )
    config = _config(tmp_path, [*lanes, synthesis])
    spawn = _Spawn({"architecture": "FAILED: could not inspect repository"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    ctx.deps.session_capacity = lambda _model: 3

    final = engine.run_phases(ctx)

    assert final["type"] == "run_failed"
    assert final["phase"] == "architecture"
    assert "research_synthesis" not in [agent for agent, _model, _prompt in spawn.calls]
    assert {event["phase"] for event in _events_of(ctx, "phase_done")} == {
        "requirements",
        "architecture",
        "technical",
    }


def test_parallel_producers_respect_capacity_and_emit_independent_completion(
    tmp_path: Path,
) -> None:
    phases = [
        PhaseDef(
            id=name,
            type="producer",
            persona=f"{name}.md",
            models=("qwen",),
            artifact=f"{name}.md",
            parallel_group="research",
        )
        for name in ("requirements", "architecture", "technical")
    ]
    config = _config(tmp_path, phases)
    first_pair_started = threading.Barrier(3)
    release = {phase.id: threading.Event() for phase in phases}
    started = {phase.id: threading.Event() for phase in phases}

    class CapacityTwoSpawn(_Spawn):
        def __call__(
            self,
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
            started[agent].set()
            if agent in ("requirements", "architecture"):
                first_pair_started.wait(timeout=2)
            release[agent].wait(timeout=2)
            return super().__call__(
                agent,
                preset,
                prompt,
                timeout=timeout,
                stream_path=stream_path,
                on_tool=on_tool,
                on_usage=on_usage,
                abort_reason=abort_reason,
            )

    ctx = _ctx(tmp_path, config, CapacityTwoSpawn({}), _FakeLoader())
    ctx.deps.session_capacity = lambda _model: 2
    worker = threading.Thread(target=engine._run_parallel_producers, args=(ctx, phases))
    worker.start()
    first_pair_started.wait(timeout=2)
    assert started["requirements"].is_set()
    assert started["architecture"].is_set()
    assert not started["technical"].is_set()

    release["architecture"].set()
    assert started["technical"].wait(timeout=2)
    assert [event["phase"] for event in _events_of(ctx, "phase_done")] == ["architecture"]
    release["technical"].set()
    release["requirements"].set()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_selective_research_gate_reruns_only_owned_lane_then_synthesis(tmp_path: Path) -> None:
    lanes = [
        PhaseDef(
            id=name,
            type="producer",
            persona=f"{name}.md",
            models=("qwen",),
            artifact=f"{name}.md",
            parallel_group="research",
        )
        for name in ("requirements", "architecture", "technical")
    ]
    synthesis = PhaseDef(
        id="research_synthesis",
        type="producer",
        persona="synthesis.md",
        models=("qwen",),
        artifact="research.md",
        synthesizes=tuple(lane.id for lane in lanes),
    )
    gate = PhaseDef(
        id="research_gate",
        type="reviewer",
        persona="gate.md",
        models=("qwen",),
        artifact="research-findings.json",
        against=("research_synthesis",),
        gates=True,
        structured_findings=True,
        retry_budget=1,
        on_block=("research_synthesis",),
        selective_on_block=tuple(lane.id for lane in lanes),
    )
    config = _config(tmp_path, [*lanes, synthesis, gate])

    class SelectiveSpawn(_Spawn):
        gate_calls = 0

        def _maybe_write_artifact(self, agent: str, prompt: str, receipt: str) -> None:
            if agent != "research_gate":
                super()._maybe_write_artifact(agent, prompt, receipt)
                return
            self.gate_calls += 1
            match = _ARTIFACT_RE.search(prompt)
            assert match is not None
            finding = {
                "id": "R1",
                "severity": "MAJOR",
                "status": "OPEN" if self.gate_calls == 1 else "RESOLVED",
                "title": "Technical evidence missing",
                "requirement": "Verify the engine API",
                "evidence": "technical.md lacks an API citation",
                "failure_scenario": "Planning invents an unsupported API",
                "required_outcome": "Cite the authoritative API",
                "owner": "technical",
            }
            Path(match.group(1).rstrip(".")).write_text(
                json.dumps({"schema_version": 1, "findings": [finding]}),
                encoding="utf-8",
            )

    spawn = SelectiveSpawn(
        {"research_gate": ["BLOCK: missing evidence", "PASS: evidence verified"]}
    )
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    ctx.deps.session_capacity = lambda _model: 3

    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    calls = [agent for agent, _model, _prompt in spawn.calls]
    assert calls.count("requirements") == 1
    assert calls.count("architecture") == 1
    assert calls.count("technical") == 2
    assert calls.count("research_synthesis") == 2
    assert calls.count("research_gate") == 2
    manifest = json.loads((ctx.run_dir / "parallel-research-manifest.json").read_text())
    assert manifest["lanes"]["technical"]["attempt"] == 2


def test_direct_selective_research_gate_reruns_only_owned_lane_without_synthesis(
    tmp_path: Path,
) -> None:
    lanes = [
        PhaseDef(
            id=name,
            type="producer",
            persona=f"{name}.md",
            models=("qwen",),
            artifact=f"{name}.md",
            parallel_group="research",
        )
        for name in ("requirements", "architecture", "technical")
    ]
    gate = PhaseDef(
        id="research_gate",
        type="reviewer",
        persona="gate.md",
        models=("qwen",),
        artifact="research-findings.json",
        against=tuple(lane.id for lane in lanes),
        gates=True,
        structured_findings=True,
        retry_budget=1,
        selective_on_block=tuple(lane.id for lane in lanes),
    )
    config = _config(tmp_path, [*lanes, gate])

    class DirectSpawn(_Spawn):
        gate_calls = 0

        def _maybe_write_artifact(self, agent: str, prompt: str, receipt: str) -> None:
            if agent != "research_gate":
                super()._maybe_write_artifact(agent, prompt, receipt)
                return
            self.gate_calls += 1
            match = _ARTIFACT_RE.search(prompt)
            assert match is not None
            finding = {
                "id": "T1",
                "severity": "MAJOR",
                "status": "OPEN" if self.gate_calls == 1 else "RESOLVED",
                "title": "Technical evidence missing",
                "requirement": "Verify the engine API",
                "evidence": "technical.md lacks an API citation",
                "failure_scenario": "Planning invents an unsupported API",
                "required_outcome": "Cite the authoritative API",
                "owner": "technical",
            }
            Path(match.group(1).rstrip(".")).write_text(
                json.dumps({"schema_version": 1, "findings": [finding]}),
                encoding="utf-8",
            )

    spawn = DirectSpawn(
        {"research_gate": ["BLOCK: missing evidence", "PASS: evidence verified"]}
    )
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    ctx.deps.session_capacity = lambda _model: 3

    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    calls = [agent for agent, _model, _prompt in spawn.calls]
    assert calls.count("requirements") == 1
    assert calls.count("architecture") == 1
    assert calls.count("technical") == 2
    assert calls.count("research_gate") == 2
    assert "research_synthesis" not in calls


def test_selective_research_gate_replaces_multiple_then_remaining_blocked_lanes(
    tmp_path: Path,
) -> None:
    lanes = [
        PhaseDef(
            id=name,
            type="producer",
            persona=f"{name}.md",
            models=("qwen",),
            artifact=f"{name}.md",
            parallel_group="research",
        )
        for name in ("requirements", "architecture", "technical")
    ]
    synthesis = PhaseDef(
        id="research_synthesis",
        type="producer",
        persona="synthesis.md",
        models=("qwen",),
        artifact="research.md",
        synthesizes=tuple(lane.id for lane in lanes),
    )
    gate = PhaseDef(
        id="research_gate",
        type="reviewer",
        persona="gate.md",
        models=("qwen",),
        against=("research_synthesis",),
        gates=True,
        structured_findings=True,
        retry_budget=2,
        on_block=("research_synthesis",),
        selective_on_block=tuple(lane.id for lane in lanes),
    )
    config = _config(tmp_path, [*lanes, synthesis, gate])

    class MultiRoundSpawn(_Spawn):
        gate_calls = 0

        def _maybe_write_artifact(self, agent: str, prompt: str, receipt: str) -> None:
            if agent != "research_gate":
                super()._maybe_write_artifact(agent, prompt, receipt)
                return
            self.gate_calls += 1
            match = _ARTIFACT_RE.search(prompt)
            assert match is not None
            findings = []
            for finding_id, owner in (("R1", "requirements"), ("T1", "technical")):
                is_open = self.gate_calls == 1 or (owner == "technical" and self.gate_calls == 2)
                findings.append(
                    {
                        "id": finding_id,
                        "severity": "MAJOR",
                        "status": "OPEN" if is_open else "RESOLVED",
                        "title": f"{owner} evidence missing",
                        "requirement": f"Complete {owner} evidence",
                        "evidence": f"{owner}.md is incomplete",
                        "failure_scenario": "Planning must guess",
                        "required_outcome": "Supply verified evidence",
                        "owner": owner,
                    }
                )
            Path(match.group(1).rstrip(".")).write_text(
                json.dumps({"schema_version": 1, "findings": findings}),
                encoding="utf-8",
            )

    spawn = MultiRoundSpawn(
        {"research_gate": ["BLOCK: two lanes", "BLOCK: one lane", "PASS: complete"]}
    )
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    ctx.deps.session_capacity = lambda _model: 3

    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    calls = [agent for agent, _model, _prompt in spawn.calls]
    assert calls.count("requirements") == 2
    assert calls.count("architecture") == 1
    assert calls.count("technical") == 3
    assert calls.count("research_synthesis") == 3
    assert calls.count("research_gate") == 3


def test_concurrent_audits_emit_done_as_each_lane_finishes(tmp_path: Path) -> None:
    audits = (
        AuditDef("architecture", "Architecture", "architecture.md", "qwen"),
        AuditDef("correctness", "Correctness", "correctness.md", "qwen"),
        AuditDef("tests", "Tests", "tests.md", "qwen"),
    )
    review = PhaseDef(id="review_impl", type="reviewer", audits=audits)
    config = _config(tmp_path, [review])
    all_started = threading.Barrier(4)
    releases = {audit.id: threading.Event() for audit in audits}

    class OrderedSpawn(_Spawn):
        def __call__(
            self,
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
            all_started.wait(timeout=2)
            releases[agent.rsplit(".", 1)[-1]].wait(timeout=2)
            return super().__call__(
                agent,
                preset,
                prompt,
                timeout=timeout,
                stream_path=stream_path,
                on_tool=on_tool,
                on_usage=on_usage,
            )

    ctx = _ctx(tmp_path, config, OrderedSpawn({}), _FakeLoader())
    ctx.deps.session_capacity = lambda _model: 3
    lane_done = {audit.id: threading.Event() for audit in audits}
    append_event = ctx.on_event

    def observe(event: dict) -> None:
        append_event(event)
        if event.get("type") == "phase_done":
            lane_done[str(event["phase"]).rsplit(".", 1)[-1]].set()

    ctx.on_event = observe
    worker = threading.Thread(target=engine._run_reviewer, args=(ctx, review))
    worker.start()
    all_started.wait(timeout=2)

    releases["tests"].set()
    assert lane_done["tests"].wait(timeout=2)
    assert not lane_done["architecture"].is_set()
    assert not lane_done["correctness"].is_set()

    releases["correctness"].set()
    assert lane_done["correctness"].wait(timeout=2)
    assert not lane_done["architecture"].is_set()

    releases["architecture"].set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert [event["phase"] for event in _events_of(ctx, "phase_done")] == [
        "review_impl.tests",
        "review_impl.correctness",
        "review_impl.architecture",
    ]


def test_concurrent_audits_capacity_two_queues_third_until_slot_opens(tmp_path: Path) -> None:
    audits = tuple(
        AuditDef(name, name.title(), f"{name}.md", "qwen")
        for name in ("architecture", "correctness", "tests")
    )
    review = PhaseDef(id="review_impl", type="reviewer", audits=audits)
    config = _config(tmp_path, [review])
    first_pair_started = threading.Barrier(3)
    release = {audit.id: threading.Event() for audit in audits}
    started = {audit.id: threading.Event() for audit in audits}

    class CapacityTwoSpawn(_Spawn):
        def __call__(
            self,
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
            lane_id = agent.rsplit(".", 1)[-1]
            started[lane_id].set()
            if lane_id in ("architecture", "correctness"):
                first_pair_started.wait(timeout=2)
            release[lane_id].wait(timeout=2)
            return super().__call__(
                agent,
                preset,
                prompt,
                timeout=timeout,
                stream_path=stream_path,
                on_tool=on_tool,
                on_usage=on_usage,
            )

    ctx = _ctx(tmp_path, config, CapacityTwoSpawn({}), _FakeLoader())
    ctx.deps.session_capacity = lambda _model: 2
    worker = threading.Thread(target=engine._run_reviewer, args=(ctx, review))
    worker.start()
    first_pair_started.wait(timeout=2)

    assert started["architecture"].is_set()
    assert started["correctness"].is_set()
    assert not started["tests"].is_set()
    assert {event["phase"] for event in _events_of(ctx, "phase_started")} == {
        "review_impl.architecture",
        "review_impl.correctness",
    }

    release["correctness"].set()
    assert started["tests"].wait(timeout=2)
    release["tests"].set()
    release["architecture"].set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(_events_of(ctx, "phase_done")) == 3
    loading = _events_of(ctx, "model_loading")
    assert len(loading) == 1
    assert loading[0]["phase"] == "review_impl"
    assert loading[0]["session_capacity"] == 2


def test_concurrent_audits_capacity_one_runs_in_configured_order(tmp_path: Path) -> None:
    audits = tuple(
        AuditDef(name, name.title(), f"{name}.md", "qwen")
        for name in ("architecture", "correctness", "tests")
    )
    review = PhaseDef(id="review_impl", type="reviewer", audits=audits)
    config = _config(tmp_path, [review])
    spawn = _Spawn({})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    ctx.deps.session_capacity = lambda _model: 1

    result = engine._run_reviewer(ctx, review)

    assert result.outcome is Outcome.DONE
    assert [call[0] for call in spawn.calls] == [
        "review_impl.architecture",
        "review_impl.correctness",
        "review_impl.tests",
    ]
    transitions = [
        (event["type"], event.get("phase"))
        for event in _EVENTS[id(ctx)]
        if event["type"] in ("phase_started", "phase_done")
    ]
    assert transitions == [
        ("phase_started", "review_impl.architecture"),
        ("phase_done", "review_impl.architecture"),
        ("phase_started", "review_impl.correctness"),
        ("phase_done", "review_impl.correctness"),
        ("phase_started", "review_impl.tests"),
        ("phase_done", "review_impl.tests"),
    ]


def test_concurrent_audits_model_load_failure_closes_every_lane(tmp_path: Path) -> None:
    audits = tuple(
        AuditDef(name, name.title(), f"{name}.md", "qwen")
        for name in ("architecture", "correctness", "tests")
    )
    review = PhaseDef(id="review_impl", type="reviewer", audits=audits)
    config = _config(tmp_path, [review])

    class FailingLoader(_FakeLoader):
        def load(self, preset: str, timeout: float = 180) -> None:
            super().load(preset, timeout)
            raise RuntimeError("router unavailable")

    spawn = _Spawn({})
    ctx = _ctx(tmp_path, config, spawn, FailingLoader())
    ctx.deps.session_capacity = lambda _model: 3

    result = engine._run_reviewer(ctx, review)

    assert result.outcome is Outcome.CRASH
    assert "router unavailable" in result.message
    assert spawn.calls == []
    assert len(_events_of(ctx, "phase_started")) == 3
    assert [event["verdict"] for event in _events_of(ctx, "phase_done")] == [
        "CRASH",
        "CRASH",
        "CRASH",
    ]
    model_load = _events_of(ctx, "model_load_done")
    assert len(model_load) == 1
    assert model_load[0]["success"] is False
    assert model_load[0]["reason"] == "router unavailable"


def test_concurrent_audits_stop_prevents_queued_lanes_from_starting(tmp_path: Path) -> None:
    audits = tuple(
        AuditDef(name, name.title(), f"{name}.md", "qwen")
        for name in ("architecture", "correctness", "tests")
    )
    review = PhaseDef(id="review_impl", type="reviewer", audits=audits)
    config = _config(tmp_path, [review])
    stopped = False

    class StopAfterFirstSpawn(_Spawn):
        def __call__(self, *args, **kwargs) -> str:  # type: ignore[no-untyped-def]
            nonlocal stopped
            result = super().__call__(*args, **kwargs)
            stopped = True
            return result

    spawn = StopAfterFirstSpawn({})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    ctx.should_stop = lambda: stopped
    ctx.deps.session_capacity = lambda _model: 1

    result = engine._run_reviewer(ctx, review)

    assert result.outcome is Outcome.CRASH
    assert [call[0] for call in spawn.calls] == ["review_impl.architecture"]
    assert [event["phase"] for event in _events_of(ctx, "phase_started")] == [
        "review_impl.architecture"
    ]
    assert [event["phase"] for event in _events_of(ctx, "phase_done")] == [
        "review_impl.architecture"
    ]


def test_concurrent_audits_take_fresh_capacity_snapshot_each_execution(tmp_path: Path) -> None:
    audits = tuple(
        AuditDef(name, name.title(), f"{name}.md", "qwen")
        for name in ("architecture", "correctness")
    )
    review = PhaseDef(id="review_impl", type="reviewer", audits=audits)
    config = _config(tmp_path, [review])
    ctx = _ctx(tmp_path, config, _Spawn({}), _FakeLoader())
    capacities = iter((1, 2))
    calls = 0

    def capacity(_model: str) -> int:
        nonlocal calls
        calls += 1
        return next(capacities)

    ctx.deps.session_capacity = capacity

    assert engine._run_reviewer(ctx, review).outcome is Outcome.DONE
    assert engine._run_reviewer(ctx, review).outcome is Outcome.DONE
    assert calls == 2
    # Capacity is discovered on every execution, but the already-resident second pass does not
    # fabricate another model-load operation.
    assert [event["session_capacity"] for event in _events_of(ctx, "model_loading")] == [1]
    assert len(_events_of(ctx, "model_load_done")) == 1


def test_model_load_is_timed_separately_from_phase_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    phase = PhaseDef(
        id="impl",
        type="producer",
        persona="impl.md",
        models=("qwen",),
        artifact="impl.md",
    )
    config = _config(tmp_path, [phase])
    ctx = _ctx(tmp_path, config, _Spawn({}), _FakeLoader())
    clock = iter((0.0, 10.0, 20.0, 50.0))
    monkeypatch.setattr(engine.time, "monotonic", lambda: next(clock))

    assert engine._run_producer(ctx, phase).outcome is Outcome.DONE

    load = _events_of(ctx, "model_load_done")[0]
    done = _events_of(ctx, "phase_done")[0]
    assert load["duration_s"] == 10.0
    assert load["success"] is True
    assert done["duration_s"] == 40.0


# -- run plan summary -------------------------------------------------------------


def test_run_plan_emitted_after_run_started(tmp_path: Path) -> None:
    """A run_plan event follows run_started and carries the runner + each phase's model + gates."""
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("plan-m",),
            artifact="plan.md",
        ),
        PhaseDef(
            id="review_plan",
            type="reviewer",
            persona="personas/review-plan.md",
            models=("rev-a", "rev-b"),
            gates=True,
            retry_budget=1,
            on_block=("plan",),
        ),
        PhaseDef(id="build_test", type="mechanical", step="build_test"),
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "DONE: ok", "review_plan": "PASS: ok"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    ctx.deps.build_test = lambda _c, _selection: (True, "ok")

    engine.run_phases(ctx)

    types = _ev_types(ctx)
    assert types.index("run_plan") == types.index("run_started") + 1  # immediately after
    plan = _events_of(ctx, "run_plan")[0]
    summary = str(plan["summary"])
    assert "runner : opencode" in summary
    assert "plan (producer) → plan-m" in summary
    assert "rev-a + rev-b" in summary  # fan-out models joined
    assert "gates (retry 1), on BLOCK → plan" in summary
    assert "build_test (mechanical)" in summary
    # Structured lines mirror the phase count for API consumers.
    assert isinstance(plan["lines"], list) and len(plan["lines"]) == 3


def test_run_plan_marks_skipped_on_resume(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("m",),
            artifact="plan.md",
        ),
        PhaseDef(
            id="impl",
            type="producer",
            persona="personas/impl.md",
            models=("m2",),
            artifact="impl.md",
        ),
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "DONE: p", "impl": "DONE: i"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())

    engine.run_phases(ctx, start_phase="impl")

    summary = str(_events_of(ctx, "run_plan")[0]["summary"])
    assert "plan (producer) → m [skipped: resume]" not in summary  # not gated, no bracket group
    assert "(skipped: resume)" in summary  # the earlier phase is flagged skipped


# -- producer ---------------------------------------------------------------------


def test_producer_runs_and_injects_run_dir(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("m1",),
            artifact="plan.md",
        )
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "DONE: wrote plan"})
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)

    final = engine.run_phases(ctx)
    assert final["type"] == "run_done"
    assert loader.loaded == ["m1"]
    # Prompt carries the run dir and the artifact name.
    _, _, prompt = spawn.calls[0]
    assert "RUN DIR: " in prompt
    assert "runs/run1" in prompt
    # The artifact path injected into the task is absolute (ends with the run dir + filename).
    assert "runs/run1/plan.md" in prompt
    assert "persona body" in prompt
    # Events are enriched with phase type, model, and a duration for the console.
    started = _events_of(ctx, "phase_started")[0]
    assert started["phase_type"] == "producer"
    assert started["model"] == "m1"
    assert _events_of(ctx, "model_loading")[0]["model"] == "m1"
    assert _events_of(ctx, "phase_executing")[0]["label"] == "plan"
    types = _ev_types(ctx)
    assert (
        types.index("phase_started") < types.index("model_loading") < types.index("phase_executing")
    )
    done = _events_of(ctx, "phase_done")[0]
    assert done["model"] == "m1"
    assert isinstance(done["duration_s"], float)
    # run_started carries the ticket title fetched into ctx.
    assert _events_of(ctx, "run_started")[0]["title"] == "Ticket 33"


# -- reviewer fan-out + finalizer -------------------------------------------------


def test_fanout_reviewer_then_finalizer(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="impl",
            type="producer",
            persona="personas/impl.md",
            models=("impl-m",),
            artifact="impl.md",
        ),
        PhaseDef(
            id="review_impl",
            type="reviewer",
            persona="personas/review-impl.md",
            models=("gemma-X", "qwen 27b"),
            against=("impl",),
        ),
        PhaseDef(
            id="final",
            type="finalizer",
            persona="personas/review-final.md",
            models=("qwen 27b",),
            reconciles=("review_impl",),
            gates=True,
            on_block=("impl",),
        ),
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn(
        {
            "impl": "DONE: implemented",
            "review_impl": "DONE: wrote findings",
            "final": "PASS: all good",
        }
    )
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)

    final = engine.run_phases(ctx)
    assert final["type"] == "run_done"
    # Fan-out loaded both reviewer models, in order, between impl and finalizer.
    assert loader.loaded == ["impl-m", "gemma-X", "qwen 27b", "qwen 27b"]
    # Two reviewer spawns wrote slugged findings names into their prompts.
    review_prompts = [p for (a, _, p) in spawn.calls if a == "review_impl"]
    assert len(review_prompts) == 2
    assert "review-review_impl-gemma-x.md" in review_prompts[0]
    assert "review-review_impl-qwen-27b.md" in review_prompts[1]
    # Engine names the upstream artifact to review against (from `against`), not the persona.
    assert "Review against these artifacts:" in review_prompts[0]
    assert "runs/run1/impl.md" in review_prompts[0]
    # Finalizer prompt lists BOTH reviewers' findings files.
    final_prompt = next(p for (a, _, p) in spawn.calls if a == "final")
    assert "review-review_impl-gemma-x.md" in final_prompt
    assert "review-review_impl-qwen-27b.md" in final_prompt


def test_structured_finalizer_cannot_omit_blocking_audit_finding(tmp_path: Path) -> None:
    audits = (
        AuditDef("architecture", "Architecture", "architecture.md", "qwen"),
        AuditDef("correctness", "Correctness", "correctness.md", "qwen"),
    )
    review = PhaseDef(
        id="review_impl",
        type="reviewer",
        audits=audits,
        structured_findings=True,
    )
    finalizer = PhaseDef(
        id="review_impl_final",
        type="finalizer",
        persona="personas/review-final.md",
        models=("qwen",),
        artifact="review_impl_final.md",
        reconciles=("review_impl",),
        gates=True,
        structured_findings=True,
        retry_budget=0,
    )
    config = _config(tmp_path, [review, finalizer])
    spawn = _Spawn({"review_impl_final": "PASS: reconciled"})
    spawn.skip_write.add("review_impl_final")
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())

    def finding(finding_id: str, status: str = "OPEN") -> dict[str, str]:
        return {
            "id": finding_id,
            "severity": "MAJOR",
            "status": status,
            "title": "Required behavior is missing",
            "requirement": "Preserve required behavior",
            "evidence": "src/app.py:10 omits it",
            "failure_scenario": "The normal path fails",
            "required_outcome": "Implement the required behavior",
        }

    ctx.artifact_path("review-review_impl-architecture.md").write_text(
        json.dumps({"schema_version": 1, "findings": [finding("architecture:F1")]}),
        encoding="utf-8",
    )
    ctx.artifact_path("review-review_impl-correctness.md").write_text(
        json.dumps({"schema_version": 1, "findings": [finding("correctness:F1")]}),
        encoding="utf-8",
    )
    ctx.artifact_path("review_impl_final.md").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "findings": [finding("architecture:F1", status="RESOLVED")],
            }
        ),
        encoding="utf-8",
    )

    result = engine._run_finalizer(ctx, finalizer)

    assert result.outcome is Outcome.GARBAGE
    assert "omitted prior blocking finding(s): correctness:F1" in result.message


def test_finalizer_verification_normalizes_reused_finding_id_collision(tmp_path: Path) -> None:
    review = PhaseDef(
        id="review_impl",
        type="reviewer",
        audits=(AuditDef("tests", "Tests", "tests.md", "qwen"),),
        structured_findings=True,
    )
    finalizer = PhaseDef(
        id="review_impl_final",
        type="finalizer",
        persona="personas/review-final.md",
        models=("qwen",),
        artifact="review_impl_final.md",
        reconciles=("review_impl",),
        gates=True,
        structured_findings=True,
    )
    config = _config(tmp_path, [review, finalizer])
    spawn = _Spawn({"review_impl_final": "PASS: reconciled"})
    spawn.skip_write.add("review_impl_final")
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    old = Finding(
        id="tests:dead-code-runtime-context",
        severity="MAJOR",
        status="OPEN",
        title="Three test functions are never executed",
        requirement="Execute the three integration tests",
        evidence="run() omits three functions",
        failure_scenario="Three integration paths go untested",
        required_outcome="Call all three tests from run()",
    )
    new = replace(
        old,
        title="Five test functions are never executed",
        requirement="Execute the five additional integration tests",
        evidence="run() omits five additional functions",
        failure_scenario="Five additional integration paths go untested",
        required_outcome="Call all five additional tests from run()",
    )
    ctx.artifact_path("review-review_impl-tests.md").write_text(
        json.dumps({"schema_version": 1, "findings": [asdict(new)]}),
        encoding="utf-8",
    )
    merged = merge_verification_findings((old,), (new,))
    resolved = [
        asdict(replace(finding, status="RESOLVED", evidence="verified fixed")) for finding in merged
    ]
    ctx.artifact_path("review_impl_final.md").write_text(
        json.dumps({"schema_version": 1, "findings": resolved}), encoding="utf-8"
    )

    result = engine._run_phase_for_verify(ctx, finalizer, prior_findings=(old,))

    assert result.outcome is Outcome.PASS
    prompt = spawn.calls[-1][2]
    # Both identities reach the model as separately adjudicable ids: the reused ID was normalized
    # to a distinct revision ID, so the original defect is not silently overwritten by the new one.
    assert prompt.count("tests:dead-code-runtime-context (MAJOR)") == 1
    assert merged[1].id in prompt
    assert "Three test functions are never executed" in prompt
    assert "Five test functions are never executed" in prompt


def test_pr_review_finalizer_repairs_inconsistent_verdict_in_same_session(
    tmp_path: Path,
) -> None:
    phase = PhaseDef(
        id="review_pr_final",
        type="finalizer",
        persona="personas/pr-review-final.md",
        models=("qwen",),
        artifact="pr-review.json",
    )
    config = _config(tmp_path, [phase])
    spawn = _Spawn({"review_pr_final": "DONE: reconciled PR review"})
    spawn.skip_write.add("review_pr_final")
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    artifact = ctx.artifact_path("pr-review.json")
    finding = {
        "id": "PRR-001",
        "severity": "MAJOR",
        "title": "Primary path fails",
        "requirement": "The primary path must work",
        "evidence": "src/app.py:42 returns false",
        "failure_scenario": "A normal request reaches the failing branch",
        "impact": "The requested behavior is unavailable",
        "required_outcome": "Make the normal request succeed",
    }
    artifact.write_text(json.dumps({"verdict": "PASS", "findings": [finding]}), encoding="utf-8")
    prompts: list[str] = []

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
        prompts.append(prompt)
        artifact.write_text(
            json.dumps({"verdict": "BLOCK", "summary": "One blocker.", "findings": [finding]}),
            encoding="utf-8",
        )
        return "DONE: repaired PR review"

    ctx.deps.session_repair = repair

    result = engine._run_finalizer(ctx, phase)

    # Quill derives the verdict from the findings that survived reconciliation, so a model that
    # wrote PASS beside a blocking finding is corrected in place. Spending a repair round on it
    # discarded a complete review over one word — a recorded run died exactly that way.
    assert result.outcome is Outcome.DONE
    assert prompts == [], "a verdict mismatch must not cost a repair round"


def test_pr_review_finalizer_fails_after_one_invalid_semantic_repair(tmp_path: Path) -> None:
    phase = PhaseDef(
        id="review_pr_final",
        type="finalizer",
        persona="personas/pr-review-final.md",
        models=("qwen",),
        artifact="pr-review.json",
    )
    config = _config(tmp_path, [phase])
    spawn = _Spawn({"review_pr_final": "DONE: reconciled PR review"})
    spawn.skip_write.add("review_pr_final")
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    artifact = ctx.artifact_path("pr-review.json")
    # Structurally invalid: a blocking finding missing a required field. A verdict that merely
    # disagrees with the findings is no longer an error — Quill derives that — so the repair path
    # has to be exercised with a defect the artifact genuinely cannot supply.
    artifact.write_text(
        json.dumps(
            {
                "verdict": "BLOCK",
                "findings": [
                    {
                        "id": "PRR-001",
                        "severity": "CRITICAL",
                        "title": "Data loss",
                        "requirement": "Preserve data",
                        "failure_scenario": "Saving an existing record deletes it",
                        "impact": "User data is lost",
                        "required_outcome": "Preserve the existing record",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    repairs = 0

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
        nonlocal repairs
        repairs += 1
        return "DONE: left invalid PR review unchanged"

    ctx.deps.session_repair = repair

    result = engine._run_finalizer(ctx, phase)

    assert result.outcome is Outcome.GARBAGE
    assert repairs == 1
    assert "invalid PR review artifact after repair" in result.message


@pytest.mark.parametrize(
    ("review", "expected"),
    [
        ({"verdict": "PASS", "summary": "Ready.", "findings": []}, Outcome.DONE),
        (
            {
                "verdict": "BLOCK",
                "summary": "Blocked.",
                "findings": [
                    {
                        "id": "PRR-001",
                        "severity": "CRITICAL",
                        "title": "Data loss",
                        "requirement": "Preserve data",
                        "evidence": "src/store.py:9 deletes the record",
                        "failure_scenario": "Saving an existing record deletes it",
                        "impact": "User data is lost",
                        "required_outcome": "Preserve the existing record",
                    }
                ],
            },
            Outcome.DONE,
        ),
    ],
)
def test_pr_review_finalizer_accepts_semantically_consistent_artifact(
    tmp_path: Path, review: dict[str, object], expected: Outcome
) -> None:
    phase = PhaseDef(
        id="review_pr_final",
        type="finalizer",
        persona="personas/pr-review-final.md",
        models=("qwen",),
        artifact="pr-review.json",
    )
    config = _config(tmp_path, [phase])
    spawn = _Spawn({"review_pr_final": "DONE: reconciled PR review"})
    spawn.skip_write.add("review_pr_final")
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    ctx.artifact_path("pr-review.json").write_text(json.dumps(review), encoding="utf-8")
    repairs: list[str] = []
    ctx.deps.session_repair = lambda *args, **kwargs: (
        repairs.append(str(args[2])) or "DONE: repaired"
    )

    result = engine._run_finalizer(ctx, phase)

    assert result.outcome is expected
    assert repairs == []


def test_pr_review_finalizer_cannot_omit_blocking_audit_finding(tmp_path: Path) -> None:
    audit = AuditDef("requirements", "Requirements", "requirements.md", "qwen")
    review = PhaseDef(
        id="review_pr",
        type="reviewer",
        audits=(audit,),
        structured_findings=True,
    )
    finalizer = PhaseDef(
        id="review_pr_final",
        type="finalizer",
        persona="personas/pr-review-final.md",
        models=("qwen",),
        artifact="pr-review.json",
        reconciles=("review_pr",),
    )
    config = _config(tmp_path, [review, finalizer])
    spawn = _Spawn({"review_pr_final": "DONE: reconciled"})
    spawn.skip_write.add("review_pr_final")
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    source_finding = {
        "id": "requirements:F1",
        "severity": "MAJOR",
        "status": "OPEN",
        "title": "Ticket outcome omitted",
        "requirement": "Deliver the ticket outcome",
        "evidence": "src/app.py:10 lacks the behavior",
        "failure_scenario": "The requested path remains unavailable",
        "required_outcome": "Implement the ticket outcome",
    }
    ctx.artifact_path("review-review_pr-requirements.md").write_text(
        json.dumps({"schema_version": 1, "findings": [source_finding]}),
        encoding="utf-8",
    )
    ctx.artifact_path("pr-review.json").write_text(
        json.dumps({"verdict": "PASS", "summary": "Ready", "findings": []}),
        encoding="utf-8",
    )

    result = engine._run_finalizer(ctx, finalizer)

    assert result.outcome is Outcome.GARBAGE
    assert "PR review omitted blocking audit finding(s): requirements:F1" in result.message


# -- model affinity ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("models", "loaded", "next_model", "expected"),
    [
        # Nothing to act on: neither hint names a model in the fan-out.
        (("a", "b", "c"), None, None, ["a", "b", "c"]),
        (("a", "b", "c"), "z", "z", ["a", "b", "c"]),
        # Resident model moves to the front, wherever it started.
        (("a", "b", "c"), "c", None, ["c", "a", "b"]),
        (("a", "b", "c"), "b", None, ["b", "a", "c"]),
        (("a", "b", "c"), "a", None, ["a", "b", "c"]),
        # Next phase's model moves to the back so it is still resident when that phase runs.
        (("a", "b", "c"), None, "a", ["b", "c", "a"]),
        (("a", "b", "c"), None, "c", ["a", "b", "c"]),
        # Both rules compose: start warm, end on what comes next.
        (("a", "b", "c"), "c", "a", ["c", "b", "a"]),
        # One model is both hints — front wins; a fan-out of one is a no-op either way.
        (("a", "b"), "a", "a", ["a", "b"]),
        (("a",), "a", "a", ["a"]),
    ],
)
def test_affinity_order(
    models: tuple[str, ...], loaded: str | None, next_model: str | None, expected: list[str]
) -> None:
    assert engine._affinity_order(models, loaded, next_model) == expected


def test_affinity_order_preserves_multiplicity(tmp_path: Path) -> None:
    """Reordering never adds or drops a pass. A model listed twice still runs twice — positions
    move, the multiset does not."""
    assert engine._affinity_order(("a", "b", "a"), "a", "b") == ["a", "a", "b"]
    assert sorted(engine._affinity_order(("a", "b", "a"), "b", None)) == ["a", "a", "b"]


def test_next_phase_model_skips_model_less_phases(tmp_path: Path) -> None:
    """A mechanical step loads nothing, so the lookahead reaches past it to the next LLM phase."""
    phases = [
        PhaseDef(id="review", type="reviewer", persona="p.md", models=("g", "q")),
        PhaseDef(id="build", type="mechanical", step="build_test"),
        PhaseDef(id="commit", type="producer", persona="p.md", models=("commit-m",)),
    ]
    config = _config(tmp_path, phases)
    assert engine._next_phase_model(config, "review") == "commit-m"
    assert engine._next_phase_model(config, "commit") is None
    assert engine._next_phase_model(config, "nope") is None


def test_fanout_starts_on_resident_model_and_ends_on_the_next_phases(tmp_path: Path) -> None:
    """The fan-out is reordered so no swap is needed to enter it, and none to leave it.

    impl runs on qwen and the finalizer on gemma, so the declared order (gemma, qwen) would swap
    three times: qwen->gemma->qwen->gemma. Starting on the resident qwen and ending on gemma leaves
    exactly one swap for the whole sequence.
    """
    phases = [
        PhaseDef(
            id="impl",
            type="producer",
            persona="personas/impl.md",
            models=("qwen",),
            artifact="impl.md",
        ),
        PhaseDef(
            id="review_impl",
            type="reviewer",
            persona="personas/review-impl.md",
            models=("gemma", "qwen"),
        ),
        PhaseDef(
            id="final",
            type="finalizer",
            persona="personas/review-final.md",
            models=("gemma",),
            reconciles=("review_impl",),
        ),
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn(
        {"impl": "DONE: implemented", "review_impl": "DONE: findings", "final": "DONE: reconciled"}
    )
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)

    assert engine.run_phases(ctx)["type"] == "run_done"
    assert loader.loaded == ["qwen", "qwen", "gemma", "gemma"]
    # What the reorder actually buys: consecutive repeats are no-ops in the real loader, so this
    # sequence costs one swap where the declared order would have cost three.
    swaps = sum(1 for a, b in zip(loader.loaded, loader.loaded[1:]) if a != b)
    assert swaps == 1
    # Both reviewer passes still ran, and each still wrote the file named for ITS model.
    review_prompts = [p for (a, _, p) in spawn.calls if a == "review_impl"]
    assert len(review_prompts) == 2
    assert "review-review_impl-qwen.md" in review_prompts[0]
    assert "review-review_impl-gemma.md" in review_prompts[1]


def _impl_only(tmp_path: Path) -> QuillfolioConfig:
    return _config(
        tmp_path,
        [
            PhaseDef(
                id="impl",
                type="producer",
                persona="personas/impl.md",
                models=("qwen",),
                artifact="impl.md",
            )
        ],
    )


def test_garbage_still_records_the_affinity_hint(tmp_path: Path) -> None:
    """The model loaded fine; only its receipt was unparsable. It is still resident, so the hint
    stands."""
    ctx = _ctx(tmp_path, _impl_only(tmp_path), _Spawn({"impl": "not a receipt"}), _FakeLoader())
    engine.run_phases(ctx)
    assert ctx.loaded_preset == "qwen"


def test_failed_same_session_self_fix_reruns_phase_fresh(tmp_path: Path) -> None:
    spawn = _Spawn({"impl": ["not a receipt", "DONE: implemented"]})
    ctx = _ctx(tmp_path, _impl_only(tmp_path), spawn, _FakeLoader())
    repair_prompts: list[str] = []

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
        repair_prompts.append(prompt)
        return "FAILED: could not repair the output contract"

    ctx.deps.session_repair = repair

    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    assert len([call for call in spawn.calls if call[0] == "impl"]) == 2
    assert len(repair_prompts) == 1
    assert "Quill rejected your phase output: not a receipt" in repair_prompts[0]
    assert "one final receipt line beginning `DONE:` or `FAILED:`" in repair_prompts[0]
    retries = _events_of(ctx, "retry")
    assert len(retries) == 1
    assert "same-session self-fix did not repair malformed output" in str(retries[0]["reason"])
    assert _ev_types(ctx).count("self_fix_started") == 1
    assert _events_of(ctx, "self_fix_done")[0]["repaired"] is False


def test_malformed_phase_fails_after_self_fix_and_fresh_attempt_are_exhausted(
    tmp_path: Path,
) -> None:
    spawn = _Spawn({"impl": "not a receipt"})
    ctx = _ctx(tmp_path, _impl_only(tmp_path), spawn, _FakeLoader())
    repairs = 0

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
        nonlocal repairs
        repairs += 1
        return "FAILED: still malformed"

    ctx.deps.session_repair = repair

    final = engine.run_phases(ctx)

    assert final["type"] == "run_failed"
    assert len([call for call in spawn.calls if call[0] == "impl"]) == 2
    assert repairs == 2
    assert _ev_types(ctx).count("retry") == 1


def test_explicit_phase_failure_does_not_trigger_contract_retry(tmp_path: Path) -> None:
    spawn = _Spawn({"impl": "FAILED: implementation is impossible"})
    ctx = _ctx(tmp_path, _impl_only(tmp_path), spawn, _FakeLoader())

    final = engine.run_phases(ctx)

    assert final["type"] == "run_failed"
    assert len(spawn.calls) == 1
    assert "retry" not in _ev_types(ctx)


def test_failed_load_clears_the_affinity_hint(tmp_path: Path) -> None:
    """A load failure surfaces as CRASH and can leave nothing resident, so the hint is dropped
    rather than left naming a model that may not be there."""

    class _FailingLoader(_FakeLoader):
        def load(self, preset: str, timeout: float = 180) -> None:
            raise ModelLoadError("router said no")

    ctx = _ctx(tmp_path, _impl_only(tmp_path), _Spawn({"impl": "DONE: ok"}), _FailingLoader())
    ctx.loaded_preset = "stale"
    engine.run_phases(ctx)
    assert ctx.loaded_preset is None


# -- gate: on_block back-edge traversal -------------------------------------------


def _ci_pipeline(tmp_path: Path) -> QuillfolioConfig:
    """impl → commit → ci, where a CI BLOCK re-runs BOTH impl and commit."""
    return _config(
        tmp_path,
        [
            PhaseDef(
                id="impl",
                type="producer",
                persona="impl.md",
                models=("impl-m",),
                artifact="impl.md",
            ),
            PhaseDef(
                id="commit",
                type="producer",
                persona="commit.md",
                models=("commit-m",),
                artifact="commit.md",
            ),
            PhaseDef(
                id="ci",
                type="mechanical",
                step="ci_check",
                gates=True,
                retry_budget=1,
                on_block=("impl",),
            ),
        ],
    )


def _scripted_mechanical(monkeypatch: pytest.MonkeyPatch, *outcomes: Outcome) -> None:
    """Drive the ci phase's verdicts without a live GitHub, one per call."""
    verdicts = iter(outcomes)

    def fake(ctx: RunContext, phase: PhaseDef, *, spawn: object) -> PhaseResult:
        return PhaseResult(next(verdicts), "scripted")

    monkeypatch.setattr(engine, "run_mechanical", fake)


def test_gate_follows_on_block_back_edge_and_retraverses_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CI back-edge to impl retraverses the intervening commit before CI verifies again."""
    spawn = _Spawn({"impl": "DONE: implemented", "commit": "DONE: pushed"})
    ctx = _ctx(tmp_path, _ci_pipeline(tmp_path), spawn, _FakeLoader())
    _scripted_mechanical(monkeypatch, Outcome.BLOCK, Outcome.PASS)

    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    order = [a for (a, _, _) in spawn.calls]
    # impl, commit (first pass), then jump to impl and traverse forward through commit again.
    assert order == ["impl", "commit", "impl", "commit"]


def test_gate_stops_the_revise_sequence_when_an_earlier_phase_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pushing after a failed fix would publish a broken or empty revision."""
    # impl succeeds on the first pass (so the run reaches the gate), then fails on the revise.
    spawn = _Spawn(
        {"impl": ["DONE: implemented", "FAILED: could not fix"], "commit": "DONE: pushed"}
    )
    ctx = _ctx(tmp_path, _ci_pipeline(tmp_path), spawn, _FakeLoader())
    _scripted_mechanical(monkeypatch, Outcome.BLOCK, Outcome.PASS)

    engine.run_phases(ctx)

    order = [a for (a, _, _) in spawn.calls]
    # The revise ran impl (which failed) and stopped — commit was never reached a second time.
    assert order == ["impl", "commit", "impl"]


def test_only_the_first_on_block_phase_is_primed_with_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """impl is being asked to fix something; commit is a follow-on step whose persona already
    says what to do. Handing commit the findings invites it to re-litigate a finished fix."""
    spawn = _Spawn({"impl": "DONE: implemented", "commit": "DONE: pushed"})
    ctx = _ctx(tmp_path, _ci_pipeline(tmp_path), spawn, _FakeLoader())
    (ctx.run_dir / "ci-findings.md").write_text("the CI failure", encoding="utf-8")
    _scripted_mechanical(monkeypatch, Outcome.BLOCK, Outcome.PASS)

    engine.run_phases(ctx)

    revise_impl = [p for (a, _, p) in spawn.calls if a == "impl"][1]
    revise_commit = [p for (a, _, p) in spawn.calls if a == "commit"][1]
    assert "ci-findings.md" in revise_impl
    assert "ci-findings.md" not in revise_commit


def test_create_mode_ci_retry_forbids_pr_comment(tmp_path: Path) -> None:
    spawn = _Spawn({"impl": "DONE: implemented", "commit": "DONE: pushed"})
    ctx = _ctx(tmp_path, _ci_pipeline(tmp_path), spawn, _FakeLoader())

    engine.run_phases(ctx)

    commit_prompt = next(prompt for agent, _, prompt in spawn.calls if agent == "commit")
    assert "Do not comment on an existing PR in create mode" in commit_prompt


def test_gate_with_no_on_block_blocks(tmp_path: Path) -> None:
    """A gate that can BLOCK but names nothing to re-run has no way forward."""
    config = _config(
        tmp_path,
        [
            PhaseDef(
                id="review",
                type="reviewer",
                persona="review.md",
                models=("m",),
                gates=True,
                retry_budget=1,
            )
        ],
    )
    spawn = _Spawn({"review": "BLOCK: no"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())

    final = engine.run_phases(ctx)

    assert final["type"] == "run_failed"


# -- gate: BLOCK -> revise on_block -> verify PASS ---------------------------------


def test_gate_block_revises_onblock_then_passes(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("plan-m",),
            artifact="plan.md",
        ),
        PhaseDef(
            id="review_plan",
            type="reviewer",
            persona="personas/review-plan.md",
            models=("rev-m",),
            gates=True,
            retry_budget=1,
            on_block=("plan",),
        ),
    ]
    config = replace(
        _config(tmp_path, phases),
        memory_enabled=True,
        memory_root=tmp_path / "memory",
    )
    # Reviewer BLOCKs first, then (verify) PASSes. Producer DONE both times.
    spawn = _Spawn(
        {
            "plan": ["DONE: planned", "DONE: revised"],
            "review_plan": ["BLOCK: missing tests", "PASS: tests added"],
        }
    )
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)

    final = engine.run_phases(ctx)
    assert final["type"] == "run_done"
    assert "retry" in _ev_types(ctx)
    # plan ran twice (initial + revise); review ran twice (initial BLOCK + verify PASS).
    assert sum(1 for (a, _, _) in spawn.calls if a == "plan") == 2
    assert sum(1 for (a, _, _) in spawn.calls if a == "review_plan") == 2
    # Both review verdicts are emitted — the initial BLOCK AND the verify PASS. A silent verify (no
    # gate_verdict) would leave a reader unable to tell why a retry did or didn't happen.
    verdicts = [
        e.get("verdict") for e in _events_of(ctx, "gate_verdict") if e["phase"] == "review_plan"
    ]
    assert verdicts == ["BLOCK", "PASS"]
    # The verify spawn also emits its own phase_started (so the console shows the verify running).
    assert sum(1 for e in _events_of(ctx, "phase_started") if e["phase"] == "review_plan") == 2
    # The engine — not the persona — carries verify semantics: only the 2nd (verify) review prompt
    # requires both prior-finding verification and a bounded regression audit. Only regressions
    # tied to an exact revision change may become new blockers.
    review_prompts = [p for (a, _, p) in spawn.calls if a == "review_plan"]
    assert "bounded regression audit" not in review_prompts[0]
    assert "bounded regression audit" in review_prompts[1]
    assert "introduced_by_revision" in review_prompts[1]
    assert "VERIFICATION mode" in review_prompts[1]
    # The REVISE producer must be pointed at the reviewer's findings file so it fixes what was
    # flagged instead of re-writing blind (else the reviewer re-BLOCKs on the same finding). The
    # first plan spawn (initial) has no findings; the second (revise) names review-review_plan-*.md.
    plan_prompts = [p for (a, _, p) in spawn.calls if a == "plan"]
    assert "REVISION" not in plan_prompts[0]
    assert "review-review_plan-rev-m.md" in plan_prompts[1]
    assert "REVISION" in plan_prompts[1]
    assert "missing tests" in verified_memory_block(ctx, "plan")


def test_gate_double_block_promotes_both_only_after_final_pass(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("plan-m",),
            artifact="plan.md",
        ),
        PhaseDef(
            id="review_plan",
            type="reviewer",
            persona="personas/review-plan.md",
            models=("rev-m",),
            gates=True,
            retry_budget=2,
            on_block=("plan",),
        ),
    ]
    config = replace(
        _config(tmp_path, phases),
        memory_enabled=True,
        memory_root=tmp_path / "memory",
    )
    spawn = _Spawn(
        {
            "plan": ["DONE: initial", "DONE: first repair", "DONE: second repair"],
            "review_plan": [
                "BLOCK: missing tests",
                "BLOCK: tests use an invented API",
                "PASS: both findings resolved",
            ],
        }
    )
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())

    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    path = config.memory_root / "me" / "proj" / "blockers.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == [
        "blocked",
        "blocked",
        "resolved",
        "resolved",
    ]
    block = verified_memory_block(ctx, "plan")
    assert "missing tests" in block
    assert "tests use an invented API" in block


@pytest.mark.parametrize("terminal", ["FAILED: repair failed", "unparseable output"])
def test_gate_block_followed_by_failed_or_garbage_repair_stays_unresolved(
    tmp_path: Path, terminal: str
) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("plan-m",),
            artifact="plan.md",
        ),
        PhaseDef(
            id="review_plan",
            type="reviewer",
            persona="personas/review-plan.md",
            models=("rev-m",),
            gates=True,
            retry_budget=2,
            on_block=("plan",),
        ),
    ]
    config = replace(
        _config(tmp_path, phases),
        memory_enabled=True,
        memory_root=tmp_path / "memory",
    )
    spawn = _Spawn(
        {
            "plan": ["DONE: initial", terminal],
            "review_plan": ["BLOCK: missing tests", "PASS: must never verify"],
        }
    )
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())

    final = engine.run_phases(ctx)

    assert final["type"] == "run_failed"
    assert sum(1 for agent, _preset, _prompt in spawn.calls if agent == "review_plan") == 1
    path = config.memory_root / "me" / "proj" / "blockers.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["blocked"]
    assert verified_memory_block(ctx, "plan") == ""


def test_gate_block_followed_by_garbage_verification_stays_unresolved(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("plan-m",),
            artifact="plan.md",
        ),
        PhaseDef(
            id="review_plan",
            type="reviewer",
            persona="personas/review-plan.md",
            models=("rev-m",),
            gates=True,
            retry_budget=2,
            on_block=("plan",),
        ),
    ]
    config = replace(
        _config(tmp_path, phases),
        memory_enabled=True,
        memory_root=tmp_path / "memory",
    )
    spawn = _Spawn(
        {
            "plan": ["DONE: initial", "DONE: repaired"],
            "review_plan": ["BLOCK: missing tests", "unparseable verifier output"],
        }
    )
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())

    final = engine.run_phases(ctx)

    assert final["type"] == "run_failed"
    path = config.memory_root / "me" / "proj" / "blockers.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["blocked"]
    assert verified_memory_block(ctx, "plan") == ""


def test_gate_block_exhausts_budget_fails(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("plan-m",),
            artifact="plan.md",
        ),
        PhaseDef(
            id="review_plan",
            type="reviewer",
            persona="personas/review-plan.md",
            models=("rev-m",),
            gates=True,
            retry_budget=1,
            on_block=("plan",),
        ),
    ]
    config = replace(
        _config(tmp_path, phases),
        memory_enabled=True,
        memory_root=tmp_path / "memory",
    )
    spawn = _Spawn({"plan": "DONE: planned", "review_plan": "BLOCK: still broken"})
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)

    final = engine.run_phases(ctx)
    assert final["type"] == "run_failed"
    path = config.memory_root / "me" / "proj" / "blockers.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["blocked", "blocked"]
    assert verified_memory_block(ctx, "plan") == ""


# -- mechanical -------------------------------------------------------------------


def test_mechanical_build_test_block_fails(tmp_path: Path) -> None:
    phases = [PhaseDef(id="bt", type="mechanical", step="build_test", gates=True, retry_budget=0)]
    config = _config(tmp_path, phases)
    spawn = _Spawn({})
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)
    ctx.deps.build_test = lambda _c, _selection: (False, "compile error")

    final = engine.run_phases(ctx)
    assert final["type"] == "run_failed"
    assert "gate_verdict" in _ev_types(ctx)


@pytest.mark.parametrize("step", ["test", "build", "build_test"])
def test_local_gate_block_passes_failure_log_into_impl_revise(tmp_path: Path, step: str) -> None:
    """A failing build_test writes build-findings.md and the impl revise is pointed at it, so the
    retry sees the compiler errors instead of building blind."""
    phases = [
        PhaseDef(
            id="impl",
            type="producer",
            persona="personas/impl.md",
            models=("m",),
            artifact="impl.md",
        ),
        PhaseDef(
            id="local_gate",
            type="mechanical",
            step=step,
            gates=True,
            retry_budget=1,
            on_block=("impl",),
        ),
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"impl": ["DONE: implemented", "DONE: fixed the build"]})
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)
    # Fail the first build, pass the second (after the revise) — exercises the revise round.
    calls = {"n": 0}

    def bt(_c: object, _selection: str) -> tuple[bool, str]:
        calls["n"] += 1
        return (True, "ok") if calls["n"] > 1 else (False, "error: 'foo' was not declared")

    ctx.deps.build_test = bt  # type: ignore[assignment]

    final = engine.run_phases(ctx)
    assert final["type"] == "run_done"
    # The failure log was persisted as a run-dir findings file...
    findings = ctx.run_dir / "build-findings.md"
    assert findings.is_file()
    assert "error: 'foo' was not declared" in findings.read_text(encoding="utf-8")
    # ...and the impl REVISE prompt names it, so the retry knows what to fix.
    impl_prompts = [p for (a, _, p) in spawn.calls if a == "impl"]
    assert "build-findings.md" not in impl_prompts[0]  # initial impl: no findings yet
    assert "build-findings.md" in impl_prompts[1]  # revise: pointed at the build failure
    assert "REVISION" in impl_prompts[1]


# -- stop + needs-decision --------------------------------------------------------


def test_stop_requested_halts(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("m",),
            artifact="plan.md",
        )
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "DONE: ok"})
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)
    ctx.should_stop = lambda: True  # type: ignore[assignment]

    final = engine.run_phases(ctx)
    assert final["type"] == "run_halted"
    assert spawn.calls == []


def test_needs_decision_unanswered_halts(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("m",),
            artifact="plan.md",
        )
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "FAILED: needs decision — which API? | result: plan.md"})
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)

    def answer_after_publication(_question: str) -> None:
        assert _ev_types(ctx)[-1] == "needs_decision"

    ctx.answer_decision = answer_after_publication

    final = engine.run_phases(ctx)
    assert final["type"] == "run_halted"
    assert "needs_decision" in _ev_types(ctx)


def test_start_phase_skips_earlier(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("m",),
            artifact="plan.md",
        ),
        PhaseDef(
            id="impl",
            type="producer",
            persona="personas/impl.md",
            models=("m2",),
            artifact="impl.md",
        ),
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "DONE: p", "impl": "DONE: i"})
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)

    final = engine.run_phases(ctx, start_phase="impl")
    assert final["type"] == "run_done"
    assert [a for (a, _, _) in spawn.calls] == ["impl"]


def test_unknown_start_phase_runs_nothing(tmp_path: Path) -> None:
    # Defensive: an unmatched start id means no phase ever flips `started` -> run completes empty.
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("m",),
            artifact="plan.md",
        )
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "DONE: ok"})
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)
    final = engine.run_phases(ctx, start_phase="ghost")
    assert final["type"] == "run_done"
    assert spawn.calls == []


@pytest.mark.parametrize("verb,etype", [("DONE", "run_done"), ("FAILED", "run_failed")])
def test_producer_terminal_outcomes(tmp_path: Path, verb: str, etype: str) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("m",),
            artifact="plan.md",
        )
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": f"{verb}: x"})
    loader = _FakeLoader()
    ctx = _ctx(tmp_path, config, spawn, loader)
    final = engine.run_phases(ctx)
    assert final["type"] == etype


# -- artifact existence gate ------------------------------------------------------


def test_producer_done_but_no_artifact_fails(tmp_path: Path) -> None:
    """A producer that reports DONE without writing its artifact must fail the run, not advance.

    The receipt is not the source of truth — the file on disk is. A missing artifact is caught
    as GARBAGE so the run fails on the real cause instead of a later phase reading a missing file.
    """
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("m",),
            artifact="plan.md",
        )
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "DONE: wrote plan"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    spawn.skip_write.add("plan")  # claim success but write nothing

    final = engine.run_phases(ctx)
    assert final["type"] == "run_failed"
    assert final["phase"] == "plan"
    assert "plan.md" in str(final["reason"])


def test_pi_repairs_plan_printed_in_chat_in_the_same_session(tmp_path: Path) -> None:
    """The observed local-model failure gets one narrow continuation, not a discarded run."""
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("gemma",),
            artifact="plan.md",
        )
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "```markdown\n# Comprehensive plan\n```"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    repairs: list[tuple[str, str, str, Path]] = []

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
        repairs.append((agent, preset, prompt, stream_path))
        ctx.artifact_path("plan.md").write_text("# Comprehensive plan\n", encoding="utf-8")
        return f"DONE: wrote plan.md | result: {ctx.artifact_path('plan.md')}"

    ctx.deps.session_repair = repair
    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    assert len(repairs) == 1
    assert repairs[0][0:2] == ("plan", "gemma")
    assert "did not complete the required artifact contract" in repairs[0][2]
    assert repairs[0][3].name.startswith("stream-plan-gemma-2")


def test_phase_restart_never_overwrites_an_inherited_transcript(tmp_path: Path) -> None:
    phase = PhaseDef(
        id="plan",
        type="producer",
        persona="personas/plan.md",
        models=("gemma",),
        artifact="plan.md",
    )
    config = _config(tmp_path, [phase])
    ctx = _ctx(tmp_path, config, _Spawn({"plan": "DONE: ok"}), _FakeLoader())
    inherited = ctx.run_dir / "stream-plan-gemma-1.jsonl"
    inherited.write_text("prior execution\n", encoding="utf-8")

    selected = engine._stream_path(ctx, phase, "gemma")

    assert selected.name == "stream-plan-gemma-2.jsonl"
    assert inherited.read_text(encoding="utf-8") == "prior execution\n"


def test_pi_repairs_missing_receipt_without_rewriting_existing_artifact(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("gemma",),
            artifact="plan.md",
        )
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "plan printed without a receipt"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    artifact = ctx.artifact_path("plan.md")
    artifact.write_text("already written", encoding="utf-8")
    prompts: list[str] = []

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
        prompts.append(prompt)
        return f"DONE: wrote plan.md | result: {artifact}"

    ctx.deps.session_repair = repair
    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    assert prompts and "existence does not prove completion" in prompts[0]
    assert "If incomplete, continue working now" in prompts[0]
    assert artifact.read_text(encoding="utf-8") == "already written"
    assert _events_of(ctx, "self_fix_done")[0]["repaired"] is True


def test_pi_repairs_malformed_structured_findings_in_same_session(tmp_path: Path) -> None:
    review = PhaseDef(
        id="review_plan",
        type="reviewer",
        persona="personas/review-plan.md",
        models=("qwen",),
        gates=True,
        structured_findings=True,
        retry_budget=0,
    )
    config = _config(tmp_path, [review])
    spawn = _Spawn({"review_plan": "PASS: artifact written"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    spawn.skip_write.add("review_plan")
    ctx.artifact_path("review-review_plan-qwen.md").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "findings": [
                    {
                        "id": "F1",
                        "severity": "MAJOR",
                        "status": "OPEN",
                        "title": "Missing requirement",
                        "requirement": "Preserve registry identity",
                        "evidence": "plan.md omits identity ownership",
                        "failure_scenario": "Definitions can change identity",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    repairs: list[str] = []

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
        repairs.append(prompt)
        ctx.artifact_path("review-review_plan-qwen.md").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "findings": [
                        {
                            "id": "F1",
                            "severity": "MAJOR",
                            "status": "OPEN",
                            "title": "Missing requirement",
                            "requirement": "Preserve registry identity",
                            "evidence": "plan.md omits identity ownership",
                            "failure_scenario": "Definitions can change identity",
                            "required_outcome": "Define immutable identity ownership",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return "BLOCK: artifact repaired"

    ctx.deps.session_repair = repair

    result = engine._run_reviewer(ctx, review)

    assert result.outcome is Outcome.BLOCK
    assert "F1 (MAJOR)" in result.message
    assert len(repairs) == 1
    assert "finding #1 has invalid required_outcome" in repairs[0]
    assert "required_outcome" in repairs[0]


def test_failed_structured_self_fix_remains_eligible_for_fresh_phase_attempt(
    tmp_path: Path,
) -> None:
    review = PhaseDef(
        id="review_plan",
        type="reviewer",
        persona="personas/review-plan.md",
        models=("qwen",),
        gates=True,
        structured_findings=True,
        retry_budget=0,
    )
    config = _config(tmp_path, [review])
    spawn = _Spawn({"review_plan": "PASS: artifact written"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    spawn.skip_write.add("review_plan")
    ctx.artifact_path("review-review_plan-qwen.md").write_text(
        json.dumps({"schema_version": 1, "findings": [{"id": "F1"}]}),
        encoding="utf-8",
    )
    ctx.deps.session_repair = lambda *args, **kwargs: "FAILED: could not repair findings JSON"

    result = engine._run_reviewer(ctx, review)

    assert result.outcome is Outcome.GARBAGE
    assert result.allow_phase_retry is True
    assert "same-session self-fix did not repair malformed output" in result.message


def test_fresh_phase_attempt_records_its_own_restart_boundary(tmp_path: Path) -> None:
    phase = PhaseDef(
        id="impl",
        type="producer",
        persona="personas/impl.md",
        models=("qwen",),
        artifact="impl.md",
    )
    ctx = _ctx(tmp_path, _config(tmp_path, [phase]), _Spawn({}), _FakeLoader())
    checkpoints: list[str] = []
    ctx.checkpoint_phase = checkpoints.append
    results = [
        PhaseResult(Outcome.GARBAGE, "malformed", allow_phase_retry=True),
        PhaseResult(Outcome.DONE, "fixed"),
    ]

    result = engine._run_with_fresh_attempts(ctx, phase, lambda: results.pop(0))

    assert result.outcome is Outcome.DONE
    assert checkpoints == ["impl"]


def test_structured_gate_repair_receives_exact_prior_and_retries_contract(tmp_path: Path) -> None:
    phase = PhaseDef(
        id="review_plan",
        type="reviewer",
        persona="personas/review-plan.md",
        models=("qwen",),
        gates=True,
        structured_findings=True,
    )
    config = _config(tmp_path, [phase])
    ctx = _ctx(tmp_path, config, _Spawn({}), _FakeLoader())
    artifact = ctx.artifact_path("review-review_plan-qwen.md")
    prior_row = {
        "id": "F1",
        "severity": "MAJOR",
        "status": "OPEN",
        "title": "Capacity validation uses assert in release builds",
        "requirement": "Invalid capacity must be rejected in every build",
        "evidence": "plan.md specifies assert(total_capacity > 0)",
        "failure_scenario": "Release builds accept invalid capacity",
        "required_outcome": "Use runtime validation that remains active in release builds",
    }
    artifact.write_text(
        json.dumps({"schema_version": 1, "findings": [prior_row]}), encoding="utf-8"
    )
    prior = load_findings(artifact)
    wrong_row = {
        **prior_row,
        "severity": "CRITICAL",
        "status": "RESOLVED",
        "title": "GameEvents was not injected",
        "requirement": "Inject GameEvents",
    }
    artifact.write_text(
        json.dumps({"schema_version": 1, "findings": [wrong_row]}), encoding="utf-8"
    )
    prompts: list[str] = []

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
        prompts.append(prompt)
        if len(prompts) == 2:
            repaired = {
                **prior_row,
                "status": "RESOLVED",
                "evidence": "plan.md now specifies an always-active validation branch",
            }
            artifact.write_text(
                json.dumps({"schema_version": 1, "findings": [repaired]}), encoding="utf-8"
            )
        return "PASS: repaired structured contract"

    ctx.deps.session_repair = repair

    result = engine._resolve_structured_gate(
        ctx,
        phase,
        model="qwen",
        artifact=artifact.name,
        result=PhaseResult(Outcome.PASS, "verified"),
        prior=prior,
    )

    assert result.outcome is Outcome.PASS
    assert len(prompts) == 2
    assert "Contract repair 1/3" in prompts[0]
    assert "Contract repair 2/3" in prompts[1]
    assert "Capacity validation uses assert in release builds" in prompts[0]
    assert "Invalid capacity must be rejected in every build" in prompts[0]
    assert "GameEvents was not injected" not in prompts[0]


def test_verification_task_includes_authoritative_prior_finding_identity(tmp_path: Path) -> None:
    phase = PhaseDef(
        id="review_plan",
        type="reviewer",
        persona="personas/review-plan.md",
        models=("qwen",),
        gates=True,
        structured_findings=True,
    )
    config = _config(tmp_path, [phase])
    ctx = _ctx(tmp_path, config, _Spawn({}), _FakeLoader())
    artifact = ctx.artifact_path("prior.json")
    artifact.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "findings": [
                    {
                        "id": "F1",
                        "severity": "MAJOR",
                        "status": "OPEN",
                        "title": "Capacity validation uses assert in release builds",
                        "requirement": "Invalid capacity must be rejected in every build",
                        "evidence": "plan.md specifies assert(total_capacity > 0)",
                        "failure_scenario": "Release builds accept invalid capacity",
                        "required_outcome": (
                            "Use runtime validation that remains active in release builds"
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    task = engine._review_task(
        ctx,
        phase,
        "review-review_plan-qwen.md",
        verify=True,
        prior_findings=load_findings(artifact),
    )

    # A gating verification answers with dispositions, so the prior blocker is listed as context
    # to adjudicate by id — never as fields to transcribe back.
    assert "PRIOR BLOCKERS you must adjudicate" in task
    assert "F1 (MAJOR): Capacity validation uses assert in release builds" in task
    assert "Use runtime validation that remains active in release builds" in task
    assert "Reference each one by id in dispositions." in task
    assert '"dispositions"' in task
    assert "do not restate" in task.lower()
    # The old contract demanded a byte-identical copy of every immutable field; that is exactly the
    # transcription step this change removes.
    assert "collision-normalized JSON" not in task
    assert '"failure_scenario":"Release builds accept invalid capacity"' not in task


def test_configured_self_check_continues_same_session_after_success(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("gemma",),
            artifact="plan.md",
            self_check=True,
        )
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "DONE: wrote plan"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    prompts: list[str] = []

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
        prompts.append(prompt)
        return f"DONE: self-checked | result: {ctx.artifact_path('plan.md')}"

    ctx.deps.session_repair = repair
    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    assert len(prompts) == 1
    assert "required skills" in prompts[0]
    assert "independent review phase" in prompts[0]
    assert _ev_types(ctx).count("self_check_started") == 1
    assert _events_of(ctx, "self_check_done")[0]["verdict"] == "DONE"


def test_self_check_cannot_fail_a_successful_phase(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("gemma",),
            artifact="plan.md",
            self_check=True,
        )
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "DONE: wrote plan"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    prompts: list[str] = []

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
        prompts.append(prompt)
        return "FAILED: self-check continuation returned an invalid terminal result"

    ctx.deps.session_repair = repair
    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    assert len(prompts) == 1
    assert "only self-check iteration for this phase attempt" in prompts[0]
    assert _events_of(ctx, "self_check_done")[0]["verdict"] == "FAILED"


def test_oversized_artifact_is_compacted_in_same_session(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("gemma",),
            artifact="plan.md",
            max_artifact_chars=20,
        )
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "DONE: wrote plan"})
    spawn.skip_write.add("plan")
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    artifact = ctx.artifact_path("plan.md")
    prompts: list[str] = []

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
        prompts.append(prompt)
        artifact.write_text("compact plan", encoding="utf-8")
        return f"DONE: wrote plan | result: {artifact}"

    ctx.deps.session_repair = repair
    artifact.write_text("x" * 30, encoding="utf-8")
    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    assert prompts and "exceeds its 20-character handoff limit" in prompts[0]
    assert artifact.read_text(encoding="utf-8") == "compact plan"


def test_producer_prompt_names_only_configured_handoff_inputs(tmp_path: Path) -> None:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("m",),
            artifact="plan.md",
        ),
        PhaseDef(
            id="impl",
            type="producer",
            persona="personas/impl.md",
            models=("m",),
            artifact="impl.md",
            inputs=("plan",),
            max_artifact_chars=8_000,
        ),
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "DONE: planned", "impl": "DONE: implemented"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())

    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    impl_prompt = next(prompt for agent, _preset, prompt in spawn.calls if agent == "impl")
    assert f"Required handoff inputs: {ctx.artifact_path('plan.md')}" in impl_prompt
    assert "Keep the artifact under 8,000 characters" in impl_prompt


def test_reviewer_done_but_no_findings_fails(tmp_path: Path) -> None:
    """A reviewer that reports DONE without writing its findings file fails the run."""
    phases = [
        PhaseDef(
            id="impl",
            type="producer",
            persona="personas/impl.md",
            models=("m",),
            artifact="impl.md",
        ),
        PhaseDef(
            id="review_impl",
            type="reviewer",
            persona="personas/review-impl.md",
            models=("gemma-X", "qwen 27b"),
            against=("impl",),
        ),
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"impl": "DONE: implemented", "review_impl": "DONE: reviewed"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    spawn.skip_write.add("review_impl")  # reviewer narrates success, writes no findings

    final = engine.run_phases(ctx)
    assert final["type"] == "run_failed"
    assert final["phase"] == "review_impl"


# -- ticket injection -------------------------------------------------------------


def test_ticket_injected_into_every_phase(tmp_path: Path) -> None:
    """The ticket title+body is injected into producer, reviewer, AND finalizer prompts so the
    reviewers/finalizer judge against the goal — not a blind 'read the ticket yourself' line."""
    phases = [
        PhaseDef(
            id="impl",
            type="producer",
            persona="personas/impl.md",
            models=("m",),
            artifact="impl.md",
        ),
        PhaseDef(
            id="review_impl", type="reviewer", persona="personas/review-impl.md", models=("rm",)
        ),
        PhaseDef(
            id="final",
            type="finalizer",
            persona="personas/review-final.md",
            models=("fm",),
            reconciles=("review_impl",),
        ),
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"impl": "DONE: x", "review_impl": "DONE: x", "final": "DONE: x"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    # ctx.body is the raw `gh issue view --json title,body` JSON; the block extracts the prose.
    ctx.title = "Add retry budget"
    ctx.body = json.dumps(
        {"title": "Add retry budget", "body": "Each gate should retry N times before failing."}
    )

    engine.run_phases(ctx)

    for agent in ("impl", "review_impl", "final"):
        prompt = next(p for (a, _, p) in spawn.calls if a == agent)
        assert "TICKET #33: Add retry budget" in prompt, agent
        assert "Each gate should retry N times before failing." in prompt, agent
        # The raw JSON envelope is not dumped into the prompt — only the body prose.
        assert '{"title"' not in prompt, agent
        # The blind self-fetch line is gone; the block carries the goal instead.
        assert "Read ticket #33 from the repo" not in prompt, agent


class _FetchGit:
    """Minimal git whose issue_body returns a fixed (possibly empty) body."""

    def __init__(self, body: str) -> None:
        self._body = body

    def issue_body(self, ticket: int) -> str:
        return self._body


def test_empty_ticket_body_fails_run(tmp_path: Path) -> None:
    """No goal to plan/review against → fail the run rather than spawn blind agents."""
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("m",),
            artifact="plan.md",
        )
    ]
    config = _config(tmp_path, phases)
    spawn = _Spawn({"plan": "DONE: x"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    ctx.title = ""
    ctx.body = ""  # branch phase didn't run (resume/start-phase); guard must fetch + reject empty
    ctx.deps = replace(ctx.deps, git=_FetchGit("   "))

    final = engine.run_phases(ctx)
    assert final["type"] == "run_failed"
    assert final["phase"] == "ticket"
    # No phase spawned.
    assert spawn.calls == []


def test_assemble_prompt_appends_skill_directive(tmp_path: Path) -> None:
    """A phase with skills gets the runner's skill-load line BEFORE the task, so TASK ends the
    prompt (a runner that expands /skill:x inline must not bury the task); none => nothing added."""
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            skills=("cpp-pro", "plan-mode"),
            models=("m",),
            artifact="plan.md",
        )
    ]
    config = _config(tmp_path, phases)
    ctx = _ctx(tmp_path, config, _Spawn({}), _FakeLoader())

    # Runner-style directive: prefix each name (stand-in for pi's /skill:<name>).
    ctx.deps = replace(
        ctx.deps,
        skill_directive=lambda names: (
            "LOAD " + " ".join(f"/skill:{n}" for n in names) if names else ""
        ),
    )
    plan = config.phase("plan")
    assert plan is not None
    prompt = engine.assemble_prompt(ctx, plan, "TASK: do it.")
    # The task is now the LAST thing in the prompt; the skill directive precedes it.
    assert prompt.rstrip().endswith("TASK: do it.")
    assert prompt.index("LOAD /skill:cpp-pro /skill:plan-mode") < prompt.index("TASK: do it.")

    # No skills => no trailing directive.
    plan_no_skills = replace(plan, skills=())
    bare = engine.assemble_prompt(ctx, plan_no_skills, "TASK: do it.")
    assert "LOAD /skill:" not in bare
    assert bare.rstrip().endswith("TASK: do it.")


# -- update mode (quill --update <ticket>) ----------------------------------------


class _UpdateGit:
    """Git fake for update runs: a fixed ticket body plus a scripted PR + feedback."""

    def __init__(
        self,
        body: str = '{"title":"T","body":"Do the thing."}',
        pr: PullRequest | None = None,
        feedback: str = "",
        pr_raises: bool = False,
        feedback_raises: bool = False,
    ) -> None:
        self._body = body
        self._pr = pr
        self._feedback = feedback
        self._pr_raises = pr_raises
        self._feedback_raises = feedback_raises
        self.feedback_calls: list[int] = []

    def issue_body(self, ticket: int) -> str:
        return self._body

    def pr_target_for_ticket(self, ticket: int) -> PullRequest | None:
        if self._pr_raises:
            raise RuntimeError("gh exploded")
        return self._pr

    def feedback_snapshot(self, pr: PullRequest) -> FeedbackSnapshot:
        self.feedback_calls.append(pr.number)
        if self._feedback_raises:
            raise RuntimeError("gh exploded")
        selected = (
            (
                FeedbackItem(
                    "feedback-1", "inline", "reviewer", self._feedback, "2026-07-02T00:00:00Z"
                ),
            )
            if self._feedback
            else ()
        )
        return FeedbackSnapshot(pr, selected)


def _update_ctx(tmp_path: Path, spawn: _Spawn, git: _UpdateGit) -> RunContext:
    phases = [
        PhaseDef(
            id="plan",
            type="producer",
            persona="personas/plan.md",
            models=("m",),
            artifact="plan.md",
        )
    ]
    config = _config(tmp_path, phases)
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    ctx.mode = "update"
    ctx.deps = replace(ctx.deps, git=git)  # type: ignore[arg-type]
    return ctx


def test_update_mode_injects_pr_feedback_into_every_phase(tmp_path: Path) -> None:
    """The PR's review feedback is the goal of an update run, so every phase must see it."""
    pr = PullRequest(number=34, branch="ticket-33-engine", title="T", url="https://x/pull/34")
    git = _UpdateGit(pr=pr, feedback="[inline] dan @ engine.py:42:\nrename this")
    spawn = _Spawn({"plan": "DONE: ok"})
    ctx = _update_ctx(tmp_path, spawn, git)

    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    prompt = next(p for (a, _, p) in spawn.calls if a == "plan")
    assert "UPDATE MODE" in prompt
    assert "PR #34" in prompt
    assert "rename this" in prompt
    # The ticket goal is still injected alongside the feedback — an update is not a blank slate.
    assert "TICKET #33" in prompt
    assert git.feedback_calls == [34]


def test_update_mode_tells_phases_to_reuse_the_existing_branch(tmp_path: Path) -> None:
    """The branch phase must check the PR's branch out, not cut a new one off the base.

    The engine has no notion of which configured phase runs git (that is persona data), so the
    rule travels in the run-wide UPDATE block rather than being special-cased per phase id.
    """
    pr = PullRequest(number=34, branch="ticket-33-engine", title="T", url="u")
    ctx = _update_ctx(tmp_path, _Spawn({"plan": "DONE: ok"}), _UpdateGit(pr=pr, feedback="fix it"))
    engine.run_phases(ctx)

    plan = ctx.config.phase("plan")
    assert plan is not None
    prompt = engine.assemble_prompt(ctx, plan, "TASK: do it.")
    assert "ticket-33-engine" in prompt
    assert "Do NOT create a new branch" in prompt
    assert "do NOT open a second pull request" in prompt
    assert ctx.branch == "ticket-33-engine"
    assert ctx.pr_number == 34


def test_review_mode_injects_exact_pr_boundary_without_requiring_feedback(tmp_path: Path) -> None:
    pr = PullRequest(number=34, branch="ticket-33-engine", title="T", url="u")
    git = _UpdateGit(pr=pr, feedback="ignored review feedback")
    spawn = _Spawn({"plan": "DONE: ok"})
    ctx = _update_ctx(tmp_path, spawn, git)
    ctx.mode = "review"

    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    prompt = next(prompt for agent, _model, prompt in spawn.calls if agent == "plan")
    assert "PULL REQUEST REVIEW MODE" in prompt
    assert "Do not modify any repository file" in prompt
    assert "complete PR diff" in prompt
    assert git.feedback_calls == []


def test_update_mode_fails_when_no_open_pr(tmp_path: Path) -> None:
    """Nothing to update: fail loudly instead of silently re-shipping the ticket from scratch."""
    spawn = _Spawn({"plan": "DONE: ok"})
    ctx = _update_ctx(tmp_path, spawn, _UpdateGit(pr=None))

    final = engine.run_phases(ctx)

    assert final["type"] == "run_failed"
    assert final["phase"] == "ticket"
    assert "no open PR" in str(final["reason"])
    assert spawn.calls == []  # no phase spawned


def test_update_mode_fails_when_comments_cannot_be_read(tmp_path: Path) -> None:
    """A PR whose comments won't load leaves the run with nothing to act on — stop, don't guess."""
    pr = PullRequest(number=34, branch="b", title="T", url="u")
    spawn = _Spawn({"plan": "DONE: ok"})
    ctx = _update_ctx(tmp_path, spawn, _UpdateGit(pr=pr, feedback_raises=True))

    final = engine.run_phases(ctx)

    assert final["type"] == "run_failed"
    assert "could not read its comments" in str(final["reason"])
    assert spawn.calls == []


def test_update_mode_fails_before_spawn_when_no_new_feedback(tmp_path: Path) -> None:
    pr = PullRequest(number=34, branch="b", title="T", url="u")
    spawn = _Spawn({"plan": "DONE: ok"})
    ctx = _update_ctx(tmp_path, spawn, _UpdateGit(pr=pr, feedback=""))

    final = engine.run_phases(ctx)

    assert final["type"] == "run_failed"
    assert "No PR feedback" in str(final["reason"])
    assert spawn.calls == []


def test_create_mode_never_reads_the_pr(tmp_path: Path) -> None:
    """A normal run must not gain an UPDATE block or spend gh calls looking for a PR."""
    pr = PullRequest(number=34, branch="b", title="T", url="u")
    git = _UpdateGit(pr=pr, feedback="should not appear")
    spawn = _Spawn({"plan": "DONE: ok"})
    ctx = _update_ctx(tmp_path, spawn, git)
    ctx.mode = "create"

    engine.run_phases(ctx)

    prompt = next(p for (a, _, p) in spawn.calls if a == "plan")
    assert "UPDATE MODE" not in prompt
    assert "should not appear" not in prompt
    assert git.feedback_calls == []


# -- gate convergence: verify-mode revise lanes + durable round budget -------------


def _blocker(finding_id: str, status: str = "OPEN", severity: str = "MAJOR") -> dict[str, str]:
    return {
        "id": finding_id,
        "severity": severity,
        "status": status,
        "title": "Required behavior is missing",
        "requirement": "Preserve required behavior",
        "evidence": "src/app.py:10 omits it",
        "failure_scenario": "The normal path fails",
        "required_outcome": "Implement the required behavior",
    }


def _audit_gate_pipeline(tmp_path: Path) -> QuillfolioConfig:
    """impl -> two concurrent audit lanes -> gating finalizer that routes back to impl."""
    return _config(
        tmp_path,
        [
            PhaseDef(
                id="impl",
                type="producer",
                persona="impl.md",
                models=("qwen",),
                artifact="impl.md",
            ),
            PhaseDef(
                id="review_impl",
                type="reviewer",
                audits=(
                    AuditDef("architecture", "Architecture", "architecture.md", "qwen"),
                    AuditDef("tests", "Tests", "tests.md", "qwen"),
                ),
                structured_findings=True,
            ),
            PhaseDef(
                id="review_impl_final",
                type="finalizer",
                persona="review-final.md",
                models=("qwen",),
                artifact="review_impl_final.md",
                reconciles=("review_impl",),
                gates=True,
                structured_findings=True,
                retry_budget=1,
                on_block=("impl",),
            ),
        ],
    )


def _carried(finding_id: str, severity: str = "MAJOR") -> Finding:
    return Finding(
        id=finding_id,
        severity=severity,
        status="OPEN",
        title=f"{finding_id} title",
        requirement="r",
        evidence="e",
        failure_scenario="fs",
        required_outcome=f"{finding_id} outcome",
    )


def test_prior_findings_prompt_separates_blockers_from_carried_advisories() -> None:
    """Ticket #20: the round-2 prompt announced findings that blocked nothing as PRIOR BLOCKERS.

    Under ``repeat-only`` a finding raised mid-loop is advisory on the round it appears. It still
    blocks the following round (deliberately — see ``BlockingPolicy``), but the gate must be told
    which of the two it is looking at.
    """
    held = (_carried("F1"), _carried("F4"), _carried("F5"), _carried("F6"))
    line = engine._prior_findings_instruction(held, delta=True, blocking_ids=frozenset({"F1"}))

    blockers, _, advisory = line.partition(" ALSO CARRIED")
    assert "F1" in blockers
    for advisory_id in ("F4", "F5", "F6"):
        assert advisory_id not in blockers, f"{advisory_id} blocked nothing but is named a blocker"
        assert advisory_id in advisory
    assert "block the next round if still unresolved" in advisory
    assert line.endswith(" Reference each one by id in dispositions.")


def test_prior_findings_prompt_without_a_policy_set_keeps_every_blocker() -> None:
    """Non-gate callers pass no blocking set and must keep the severity-only behavior."""
    held = (_carried("F1"), _carried("F2"))
    line = engine._prior_findings_instruction(held, delta=True)
    assert "ALSO CARRIED" not in line
    assert "F1" in line and "F2" in line


def test_synthesis_revise_is_not_told_to_resolve_lane_owned_findings(tmp_path: Path) -> None:
    """A synthesizer holds no evidence of its own, so 'address EVERY finding' can only be met by
    asserting. Ticket #20's gate rejected exactly that as 'asserted correctness by pattern-matching'.
    """
    lanes = [
        PhaseDef(
            id=name,
            type="producer",
            persona=f"{name}.md",
            models=("qwen",),
            artifact=f"{name}.md",
            parallel_group="research",
        )
        for name in ("requirements", "technical")
    ]
    synthesis = PhaseDef(
        id="research_synthesis",
        type="producer",
        persona="synthesis.md",
        models=("qwen",),
        artifact="research.md",
        synthesizes=tuple(lane.id for lane in lanes),
    )
    plain = PhaseDef(
        id="plan", type="producer", persona="plan.md", models=("qwen",), artifact="plan.md"
    )
    config = _config(tmp_path, [*lanes, synthesis, plain])
    ctx = _ctx(tmp_path, config, _Spawn({}), _FakeLoader())

    lane_task = engine._producer_task(
        ctx, lanes[1], "technical.md", findings="f.md", finding_owner="technical"
    )
    synthesis_task = engine._producer_task(ctx, synthesis, "research.md", findings="f.md")
    plain_task = engine._producer_task(ctx, plain, "plan.md", findings="f.md")

    assert "Address only findings whose owner is 'technical'" in lane_task
    assert "Address EVERY" not in synthesis_task
    assert "Do not resolve a finding the lane artifacts do not support." in synthesis_task
    # A producer that owns its whole artifact is still told to address everything.
    assert "Address EVERY Critical/Major finding" in plain_task


def test_revise_route_reviewers_run_in_verification_mode(tmp_path: Path) -> None:
    """The ticket #19 treadmill: audit lanes re-run inside a revise route must not audit fresh."""
    config = _audit_gate_pipeline(tmp_path)
    spawn = _Spawn({"review_impl_final": ["BLOCK: blocked", "PASS: resolved"]})
    # Lane and gate artifacts are pre-written valid findings JSON; the fake worker must not
    # overwrite them with placeholder prose.
    spawn.skip_write.update({"review_impl_final", "review_impl.architecture", "review_impl.tests"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())

    for name in ("review-review_impl-architecture.md", "review-review_impl-tests.md"):
        ctx.artifact_path(name).write_text(
            json.dumps({"schema_version": 1, "findings": [_blocker("F1")]}), encoding="utf-8"
        )
    # The finalizer must carry both lane blockers or the gate never reaches its revise loop.
    ctx.artifact_path("review_impl_final.md").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "findings": [_blocker("architecture:F1"), _blocker("tests:F1")],
            }
        ),
        encoding="utf-8",
    )

    engine._run_phase(ctx, config.phases[1])
    engine._run_finalizer(ctx, config.phases[2])

    lane_prompts = [p for (a, _, p) in spawn.calls if a.startswith("review_impl.")]
    # Two lanes on the first pass (plain review), two more inside the revise route (verification).
    assert len(lane_prompts) == 4
    assert all("VERIFICATION mode" not in p for p in lane_prompts[:2])
    assert all("VERIFICATION mode" in p for p in lane_prompts[2:])
    assert all("PRIOR BLOCKERS" in p for p in lane_prompts[2:])


def _simple_gate_pipeline(tmp_path: Path, budget: int) -> QuillfolioConfig:
    return _config(
        tmp_path,
        [
            PhaseDef(
                id="impl", type="producer", persona="impl.md", models=("qwen",), artifact="impl.md"
            ),
            PhaseDef(
                id="review",
                type="reviewer",
                persona="review.md",
                models=("qwen",),
                gates=True,
                retry_budget=budget,
                on_block=("impl",),
            ),
        ],
    )


def test_gate_rounds_survive_a_fresh_phase_attempt(tmp_path: Path) -> None:
    """A GARBAGE re-entry must not hand the gate a brand-new retry budget."""
    config = _simple_gate_pipeline(tmp_path, budget=1)
    ctx = _ctx(tmp_path, config, _Spawn({"impl": "DONE: ok"}), _FakeLoader())
    phase = config.phases[1]

    calls: list[int] = []

    def verify(attempt: int) -> PhaseResult:
        calls.append(attempt)
        return PhaseResult(Outcome.BLOCK, "still blocked")

    # First entry consumes the single configured round.
    first = engine._gate(ctx, phase, PhaseResult(Outcome.BLOCK, "blocked"), verify=verify)
    assert first.outcome is Outcome.BLOCK
    assert ctx.gate_rounds_spent["review"] == 1

    # A fresh phase attempt re-enters _gate; the exhausted budget must stay exhausted.
    second = engine._gate(ctx, phase, PhaseResult(Outcome.BLOCK, "blocked"), verify=verify)

    assert second.outcome is Outcome.BLOCK
    assert calls == [1], "verify must not run again after the per-run budget is spent"
    assert ctx.gate_rounds_spent["review"] == 1


def test_gate_retry_events_report_run_scoped_round_numbers(tmp_path: Path) -> None:
    config = _simple_gate_pipeline(tmp_path, budget=3)
    spawn = _Spawn({"impl": "DONE: ok", "review": "BLOCK: nope"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    ctx.gate_rounds_spent["review"] = 1

    engine._gate(ctx, config.phases[1], PhaseResult(Outcome.BLOCK, "blocked"))

    retries = [e for e in _EVENTS[id(ctx)] if e["type"] == "retry" and e["phase"] == "review"]
    # One round already spent, so the remaining two are reported as rounds 2 and 3 of 3.
    assert [(e["attempt"], e["max_attempts"]) for e in retries] == [(2, 3), (3, 3)]


def test_gating_verification_round_trips_a_status_delta(tmp_path: Path) -> None:
    """A re-review answers with dispositions; Quill reassembles the authoritative artifact."""
    config = _simple_gate_pipeline(tmp_path, budget=1)
    phase = replace(config.phases[1], structured_findings=True)
    config = _config(tmp_path, [config.phases[0], phase])
    findings_name = engine._findings_name(phase, "qwen")

    spawn = _Spawn({"review": "PASS: verified"})
    spawn.skip_write.add("review")
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())

    prior = (
        Finding(**_blocker("F1")),
        Finding(**_blocker("F2")),
    )
    # The worker writes only what it genuinely knows: a status and evidence per prior id.
    ctx.artifact_path(findings_name).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dispositions": [
                    {
                        "id": "F1",
                        "status": "RESOLVED",
                        "evidence": "src/app.py:10 now implements it",
                    },
                    {"id": "F2", "status": "RESOLVED", "evidence": "covered by a new test"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = engine._run_phase_for_verify(
        ctx, phase, prior_findings=prior, gate_round=engine.GateRound(index=1)
    )

    assert result.outcome is Outcome.PASS
    # Quill rebuilt the full contract from its own copy: identity intact, model's evidence applied.
    rebuilt = load_findings(ctx.artifact_path(findings_name))
    assert [(f.id, f.status) for f in rebuilt] == [("F1", "RESOLVED"), ("F2", "RESOLVED")]
    assert rebuilt[0].title == "Required behavior is missing"
    assert rebuilt[0].evidence == "src/app.py:10 now implements it"

    prompt = spawn.calls[-1][2]
    assert '"dispositions"' in prompt
    assert "PRIOR BLOCKERS you must adjudicate" in prompt


def test_structured_gate_self_check_runs_on_block_with_its_persona(tmp_path: Path) -> None:
    """A gate that BLOCKs still re-verifies its findings before the workflow pays for a retry."""
    producer = PhaseDef(
        id="impl",
        type="producer",
        persona="impl.md",
        models=("qwen",),
        artifact="impl.md",
    )
    gate = PhaseDef(
        id="review_impl_final",
        type="reviewer",
        persona="review-final.md",
        models=("qwen",),
        artifact="impl-findings.json",
        against=("impl",),
        gates=True,
        structured_findings=True,
        retry_budget=0,
        on_block=("impl",),
        self_check=True,
        self_check_persona="self-check-findings.md",
    )
    config = _config(tmp_path, [producer, gate])
    config.personas_root.mkdir(parents=True, exist_ok=True)
    (config.personas_root / "self-check-findings.md").write_text(
        "---\nname: self-check-findings\n---\nRe-open the file your evidence cites.",
        encoding="utf-8",
    )

    class BlockingSpawn(_Spawn):
        def _maybe_write_artifact(self, agent: str, prompt: str, receipt: str) -> None:
            if agent != "review_impl_final":
                super()._maybe_write_artifact(agent, prompt, receipt)
                return
            match = _ARTIFACT_RE.search(prompt)
            assert match is not None
            Path(match.group(1).rstrip(".")).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "findings": [
                            {
                                "id": "F1",
                                "severity": "MAJOR",
                                "status": "OPEN",
                                "title": "test does not cover the split case",
                                "requirement": "Ticket AC 1",
                                "evidence": "test_production.gd:585",
                                "failure_scenario": "regression ships",
                                "required_outcome": "cover the split case",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

    spawn = BlockingSpawn({"impl": "DONE: implemented", "review_impl_final": "BLOCK: F1 open"})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    prompts: list[str] = []

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
        prompts.append(prompt)
        return "BLOCK: F1 still open"

    ctx.deps.session_repair = repair
    engine.run_phases(ctx)

    assert len(prompts) == 1, "a blocking structured gate must still get its self-check"
    assert "Re-open the file your evidence cites." in prompts[0]
    assert "You are a headless worker" not in prompts[0], (
        "continuation must not repeat the preamble"
    )
    assert "Your artifact for this phase is at" in prompts[0]
    assert _ev_types(ctx).count("self_check_started") == 1


def _contract_producer() -> PhaseDef:
    return PhaseDef(
        id="plan",
        type="producer",
        persona="plan.md",
        models=("gemma",),
        artifact="plan.md",
        self_check=True,
        produces_contract="quill.plan/v1",
    )


def _valid_plan_payload() -> dict[str, object]:
    return {
        "summary": "Implement the requested behavior",
        "decisions": ["Keep compatibility"],
        "phases": ["Implement", "Verify"],
        "evidence": ["plan.md#decision"],
        "verification": ["Run focused and full tests"],
        "unknowns": [],
    }


def test_contract_producer_is_schema_blind_until_projection_and_publishes(
    tmp_path: Path,
) -> None:
    phase = _contract_producer()
    ctx = _ctx(tmp_path, _config(tmp_path, [phase]), _Spawn({"plan": "DONE: planned"}), _FakeLoader())
    ctx.phase_checkpoints["plan"] = "c" * 40
    continuations: list[tuple[str, float]] = []

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
        continuations.append((prompt, timeout))
        if match := _PROJECTION_RE.search(prompt):
            Path(match.group(1)).write_text(json.dumps(_valid_plan_payload()), encoding="utf-8")
        return "DONE: complete"

    ctx.deps.session_repair = repair
    result = engine._run_producer(ctx, phase)

    assert result.outcome is Outcome.DONE
    assert result.contract_ref is not None
    assert result.contract_ref == ctx.contracts["plan"]
    initial_prompt = ctx.deps.spawn.calls[0][2]  # type: ignore[attr-defined]
    assert "schema" not in initial_prompt.lower()
    assert "machine-readable" not in initial_prompt.lower()
    assert len(continuations) == 2
    assert "schema" not in continuations[0][0].lower()
    assert "machine-readable" not in continuations[0][0].lower()
    assert ".contract-staging" not in continuations[0][0]
    assert "final data projection" in continuations[1][0]
    assert all(timeout == ctx.config.opencode_run_seconds for _prompt, timeout in continuations)
    assert (ctx.run_dir / result.contract_ref.path).is_file()
    assert (ctx.run_dir / "work" / "plan" / "attempt-1.md").read_text() == "artifact body"
    contract = load_contract(
        ctx.run_dir / result.contract_ref.path,
        default_catalog(),
        run_dir=ctx.run_dir,
    )
    assert contract.checkpoint == "c" * 40


def test_mechanical_phase_publishes_exact_typed_command_evidence(tmp_path: Path) -> None:
    phase = PhaseDef(
        id="verify",
        type="mechanical",
        step="build_test",
        produces_contract="quill.verification/v1",
    )
    ctx = _ctx(tmp_path, _config(tmp_path, [phase]), _Spawn({}), _FakeLoader())
    ctx.deps.build_test = lambda _config, selection: VerificationResult(
        selection,
        (
            CommandResult(
                "make",
                0,
                False,
                False,
                "2026-08-05T01:00:00+00:00",
                "2026-08-05T01:00:01+00:00",
                "build ok\n",
            ),
            CommandResult(
                "make test",
                None,
                False,
                True,
                "2026-08-05T01:00:01+00:00",
                "2026-08-05T01:00:03+00:00",
                "test timed out\n",
            ),
        ),
    )

    result = engine._run_mechanical(ctx, phase)

    assert result.outcome is Outcome.BLOCK
    assert result.contract_ref is not None
    contract = load_contract(
        ctx.run_dir / result.contract_ref.path,
        default_catalog(),
        run_dir=ctx.run_dir,
    )
    commands = contract.payload["commands"]  # type: ignore[index]
    assert commands[0]["command"] == "make"
    assert commands[0]["exit_code"] == 0
    assert commands[1]["exit_code"] == -1
    assert commands[1]["timed_out"] is True
    for command in commands:
        log = ctx.run_dir / command["log"]
        assert log.is_file()
        assert len(command["log_sha256"]) == 64
    assert _ev_types(ctx).count("contract_validated") == 1
    assert _ev_types(ctx).count("contract_published") == 1


def test_unavailable_mechanical_phase_publishes_unavailable_not_pass(tmp_path: Path) -> None:
    phase = PhaseDef(
        id="verify",
        type="mechanical",
        step="build_test",
        produces_contract="quill.verification/v1",
    )
    ctx = _ctx(tmp_path, _config(tmp_path, [phase]), _Spawn({}), _FakeLoader())

    result = engine._run_mechanical(ctx, phase)

    assert result.outcome is Outcome.FAILED
    assert result.contract_ref is not None
    contract = load_contract(
        ctx.run_dir / result.contract_ref.path,
        default_catalog(),
        run_dir=ctx.run_dir,
    )
    assert contract.contract_status.value == "UNAVAILABLE"
    assert contract.payload == {"selection": "build_test", "commands": []}


def test_contract_self_check_without_continuation_support_fails_closed(tmp_path: Path) -> None:
    phase = _contract_producer()
    ctx = _ctx(tmp_path, _config(tmp_path, [phase]), _Spawn({"plan": "DONE: planned"}), _FakeLoader())

    result = engine._run_producer(ctx, phase)

    assert result.outcome is Outcome.GARBAGE
    assert "requires same-session continuation" in result.message
    assert not (ctx.run_dir / "contracts").exists()


def test_contract_self_check_repairs_only_a_missing_receipt_without_restarting_phase(
    tmp_path: Path,
) -> None:
    phase = _contract_producer()
    ctx = _ctx(tmp_path, _config(tmp_path, [phase]), _Spawn({"plan": "DONE: planned"}), _FakeLoader())
    prompts: list[str] = []

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
        prompts.append(prompt)
        if match := _PROJECTION_RE.search(prompt):
            Path(match.group(1)).write_text(json.dumps(_valid_plan_payload()), encoding="utf-8")
            return "DONE: projected contract"
        if "could not parse the terminal receipt" in prompt:
            return "DONE: self-check complete"
        return "Let me do one final verification pass on the artifact."

    ctx.deps.session_repair = repair
    final = engine.run_phases(ctx)

    assert final["type"] == "run_done"
    assert ctx.contracts["plan"].attempt == 1
    assert ctx.phase_call_counts == {"plan": 1}
    assert _ev_types(ctx).count("phase_started") == 1
    assert _ev_types(ctx).count("retry") == 0
    assert _events_of(ctx, "self_check_done")[0]["verdict"] == "DONE"
    assert len(prompts) == 3
    receipt_prompt = prompts[1]
    assert "Do not call tools" in receipt_prompt
    assert "repeat the self-check" in receipt_prompt
    assert "exactly one receipt line" in receipt_prompt


def test_contract_self_check_preserves_valid_work_when_receipt_repair_is_still_garbage(
    tmp_path: Path,
) -> None:
    phase = _contract_producer()
    ctx = _ctx(tmp_path, _config(tmp_path, [phase]), _Spawn({"plan": "DONE: planned"}), _FakeLoader())
    prompts: list[str] = []

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
        prompts.append(prompt)
        if match := _PROJECTION_RE.search(prompt):
            Path(match.group(1)).write_text(json.dumps(_valid_plan_payload()), encoding="utf-8")
            return "DONE: projected contract"
        return "Still checking."

    ctx.deps.session_repair = repair
    result = engine._run_producer(ctx, phase)

    assert result.outcome is Outcome.DONE
    assert result.contract_ref is not None
    assert result.contract_ref.attempt == 1
    assert ctx.phase_call_counts == {"plan": 1}
    assert _ev_types(ctx).count("retry") == 0
    assert _events_of(ctx, "self_check_done")[0]["verdict"] == "GARBAGE"
    assert len(prompts) == 3


def test_contract_self_check_does_not_preserve_work_if_receipt_repair_removes_artifact(
    tmp_path: Path,
) -> None:
    phase = _contract_producer()
    ctx = _ctx(tmp_path, _config(tmp_path, [phase]), _Spawn({"plan": "DONE: planned"}), _FakeLoader())
    calls = 0

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
        nonlocal calls
        calls += 1
        if calls == 2:
            ctx.artifact_path("plan.md").unlink()
            return "DONE: self-check complete"
        return "Still checking."

    ctx.deps.session_repair = repair
    result = engine._run_producer(ctx, phase)

    assert result.outcome is Outcome.GARBAGE
    assert "artifact 'plan.md' is missing" in result.message
    assert result.contract_ref is None
    assert calls == 2
    assert not (ctx.run_dir / "contracts").exists()


def test_projection_cannot_mutate_frozen_natural_artifact(tmp_path: Path) -> None:
    phase = _contract_producer()
    ctx = _ctx(tmp_path, _config(tmp_path, [phase]), _Spawn({"plan": "DONE: planned"}), _FakeLoader())

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
        if match := _PROJECTION_RE.search(prompt):
            Path(match.group(1)).write_text(json.dumps(_valid_plan_payload()), encoding="utf-8")
            (ctx.run_dir / "plan.md").write_text("unauthorized mutation", encoding="utf-8")
        return "DONE: complete"

    ctx.deps.session_repair = repair
    result = engine._run_producer(ctx, phase)

    assert result.outcome is Outcome.GARBAGE
    assert "modified its frozen source artifact" in result.message
    assert not (ctx.run_dir / "contracts").exists()


def test_failed_projection_continuation_still_rejects_repository_mutation(tmp_path: Path) -> None:
    phase = _contract_producer()
    ctx = _ctx(tmp_path, _config(tmp_path, [phase]), _Spawn({"plan": "DONE: planned"}), _FakeLoader())

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
        if _PROJECTION_RE.search(prompt):
            (ctx.run_dir / "plan.md").write_text("mutated before crash", encoding="utf-8")
            return "CRASH: projection failed"
        return "DONE: complete"

    ctx.deps.session_repair = repair
    result = engine._run_producer(ctx, phase)

    assert result.outcome is Outcome.GARBAGE
    assert "modified its frozen source artifact" in result.message
    assert not (ctx.run_dir / "contracts").exists()


def test_invalid_projection_gets_bounded_projection_only_repair(tmp_path: Path) -> None:
    phase = _contract_producer()
    ctx = _ctx(tmp_path, _config(tmp_path, [phase]), _Spawn({"plan": "DONE: planned"}), _FakeLoader())
    projection_prompts: list[str] = []

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
        match = _PROJECTION_RE.search(prompt)
        if match:
            projection_prompts.append(prompt)
            Path(match.group(1)).write_text("{broken", encoding="utf-8")
        elif "Projection repair" in prompt:
            projection_prompts.append(prompt)
            staging = ctx.run_dir / ".contract-staging" / "plan" / "attempt-1.json"
            staging.write_text(json.dumps(_valid_plan_payload()), encoding="utf-8")
        return "DONE: complete"

    ctx.deps.session_repair = repair
    result = engine._run_producer(ctx, phase)

    assert result.outcome is Outcome.DONE
    assert result.contract_ref is not None
    assert len(projection_prompts) == 2
    assert "Edit only that staging JSON" in projection_prompts[1]
    assert "do not research" in projection_prompts[1]


def test_incomplete_projection_retries_natural_work_without_revealing_schema(
    tmp_path: Path,
) -> None:
    phase = _contract_producer()
    config = replace(_config(tmp_path, [phase]), retries={"spawn": 1})
    spawn = _Spawn({"plan": ["DONE: first", "DONE: second"]})
    ctx = _ctx(tmp_path, config, spawn, _FakeLoader())
    projection_count = 0

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
        nonlocal projection_count
        if match := _PROJECTION_RE.search(prompt):
            projection_count += 1
            payload: object = (
                {
                    "contract_status": "INCOMPLETE",
                    "missing": [
                        {
                            "field": "verification",
                            "reason": "no verification was selected",
                            "evidence": "plan.md:1",
                        }
                    ],
                }
                if projection_count == 1
                else _valid_plan_payload()
            )
            Path(match.group(1)).write_text(json.dumps(payload), encoding="utf-8")
        return "DONE: complete"

    ctx.deps.session_repair = repair
    result = engine._run_phase(ctx, phase)

    assert result.outcome is Outcome.DONE
    assert result.contract_ref is not None
    assert len(spawn.calls) == 2
    assert "SEMANTIC CORRECTION REQUIRED" in spawn.calls[1][2]
    assert "verification" in spawn.calls[1][2]
    assert "schema" not in spawn.calls[1][2].lower()
    assert not (ctx.run_dir / "contracts" / "plan" / "attempt-1.json").exists()
    assert (ctx.run_dir / "contracts" / "plan" / "attempt-2.json").is_file()


def test_delivery_projection_exposes_only_semantics_and_binds_observed_identity(
    tmp_path: Path,
) -> None:
    phase = PhaseDef(
        id="commit",
        type="producer",
        persona="commit.md",
        models=("gemma",),
        artifact="delivery.md",
        self_check=True,
        produces_contract="quill.delivery/v1",
    )
    ctx = _ctx(tmp_path, _config(tmp_path, [phase]), _Spawn({"commit": "DONE: delivered"}), _FakeLoader())
    ctx.branch = "feature/ticket-33"

    class DeliveryGit:
        def pr_for_branch(self, branch: str) -> PullRequest | None:
            assert branch == "feature/ticket-33"
            return PullRequest(7, branch, "Ticket 33", "https://example.test/pr/7")

        def pr_for_ticket(self, ticket: int) -> PullRequest | None:
            raise AssertionError(f"branch lookup should resolve before ticket {ticket}")

        def local_head_sha(self) -> str:
            return "abc123"

        def pr_head_sha(self, pr_number: int) -> str:
            assert pr_number == 7
            return "abc123"

        def workspace_status(self) -> str:
            return ""

    ctx.deps.git = DeliveryGit()  # type: ignore[assignment]
    projections: list[str] = []

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
        if match := _PROJECTION_RE.search(prompt):
            projections.append(prompt)
            Path(match.group(1)).write_text(
                json.dumps({"summary": "Delivered ticket 33", "unresolved": []}),
                encoding="utf-8",
            )
        return "DONE: complete"

    ctx.deps.session_repair = repair
    result = engine._run_producer(ctx, phase)

    assert result.outcome is Outcome.DONE
    assert result.contract_ref is not None
    assert len(projections) == 1
    assert "local_sha" not in projections[0]
    assert "remote_sha" not in projections[0]
    contract = load_contract(
        ctx.run_dir / result.contract_ref.path,
        default_catalog(),
        run_dir=ctx.run_dir,
    )
    assert contract.payload == {
        "summary": "Delivered ticket 33",
        "unresolved": [],
        "branch": "feature/ticket-33",
        "local_sha": "abc123",
        "remote_sha": "abc123",
        "pr": 7,
        "pr_url": "https://example.test/pr/7",
        "clean": True,
    }


@pytest.mark.parametrize(
    ("remote_sha", "workspace", "pr_branch", "message"),
    [
        ("def456", "", "feature/ticket-33", "identity mismatch"),
        ("abc123", " M changed.py", "feature/ticket-33", "worktree dirty"),
        ("abc123", "", "feature/different", "branch mismatch"),
    ],
)
def test_delivery_projection_rejects_unverified_identity(
    tmp_path: Path,
    remote_sha: str,
    workspace: str,
    pr_branch: str,
    message: str,
) -> None:
    phase = PhaseDef(
        id="commit",
        type="producer",
        persona="commit.md",
        models=("gemma",),
        artifact="delivery.md",
        self_check=True,
        produces_contract="quill.delivery/v1",
    )
    ctx = _ctx(tmp_path, _config(tmp_path, [phase]), _Spawn({"commit": "DONE: delivered"}), _FakeLoader())
    ctx.branch = "feature/ticket-33"

    class DeliveryGit:
        def pr_for_branch(self, branch: str) -> PullRequest | None:
            return PullRequest(7, pr_branch, "Ticket 33", "https://example.test/pr/7")

        def pr_for_ticket(self, ticket: int) -> PullRequest | None:
            return None

        def local_head_sha(self) -> str:
            return "abc123"

        def pr_head_sha(self, pr_number: int) -> str:
            return remote_sha

        def workspace_status(self) -> str:
            return workspace

    ctx.deps.git = DeliveryGit()  # type: ignore[assignment]

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
        if match := _PROJECTION_RE.search(prompt):
            Path(match.group(1)).write_text(
                json.dumps({"summary": "Delivered ticket 33", "unresolved": []}),
                encoding="utf-8",
            )
        return "DONE: complete"

    ctx.deps.session_repair = repair
    result = engine._run_producer(ctx, phase)

    assert result.outcome is Outcome.GARBAGE
    assert message in result.message
    assert not (ctx.run_dir / "contracts" / "commit").exists()
