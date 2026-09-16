"""End-to-end tests: run the actual doctor_fix subcommands against a fake
CODEX_HOME with a synthetic broken session, and verify the promised
invariants hold on disk afterwards."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import doctor_fix

from conftest import GATEWAY_ERROR, GONE_TURN, SID


def make_args(**kw) -> argparse.Namespace:
    base = {
        "session_id": SID,
        "file": None,
        "backup_dir": None,
        "backup": None,
        "model": None,
        "effort": None,
        "key": None,
        "value": None,
        "expect_count": None,
        "checkpoint": None,
        "checkpoint_time": None,
        "error_text": [],
        "orphan_turn": [],
        "dedupe_index": False,
        "anchor_text": None,
        "continuation": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def session_file(home: Path) -> Path:
    hits = list((home / "sessions").rglob("*.jsonl"))
    assert len(hits) == 1
    return hits[0]


def reload_rows(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_bytes().splitlines() if l.strip()]


def backups(home: Path) -> list[Path]:
    return sorted((home / ".thread-doctor-backups").glob("*.jsonl"))


def test_normalize_full_session(home: Path):
    path = session_file(home)
    args = make_args(model="target-model", effort="medium")
    doctor_fix.cmd_normalize(args)

    rows = reload_rows(path)
    models = set()
    efforts = set()
    for row in rows:
        models |= set(_collect(row, "model"))
        efforts |= set(_collect(row, "effort") + _collect(row, "reasoning_effort"))
    assert models == {"target-model"}
    assert efforts == {"medium"}
    assert backups(home), "backup must exist"
    text = path.read_text(encoding="utf-8")
    assert "old-model-1" not in text and "deprecated/old-model-3" not in text


def _collect(obj, key: str):
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str):
                found.append(v)
            else:
                found += _collect(v, key)
    elif isinstance(obj, list):
        for item in obj:
            found += _collect(item, key)
    return found


def test_call_ids_repair(home: Path):
    rows = reload_rows(session_file(home))
    empty_before = sum(
        1 for r in rows
        if (r.get("payload") or {}).get("type") in ("function_call",)
        and not (r.get("payload") or {}).get("call_id")
    )
    assert empty_before > 0
    doctor_fix.cmd_call_ids(make_args())
    rows = reload_rows(session_file(home))
    empties = [
        r for r in rows
        if (r.get("payload") or {}).get("type") in ("function_call",)
        and not (r.get("payload") or {}).get("call_id")
    ]
    assert not empties
    assert doctor_fix.unmatched_outputs(rows) == 0


def test_remove_lines_exact_count(home: Path):
    bad_rs = "ocxr1:eyJzaWciOiJraXJvLXJzLXRoaW5raW5nLXNpZ25hdHVyZSJ9"
    rows_before = reload_rows(session_file(home))
    doctor_fix.cmd_remove_lines(make_args(
        key="encrypted_content", value=bad_rs, expect_count=1,
    ))
    text = session_file(home).read_text(encoding="utf-8")
    assert bad_rs not in text
    rows_after = reload_rows(session_file(home))
    assert len(rows_before) - len(rows_after) == 1


def test_remove_compaction_keeps_balance(home: Path):
    doctor_fix.cmd_remove_compaction(make_args(checkpoint=["cmp_bad1"]))
    rows = reload_rows(session_file(home))
    c, e = 0, 0
    for r in rows:
        if r.get("type") == "compacted":
            c += 1
        if (r.get("event_msg") or {}).get("type") == "context_compacted":
            e += 1
    assert c == 0 and e == 0


def test_scrub_cleans_everything(home: Path):
    doctor_fix.cmd_scrub(make_args(
        error_text=[GATEWAY_ERROR],
        orphan_turn=[GONE_TURN],
        dedupe_index=True,
    ))
    rows = reload_rows(session_file(home))
    found = [
        (r.get("payload") or {}).get("message")
        for r in rows if (r.get("payload") or {}).get("type") == "agent_message"
    ]
    assert not any(GATEWAY_ERROR in (m or "") for m in found)
    orphans = [
        r for r in rows
        if (r.get("payload") or {}).get("type") == "task_started"
        and (r.get("payload") or {}).get("turn_id") == GONE_TURN
    ]
    assert not orphans
    index = reload_rows(home / "session_index.jsonl")
    assert sum(1 for e in index if e.get("id") == SID) == 1
    assert backups(home)


def test_trim_cuts_history_at_anchor(home: Path):
    path = session_file(home)
    rows_before = reload_rows(path)
    anchor = "--- TRIM ANCHOR HERE ---"
    doctor_fix.cmd_trim(make_args(anchor_text=anchor))

    rows = reload_rows(path)
    user_msgs = [
        ((r.get("payload") or {}).get("input_text") or "")
        for r in rows
        if (r.get("payload") or {}).get("type") == "message"
        and (r.get("payload") or {}).get("role") == "user"
    ]
    assert user_msgs and user_msgs[0].strip() == anchor
    text = path.read_text(encoding="utf-8")
    assert "earlier history, to be trimmed" not in text
    assert "cmp_bad1" not in text
    assert len(rows) < len(rows_before)


def test_restore_round_trip(home: Path, tmp_path: Path):
    path = session_file(home)
    original = path.read_bytes()
    backup_dir = tmp_path / "bk"
    _, _ = doctor_fix.lib.save_snapshot(path, backup_dir, "test")
    # break the file, then restore it
    path.write_bytes(b"garbage\n")
    backup = next(backup_dir.glob("*.jsonl"))
    doctor_fix.cmd_restore(make_args(backup=str(backup), target=str(path)))
    assert path.read_bytes() == original


def test_normalize_is_idempotent(home: Path):
    args = make_args(model="target-model", effort="medium")
    doctor_fix.cmd_normalize(args)
    first = session_file(home).read_bytes()
    doctor_fix.cmd_normalize(args)
    second = session_file(home).read_bytes()
    assert first == second  # second run changes nothing byte-for-byte