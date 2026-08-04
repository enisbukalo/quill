"""Discovery of switchable vLLM model services, and the switch state machine.

Every test drives fake ``systemctl`` output. Nothing here may start, stop, or probe a real model
service — the discovery probe is read-only by construction, and a switch is not exercised live.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

import pytest

from quill.loader import ModelLoadError
from quill_api.model_registry import (
    ModelSwitcher,
    ServiceModelRegistry,
    SwitchableModel,
    SwitchInProgress,
)

# Two model services and three decoys: a template unit, a unit whose ExecStart is an interpreter
# with no --served-model-name, and a unit with no ExecStart at all.
_UNIT_FILES = """\
apparmor.service                enabled  enabled
apport-coredump-hook@.service   static   -
qwen27-nvfp4.service            linked   enabled
qwen35-nvfp4.service            linked   enabled
gemma31.service                 disabled enabled
qwencord.service                enabled  enabled
masked-model.service            masked   -
"""

_EXEC_START = """\
Id=apparmor.service
ExecStart={ path=/lib/apparmor/apparmor.systemd ; argv[]=/lib/apparmor/apparmor.systemd reload ; }
Id=qwen27-nvfp4.service
ExecStart={ path=/srv/serve-qwen27-nvfp4.sh ; argv[]=/srv/serve-qwen27-nvfp4.sh ; }
Id=qwen35-nvfp4.service
ExecStart={ path=/srv/serve-qwen35-nvfp4.sh ; argv[]=/srv/serve-qwen35-nvfp4.sh ; }
Id=gemma31.service
ExecStart={ path=/srv/serve-gemma31.sh ; argv[]=/srv/serve-gemma31.sh ; }
Id=qwencord.service
ExecStart={ path=/srv/qwencord/.venv/bin/python ; argv[]=/srv/qwencord/.venv/bin/python -m bot ; }
Id=masked-model.service
ExecStart={ path=/srv/serve-masked.sh ; argv[]=/srv/serve-masked.sh ; }
"""

_SUDO = """\
User quill-test may run the following commands on box:
    (ALL) NOPASSWD: /usr/bin/systemctl start qwen27-nvfp4, /usr/bin/systemctl stop qwen27-nvfp4, \
/usr/bin/systemctl start gemma31, /usr/bin/systemctl stop gemma31, \
/usr/bin/systemctl start masked-model, /usr/bin/systemctl stop masked-model
"""

_SCRIPTS = {
    "/srv/serve-qwen27-nvfp4.sh": "vllm serve /models/q27 \\\n  --served-model-name Qwen3.6_27B_NVFP4 \\\n",
    # Mirrors the real gemma script, which discusses its own tuning in comments: a naive scan
    # reads "--max-model-len 999999" and "--max-num-seqs is" out of the prose.
    "/srv/serve-qwen35-nvfp4.sh": (
        "# Tried --max-model-len 999999 but it OOMed; --max-num-seqs is tricky here.\n"
        "vllm serve /models/q35 \\\n"
        "  --served-model-name Qwen3.6_35B_A3B_NVFP4 \\\n"
        "  --max-model-len 200000 --max-num-seqs 4 --max-num-batched-tokens 8192 \\\n"
        "  --tensor-parallel-size 4 --quantization modelopt --kv-cache-dtype fp8\n"
    ),
    "/srv/serve-gemma31.sh": "vllm serve /models/g31 --served-model-name Gemma4_31B_FP8\n",
    "/srv/qwencord/.venv/bin/python": "\x7fELF binary-ish, no flags here\n",
    "/srv/serve-masked.sh": "vllm serve /models/m --served-model-name Masked_Model\n",
}


def _registry(
    *, sudo: str = _SUDO, scripts: dict[str, str] | None = None
) -> tuple[ServiceModelRegistry, list[list[str]]]:
    calls: list[list[str]] = []
    files = _SCRIPTS if scripts is None else scripts

    def runner(argv: Sequence[str]) -> str:
        calls.append(list(argv))
        if "list-unit-files" in argv:
            return _UNIT_FILES
        if argv[:2] == ["systemctl", "show"]:
            assert not any("@." in arg for arg in argv), (
                "template units must be filtered before `systemctl show` — it rejects them and "
                "aborts the whole batch, which yields zero results rather than a partial list"
            )
            return _EXEC_START
        if argv[:2] == ["sudo", "-n"]:
            return sudo
        raise AssertionError(f"unexpected command {argv}")

    def read_text(path: str) -> str:
        if path not in files:
            raise OSError(f"missing {path}")
        return files[path]

    return ServiceModelRegistry(runner=runner, read_text=read_text), calls


def test_discovers_model_services_and_drops_everything_else() -> None:
    models = _registry()[0].models()
    assert [entry.model_id for entry in models] == [
        "Gemma4_31B_FP8",
        "Masked_Model",
        "Qwen3.6_27B_NVFP4",
        "Qwen3.6_35B_A3B_NVFP4",
    ]
    services = {entry.service for entry in models}
    # apparmor has an ExecStart but no --served-model-name; qwencord's is an interpreter.
    assert "apparmor.service" not in services
    assert "qwencord.service" not in services


def test_template_units_are_filtered_before_systemctl_show() -> None:
    """A template unit makes `systemctl show` fail for the whole batch, not skip that entry."""
    registry, calls = _registry()
    registry.models()
    shown = [call for call in calls if call[:2] == ["systemctl", "show"]]
    assert shown, "expected a batched systemctl show"
    assert all("@." not in arg for call in shown for arg in call)


def test_unit_state_that_cannot_start_is_reported_with_a_reason() -> None:
    masked = next(e for e in _registry()[0].models() if e.model_id == "Masked_Model")
    assert masked.available is False
    assert masked.unavailable_reason == "unit is masked"


def test_model_outside_the_sudo_allowlist_is_unavailable_not_hidden() -> None:
    qwen35 = next(e for e in _registry()[0].models() if e.model_id == "Qwen3.6_35B_A3B_NVFP4")
    assert qwen35.available is False
    assert qwen35.unavailable_reason == "not permitted without a password"
    # ...while one that is on the list stays available.
    assert next(e for e in _registry()[0].models() if e.model_id == "Qwen3.6_27B_NVFP4").available


def test_unreadable_allowlist_does_not_block_every_switch() -> None:
    """ "Could not tell" must not be treated as "nothing is permitted"."""
    models = _registry(sudo="sudo: a password is required")[0].models()
    startable = [e for e in models if e.unit_state != "masked"]
    assert startable and all(entry.available for entry in startable)


def test_unreadable_script_degrades_one_entry_not_the_list() -> None:
    scripts = dict(_SCRIPTS)
    del scripts["/srv/serve-gemma31.sh"]
    models = _registry(scripts=scripts)[0].models()
    assert "Gemma4_31B_FP8" not in {entry.model_id for entry in models}
    assert "Qwen3.6_27B_NVFP4" in {entry.model_id for entry in models}


def test_discovery_is_cached_until_refresh_is_requested() -> None:
    registry, calls = _registry()
    registry.models()
    first = len(calls)
    registry.models()
    assert len(calls) == first, "second read must come from cache"
    registry.models(refresh=True)
    assert len(calls) > first


def test_resolve_returns_none_for_a_model_this_machine_cannot_serve() -> None:
    assert _registry()[0].resolve("Nonexistent_Model") is None
    assert _registry()[0].resolve("Qwen3.6_27B_NVFP4") is not None


# -- switching ------------------------------------------------------------------


class _FakeServer:
    """Stands in for VllmServer. Records control calls without touching a real service."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.unload_calls: list[tuple[str, str, tuple[str, ...]]] = []

    def __enter__(self) -> _FakeServer:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def switch_to(
        self, preset: str, service: str, timeout: float, *, command: tuple[str, ...] = ()
    ) -> None:
        self.calls.append((preset, service, command))
        if self.error is not None:
            raise self.error

    def unload_service(
        self,
        preset: str,
        service: str,
        timeout: float,
        *,
        command: tuple[str, ...],
    ) -> None:
        self.unload_calls.append((preset, service, command))
        if self.error is not None:
            raise self.error


_MODEL = SwitchableModel(
    model_id="Qwen3.6_35B_A3B_NVFP4",
    service="qwen35-nvfp4.service",
    unit_state="linked",
    available=True,
)


def _switcher(server: _FakeServer) -> tuple[ModelSwitcher, list[str]]:
    seen: list[str] = []
    switcher = ModelSwitcher(
        server_factory=lambda: server,
        command=("sudo", "systemctl", "start"),
        stop_command=("sudo", "systemctl", "stop"),
        on_change=lambda state: seen.append(state.status),
    )
    return switcher, seen


def _settle(switcher: ModelSwitcher) -> None:
    for _ in range(200):
        if switcher.state.status not in {"switching", "unloading"}:
            return
        threading.Event().wait(0.01)
    raise AssertionError("switch never settled")


def test_switch_starts_the_service_and_reports_ready() -> None:
    server = _FakeServer()
    switcher, seen = _switcher(server)
    switcher.start(_MODEL)
    _settle(switcher)

    # Start only: each unit declares Conflicts= against its siblings, so systemd stops the incumbent.
    # The unit is sent without its `.service` suffix — the passwordless sudoers rules are written
    # that way, and the suffixed spelling falls through to a rule that demands a password.
    assert server.calls == [
        ("Qwen3.6_35B_A3B_NVFP4", "qwen35-nvfp4", ("sudo", "systemctl", "start"))
    ]
    assert switcher.state.status == "ready"
    assert switcher.state.error is None
    assert seen == ["switching", "ready"]


def test_unload_stops_the_service_and_reports_unloaded() -> None:
    server = _FakeServer()
    switcher, seen = _switcher(server)
    switcher.unload(_MODEL)
    _settle(switcher)

    assert server.unload_calls == [
        ("Qwen3.6_35B_A3B_NVFP4", "qwen35-nvfp4", ("sudo", "systemctl", "stop"))
    ]
    assert switcher.state.status == "unloaded"
    assert switcher.state.error is None
    assert seen == ["unloading", "unloaded"]


def test_failed_unload_is_reported_loudly() -> None:
    server = _FakeServer(error=ModelLoadError("systemctl stop failed"))
    switcher, seen = _switcher(server)
    switcher.unload(_MODEL)
    _settle(switcher)

    assert switcher.state.status == "failed"
    assert "systemctl stop failed" in (switcher.state.error or "")
    assert seen == ["unloading", "failed"]


def test_failed_switch_is_reported_loudly() -> None:
    """Conflicts= stops the incumbent first, so a target that fails to boot leaves nothing loaded."""
    server = _FakeServer(error=ModelLoadError("timed out after 600s waiting for vllm model"))
    switcher, seen = _switcher(server)
    switcher.start(_MODEL)
    _settle(switcher)

    assert switcher.state.status == "failed"
    assert "timed out" in (switcher.state.error or "")
    assert seen == ["switching", "failed"]


def test_unexpected_failure_does_not_kill_the_worker_silently() -> None:
    server = _FakeServer(error=RuntimeError("nvml exploded"))
    switcher, _ = _switcher(server)
    switcher.start(_MODEL)
    _settle(switcher)
    assert switcher.state.status == "failed"
    assert "nvml exploded" in (switcher.state.error or "")


def test_second_switch_while_one_is_running_is_rejected() -> None:
    release = threading.Event()

    class _Blocking(_FakeServer):
        def switch_to(self, preset: str, service: str, timeout: float, **_: object) -> None:
            release.wait(2)

    switcher, _ = _switcher(_Blocking())
    switcher.start(_MODEL)
    try:
        with pytest.raises(SwitchInProgress):
            switcher.start(_MODEL)
    finally:
        release.set()
    _settle(switcher)


def test_launch_script_flags_become_model_specs() -> None:
    entry = next(e for e in _registry()[0].models() if e.model_id == "Qwen3.6_35B_A3B_NVFP4")
    assert entry.max_model_len == 200_000
    assert entry.max_concurrency == 4
    assert entry.max_batched_tokens == 8192
    assert entry.tensor_parallel_size == 4
    assert entry.quantization == "modelopt"
    assert entry.kv_cache_dtype == "fp8"


def test_flags_discussed_in_comments_are_not_read_as_configuration() -> None:
    """The real gemma script argues with itself in prose; only the invocation counts."""
    entry = next(e for e in _registry()[0].models() if e.model_id == "Qwen3.6_35B_A3B_NVFP4")
    assert entry.max_model_len == 200_000, "picked a value out of a comment"
    assert entry.max_concurrency == 4, "picked a value out of a comment"


def test_a_script_without_limits_still_yields_a_usable_entry() -> None:
    scripts = dict(_SCRIPTS)
    scripts["/srv/serve-gemma31.sh"] = "vllm serve /m --served-model-name Gemma4_31B_FP8\n"
    entry = next(
        e for e in _registry(scripts=scripts)[0].models() if e.model_id == "Gemma4_31B_FP8"
    )
    assert entry.available is True
    assert entry.max_model_len is None and entry.max_concurrency is None


def test_a_failed_start_reports_the_command_stderr() -> None:
    """`returned non-zero exit status 1` says nothing; sudo's own message says everything."""
    from quill.modelserver import _run_command

    with pytest.raises(ModelLoadError) as caught:
        _run_command(["sh", "-c", "echo 'A terminal is required to authenticate' >&2; exit 1"])
    assert "A terminal is required to authenticate" in str(caught.value)
