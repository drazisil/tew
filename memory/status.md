# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Holds only the single most-recent `## Current status` entry — do not let `## Previous status` entries accumulate here again; rotate them into `status_archive.md` instead (see below) once a new "Current status" replaces them. Completed work goes in changelog.md — do not add "what's fixed" sections here.*

*Full investigation history lives in two places, both newest-first — do not re-derive any of it from scratch, grep instead: `changelog.md` (durable, organized by fix) and `status_archive.md` (rotated-out `## Previous status` entries, 2026-08-02 through 2026-08-28, some session-in-progress detail not duplicated in changelog.md).*
---

## Known false leads (permanent — do not remove on rotation)

- **`dbcode.c(3376) "The class has not been licensed"`**: prints every run, every time DAO/Jet does COM work, well before any actual failure. Molly confirmed (2026-08-16) this is expected/ignorable — NOT the cause of `CreateQueryDef`/DAO-3075 failures. Got mistakenly re-flagged as a "new lead" once already the same night (see `status_archive.md`, "Previous status (2026-08-16, cont'd x4)", for the correction) — check here before treating it as new again.

## Current status (2026-09-02, cont'd x48) — `__free_dbg` (0x009f6e20) un-no-op'd; run now reaches **GUI Initialized** for the first time in this project's history once the artificial `TEW_MAX_STEPS` ceiling is raised. New real blocker found: `user32.dll!GetDoubleClickTime` unimplemented.

**`__free_dbg` fix** (`tew/api/patch_internals.py`): previously patched to a hard no-op on the reasoning "our bump allocator never writes MSVC debug block headers, so any call would assert" — false as of x47's `free()`/reclaim work landing (`__heap_alloc_dbg`, 0x009f6460, was already unpatched real guest code writing real debug headers before x47; `__free_dbg`'s own `_BLOCK_TYPE_IS_VALID` check has real headers to validate). Left it unpatched instead. Live-verified (300s run): reached 12,165 times, zero asserts, no `except.txt`, clean exit at the (then-500M) step cap. The no-op was silently dropping every debug-tracked free at **both** the guest's own leak-tracking level (block never unlinked from `_CrtDumpMemoryLeaks`'s walked list) **and**, now that real `free()`/reclaim exists, at the host level too (`state.simple_free()` never ran for these blocks). Removed the now-obsolete `TestFreeDbgNoop` unit test (nothing left to unit-test — no Python handler exists there anymore).

**Milestone**: `TEW_MAX_STEPS` (env var, default 500,000,000) was the *actual* ceiling ending every recent "clean" run, not a real blocker — raising it to 5,000,000,000 let a run push to 692,847,967 steps / 72.4s vtime and reach `GUI Initialized @ 8388608 bytes: Version 1.31.14-DW` in `stdout.txt` — past all car-list loading, past DB init, further than this project has ever gotten. Halted there on a genuinely new, mundane blocker: `[UNIMPLEMENTED] user32.dll!GetDoubleClickTime — halting` (EIP=0x002072e2). This is an ordinary missing-handler halt (`cpu.halted`/`fatal_halt`, via `diagnose_halt`), **not** a CPU fault (`cpu.faulted`, via `diagnose_fault`) — real `GetDoubleClickTime` just returns a `UINT` (Windows default 500ms); trivial next fix, not yet done.

**Correction re: "reconnecting the leak report"**: `_dump_crt_memory_leaks` only runs from `diagnose_fault`, gated on `cpu.faulted` — a genuine CPU-level fault, distinct from an ordinary handler halt like the one above. Neither this session's `GetDoubleClickTime` halt nor any other run since `free()`/reclaim landed has hit a real `cpu.faulted` condition, so **the leak-dump path has not actually been re-exercised against real free() yet** — there's no live data to compare against §9-11's old bump-allocator-era numbers. Re-verifying it needs an actual fault, not just any halt.

Repro: `cd /data/Code/tew/.claude/worktrees/crt-leak-report-with-free && TEW_MAX_STEPS=5000000000 LOG_LEVEL=info LOG_CATEGORIES=exception,seh,startup timeout 570 .venv/bin/python run_exe.py`. Branch: `worktree-crt-leak-report-with-free`. Full suite passing (1194 tests, one fewer than x47's 1195 — `TestFreeDbgNoop` removed, nothing replaced it).

**Next**: implement `GetDoubleClickTime` (trivial) and see how much further `TEW_MAX_STEPS=5e9` gets past GUI init; separately, find or force a real `cpu.faulted` condition to actually re-verify the leak-dump path under real `free()`.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
- **CORRECTION (2026-08-30), supersedes `status_archive.md`'s x12/2026-08-25 and 2026-07-24 entries**: "restart the compositor in place" (`kwin_wayland --replace ...`, or `systemctl --user restart plasma-kwin_wayland.service`) is **no longer a valid stuck-SDL troubleshooting step** -- confirmed tonight (2026-08-30) that a compositor restart crashes the *entire user session*, not just the wedged tew client, twice in a row. Whatever made this a safe in-place recovery in 2026-07-24/2026-08-25 no longer holds (environment/KWin-version drift, most likely -- not investigated further). Do not attempt a compositor restart as an automated recovery step; if a run hangs at SDL2/window init, check for and clean up orphaned `run_exe.py` processes first (`SIGTERM`, not `-9`, to let `window_manager.shutdown()` run), and otherwise stop and ask Molly rather than touching the compositor.
