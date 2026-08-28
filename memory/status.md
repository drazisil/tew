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

## Current status (2026-08-28, cont'd x38) — `StockAssembly_SelectAPT`'s `Parameters.Count` failure traced all the way into real Jet SQL-compiler internals (`expsrv.dll`/`msjet35.dll`); two more real bugs found and fixed along the way, neither is the root cause; still open.

**Two real fixes this session, both independently verified, neither resolves the actual blocker**:

1. **`kernel32.dll!GetEnvironmentStringsW`/`GetEnvironmentStrings` returned hardcoded addresses (`0x002100F0`/`0x002100F8`) that were never written to** -- both addresses fall inside the live INT-0xFE trampoline dispatch table (`0x00200000`-`0x0021FFFF`), so any real CRT code reading them back (e.g. `_CRT_INIT`'s env-block scan) read Win32-handler dispatch machine code as if it were string data. Root-caused as why `MSJINT35.dll`'s `DllMain` was returning FALSE. Fixed: both now lazily allocate real memory (via `state.simple_alloc`) and write a valid, empty (`\0`) double-null-terminated block.
2. **`_invoke_dependency_dllmain` (the mechanism that runs a recursively-loaded dependency DLL's own `DllMain`) fires *before* `patch_dll_iats` has patched that DLL's own IAT** -- `dll_loader.py`'s recursive `load_dll` walk calls the dependency-DllMain callback mid-walk, but `patch_dll_iats` only runs as a separate, later pass. So `MSJINT35.dll`'s `DllMain` called `GetVersion`/`GetCommandLineA`/etc. through unpatched (garbage/zero) IAT slots instead of our registered handlers, "genuinely completing" almost instantly with a leftover `EAX` that looked like a real `FALSE` return. Fixed: `_invoke_dependency_dllmain` now calls `dll_loader.patch_dll_iats(memory, stubs)` on entry (cheap/safe -- it's cursor-based, only processes newly-added entries) before invoking `DllMain`. Confirmed live: `MSJINT35.dll`'s `DllMain` now returns `1`/TRUE, `LoadStringA` returns real resource text (`"Syntax error in date"`, DAO-3075's `"|1 in query expression '|2'."`) instead of empty strings.
3. **`kernel32.dll!WriteFile` and `msvcrt.dll!_write`'s non-overlapped path used `os.write(entry.fd, data)` (implicit, kernel-fd-tracked position) instead of `entry.position`** -- `_llseek`/`_lseek`/`SetFilePointer` only ever update `entry.position`, they never call `os.lseek()` on the real fd, so the moment any seek happens on a handle, a subsequent plain `WriteFile`/`_write` silently lands wherever the real fd's own kernel cursor happens to be, not where `entry.position` says it should. Confirmed live via `~/.emu32/showplan.out` (JETSHOWPLAN diagnostic output, real Jet SQL-compiler plan dump): a later, shorter write partially overwrote a longer earlier line, leaving a garbled fragment (`edPart.PartTypeID`, the tail of `BrandedPart.PartTypeID` with its first 5 bytes clobbered); a separate spot lost an entire query's own `--- QueryName ---` header + index-stats lines outright. Fixed: both now use `os.pwrite(entry.fd, data, entry.position)`, matching the already-correct explicit-position pattern reads use elsewhere in this codebase. Re-verified: `showplan.out` is now clean for every write in a fresh run (old pre-fix corruption at the top of the file persists since the file isn't truncated between runs -- separate, minor, not investigated).

**Neither fix touches the actual `StockAssembly_SelectAPT` blocker** -- confirmed via live re-run after each: identical `HRESULT=0x800a0c03` (DAO error 3075), identical halt.

**Full mechanism traced end-to-end, via a chain of live probes correlated against the exact failing call's timestamp window** (retracing the same technique the earlier `StockVehicleAttributes_SelectAll2`/`Fields.Count` investigation used):

```
DBParamQuery::DBParamQuery (MCity_d.exe 0x00995970) -- three vtable calls:
  0x00995b1c = get_Parameters (succeeds, this out-param confirmed via decompile)
  0x00995c7b = get_Count (FAILS here, HRESULT=0x800a0c03)
 → dao350.dll thunk (FUN_0447dfe2): forwards to *(this+8)'s vtable slot 0x24
 → real get_Count implementer (FUN_0447dc1c) -- same function the Fields.Count
   investigation already found; reads a raw `+0x2c` count field
 → refresh gate (FUN_044d26ce) -- type-indexed dispatch table (DAT_044770b0),
   type_idx=25=Parameters, only calls the real handler when count not cached
 → per-type populate handler (FUN_044c69bc) -- allocates a 68-byte buffer from
   dao350.dll's own free-list pool allocator (FUN_044e2b5c); confirmed live the
   allocation SUCCEEDS (ruling out an earlier false lead -- see below)
 → name-based lookup (FUN_044d525b), given the query's own name directly:
   lpcstr='StockAssembly_SelectAPT'
 → dynamically-bound call into real msjet35.dll (DAT_044e52c8 = 0x1705ff40,
   confirmed live -- two EARLIER calls through this exact same pointer for
   OTHER queries succeeded (EAX=0) this same run, ruling out a structural
   code-path bug)
 → real msjet35.dll dispatcher (FUN_7a89ff40) -- name validation passes
   (FUN_7a8536a6 succeeds), reaches the real dispatch target
 → FUN_7a89fd45 → FUN_7a862215, the real Jet SQL execution-plan compiler
   (same JETSHOWPLAN code path, reads SOFTWARE\Microsoft\Jet\3.5\Engines\Debug)
 → returns raw internal error -3100 (0xfffff3e4)
 → FUN_044d418f (dao350.dll's real DAOError-formatting plumbing, confirmed
   NOT a plumbing bug -- see below) translates -3100 into DAO error 3075 via
   MSJTER35.DLL's real ordinal #5, producing the observed HRESULT
```

**False lead ruled out live**: initially suspected `FUN_044e2b5c` (the pool allocator) was returning NULL for the 68-byte request. Live probe confirmed it succeeds (`EAX=0x7309c4c`, a real pointer) -- the pool keeps serving many more allocations immediately after ours in the same run. The earlier "neither dynamically-bound branch fires" observation that led to this false lead was itself an artifact of the **8-logpoint-slot cap silently dropping registrations past the limit** (`cpu/src/core.zig`: `lp_eip: [8]u32`/`lp_cb: [8]?LogpointFn`, fixed-size FFI-struct arrays; `cpu_add_logpoint` in `kernel.zig` just returns with no error when all 8 slots are full) -- had 9-10 active logpoints at the time from stacking new probes on top of stale ones from earlier, already-resolved investigations (`CoGetMalloc`/`TlsSetValue`/`CoSetState`/`TlsAlloc` from the 2026-08-26 DllMain milestone work, `createinstancelic-*` from the original BSTR bug). Pruned to 5-8 active at any time going forward. **Not yet fixed**: `cpu_add_logpoint` should fail loudly (return a bool / log) when full instead of silently discarding -- flagged, deferred, see `TODO.md`.

**Also confirmed NOT the bug, via the earlier-session (2026-08-25/26-ish) `DumpErrors`/`Error.Description` investigation**: the `CreateErrorInfo`/`SetErrorInfo`/`GetErrorInfo` OLE rich-error-info plumbing (`oleaut32.dll` ordinals 201/202) was already implemented in a prior session and confirmed working -- but `Error.Description` for this error class comes back as a real, validly-allocated, genuinely zero-length BSTR (not a plumbing bug, that's what real Jet actually produces for DAO-3075). `DBParamQuery`'s own `get_Count` failure branch doesn't even call `GetErrorInfo` anyway -- it aborts with a hardcoded format string directly.

**Open, next session should continue here**: `FUN_7a862215`'s real return value traces to `local_44 = FUN_7a85e7e1(local_18, local_1c, local_14[0x1f])` -- not yet live-probed. This is genuine, deep, undocumented Microsoft Jet SQL-compiler internals now (hundreds of lines, dozens of sub-calls, several early-return branches on negative sub-results) -- same `JETSHOWPLAN` code path the `StockVehicleAttributes_SelectAll2`/`Fields.Count` investigation also reached, which concluded its own root cause was upstream in multi-table `Table.Column`-qualified-reference tokenization, never fully located. `StockAssembly_SelectAPT` never appears as its own top-level plan in `showplan.out` (even after the write-corruption fix) -- consistent with compilation failing before a plan gets written, i.e. before `FUN_7a862215` would call whatever writes the `--- QueryName ---` header.

Repro: `cd /data/Code/tew && TEW_FIXED_HEARTBEAT_MS=100 TEW_MAX_STEPS=5000000000 LOG_LEVEL=debug LOG_CATEGORIES=cpu,startup,loader,com,handlers timeout 300 .venv/bin/python run_exe.py`. Active `run_exe.py` logpoints for this investigation (5 of 8 slots): `_dbparamquery_getcount_pre_probe` (0x00995c7b), `_dbparamquery_getcount_return_probe` (0x00995c7e), `_refresh_gate_entry_probe` (0x044d26ce), `_param_lookup_probe` (0x044d525b), `_pool_allocator_entry_probe`+`_pool_allocator_return_probe` (0x044e2b5c/0x044d5271) -- plus `_jet_lookup_returnA_probe`/`_jet_lookup_returnB_probe` (0x044d529f/0x044d52be) currently also active, at exactly 8. Grep `run_exe.py` for `2026-08-28` for the full trail with addresses and reasoning.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- `CreateThread`'s log line (`tew/api/kernel32_io.py`) downgraded from `info` to `debug` -- was disproportionately noisy for a routine per-spawn event at `info` level.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
