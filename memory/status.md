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

## Current status (2026-08-26, cont'd x34) — Found and fixed the real cause of the DAO license-key BSTR bug: statically-imported real DLLs never ran their own `DllMain`. Fix implemented; now working through a new DLL's worth of previously-dormant, never-before-exercised code hitting missing handlers.

**Root cause, traced all the way down**: `Ordinal_2`/`SysAllocString` (real `oleaut32.dll`) returned NULL for a perfectly valid string. Decompiling it live (see `status_archive.md` x32 for the trace) showed it defers to `SysAllocStringLen`, which lazily bootstraps a per-thread OLE-automation state block on first use via a TLS slot (`DAT_771a1000`). That slot was stuck at `0xFFFFFFFF` (`TLS_OUT_OF_INDEXES`) because `TlsAlloc()` -- called only from `oleaut32.dll`'s own real `DllMain` (`FUN_771215d4`, reason `DLL_PROCESS_ATTACH`) -- never ran at all. Confirmed via a live "log every DllMain call" pass: the only `DllMain` invocations all session are 3 runtime-`LoadLibraryA`-loaded DLLs plus `dao350.dll` (via `_ensure_dll_ready`) -- **zero** for any of the 4 DLLs `MCity_d.exe` statically imports (`d3d8.dll`, `oleaut32.dll`, `rpcrt4.dll`, `secur32.dll`).

**Why**: `should_invoke_dependency_dllmain`'s own docstring (`dll_loader.py`) documents this as a *deliberate* scoping choice from when the `on_dependency_loaded` mechanism was first built (2026-08-16, to fix `msjint35.dll`'s HINSTANCE global) -- "callers that never pass one -- e.g. startup-time static-import loading -- get the pre-fix behavior completely unchanged." `import_resolver.py`'s `build_iat_map` (the static-import path) never passed a callback, so this was never wired up for it.

**Fix implemented** (branch `fix/static-import-dllmain`): `build_iat_map` now accepts an `on_dependency_loaded` callback and applies the exact same `should_invoke_dependency_dllmain` check `load_dll` already uses for nested dependencies -- so dependency-before-dependent ordering falls out for free (a DLL's own recursive `load_dll` call, and thus its own DllMain, always completes before its dependent's does). `run_exe.py` passes `_pending_dllmain_dlls.append` as that callback, then actually invokes `_invoke_dependency_dllmain` for each collected DLL **after** the main thread's stack/kernel-structures are initialized (not right after `build_iat_map`/`write_iat_handlers` -- tried that first, crashed: `_invoke_emulated_proc` builds its nested call frame on top of the current `ESP`, which is still 0 that early) but still strictly before the guest's own entry point starts running, matching real Windows load-then-attach-then-run ordering.

**Consequence, expected and desired**: every one of these 4 DLLs' real `DllMain` now executes for the first time ever in this emulator, which means a long tail of previously-dormant code is exercising real Win32 APIs never hit before. Two found and fixed already: `GetSystemTimeAsFileTime` (`kernel32_io.py`, no handler existed at all) and (earlier, same investigation) `msvcrt.dll!wcslen`. **Current blocker**: `kernel32.dll!LoadLibraryExW` is unimplemented, hit by `d3d8.dll`'s own real `DllMain` (the dgVoodoo/Rayman shim at `/data/Downloads/rayman_d3d8/d3d8.dll` -- unrelated to the DAO/oleaut32 investigation itself, just newly reachable). Molly's call to keep fixing these one at a time rather than skip `d3d8.dll` or pre-survey its `DllMain` first.

**Not yet re-verified**: whether `oleaut32.dll`'s `TlsAlloc`/`SysAllocString` bug is actually fixed end-to-end -- blocked on getting past `d3d8.dll`'s `DllMain` first (it runs before `oleaut32.dll`'s in dependency order), so the original DAO license-key bug hasn't been re-tested against the fix yet.

**Also newly discovered, unrelated regression (pre-existing, not caused by this fix)**: 101 unit tests in `tests/unit/api/test_oleaut32_*.py` fail against the last commit on `main` (`02d7c46`) too -- they call `stubs.get("oleaut32.dll", "Ordinal #N")` directly, which now raises `KeyError` since the `_NoOleaut32Stubs` wrapper (previous entry) silently drops every `"oleaut32.dll"` handler registration. Confirmed via `git stash` that this predates today's `build_iat_map`/`DllMain` work. Not yet fixed -- these tests test the now-intentionally-unused Python handlers directly; they need to either be deleted (if those handlers are genuinely dead code now) or reworked to test through the real-DLL path instead.

Repro: `cd /data/Code/tew && TEW_WATCH_ADDR=82bfa60 TEW_FIXED_HEARTBEAT_MS=100 TEW_MAX_STEPS=5000000000 LOG_LEVEL=debug LOG_CATEGORIES=cpu,startup,loader,com,handlers timeout 120 .venv/bin/python run_exe.py`.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- `CreateThread`'s log line (`tew/api/kernel32_io.py`) downgraded from `info` to `debug` -- was disproportionately noisy for a routine per-spawn event at `info` level.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
