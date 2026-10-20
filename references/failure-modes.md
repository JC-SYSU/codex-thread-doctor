# Failure modes and their treatments

One line per failure: the symptom you see, the root cause in the JSONL, the
machine-state evidence to confirm it, and the treatment. Read this table only
when a symptom matches — the Diagnose/Verify flow in SKILL.md stays the same
for every entry.

## 1. `model ... is not supported` on resume

- **Symptom**: resuming the thread fails immediately with "`<model>` is not
  supported"; the app may also refuse to open the thread.
- **Root cause**: one or more historical `turn_context` / `thread_settings`
  records carry a model id your ChatGPT account does not serve (a model the
  user swapped in earlier, an alias from a proxy/extension like
  `<provider>/<model-name>`, a flash-tier id that was dropped). Cold
  resume reads the last effective state, not just the UI settings.
- **Evidence**: `doctor_audit.py --session <id>` shows `last_model` /
  `history_models` values outside your allowed set.
- **Treatment**: `doctor_fix.py normalize --session-id <id> --model <target> [--effort <level>]`.
  Pick a model id your account actually serves. Unifying effort is optional
  but keeps the session consistent.
- **Verify**: `doctor_verify.py --session-id <id>` (shadow mode first).

## 2. Long threads fail to compact (`HTTP 400`, "empty call_id")

- **Symptom**: the thread works until it needs auto-compaction, then the
  resume/continue errors with a 400 from the server; the CLI log mentions
  `call_id` or "invalid paginated history".
- **Root cause**: tool calls with blank or duplicate `call_id` (often from
  crashed/interrupted tool turns). The server refuses to compress the
  transcript until every call/output pair is well-formed.
- **Evidence**: `audit` shows `EMPTY_CALL_ID:n` / `UNMATCHED:n`; or the
  repair scripts from before this toolkit were used.
- **Treatment**: `doctor_fix.py call-ids --session-id <id>` — empty ids take
  the call's own `id` (or a generated one), outputs are FIFO-paired to the
  matching call type, duplicates get a `call_repaired_…` id.
- **Verify**: `doctor_verify.py --session-id <id> --force-compact-window 40000`
  forces a real compaction in the shadow copy so the pairing is exercised.

## 3. `invalid_encrypted_content` / "could not be decrypted or parsed"

- **Symptom**: resume fails on an item id (`rs_…`) that cannot be decrypted;
  the same thread may fail at several idle items one after another.
- **Root cause**: `reasoning` records whose `encrypted_content` is a
  placeholder that fails validation (a known bad value seen in the wild is
  the JWK-ish `ocxr1:eyJzaWciOiJraXJvLXJzLXRoaW5raW5nLXNpZ25hdHVyZSJ9`).
  These are not user text and take no part in call pairing.
- **Evidence**: grep the file for the failing item id; check every `reasoning`
  row's `encrypted_content` for the placeholder.
- **Treatment**: `doctor_fix.py remove-lines --session-id <id> --key encrypted_content --value <bad> --expect-count N`
  — the exact `--expect-count` is your guard against deleting more than the
  broken items (adjust from the pre-check count).
- **Verify**: `doctor_verify.py` shadow resume shows no `invalid_encrypted_content`.

## 4. Resume fails on a compaction checkpoint

- **Symptom**: resume dies with `invalid_encrypted_content` (or a decrypt
  error) pointing at a `cmp_…` id; sometimes several consecutive checkpoints
  are bad and the thread can never get past the oldest one.
- **Root cause**: one or more `compacted` records whose checkpoint payload is
  corrupted. Older checkpoints sit in `replacement_history` chains, so a
  single bad checkpoint can strand every later compression.
- **Evidence**: the failing `cmp_…` id; `audit` shows the thread, and the
  shadow verify reports the same decrypt error.
- **Treatment**: `doctor_fix.py remove-compaction --session-id <id> --checkpoint <cmp_…> [--checkpoint-time <ts>]`.
  Repeat for each bad id. The command also removes the paired
  `context_compacted` events; a thread whose raw history is still intact
  resumes fine without any compression checkpoints.
- **Verify**: shadow resume must reach the final answer without decrypt
  errors; same-batch other `cmp_…` ids that fail next should also be removed
  until the thread is clean.

## 5. History won't fit or is full of stale noise

- **Symptom**: the thread is enormous, mostly prior unrelated work; you want
  to keep one anchor message onward and drop the rest.
- **Root cause**: n/a — this is a size/cleanliness operation, not a failure.
- **Treatment**: `doctor_fix.py trim --session-id <id> --anchor-text '<exact user message>' [--continuation <file>]`.
  The anchor must match a user message byte-for-byte (leading/trailing
  whitespace ignored). For cross-file threads (a continuation rollout), pass
  the second file — only its compaction records are stripped, all content stays.
- **Verify**: shadow resume after trim shows the anchor as the first user
  message and no compaction errors.

## 6. Stale traces that don't block anything (optional cleanup)

- **Symptom**: nothing breaks; you just want error text, orphaned starts and
  duplicate index entries gone.
- **Evidence**: `audit` shows `UNFINISHED:n` (informational); the file
  contains a known gateway error string in `agent_message.message` /
  `output_text.text` / `task_complete.last_agent_message`.
- **Treatment**: `doctor_fix.py scrub --session-id <id> --error-text '<text>' --orphan-turn <turn_id> --dedupe-index`.
  Each flag is independent and optional; blanking only affects the three
  message-bearing fields, orphaned starts are dropped only for the turn_ids
  you name, `--dedupe-index` keeps the newest `session_index.jsonl` entry.

## 7. Relay outages: `stream disconnected before completion` banners

- **Symptom**: the UI shows repeated "stream disconnected before completion:
  …" cards and reconnect attempts; the thread eventually continues or the
  turn ends with an error.
- **What is stored**: the live banners are not durable. In the current
  writer, `EventMsg::Error` and `EventMsg::StreamError` are classified as
  transient events (`codex-rs/rollout/src/policy.rs`) and never reach the
  JSONL. Only the resulting lifecycle records survive:
  `task_started` / `task_complete` / `turn_aborted`, with an optional
  `error.codex_error_info` on terminal events.
- **Evidence**: `audit` reports `ERRORS:<kind>=n` (e.g.
  `usage_limit_exceeded`, `response_stream_disconnected`,
  `server_overloaded`, `response_too_many_failed_attempts`) and
  `ABORTED:interrupted=n`. `CALLS_NO_OUTPUT:n` means a stream died after a
  tool call and its output never arrived.
- **Treatment**: runtime errors alone need no file surgery — resume and let
  the client retry. Repair only the structural residue:
  `doctor_fix.py call-ids` for empty/duplicate ids, then
  `doctor_fix.py drop-unanswered-calls --expect-count <n>` for calls whose
  output never arrived (removing a call that never completed also removes
  the half-turn the server would otherwise try to compress). If an old
  rollout did persist failure text, blank it with
  `doctor_fix.py scrub --error-text '<text>' --clear-error-texts`; the error
  status (`codex_error_info`) stays intact.
- **Verify**: `doctor_verify.py --force-compact-window 40000` on the shadow
  copy — a thread that survives a real compaction is structurally healthy.

## Cross-cutting truths (from real repair sessions)

- `codex exec resume --ephemeral` still appends lifecycle events to the real
  JSONL — never use a "read-only" claim on the live thread. Shadow copies are
  the only zero-touch verification.
- After any `doctor_fix` run: backup exists in
  `~/.codex/.thread-doctor-backups/`, output prints `sha256: before -> after`,
  and the file was re-read and asserted from disk before the command returned.
  On assertion failure the original is restored automatically and the command
  exits 2.
- Never edit a session while the Codex app has it open; the app caches state
  and may overwrite your fix. Close the thread (or quit the app) before
  treating, and confirm with a fresh `doctor_verify` afterwards.
- If a thread is archived and broken, un-archive it first — the doctor
  reports active vs archived hits from `find_session_file` and treats the
  active copy as authoritative.