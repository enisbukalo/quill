"""Pluggable model-server backends behind one pre-spawn seam.

quill loads a model server *before* each phase spawn and tears it down at run end. Two backends
exist, with different contracts:

* **llama.cpp router** (:class:`quill.loader.ModelLoader`) — swaps presets: ``POST /models/load`` /
  ``/models/unload``, one model at a time. Each phase can request a different preset (= a
  different sampling profile), and the swap reloads the server.

* **vllm service-switched** (:class:`VllmServer`, this module) — one model is resident at a time;
  a configured service command swaps it when a phase requests another model. Every run clears the
  resident model's prefix cache once before its first prompt, then preserves prefixes between
  phases for the rest of that run.

Both satisfy the same tiny protocol the engine calls (:class:`quill.phases.ModelLoaderLike`):
``load(preset, timeout)`` before every spawn and ``unload_all()`` at run end. The engine, phases,
and every test fake are agnostic to which backend is wired — the CLI picks one from
``[runner] backend`` in the config.

vllm control surface (server 0.23.1rc1.dev799, observed live):

* ``GET  /health``               → 200 when the server is up and the model is ready.
* ``POST /reset_prefix_cache``    → 200, empty body; flushes the prefix cache. Mounted only when the
  server is launched with ``VLLM_SERVER_DEV_MODE=1`` (else 404).

Failure policy (matches the llama.cpp loader): a health check that isn't 200, or a reset that
errors / answers non-2xx, raises :class:`~quill.loader.ModelLoadError`. The driver treats that as a
CRASH and re-spawns per ``[retries].spawn`` — a stale cache is never silently accepted.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Self

import httpx

from quill.loader import ModelLoader, ModelLoadError

if TYPE_CHECKING:
    from quill.config import QuillfolioConfig
    from quill.phases import ModelLoaderLike

_HEALTH_TIMEOUT = 5.0
_RESET_TIMEOUT = 30.0
_MODEL_POLL_SECONDS = 2.0

type CommandRunner = Callable[[Sequence[str]], None]


class VllmServer:
    """Always-on vllm backend with a mandatory per-run prefix-cache boundary.

    Implements the same ``load`` / ``unload_all`` seam as :class:`quill.loader.ModelLoader`, so it
    drops into ``PipelineDeps.loader`` unchanged. ``load`` requires an exact ID from ``/v1/models``
    and starts the associated service when another model is resident.
    """

    def __init__(
        self,
        url: str,
        client: httpx.Client | None = None,
        *,
        clear_prefix_cache: bool = False,
        command: tuple[str, ...] = (),
        models: dict[str, str] | None = None,
        command_runner: CommandRunner | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.url = url.rstrip("/")
        # Kept as an accepted constructor argument while older API/CLI clients roll forward. It no
        # longer changes behavior: cache isolation is always once per run, never between phases.
        _ = clear_prefix_cache
        self.command = command
        self.models = models or {}
        self._command_runner = command_runner or _run_command
        self._monotonic = monotonic
        self._sleep = sleep
        # An injected client (tests/transport mocks) is borrowed, not owned, so we don't close it
        # out from under the caller.
        self._client = client or httpx.Client()
        self._owns_client = client is None
        self._prefix_cache_prepared = False

    # -- the pre-spawn seam -----------------------------------------------------

    def load(self, preset: str = "", timeout: float = 0.0) -> None:
        """Ready the server for the next phase and establish the run's cache boundary.

        ``preset`` must exactly match a model ID advertised by ``GET /v1/models``. When it does
        not, the configured command and service association are used and polled until ``timeout``.

        Raises:
            ModelLoadError: the server is not healthy, or the prefix-cache reset failed. The driver
                treats this as a CRASH and re-spawns per ``[retries].spawn``.
        """
        if preset:
            self._ensure_model(preset, timeout)
        else:
            self._check_health()
        if not self._prefix_cache_prepared:
            self._reset_prefix_cache()
            self._prefix_cache_prepared = True

    def unload_all(self) -> None:
        """No-op: vllm serves one model for the whole run; there is nothing to unload."""

    def healthy(self) -> bool:
        """True if the server answers ``GET /health`` with 200. Never raises (for status probes)."""
        try:
            self._check_health()
        except ModelLoadError:
            return False
        return True

    def model_ids(self) -> list[str]:
        """Exact model IDs currently advertised by vLLM's OpenAI-compatible API."""
        return [str(card["id"]) for card in self.model_cards()]

    def needs_load(self, preset: str) -> bool:
        """Return whether vLLM must switch services before it can serve ``preset``."""
        return preset not in self.model_ids()

    def model_cards(self) -> list[dict[str, Any]]:
        """Model cards currently advertised by vLLM's OpenAI-compatible API."""
        try:
            resp = self._client.get(f"{self.url}/v1/models", timeout=_HEALTH_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise ValueError("response has no data list")
            return [item for item in data if isinstance(item, dict) and "id" in item]
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelLoadError(
                f"could not read vllm models from {self.url}/v1/models: {exc}"
            ) from exc

    # -- low-level router calls -------------------------------------------------

    def _check_health(self) -> None:
        try:
            resp = self._client.get(f"{self.url}/health", timeout=_HEALTH_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ModelLoadError(f"vllm health check to {self.url} failed: {exc}") from exc
        if resp.status_code != 200:
            raise ModelLoadError(
                f"vllm at {self.url} is not healthy (GET /health returned {resp.status_code})."
            )

    def _ensure_model(self, preset: str, timeout: float) -> None:
        """Switch services when needed, then wait until vLLM advertises exactly ``preset``."""
        try:
            loaded = self.model_ids()
        except ModelLoadError:
            loaded = []
        if preset in loaded:
            self._check_health()
            return

        service = self.models.get(preset)
        if service is None:
            shown = ", ".join(loaded) or "none"
            raise ModelLoadError(
                f"vllm model '{preset}' is not loaded (advertised: {shown}) and has no "
                "runner.vllm.models service association."
            )
        self.switch_to(preset, service, timeout)

    def switch_to(
        self,
        preset: str,
        service: str,
        timeout: float,
        *,
        command: tuple[str, ...] = (),
    ) -> None:
        """Start ``service`` and wait until vLLM advertises exactly ``preset``.

        Each model unit declares ``Conflicts=`` against its siblings, so starting one stops whatever
        was resident. There is deliberately no stop step.

        ``command`` overrides the configured launcher for callers that hold a machine-level command
        rather than a repository's. Interactive switching goes through here rather than
        :meth:`load`, which additionally resets the prefix cache — that is a per-run boundary and
        means nothing outside a run.

        Raises:
            ModelLoadError: the command failed, or ``preset`` never appeared within ``timeout``.
        """
        launcher = command or self.command
        if not launcher:
            raise ModelLoadError(
                f"vllm model '{preset}' needs a switch but no command is configured."
            )
        argv = (*launcher, service)
        try:
            self._command_runner(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModelLoadError(f"could not start vllm service '{service}': {exc}") from exc

        deadline = self._monotonic() + timeout
        last_loaded: list[str] = []
        while self._monotonic() < deadline:
            try:
                last_loaded = self.model_ids()
                if preset in last_loaded:
                    self._check_health()
                    return
            except ModelLoadError:
                pass
            self._sleep(_MODEL_POLL_SECONDS)
        shown = ", ".join(last_loaded) or "none"
        raise ModelLoadError(
            f"timed out after {timeout:g}s waiting for vllm model '{preset}' "
            f"after starting service '{service}' (advertised: {shown})."
        )

    def unload_service(
        self,
        preset: str,
        service: str,
        timeout: float,
        *,
        command: tuple[str, ...],
    ) -> None:
        """Stop ``service`` and wait until vLLM no longer advertises ``preset``.

        An unreachable server is the expected cold state after stopping the only resident model.
        A still-reachable server also counts as unloaded once the requested model disappears.

        Raises:
            ModelLoadError: the command failed, or ``preset`` remains advertised through timeout.
        """
        if not command:
            raise ModelLoadError(
                f"vllm model '{preset}' cannot be unloaded because no stop command is configured."
            )
        argv = (*command, service)
        try:
            self._command_runner(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModelLoadError(f"could not stop vllm service '{service}': {exc}") from exc

        deadline = self._monotonic() + timeout
        while self._monotonic() < deadline:
            try:
                if preset not in self.model_ids():
                    return
            except ModelLoadError:
                return
            self._sleep(_MODEL_POLL_SECONDS)
        raise ModelLoadError(
            f"timed out after {timeout:g}s waiting for vllm model '{preset}' "
            f"to unload after stopping service '{service}'."
        )

    def _reset_prefix_cache(self) -> None:
        try:
            resp = self._client.post(f"{self.url}/reset_prefix_cache", timeout=_RESET_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ModelLoadError(f"vllm prefix-cache reset to {self.url} failed: {exc}") from exc
        if resp.status_code // 100 != 2:
            # 404 here means the server was launched without VLLM_SERVER_DEV_MODE=1, so the
            # /reset_prefix_cache route is unmounted — call that out specifically.
            hint = (
                " — the route is unmounted; launch vllm with VLLM_SERVER_DEV_MODE=1"
                if resp.status_code == 404
                else ""
            )
            raise ModelLoadError(f"vllm prefix-cache reset returned {resp.status_code}{hint}.")

    # -- lifecycle --------------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def make_model_server(
    config: QuillfolioConfig, *, clear_prefix_cache: bool = False
) -> ModelLoaderLike:
    """The model-server backend for ``config``, chosen by ``[runner] backend``.

    ``vllm`` → :class:`VllmServer` (always-on model, one reset per run by default); anything else →
    the llama.cpp :class:`~quill.loader.ModelLoader` (preset swap per phase). Config validation has
    already guaranteed a vllm backend carries a machine-level ``vllm_url``. Shared by the CLI and the API so
    both wire the same backend from one place.
    """
    if config.backend == "vllm":
        return VllmServer(
            config.vllm_url,
            clear_prefix_cache=clear_prefix_cache,
            command=config.vllm_command,
            models=config.vllm_models,
        )
    return ModelLoader()


def _run_command(args: Sequence[str]) -> None:
    """Start a configured vLLM service without invoking a shell.

    Raises with the command's own stderr rather than a bare ``CalledProcessError``. The useful part
    of a failed switch is always in stderr — "sudo: A terminal is required to authenticate" tells
    you the sudoers rule did not match, where "returned non-zero exit status 1" tells you nothing.
    """
    result = subprocess.run(list(args), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (
            result.stderr or result.stdout or ""
        ).strip() or f"exit status {result.returncode}"
        raise ModelLoadError(f"`{' '.join(args)}` failed: {detail}")
