# Codex session storage — what the doctor reads

## Layout

Codex CLI keeps everything under one config directory: `$CODEX_HOME` if set,
else `~/.codex` (`%USERPROFILE%\.codex` on Windows).

| Path (relative to the home) | Contents |
| ---- | ---- |
| `sessions/YYYY/MM/DD/rollout-<timestamp>-<session-id>.jsonl` | active sessions, one JSON object per line |
| `archived_sessions/.../rollout-...jsonl` | sessions the user archived; the app does not resume these by default |
| `session_index.jsonl` | one line per session: `{"id": ..., "thread_name": ..., "updated_at": ...}`; duplicates happen and are harmless to the session itself |

The filename embeds the session id (UUID). A `rollout-…` file can be byte-for-byte
identical in `sessions/` and `archived_sessions/` — always treat the active copy
as authoritative.

## Record types that matter for repair

| `type` | Shape | Why it matters |
| ---- | ---- | ---- |
| `session_meta` | first line, `payload.session_id` | used to preflight that we opened the right file |
| `turn_context` | `payload.model`, `payload.effort`, `payload.collaboration_mode.settings.model / .reasoning_effort` | the model/effort the turn actually ran with; a stale unsupported value here blocks cold resume |
| `thread_settings` | `payload.settings.model`, `.reasoning_effort` | per-thread settings override; the app prefers these over turn defaults |
| `compacted` | `type=compacted`, `timestamp`, `replacement_history` (array of `{checkpoint_id, timestamp, …}`) | compression checkpoints; a corrupted one raises `invalid_encrypted_content` on resume and cannot be repaired from inside the app |
| `event_msg / context_compacted` | `event_msg.type=context_compacted` | compression completion event; may be absent even though `compacted` records exist (normal) |
| `function_call` / `function_call_output` | `payload.call_id` (+ `id`, `name`, `arguments`) | empty or duplicate `call_id` breaks server-side compaction (`HTTP 400`) once the thread gets long enough |
| `custom_tool_call` / `custom_tool_call_output`, `tool_search_call` / `tool_search_output` | same pattern | same pairing rules |
| `reasoning` | `payload.response_item.encrypted_content` (JWK-style base64) | `Payload too long` / validation errors when the encrypted blob is a placeholder that cannot be decrypted |
| `task_started` / `task_complete` / `task_error` | `payload.turn_id` | orphaned `task_started` (no later finish) is usually a rolled-back turn, informational |
| `message` | `payload.role == "user"`, `payload.input_text` | the only record the trim tool locates anchors in |

## Field-level invariants

- A session with a readable opener and valid JSON per line is parseable; the
  doctor refuses mid-fix on the first malformed line, naming its line number.
- `model` / `effort` / `reasoning_effort` are *strings* in the machine-state
  records. Never touch `arguments` / `input` / `input_text` / `message`
  subtrees — they can contain the same keys as free text (a tool calling a
  model router, a message about the model, …).
- `call_id` pairing is per call/output type: an output matches a call of the
  *same* type; ids must be unique within a type.

## What the doctor never touches

User/assistant message bodies (beyond the explicit `--error-text` scrub),
`cwd`, permission/sandbox fields, turn ids, `comp_hash`, CLI versions, and
session ids. History that *mentions* an old error is evidence, not a defect —
only machine-state fields and lifecycle records are rewritten or dropped.