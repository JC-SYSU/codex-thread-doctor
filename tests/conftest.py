"""Shared fixtures: a synthetic broken Codex session inside a fake CODEX_HOME.

The fixture mimics the machine-state fields the tools actually read:
session_meta opener, turn_context, thread_settings, task lifecycle events,
tool calls+outputs, compaction pairs, user messages.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

SID = "019f0000-0000-0000-0000-000000000001"
TURN_1 = "019f0000-0000-0000-0000-000000000101"
GONE_TURN = "019f0000-0000-0000-0000-000000000999"
GATEWAY_ERROR = "[gateway] unsupported input item (no role)"


def row(payload: dict, event: str | None = None, **extra) -> dict:
    r = {} if event is None else {"event_msg": {"type": event}}
    r["payload"] = payload
    r.update(extra)
    return r


def session_meta(sid: str = SID) -> dict:
    return row({"type": "session_meta", "session_id": sid, "cwd": "/work"})


def turn_context(turn_id: str, model: str, effort: str = "high",
                 collab_model: str | None = None) -> dict:
    cm = {
        "mode": "default",
        "settings": {"model": collab_model or model,
                     "reasoning_effort": effort},
    }
    return row(
        {"type": "turn_context", "session_id": SID, "turn_id": turn_id,
         "cwd": "/work", "model": model, "effort": effort,
         "collaboration_mode": cm}, event="turn_context")


def thread_settings(model: str, effort: str = "high") -> dict:
    return row(
        {"type": "thread_settings", "settings": {"model": model,
                                                 "reasoning_effort": effort}},
        event="thread_settings")


def user_message(text: str, turn_id: str = TURN_1, msg_id: str | None = None) -> dict:
    return row(
        {"type": "message", "role": "user", "turn_id": turn_id,
         "input_text": text, "message_id": msg_id or uuid.uuid4().hex})


def task_started(turn_id: str = TURN_1) -> dict:
    return row({"type": "task_started", "session_id": SID, "turn_id": turn_id,
                "task_id": f"t-{turn_id[-4:]}"})


def task_complete(turn_id: str = TURN_1) -> dict:
    return row({"type": "task_complete", "session_id": SID, "turn_id": turn_id,
                "task_id": f"t-{turn_id[-4:]}"})


_NO_ID = object()


def function_call(call_id: str | None, fn: str = "shell", args: str = "{}",
                  tool_call_id: str | None = None,
                  item_id=_NO_ID) -> dict:
    cid = call_id if call_id is not None else tool_call_id
    the_id = item_id if item_id is not _NO_ID else (tool_call_id or uuid.uuid4().hex)
    return row({"type": "function_call", "call_id": cid,
                "id": the_id, "name": fn, "arguments": args})


def function_call_output(call_id: str | None, output: str = "ok") -> dict:
    return row({"type": "function_call_output", "call_id": call_id,
                "output": output})


def compacted(checkpoint_id: str, ts: str, history: list[dict]) -> dict:
    return {
        "type": "compacted",
        "timestamp": ts,
        "replacement_history": history,
        "payload": {"type": "compacted", "next_checkpoint_id": checkpoint_id},
    }


def context_compacted(ts: str, checkpoint_id: str) -> dict:
    return row(
        {"type": "context_compacted", "timestamp": ts,
         "compaction": {"checkpoint_id": checkpoint_id}},
        event="context_compacted")


def reasoning_item(enc: str, rs_id: str) -> dict:
    return row({"type": "reasoning", "response_item": {"encrypted_content": enc,
                                                       "id": rs_id}})


def agent_message(text: str) -> dict:
    return row({"type": "agent_message", "message": text})


def encode_session(rows: list[dict], eol: str = "\n") -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + eol
                   for r in rows).encode("utf-8")


def write_session(home: Path, rows: list[dict], sid: str = SID,
                  date: str = "2026/01/01") -> Path:
    sub = home / "sessions" / date
    sub.mkdir(parents=True, exist_ok=True)
    path = sub / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
    path.write_bytes(encode_session(rows))
    return path


def bad_session_rows() -> list[dict]:
    """A session with every repairable symptom at once."""
    rs = "ocxr1:eyJzaWciOiJraXJvLXJzLXRoaW5raW5nLXNpZ25hdHVyZSJ9"
    return [
        session_meta(),
        turn_context("t0", "old-model-1", "high"),
        thread_settings("old-model-2"),
        user_message("earlier history, to be trimmed"),
        task_started(GONE_TURN),
        reasoning_item(rs, "rs_deadbeef1"),
        agent_message(GATEWAY_ERROR),
        function_call(None, tool_call_id="call_string_id_1"),     # empty -> id
        function_call(None),                                      # empty, no id -> generated
        function_call("dup_call"),                                # duplicates below
        function_call("dup_call"),                                # -> repaired
        function_call_output("dup_call"),                         # pairs first dup
        function_call_output(None),                               # -> next unmatched
        # NOTE: no task_complete for GONE_TURN on purpose — it is an orphan.
        user_message("--- TRIM ANCHOR HERE ---", turn_id="t0"),
        task_started(),
        turn_context("t0", "deprecated/old-model-3", "medium", collab_model="deprecated/old-model-3"),
        task_complete(),
        compacted("cmp_bad1", "2026-01-01T00:00:01Z",
                  [{"checkpoint_id": "cmp_bad1", "timestamp": "2026-01-01T00:00:01Z"}]),
        context_compacted("2026-01-01T00:00:01Z", "cmp_bad1"),
        task_started(),
        turn_context("t1", "old-model-4", "xhigh", collab_model="old-model-4"),
        task_complete(),
    ]


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A fake CODEX_HOME with one broken session file."""
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    (home / "archived_sessions").mkdir(parents=True)
    write_session(home, bad_session_rows())
    index = home / "session_index.jsonl"
    index.write_text(
        json.dumps({"id": SID, "thread_name": "x", "updated_at": "2026-01-01T00:00:00Z"})
        + "\n"
        + json.dumps({"id": SID, "thread_name": "x (2)", "updated_at": "2026-01-02T00:00:00Z"})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture
def bad_rows() -> list[dict]:
    return bad_session_rows()