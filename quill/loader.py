"""ModelLoader — load/unload presets on the llama.cpp router over HTTP (WI-1b).

Replaces the bash ``wb-load.sh`` / ``wb-unload.sh`` scripts in the pipeline. The
driver is Python; model loading is just HTTP to the router, whose base URL comes from
``QUILL_ROUTER_URL`` (machine-level, same file as ``QUILL_VLLM_URL``). No bash, no curl,
no subprocess.

Router contract (observed from the bash scripts it replaces):

* ``GET  /models``          → ``{"data": [{"id": str, "status": {"value": str}}, ...]}``
  where ``status.value`` is one of ``loaded`` / ``loading`` / ``unloaded``.
* ``POST /models/load``     → body ``{"model": <preset>}``; success ⇒ ``{"success": true, ...}``.
* ``POST /models/unload``   → body ``{"model": <preset>}``; idempotent — a preset that
  is already unloaded answers ``"... is not running"``, which is treated as success.

Invariants:

* **One-at-a-time.** :meth:`load` first unloads any *other* loaded preset (queried
  live from ``/models`` — names are never hardcoded), then loads the target. If the
  target is already loaded it is a no-op.
* **No allowlist.** The driver only ever passes presets from the resolved config,
  so the wrong model can't be requested here.
* **Typed failure.** A load that the router rejects, or that never reaches
  ``loaded`` within the timeout, raises :class:`ModelLoadError` — the driver treats
  this as a CRASH and re-spawns per its retry budget.
"""

from __future__ import annotations

import os
import time
from typing import Self

import httpx

#: Fallback when ``QUILL_ROUTER_URL`` is unset. Machine addresses belong in the environment
#: file, never in source or a repository's ``quillfolio.toml`` — see :func:`router_url`.
DEFAULT_ROUTER_URL = "http://localhost:8001"
LOAD_TIMEOUT = 180
_POLL_INTERVAL = 3.0
# Request timeouts: list/status is cheap; the load POST itself can take a while
# as the router spins up the server, so it gets a generous read timeout.
_LIST_TIMEOUT = 5.0
_UNLOAD_TIMEOUT = 30.0
_LOAD_TIMEOUT = 120.0


def router_url() -> str:
    """The llama.cpp router's base URL, from ``QUILL_ROUTER_URL`` or the local default.

    Read at call time rather than import time so a test — or a service that loads its
    environment file after import — sees the current value.
    """
    return (os.environ.get("QUILL_ROUTER_URL") or "").strip() or DEFAULT_ROUTER_URL


class ModelLoadError(RuntimeError):
    """A load was rejected by the router or never reached ``loaded`` in time.

    The driver treats this as a CRASH and re-spawns per ``[retries].spawn``.
    """


class ModelLoader:
    """Load/unload llama.cpp router presets, one model at a time."""

    def __init__(self, host: str | None = None, client: httpx.Client | None = None) -> None:
        self.host = (host or router_url()).rstrip("/")
        # An injected client (tests/transport mocks) is borrowed, not owned, so we
        # don't close it out from under the caller.
        self._client = client or httpx.Client()
        self._owns_client = client is None

    # -- low-level router calls -------------------------------------------------

    def _models(self) -> list[dict[str, object]]:
        """GET /models → the raw ``data`` list (empty on any error)."""
        try:
            resp = self._client.get(f"{self.host}/models", timeout=_LIST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        items = data.get("data") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    @staticmethod
    def _status_value(model: dict[str, object]) -> str:
        """Pull ``status.value`` from one /models entry, tolerating shape drift."""
        status = model.get("status")
        if isinstance(status, dict):
            value = status.get("value")
            if isinstance(value, str):
                return value.strip()
        return "unknown"

    @staticmethod
    def _id(model: dict[str, object]) -> str:
        """Preset id from one /models entry, CRLF-stripped."""
        mid = model.get("id")
        return mid.strip() if isinstance(mid, str) else ""

    # -- public API -------------------------------------------------------------

    def loaded(self) -> list[str]:
        """Preset ids whose ``status.value`` is ``loaded``."""
        return [
            self._id(m) for m in self._models() if self._status_value(m) == "loaded" and self._id(m)
        ]

    def presets(self) -> list[str]:
        """Every preset id the router knows about, loaded or not.

        This is the set a config may legally name in a phase's ``model``, which is what makes it
        worth exposing: a client writing a config needs the menu, not just what happens to be
        resident right now.
        """
        return [pid for m in self._models() if (pid := self._id(m))]

    def status(self, preset: str) -> str:
        """``status.value`` for ``preset`` (``unloaded`` if the router doesn't list it)."""
        target = preset.strip()
        for m in self._models():
            if self._id(m) == target:
                return self._status_value(m)
        return "unloaded"

    def needs_load(self, preset: str) -> bool:
        """Return whether ``preset`` is not already resident in the router."""
        return self.status(preset.strip()) != "loaded"

    def unload(self, preset: str) -> None:
        """Unload ``preset``. Idempotent — already-unloaded is not an error."""
        try:
            resp = self._client.post(
                f"{self.host}/models/unload",
                json={"model": preset},
                timeout=_UNLOAD_TIMEOUT,
            )
        except httpx.HTTPError:
            # Network hiccup on an unload is non-fatal: the swap path re-queries
            # /models anyway, and unload is always safe to retry.
            return
        body = resp.text
        if '"success":true' in body.replace(" ", "") or "is not running" in body:
            return
        # Any other response is still not worth raising on — unloading is never a
        # gate — but the caller's subsequent load poll will catch a real problem.

    def unload_all(self) -> None:
        """Unload every currently-loaded preset (queried live)."""
        for preset in self.loaded():
            self.unload(preset)

    def load(self, preset: str, timeout: float = LOAD_TIMEOUT) -> None:
        """Load ``preset``, enforcing one-at-a-time, and block until it is ready.

        Unloads any *other* loaded preset first, skips the load if ``preset`` is
        already loaded, then POSTs the load and polls until ``status`` reads
        ``loaded`` or ``timeout`` seconds elapse.

        Latch hardening (ticket #33): some routers latch a force-unloaded state on
        ``unload_all`` and then reject / stall the *next* load — which would let the
        always-unload-at-run-end policy brick the following run's phase-1 load. So if the
        first attempt fails (rejected, or never reaches ``loaded``), we send a full unload
        of the target to clear any latched state and retry the load **once**.

        Raises:
            ModelLoadError: both the initial attempt and the post-unload retry failed
                (rejected, or never reached ``loaded`` within ``timeout``).
        """
        target = preset.strip()

        # One-at-a-time: drop every other loaded preset (don't touch the target).
        for other in self.loaded():
            if other != target:
                self.unload(other)

        # Already up? Nothing to do.
        if self.status(target) == "loaded":
            return

        try:
            self._attempt_load(preset, target, timeout)
            return
        except ModelLoadError as first:
            # Clear any latched force-unload state, then retry the load exactly once.
            self.unload(preset)
            try:
                self._attempt_load(preset, target, timeout)
                return
            except ModelLoadError as second:
                raise ModelLoadError(
                    f"load of {preset!r} failed, and the retry after a full unload also failed: "
                    f"{second}"
                ) from first

    def _attempt_load(self, preset: str, target: str, timeout: float) -> None:
        """One load attempt: POST the load, then poll until ``loaded`` or ``timeout``."""
        try:
            resp = self._client.post(
                f"{self.host}/models/load",
                json={"model": preset},
                timeout=_LOAD_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ModelLoadError(f"load request to router failed for {preset!r}: {exc}") from exc
        if '"success":true' not in resp.text.replace(" ", ""):
            raise ModelLoadError(
                f"router did not accept load of {preset!r} (is the preset name correct?): "
                f"{resp.text.strip()}"
            )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status(target) == "loaded":
                return
            time.sleep(_POLL_INTERVAL)
        # One last check after the loop in case the final sleep straddled the deadline.
        if self.status(target) == "loaded":
            return
        raise ModelLoadError(f"{preset!r} did not reach 'loaded' within {timeout:g}s")

    # -- lifecycle --------------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
