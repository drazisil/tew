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

## Current status (2026-09-02, cont'd x47) — Real `free()`/reclaim landed in the `simple-alloc-real-free` worktree: `state.simple_free()` returns blocks to a first-fit free list that `simple_alloc()` now searches before bumping the cursor, wired into `msvcrt.dll`'s `free()`/`operator delete`. A clean foreground run reached the 500M-step execution cap at 187s of vtime with no heap-exhaustion halt — previously the x42/x45 ceiling reliably hit in the 85-100s window. Not yet proven gone for good (run didn't go indefinitely), but reclaim is clearly working. `HeapAlloc`'s per-call log line also moved off `handlers` into a new default-off `memory` category (opt in with `+memory`) since it was flooding at 62,501 lines/run now that allocations actually cycle.

**Note (worktree base)**: this worktree (`worktree-simple-alloc-real-free`) is based on `6e5dcf5` (PR #8, merged), which already carries both the x45 `handle_exception` fix and the x46 `_dump_crt_memory_leaks`/`FatalHaltError` fix (confirmed via `git blame` on `exception_diagnostics.py:302` — both landed as part of that single commit, not as a separate follow-up) — so this branch already has the full x45/x46 exception-handling story, on top of which the free()/reclaim work above was done.

Repro: `cd /data/Code/tew/.claude/worktrees/simple-alloc-real-free && LOG_LEVEL=debug LOG_CATEGORIES=handlers,cpu,exception,startup,fileio timeout 300 .venv/bin/python run_exe.py`. Branch: `worktree-simple-alloc-real-free`. Full suite passing (1195 tests).

**Next**: confirm the free list holds up over a longer/full run (this one stopped at the step cap, not a natural end, so the ceiling being gone for good isn't proven yet).

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
- **CORRECTION (2026-08-30), supersedes `status_archive.md`'s x12/2026-08-25 and 2026-07-24 entries**: "restart the compositor in place" (`kwin_wayland --replace ...`, or `systemctl --user restart plasma-kwin_wayland.service`) is **no longer a valid stuck-SDL troubleshooting step** -- confirmed tonight (2026-08-30) that a compositor restart crashes the *entire user session*, not just the wedged tew client, twice in a row. Whatever made this a safe in-place recovery in 2026-07-24/2026-08-25 no longer holds (environment/KWin-version drift, most likely -- not investigated further). Do not attempt a compositor restart as an automated recovery step; if a run hangs at SDL2/window init, check for and clean up orphaned `run_exe.py` processes first (`SIGTERM`, not `-9`, to let `window_manager.shutdown()` run), and otherwise stop and ask Molly rather than touching the compositor.
