"""Audit tests against the current rollout format."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import doctor_audit

from conftest import SID, write_modern_session


def test_modern_session_stats(tmp_path: Path):
    home = tmp_path / "modern"
    path = write_modern_session(home)
    stats = doctor_audit.session_stats(path)

    assert stats["parse_error"] is None
    assert stats["thread_id"] == SID
    assert stats["compactions"] == 1
    assert stats["compaction_events"] == 1
    assert stats["compaction_balanced"] is True
    # one interrupted turn, one still-running turn
    assert stats["aborted_turns"] == {"interrupted": 1}
    assert stats["unfinished_turns"] == 1
    # the embedded error on task_complete is the durable error record
    assert stats["error_kinds"] == {"usage_limit_exceeded": 1}
    # one call whose output never arrived
    assert stats["calls_without_outputs"] == 1
    assert stats["unmatched_outputs"] == 0
    assert stats["empty_call_ids"] == 0


def test_modern_session_is_at_risk_for_missing_output(tmp_path: Path):
    home = tmp_path / "modern"
    path = write_modern_session(home)
    stats = doctor_audit.session_stats(path)
    stats["at_risk"] = doctor_audit.at_risk(stats, set())
    assert stats["at_risk"] is True

    table = doctor_audit.render_table(
        {"sessions": [stats], "summary": {"risk": 1}}, set()
    )
    assert "CALLS_NO_OUTPUT:1" in table
    assert "ERRORS:usage_limit_exceeded=1" in table
    assert "ABORTED:interrupted=1" in table


def test_legacy_and_modern_events_do_not_double_count(tmp_path: Path):
    """The same turn counts once whether the writer used task_* or turn_*."""
    from conftest import encode_session, session_meta

    rows = [
        session_meta(),
        {"event_msg": {"type": "task_started", "turn_id": "t1"}},
        {"type": "event_msg",
         "payload": {"type": "turn_complete", "turn_id": "t1"}},
    ]
    home = tmp_path / "mixed"
    (home / "sessions").mkdir(parents=True)
    path = home / "sessions" / f"rollout-x-{SID}.jsonl"
    path.write_bytes(encode_session(rows))

    stats = doctor_audit.session_stats(path)
    assert stats["unfinished_turns"] == 0