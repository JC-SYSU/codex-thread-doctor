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
  python doctor_fix.py drop-unanswered-calls --session-id <id> [--expect-count N]
  python doctor_fix.py scrub --session-id <id> [--error-text <text>]... [--clear-error-texts] [--orphan-turn <id>]... [--dedupe-index]
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
from typing import Callable, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))

import doctor_lib as lib
from doctor_lib import atomic_write, find_session_file

MODEL_KEYS = {"model"}
EFFORT_KEYS = {"effort", "reasoning_effort"}
# Row types that may legitimately precede the first user message and survive
# a trim: legacy event types plus the current top-level record types.
PRE_ANCHOR_TYPES = {
    "session_meta",
    "turn_context",
    "thread_settings",
    "thread_settings_applied",
    "world_state",
    "token_usage_record",
}


# ------------------------------------------------------------------ helpers

def die(msg: str) -> NoReturn:
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
        if not isinstance(rows[-1], dict):
            raise ValueError(
                f"line {i}: expected a JSON object, got "
                f"{type(rows[-1]).__name__}"
            )
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
    try:
        return _load_target(args)
    except (ValueError, AttributeError) as exc:
        die(f"{exc}")


def _load_target(args) -> tuple[Path, list[bytes], list[dict], list[int]]:
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
            for bk, tgt in (getattr(args, "extra_restores", None) or []):
                if Path(bk).is_file():
                    lib.restore_backup(Path(bk), Path(tgt))
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
        "--backup-dir", default=None,
        help="backup directory (default: $CODEX_HOME/.thread-doctor-backups)",
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

    def history_ids(row: dict) -> set[str]:
        # Current rollouts nest the history under payload; legacy ones put it
        # at the top level (or inside replacement_history_metadata).
        candidates = []
        payload = row.get("payload")
        if isinstance(payload, dict):
            candidates.append(payload.get("replacement_history"))
            meta = payload.get("replacement_history_metadata")
            if isinstance(meta, list):
                candidates.append(meta)
        candidates.append(row.get("replacement_history"))
        ids: set[str] = set()
        for history in candidates:
            if not isinstance(history, list):
                continue
            for e in history:
                if not isinstance(e, dict):
                    continue
                for key in ("checkpoint_id", "id", "window_id", "previous_window_id"):
                    v = e.get(key)
                    if isinstance(v, str) and v:
                        ids.add(v)
        return ids

    def ts_matches(row: dict) -> bool:
        if not args.checkpoint_time:
            return False
        return (
            row.get("timestamp") == args.checkpoint_time
            or (row.get("event_msg") or {}).get("timestamp") == args.checkpoint_time
            or (row.get("payload") or {}).get("timestamp") == args.checkpoint_time
        )

    def event_cp(row: dict):
        """Checkpoint id a context_compacted event reports, anywhere a writer
        may have put it."""
        ev = row.get("event_msg") or {}
        cp = (ev.get("compaction") or {}).get("checkpoint_id")
        if cp is None:
            cp = ((row.get("payload") or {}).get("compaction") or {}).get("checkpoint_id")
        return cp

    def bad_compacted(row: dict) -> bool:
        if row.get("type") != "compacted":
            return False
        return bool(history_ids(row) & checkpoints) or ts_matches(row)

    drop: set[int] = set()
    rows, kept, removed_compacted, first_dropped = filter_rows(rows, kept, bad_compacted)
    drop |= first_dropped

    def paired_event(row: dict) -> bool:
        if lib.event_type(row) != "context_compacted":
            return False
        # Primary key: the checkpoint id the event reports. Timestamp is the
        # fallback for writers that don't carry the id.
        return event_cp(row) in checkpoints or ts_matches(row)

    rows, kept, removed_events, second_dropped = filter_rows(rows, kept, paired_event)
    drop |= second_dropped

    if not removed_compacted:
        print(f"no compacted records matched {sorted(checkpoints)} in {path}")
        return

    def no_residual(r: list[dict]) -> bool:
        for row in r:
            ev = row.get("event_msg") or {}
            if row.get("type") == "compacted" and (history_ids(row) & checkpoints):
                return False
            if (
                ev.get("type") == "context_compacted"
                and (event_cp(row) is not None and event_cp(row) in checkpoints
                     or ts_matches(row))
            ):
                return False
        return True

    finish(
        args, path, raw_lines, rows, kept, "remove-compaction", pre_sha,
        [f"removed compacted records: {len(removed_compacted)}",
         f"removed context_compacted events: {len(removed_events)}"],
        [("no matching checkpoints or events remain", no_residual)],
        remove_raw=drop,
    )


def cmd_drop_unanswered_calls(args) -> None:
    path, raw_lines, rows, kept = load_target(args)
    backup_dir = Path(args.backup_dir or lib.default_backup_dir())
    args.backup, pre_sha = lib.save_snapshot(path, backup_dir, "drop-unanswered-calls")

    missing = lib.unanswered_call_ids(rows)
    if args.expect_count is not None and len(missing) != args.expect_count:
        die(
            f"expected {args.expect_count} unanswered calls, found {len(missing)} "
            f"(backup at {args.backup})"
        )
    if not missing:
        print(f"no unanswered calls in {path}")
        return

    def is_unanswered_call(row: dict) -> bool:
        payload = lib.state_payload(row)
        if payload.get("type") not in lib.CALL_TYPES:
            return False
        cid = payload.get("call_id") or payload.get("id")
        return isinstance(cid, str) and cid in missing

    rows, kept, removed, dropped = filter_rows(rows, kept, is_unanswered_call)
    finish(
        args, path, raw_lines, rows, kept, "drop-unanswered-calls", pre_sha,
        [f"removed unanswered tool calls: {len(removed)}",
         f"removed ids: {', '.join(sorted(missing))}"],
        [("no unanswered calls remain", lambda r: not lib.unanswered_call_ids(r))],
        remove_raw=dropped,
    )


def cmd_scrub(args) -> None:
    path, raw_lines, rows, kept = load_target(args)
    backup_dir = Path(args.backup_dir or lib.default_backup_dir())
    args.backup, pre_sha = lib.save_snapshot(path, backup_dir, "scrub")

    error_texts = set(args.error_text)
    scrubbed = 0
    # Legacy display fields plus the current content-block shape. Only
    # message-bearing fields are touched; tool arguments and user/assistant
    # prose stay byte-identical.
    display_fields = ("message", "text", "last_agent_message")
    for row in rows:
        payload = row.get("payload") or {}
        for key in display_fields:
            v = payload.get(key)
            if isinstance(v, str) and v in error_texts:
                payload[key] = ""
                scrubbed += 1
        content = payload.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    v = block.get("text")
                    if isinstance(v, str) and v in error_texts:
                        block["text"] = ""
                        scrubbed += 1

    # Opt-in: clear terminal error messages that match, keeping the error
    # object (and its status) intact.
    errors_cleared = 0
    if args.clear_error_texts:
        for row in rows:
            et = lib.event_of(row)
            err = et.get("error")
            if isinstance(err, dict):
                msg = err.get("message")
                if isinstance(msg, str) and msg in error_texts:
                    err["message"] = ""
                    errors_cleared += 1

    orphan_turns = set(args.orphan_turn)
    dropped: set[int] = set()
    if orphan_turns:
        def is_orphan_start(row: dict) -> bool:
            payload = row.get("payload") or {}
            if payload.get("type") in lib.LIFECYCLE_START:
                turn = payload.get("turn_id") or payload.get("task_id")
            elif lib.event_type(row) in lib.LIFECYCLE_START:
                event = lib.event_of(row)
                turn = event.get("turn_id") or event.get("task_id")
            else:
                return False
            return turn in orphan_turns

        rows, kept, removed_orphans, dropped = filter_rows(rows, kept, is_orphan_start)
    else:
        removed_orphans = []

    index_removed = 0
    if args.dedupe_index:
        index = lib.codex_home() / "session_index.jsonl"
        if index.is_file():
            blob = index.read_bytes()
            new_blob, index_removed = lib.dedupe_index(blob, args.session_id)
            if index_removed:
                index_backup, _ = lib.save_snapshot(index, backup_dir, "scrub-index")
                args.extra_restores = (getattr(args, "extra_restores", None) or []) + [
                    (str(index_backup), str(index))
                ]
                atomic_write(index, new_blob)
                print(f"index:  deduplicated {index_removed} entry/ies in {index}")

    def clean(r: list[dict]) -> bool:
        return not scan_error_text(r, error_texts, args.clear_error_texts)

    finish(
        args, path, raw_lines, rows, kept, "scrub", pre_sha,
        [f"error text cleared: {scrubbed}",
         f"terminal error messages cleared: {errors_cleared}",
         f"orphan start events removed: {len(removed_orphans)}",
         f"index duplicates removed: {index_removed}"],
        [("no matching error text remains", clean)],
        remove_raw=dropped,
    )


def scan_error_text(rows: list[dict], texts: set[str],
                    include_errors: bool = False) -> list[tuple[str, str]]:
    hits = []
    for row in rows:
        payload = row.get("payload") or {}
        for key in ("message", "text", "last_agent_message"):
            v = payload.get(key)
            if isinstance(v, str) and v in texts:
                hits.append((key, v))
        content = payload.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    v = block.get("text")
                    if isinstance(v, str) and v in texts:
                        hits.append(("content.text", v))
        if include_errors:
            err = lib.event_of(row).get("error")
            if isinstance(err, dict):
                v = err.get("message")
                if isinstance(v, str) and v in texts:
                    hits.append(("error.message", v))
    return hits


def cmd_trim(args) -> None:
    path, raw_lines, rows, kept = load_target(args)
    backup_dir = Path(args.backup_dir or lib.default_backup_dir())
    args.backup, pre_sha = lib.save_snapshot(path, backup_dir, "trim")

    # Locate the anchor among user messages in any persisted shape.
    anchor_text = args.anchor_text.strip()
    anchor_idx = None
    for i, text in lib.user_messages(rows):
        if text.strip() == anchor_text:
            anchor_idx = i
            break
    if anchor_idx is None:
        die(f"anchor text not found among user messages in {path} (backup at {args.backup})")
    assert anchor_idx is not None  # for the type checker after die()

    anchor_row = rows[anchor_idx]
    anchor_turn = (
        (anchor_row.get("payload") or {}).get("turn_id")
        or lib.event_of(anchor_row).get("turn_id")
    )
    removed_all: list[dict] = []
    dropped_raw: set[int] = set()
    kept_rows, new_kept = [], []
    for i, (raw_idx, row) in enumerate(zip(kept, rows)):
        payload = row.get("payload") or {}
        event = lib.event_of(row)
        if lib.is_compaction(row):
            removed_all.append(row)
            dropped_raw.add(raw_idx)
            continue
        if i < anchor_idx:
            # Keep opener / metadata rows by type (not by position — the
            # second line may well be a user message) plus records chained to
            # the anchor turn; everything else before the anchor is cut.
            keep = (
                payload.get("type") in PRE_ANCHOR_TYPES
                or row.get("type") in PRE_ANCHOR_TYPES
                or (
                    anchor_turn is not None
                    and (
                        payload.get("turn_id") == anchor_turn
                        or event.get("turn_id") == anchor_turn
                    )
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
        if cont_removed:
            cont_backup, _ = lib.save_snapshot(cont_path, backup_dir, "trim-continuation")
            args.extra_restores = (getattr(args, "extra_restores", None) or []) + [
                (str(cont_backup), str(cont_path))
            ]
        atomic_write(cont_path, render_output(cont_raw, cont_kept_rows, cont_new_kept))
        cont_report = (
            f"{cont_path} (removed {len(cont_removed)} compaction records)"
        )

    def first_user_is_anchor(r: list[dict]) -> bool:
        for _, text in lib.user_messages(r):
            return text.strip() == anchor_text
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

    p = sub.add_parser(
        "drop-unanswered-calls",
        help="remove tool calls whose output never arrived (stream died mid-call)",
    )
    add_session_args(p)
    p.add_argument("--expect-count", type=int,
                   help="assert the exact number of unanswered calls removed")
    p.set_defaults(func=cmd_drop_unanswered_calls)

    p = sub.add_parser("scrub", help="clear error text, drop orphaned task_started, dedupe index")
    add_session_args(p)
    p.add_argument("--error-text", action="append", default=[],
                   help="exact text to blank out of message-bearing fields (repeatable)")
    p.add_argument("--clear-error-texts", action="store_true",
                   help="also clear matching messages inside terminal error objects "
                        "(the error status itself is kept)")
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