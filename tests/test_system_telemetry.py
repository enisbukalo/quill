from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from quill_api.settings import Settings
from quill_api.telemetry import (
    CpuTelemetry,
    GpuTelemetry,
    LinuxTelemetryReader,
    PerfCpuPowerSampler,
    SystemTelemetryMonitor,
    SystemTelemetrySnapshot,
    VllmThroughputSampler,
    _vllm_counters,
    combined_power_draw_w,
)


def test_telemetry_settings_default_and_interval_bound(tmp_path) -> None:
    settings = Settings.from_env(
        {"QUILL_STATE_DIR": str(tmp_path), "QUILL_VLLM_URL": "http://vllm.example:8000"}
    )
    assert settings.telemetry_interval_s == 0.125
    assert settings.pr_watch_enabled
    assert settings.pr_watch_interval_s == 15.0
    assert settings.vllm_stop_command == ("sudo", "systemctl", "stop")
    bounded = Settings.from_env(
        {
            "QUILL_STATE_DIR": str(tmp_path),
            "QUILL_VLLM_URL": "http://vllm.example:8000",
            "QUILL_TELEMETRY_INTERVAL_SECONDS": "0.01",
            "QUILL_PR_WATCH_ENABLED": "false",
            "QUILL_PR_WATCH_INTERVAL_SECONDS": "1",
            "QUILL_VLLM_STOP_COMMAND": "machine-control stop",
        }
    )
    assert bounded.telemetry_interval_s == 0.125
    assert not bounded.pr_watch_enabled
    assert bounded.pr_watch_interval_s == 5.0
    assert bounded.vllm_stop_command == ("machine-control", "stop")


def test_settings_require_vllm_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="QUILL_VLLM_URL must be set"):
        Settings.from_env({"QUILL_STATE_DIR": str(tmp_path)})


def test_pr_feedback_loop_settings_are_bounded(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "QUILL_STATE_DIR": str(tmp_path),
            "QUILL_VLLM_URL": "http://vllm.example:8000",
            "QUILL_PR_FEEDBACK_LOOP_ENABLED": "off",
            "QUILL_PR_FEEDBACK_LOOP_MAX_CYCLES": "999",
        }
    )

    assert not settings.pr_feedback_loop_enabled
    assert settings.pr_feedback_loop_max_cycles == 20


def test_monitor_subscription_coalesces_to_latest_value() -> None:
    async def exercise() -> None:
        monitor = SystemTelemetryMonitor(
            LinuxTelemetryReader(VllmThroughputSampler("http://vllm.example:8000")), 0.125
        )
        stream = monitor.subscribe()
        initial = await anext(stream)
        assert initial.sampled_at is None
        first = replace(initial, sampled_at=1.0, cpu=CpuTelemetry(10, 40))
        latest = replace(initial, sampled_at=2.0, cpu=CpuTelemetry(90, 41))
        monitor._publish(first)
        monitor._publish(latest)
        assert (await anext(stream)).sampled_at == 2.0

    asyncio.run(exercise())


def test_snapshot_serializes_tuple_gpus_as_json_compatible_data() -> None:
    snapshot = SystemTelemetrySnapshot(
        sampled_at=1.0,
        cpu=CpuTelemetry(12.5, 43.0, 8192.0, 32768.0),
    )
    assert snapshot.as_dict()["cpu"] == {
        "utilization_percent": 12.5,
        "temperature_c": 43.0,
        "memory_used_mb": 8192.0,
        "memory_total_mb": 32768.0,
        "name": None,
        "fan_percent": None,
        "power_draw_w": None,
    }


def test_combined_power_requires_cpu_and_every_reported_gpu() -> None:
    complete = SystemTelemetrySnapshot(
        cpu=CpuTelemetry(power_draw_w=100.0),
        gpus=(
            GpuTelemetry(0, "GPU 0", power_draw_w=60.0),
            GpuTelemetry(1, "GPU 1", power_draw_w=65.0),
        ),
    )
    assert combined_power_draw_w(complete) == 225.0
    assert combined_power_draw_w(replace(complete, cpu=CpuTelemetry())) is None
    assert combined_power_draw_w(replace(complete, gpus=())) is None


def test_reader_reports_configured_cpu_pwm_as_percent(tmp_path: Path) -> None:
    controller = tmp_path / "hwmon7"
    controller.mkdir()
    (controller / "name").write_text("nct6779\n", encoding="utf-8")
    (controller / "pwm3").write_text("128\n", encoding="utf-8")
    reader = LinuxTelemetryReader(
        VllmThroughputSampler("http://vllm.example:8000"),
        cpu_fan_hwmon_name="nct6779",
        cpu_fan_pwm_channel=3,
        hwmon_root=tmp_path,
    )

    assert reader._cpu_fan_percent() == 50.2


def test_cpu_fan_settings_are_optional_and_bounded(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "QUILL_STATE_DIR": str(tmp_path),
            "QUILL_VLLM_URL": "http://vllm.example:8000",
            "QUILL_CPU_FAN_HWMON_NAME": "nct6779",
            "QUILL_CPU_FAN_PWM_CHANNEL": "99",
        }
    )

    assert settings.cpu_fan_hwmon_name == "nct6779"
    assert settings.cpu_fan_pwm_channel == 32


def test_gpu_fallback_reports_nvidia_smi_fan_percent(monkeypatch) -> None:
    command_seen: list[str] = []

    def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        command_seen.extend(command)
        return subprocess.CompletedProcess(
            command, 0, "0, Example GPU, 25, 45, 1024, 8192, 37, 62.5, 180\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    reader = LinuxTelemetryReader(VllmThroughputSampler("http://vllm.example:8000"))

    gpu = reader._gpus(10.0)[0]
    assert gpu.fan_percent == 37.0
    assert gpu.power_draw_w == 62.5
    assert gpu.power_limit_w == 180.0
    assert "fan.speed" in command_seen[1]


def test_unsupported_nvml_fan_does_not_hide_other_gpu_metrics() -> None:
    class NvmlWithoutFan:
        NVML_TEMPERATURE_GPU = 0

        @staticmethod
        def nvmlDeviceGetCount() -> int:
            return 1

        @staticmethod
        def nvmlDeviceGetHandleByIndex(index: int) -> int:
            return index

        @staticmethod
        def nvmlDeviceGetMemoryInfo(_handle: int) -> object:
            return type("Memory", (), {"used": 1024**2, "total": 2 * 1024**2})()

        @staticmethod
        def nvmlDeviceGetName(_handle: int) -> str:
            return "Passively Cooled GPU"

        @staticmethod
        def nvmlDeviceGetUtilizationRates(_handle: int) -> object:
            return type("Utilization", (), {"gpu": 12})()

        @staticmethod
        def nvmlDeviceGetTemperature(_handle: int, _sensor: int) -> int:
            return 40

        @staticmethod
        def nvmlDeviceGetFanSpeed(_handle: int) -> float:
            raise RuntimeError("not supported")

        @staticmethod
        def nvmlDeviceGetPowerUsage(_handle: int) -> int:
            return 62500

        @staticmethod
        def nvmlDeviceGetEnforcedPowerLimit(_handle: int) -> int:
            return 180000

    reader = LinuxTelemetryReader(VllmThroughputSampler("http://vllm.example:8000"))
    reader._nvml = NvmlWithoutFan()

    gpu = reader._gpus(10.0)[0]
    assert gpu.utilization_percent == 12.0
    assert gpu.fan_percent is None
    assert gpu.power_draw_w == 62.5
    assert gpu.power_limit_w == 180.0


def test_perf_cpu_power_sampler_derives_watts_from_interval_energy() -> None:
    now = 10.0
    sampler = PerfCpuPowerSampler(interval_s=0.25, monotonic=lambda: now)

    sampler._record_perf_line("0.250,15.625,Joules,power/energy-pkg/,\n")
    assert sampler.power_draw_w() == 62.5
    sampler._record_perf_line("diagnostic text\n")
    assert sampler.power_draw_w() == 62.5

    now = 11.1
    assert sampler.power_draw_w() is None


def test_reader_reports_cpu_model_name(monkeypatch) -> None:
    original = Path.read_text

    def fake_read_text(path: Path, *args, **kwargs) -> str:
        if path == Path("/proc/cpuinfo"):
            return "processor: 0\nmodel name: Example 16-Core CPU\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    reader = LinuxTelemetryReader(VllmThroughputSampler("http://vllm.example:8000"))
    assert reader._cpu_name == "Example 16-Core CPU"


def test_vllm_counter_parser_aggregates_engine_series() -> None:
    metrics = """
vllm:prompt_tokens_total{engine="0",model_name="model-a"} 100
vllm:prompt_tokens_total{engine="1",model_name="model-a"} 50
vllm:prompt_tokens_by_source_total{engine="0",model_name="model-a",source="local_compute"} 60
vllm:prompt_tokens_by_source_total{engine="1",model_name="model-a",source="local_compute"} 30
vllm:generation_tokens_total{engine="0",model_name="model-a"} 20
vllm:generation_tokens_total{engine="1",model_name="model-a"} 5
"""
    assert _vllm_counters(metrics) == (90.0, 25.0, ("model-a",))


def test_vllm_sampler_keeps_100_positive_work_rates_and_ignores_idle() -> None:
    now = 0.0
    prompt = 100.0
    generation = 10.0

    def clock() -> float:
        return now

    def fetch() -> str:
        return (
            f'vllm:prompt_tokens_total{{model_name="model-a"}} {prompt}\n'
            f'vllm:generation_tokens_total{{model_name="model-a"}} {generation}\n'
        )

    sampler = VllmThroughputSampler(
        "http://vllm.example:8000", fetch=fetch, monotonic=clock, interval_s=0.25
    )
    sampler.sample()
    for index in range(105):
        now += 0.25
        prompt += index + 1
        generation += 2
        sampled = sampler.sample()
    assert sampled.processing_samples == 100
    assert sampled.generation_samples == 100
    assert sampled.loaded_models == ("model-a",)
    previous = sampled
    now += 0.25
    assert sampler.sample() == previous


def test_vllm_sampler_resets_window_when_model_or_counter_changes() -> None:
    samples = iter(
        [
            'vllm:prompt_tokens_total{model_name="a"} 10\nvllm:generation_tokens_total{model_name="a"} 2',
            'vllm:prompt_tokens_total{model_name="a"} 20\nvllm:generation_tokens_total{model_name="a"} 4',
            'vllm:prompt_tokens_total{model_name="b"} 1\nvllm:generation_tokens_total{model_name="b"} 0',
        ]
    )
    now = 0.0

    def clock() -> float:
        return now

    sampler = VllmThroughputSampler(
        "http://vllm.example:8000", fetch=lambda: next(samples), monotonic=clock
    )
    sampler.sample()
    now = 0.25
    assert sampler.sample().processing_samples == 1
    now = 0.5
    reset = sampler.sample()
    assert reset.processing_samples == 0
    assert reset.loaded_models == ("b",)


def test_vllm_sampler_clears_stale_rates_when_metrics_become_unreachable() -> None:
    now = 0.0
    prompt = 10.0
    generation = 2.0
    reachable = True

    def clock() -> float:
        return now

    def fetch() -> str:
        if not reachable:
            raise OSError("vLLM is unloaded")
        return (
            f'vllm:prompt_tokens_total{{model_name="model-a"}} {prompt}\n'
            f'vllm:generation_tokens_total{{model_name="model-a"}} {generation}\n'
        )

    sampler = VllmThroughputSampler("http://vllm.example:8000", fetch=fetch, monotonic=clock)
    sampler.sample()
    now = 0.25
    prompt = 20.0
    generation = 4.0
    active = sampler.sample()
    assert active.loaded_models == ("model-a",)
    assert active.processing_samples == 1
    assert active.generation_samples == 1

    now = 0.5
    reachable = False
    cleared = sampler.sample()
    assert cleared.loaded_models == ()
    assert cleared.processing_tokens_per_second is None
    assert cleared.generation_tokens_per_second is None
    assert cleared.processing_samples == 0
    assert cleared.generation_samples == 0

    now = 0.75
    reachable = True
    reloaded = sampler.sample()
    assert reloaded.loaded_models == ("model-a",)
    assert reloaded.processing_samples == 0
    assert reloaded.generation_samples == 0


def test_vllm_sampler_clears_stale_rates_when_metrics_have_no_token_counters() -> None:
    samples = iter(
        [
            'vllm:prompt_tokens_total{model_name="a"} 10\n'
            'vllm:generation_tokens_total{model_name="a"} 2',
            'vllm:prompt_tokens_total{model_name="a"} 20\n'
            'vllm:generation_tokens_total{model_name="a"} 4',
            "# vLLM is running without a loaded model",
        ]
    )
    now = 0.0

    sampler = VllmThroughputSampler(
        "http://vllm.example:8000", fetch=lambda: next(samples), monotonic=lambda: now
    )
    sampler.sample()
    now = 0.25
    assert sampler.sample().processing_samples == 1
    now = 0.5
    cleared = sampler.sample()
    assert cleared.loaded_models == ()
    assert cleared.processing_tokens_per_second is None
    assert cleared.generation_tokens_per_second is None
    assert cleared.processing_samples == 0
    assert cleared.generation_samples == 0


def test_git_author_defaults_are_generic(tmp_path: Path) -> None:
    """The packaged default must not name any real person or account."""
    settings = Settings.from_env(
        {"QUILL_STATE_DIR": str(tmp_path), "QUILL_VLLM_URL": "http://vllm.example:8000"}
    )
    assert settings.git_author_name == "quill"
    assert settings.git_author_email == "quill@users.noreply.github.com"


def test_git_author_comes_from_the_environment(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "QUILL_STATE_DIR": str(tmp_path),
            "QUILL_VLLM_URL": "http://vllm.example:8000",
            "QUILL_GIT_AUTHOR_NAME": "agent-bot",
            "QUILL_GIT_AUTHOR_EMAIL": "1+agent-bot@users.noreply.github.com",
        }
    )
    assert settings.git_author_name == "agent-bot"
    assert settings.git_author_email == "1+agent-bot@users.noreply.github.com"


def test_blank_git_author_falls_back_to_the_default(tmp_path: Path) -> None:
    """An empty value in an env file must not produce a commit with no author."""
    settings = Settings.from_env(
        {
            "QUILL_STATE_DIR": str(tmp_path),
            "QUILL_VLLM_URL": "http://vllm.example:8000",
            "QUILL_GIT_AUTHOR_NAME": "   ",
            "QUILL_GIT_AUTHOR_EMAIL": "",
        }
    )
    assert settings.git_author_name == "quill"
    assert settings.git_author_email == "quill@users.noreply.github.com"
