"""Unit tests for doctor_lib primitives."""
from __future__ import annotations

import json
from pathlib import Path

import doctor_lib as lib

from conftest import (
    SID,
    compacted,
    context_compacted,
    encode_session,
    function_call,
    function_call_output,
    session_meta,
    turn_context,
)


def test_atomic_write_roundtrip(tmp_path: Path):
    p = tmp_path / "out.jsonl"
    lib.atomic_write(p, b"hello\n")
    assert p.read_bytes() == b"hello\n"
    lib.atomic_write(p, b"world\n")
    assert p.read_bytes() == b"world\n"
    assert not list(tmp_path.glob("*.tmp"))  # no temp litter


def test_backup_verifies_by_hash(tmp_path: Path):
    src = tmp_path / "s.jsonl"
    src.write_bytes(b"the bytes\n")
    bk = lib.backup_file(src, tmp_path / "bk", "normalize")
    assert bk.name.startswith("s.jsonl.before-normalize-")
    assert bk.read_bytes() == b"the bytes\n"
    assert lib.sha256(bk.read_bytes()) == lib.sha256(b"the bytes\n")


def test_restore_backup(tmp_path: Path):
    src = tmp_path / "s.jsonl"
    src.write_bytes(b"broken\n")
    bk = tmp_path / "bk.jsonl"
    bk.write_bytes(b"original\n")
    lib.restore_backup(bk, src)
    assert src.read_bytes() == b"original\n"


def test_codex_home_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert lib.codex_home() == tmp_path


def test_walk_replace_respects_skip_keys():
    row = {
        "payload": {
            "model": "old-a",                       # replaced
            "collaboration_mode": {"settings": {"model": "old-b", "reasoning_effort": "high"}},
            "arguments": '{"model": "keep-me"}',    # string, never scanned
            "input": {"model": "keep-me-too"},      # dict under skipped key
        }
    }
    rows = [row]
    n = lib.walk_replace(rows, {"model"}, "new")
    assert n == 2
    assert row["payload"]["model"] == "new"
    assert row["payload"]["collaboration_mode"]["settings"]["model"] == "new"
    assert row["payload"]["arguments"] == '{"model": "keep-me"}'
    assert row["payload"]["input"]["model"] == "keep-me-too"


def test_walk_collect_finds_only_targets():
    rows = [
        {"payload": {"model": "a", "nested": [{"model": "b"}], "x": {"y": 1}}},
        {"payload": {"other": "z"}},
    ]
    assert lib.walk_collect(rows, {"model"}) == ["a", "b"]


def test_repair_empty_call_ids():
    rows = [
        function_call("dup", tool_call_id="id-a"),
        function_call("dup", tool_call_id="id-b"),   # duplicate -> repaired
        function_call(None, tool_call_id="id-c"),    # empty -> takes id
        function_call(None, item_id=None),          # empty, no id -> generated
        function_call_output("dup"),                 # pairs the first dup
        function_call_output(None),                  # -> pending fifo gives "id-c"
        function_call_output(None),                  # -> generated id
    ]
    rows, stats = lib.repair_empty_call_ids(rows)
    assert stats["auto_assigned"] == 4  # 3 call-side + 1 output-side fifo backfill
    cids = [r["payload"]["call_id"] for r in rows]
    assert cids[0] == "dup" and cids[1] != "dup"
    assert cids[1].startswith("call_repaired_")
    assert cids[2] == "id-c"
    assert cids[3].startswith("call_repaired_")
    assert cids[4] == "dup"
    # FIFO: dup was consumed by the first output; the next pending call is
    # the repaired duplicate, then id-c, then the generated id.
    assert cids[5] == "call_repaired_function_call_2"
    assert cids[6] == "id-c"
    assert stats["unmatched_outputs"] == 0


def test_repair_empty_call_ids_reports_unmatched():
    rows = [
        function_call_output(None),   # no pending call at all
    ]
    _, stats = lib.repair_empty_call_ids(rows)
    assert stats["unmatched_outputs"] == 1


def test_compaction_pair_counts():
    rows = [
        compacted("c1", "t1", [{"checkpoint_id": "c1"}]),
        context_compacted("t1", "c1"),
        session_meta(),
    ]
    c, e = lib.compaction_pair_counts(rows)
    assert (c, e) == (1, 1)
    assert lib.is_compaction(rows[0]) and lib.is_compaction(rows[1])
    assert not lib.is_compaction(rows[2])


def test_dedupe_index():
    blob = (
        json.dumps({"id": SID, "updated_at": "a"}) + "\n"
        + "not-json-line\n"
        + json.dumps({"id": SID, "updated_at": "b"}) + "\n"
        + json.dumps({"id": "other", "updated_at": "c"}) + "\n"
    ).encode()
    new_blob, removed = lib.dedupe_index(blob, SID)
    assert removed == 1
    decoded = new_blob.decode()
    assert decoded.count(f'"id": "{SID}"') == 1
    assert "not-json-line" in decoded
    assert "updated_at\": \"b" in decoded  # newest kept


def test_parse_jsonl_line_number_error():
    blob = b'{"a": 1}\n{broken\n{"a": 2}\n'
    try:
        lib.parse_jsonl(blob)
    except ValueError as exc:
        assert "line 2" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_find_session_file_archived_and_active(tmp_path: Path):
    home = tmp_path
    (home / "sessions" / "2026" / "01" / "01").mkdir(parents=True)
    (home / "archived_sessions").mkdir()
    active = home / "sessions" / "2026" / "01" / "01" / f"rollout-x-{SID}.jsonl"
    active.write_bytes(encode_session([session_meta(), turn_context("t", "m")]))
    archived = home / "archived_sessions" / f"rollout-x-{SID}.jsonl"
    archived.write_bytes(active.read_bytes())
    hits = lib.find_session_file(home, SID)
    assert len(hits) == 2
    assert hits[0] == active  # active first