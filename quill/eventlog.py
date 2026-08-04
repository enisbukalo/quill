"""Durable append-only workflow history for one Quill run."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TextIO

from quill.events import Event

EVENT_LOG_NAME = "state.jsonl"


class EventLog:
    """Append JSON events and force every complete line to stable storage."""

    def __init__(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        self.path = run_dir / EVENT_LOG_NAME
        self._stream: TextIO = self.path.open("a", encoding="utf-8")

    def append(self, event: Event) -> None:
        self._stream.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
