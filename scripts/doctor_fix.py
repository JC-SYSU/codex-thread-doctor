#!/usr/bin/env python3
"""Codex Thread Doctor — session repair commands.

Every subcommand follows the same loop:

  1. locate the session JSONL by id (or --file) and preflight the session id
  2. backup_file() + SHA-256 snapshot
  3. modify in memory, keeping untouched lines at their original bytes
  4. atomic_write()
  5. re-read the file and assert the invariant each command promises

Usages:
  python doctor_fix.py normalize --session-id <id> --model <id> [--effort <level>]
  python doctor_fix.py call-ids --session-id <id>
  python doctor_fix.py remove-lines --session-id <id> --key encrypted_content --value <bad> [--expect-count N]
  python doctor_fix.py remove-compaction --session-id <id> --checkpoint <id>...
  python doctor_fix.py scrub --session-id <id> [--error-text <text>]... [--orphan-turn <id>]... [--dedupe-index]
  python doctor_fix.py trim --session-id <id> --anchor-text <text> [--continuation <file>]
  python doctor_fix.py restore --backup <file> --target <file>

Never runs ``codex``; never touches conversation text. Only machine state
fields and records are rewritten. On any assertion failure the tool exits 2
and tells you the backup path — restore it with ``restore``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import doctor_lib as lib
from doctor_lib import atomic_write, find_session_file

MODEL_KEYS = {"model"}
EFFORT_KEYS = {"effort", "reasoning_effort"}


# ------------------------------------------------------------------ helpers

def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


def split_parse(data: bytes, session_id: str | None = None):
    """Parse preserving per-line original bytes; blank lines are kept as-is.

    Returns (raw_lines, rows, kept) where kept[i] is the raw line index of rows[i].
    Malformed lines raise with their line number.
    """
    raw_lines = data.splitlines(keepends=True)
    rows: list[dict] = []
    kept: list[int] = []
    for i, raw in enumerate(raw_lines, 1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            rows.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {i}: {exc}") from exc
        kept.append(i - 1)
    if session_id is not None and rows:
        actual = lib.row_session_id(rows)
        if actual != session_id:
            raise ValueError(
                f"session id mismatch: expected {session_id}, file starts with {actual}"
            )
    return raw_lines, rows, kept


def render_output(
    raw_lines: list[bytes],
    rows: list[dict],
    kept: list[int],
    remove_raw: set[int] | None = None,
) -> bytes:
    """Re-encode only changed rows; every other kept line keeps its raw bytes;
    lines whose raw index is in ``remove_raw`` are dropped entirely."""
    remove_raw = remove_raw or set()
    out: list[bytes] = []
    j = 0
    for i, raw in enumerate(raw_lines):
        if j < len(kept) and kept[j] == i:
            encoded = json.dumps(rows[j], ensure_ascii=False, separators=(",", ":"))
            if raw.rstrip(b"\r\n").decode("utf-8", errors="strict") == encoded:
                out.append(raw)
            else:
                tail = b"\r\n" if raw.endswith(b"\r\n") else b"\n"
                out.append(encoded.encode("utf-8") + tail)
            j += 1
        else:
            if i in remove_raw:
                continue  # dropped row
            out.append(raw)  # blank line, untouched
    assert j == len(rows), "row/line alignment broken — kept list out of sync"
    return b"".join(out)


def filter_rows(
    rows: list[dict], kept: list[int], predicate
) -> tuple[list[dict], list[int], list[dict], set[int]]:
    """Split (rows, kept) by predicate. Returns (kept_rows, new_kept, removed,
    dropped_raw) where dropped_raw carries the raw line indexes of removed rows."""
    kept_rows, new_kept, removed = [], [], []
    dropped_raw: set[int] = set()
    for raw_idx, row in zip(kept, rows):
        if predicate(row):
            removed.append(row)
            dropped_raw.add(raw_idx)
        else:
            kept_rows.append(row)
            new_kept.append(raw_idx)
    return kept_rows, new_kept, removed, dropped_raw


def load_target(args) -> tuple[Path, list[bytes], list[dict], list[int]]:
    home = lib.codex_home()
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            die(f"--file not found: {path}")
    else:
        hits = find_session_file(home, args.session_id)
        if not hits:
            die(f"no session file found for {args.session_id} under {home}")
        if len(hits) > 1:
            print(
                f"warning: multiple files match {args.session_id} — using {hits[0]}:\n  "
                + "\n  ".join(str(p) for p in hits)
            )
        path = hits[0]
    data = path.read_bytes()
    if not data.strip():
        die(f"empty session file: {path}")
    return path, *split_parse(data, args.session_id)


def finish(
    args,
    path: Path,
    raw_lines: list[bytes],
    rows: list[dict],
    kept: list[int],
    tag: str,
    pre_sha: str,
    summary: list[str],
    checks: list[tuple[str, Callable[[list[dict]], bool]]],
    remove_raw: set[int] | None = None,
) -> None:
    """Atomic write + re-read from disk + assert. On failure restore the backup."""
    out = render_output(raw_lines, rows, kept, remove_raw)
    atomic_write(path, out)
    verified = split_parse(path.read_bytes())[1]
    for name, cond in checks:
        if not cond(verified):
            backup = Path(args.backup)
            if backup.is_file():
                lib.restore_backup(backup, path)
            die(f"post-write check failed: {name} (original restored from {backup})")
    post_sha = lib.sha256(path.read_bytes())
    print(f"session: {args.session_id}")
    print(f"op:      {tag}")
    print(f"file:    {path}")
    print(f"sha256:  {pre_sha} -> {post_sha}")
    for line in summary:
        print(line)
    print(f"backup:  {args.backup}")


def add_session_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session-id", required=True, help="Codex thread id (UUID)")
    parser.add_argument(
        "--file",
        help="session JSONL path; overrides id-based lookup (still preflights the id)",
    )
    parser.add_argument(
        "--backup-dir", default=None, help="backup directory (default: ~/.codex/.thread-doctor-backups)"
    )


# ------------------------------------------------------------------ commands

def cmd_normalize(args) -> None:
    path, raw_lines, rows, kept = load_target(args)
    backup_dir = Path(args.backup_dir or lib.default_backup_dir())
    args.backup, pre_sha = lib.save_snapshot(path, backup_dir, "normalize")

    keys = set(MODEL_KEYS)
    changed = lib.walk_replace(rows, keys, args.model)
    if args.effort is not None:
        changed += lib.walk_replace(rows, EFFORT_KEYS, args.effort)
    if changed == 0:
        print(f"no model/effort fields to rewrite in {path}")
        return

    def models_ok(r: list[dict]) -> bool:
        return set(lib.walk_collect(r, MODEL_KEYS)) == {args.model}

    def efforts_ok(r: list[dict]) -> bool:
        return args.effort is None or set(lib.walk_collect(r, EFFORT_KEYS)) == {args.effort}

    finish(
        args, path, raw_lines, rows, kept, "normalize", pre_sha,
        [f"model fields rewritten: {changed}",
         f"target model: {args.model}",
         f"target effort: {args.effort or '(unchanged)'}"],
        [("models unified", models_ok), ("efforts unified", efforts_ok)],
    )


def cmd_call_ids(args) -> None:
    path, raw_lines, rows, kept = load_target(args)
    backup_dir = Path(args.backup_dir or lib.default_backup_dir())
    args.backup, pre_sha = lib.save_snapshot(path, backup_dir, "call-ids")

    rows, stats = lib.repair_empty_call_ids(rows)

    def no_empty(r: list[dict]) -> bool:
        return count_empty_call_ids(r) == 0

    finish(
        args, path, raw_lines, rows, kept, "call-ids", pre_sha,
        [f"auto-assigned call ids: {stats['auto_assigned']}",
         f"calls seen: {stats['calls']}, outputs seen: {stats['outputs']}",
         f"unmatched outputs: {stats['unmatched_outputs']}"],
        [("no empty call ids", no_empty),
         ("no unmatched outputs", lambda r: unmatched_outputs(r) == 0)],
    )


def count_empty_call_ids(rows: list[dict]) -> int:
    n = 0
    for row in rows:
        payload = row.get("payload") or {}
        for t in lib.CALL_TYPES:
            if payload.get("type") == t and not payload.get("call_id"):
                n += 1
    return n


def unmatched_outputs(rows: list[dict]) -> int:
    """Outputs whose call_id references no matching call of the same type
    (two passes, so an output preceding its call is still matched)."""
    called: set[str] = set()
    for row in rows:
        payload = row.get("payload") or {}
        for t in lib.CALL_TYPES:
            if payload.get("type") == t and payload.get("call_id"):
                called.add(payload["call_id"])
    unmatched = 0
    for row in rows:
        payload = row.get("payload") or {}
        for out_t in lib.CALL_TYPES.values():
            if payload.get("type") == out_t:
                cid = payload.get("call_id")
                if not cid or cid not in called:
                    unmatched += 1
    return unmatched


def cmd_remove_lines(args) -> None:
    path, raw_lines, rows, kept = load_target(args)
    backup_dir = Path(args.backup_dir or lib.default_backup_dir())
    args.backup, pre_sha = lib.save_snapshot(path, backup_dir, "remove-lines")

    def match(row: dict) -> bool:
        return any(
            v == args.value for v in lib.walk_collect(row, {args.key})
        )

    kept_rows, new_kept, removed, dropped_raw = filter_rows(rows, kept, match)
    if args.expect_count is not None and len(removed) != args.expect_count:
        die(
            f"expected to remove {args.expect_count} rows, found {len(removed)} "
            f"(backup at {args.backup})"
        )
    if not removed:
        print(f"no rows matched {args.key}={args.value} in {path}")
        return

    finish(
        args, path, raw_lines, kept_rows, new_kept, "remove-lines", pre_sha,
        [f"removed rows: {len(removed)} (of {len(rows)})",
         f"matched {args.key}={args.value}"],
        [("no matching rows remain", lambda r: not r or not any(match(x) for x in r))],
        remove_raw=dropped_raw,
    )


def cmd_remove_compaction(args) -> None:
    path, raw_lines, rows, kept = load_target(args)
    backup_dir = Path(args.backup_dir or lib.default_backup_dir())
    args.backup, pre_sha = lib.save_snapshot(path, backup_dir, "remove-compaction")

    checkpoints = set(args.checkpoint)
    hit_timestamps: set[str] = set()

    def bad_compacted(row: dict) -> bool:
        if row.get("type") != "compacted":
            return False
        if args.checkpoint_time and row.get("timestamp") == args.checkpoint_time:
            hit_timestamps.add(row["timestamp"])
            return True
        history = row.get("replacement_history") or []
        ids = {
            e.get("checkpoint_id") or e.get("id")
            for e in history if isinstance(e, dict)
        }
        if ids & checkpoints:
            ts = row.get("timestamp")
            if ts:
                hit_timestamps.add(ts)
            return True
        return False

    dropped: set[int] = set()
    rows, kept, removed_compacted, first_dropped = filter_rows(rows, kept, bad_compacted)
    dropped |= first_dropped

    def paired_event(row: dict) -> bool:
        ev = row.get("event_msg") or {}
        if ev.get("type") != "context_compacted":
            return False
        # The timestamp may live on event_msg or payload depending on writer.
        ts = ev.get("timestamp") or (row.get("payload") or {}).get("timestamp")
        return ts in hit_timestamps

    rows, kept, removed_events, second_dropped = filter_rows(rows, kept, paired_event)
    dropped |= second_dropped

    if not removed_compacted:
        print(f"no compacted records matched {sorted(checkpoints)} in {path}")
        return

    def balanced(r: list[dict]) -> bool:
        c, e = lib.compaction_pair_counts(r)
        # A compacted record with no completion event is the normal shape;
        # only an event with no record (or an excess of events) is an anomaly.
        return c == e or e == 0

    def no_trace(r: list[dict]) -> bool:
        for row in r:
            if bad_compacted(row):
                return False
        return True

    finish(
        args, path, raw_lines, rows, kept, "remove-compaction", pre_sha,
        [f"removed compacted records: {len(removed_compacted)}",
         f"removed context_compacted events: {len(removed_events)}"],
        [("compaction pairs balanced", balanced), ("no matching checkpoints left", no_trace)],
        remove_raw=dropped,
    )


def cmd_scrub(args) -> None:
    path, raw_lines, rows, kept = load_target(args)
    backup_dir = Path(args.backup_dir or lib.default_backup_dir())
    args.backup, pre_sha = lib.save_snapshot(path, backup_dir, "scrub")

    error_texts = set(args.error_text)
    msg_fields = ("message", "text", "last_agent_message")
    scrubbed = 0
    for row in rows:
        payload = row.get("payload") or {}
        for key in msg_fields:
            v = payload.get(key)
            if isinstance(v, str) and v in error_texts:
                payload[key] = ""
                scrubbed += 1

    orphan_turns = set(args.orphan_turn)
    dropped: set[int] = set()
    if orphan_turns:
        rows, kept, removed_orphans, dropped = filter_rows(
            rows, kept,
            lambda r: r.get("payload", {}).get("type") == "task_started"
            and r.get("payload", {}).get("turn_id") in orphan_turns,
        )
    else:
        removed_orphans = []

    index_removed = 0
    if args.dedupe_index:
        index = lib.codex_home() / "session_index.jsonl"
        if index.is_file():
            blob = index.read_bytes()
            new_blob, index_removed = lib.dedupe_index(blob, args.session_id)
            if index_removed:
                atomic_write(index, new_blob)
                print(f"index:  deduplicated {index_removed} entry/ies in {index}")

    def clean(r: list[dict]) -> bool:
        return not scan_error_text(r, error_texts)

    finish(
        args, path, raw_lines, rows, kept, "scrub", pre_sha,
        [f"error text cleared: {scrubbed}",
         f"orphan task_started removed: {len(removed_orphans)}",
         f"index duplicates removed: {index_removed}"],
        [("no matching error text remains", clean)],
        remove_raw=dropped,
    )


def scan_error_text(rows: list[dict], texts: set[str]) -> list[tuple[str, str]]:
    hits = []
    for row in rows:
        payload = row.get("payload") or {}
        for key in ("message", "text", "last_agent_message"):
            v = payload.get(key)
            if isinstance(v, str) and v in texts:
                hits.append((key, v))
    return hits


def cmd_trim(args) -> None:
    path, raw_lines, rows, kept = load_target(args)
    backup_dir = Path(args.backup_dir or lib.default_backup_dir())
    args.backup, pre_sha = lib.save_snapshot(path, backup_dir, "trim")

    # Locate the anchor: the first user message whose input_text matches.
    anchor_text = args.anchor_text.strip()
    anchor_idx = None
    for i, row in enumerate(rows):
        payload = row.get("payload") or {}
        if payload.get("type") == "message" and payload.get("role") == "user":
            text = payload.get("input_text")
            if isinstance(text, str) and text.strip() == anchor_text:
                anchor_idx = i
                break
    if anchor_idx is None:
        die(f"anchor text not found among user messages in {path} (backup at {args.backup})")
    assert anchor_idx is not None  # for the type checker after die()

    anchor_turn = rows[anchor_idx].get("payload", {}).get("turn_id")
    removed_all: list[dict] = []
    dropped_raw: set[int] = set()
    kept_rows, new_kept = [], []
    for i, (raw_idx, row) in enumerate(zip(kept, rows)):
        payload = row.get("payload") or {}
        event = row.get("event_msg") or {}
        if lib.is_compaction(row):
            removed_all.append(row)
            dropped_raw.add(raw_idx)
            continue
        if i < anchor_idx:
            # Keep the session opener rows, plus records chained to the
            # anchor turn (its turn_context / lifecycle records); everything
            # else before the anchor is cut.
            keep = i < 2 or (
                anchor_turn is not None
                and (
                    payload.get("turn_id") == anchor_turn
                    or event.get("turn_id") == anchor_turn
                )
            )
            if not keep:
                removed_all.append(row)
                dropped_raw.add(raw_idx)
                continue
        kept_rows.append(row)
        new_kept.append(raw_idx)

    removed = len(rows) - len(kept_rows)

    cont_report = "no continuation file"
    if args.continuation:
        cont_path = Path(args.continuation)
        if not cont_path.is_file():
            die(f"--continuation not found: {cont_path}")
        cont_raw, cont_rows, cont_kept = split_parse(cont_path.read_bytes())
        cont_kept_rows, cont_new_kept, cont_removed = [], [], []
        for raw_idx, row in zip(cont_kept, cont_rows):
            if lib.is_compaction(row):
                cont_removed.append(row)
            else:
                cont_kept_rows.append(row)
                cont_new_kept.append(raw_idx)
        atomic_write(cont_path, render_output(cont_raw, cont_kept_rows, cont_new_kept))
        cont_report = (
            f"{cont_path} (removed {len(cont_removed)} compaction records)"
        )

    def first_user_is_anchor(r: list[dict]) -> bool:
        for row in r:
            payload = row.get("payload") or {}
            if payload.get("type") == "message" and payload.get("role") == "user":
                return isinstance(payload.get("input_text"), str) and (
                    payload["input_text"].strip() == anchor_text
                )
        return False

    def no_compaction(r: list[dict]) -> bool:
        c, e = lib.compaction_pair_counts(r)
        return c == 0 and e == 0

    finish(
        args, path, raw_lines, kept_rows, new_kept, "trim", pre_sha,
        [f"rows kept: {len(kept_rows)} (removed {removed} rows before the anchor)",
         f"continuation: {cont_report}"],
        [("anchor is first user message", first_user_is_anchor),
         ("no compaction records remain", no_compaction)],
        remove_raw=dropped_raw,
    )


def cmd_restore(args) -> None:
    backup = Path(args.backup)
    target = Path(args.target)
    if not backup.is_file():
        die(f"backup not found: {backup}")
    if not target.is_file():
        die(f"target not found: {target}")
    lib.restore_backup(backup, target)
    print(f"restored {target} from {backup}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="doctor_fix.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("normalize", help="unify model/effort fields across the session")
    add_session_args(p)
    p.add_argument("--model", required=True,
                   help="target model id — ask the user which model their account serves")
    p.add_argument("--effort", help="target effort, e.g. high, medium, xhigh")
    p.set_defaults(func=cmd_normalize)

    p = sub.add_parser("call-ids", help="pair tool calls with outputs, repairing empty call ids")
    add_session_args(p)
    p.set_defaults(func=cmd_call_ids)

    p = sub.add_parser("remove-lines", help="drop rows containing a key/value deep inside")
    add_session_args(p)
    p.add_argument("--key", required=True, help="field name to match at any depth")
    p.add_argument("--value", required=True, help="exact value to match")
    p.add_argument("--expect-count", type=int, help="assert the exact number of rows removed")
    p.set_defaults(func=cmd_remove_lines)

    p = sub.add_parser("remove-compaction", help="drop corrupted compaction checkpoints and their events")
    add_session_args(p)
    p.add_argument("--checkpoint", action="append", required=True,
                   help="checkpoint id to remove (repeatable)")
    p.add_argument("--checkpoint-time", help="fallback: match the compacted record by timestamp")
    p.set_defaults(func=cmd_remove_compaction)

    p = sub.add_parser("scrub", help="clear error text, drop orphaned task_started, dedupe index")
    add_session_args(p)
    p.add_argument("--error-text", action="append", default=[],
                   help="exact text to blank out of message-bearing fields (repeatable)")
    p.add_argument("--orphan-turn", action="append", default=[],
                   help="turn id whose orphaned task_started should be dropped (repeatable)")
    p.add_argument("--dedupe-index", action="store_true",
                   help="remove duplicate session_index.jsonl entries for this session")
    p.set_defaults(func=cmd_scrub)

    p = sub.add_parser("trim", help="cut history before an anchor user message")
    add_session_args(p)
    p.add_argument("--anchor-text", required=True, help="exact user message text to keep from")
    p.add_argument("--continuation",
                   help="second physical file of the same thread; only compaction records are stripped there")
    p.set_defaults(func=cmd_trim)

    p = sub.add_parser("restore", help="restore a backup over a target file")
    p.add_argument("--backup", required=True)
    p.add_argument("--target", required=True)
    p.set_defaults(func=cmd_restore)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()