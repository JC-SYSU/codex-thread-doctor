[中文版](INSTALL.zh-CN.md) | English

# Installation Guide (for the agent performing the install)

This document is an operating spec: run the steps in order, every step lists
an **expected output** and **failure handling**. When everything passes, run
the "Acceptance checklist" at the end and report the results.

Let `$REPO` be the repository root. All commands run from `$REPO`. Platforms:
**macOS / Linux / Windows**; on Windows replace every `python3` below with
`python` (or `py -3`). The Codex config directory is `$CODEX_HOME` if that env
var is set, else `~/.codex` (`%USERPROFILE%\.codex` on Windows) — the doctor
resolves it the same way at runtime, so whatever you verify here is what it will
use.

## 0. Prerequisites

| # | Check | Command | Expected | Failure handling |
| ---- | ---- | ---- | ---- | ---- |
| 0.1 | Python >= 3.11 | `python3 --version` | 3.11.x or newer | install it, then restart the shell |
| 0.2 | Codex CLI on PATH | `codex --version` | prints a version | install Codex first, or set `CODEX_BIN` to the binary; the doctor only *needs* the CLI for `doctor_verify.py` and audit `--shadow-test` |
| 0.3 | Config dir resolvable | `python3 -c "import os,pathlib;print(os.environ.get('CODEX_HOME') or str(pathlib.Path.home()/'.codex'))"` | prints your config dir | if it prints something else than what you expect, unset/export `CODEX_HOME` deliberately before continuing |

## 1. Place the repository

```bash
cd <your chosen directory>        # e.g. ~/codex-thread-doctor
# already cloned per README, or copy the folder there
```

Expected: `SKILL.md`, `scripts/`, `references/`, `tests/` present in `$REPO`.
Failure: missing files → the clone/copy is broken, redo it.

## 2. Smoke-test the three entry points

```bash
python3 -m py_compile scripts/doctor_lib.py scripts/doctor_fix.py scripts/doctor_verify.py scripts/doctor_audit.py
python3 scripts/doctor_audit.py --help >/dev/null && echo AUDIT_OK
python3 scripts/doctor_fix.py --help     >/dev/null && echo FIX_OK
python3 scripts/doctor_verify.py --help  >/dev/null && echo VERIFY_OK
```

Expected: `AUDIT_OK` `FIX_OK` `VERIFY_OK` printed.
Failure: interpreter errors on `py_compile` → Python too old or broken
encoding setup; `ModuleNotFoundError` → you are not running from `$REPO` or
the `sys.path` bootstrap at the top of each script is missing.

## 3. Run the test suite (optional but recommended)

```bash
python3 -m pytest tests/ -q        # requires pytest (dev-only dependency)
```

Expected: all tests pass (`20 passed` with a clean checkout).
Failure: `ModuleNotFoundError: pytest` → `python3 -m pip install -r requirements.txt`,
then re-run; the toolkit itself has zero runtime dependencies, so this step is
optional.

## 4. Confirm the doctor sees your sessions

```bash
python3 scripts/doctor_audit.py --all | head -20
```

Expected: a table of your session files with column headers; the path under
"file" is not shown but its archive flag, row count and last model are. A
`summary: {"ok": N, "risk": M}` line at the end of the full table.
Failure: "no session file found" errors or a print showing an unexpected
config dir → re-check 0.3. The full table may be long; `head` is fine for
confirmation.

Never run `doctor_fix.py` in this step — it writes. The acceptance checklist
below is the end of the install.

## 5. Acceptance checklist — report each item to the user

| # | Item | How to check | Pass condition |
| ---- | ---- | ---- | ---- |
| 5.1 | CLI entry points work | run step 2 commands again | `AUDIT_OK` `FIX_OK` `VERIFY_OK` |
| 5.2 | Import check clean | `python3 -m py_compile scripts/*.py` | no output, exit 0 |
| 5.3 | Doctor resolves the real config dir | step 4 output | table shows your sessions; `summary` line present |
| 5.4 | (optional) tests pass | step 3 | `20 passed` |
| 5.5 | Backup dir writable | `python3 -c "import sys;sys.path.insert(0,'scripts');import doctor_lib;print(doctor_lib.default_backup_dir())"` | prints `.../.thread-doctor-backups`; nothing written yet |

## 6. Uninstall

Delete the repository directory. Nothing was ever installed outside `$REPO`;
backups you created stay in `~/.codex/.thread-doctor-backups/` until you
delete them.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
| ---- | ---- | ---- |
| `doctor_audit.py: error: pass exactly one of --session <id> or --all` | both/neither selector given | pass exactly one |
| `no session file found for <id>` | typo in the id, or the thread is archived and the name differs | verify the id; archive duplicates are reported, not guessed |
| `doctor_fix.py` exits 2 with "post-write check failed" | a fix invariant did not hold on disk | the original was auto-restored from the printed backup; read the failing check name and re-run the treatment |
| `codex CLI not found` (verify only) | Codex not on PATH (macOS: often inside the ChatGPT.app bundle's Resources dir) | install Codex, set `CODEX_BIN` to the binary, or add it to PATH |
| Windows: `python3` not recognized | interpreter is `python` / `py -3` here | use those instead (replacements noted in section header) |
| Hash guard trips in shadow verify | the Codex app wrote to the session while verifying | close the thread/app; re-run; if the app rewrites on open, the file is changing under you — treat it as evidence the app holds the session |