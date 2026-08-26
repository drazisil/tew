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

## Current status (2026-08-26, cont'd x32) — Real root cause found and fixed: a genuinely-loaded `oleaut32.dll` was being unconditionally shadowed by this project's own Python handlers. New, different, legitimate blocker now surfaced: a real in-game DB-init assertion.

**The whole `LoadTypeLibEx`/expression-function investigation (see status_archive.md, x9-x31) was chasing a fake symptom.** `oleaut32.dll` genuinely loads as real code here -- but `oleaut32_handlers.py`'s ~35 registered Python handlers unconditionally won over it every time (`dll_loader.py`'s `patch_iat_entry` tries a registered handler before ever checking a real DLL's own export). Fixed by wrapping `register_oleaut32_ole32_handlers`'s `stubs` so every `"oleaut32.dll"` registration it makes is silently dropped -- real code now handles all of it.

**Two more real bugs this exposed, both fixed**: (1) `run_exe.py`'s `build_iat_map()` ran before the `~/.emu32/WINDOWS/System32/` search path was registered, so `MCity_d.exe`'s own early, direct `oleaut32.dll` import permanently cached as unresolved -- moved the search-path registration earlier. (2) `msvcrt.dll!wcslen` had no handler at all (never previously exercised) -- added, matching `strlen`'s pattern.

**Current blocker**: with real `oleaut32.dll` now genuinely running (confirmed via a real `OLEAUT32.dll+0x4bba` return address on the stack), the emulator reaches a **legitimate, real `INT3` assertion inside `MCity_d.exe` itself** at ~40.6s (`tid=1000`). The game's own `~/.emu32/MCity/stdout.txt` gives the real reason directly: `Nfs.c(677) Database initialization failed!` / `nfspc.c(1164) NFS_abortmsg callback 'Failed to initialize database. Please be sure you have setup the DCOM and DAO drivers provided on your installation disk...'`. **Not yet investigated.** Unclear whether this is related to the original `expsrv.dll`/locale-info crash chain at all -- that code path may not even be reached anymore now that real `oleaut32.dll` runs instead of the old trap objects.

**Method note, worth remembering generally**: before spending an entire session investigating why a specific DLL's function "fails" or "returns garbage," check whether that DLL is actually loaded as real code or being intercepted by a Python handler *first* -- not after building an elaborate fix for the wrong layer. `dll_loader.py`'s `patch_iat_entry` docstring already documents the precedence (handler > real export > auto-stub); this should be habit, not something to discover by being asked directly.

**Breakpoints/logpoints in `run_exe.py` right now**: mostly stale from the now-obsolete LoadTypeLibEx investigation (`_fields_probe`, `_fields_count_probe`, `_expsrv_vtable_call_probe`, `_locale_info_object_probe`, `_beginthread_call_probe`, `_crypto_object_probe`, `_beginthreadex_entry_probe`, plus a couple logpoints) -- needs a housekeeping pass before the next investigation needs the slots, since none of them are relevant to the DB-init assertion.

Repro: `cd /data/Code/tew && TEW_FIXED_HEARTBEAT_MS=100 TEW_MAX_STEPS=5000000000 LOG_LEVEL=debug LOG_CATEGORIES=cpu,startup,loader,com timeout 120 .venv/bin/python run_exe.py`.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- `CreateThread`'s log line (`tew/api/kernel32_io.py`) downgraded from `info` to `debug` -- was disproportionately noisy for a routine per-spawn event at `info` level.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
