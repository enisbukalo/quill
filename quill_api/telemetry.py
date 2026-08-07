"""Low-overhead Linux host telemetry with latest-value async fan-out."""

from __future__ import annotations

import asyncio
import csv
import math
import subprocess
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx


_VLLM_METRICS_INTERVAL_SECONDS = 0.25
_VLLM_RATE_WINDOW = 100


@dataclass(frozen=True, slots=True)
class CpuTelemetry:
    utilization_percent: float | None = None
    temperature_c: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    name: str | None = None
    fan_percent: float | None = None
    power_draw_w: float | None = None


@dataclass(frozen=True, slots=True)
class GpuTelemetry:
    index: int
    name: str
    utilization_percent: float | None = None
    temperature_c: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    sampled_at: float | None = None
    fan_percent: float | None = None
    power_draw_w: float | None = None
    power_limit_w: float | None = None


@dataclass(frozen=True, slots=True)
class VllmThroughputTelemetry:
    processing_tokens_per_second: float | None = None
    generation_tokens_per_second: float | None = None
    processing_samples: int = 0
    generation_samples: int = 0
    loaded_models: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelSwitchTelemetry:
    """Progress of an interactive model switch, carried on every sample.

    Riding along with the gauges rather than arriving as its own event is deliberate: the per-client
    telemetry queue keeps only the newest sample, so a switch that starts and finishes between two
    deliveries would lose its terminal transition if this were an event. Repeated on every sample,
    coalescing is harmless.
    """

    status: str = "idle"
    model_id: str | None = None
    service: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    forced: bool = False


@dataclass(frozen=True, slots=True)
class SystemTelemetrySnapshot:
    sampled_at: float | None = None
    platform: str = "linux"
    cpu: CpuTelemetry = field(default_factory=CpuTelemetry)
    gpus: tuple[GpuTelemetry, ...] = ()
    vllm: VllmThroughputTelemetry = field(default_factory=VllmThroughputTelemetry)
    model_switch: ModelSwitchTelemetry = field(default_factory=ModelSwitchTelemetry)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def combined_power_draw_w(snapshot: SystemTelemetrySnapshot) -> float | None:
    """Return CPU package plus every reported GPU board draw, or ``None`` if incomplete."""
    cpu_power = snapshot.cpu.power_draw_w
    gpu_powers = [gpu.power_draw_w for gpu in snapshot.gpus]
    if cpu_power is None or not gpu_powers or any(power is None for power in gpu_powers):
        return None
    return round(cpu_power + sum(power for power in gpu_powers if power is not None), 1)


def _number(value: object) -> float | None:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _vllm_counters(metrics: str) -> tuple[float, float, tuple[str, ...]] | None:
    """Return aggregate prompt/generation counters and their loaded-model identity."""
    totals = {"prompt": 0.0, "prompt_compute": 0.0, "generation": 0.0}
    found: set[str] = set()
    models: set[str] = set()
    for raw_line in metrics.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, raw_value = line.rpartition(" ")
        if not separator:
            continue
        metric_name = name.partition("{")[0]
        kind = None
        if metric_name == "vllm:prompt_tokens_total":
            kind = "prompt"
        elif (
            metric_name == "vllm:prompt_tokens_by_source_total" and 'source="local_compute"' in name
        ):
            kind = "prompt_compute"
        elif metric_name == "vllm:generation_tokens_total":
            kind = "generation"
        if kind is None:
            continue
        value = _number(raw_value)
        if value is None or not math.isfinite(value):
            continue
        totals[kind] += value
        found.add(kind)
        marker = 'model_name="'
        if marker in name:
            models.add(name.split(marker, 1)[1].split('"', 1)[0])
    if "generation" not in found or not {"prompt", "prompt_compute"} & found:
        return None
    prompt = totals["prompt_compute"] if "prompt_compute" in found else totals["prompt"]
    return prompt, totals["generation"], tuple(sorted(models))


class VllmThroughputSampler:
    """Derive rolling prefill and decode rates from vLLM's monotonic counters."""

    def __init__(
        self,
        url: str,
        *,
        interval_s: float = _VLLM_METRICS_INTERVAL_SECONDS,
        window_size: int = _VLLM_RATE_WINDOW,
        fetch: Callable[[], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url.rstrip("/")
        self.interval_s = interval_s
        self._processing: deque[float] = deque(maxlen=window_size)
        self._generation: deque[float] = deque(maxlen=window_size)
        self._previous: tuple[float, float, float, tuple[str, ...]] | None = None
        self._last_poll = -math.inf
        self._latest = VllmThroughputTelemetry()
        self._fetch = fetch
        self._monotonic = monotonic
        self._client: httpx.Client | None = None

    def start(self) -> None:
        if self._fetch is None:
            self._client = httpx.Client(timeout=0.2)

    def stop(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None

    def sample(self) -> VllmThroughputTelemetry:
        now = self._monotonic()
        if now - self._last_poll < self.interval_s:
            return self._latest
        self._last_poll = now
        try:
            metrics = self._fetch() if self._fetch is not None else self._get_metrics()
        except (httpx.HTTPError, OSError):
            return self._reset()
        counters = _vllm_counters(metrics)
        if counters is None:
            return self._reset()
        prompt, generation, models = counters
        previous = self._previous
        self._previous = (now, prompt, generation, models)
        if previous is None:
            self._latest = VllmThroughputTelemetry(loaded_models=models)
            return self._latest
        elapsed = now - previous[0]
        if (
            elapsed <= 0
            or models != previous[3]
            or prompt < previous[1]
            or generation < previous[2]
        ):
            self._processing.clear()
            self._generation.clear()
            self._latest = VllmThroughputTelemetry(loaded_models=models)
            return self._latest
        prompt_rate = (prompt - previous[1]) / elapsed
        generation_rate = (generation - previous[2]) / elapsed
        if prompt_rate > 0:
            self._processing.append(prompt_rate)
        if generation_rate > 0:
            self._generation.append(generation_rate)
        self._latest = VllmThroughputTelemetry(
            processing_tokens_per_second=self._average(self._processing),
            generation_tokens_per_second=self._average(self._generation),
            processing_samples=len(self._processing),
            generation_samples=len(self._generation),
            loaded_models=models,
        )
        return self._latest

    def _reset(self) -> VllmThroughputTelemetry:
        """Discard rates and model identity when vLLM no longer advertises usable metrics."""
        self._processing.clear()
        self._generation.clear()
        self._previous = None
        self._latest = VllmThroughputTelemetry()
        return self._latest

    def _get_metrics(self) -> str:
        client = self._client
        if client is None:
            raise OSError("vLLM metrics client is not started")
        response = client.get(f"{self.url}/metrics")
        response.raise_for_status()
        return response.text

    @staticmethod
    def _average(values: deque[float]) -> float | None:
        return round(sum(values) / len(values), 1) if values else None


class PerfCpuPowerSampler:
    """Continuously derive CPU package watts from Linux's RAPL perf event."""

    def __init__(
        self,
        *,
        interval_s: float = 0.25,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval_s = interval_s
        self._monotonic = monotonic
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._latest_w: float | None = None
        self._sampled_at = -math.inf
        self._previous_perf_time = 0.0

    def start(self) -> None:
        if self._process is not None:
            return
        command = [
            "perf",
            "stat",
            "-a",
            "-e",
            "power/energy-pkg/",
            "-I",
            str(round(self.interval_s * 1000)),
            "--no-big-num",
            "--field-separator=,",
            "--",
            "sleep",
            "infinity",
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError:
            self._process = None
            return
        self._thread = threading.Thread(
            target=self._read_output,
            name="quill-cpu-power",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
        self._latest_w = None
        self._previous_perf_time = 0.0

    def power_draw_w(self) -> float | None:
        if self._monotonic() - self._sampled_at > self.interval_s * 4:
            return None
        return self._latest_w

    def _read_output(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self._record_perf_line(line)

    def _record_perf_line(self, line: str) -> None:
        """Consume one ``perf stat -I -x,`` row."""
        try:
            row = next(csv.reader([line]))
        except (csv.Error, StopIteration):
            return
        if len(row) < 4 or "energy-pkg" not in row[3]:
            return
        perf_time = _number(row[0])
        energy_j = _number(row[1])
        if perf_time is None or energy_j is None or energy_j < 0:
            return
        elapsed = perf_time - self._previous_perf_time
        self._previous_perf_time = perf_time
        if elapsed <= 0:
            return
        self._latest_w = round(energy_j / elapsed, 1)
        self._sampled_at = self._monotonic()


class LinuxTelemetryReader:
    """Read Linux CPU counters/sensors and NVIDIA metrics without blocking request handlers."""

    def __init__(
        self,
        vllm: VllmThroughputSampler,
        *,
        cpu_fan_hwmon_name: str | None = None,
        cpu_fan_pwm_channel: int | None = None,
        hwmon_root: Path = Path("/sys/class/hwmon"),
        cpu_power: PerfCpuPowerSampler | None = None,
    ) -> None:
        self._previous_cpu: tuple[int, int] | None = None
        self._cpu_name = self._read_cpu_name()
        self._cpu_fan_hwmon_name = cpu_fan_hwmon_name
        self._cpu_fan_pwm_channel = cpu_fan_pwm_channel
        self._hwmon_root = hwmon_root
        self._cpu_power = cpu_power or PerfCpuPowerSampler()
        self._nvml: Any = None
        self._last_fallback = 0.0
        self._fallback_gpus: tuple[GpuTelemetry, ...] = ()
        self._vllm = vllm

    def start(self) -> None:
        self._vllm.start()
        self._cpu_power.start()
        try:
            import pynvml  # type: ignore[import-untyped]

            pynvml.nvmlInit()
            self._nvml = pynvml
        except Exception:  # noqa: BLE001 - absent driver/library is a supported state
            self._nvml = None

    def stop(self) -> None:
        self._vllm.stop()
        self._cpu_power.stop()
        if self._nvml is not None:
            try:
                self._nvml.nvmlShutdown()
            except Exception:  # noqa: BLE001
                pass
        self._nvml = None

    def sample(self) -> SystemTelemetrySnapshot:
        now = time.time()
        temperature = self._cpu_temperature()
        return SystemTelemetrySnapshot(
            sampled_at=now,
            cpu=CpuTelemetry(
                self._cpu_load(),
                temperature,
                *self._memory(),
                self._cpu_name,
                fan_percent=self._cpu_fan_percent(),
                power_draw_w=self._cpu_power.power_draw_w(),
            ),
            gpus=self._gpus(now),
            vllm=self._vllm.sample(),
        )

    def _cpu_load(self) -> float | None:
        try:
            fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
            values = [int(value) for value in fields]
        except (OSError, ValueError, IndexError):
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        previous, self._previous_cpu = self._previous_cpu, (total, idle)
        if previous is None:
            return None
        total_delta, idle_delta = total - previous[0], idle - previous[1]
        if total_delta <= 0 or idle_delta < 0:
            return None
        return round(max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta)), 1)

    @staticmethod
    def _read_cpu_name() -> str | None:
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition(":")
                if separator and key.strip() in {"model name", "Hardware", "Processor"}:
                    name = " ".join(value.split())
                    if name:
                        return name
        except OSError:
            return None
        return None

    def _cpu_temperature(self) -> float | None:
        candidates: list[tuple[int, Path]] = []
        for root in sorted(self._hwmon_root.glob("hwmon*")):
            try:
                name = (root / "name").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not name.startswith(("coretemp", "k10temp", "zenpower")):
                continue
            for source in root.glob("temp*_input"):
                label_path = source.with_name(source.name.replace("_input", "_label"))
                try:
                    label = label_path.read_text(encoding="utf-8").strip()
                except OSError:
                    label = ""
                rank = (
                    0
                    if label == "Tdie"
                    else 1
                    if label == "Package id 0"
                    else 2
                    if label == "Tctl"
                    else 3
                )
                candidates.append((rank, source))
        for _rank, source in sorted(candidates, key=lambda item: (item[0], str(item[1]))):
            value = self._sensor_value(source)
            if value is not None:
                return value
        for root in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            try:
                if (root / "type").read_text(encoding="utf-8").strip() == "x86_pkg_temp":
                    return self._sensor_value(root / "temp")
            except OSError:
                continue
        return None

    def _cpu_fan_percent(self) -> float | None:
        """Read the configured motherboard PWM output as a percentage.

        The kernel's ``hwmonN`` number is not stable across boots, so locate the controller by its
        driver name every time rather than retaining a numbered sysfs path.
        """
        if self._cpu_fan_hwmon_name is None or self._cpu_fan_pwm_channel is None:
            return None
        for root in sorted(self._hwmon_root.glob("hwmon*")):
            try:
                name = (root / "name").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if name != self._cpu_fan_hwmon_name:
                continue
            try:
                pwm = int((root / f"pwm{self._cpu_fan_pwm_channel}").read_text().strip())
            except (OSError, ValueError):
                return None
            return round(pwm * 100.0 / 255.0, 1) if 0 <= pwm <= 255 else None
        return None

    @staticmethod
    def _memory() -> tuple[float | None, float | None]:
        try:
            values = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, raw = line.partition(":")
                if key in {"MemTotal", "MemAvailable"}:
                    values[key] = float(raw.split()[0]) / 1024.0
            total = values["MemTotal"]
            available = values["MemAvailable"]
        except (OSError, ValueError, KeyError, IndexError):
            return None, None
        return round(max(0.0, total - available), 1), round(total, 1)

    @staticmethod
    def _sensor_value(path: Path) -> float | None:
        try:
            value = float(path.read_text(encoding="utf-8").strip()) / 1000.0
        except (OSError, ValueError):
            return None
        return value if -100.0 <= value <= 250.0 else None

    def _gpus(self, now: float) -> tuple[GpuTelemetry, ...]:
        if self._nvml is not None:
            try:
                result = []
                for index in range(self._nvml.nvmlDeviceGetCount()):
                    handle = self._nvml.nvmlDeviceGetHandleByIndex(index)
                    memory = self._nvml.nvmlDeviceGetMemoryInfo(handle)
                    name = self._nvml.nvmlDeviceGetName(handle)
                    if isinstance(name, bytes):
                        name = name.decode(errors="replace")
                    result.append(
                        GpuTelemetry(
                            index=index,
                            name=str(name),
                            utilization_percent=float(
                                self._nvml.nvmlDeviceGetUtilizationRates(handle).gpu
                            ),
                            temperature_c=float(
                                self._nvml.nvmlDeviceGetTemperature(
                                    handle, self._nvml.NVML_TEMPERATURE_GPU
                                )
                            ),
                            memory_used_mb=round(memory.used / 1024 / 1024, 1),
                            memory_total_mb=round(memory.total / 1024 / 1024, 1),
                            sampled_at=now,
                            fan_percent=self._nvml_fan_percent(handle),
                            power_draw_w=self._nvml_power_w(handle),
                            power_limit_w=self._nvml_power_limit_w(handle),
                        )
                    )
                return tuple(result)
            except Exception:  # noqa: BLE001
                pass
        if now - self._last_fallback < 1.0:
            return self._fallback_gpus
        self._last_fallback = now
        try:
            command = [
                "nvidia-smi",
                "--query-gpu=index,name,utilization.gpu,temperature.gpu,memory.used,memory.total,fan.speed,power.draw,power.limit",
                "--format=csv,noheader,nounits",
            ]
            output = subprocess.run(
                command, capture_output=True, text=True, timeout=0.8, check=True
            ).stdout
            rows = []
            for row in csv.reader(output.splitlines()):
                if len(row) < 7:
                    continue
                rows.append(
                    GpuTelemetry(
                        int(row[0]),
                        row[1].strip(),
                        _number(row[2]),
                        _number(row[3]),
                        _number(row[4]),
                        _number(row[5]),
                        now,
                        _number(row[6]),
                        _number(row[7]) if len(row) > 7 else None,
                        _number(row[8]) if len(row) > 8 else None,
                    )
                )
            self._fallback_gpus = tuple(sorted(rows, key=lambda gpu: gpu.index))
        except (OSError, subprocess.SubprocessError, ValueError):
            self._fallback_gpus = ()
        return self._fallback_gpus

    def _nvml_fan_percent(self, handle: object) -> float | None:
        """Return NVML's per-GPU fan percentage without failing the rest of the sample."""
        try:
            value = float(self._nvml.nvmlDeviceGetFanSpeed(handle))
        except Exception:  # noqa: BLE001 - passive or unsupported cooling is a valid state
            return None
        return value if 0.0 <= value <= 100.0 else None

    def _nvml_power_w(self, handle: object) -> float | None:
        """Return current board power in watts; NVML reports integer milliwatts."""
        try:
            value = float(self._nvml.nvmlDeviceGetPowerUsage(handle)) / 1000.0
        except Exception:  # noqa: BLE001 - unsupported power telemetry is a valid state
            return None
        return round(value, 1) if value >= 0 else None

    def _nvml_power_limit_w(self, handle: object) -> float | None:
        """Return the enforced board power limit in watts when NVML exposes it."""
        try:
            value = float(self._nvml.nvmlDeviceGetEnforcedPowerLimit(handle)) / 1000.0
        except Exception:  # noqa: BLE001 - unsupported power limits are a valid state
            return None
        return round(value, 1) if value > 0 else None


class SystemTelemetryMonitor:
    def __init__(
        self,
        reader: LinuxTelemetryReader,
        interval_s: float = 0.125,
        switch_state: Callable[[], ModelSwitchTelemetry] | None = None,
        on_sample: Callable[[SystemTelemetrySnapshot], None] | None = None,
    ) -> None:
        self.reader = reader
        self.interval_s = interval_s
        self._switch_state = switch_state
        self._on_sample = on_sample
        self.latest = SystemTelemetrySnapshot()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[SystemTelemetrySnapshot]] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.reader.start()
        self._thread = threading.Thread(target=self._run, name="quill-telemetry", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self.reader.stop()

    def _run(self) -> None:
        deadline = time.monotonic()
        while not self._stop.is_set():
            try:
                snapshot = self.reader.sample()
            except Exception:  # noqa: BLE001
                snapshot = SystemTelemetrySnapshot(sampled_at=time.time())
            if self._switch_state is not None:
                try:
                    snapshot = replace(snapshot, model_switch=self._switch_state())
                except Exception:  # noqa: BLE001 - never let a switch probe stop the gauges
                    pass
            if self._on_sample is not None:
                try:
                    self._on_sample(snapshot)
                except Exception:  # noqa: BLE001 - persistence must never stop live telemetry
                    pass
            self.latest = snapshot
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._publish, snapshot)
            deadline += self.interval_s
            now = time.monotonic()
            if deadline <= now:
                deadline = now + self.interval_s
            self._stop.wait(deadline - now)

    def _publish(self, snapshot: SystemTelemetrySnapshot) -> None:
        for queue in self._subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(snapshot)

    async def subscribe(self) -> AsyncIterator[SystemTelemetrySnapshot]:
        queue: asyncio.Queue[SystemTelemetrySnapshot] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        try:
            yield self.latest
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)
