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

## Current status (2026-08-26, cont'd x35) — MILESTONE: the DAO license-key BSTR bug (this whole session's original goal) is fixed and confirmed end-to-end. Game now runs real single-race gameplay DB traffic. New, unrelated, later-stage blocker found: `SearchPathW` unimplemented, deep in `expsrv.dll`/`MSJET35.DLL` typelib code.

**Confirmed fixed, live**: with statically-imported DLLs' `DllMain` now running (previous entry), `oleaut32.dll`'s `TlsAlloc` succeeds (`dwTlsIndex=0x4`, not `0xFFFFFFFF`), and `Dbcode_InitDao`'s `Ordinal_2`/`SysAllocString` call now returns a real heap BSTR pointer (e.g. `0x06fa0814`) instead of `NULL`/`0xCCCCCCCC`. `dblog.txt` now shows the game proceeding straight past DAO init into real gameplay: `DB_StartUpDatabase`, `DBServiceRequestQ` handling `DBT_GO_SINGLERACE`/`DBT_STARTUP`/`DBT_GET_GAMECONFIG_CAR_TABLE`, `DBPhysics_GetTireAuxData`, `DBMem_Alloc`. `stdout.txt` shows only the two known-benign "class has not been licensed" lines (see false-leads section above) -- no more `Database initialization failed!`. Run now reaches 60+ seconds before the next halt, vs. ~2-3s before this fix.

**Handlers added/fixed working through the newly-exercised `DllMain` code for all 4 statically-imported DLLs** (`d3d8.dll`, `Secur32.dll`, `RPCRT4.dll`, `OLEAUT32.dll`, invoked in that dependency order):
- `kernel32.dll!GetSystemTimeAsFileTime` (`kernel32_io.py`) -- no handler existed at all.
- `kernel32.dll!LoadLibraryExW` (`kernel32_handlers.py`) -- added alongside a fix to `LoadLibraryExA`/`LoadLibraryExW`'s `dwFlags` handling: search-scope-only flags (`LOAD_LIBRARY_SEARCH_SYSTEM32` etc.) are now ignored rather than halting, since they only affect *where* Windows looks for the DLL, which is irrelevant to `dll_loader`'s own fixed, non-attacker-influenced search path.
- `kernel32.dll!InitializeSListHead`, `kernel32.dll!CreateEventW` (`kernel32_io.py`/`kernel32_sync.py`) -- straightforward, matched existing `A`-suffix/critical-section patterns.
- `ntdll.dll!RtlInitializeCriticalSection`, `RtlInitializeCriticalSectionAndSpinCount`, `RtlInitializeResource`, `RtlAcquireResourceExclusive`, `RtlReleaseResource` (`kernel32_sync.py`) -- first-ever `ntdll.dll`-exported (not `INT 0x2E` syscall) handlers in this project; `RtlInitializeCriticalSection`/`AndSpinCount` reuse the same struct-init logic as their kernel32 wrapper counterparts (real Windows forwards one to the other) but correctly return NTSTATUS (0) instead of BOOL. Resource-lock acquire/release don't model real contention (single-threaded at every instruction boundary here) -- acquire always succeeds immediately.
- `user32.dll!wsprintfA` (`user32_handlers.py`) -- reuses msvcrt's existing shared `_sprintf_format`/`_write_cstring` engine.
- `user32.dll!RegisterClipboardFormatA` (`user32_handlers.py`) -- simple name->ID table starting at `0xC000`, matching real Windows' app-registered-format convention.
- `kernel32.dll!GetSystemDirectoryA` (`kernel32_io.py`) -- mirrors the existing `GetWindowsDirectoryA` pattern, returns `C:\WINDOWS\SYSTEM32`.
- `ole32.dll!CoSetState` (`oleaut32_handlers.py`) -- the actual call inside `oleaut32.dll`'s lazy per-thread automation-state init (`FUN_77125311`) that was failing; a real no-op returning `S_OK` is sufficient (undocumented internal API, no real cross-apartment state tracked here).

- `kernel32.dll!SearchPathA`/`SearchPathW` (`kernel32_io.py`) -- implemented standard Win32 search sequence (App dir, CWD, System32, Windows, PATH) and custom `lpPath` search; verified live resolving `C:\WINDOWS\SYSTEM32\expsrv.dll`.

`d3d8.dll`'s own `DllMain` returns `0` (FALSE/failure) -- not investigated further since nothing downstream currently depends on it succeeding, but worth remembering if a d3d8-related bug shows up later.

**New blocker, unrelated to tonight's bug**: `msvcrt.dll!wcsncpy` is unimplemented, hit ~61.3s in, called by `OLEAUT32.dll` to copy the resolved typelib DLL path from `SearchPathW` (EBP chain: `expsrv.dll+0x1cbd7` -> `expsrv.dll+0x9d1b` -> `OLEAUT32.dll+0x863d` -> `OLEAUT32.dll+0x38f82` -> `OLEAUT32.dll+0x6f028` -> `OLEAUT32.dll+0x6ef0d` -> `msvcrt.dll!wcsncpy`).

Repro: `cd /data/Code/tew && TEW_WATCH_ADDR=82bfa60 TEW_FIXED_HEARTBEAT_MS=100 TEW_MAX_STEPS=5000000000 LOG_LEVEL=debug LOG_CATEGORIES=cpu,startup,loader,com,handlers timeout 120 .venv/bin/python run_exe.py`.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- `CreateThread`'s log line (`tew/api/kernel32_io.py`) downgraded from `info` to `debug` -- was disproportionately noisy for a routine per-spawn event at `info` level.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
