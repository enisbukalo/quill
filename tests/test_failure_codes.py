from quill.failure_codes import classify_failure, classify_terminal_failure


def test_failure_codes_prioritize_actionable_known_conditions() -> None:
    assert classify_failure("no receipt in worker output", "plan").code == "worker_no_receipt"
    assert classify_failure("tests failed", "test").code == "tests_failed"
    assert classify_failure("internal error: boom", "plan").code == "internal_error"


def test_vllm_disconnect_replaces_missing_receipt_symptom() -> None:
    result = classify_terminal_failure(
        "no receipt in worker output",
        "review",
        backend="vllm",
        model_server_healthy=False,
    )
    assert result.code == "vllm_disconnected"
    assert result.label == "Lost connection to vLLM"
