"""Shared exact live-usage value passed from coding-agent runners to Quill."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LiveUsage:
    """Processed usage plus the current occupied context across a runner or phase scope."""

    input_tokens: int = 0
    output_tokens: int = 0
    context_window_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens
