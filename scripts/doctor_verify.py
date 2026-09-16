#!/usr/bin/env python3
"""Codex Thread Doctor — resume verification.

Answers the question "can this thread continue?" by actually resuming it with
the Codex CLI and checking the outcome, not by guessing from static fields.

Two modes:

  --shadow (default)  Zero-touch. The thread file (plus auth and config) is
                      copied into a temporary CODEX_HOME; the original file's
                      SHA-256 is checked before and after, and `codex resume`
                      never sees the real directory at all.

  --live              Runs directly against the real thread. Use only when you
                      want the outcome persisted (e.g. a real auto-compaction
                      written back into the session). Note: even with
                      `--ephemeral`, Codex appends lifecycle events to the
                      original JSONL — the file WILL change; `--live` does not
                      guard hashes, only reports them.

The verdict is mechanical: exit code 0 + the marker in the final output + no
"not supported" anywhere + no timeout. Everything else is reported as evidence
in the JSON summary, never as a guess.

Usage:
  python doctor_verify.py --session-id <id> [--shadow|--live] [--marker TEXT]
                          [--force-compact-window N] [--timeout 300]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import doctor_lib as lib

DEFAULT_PROMPT = "Do not call tools. Reply with exactly: THREAD_RESUME_OK"
DEFAULT_MARKER = "THREAD_RESUME_OK"
SHADOW_COPY_FILES = (
    "auth.json",
    "config.toml",
    "models_cache.json",
    "version.json",
    "installation_id",
    "model-instructions.md",
    "AGENTS.md",
)
ERROR_SIGNATURES = (
    ("model_blocked", "is not supported"),
    ("encrypted_content", "invalid_encrypted_content"),
    ("encrypted_content", "could not be decrypted"),
    ("encrypted_content", "could not be verified"),
    ("compaction_hint", "compaction failed"),
    ("compaction_hint", "invalid paginated history"),
    ("http_400", "400"),
)


class VerdictError(Exception):
    pass


def build_shadow_home(home: Path, session_id: str, session_file: Path) -> Path:
    """Copy a minimal CODEX_HOME with the target thread, auth and config.

    The thread keeps its YYYY/MM/DD relative path so nothing about the layout
    changes between real and shadow. Returns the shadow root.
    """
    root = Path(tempfile.mkdtemp(prefix="ctd-shadow-"))
    for name in SHADOW_COPY_FILES:
        src = home / name
        if src.is_file():
            shutil.copy2(src, root / name)
    rel = session_file.relative_to(home)
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(session_file, dest)
    # A single index line for the session, when the real index has one.
    index = home / "session_index.jsonl"
    if index.is_file():
        entry = None
        for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if parsed.get("id") == session_id:
                entry = json.dumps(parsed, ensure_ascii=False)
                break
        if entry is not None:
            (root / "session_index.jsonl").write_text(entry + "\n", encoding="utf-8")
    return root


def classify(output: str) -> dict:
    hits: dict[str, str] = {}
    for label, sig in ERROR_SIGNATURES:
        if sig in output:
            hits.setdefault(label, sig)
    return hits


def run_verify(
    session_id: str,
    *,
    mode: str,
    prompt: str,
    marker: str,
    force_compact_window: int | None,
    timeout: int,
    backup_dir: Path,
) -> dict:
    home = lib.codex_home()
    hits = lib.find_session_file(home, session_id)
    if not hits:
        raise VerdictError(f"no session file found for {session_id} under {home}")
    session_file = hits[0]

    extra = ["--json", "--skip-git-repo-check"]
    if force_compact_window is not None:
        extra += ["-c", f"model_context_window={force_compact_window}"]

    result: dict = {
        "thread_id": session_id,
        "mode": mode,
        "session_file": str(session_file),
        "archive": session_file.as_posix().find("archived_sessions") != -1,
        "rc": None,
        "timed_out": False,
        "response_ok": False,
        "signatures": {},
        "sha256_before": None,
        "sha256_after": None,
        "hash_guard": None,
        "verdict": None,
    }

    pre = lib.sha256(session_file.read_bytes())
    result["sha256_before"] = pre

    if mode == "shadow":
        shadow_root = build_shadow_home(home, session_id, session_file)
        env = {**os.environ, "CODEX_HOME": str(shadow_root)}
        run = lib.run_codex(
            [*extra, session_id, prompt],
            cwd=shadow_root,
            env=env,
            timeout=timeout,
        )
        post = lib.sha256(session_file.read_bytes())
        result["sha256_after"] = post
        result["hash_guard"] = post == pre
        shutil.rmtree(shadow_root, ignore_errors=True)
    else:
        run = lib.run_codex([*extra, session_id, prompt], timeout=timeout)
        result["sha256_after"] = _ = lib.sha256(session_file.read_bytes())
        del _

    result["rc"] = run["rc"]
    result["timed_out"] = run["timed_out"]
    output = run["output"]
    result["signature_hits"] = classify(output)
    result["response_contains_marker"] = marker in output

    if mode == "shadow" and not result["hash_guard"]:
        result["verdict"] = "hash_changed"
    elif run["timed_out"]:
        result["verdict"] = "timeout"
    elif run["rc"] != 0:
        result["verdict"] = "resume_failed"
    elif "is not supported" in output:
        result["verdict"] = "model_blocked"
    elif marker not in output:
        result["verdict"] = "marker_missing"
    else:
        result["verdict"] = "ok"

    result["healthy"] = result["verdict"] == "ok"
    # Persist a copy of the run log next to the session's backups.
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / f"verify-{session_id}.log").write_text(output, encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True, help="Codex thread id (UUID)")
    parser.add_argument(
        "--mode", choices=("shadow", "live"), default="shadow",
        help="shadow = zero-touch copy in a temp CODEX_HOME (default); live = real thread",
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--marker", default=DEFAULT_MARKER)
    parser.add_argument(
        "--force-compact-window", type=int, metavar="TOKENS",
        help="pass -c model_context_window=N so the resume compacts for real",
    )
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--backup-dir", default=None,
        help="where to keep the run log (default: ~/.codex/.thread-doctor-backups)",
    )
    parser.add_argument("--json", action="store_true", help="print the machine-readable summary")
    args = parser.parse_args()

    try:
        result = run_verify(
            args.session_id,
            mode=args.mode,
            prompt=args.prompt,
            marker=args.marker,
            force_compact_window=args.force_compact_window,
            timeout=args.timeout,
            backup_dir=Path(args.backup_dir or lib.default_backup_dir()),
        )
    except VerdictError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"thread:    {result['thread_id']}")
        print(f"mode:      {result['mode']}  file: {result['session_file']}")
        print(f"rc:        {result['rc']}  timed_out: {result['timed_out']}")
        print(f"marker:    {'present' if result['response_contains_marker'] else 'MISSING'}")
        print(f"signatures:{json.dumps(result['signature_hits'], ensure_ascii=False) or 'none'}")
        if result["mode"] == "shadow":
            print(f"hash:      {'unchanged' if result['hash_guard'] else 'CHANGED (abort!)'}")
        print(f"verdict:   {result['verdict']}")
    sys.exit(0 if result.get("healthy") else 1)


if __name__ == "__main__":
    main()