from __future__ import annotations

import json
from pathlib import Path

from quill.eventlog import EventLog


def test_event_log_flushes_and_syncs_every_append(tmp_path: Path, monkeypatch) -> None:
    synced: list[int] = []
    monkeypatch.setattr("quill.eventlog.os.fsync", synced.append)

    with EventLog(tmp_path / "run") as log:
        log.append({"type": "phase_started", "ts": 1.0, "phase": "plan"})
        assert json.loads(log.path.read_text(encoding="utf-8")) == {
            "type": "phase_started",
            "ts": 1.0,
            "phase": "plan",
        }
        log.append({"type": "phase_done", "ts": 2.0, "phase": "plan"})

    assert len(synced) == 2
    assert len((tmp_path / "run" / "state.jsonl").read_text().splitlines()) == 2
