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

## Current status (2026-09-02, cont'd x49) — `GetDoubleClickTime` implemented; found and fixed a real bug where `cpu.faulted` went stale *during* SEH dispatch itself, which had silently prevented the crash-diagnostic leak dump from ever running on a real fault. Leak dump now genuinely fires — and immediately surfaces a new fault inside its own nested guest call.

**`GetDoubleClickTime`** (`tew/api/user32_handlers.py`): trivial no-arg handler, returns `500` (real Windows default; `SPI_GETDOUBLECLICKTIME`/registry not modeled). New test file `test_user32_getdoubleclicktime.py`.

**Real bug found and fixed**: raising `TEW_MAX_STEPS` past `GetDoubleClickTime` let a run reach a genuine unhandled CPU fault (`tid=1011`, EIP=0x00a8299b) at 75s vtime — but the post-run dispatch (`elif cpu.faulted: diagnose_fault(...) elif cpu.halted: diagnose_halt(...)`) took the `diagnose_halt` branch, not `diagnose_fault`, even though this *is* a `cpu.faulted` condition — meaning the crash-diagnostic leak dump (`_dump_crt_memory_leaks`, x43) silently didn't run. Diagnosed with temporary logging: `dispatch_exception()` (walking the game's real SEH handler chain) executes guest handler code via nested `cpu.run()` calls, and confirmed live that **even when the whole chain concludes "unhandled"**, `cpu.faulted` already reads `False` the instant `dispatch_exception()` returns — the walk's own successful intermediate steps clear the native `cpu_is_faulted()` flag as a side effect, the same mechanism as two *already-documented* fixes at this exact call site (see the inline comment history in `run_exe.py`), just one layer deeper than either. This means `_dump_crt_memory_leaks` had never actually fired for a real fault since it was added — every fault that made it this far already had this problem.

**Fix** (`run_exe.py`, the unhandled-SEH branch): `cpu.faulted = True` explicitly, right before `cpu.halted = True; break`. `cpu.faulted`'s setter sets the sticky `_py_faulted` flag (`cpu_zig.py`: `self._py_faulted or _lib.cpu_is_faulted(...)`), which survives further native-flag resets — so the post-run dispatch now sees the fault this branch already determined, not whatever the native flag happens to read by then.

**Confirmed live**: `diagnose_fault` now runs and `_dump_crt_memory_leaks` genuinely invokes the guest's real `_CrtDumpMemoryLeaks` — but it immediately hits **another** fault, at `EIP=0x004d980f`, the *same* address that self-recovered via real SEH earlier in the same run (3.5s, main thread `tid=1000`) — this time it doesn't recover (different thread context, `tid=1011`), and the dump never completes (no `except.txt`, no new `stdout.txt` content, no per-block leak lines). Not yet investigated further — a genuinely new, separate thread worth its own session.

Repro: `cd /data/Code/tew/.claude/worktrees/getdoubleclicktime && TEW_MAX_STEPS=5000000000 LOG_LEVEL=info LOG_CATEGORIES=exception,seh,startup timeout 120 .venv/bin/python run_exe.py`. Branch: `worktree-getdoubleclicktime` (stacked on `worktree-crt-leak-report-with-free`, PR #12). Full suite passing (1197 tests).

**Next**: investigate why `0x004d980f` faults unrecovered on `tid=1011` but self-recovers on `tid=1000` — likely a thread-context/stack difference in how SEH frame lookup resolves for this address, not a leak-report-specific bug.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
- **CORRECTION (2026-08-30), supersedes `status_archive.md`'s x12/2026-08-25 and 2026-07-24 entries**: "restart the compositor in place" (`kwin_wayland --replace ...`, or `systemctl --user restart plasma-kwin_wayland.service`) is **no longer a valid stuck-SDL troubleshooting step** -- confirmed tonight (2026-08-30) that a compositor restart crashes the *entire user session*, not just the wedged tew client, twice in a row. Whatever made this a safe in-place recovery in 2026-07-24/2026-08-25 no longer holds (environment/KWin-version drift, most likely -- not investigated further). Do not attempt a compositor restart as an automated recovery step; if a run hangs at SDL2/window init, check for and clean up orphaned `run_exe.py` processes first (`SIGTERM`, not `-9`, to let `window_manager.shutdown()` run), and otherwise stop and ask Molly rather than touching the compositor.
