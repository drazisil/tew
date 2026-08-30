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

## Current status (2026-08-30, cont'd x45) — RESOLVED: the `crtReportHookCallback` `INT 3`/memleaksCRT.txt mystery was tew's own exception-swallowing bug, not a guest gap. Fixed. **The real remaining blocker is the x42 heap-exhaustion ceiling itself** — now reliably visible instead of silently vanishing, which is real progress but means it needs an actual fix (not just better diagnostics) before any run can get past it.

**What x44 found and fixed, in one line each** (full narrative in `status_archive.md`, "Previous status (2026-08-30, cont'd x44)"): a D3D8/Vulkan instance-extension bug (`_platform_vulkan_extensions()` enabling both `VK_KHR_xlib_surface` and `VK_KHR_wayland_surface`) was causing a real `SIGSEGV` on every live run past ~14s, wrongly diagnosed earlier as an NVIDIA driver bug — fixed to match `SDL_GetCurrentVideoDriver()`'s live answer. With that cleared, `crtReportHookCallback`'s `INT 3` was bisected (13 `cpu_add_logpoint` probes plus a manual `cpu.run(1)` single-step trace) down to `state.simple_alloc()` hitting the heap ceiling and raising a plain `RuntimeError` deep inside a nested Win32-handler callback — which `CPU.handle_exception` (`cpu_zig.py`) used to silently swallow (no log, `cpu.halted` set but not `fatal_halt`, so `_invoke_emulated_proc`'s own cleanup cleared it and execution drifted into unrelated code). Fixed: `handle_exception` now always logs the caught exception and sets `fatal_halt`. Confirmed live — the run now stops cleanly with the full original error message at the exact point of failure, every time, wherever it happens.

**What this means going forward**: the heap-exhaustion ceiling (bump allocator, no reclaim, hits `THREAD_STACK_BASE` on a sufficiently long run) is no longer a rare, hard-to-reproduce mystery — it will now visibly and reliably stop *any* run once cumulative allocation gets large enough (confirmed hit at both a 2040-byte and a 4144-byte allocation across different runs tonight, at different points in the same ~85-100s window). This was always the real, open problem since x42; tonight just made it impossible to miss. Next session's actual task: fix the heap exhaustion itself — options noted in x42's original writeup are real `free()`/reclaim support, enlarging the heap region, or something else; not yet decided.

**Also fixed along the way** (real, but confirmed inert for `AppendToCRTLeaksFile`'s own call chain, which bypasses the `msvcrt.dll` IAT entirely via direct internal addresses — worth having for any code path that genuinely calls through the public names): `_fopen`'s handler always used `CREATE_ALWAYS` disposition regardless of mode, silently truncating append-mode opens — fixed to `OPEN_ALWAYS` + seek-to-EOF (`msvcrt_handlers.py`). Registered the missing underscore-prefixed `_fputs`/`_fclose` handlers.

**Report hook**: left enabled (the x42/x43 `_CRT_REPORT_HOOK_PTR` disable-workaround stays removed) — with the exception-handling fix in place, the crash-diagnostic dump now stops loudly at the real heap-ceiling error instead of ever silently reaching the hook's `INT 3`.

Repro: `cd /data/Code/tew && LOG_LEVEL=debug LOG_CATEGORIES=com,cpu,exception,startup,handlers,fileio,seh,d3d8 timeout 300 .venv/bin/python run_exe.py`. Branch: `crt-leak-report-on-crash`. Full suite passing (1183 tests). All 8 `cpu_add_logpoint` slots freed (see `run_exe.py`) — the 8-slot cap itself is still open, see `TODO.md`.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
- **CORRECTION (2026-08-30), supersedes `status_archive.md`'s x12/2026-08-25 and 2026-07-24 entries**: "restart the compositor in place" (`kwin_wayland --replace ...`, or `systemctl --user restart plasma-kwin_wayland.service`) is **no longer a valid stuck-SDL troubleshooting step** -- confirmed tonight (2026-08-30) that a compositor restart crashes the *entire user session*, not just the wedged tew client, twice in a row. Whatever made this a safe in-place recovery in 2026-07-24/2026-08-25 no longer holds (environment/KWin-version drift, most likely -- not investigated further). Do not attempt a compositor restart as an automated recovery step; if a run hangs at SDL2/window init, check for and clean up orphaned `run_exe.py` processes first (`SIGTERM`, not `-9`, to let `window_manager.shutdown()` run), and otherwise stop and ask Molly rather than touching the compositor.
