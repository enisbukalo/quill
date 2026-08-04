"""Discover the vLLM model services this machine can switch to, and drive a switch.

There is no model list anywhere on disk to read: models are launch scripts, and the services that
run them are systemd units. The registry derives the list instead, in three steps that need no
hand-maintained mapping and no knowledge of where the scripts live:

1. ``systemctl list-unit-files --type=service`` for candidate names. **Not** a glob passed to
   ``systemctl show`` — that only matches units systemd already holds in memory, so a model never
   referenced since boot is silently absent (observed: 4 of 6 services, both ``gemma31`` units
   missing). ``list-unit-files`` reads the filesystem, and naming a unit explicitly makes systemd
   load it on demand. Template units (``name@.service``) are filtered first because ``systemctl
   show`` rejects them and aborts the whole batch, yielding nothing rather than a partial list.
2. ``systemctl show -p Id -p ExecStart`` for each name, batched into one call, to get the launch
   script path from systemd rather than from a hardcoded directory.
3. ``--served-model-name`` out of that script. That flag is exactly what vLLM advertises at
   ``/v1/models`` and exactly the key ``[runner.vllm.models]`` uses, so the derived ID is directly
   comparable to both. A unit whose script has no such flag is not a model service and is dropped —
   which is what excludes a unit whose ``ExecStart`` is a plain interpreter.

The full probe costs seconds on a few hundred units, so it is cached and refreshed explicitly rather
than run per request.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from typing import Protocol, Self

from quill.loader import ModelLoadError

#: Systemd unit-file states that mean the unit exists and can be started.
_STARTABLE_STATES = frozenset(
    {
        "enabled",
        "enabled-runtime",
        "linked",
        "linked-runtime",
        "static",
        "disabled",
        "indirect",
        "generated",
        "transient",
    }
)
_SERVED_MODEL_NAME = re.compile(r"--served-model-name[=\s]+([^\s\\'\"]+)")
#: Shell comments are stripped before any flag is read. The launch scripts discuss their own tuning
#: in prose — one contains "--max-model-len 250000:" and "--max-num-batched-tokens is" inside
#: comments — so a naive scan reports commentary as configuration.
_COMMENT = re.compile(r"(?m)(?<=\s)#.*$|^#.*$")
_EXEC_PATH = re.compile(r"path=([^ ;]+)")
_SUDO_UNIT = re.compile(r"systemctl\s+start\s+(\S+?)(?:\.service)?\s*(?:,|$)")
#: A launch script big enough to be something other than a vLLM invocation is not worth scanning.
_MAX_SCRIPT_BYTES = 512_000
_DISCOVERY_TIMEOUT = 60.0
DEFAULT_REGISTRY_TTL_S = 900.0
DEFAULT_SWITCH_TIMEOUT_S = 600.0

type Runner = Callable[[Sequence[str]], str]


def _flag(text: str, flag: str) -> str | None:
    """The raw value of a vLLM flag in an already comment-stripped script."""
    match = re.search(rf"--{flag}[=\s]+([^\s\\'\"]+)", text)
    return match.group(1) if match else None


def _int_flag(text: str, flag: str) -> int | None:
    raw = _flag(text, flag)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        # A value that is a shell variable rather than a literal is simply unknown; it is not a
        # reason to discard the model.
        return None


def _float_flag(text: str, flag: str) -> float | None:
    raw = _flag(text, flag)
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class ModelSpecs:
    """vLLM limits read from a launch script."""

    max_model_len: int | None = None
    max_concurrency: int | None = None
    max_batched_tokens: int | None = None
    tensor_parallel_size: int | None = None
    quantization: str | None = None
    kv_cache_dtype: str | None = None
    gpu_memory_utilization: float | None = None


@dataclass(frozen=True, slots=True)
class SwitchableModel:
    """One vLLM model this machine can make resident."""

    model_id: str
    service: str
    unit_state: str
    available: bool
    unavailable_reason: str | None = None
    resident: bool = False
    #: Read from the launch script's vLLM flags. `max_concurrency` is --max-num-seqs: how many
    #: sequences the server will decode at once, so 1 means one chat at a time. `max_model_len` is
    #: the context ceiling for a single conversation. Absent when a script does not set the flag.
    max_model_len: int | None = None
    max_concurrency: int | None = None
    max_batched_tokens: int | None = None
    tensor_parallel_size: int | None = None
    quantization: str | None = None
    kv_cache_dtype: str | None = None
    gpu_memory_utilization: float | None = None


def _run(argv: Sequence[str]) -> str:
    """Run a read-only systemd/sudo query and return stdout, tolerating a non-zero exit."""
    result = subprocess.run(
        list(argv), capture_output=True, text=True, timeout=_DISCOVERY_TIMEOUT, check=False
    )
    return result.stdout


class ServiceModelRegistry:
    """Cached discovery of switchable vLLM model services (see module docstring)."""

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        read_text: Callable[[str], str] | None = None,
        ttl_s: float = DEFAULT_REGISTRY_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._run = runner or _run
        self._read_text = read_text or _read_script
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[SwitchableModel, ...] = ()
        self._cached_at: float | None = None

    def models(self, *, refresh: bool = False) -> tuple[SwitchableModel, ...]:
        """Discovered services, from cache unless it is stale or ``refresh`` is set."""
        with self._lock:
            fresh = self._cached_at is not None and (self._clock() - self._cached_at) < self._ttl_s
            if fresh and not refresh:
                return self._cached
            self._cached = tuple(self._discover())
            self._cached_at = self._clock()
            return self._cached

    def resolve(self, model_id: str) -> SwitchableModel | None:
        """The discovered entry for ``model_id``, or None when this machine cannot serve it."""
        return next((m for m in self.models() if m.model_id == model_id), None)

    # -- discovery --------------------------------------------------------------

    def _discover(self) -> list[SwitchableModel]:
        units = self._unit_states()
        if not units:
            return []
        allowlist = self._sudo_allowlist()
        found: list[SwitchableModel] = []
        for unit, script in self._exec_paths(list(units)).items():
            model_id, specs = self._parse_script(script)
            if model_id is None:
                continue
            state = units.get(unit, "unknown")
            found.append(
                _with_availability(
                    SwitchableModel(
                        model_id=model_id,
                        service=unit,
                        unit_state=state,
                        available=False,
                        max_model_len=specs.max_model_len,
                        max_concurrency=specs.max_concurrency,
                        max_batched_tokens=specs.max_batched_tokens,
                        tensor_parallel_size=specs.tensor_parallel_size,
                        quantization=specs.quantization,
                        kv_cache_dtype=specs.kv_cache_dtype,
                        gpu_memory_utilization=specs.gpu_memory_utilization,
                    ),
                    allowlist=allowlist,
                )
            )
        return sorted(found, key=lambda entry: entry.model_id)

    def _unit_states(self) -> dict[str, str]:
        """Unit name → unit-file state, from the filesystem, with template units removed."""
        states: dict[str, str] = {}
        for line in self._run(
            ["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"]
        ).splitlines():
            parts = line.split()
            if len(parts) < 2 or "@." in parts[0]:
                continue
            states[parts[0]] = parts[1]
        return states

    def _exec_paths(self, units: list[str]) -> dict[str, str]:
        """Unit name → ExecStart script path, from one batched ``systemctl show``."""
        if not units:
            return {}
        paths: dict[str, str] = {}
        unit = None
        for line in self._run(
            ["systemctl", "show", *units, "-p", "Id", "-p", "ExecStart", "--no-pager"]
        ).splitlines():
            line = line.strip()
            if line.startswith("Id="):
                unit = line[3:]
            elif line.startswith("ExecStart=") and unit:
                match = _EXEC_PATH.search(line)
                if match:
                    paths[unit] = match.group(1)
        return paths

    def _parse_script(self, script: str) -> tuple[str | None, ModelSpecs]:
        """The model ID a launch script serves, plus the vLLM limits it configures.

        Returns ``(None, ModelSpecs())`` when the script declares no ``--served-model-name`` — that
        is what marks a unit as something other than a vLLM model service.
        """
        try:
            text = _COMMENT.sub("", self._read_text(script))
        except OSError:
            return None, ModelSpecs()
        match = _SERVED_MODEL_NAME.search(text)
        if match is None:
            return None, ModelSpecs()
        return match.group(1), ModelSpecs(
            max_model_len=_int_flag(text, "max-model-len"),
            # --max-num-seqs is how many sequences decode at once, so 1 means one chat at a time.
            max_concurrency=_int_flag(text, "max-num-seqs"),
            max_batched_tokens=_int_flag(text, "max-num-batched-tokens"),
            tensor_parallel_size=_int_flag(text, "tensor-parallel-size"),
            quantization=_flag(text, "quantization"),
            kv_cache_dtype=_flag(text, "kv-cache-dtype"),
            gpu_memory_utilization=_float_flag(text, "gpu-memory-utilization"),
        )

    def _sudo_allowlist(self) -> frozenset[str] | None:
        """Services startable without a password, or None when the allowlist can't be determined.

        None and an empty set mean different things — "we could not tell" versus "nothing is
        permitted" — and the caller must not block every switch on the former.
        """
        try:
            output = self._run(["sudo", "-n", "-l"])
        except (OSError, subprocess.SubprocessError):
            return None
        if "NOPASSWD" not in output:
            return None
        return frozenset(_SUDO_UNIT.findall(output))


def _read_script(path: str) -> str:
    target = Path(path)
    if target.stat().st_size > _MAX_SCRIPT_BYTES:
        raise OSError(f"{path} is too large to scan")
    return target.read_text(encoding="utf-8", errors="ignore")


def _with_availability(
    entry: SwitchableModel, *, allowlist: frozenset[str] | None
) -> SwitchableModel:
    """Decide whether an entry can actually be started, and say why when it cannot."""
    if entry.unit_state not in _STARTABLE_STATES:
        return replace(entry, available=False, unavailable_reason=f"unit is {entry.unit_state}")
    if allowlist is not None and entry.service.removesuffix(".service") not in allowlist:
        return replace(
            entry, available=False, unavailable_reason="not permitted without a password"
        )
    return replace(entry, available=True, unavailable_reason=None)


# -- switching ------------------------------------------------------------------


class SwitchableServer(Protocol):
    """The slice of :class:`~quill.modelserver.VllmServer` a switch actually needs.

    Narrower than the concrete class so a test can substitute a recorder without starting anything.
    """

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc: object) -> None: ...

    def switch_to(
        self, preset: str, service: str, timeout: float, *, command: tuple[str, ...] = ()
    ) -> None: ...

    def unload_service(
        self,
        preset: str,
        service: str,
        timeout: float,
        *,
        command: tuple[str, ...],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SwitchState:
    """What the last (or current) interactive switch is doing."""

    status: str = "idle"  # idle | switching | ready | unloading | unloaded | failed
    model_id: str | None = None
    service: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    forced: bool = False


class SwitchInProgress(RuntimeError):
    """A switch was requested while one was already running."""


@dataclass
class _Task:
    thread: threading.Thread | None = None


class ModelSwitcher:
    """Starts or stops a model service in the background and tracks it to completion.

    Each unit declares ``Conflicts=`` against its siblings, so starting the target stops whatever
    was resident. Explicit unload uses the associated unit's stop command. A target which fails to
    boot can leave the GPU with nothing loaded, so failures are reported loudly rather than as a
    no-op.
    """

    def __init__(
        self,
        *,
        server_factory: Callable[[], SwitchableServer],
        command: tuple[str, ...],
        stop_command: tuple[str, ...],
        timeout_s: float = DEFAULT_SWITCH_TIMEOUT_S,
        on_change: Callable[[SwitchState], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._server_factory = server_factory
        self._command = command
        self._stop_command = stop_command
        self._timeout_s = timeout_s
        self._on_change = on_change
        self._clock = clock
        self._lock = threading.Lock()
        self._state = SwitchState()
        self._task = _Task()

    @property
    def state(self) -> SwitchState:
        with self._lock:
            return self._state

    def start(self, model: SwitchableModel, *, forced: bool = False) -> SwitchState:
        """Begin a switch to ``model``. Raises :class:`SwitchInProgress` if one is already running."""
        with self._lock:
            if self._state.status in {"switching", "unloading"}:
                raise SwitchInProgress(
                    f"a model operation for '{self._state.model_id}' is already in progress"
                )
            self._state = SwitchState(
                status="switching",
                model_id=model.model_id,
                service=model.service,
                started_at=self._clock(),
                forced=forced,
            )
            state = self._state
        self._publish(state)
        thread = threading.Thread(
            target=self._run_switch, args=(model,), name="quill-model-switch", daemon=True
        )
        self._task.thread = thread
        thread.start()
        return state

    def unload(self, model: SwitchableModel, *, forced: bool = False) -> SwitchState:
        """Begin unloading ``model`` without blocking the request thread."""
        with self._lock:
            if self._state.status in {"switching", "unloading"}:
                raise SwitchInProgress(
                    f"a model operation for '{self._state.model_id}' is already in progress"
                )
            self._state = SwitchState(
                status="unloading",
                model_id=model.model_id,
                service=model.service,
                started_at=self._clock(),
                forced=forced,
            )
            state = self._state
        self._publish(state)
        thread = threading.Thread(
            target=self._run_unload, args=(model,), name="quill-model-unload", daemon=True
        )
        self._task.thread = thread
        thread.start()
        return state

    def _run_switch(self, model: SwitchableModel) -> None:
        error: str | None = None
        try:
            with self._server_factory() as server:
                # systemd's own Id carries the `.service` suffix, but the passwordless sudoers rules
                # are written against the bare unit name. `sudo systemctl start qwen35-nvfp4.service`
                # therefore misses NOPASSWD, falls through to a rule that wants a password, and dies
                # with "a terminal is required to authenticate". systemctl treats both spellings the
                # same, so send the one sudo can match.
                unit = model.service.removesuffix(".service")
                server.switch_to(model.model_id, unit, self._timeout_s, command=self._command)
        except ModelLoadError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a background thread must never die silently
            error = f"unexpected failure switching to '{model.model_id}': {exc}"
        with self._lock:
            self._state = replace(
                self._state,
                status="failed" if error else "ready",
                finished_at=self._clock(),
                error=error,
            )
            state = self._state
        self._publish(state)

    def _run_unload(self, model: SwitchableModel) -> None:
        error: str | None = None
        try:
            with self._server_factory() as server:
                unit = model.service.removesuffix(".service")
                server.unload_service(
                    model.model_id, unit, self._timeout_s, command=self._stop_command
                )
        except ModelLoadError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a background thread must never die silently
            error = f"unexpected failure unloading '{model.model_id}': {exc}"
        with self._lock:
            self._state = replace(
                self._state,
                status="failed" if error else "unloaded",
                finished_at=self._clock(),
                error=error,
            )
            state = self._state
        self._publish(state)

    def _publish(self, state: SwitchState) -> None:
        if self._on_change is not None:
            self._on_change(state)
