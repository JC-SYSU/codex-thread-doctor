---
name: codex-thread-doctor
description: >-
  Diagnose and repair broken Codex session threads that cannot resume or
  compact. Use when a Codex thread fails with "model is not supported",
  "invalid_encrypted_content", "could not be decrypted", HTTP 400 on
  compaction, empty/duplicate tool call_ids, corrupted compaction checkpoints,
  or when the user asks to audit all sessions, fix/unblock/resume/trim/clean a
  session JSONL, or verify a thread still works. Never used for Claude Code
  sessions.
---

# Codex Thread Doctor

A stuck thread is repaired in four phases, in order: **Diagnose**, **Preserve**,
**Treat**, **Verify**. Every phase ends with a checkable condition; do not move
on before the current one holds. The invariants in [Golden rules](#golden-rules)
bind every phase.

## Phase 1 — Diagnose (read-only)

Run a static health check first, on the single thread or fleet-wide. Never
write anything in this phase.

```bash
python3 scripts/doctor_audit.py --session <THREAD_ID>            # one thread
python3 scripts/doctor_audit.py --all --blocked-model <MODEL_ID> # fleet-wide
```

Treat only machine-state indicators: `last_model`, `history_models`,
`empty_call_ids`, `unmatched_outputs`, `calls_without_outputs`,
`compaction_balanced`, `parse_error`. The runtime-failure counters
(`error_kinds`, `aborted_turns`) show what the thread survived — a relay
outage leaves `usage_limit_exceeded` / `response_stream_disconnected` /
`server_overloaded` entries and interrupted turns, and those alone do not
block resume. `UNFINISHED` is informational (rolled-back turns) — not a
blocker. Both rollout generations are read: legacy `{event_msg:{…}}` and the
current `{type:"event_msg", payload:{…}}` shape.

**Done when**: the risk indicators for the thread are listed and each maps to
a row in `references/failure-modes.md`. If the audit shows no risk at all, the
most useful evidence for "really resumable" is a shadow verify (Phase 4)
anyway — run it before concluding.

## Phase 2 — Preserve (every write is a two-way door)

```bash
python3 scripts/doctor_fix.py restore --backup <FILE> --target <FILE>
```

Every Treat command writes its own timestamped backup under
`~/.codex/.thread-doctor-backups/` and prints `sha256: before -> after`.
On a post-write assertion failure the command restores the backup itself and
exits 2.

**Done when**: a backup file exists for every file about to be modified, and
its SHA-256 is recorded (the tool prints it). Reload the thread/app first if
the Codex app has the session open — the app caches state and will stomp the
fix.

## Phase 3 — Treat (one row of the table per command)

| Failure (from Phase 1 / user report) | Command |
| ---- | ---- |
| model not supported / stale history models | `doctor_fix.py normalize --session-id <id> --model <target-model> [--effort <level>]` |
| empty / duplicate call_id (compaction 400) | `doctor_fix.py call-ids --session-id <id>` |
| invalid_encrypted_content on a `rs_…` item | `doctor_fix.py remove-lines --session-id <id> --key encrypted_content --value <bad> --expect-count N` |
| corrupted compaction checkpoint `cmp_…` | `doctor_fix.py remove-compaction --session-id <id> --checkpoint <cmp_…> [--checkpoint-time <ts>]` |
| tool call with no output (stream died mid-call) | `doctor_fix.py drop-unanswered-calls --session-id <id> [--expect-count N]` |
| trim history at an anchor message | `doctor_fix.py trim --session-id <id> --anchor-text '<exact user text>' [--continuation <file>]` |
| optional cleanup: error text / orphan starts / index dupes | `doctor_fix.py scrub --session-id <id> [--error-text '<t>'] [--clear-error-texts] [--orphan-turn <t>] [--dedupe-index]` |

Details per failure — symptoms, evidence, per-command caveats, repeated
checkpoint chains — live in `references/failure-modes.md`; read the matching
row before running the command. `normalize`'s target model comes from the
user, never from your own judgment: ask which model their account serves if
they have not said, and confirm the id (spelling, `auto/` or provider
prefixes) before rewriting a whole session.

**Done when**: every probe in Phase 1 that justified a repair now reads
clean (e.g. no empty `call_id`, no `model` outside the target set, no bad
`encrypted_content` in the file). `doctor_fix` already re-asserts its own
invariants from disk; Phase 4 is the proof on the live model.

## Phase 4 — Verify (shadow first, live only on purpose)

```bash
python3 scripts/doctor_verify.py --session-id <id>                          # zero-touch
python3 scripts/doctor_verify.py --session-id <id> --force-compact-window 40000  # exercise auto-compaction
python3 scripts/doctor_verify.py --session-id <id> --mode live             # only to persist a real compaction
```

Shadow mode copies the thread (plus auth/config) into a temp `CODEX_HOME`,
resumes it with the real Codex CLI, and fails the run if the original file's
SHA-256 changed. `--force-compact-window` forces a genuine compaction in the
shadow so pairing/checkpoint fixes are exercised, not just the happy path.
The verdict is mechanical: exit code 0, the marker present in the output, no
"is not supported", no timeout. Anything else is reported as evidence, never
guessed; a failing verdict repeats the shadow run after the fix, not a live
one.

**Done when**: the shadow verdict says `ok` (or the failing evidence names a
specific signature), AND the original thread file's hash is unchanged. If the
goal was to persist a compaction into the real thread, only now run
`--mode live` — knowing that even `--ephemeral` appends lifecycle events to
the real JSONL.

## Golden rules

- **Machine state only.** Rewrite `model` / `effort` / lifecycle records;
  never alter message bodies (beyond explicit `--error-text`), `cwd`,
  permissions, turn ids, `comp_hash`, session id. History that *mentions* an
  old error is evidence, not a defect.
- **Shadow, not "read-only".** `codex exec resume --ephemeral` still writes
  to the real JSONL (lifecycle events, possibly compaction). Zero-touch
  verification is a shadow `CODEX_HOME` copy only; hash-guard on the original.
- **Never repair while the app holds the thread.** Close the thread (or quit
  the app) before Treat, and re-verify afterwards from a fresh process.
- **Backup before every write; restore on any doubt.** The `restore` command
  above is the way back; a post-write check failure already restores and
  aborts with exit 2.
- **One command per failure.** Don't chain `normalize` + `call-ids` + `trim`
  into a single session without re-running Phase 1 between steps — each step
  is verified before the next begins.

## Files

| Path | Role |
| ---- | ---- |
| `scripts/doctor_audit.py` | read-only fleet / single-thread health scan (+ optional shadow tests) |
| `scripts/doctor_fix.py` | repair subcommands: `normalize` `call-ids` `remove-lines` `remove-compaction` `scrub` `trim` `restore` |
| `scripts/doctor_verify.py` | shadow/live resume verification against the real Codex CLI |
| `scripts/doctor_lib.py` | shared library: backups, hashes, atomic writes, JSONL parse/render, call-id pairing |
| `references/failure-modes.md` | symptom → root cause → evidence → treatment per failure |
| `references/session-jsonl.md` | storage layout, record types, field-level invariants, what is never touched |