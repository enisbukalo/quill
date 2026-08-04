"""Unit tests for ModelLoader (WI-1b).

The router is faked with ``httpx.MockTransport`` — a stateful handler that mimics
``GET /models`` / ``POST /models/load`` / ``POST /models/unload`` so we can assert
the parsing (incl. CRLF safety), the one-at-a-time invariant, idempotent unload,
and the typed-error paths without a live router.
"""

from __future__ import annotations

import json

import httpx
import pytest

from quill.loader import ModelLoader, ModelLoadError


class FakeRouter:
    """In-memory router: tracks each preset's status and serves the 3 endpoints."""

    def __init__(self, statuses: dict[str, str], *, line_sep: str = "\n") -> None:
        # line_sep lets a test inject CRLF into the JSON payload (status.value /
        # id arrive with trailing \r when the router emits CRLF-terminated lines).
        self._statuses = dict(statuses)
        self._line_sep = line_sep
        self.load_calls: list[str] = []
        self.unload_calls: list[str] = []
        # load() makes a preset "loaded" immediately by default; a test can flip
        # this to simulate a model that accepts the load but never comes up.
        self.load_succeeds = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/models":
            return self._models_response()
        body = json.loads(request.content) if request.content else {}
        model = body.get("model", "")
        if path == "/models/load":
            self.load_calls.append(model)
            if self.load_succeeds:
                self._statuses[model] = "loaded"
            return httpx.Response(200, json={"success": True})
        if path == "/models/unload":
            self.unload_calls.append(model)
            if self._statuses.get(model) == "loaded":
                self._statuses[model] = "unloaded"
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"success": False, "error": "model is not running"})
        return httpx.Response(404)

    def _models_response(self) -> httpx.Response:
        data = {
            "data": [
                {"id": name, "status": {"value": value}} for name, value in self._statuses.items()
            ]
        }
        # Emit with the chosen line separator so CRLF can leak into id/value.
        text = json.dumps(data, indent=2).replace("\n", self._line_sep)
        return httpx.Response(
            200, content=text.encode(), headers={"content-type": "application/json"}
        )


def make_loader(router: FakeRouter) -> ModelLoader:
    client = httpx.Client(transport=httpx.MockTransport(router.handler))
    return ModelLoader(client=client)


def test_loaded_lists_only_loaded() -> None:
    router = FakeRouter({"plan-27b": "loaded", "impl-35b": "unloaded", "review-27b": "loading"})
    loader = make_loader(router)
    assert loader.loaded() == ["plan-27b"]


def test_status_known_and_unknown() -> None:
    router = FakeRouter({"plan-27b": "loaded", "impl-35b": "loading"})
    loader = make_loader(router)
    assert loader.status("plan-27b") == "loaded"
    assert loader.status("impl-35b") == "loading"


def test_needs_load_reflects_resident_status() -> None:
    loader = make_loader(FakeRouter({"qwen": "loaded", "gemma": "unloaded"}))

    assert loader.needs_load("qwen") is False
    assert loader.needs_load("gemma") is True
    # Preset the router doesn't list at all reads as unloaded.
    assert loader.status("ghost") == "unloaded"


def test_crlf_safe_parse() -> None:
    """CRLF-terminated router JSON must not bleed \\r into ids or status values."""
    router = FakeRouter({"plan-27b": "loaded"}, line_sep="\r\n")
    loader = make_loader(router)
    assert loader.loaded() == ["plan-27b"]
    assert loader.status("plan-27b") == "loaded"


def test_load_already_loaded_is_noop() -> None:
    router = FakeRouter({"plan-27b": "loaded"})
    loader = make_loader(router)
    loader.load("plan-27b")
    assert router.load_calls == []  # no load POST when already up


def test_load_unloads_other_first() -> None:
    """Loading B while A is up unloads A first (one-at-a-time invariant)."""
    router = FakeRouter({"plan-27b": "loaded", "impl-35b": "unloaded"})
    loader = make_loader(router)
    loader.load("impl-35b")
    assert router.unload_calls == ["plan-27b"]
    assert router.load_calls == ["impl-35b"]
    assert loader.status("impl-35b") == "loaded"
    assert loader.status("plan-27b") == "unloaded"


def test_load_from_cold_no_unload() -> None:
    router = FakeRouter({"impl-35b": "unloaded"})
    loader = make_loader(router)
    loader.load("impl-35b")
    assert router.unload_calls == []
    assert router.load_calls == ["impl-35b"]


def test_load_preset_with_spaces() -> None:
    """Gemma preset name has spaces — must round-trip unchanged."""
    router = FakeRouter({"gemma-4-31B-it-Q8 MTP": "unloaded"})
    loader = make_loader(router)
    loader.load("gemma-4-31B-it-Q8 MTP")
    assert router.load_calls == ["gemma-4-31B-it-Q8 MTP"]
    assert loader.status("gemma-4-31B-it-Q8 MTP") == "loaded"


def test_load_rejected_raises() -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"success": False, "error": "unknown preset"})

    loader = ModelLoader(client=httpx.Client(transport=httpx.MockTransport(reject)))
    with pytest.raises(ModelLoadError, match="did not accept"):
        loader.load("bogus")


def test_load_timeout_raises() -> None:
    """Router accepts the load but the preset never reaches 'loaded'."""
    router = FakeRouter({"impl-35b": "unloaded"})
    router.load_succeeds = False  # POST returns success but status stays unloaded
    loader = make_loader(router)
    with pytest.raises(ModelLoadError, match="did not reach 'loaded'"):
        loader.load("impl-35b", timeout=0.05)


def test_load_retries_after_full_unload_on_reject() -> None:
    """First load POST is rejected; after a full unload the retry is accepted (#33 latch fix)."""

    first = True
    status = "unloaded"
    loads: list[str] = []
    unloads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal first, status
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "m", "status": {"value": status}}]})
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/models/unload":
            unloads.append(body.get("model", ""))
            status = "unloaded"
            return httpx.Response(200, json={"success": True})
        # load
        loads.append(body.get("model", ""))
        if first:
            first = False
            return httpx.Response(200, json={"success": False, "error": "latched"})
        status = "loaded"
        return httpx.Response(200, json={"success": True})

    loader = ModelLoader(client=httpx.Client(transport=httpx.MockTransport(handler)))
    loader.load("m", timeout=1.0)  # must not raise
    assert len(loads) == 2  # initial reject + retry
    assert unloads == ["m"]  # full unload between attempts
    assert loader.status("m") == "loaded"


def test_load_retries_after_full_unload_on_stall() -> None:
    """First load accepted but stalls; after a full unload the retry comes up loaded."""

    attempts = 0
    status = "unloaded"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts, status
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "m", "status": {"value": status}}]})
        if request.url.path == "/models/unload":
            status = "unloaded"
            return httpx.Response(200, json={"success": True})
        attempts += 1
        # First load: accepted but never comes up. Second load: comes up.
        if attempts >= 2:
            status = "loaded"
        return httpx.Response(200, json={"success": True})

    loader = ModelLoader(client=httpx.Client(transport=httpx.MockTransport(handler)))
    loader.load("m", timeout=0.1)  # must not raise; retry succeeds
    assert loader.status("m") == "loaded"


def test_unload_idempotent() -> None:
    """Unloading a preset that isn't running is a no-op, not an error."""
    router = FakeRouter({"plan-27b": "unloaded"})
    loader = make_loader(router)
    loader.unload("plan-27b")  # must not raise
    assert router.unload_calls == ["plan-27b"]


def test_unload_all() -> None:
    router = FakeRouter({"plan-27b": "loaded", "impl-35b": "loaded", "review-27b": "unloaded"})
    loader = make_loader(router)
    loader.unload_all()
    assert sorted(router.unload_calls) == ["impl-35b", "plan-27b"]
    assert loader.loaded() == []


def test_context_manager_closes_owned_client() -> None:
    with ModelLoader(host="http://localhost:8001") as loader:
        assert loader.host == "http://localhost:8001"
    assert loader._client.is_closed
