"""Stable terminal-run failure categories for API presentation and lifetime accounting."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FailureInfo:
    code: str
    label: str


def classify_failure(reason: str | None, phase: str | None) -> FailureInfo:
    text = (reason or "").lower()
    if "no receipt" in text or "missing receipt" in text:
        return FailureInfo("worker_no_receipt", "Worker returned no completion receipt")
    if "timed out" in text or "timeout" in text:
        return FailureInfo("timeout", "Operation timed out")
    if "ci" in (phase or "").lower() or "check" in text and "fail" in text:
        return FailureInfo("ci_failed", "CI checks failed")
    if phase == "test" or "test failed" in text:
        return FailureInfo("tests_failed", "Local tests failed")
    if phase == "build" or "build failed" in text:
        return FailureInfo("build_failed", "Local build failed")
    if text.startswith("internal error"):
        return FailureInfo("internal_error", "Quill encountered an internal error")
    if "config" in text or "workflow" in text:
        return FailureInfo("configuration_error", "Run configuration is invalid")
    if "workspace" in text or "branch" in text or "open pr" in text:
        return FailureInfo("workspace_error", "Workspace or branch preparation failed")
    return FailureInfo("phase_failed", "Phase execution failed")


def classify_terminal_failure(
    reason: str | None, phase: str | None, *, backend: str, model_server_healthy: bool
) -> FailureInfo:
    """Prefer model-server loss over the downstream missing-receipt symptom it causes."""
    if backend == "vllm" and not model_server_healthy:
        return FailureInfo("vllm_disconnected", "Lost connection to vLLM")
    return classify_failure(reason, phase)
