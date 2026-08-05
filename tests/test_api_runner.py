"""Focused tests for live API run attribution."""

from quill.live_usage import LiveUsage
from quill_api.runner import _LiveUsageAttribution


def test_live_usage_keeps_retry_row_attempt_local_and_phase_total_cumulative() -> None:
    attribution = _LiveUsageAttribution({"technical": LiveUsage(100, 20, 90)})

    attribution.start_execution("technical")
    attribution.update("technical", LiveUsage(40, 10, 35))

    assert attribution.phase_usage["technical"] == LiveUsage(140, 30, 125)
    assert attribution.active_execution_usage["technical"] == LiveUsage(40, 10, 35)

    attribution.start_execution("technical")
    attribution.update("technical", LiveUsage(75, 18, 62))

    assert attribution.phase_usage["technical"] == LiveUsage(175, 38, 152)
    assert attribution.active_execution_usage["technical"] == LiveUsage(35, 8, 27)


def test_live_usage_falls_back_to_inherited_baseline_if_start_event_is_absent() -> None:
    attribution = _LiveUsageAttribution({"technical": LiveUsage(100, 20, 90)})

    attribution.update("technical", LiveUsage(40, 10, 35))

    assert attribution.phase_usage["technical"] == LiveUsage(140, 30, 125)
    assert attribution.active_execution_usage["technical"] == LiveUsage(40, 10, 35)
