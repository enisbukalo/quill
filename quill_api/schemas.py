"""Pydantic request/response models for the HTTP surface.

Typed models rather than bare dicts: FastAPI validates requests and filters responses from these,
so a malformed body is a 422 with a field-level message instead of a hand-written check, and a
response can never leak a field the model does not declare.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: A GitHub ``owner/name``. Validated again in the workspace layer before it reaches a path or an
#: argv entry — this is the friendly 422, that is the guarantee.
RepoName = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", examples=["me/proj"])]
BranchName = Annotated[str, Field(min_length=1, max_length=255, examples=["ticket-42-fix"])]
Reason = Annotated[
    str,
    Field(
        min_length=3,
        max_length=200,
        description="Why this change is being made. Becomes the commit message.",
    ),
]


class StartRunRequest(BaseModel):
    """Start a run using the ``quillfolio.toml`` committed in the target repository."""

    model_config = ConfigDict(extra="forbid")

    repo: RepoName
    branch: BranchName
    ticket: Annotated[int, Field(gt=0)]
    mode: Literal["create", "update", "review"] = "create"
    workflow: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]+$")] = "ticket"
    generated_branch: bool = False
    clear_prefix_cache: bool = False
    model_overrides: dict[
        Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]+$")],
        Annotated[str, Field(min_length=1, max_length=200)],
    ] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def workflow_mode_agrees(self) -> StartRunRequest:
        expected = {"ticket": "create", "pr_update": "update", "pr_review": "review"}.get(
            self.workflow
        )
        if expected is not None and self.mode != expected:
            raise ValueError(f"workflow '{self.workflow}' requires mode '{expected}'")
        return self


class GitHubRepository(BaseModel):
    name: RepoName
    visibility: str
    updated_at: str
    default_branch: str = ""
    config_sha: str = ""
    project_board: str | None = None


class GitHubRepositoryList(BaseModel):
    login: str
    repositories: list[GitHubRepository]
    scanned_at: float | None = None
    error: str | None = None


class GitHubIssue(BaseModel):
    number: int
    title: str
    labels: list[str] = []


class GitHubIssueList(BaseModel):
    repo: RepoName
    issues: list[GitHubIssue]
    work_types: list[str]


class ProjectQueueCandidate(BaseModel):
    number: int
    title: str
    labels: list[str] = Field(default_factory=list)
    status: str = ""
    selectable: bool = False
    reason: str | None = None


class ProjectQueueCandidateGroup(BaseModel):
    epic_number: int | None = None
    epic_title: str
    tickets: list[ProjectQueueCandidate] = Field(default_factory=list)


class ProjectQueueCandidates(BaseModel):
    repo: RepoName
    project_board: str
    groups: list[ProjectQueueCandidateGroup] = Field(default_factory=list)


class AddProjectQueueBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tickets: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def tickets_are_unique(self) -> AddProjectQueueBatchRequest:
        if len(self.tickets) != len(set(self.tickets)):
            raise ValueError("tickets must be unique")
        return self


class ProjectQueueAddResult(BaseModel):
    ticket: int
    queued: bool
    reason: str | None = None


class ProjectQueueBatchResult(BaseModel):
    batch_id: str | None = None
    results: list[ProjectQueueAddResult] = Field(default_factory=list)


class ProjectQueueItemInfo(BaseModel):
    ticket: int
    title: str
    epic_number: int | None = None
    epic_title: str | None = None
    position: int
    state: str
    board_status: str = ""
    run_id: str | None = None
    pr_number: int | None = None
    error: str | None = None


class ProjectQueueBatchInfo(BaseModel):
    batch_id: str
    position: int
    repo: RepoName
    state: str
    submitted_at: float
    error: str | None = None
    items: list[ProjectQueueItemInfo] = Field(default_factory=list)


class ProjectQueueView(BaseModel):
    batches: list[ProjectQueueBatchInfo] = Field(default_factory=list)
    depth: int = 0


class WorkflowPhaseChoice(BaseModel):
    id: str
    label: str
    model: str
    parallel_group: str | None = None


class WorkflowChoice(BaseModel):
    id: str
    label: str
    mode: str
    phases: list[WorkflowPhaseChoice] = []


class WorkflowChoiceList(BaseModel):
    repo: RepoName
    default: str
    workflows: list[WorkflowChoice]
    models: list[str] = []
    excluded_issue_labels: list[str] = []


class UpdateTarget(BaseModel):
    available: bool
    reason: str | None = None
    pr_number: int | None = None
    title: str = ""
    branch: str = ""
    url: str = ""
    head_sha: str = ""
    committed_at: str = ""
    feedback_count: int = 0
    local_branch: bool = False


class WorkspaceInfo(BaseModel):
    """One server-local persistent checkout. The on-disk path is deliberately omitted — it is
    server-internal and must never reach a client."""

    repo: RepoName
    branch: str


class WorkspaceList(BaseModel):
    workspaces: list[WorkspaceInfo]


class WorkspaceBranchInfo(BaseModel):
    """A selectable branch and where it exists, so the UI can mark current/local/remote state."""

    name: BranchName
    current: bool
    local: bool
    remote: bool


class WorkspaceBranchList(BaseModel):
    repo: RepoName
    current: str | None = None
    branches: list[WorkspaceBranchInfo]


class WorkspaceMutationResult(BaseModel):
    """The outcome of a pull or delete: the branch now checked out and an operator-facing message."""

    repo: RepoName
    branch: str
    message: str


class DecisionRequest(BaseModel):
    answer: Annotated[str, Field(min_length=1)]


class RestartRunRequest(BaseModel):
    """Start a new run from one durable phase boundary of a terminal run."""

    model_config = ConfigDict(extra="forbid")

    phase: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]+$")]
    sequence: Annotated[int, Field(gt=0)] | None = None
    model_overrides: dict[
        Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]+$")],
        Annotated[str, Field(min_length=1, max_length=200)],
    ] = Field(default_factory=dict, max_length=100)


class RestartPhaseChoice(BaseModel):
    id: str
    label: str
    sequence: int
    call_number: int
    start_phase: str
    verdict: str | None = None
    model: str | None = None


class RestartOptions(BaseModel):
    eligible: bool
    reason: str | None = None
    phases: list[RestartPhaseChoice] = Field(default_factory=list)


class PhaseGraphNode(BaseModel):
    id: str
    label: str
    type: str
    order: int
    column: int | None = None
    lane: int = 0
    group: str | None = None
    self_check: bool = False
    self_fix: bool = False


class PhaseGraphEdge(BaseModel):
    key: str
    source: str
    target: str
    kinds: list[Literal["normal", "retry"]]


class PhaseGraph(BaseModel):
    nodes: list[PhaseGraphNode] = []
    edges: list[PhaseGraphEdge] = []


class ModelLoadInfo(BaseModel):
    load_id: str
    phase: str
    label: str
    model: str
    started_at: float
    duration_s: float | None = None
    status: Literal["active", "completed", "failed"]
    reason: str | None = None


class RunSummary(BaseModel):
    run_id: str
    status: str
    repo: str
    branch: str | None = None
    ticket: int
    mode: str
    workflow: str = "ticket"
    pr_number: int | None = None
    pr_head_sha: str | None = None
    feedback_digest: str | None = None
    source_run_id: str | None = None
    start_phase: str | None = None
    clear_prefix_cache: bool = False
    phase: str | None = None
    phase_label: str | None = None
    phase_started_at: float | None = None
    active_phases: dict[str, float] = Field(default_factory=dict)
    self_checks: dict[str, str] = Field(default_factory=dict)
    self_fixes: dict[str, str] = Field(default_factory=dict)
    activity: str | None = None
    activity_label: str | None = None
    attempt: int = 0
    max_attempts: int = 0
    pr_url: str | None = None
    question: str | None = None
    error: str | None = None
    failure_code: str | None = None
    failure_label: str | None = None
    queued_at: float
    started_at: float | None = None
    updated_at: float
    live_usage: dict[str, object] = Field(default_factory=dict)
    phase_graph: PhaseGraph | None = None
    phase_route_counts: dict[str, int] = Field(default_factory=dict)
    phase_durations: dict[str, float] = Field(default_factory=dict)
    model_loads: list[ModelLoadInfo] = Field(default_factory=list)
    #: How many runs are ahead of this one; 0 once it is executing.
    queue_position: int | None = None


class PhaseHistoryEntry(BaseModel):
    phase: str
    label: str
    verdict: str | None = None
    attempt: int
    ts: float
    phase_type: str | None = None
    model: str | None = None
    duration_s: float | None = None
    tools: dict[str, int] = {}
    reason: str | None = None


class RunDetail(RunSummary):
    history: list[PhaseHistoryEntry] = []


class RunList(BaseModel):
    runs: list[RunSummary]
    limit: int = 200
    offset: int = 0
    has_more: bool = False


class ModelLifetimeStats(BaseModel):
    model: str
    calls: int = 0
    duration_s: float = 0.0
    context_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0


class PhaseLifetimeStats(BaseModel):
    phase: str
    label: str
    executions: int = 0
    duration_s: float = 0.0
    total_tokens: int = 0
    tool_calls: int = 0


class RunLifetimePoint(BaseModel):
    run_id: str
    status: str
    started_at: float = 0.0
    duration_s: float = 0.0
    total_tokens: int = 0


class FailureLifetimeStats(BaseModel):
    code: str
    label: str
    runs: int = 0


class LifetimeStats(BaseModel):
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    halted_runs: int = 0
    other_runs: int = 0
    repositories: int = 0
    tickets: int = 0
    duration_s: float = 0.0
    context_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    phase_executions: int = 0
    tool_calls: int = 0
    self_checks: int = 0
    repeat_attempts: int = 0
    model_loads: int = 0
    model_load_duration_s: float = 0.0
    models: list[ModelLifetimeStats] = Field(default_factory=list)
    phases: list[PhaseLifetimeStats] = Field(default_factory=list)
    recent_runs: list[RunLifetimePoint] = Field(default_factory=list)
    failures: list[FailureLifetimeStats] = Field(default_factory=list)


class DeleteRunsRequest(BaseModel):
    run_ids: list[str] = Field(min_length=1, max_length=200)


class DeleteRunsResult(BaseModel):
    deleted: list[str]


class MemoryEntry(BaseModel):
    memory_id: str
    repo: str
    finding: str
    phases: list[str] = Field(default_factory=list)
    occurrences: int = 0
    last_verified_at: str = ""
    changed_files: list[str] = Field(default_factory=list)


class MemoryList(BaseModel):
    memories: list[MemoryEntry] = Field(default_factory=list)
    archived_events: int = 0


class DeleteMemoriesRequest(BaseModel):
    memory_ids: list[str] = Field(default_factory=list, max_length=1000)
    delete_all: bool = False


class DeleteMemoriesResult(BaseModel):
    deleted: list[str] = Field(default_factory=list)


class QueueView(BaseModel):
    active: RunSummary | None = None
    queued: list[RunSummary] = []
    depth: int = 0


class ArtifactInfo(BaseModel):
    name: str
    size: int


class ArtifactList(BaseModel):
    run_id: str
    artifacts: list[ArtifactInfo]


class ArtifactContent(BaseModel):
    run_id: str
    name: str
    content: str


class CatalogEntryInfo(BaseModel):
    name: str
    description: str = ""
    #: Personas only: which phase type this one is written for.
    suits: str | None = None


class CatalogList(BaseModel):
    root: str
    entries: list[CatalogEntryInfo]


class PersonaDetail(BaseModel):
    name: str
    description: str = ""
    suits: str | None = None
    body: str


class SkillDetail(BaseModel):
    name: str
    description: str = ""
    body: str
    #: Auxiliary files beside SKILL.md, relative to the skill directory.
    files: list[str] = []


class WriteResult(BaseModel):
    """What happened to a catalog write, including whether it reached the remote.

    ``pushed`` is reported rather than assumed: a commit that could not be pushed is still safely
    in local history, so the request succeeds and says so instead of failing over a network blip.
    """

    name: str
    path: str
    committed: bool
    pushed: bool
    sha: str | None = None
    error: str | None = None


class PersonaWrite(BaseModel):
    body: Annotated[str, Field(min_length=1)]
    reason: Reason


class PersonaCreate(PersonaWrite):
    name: Annotated[str, Field(min_length=1, max_length=100)]


class SkillWrite(BaseModel):
    body: Annotated[str, Field(min_length=1)]
    reason: Reason
    #: Optional auxiliary files, keyed by path relative to the skill directory.
    files: dict[str, str] = {}


class SkillCreate(SkillWrite):
    name: Annotated[str, Field(min_length=1, max_length=100)]


class FileWrite(BaseModel):
    content: str
    reason: Reason


class DeleteRequest(BaseModel):
    reason: Reason


class ConfigSchema(BaseModel):
    phase_types: list[str]
    mechanical_steps: list[str]
    required: list[str]
    defaults: dict[str, object]


class InitPayload(BaseModel):
    """Everything an agent needs to write a repo's quillfolio.toml without guessing."""

    config_filename: str
    config_schema: ConfigSchema
    personas: list[CatalogEntryInfo]
    skills: list[CatalogEntryInfo]
    models: list[str]
    endpoints: dict[str, str]
    starter_config: str


class HealthInfo(BaseModel):
    status: str
    uptime_s: float
    driver_version: str
    gh_available: bool
    gh_authenticated: bool
    queue_depth: int


class VersionInfo(BaseModel):
    quill: str
    api: str


class LoadedModelInfo(BaseModel):
    id: str
    max_model_len: int | None = None
    root: str | None = None


class SwitchableModelInfo(BaseModel):
    """A vLLM model this machine can make resident, discovered from its systemd unit."""

    model_id: str
    service: str
    unit_state: str
    available: bool
    #: Why this entry cannot be started. Present so an undiscoverable or unpermitted model is
    #: visible with a cause rather than silently absent from the list.
    unavailable_reason: str | None = None
    resident: bool = False
    #: vLLM limits read from the launch script. ``max_concurrency`` is ``--max-num-seqs`` — how many
    #: sequences decode at once, so 1 means one chat at a time. ``max_model_len`` is the context
    #: ceiling for a single conversation. Null when the script does not set the flag.
    max_model_len: int | None = None
    max_concurrency: int | None = None
    max_batched_tokens: int | None = None
    tensor_parallel_size: int | None = None
    quantization: str | None = None
    kv_cache_dtype: str | None = None
    gpu_memory_utilization: float | None = None


class ModelSwitchStateInfo(BaseModel):
    """Progress of an interactive model load or unload operation."""

    status: str = "idle"
    model_id: str | None = None
    service: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    forced: bool = False


class ModelSwitchRequest(BaseModel):
    model_id: str
    #: Switch even though runs are active. Starting a service stops the resident model, so this
    #: will break every phase mid-flight; the caller must ask for it explicitly.
    force: bool = False


class ModelsInfo(BaseModel):
    backend: str
    loaded: list[str] = []
    model_details: list[LoadedModelInfo] = []
    switchable: list[SwitchableModelInfo] = []
    switch: ModelSwitchStateInfo = ModelSwitchStateInfo()
    reachable: bool = False
    url: str | None = None


class CpuTelemetryInfo(BaseModel):
    utilization_percent: float | None = None
    temperature_c: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    name: str | None = None
    fan_percent: float | None = None


class GpuTelemetryInfo(BaseModel):
    index: int
    name: str
    utilization_percent: float | None = None
    temperature_c: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    sampled_at: float | None = None
    fan_percent: float | None = None


class VllmThroughputTelemetryInfo(BaseModel):
    processing_tokens_per_second: float | None = None
    generation_tokens_per_second: float | None = None
    processing_samples: int = 0
    generation_samples: int = 0
    loaded_models: list[str] = Field(default_factory=list)


class SystemTelemetryInfo(BaseModel):
    sampled_at: float | None = None
    platform: str = "linux"
    cpu: CpuTelemetryInfo = CpuTelemetryInfo()
    gpus: list[GpuTelemetryInfo] = []
    vllm: VllmThroughputTelemetryInfo = VllmThroughputTelemetryInfo()
    #: Repeated on every sample rather than pushed as an event — see ModelSwitchTelemetry.
    model_switch: ModelSwitchStateInfo = ModelSwitchStateInfo()


class TelemetryDisplaySettings(BaseModel):
    cpu_temperature_min_c: float = Field(default=20.0, ge=-20.0, le=150.0)
    cpu_temperature_max_c: float = Field(default=70.0, ge=-20.0, le=150.0)
    gpu_temperature_min_c: float = Field(default=20.0, ge=-20.0, le=150.0)
    gpu_temperature_max_c: float = Field(default=80.0, ge=-20.0, le=150.0)

    @model_validator(mode="after")
    def validate_ranges(self) -> TelemetryDisplaySettings:
        if self.cpu_temperature_min_c >= self.cpu_temperature_max_c:
            raise ValueError("CPU minimum temperature must be below its maximum")
        if self.gpu_temperature_min_c >= self.gpu_temperature_max_c:
            raise ValueError("GPU minimum temperature must be below its maximum")
        return self


class ErrorResponse(BaseModel):
    error: str
    detail: str
