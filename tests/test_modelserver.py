"""Unit tests for VllmServer (the always-on vllm backend).

The server is faked with ``httpx.MockTransport``: a stateful handler mimicking ``GET /health`` and
``POST /reset_prefix_cache`` so we can assert the health-gate, the cache reset, and the typed-error
paths (unhealthy, reset-non-2xx, 404 dev-mode hint, network error) without a live server.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from quill.loader import ModelLoadError
from quill.modelserver import VllmServer


class FakeVllm:
    """In-memory vllm: serves /health + /reset_prefix_cache and records reset calls."""

    def __init__(
        self, *, health: int = 200, reset: int = 200, models: list[str] | None = None
    ) -> None:
        self.health_status = health
        self.reset_status = reset
        self.reset_calls = 0
        self.health_calls = 0
        self.models = models or []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/health":
            self.health_calls += 1
            return httpx.Response(self.health_status)
        if request.method == "GET" and path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": model} for model in self.models]})
        if request.method == "POST" and path == "/reset_prefix_cache":
            self.reset_calls += 1
            return httpx.Response(self.reset_status)
        return httpx.Response(404)


def make_server(vllm: FakeVllm, *, clear_prefix_cache: bool = False) -> VllmServer:
    client = httpx.Client(transport=httpx.MockTransport(vllm.handler))
    return VllmServer(
        url="http://vllm.example:8000", client=client, clear_prefix_cache=clear_prefix_cache
    )


def test_load_health_gates_then_resets() -> None:
    vllm = FakeVllm(models=["Qwen3.6_27B_FP8"])
    server = make_server(vllm, clear_prefix_cache=True)
    server.load("Qwen3.6_27B_FP8", 0.0)
    assert vllm.health_calls == 1
    assert vllm.reset_calls == 1


def test_needs_load_uses_advertised_model_identity() -> None:
    server = make_server(FakeVllm(models=["Qwen3.6_27B_FP8"]))

    assert server.needs_load("Qwen3.6_27B_FP8") is False
    assert server.needs_load("Gemma4_31B_FP8") is True


def test_default_load_clears_once_then_preserves_cache_across_phases() -> None:
    vllm = FakeVllm(models=["Qwen3.6_27B_FP8"])
    server = make_server(vllm)

    server.load("Qwen3.6_27B_FP8", 0.0)
    server.load("Qwen3.6_27B_FP8", 0.0)

    assert vllm.reset_calls == 1


def test_legacy_cold_phase_option_does_not_clear_between_phases() -> None:
    vllm = FakeVllm(models=["Qwen3.6_27B_FP8"])
    server = make_server(vllm, clear_prefix_cache=True)

    server.load("Qwen3.6_27B_FP8", 0.0)
    server.load("Qwen3.6_27B_FP8", 0.0)

    assert vllm.reset_calls == 1


def test_load_without_preset_only_health_checks() -> None:
    vllm = FakeVllm()
    server = make_server(vllm, clear_prefix_cache=True)
    server.load()  # no preset, no timeout
    assert vllm.reset_calls == 1


def test_unhealthy_raises_and_skips_reset() -> None:
    vllm = FakeVllm(health=503)
    server = make_server(vllm, clear_prefix_cache=True)
    with pytest.raises(ModelLoadError, match="not healthy"):
        server.load()
    assert vllm.reset_calls == 0  # gated out before the reset


def test_wrong_model_without_association_fails_exactly() -> None:
    server = make_server(FakeVllm(models=["Gemma4_31B_FP8"]))
    with pytest.raises(ModelLoadError, match="has no runner.vllm.models service association"):
        server.load("Qwen3_32B_FP8", 360)


def test_wrong_model_starts_service_and_polls_until_exact_id() -> None:
    vllm = FakeVllm(models=["Qwen3_32B_FP8"])
    commands: list[tuple[str, ...]] = []
    now = 0.0

    def command(args: Sequence[str]) -> None:
        commands.append(tuple(args))

    def monotonic() -> float:
        return now

    def sleep(_seconds: float) -> None:
        nonlocal now
        now += 2
        vllm.models = ["Gemma4_31B_NVFP4"]

    client = httpx.Client(transport=httpx.MockTransport(vllm.handler))
    server = VllmServer(
        "http://vllm.example:8000",
        client,
        command=("sudo", "systemctl", "start"),
        models={"Gemma4_31B_NVFP4": "gemma431-nvfp4"},
        command_runner=command,
        monotonic=monotonic,
        sleep=sleep,
    )

    server.load("Gemma4_31B_NVFP4", 360)

    assert commands == [("sudo", "systemctl", "start", "gemma431-nvfp4")]
    assert vllm.reset_calls == 1


def test_model_switch_times_out_after_deadline() -> None:
    vllm = FakeVllm(models=["Qwen3_32B_FP8"])
    now = 0.0

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    client = httpx.Client(transport=httpx.MockTransport(vllm.handler))
    server = VllmServer(
        "http://vllm.example:8000",
        client,
        command=("systemctl", "start"),
        models={"Gemma4_31B_NVFP4": "gemma431-nvfp4"},
        command_runner=lambda _args: None,
        monotonic=lambda: now,
        sleep=sleep,
    )

    with pytest.raises(ModelLoadError, match="timed out after 6s"):
        server.load("Gemma4_31B_NVFP4", 6)


def test_unload_service_stops_unit_and_waits_for_model_to_disappear() -> None:
    vllm = FakeVllm(models=["Qwen3.6_27B_NVFP4"])
    commands: list[tuple[str, ...]] = []

    def command(args: Sequence[str]) -> None:
        commands.append(tuple(args))
        vllm.models = []

    client = httpx.Client(transport=httpx.MockTransport(vllm.handler))
    server = VllmServer("http://vllm.example:8000", client, command_runner=command)

    server.unload_service(
        "Qwen3.6_27B_NVFP4",
        "qwen27-nvfp4",
        10,
        command=("sudo", "systemctl", "stop"),
    )

    assert commands == [("sudo", "systemctl", "stop", "qwen27-nvfp4")]


def test_unload_service_times_out_if_model_remains_advertised() -> None:
    vllm = FakeVllm(models=["Qwen3.6_27B_NVFP4"])
    now = 0.0

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    client = httpx.Client(transport=httpx.MockTransport(vllm.handler))
    server = VllmServer(
        "http://vllm.example:8000",
        client,
        command_runner=lambda _args: None,
        monotonic=lambda: now,
        sleep=sleep,
    )

    with pytest.raises(ModelLoadError, match="waiting for vllm model.*to unload"):
        server.unload_service(
            "Qwen3.6_27B_NVFP4",
            "qwen27-nvfp4",
            6,
            command=("sudo", "systemctl", "stop"),
        )


def test_reset_non_2xx_raises() -> None:
    vllm = FakeVllm(reset=500)
    server = make_server(vllm, clear_prefix_cache=True)
    with pytest.raises(ModelLoadError, match="500"):
        server.load()


def test_reset_404_hints_dev_mode() -> None:
    vllm = FakeVllm(reset=404)
    server = make_server(vllm, clear_prefix_cache=True)
    with pytest.raises(ModelLoadError, match="VLLM_SERVER_DEV_MODE"):
        server.load()


def test_health_network_error_raises() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    server = VllmServer(url="http://vllm.example:8000", client=client)
    with pytest.raises(ModelLoadError, match="health check"):
        server.load()


def test_unload_all_is_noop() -> None:
    vllm = FakeVllm()
    server = make_server(vllm)
    server.unload_all()  # must not touch the server or raise
    assert vllm.reset_calls == 0
    assert vllm.health_calls == 0


def test_default_load_establishes_run_prefix_cache_boundary() -> None:
    vllm = FakeVllm()
    server = make_server(vllm)
    server.load()
    assert vllm.health_calls == 1
    assert vllm.reset_calls == 1


def test_url_trailing_slash_stripped() -> None:
    server = VllmServer(url="http://vllm.example:8000/")
    assert server.url == "http://vllm.example:8000"


def test_healthy_probe_true_and_false() -> None:
    assert make_server(FakeVllm(health=200)).healthy() is True
    assert make_server(FakeVllm(health=503)).healthy() is False


def test_make_model_server_dispatches_on_backend() -> None:
    from quill.config import QuillfolioConfig
    from quill.loader import ModelLoader
    from quill.modelserver import make_model_server

    def cfg(backend: str, url: str = "") -> QuillfolioConfig:
        return QuillfolioConfig(
            directory=Path("."),
            repo="me/r",
            pr_base="main",
            runner="pi",
            build_command="b",
            test_command="t",
            log_dir="logs",
            phases=[],
            backend=backend,
            vllm_url=url,
        )

    config = cfg("vllm", "http://vllm.example:8000")
    config.vllm_command = ("systemctl", "start")
    config.vllm_models = {"Gemma": "gemma.service"}
    default = make_model_server(config)
    legacy_cold = make_model_server(
        cfg("vllm", "http://vllm.example:8000"), clear_prefix_cache=True
    )
    assert isinstance(default, VllmServer)
    assert default.command == ("systemctl", "start")
    assert default.models == {"Gemma": "gemma.service"}
    assert isinstance(legacy_cold, VllmServer)
    assert isinstance(make_model_server(cfg("llamacpp")), ModelLoader)
