#!/usr/bin/env python3
"""Codex Thread Doctor — read-only fleet health audit.

Scans every session JSONL under ~/.codex/sessions and ~/.codex/archived_sessions,
computes machine-state health indicators, and — with --shadow-test — actually
resumes the at-risk candidates in temporary shadow CODEX_HOME copies.

Read-only by design: original files are only ever opened for reading; shadow
tests write only to temp directories (each verification's run log lands in
the backup directory, not in the session tree); the hash guard fails the run
if any original session file changed underneath the audit.

Indicators per session (all computed from machine state fields, never from
conversation text):
  last_model / last_effort     the final model & effort seen in the file
  history_models               every distinct model string, in order of appearance
  empty_call_ids               calls whose call_id is blank (compaction hazard)
  unmatched_outputs            outputs whose call_id references no same-type call
  compaction_balanced          compacted records == context_compacted events
  unfinished_starts            task_started without a later task_complete/task_error
  parse_errors                 unparseable lines (line numbers reported)

Usage:
  python doctor_audit.py --all [--blocked-model <id>]... [--shadow-test] [--json out.json]
  python doctor_audit.py --session <id> [--shadow-test]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import doctor_lib as lib
import doctor_verify as verify


def session_stats(path: Path) -> dict:
    """Static indicators for one session file. Never modifies anything."""
    stats: dict = {
        "thread_id": None,
        "file": str(path),
        "archived": "archived_sessions" in path.as_posix(),
        "rows": 0,
        "parse_error": None,
        "last_model": None,
        "last_effort": None,
        "history_models": [],
        "empty_call_ids": 0,
        "unmatched_outputs": 0,
        "compaction_balanced": True,
        "compactions": 0,
        "unfinished_starts": 0,
    }
    data = path.read_bytes()
    if not data.strip():
        stats["parse_error"] = "empty file"
        return stats
    try:
        rows = lib.parse_jsonl(data)
    except ValueError as exc:
        stats["parse_error"] = str(exc)
        return stats
    stats["rows"] = len(rows)
    m = lib.SESSION_ID_RE.search(path.name)
    stats["thread_id"] = lib.row_session_id(rows) or (
        m.group(1) if m else f"(no-id:{path.name})"
    )

    for row in rows:
        models = lib.walk_collect(row, {"model"})
        if models:
            stats["last_model"] = models[-1]
            for m in models:
                if m not in stats["history_models"]:
                    stats["history_models"].append(m)
        efforts = lib.walk_collect(row, {"effort", "reasoning_effort"})
        if efforts:
            stats["last_effort"] = efforts[-1]
    for row in rows:
        payload = row.get("payload") or {}
        for t in lib.CALL_TYPES:
            if payload.get("type") == t and not payload.get("call_id"):
                stats["empty_call_ids"] += 1
    stats["unmatched_outputs"] = count_unmatched(rows)
    c, e = lib.compaction_pair_counts(rows)
    stats["compactions"] = c
    stats["compaction_events"] = e
    # A compacted record with no event is the normal shape. The anomaly is
    # the reverse: completion events with no compacted record at all.
    stats["compaction_balanced"] = not (e > 0 and c == 0)
    started = completed = 0
    for row in rows:
        payload = row.get("payload") or {}
        if payload.get("type") == "task_started":
            started += 1
        if payload.get("type") in ("task_complete", "task_error"):
            completed += 1
    stats["unfinished_starts"] = max(0, started - completed)
    return stats


def count_unmatched(rows) -> int:
    """Outputs whose call_id references no same-type call (two passes; order
    independent, so an output preceding its call is still matched)."""
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


def at_risk(stats: dict, blocked: set[str]) -> bool:
    if stats.get("parse_error"):
        return True
    if stats.get("last_model") in blocked:
        return True
    if stats.get("empty_call_ids"):
        return True
    if stats.get("unmatched_outputs"):
        return True
    if not stats.get("compaction_balanced"):
        return True
    return False


def collect_all(home: Path, session_filter: str | None) -> list[Path]:
    files: list[Path] = []
    if session_filter:
        return lib.find_session_file(home, session_filter)
    for root in ("sessions", "archived_sessions"):
        files.extend((home / root).rglob("*.jsonl"))
    return files


def audit(home: Path, files: list[Path], blocked: set[str]) -> dict:
    sessions: list[dict] = []
    for path in files:
        stats = session_stats(path)
        stats["at_risk"] = at_risk(stats, blocked)
        sessions.append(stats)
    audit_result = {"home": str(home), "sessions": sessions}
    counts: dict = {}
    for stats in sessions:
        key = (
            "parse_error" if stats["parse_error"]
            else "risk" if stats["at_risk"]
            else "ok"
        )
        counts[key] = counts.get(key, 0) + 1
    audit_result["summary"] = counts
    return audit_result


def render_table(result: dict, blocked: set[str]) -> str:
    lines = []
    rows = sorted(result["sessions"], key=lambda s: (s["archived"], s["file"]))
    header = (
        f"{'session'.ljust(38)} {'arch'.ljust(4)} {'rows'.ljust(6)} "
        f"{'last_model'.ljust(16)} risks"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for s in rows:
        risks = []
        if s["parse_error"]:
            risks.append(f"PARSE:{s['parse_error'][:60]}")
        if s["last_model"] in blocked:
            risks.append(f"MODEL:{s['last_model']}")
        if s["empty_call_ids"]:
            risks.append(f"EMPTY_CALL_ID:{s['empty_call_ids']}")
        if s["unmatched_outputs"]:
            risks.append(f"UNMATCHED:{s['unmatched_outputs']}")
        if not s["compaction_balanced"]:
            risks.append(f"COMPACTION:{s['compactions']}")
        if s["unfinished_starts"]:
            risks.append(f"UNFINISHED:{s['unfinished_starts']}")  # informational only
        lines.append(
            f"{s['thread_id'][:38].ljust(38)} {'A' if s['archived'] else ' '} "
            f"{str(s['rows']).ljust(6)} {(s['last_model'] or '-')[:16].ljust(16)} "
            + ("; ".join(risks) if risks else "ok")
        )
    lines.append("")
    lines.append(
        "summary: " + json.dumps(result["summary"], ensure_ascii=False)
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", help="audit a single session id")
    parser.add_argument("--all", action="store_true", help="audit every session file")
    parser.add_argument("--blocked-model", action="append", default=[],
                        help="model id to flag as a blocker (repeatable) — decide per account, don't copy lists")
    parser.add_argument("--shadow-test", action="store_true",
                        help="resume at-risk candidates in a temp CODEX_HOME and report verdicts")
    parser.add_argument("--json", help="write the full machine-readable report here")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    if args.session == args.all or (not args.session and not args.all):
        parser.error("pass exactly one of --session <id> or --all")

    home = lib.codex_home()
    files = collect_all(home, args.session)
    if args.session and not files:
        print(f"error: no session file found for {args.session} under {home}",
              file=sys.stderr)
        sys.exit(2)
    blocked = set(args.blocked_model)
    result = audit(home, files, blocked)

    if args.shadow_test:
        candidates = [
            s for s in result["sessions"]
            if s["at_risk"] and not s["parse_error"]
        ]
        if not candidates:
            print("no at-risk candidates to shadow-test")
        verdicts = {}
        for cand in sorted(candidates, key=lambda s: s["file"]):
            try:
                vr = verify.run_verify(
                    cand["thread_id"],
                    mode="shadow",
                    prompt=verify.DEFAULT_PROMPT,
                    marker=verify.DEFAULT_MARKER,
                    force_compact_window=None,
                    timeout=args.timeout,
                    backup_dir=lib.default_backup_dir() / "audit",
                )
            except verify.VerdictError as exc:
                verdicts[cand["thread_id"]] = {"error": str(exc)}
                continue
            verdicts[cand["thread_id"]] = {
                "verdict": vr["verdict"],
                "rc": vr["rc"],
                "timed_out": vr["timed_out"],
            }
        result["shadow_verdicts"] = verdicts
        print("shadow tests:")
        for tid, v in verdicts.items():
            print(f"  {tid}  {v.get('verdict') or v.get('error')}")
    else:
        print(render_table(result, blocked))

    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nwrote report: {args.json}")


if __name__ == "__main__":
    main()