# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Holds only the single most-recent `## Current status` entry — do not let `## Previous status` entries accumulate here again; rotate them into `status_archive.md` instead (see below) once a new "Current status" replaces them. Completed work goes in changelog.md — do not add "what's fixed" sections here.*

*Full investigation history lives in two places, both newest-first — do not re-derive any of it from scratch, grep instead: `changelog.md` (durable, organized by fix) and `status_archive.md` (rotated-out `## Previous status` entries, 2026-08-02 through 2026-08-22, some session-in-progress detail not duplicated in changelog.md).*
---

## Known false leads (permanent — do not remove on rotation)

- **`dbcode.c(3376) "The class has not been licensed"`**: prints every run, every time DAO/Jet does COM work, well before any actual failure. Molly confirmed (2026-08-16) this is expected/ignorable — NOT the cause of `CreateQueryDef`/DAO-3075 failures. Got mistakenly re-flagged as a "new lead" once already the same night (see `status_archive.md`, "Previous status (2026-08-16, cont'd x4)", for the correction) — check here before treating it as new again.

## Current status (2026-08-22) — msjet35.dll collation-cache crash RESOLVED (CompareStringA/W never validated the locale argument); new blocker is a real debug-build INT3 assertion deep in MCity_d.exe's own code, not yet investigated

**Context**: the `msjet35.dll` collation-cache crash (`FUN_7a87ba0a`, `DAT_7a9362c0[session]+0x2c0` reading a never-populated collation-interface pointer) covered in the prior entry is now fully resolved — see `status_archive.md` "Previous status (2026-08-22)" for the full investigation (three ruled-out hypotheses, dynamic ClickHouse-capture confirmation, static trace to `FUN_7a878159`/`FUN_7a84c830`) and `changelog.md`'s "2026-08-22" entry for the concise root-cause/fix summary.

**Root cause, in one line**: MCity_d.exe's own `CreateDatabase` call for `Tmp.MDB` passes an empty Locale connect-string; msjet35.dll's own default-collating-order fallback logic (`FUN_7a84c830`) tries to detect this by probing `CompareStringA` with the unspecified locale (0) and checking whether it fails — but tew's `CompareStringA`/`CompareStringW` (`tew/api/kernel32_io.py`) never read the locale argument at all, so any value silently "succeeded", the fallback never triggered, and the session-level collation cache stayed null until a later query crashed on it.

**Fix**: both handlers now validate the locale against `0x0409` (the one locale this emulator models everywhere else — `_is_valid_locale`/`_get_user_default_lcid`/`_get_system_default_lang_id` are all already hardcoded to it) and return 0 + `ERROR_INVALID_PARAMETER` for anything else. 11 new tests, `tests/unit/api/test_kernel32_io_compare_string.py`. `pytest -q`: 1232/1232.

**Live-verified**: the `MSJET35.DLL+0x3bc04` collation crash no longer occurs. The run progresses much further — real `MCity_d.exe` return addresses on the stack, not a DLL crash — to `INT3 breakpoint at EIP=0x00688c68 unhandled by SEH chain -- halting`. Per established policy (`feedback_no_auto_continue_debug_break.md`), a genuinely unhandled `MCity_d.exe` INT3 correctly stays a hard halt rather than being silently resumed. **This is the new current blocker, not yet investigated at all** — no Ghidra work, no static tracing, no hypothesis yet on what real-build assertion this corresponds to or why it's firing.

**Housekeeping done this session, still live**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) is running and has real schema loaded — useful again for any future "what wrote address X" investigation. Gated capture window in `run_exe.py` (`_HISTORY_CAPTURE_START_STEP`/`_HISTORY_CAPTURE_STOP_STEP`/`_HISTORY_CAPTURE_DONE`, currently 500K-8M) is a one-shot, safe to leave or retarget.
- `run_exe.py`'s breakpoint slots: 7 of 8 are stale leftovers from the DAO-3075 investigation (explicitly noted as free to repurpose), 1 (`_fun_86a5a7_probe`'s slot) was freed and reused this session for a now-removed collation-locale probe — all 8 slots are effectively free for the next investigation.

**Not yet resolved**: what triggers the `INT3` at `0x00688c68`, and whether it's a real, expected game-side assertion (something the fixed collation bug used to prevent from ever being reached) or a new tew-side gap. Next step: read the debug-build's own assertion message/context around this EIP (likely needs `LOG_CATEGORIES` including whatever category logs INT3/assertion text, or a Ghidra look at `0x00688c68` in `MCity_d.exe` itself — static==runtime for the main EXE, no relocation delta needed).
