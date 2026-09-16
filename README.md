[中文版](README.zh-CN.md) | English

# Codex Thread Doctor

Diagnoses and repairs Codex session threads that can no longer resume or
compact — the "`<model>` is not supported" wall, the HTTP 400 on a long
thread's auto-compaction, the `invalid_encrypted_content` dead end. Instead of
telling you to start over, it fixes the session file so the thread is usable
again, and proves it by actually resuming the thread with the real Codex CLI.

## How it works

Every repair is the same four phases: **diagnose** (read-only fleet scan or
single-thread check), **preserve** (timestamped, hash-verified backup before
any write), **treat** (a command per failure mode; untouched lines keep their
original bytes), and **verify** (a zero-touch shadow resume against the real
Codex CLI, hash-guarded on the original file).

```text
audit (read-only) ──► backup + SHA-256 ──► fix (atomic, re-asserted) ──► shadow resume ──► verdict
```

## What it fixes

| Failure | Command |
| ---- | ---- |
| `model … is not supported` on resume / stale model history | `normalize --model <target>` |
| empty or duplicate tool `call_id` (breaks auto-compaction with HTTP 400) | `call-ids` |
| `invalid_encrypted_content` on reasoning items | `remove-lines` with an exact `--expect-count` guard |
| corrupted compaction checkpoint `cmp_…` blocking resume | `remove-compaction` |
| trim history before an anchor message (cross-file threads included) | `trim` |
| optional cleanup: gateway error text, orphaned starts, duplicate index entries | `scrub` |

It never rewrites conversation text (beyond an explicit `--error-text` scrub),
`cwd`, permissions, turn ids, or session ids. History that merely *mentions* an
old error is evidence, not a defect — the tools only read machine-state fields
and lifecycle records. Runs on macOS, Linux, and Windows; Python 3.11+, zero
runtime dependencies; the only external tool it drives is the Codex CLI
itself. `session_index.jsonl` is left alone unless you ask for `--dedupe-index`.

## Install

Copy the block below and paste it into your agent (Codex, Claude Code, or any
coding agent). The agent reads `INSTALL.md` and does the work:

```
You are installing Codex Thread Doctor. The source repository is
https://github.com/<your-org>/codex-thread-doctor. Do the following:

1. Clone that repository to a sensible location (e.g. ~/codex-thread-doctor) and cd into it.
   If you cannot clone, read the installation spec directly:
   https://raw.githubusercontent.com/<your-org>/codex-thread-doctor/main/INSTALL.md
2. Read INSTALL.md in full (INSTALL.zh-CN.md if preferred) and execute it step by step:
   verify the prerequisites, confirm the Codex config directory, smoke-test the three
   entry points (doctor_audit.py / doctor_fix.py / doctor_verify.py --help), and run the
   test suite (python3 -m pytest tests/ -q) if pytest is available.
3. Before running any write-side command (doctor_fix) or --shadow-test, show me what
   you are about to do and wait for my confirmation.
4. When everything passes, run the acceptance checklist from INSTALL.md section 5 and
   report each item's result to me.
```

Prefer the agent install; manual installation is: clone, run the acceptance
checklist — there is nothing else to install.

## Day-to-day

```bash
# Fleet health scan (read-only), flag a model your account doesn't serve
python3 scripts/doctor_audit.py --all --blocked-model <model-id>

# One thread's health + a real shadow resume test
python3 scripts/doctor_audit.py --session <thread-id> --shadow-test

# Fix, then prove it works
python3 scripts/doctor_fix.py normalize --session-id <thread-id> --model <target-model>
python3 scripts/doctor_verify.py --session-id <thread-id>
```

Backups live in `~/.codex/.thread-doctor-backups/`; every fix prints
`sha256: before -> after` and re-asserts its invariant from disk before
returning. A failed assertion restores the backup and exits 2.

## What it doesn't do

It does not modify the Codex app, its hooks, or your auth. It does not touch
archived sessions unless you point it at one. It is not a cleanup fairy:
orphaned `task_started` records and old error strings are only removed when
you explicitly ask (via `scrub`). A thread that failed for account-level
reasons (a model your account can't serve at all) can be repaired only to the
model you do have. The shadow verify proves the file is resumable on your
local machine; it cannot promise the remote service accepts a transcript it
has never seen.

## Data, cleanup, safety

A repair reads the session JSONL, writes a verified backup under
`~/.codex/.thread-doctor-backups/`, and atomically replaces the file. To undo
any repair: `doctor_fix.py restore --backup <file> --target <file>`, or delete
the backup directory entirely. Shadow verifications write only to temp
directories removed on exit (their run logs stay in the backup directory).
Re-run the audit any time; both audit and verify are read-only against the
real home.

Check that each script reports 0 errors and the tests pass on your machine
after the agent install (INSTALL.md section 5). Codex CLI is an external
requirement: `doctor_verify.py` needs the `codex` binary on PATH (or `CODEX_BIN`
pointing at it; see INSTALL.md section 7 for the macOS app-bundle case).

## License

MIT — see [LICENSE](LICENSE). Symptoms and per-failure treatment details are
in SKILL.md and `references/failure-modes.md`.