#!/usr/bin/env python3
"""Codex Thread Doctor — shared library.

Zero third-party dependencies, Python 3.11+. Works on macOS / Linux / Windows.

Every write path in this toolkit follows the same discipline:
  1. backup_file()  -> a byte-identical copy plus its SHA-256, written and read back
  2. modify rows     -> keep untouched lines at their original bytes
  3. atomic_write()  -> same-directory temp file + fsync + os.replace
  4. re-read and assert -> the invariant the tool promises actually holds
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# A session file is named rollout-2026-01-02T03-04-05-<session-id>.jsonl
SESSION_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
# Tool-call item types that carry a call_id, with their output counterparts.
CALL_TYPES = {
    "function_call": "function_call_output",
    "custom_tool_call": "custom_tool_call_output",
    "tool_search_call": "tool_search_output",
}
DEFAULT_BACKUP_SUBDIR = ".thread-doctor-backups"


# ---------------------------------------------------------------- primitives

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_stamp() -> str:
    """UTC timestamp safe for filenames, e.g. 20260916T143000Z."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def codex_home() -> Path:
    """Resolve the Codex config directory: $CODEX_HOME or ~/.codex."""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


def parse_jsonl(data: bytes, session_id: str | None = None) -> list[dict]:
    """Parse JSONL lines; report the failing line number instead of a bare traceback."""
    rows: list[dict] = []
    for ln, raw in enumerate(data.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {ln}: {exc}") from exc
        rows.append(row)
    if session_id is not None:
        actual = row_session_id(rows)
        if actual != session_id:
            raise ValueError(
                f"session id mismatch: expected {session_id}, file starts with {actual}"
            )
    return rows


def row_session_id(rows: list[dict]) -> str | None:
    """Session id from the first line's payload (the session metadata opener)."""
    first = rows[0]
    payload = first.get("payload") or {}
    sid = payload.get("session_id") or first.get("session_id")
    if not sid:
        # Some archives carry it on the top-level thread id instead.
        sid = first.get("thread_id")
    return sid


def encode_rows(rows: list[dict]) -> bytes:
    out = []
    for row in rows:
        out.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(out) + "\n").encode("utf-8")


def atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    """Write via same-directory temp file, fsync, chmod, then os.replace.

    Atomic on both POSIX and Windows (os.replace works on NTFS); the temp file
    is cleaned up on any failure so no partial outputs survive.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is None and path.exists():
            mode = stat.S_IMODE(path.stat().st_mode)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ backups

def default_backup_dir() -> Path:
    return codex_home() / DEFAULT_BACKUP_SUBDIR


def backup_file(path: Path, backup_dir: Path | str, tag: str) -> Path:
    """Copy ``path`` to ``backup_dir`` with a timestamped name, verify by hash.

    Returns the backup path. The copy is verified by reading it back and
    comparing SHA-256, so a silent partial write can never pass as a backup.
    """
    path = Path(path)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{path.name}.before-{tag}-{utc_stamp()}.jsonl"
    data = path.read_bytes()
    backup.write_bytes(data)
    if sha256(backup.read_bytes()) != sha256(data):
        raise OSError(f"backup verification failed: {backup}")
    return backup


def restore_backup(backup: Path, target: Path) -> None:
    """Restore ``backup`` over ``target``, written atomically."""
    data = backup.read_bytes()
    atomic_write(target, data)


def save_snapshot(path: Path, backup_dir: Path | str, tag: str) -> tuple[Path, str]:
    """Backup a file, return (backup_path, pre_change_sha256)."""
    path = Path(path)
    backup = backup_file(path, backup_dir, tag)
    return backup, sha256(path.read_bytes())


# ------------------------------------------------------------- walk helpers

SKIP_KEYS = {"arguments", "input", "input_text", "message"}


def walk_replace(value, keys: set[str], new, skip_keys: set[str] | None = None):
    """Recursively replace string values under any of ``keys``.

    Branches named in ``skip_keys`` (tool arguments, message bodies) are never
    entered, so free-form text can never be rewritten by accident; only machine
    state fields like ``model`` / ``effort`` get touched.

    Returns the number of replaced values.
    """
    if skip_keys is None:
        skip_keys = SKIP_KEYS
    count = 0
    if isinstance(value, dict):
        for k, v in value.items():
            if k in skip_keys:
                continue
            if k in keys and isinstance(v, str):
                if v != new:
                    value[k] = new
                    count += 1
            else:
                count += walk_replace(v, keys, new, skip_keys)
    elif isinstance(value, list):
        for item in value:
            count += walk_replace(item, keys, new, skip_keys)
    return count


def walk_collect(value, keys: set[str], skip_keys: set[str] | None = None) -> list:
    """Collect (path, value) pairs for values under any of ``keys``; used for
    post-change assertions like ``set(remaining) == expected``."""
    if skip_keys is None:
        skip_keys = SKIP_KEYS
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k in skip_keys:
                continue
            if k in keys and isinstance(v, str):
                found.append(v)
            else:
                found.extend(walk_collect(v, keys, skip_keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(walk_collect(item, keys, skip_keys))
    return found


# -------------------------------------------------------------- call id fix

def repair_empty_call_ids(
    rows: list[dict],
) -> tuple[list[dict], dict[str, int]]:
    """Pair tool calls with their outputs by call_id, repairing empty ids.

    Rules (learned from real broken archives):
      * a call with an empty call_id takes its own ``id``;
      * a call whose id duplicates an earlier call of the same type gets a
        generated id ``call_repaired_<type>_<line>`` so ids stay unique;
      * an output with an empty call_id takes the next unmatched call id of
        the matching type (FIFO);
      * an output that cannot be matched is left untouched and reported in the
        return value's stats — the invariant to assert afterwards is
        ``stats["unmatched_outputs"] == 0``.
    """
    stats = {"calls": 0, "outputs": 0, "auto_assigned": 0, "unmatched_outputs": 0}
    seen: dict[str, set[str]] = {t: set() for t in CALL_TYPES}
    pending: dict[str, list[str]] = {t: [] for t in CALL_TYPES}

    for ln, row in enumerate(rows, 1):
        payload = row.get("payload") or {}
        rtype = payload.get("type")
        for t, out_t in CALL_TYPES.items():
            if rtype == t:
                stats["calls"] += 1
                cid = payload.get("call_id")
                if cid:
                    if cid in seen[t]:
                        cid = f"call_repaired_{t}_{ln}"
                        payload["call_id"] = cid
                        stats["auto_assigned"] += 1
                    seen[t].add(cid)
                else:
                    cid = payload.get("id") or None
                    if not cid:
                        cid = f"call_repaired_{t}_{ln}"
                    payload["call_id"] = cid
                    seen[t].add(cid)
                    stats["auto_assigned"] += 1
                pending[t].append(cid)
            elif rtype == out_t:
                stats["outputs"] += 1
                cid = payload.get("call_id")
                if cid:
                    # Cross-validate the referent exists; mismatch stays for
                    # the caller to judge, only empty ids are auto-repaired.
                    if cid in seen[t]:
                        try:
                            pending[t].remove(cid)
                        except ValueError:
                            pass
                else:
                    if pending[t]:
                        payload["call_id"] = pending[t].pop(0)
                        stats["auto_assigned"] += 1
                    else:
                        stats["unmatched_outputs"] += 1
    return rows, stats


# --------------------------------------------------------------- compaction

def is_compaction(row: dict) -> bool:
    return (
        row.get("type") == "compacted"
        or (row.get("event_msg", {}).get("type") == "context_compacted")
        or ("compaction.summary" in json.dumps(row))
    )


def compaction_pair_counts(rows: list[dict]) -> tuple[int, int]:
    compacted = sum(1 for r in rows if r.get("type") == "compacted")
    events = sum(
        1 for r in rows if (r.get("event_msg") or {}).get("type") == "context_compacted"
    )
    return compacted, events


# ------------------------------------------------------------------- misc

def drop_rows(rows: list[dict], predicate) -> tuple[list[dict], list[dict]]:
    """Split rows into (kept, removed) by ``predicate``."""
    kept, removed = [], []
    for row in rows:
        (kept if not predicate(row) else removed).append(row)
    return kept, removed


def dedupe_index(blob: bytes, session_id: str) -> tuple[bytes, int]:
    """Remove duplicate session_index.jsonl entries for one session id, keeping
    the NEWEST occurrence; lines that don't parse are preserved byte-for-byte."""
    lines = blob.splitlines(keepends=True)
    kept, removed = [], 0
    seen = False
    for line in reversed(lines):
        if not line.strip():
            kept.append(line)
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if entry.get("id") == session_id:
            if seen:
                removed += 1
                continue
            seen = True
        kept.append(line)
    kept.reverse()
    return b"".join(kept), removed


def find_codex_bin() -> str:
    """Locate the ``codex`` CLI: $CODEX_BIN, then PATH.

    macOS users whose Codex lives inside the ChatGPT.app bundle can export
    CODEX_BIN (or add the bundle's Resources dir to PATH); nothing here is
    hard-coded to a machine layout.
    """
    env_bin = os.environ.get("CODEX_BIN")
    if env_bin:
        candidate = Path(env_bin).expanduser()
        if candidate.is_file():
            return str(candidate)
        raise FileNotFoundError(
            f"CODEX_BIN is set but not a file: {candidate}"
        )
    found = shutil.which("codex")
    if found:
        return found
    raise FileNotFoundError(
        "codex CLI not found. Install Codex, set CODEX_BIN to its binary, "
        "or add it to PATH."
    )


def run_codex(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> dict:
    """Run ``codex`` and return {rc, output, timed_out}.

    ``output`` is stdout+stderr merged with lossless decoding; timed-out runs
    are reaped and reported, never left running.
    """
    try:
        proc = subprocess.run(
            [find_codex_bin(), "exec", "resume", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd) if cwd else None,
            env=env,
            timeout=timeout,
        )
        return {"rc": proc.returncode, "output": proc.stdout + proc.stderr, "timed_out": False}
    except subprocess.TimeoutExpired as exc:
        return {
            "rc": 124,
            "output": (exc.stdout or b"").decode("utf-8", errors="replace")
            + (exc.stderr or b"").decode("utf-8", errors="replace"),
            "timed_out": True,
        }


def find_session_file(home: Path, session_id: str) -> list[Path]:
    """Locate a session file by id under sessions/ and archived_sessions/
    (walking YYYY/MM/DD). Returns all hits, active first — multiple hits mean
    the archive duplicates an active file and need human judgment."""
    hits: list[Path] = []
    for root in ("sessions", "archived_sessions"):
        for p in (home / root).rglob("*.jsonl"):
            if SESSION_ID_RE.search(p.name) and session_id in p.name:
                hits.append(p)
                continue
            # Fall back to reading the opener for id the filename doesn't show.
            try:
                first = p.read_bytes().splitlines()[0]
                for hit in [json.loads(first)]:
                    pid = (hit.get("payload") or {}).get("session_id")
                    if pid == session_id:
                        hits.append(p)
            except (OSError, IndexError, json.JSONDecodeError):
                pass
    # De-duplicate and report archived entries after active ones.
    return sorted(
        set(hits), key=lambda p: ("archived_sessions" in p.parts, str(p))
    )