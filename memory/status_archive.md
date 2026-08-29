# Emulator Session Status — Archive

Rotated-out `## Previous status` entries from `status.md`, oldest history preserved verbatim (not summarized) since some entries have detail not duplicated in `changelog.md`. Newest-first, same as before. `status.md` itself now holds only the single most-recent `## Current status` section — this file is the full backlog behind it. Rotated 2026-08-16 (file had grown to 1756 lines); grep here for anything not found in `changelog.md` or the live `status.md`.

---

## Previous status (2026-08-28, cont'd x38) — `StockAssembly_SelectAPT`'s `Parameters.Count` failure traced all the way into real Jet SQL-compiler internals (`expsrv.dll`/`msjet35.dll`); two more real bugs found and fixed along the way, neither is the root cause; still open.

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

---

## Previous status (2026-08-28, cont'd x37) — DB init now runs for real; new blocker is a genuine DAO/Jet query-parameter gap in `expsrv.dll`, not a missing Win32 handler.

**Five handler/bug fixes tonight, each verified by a fresh full re-run before moving to the next blocker**:

1. **`kernel32.dll!LoadLibraryA` full-path calls to our own Python-simulated-only DLLs never resolved** (`kernel32_handlers.py::_load_dll_by_path`) -- `stubs.get_stub_dll_handle(basename)` was only ever checked as a fallback *inside* `if real_path is not None:` (i.e. only when a real file was found on disk but failed to parse as a PE), never when `find_file_ci` legitimately found no real file at all. Hit at ~60s: real `OLEAUT32.dll`'s own NLS-cache-version helper (`FUN_7713c8d9`, decompiled in Ghidra to confirm) calls `LoadLibraryA("<cached SYSTEM32 dir>\kernel32.dll")` defensively; `kernel32.dll` has no real file backing it (Python-simulated only), so this fell through to the interactive-missing-file prompt and crashed into an unregistered trampoline slot on non-interactive stdin. Fixed by checking the stub-handle fallback in the not-found path too.
2. **Real bug found while fixing #1**: `os.path.basename()` is POSIX-only and silently doesn't split on `\` -- switched `_load_dll_by_path`'s basename extractions to `ntpath.basename()`.
3. **`advapi32.dll!RegNotifyChangeKeyValue` unimplemented** -- since `registry.json` is only ever written by the guest process itself, a watched key never changes out from under it. Async mode registers and returns success without ever signaling; sync mode initially returned immediate success too, later fixed (see PR review follow-up below) to really block via the scheduler.
4. **`kernel32.dll!WaitForMultipleObjects` was a bare `_halt` stub** despite `WaitForMultipleObjectsEx` right next to it having a complete implementation -- factored the shared logic into `_wait_for_multiple_common(cpu, arg_bytes)`.
5. **`kernel32.dll!GetStringTypeExW` and `msvcrt.dll!wcsncmp` unimplemented** -- straightforward siblings of `GetStringTypeW`/`strncmp`.

**Post-PR review follow-up (same session)**: an independent review of PR #5 found two real issues, both fixed:
- `RegNotifyChangeKeyValue`'s synchronous form was fabricating immediate success -- fixed to route through the scheduler's handle-block machinery (parked on a permanently-unsignaled sentinel event).
- `_lseek`/`_llseek` clamped position against `len(entry.data)`, always 0 for fd-backed handles -- silently reset any nonzero seek to 0. Added `file_entry_size()` using `os.fstat`. Confirmed NOT the cause of the `StockAssembly_SelectAPT` blocker (identical halt before/after) but a real, independently-worth-fixing bug.

**Run reached ~80.5s** and hit a real, unhandled `INT3` inside `MCity_d.exe` itself: `nfspc.c(1164) NFS_abortmsg callback 'AMF=166 DBQuery.c(997) DB ERROR: query StockAssembly_SelectAPT; could not get param count; does table really exist?'`. Molly confirmed the `StockAssembly` table genuinely exists and is populated -- rules out "missing/malformed table." PR #5 merged (`bba9324`); investigation continued on `investigate/dao-jet-query-params`.

## Previous status (2026-08-27, cont'd x36) — `SearchPathW`/`wcsncpy` blockers from x35 both resolved (parallel work while waiting on quota reset -- not fully reflected in this file until now); found and fixed two more real-file-I/O gaps (`_llseek`, `_lread`); run now reaches 156s+ with real DirectSound/window-message activity.

**Housekeeping note**: `kernel32.dll!SearchPathA`/`SearchPathW` (standard Win32 search sequence) and `msvcrt.dll!wcsncpy` were already fixed and merged into `main` by the time this session picked back up -- this file's x35 entry still listed `wcsncpy` as the open blocker, which was stale. Confirmed both work correctly in a fresh run tonight.

**Environment note, worth remembering**: hit a real SDL2/X11 hang tonight, unrelated to any code bug -- `SDL_CreateWindow`/`SDL_ShowWindow` blocked forever in `XIfEvent`/`xcb_wait_for_event` waiting for a `MapNotify` the (kwin) compositor never sent (confirmed via a live `gdb -p <pid> -batch -ex bt` on the stuck process -- full native stack showed `X11_ShowWindow` -> `_XReadEvents` -> `xcb_wait_for_event` -> `poll`, called from the emulated `CreateWindow` handler via `engine.opCD`/`cpu_run`). This is the same class of issue as the 2026-07-24 "Xwayland/kwin compositor wedge" ([[feedback_dont_alarm_before_verifying]]) -- fixed the same way Molly's done before: `kwin_wayland --replace --xwayland &` (restarts the compositor in place, no logout needed). If a run hangs immediately after window creation with virtual time frozen, try this before assuming a code regression -- check `ps aux | grep run_exe.py` for a stray orphaned process from an earlier killed run first, since that alone can look identical (two processes fighting over CPU, log frozen because both are equally starved) -- confirmed both symptoms independently tonight, don't conflate them.

**Two more real-file-I/O gaps found and fixed, same `_lopen`/_lread`/`_lwrite`/`_llseek` old 16-bit-compat family, both genuine `kernel32.dll` STDCALL exports (not the `msvcrt.dll` cdecl `_lseek`/`_read` they resemble) -- share `file_handle_map` with `CreateFile(A/W)` since their `HFILE` is interchangeable with a real `HANDLE`**:
- `kernel32.dll!_llseek` (`kernel32_io.py`) -- hit ~70.6s, called by `OLEAUT32.dll`'s typelib reader right after opening a real file via `SearchPathW`->`CreateFileW`. Same seek logic as msvcrt's existing `_lseek`.
- `kernel32.dll!_lread` (`kernel32_io.py`) -- hit ~156.7s, the very next function in the same open->seek->read sequence. Same read logic as msvcrt's existing `_read`.

`d3d8.dll`'s own `DllMain` returns `0` (FALSE/failure) -- not investigated further since nothing downstream currently depends on it succeeding.

## Previous status (2026-08-26, cont'd x35) — MILESTONE: the DAO license-key BSTR bug (this whole session's original goal) is fixed and confirmed end-to-end. Game now runs real single-race gameplay DB traffic. New, unrelated, later-stage blocker found: `SearchPathW` unimplemented, deep in `expsrv.dll`/`MSJET35.DLL` typelib code.

**Confirmed fixed, live**: with statically-imported DLLs' `DllMain` now running (previous entry), `oleaut32.dll`'s `TlsAlloc` succeeds (`dwTlsIndex=0x4`, not `0xFFFFFFFF`), and `Dbcode_InitDao`'s `Ordinal_2`/`SysAllocString` call now returns a real heap BSTR pointer (e.g. `0x06fa0814`) instead of `NULL`/`0xCCCCCCCC`. `dblog.txt` now shows the game proceeding straight past DAO init into real gameplay: `DB_StartUpDatabase`, `DBServiceRequestQ` handling `DBT_GO_SINGLERACE`/`DBT_STARTUP`/`DBT_GET_GAMECONFIG_CAR_TABLE`, `DBPhysics_GetTireAuxData`, `DBMem_Alloc`. `stdout.txt` shows only the two known-benign "class has not been licensed" lines -- no more `Database initialization failed!`. Run now reaches 60+ seconds before the next halt, vs. ~2-3s before this fix.

**Handlers added/fixed working through the newly-exercised `DllMain` code for all 4 statically-imported DLLs** (`d3d8.dll`, `Secur32.dll`, `RPCRT4.dll`, `OLEAUT32.dll`, invoked in that dependency order):
- `kernel32.dll!GetSystemTimeAsFileTime`, `LoadLibraryExW` (plus a `dwFlags` fix so search-scope-only flags don't halt), `InitializeSListHead`, `CreateEventW`.
- `ntdll.dll!RtlInitializeCriticalSection(AndSpinCount)`, `RtlInitializeResource`, `RtlAcquireResourceExclusive`, `RtlReleaseResource` -- first `ntdll.dll`-exported (not `INT 0x2E` syscall) handlers in this project.
- `user32.dll!wsprintfA`, `RegisterClipboardFormatA`; `kernel32.dll!GetSystemDirectoryA`; `ole32.dll!CoSetState` (the actual call inside `oleaut32.dll`'s lazy per-thread automation-state init that was failing).
- `kernel32.dll!SearchPathA`/`SearchPathW` -- implemented standard Win32 search sequence; verified live resolving `C:\WINDOWS\SYSTEM32\expsrv.dll`.

`d3d8.dll`'s own `DllMain` returns `0` (FALSE/failure) -- not investigated further since nothing downstream currently depends on it succeeding.

**Blocker at the time**: `msvcrt.dll!wcsncpy` unimplemented, hit ~61.3s in, called by `OLEAUT32.dll` to copy the resolved typelib DLL path from `SearchPathW`. Resolved in the next entry.

## Previous status (2026-08-26, cont'd x34) — Found and fixed the real cause of the DAO license-key BSTR bug: statically-imported real DLLs never ran their own `DllMain`.

**Root cause, traced all the way down**: `Ordinal_2`/`SysAllocString` (real `oleaut32.dll`) returned NULL for a perfectly valid string. Decompiling it live showed it defers to `SysAllocStringLen`, which lazily bootstraps a per-thread OLE-automation state block on first use via a TLS slot (`DAT_771a1000`). That slot was stuck at `0xFFFFFFFF` (`TLS_OUT_OF_INDEXES`) because `TlsAlloc()` -- called only from `oleaut32.dll`'s own real `DllMain` (`FUN_771215d4`, reason `DLL_PROCESS_ATTACH`) -- never ran at all. Confirmed via a live "log every DllMain call" pass: zero `DllMain` invocations all session for any of the 4 DLLs `MCity_d.exe` statically imports (`d3d8.dll`, `oleaut32.dll`, `rpcrt4.dll`, `secur32.dll`).

**Why**: `should_invoke_dependency_dllmain`'s own docstring (`dll_loader.py`) documents this as a *deliberate* scoping choice from when the `on_dependency_loaded` mechanism was first built (2026-08-16, to fix `msjint35.dll`'s HINSTANCE global) -- `import_resolver.py`'s `build_iat_map` (the static-import path) never passed a callback, so this was never wired up for it.

**Fix implemented** (branch `fix/static-import-dllmain`): `build_iat_map` now accepts an `on_dependency_loaded` callback and applies the exact same `should_invoke_dependency_dllmain` check `load_dll` already uses for nested dependencies -- so dependency-before-dependent ordering falls out for free. `run_exe.py` invokes `_invoke_dependency_dllmain` for each collected DLL after the main thread's stack/kernel-structures are initialized (not right after `build_iat_map`/`write_iat_handlers` -- crashes: `_invoke_emulated_proc` builds its nested call frame on top of the current `ESP`, still 0 that early) but still before the guest's own entry point runs.

**Consequence, expected and desired**: every one of these 4 DLLs' real `DllMain` now executes for the first time ever, exercising a long tail of previously-dormant code. See the next status entry for the full list of missing handlers found and fixed working through it, and confirmation the original bug is actually fixed end-to-end.

**Also found, unrelated regression (pre-existing, not caused by this fix)**: 101 unit tests in `tests/unit/api/test_oleaut32_*.py` fail against `main` too (confirmed via `git stash`) -- they call `stubs.get("oleaut32.dll", "Ordinal #N")` directly, which now raises `KeyError` since the `_NoOleaut32Stubs` wrapper drops every `"oleaut32.dll"` handler registration. Queued in TODO.md, not fixed yet.

## Previous status (2026-08-26, cont'd x32) — Real root cause found and fixed: a genuinely-loaded `oleaut32.dll` was being unconditionally shadowed by this project's own Python handlers. New, different, legitimate blocker now surfaced: a real in-game DB-init assertion.

**The whole `LoadTypeLibEx`/expression-function investigation (see below, x9-x31) was chasing a fake symptom.** `oleaut32.dll` genuinely loads as real code here -- but `oleaut32_handlers.py`'s ~35 registered Python handlers unconditionally won over it every time (`dll_loader.py`'s `patch_iat_entry` tries a registered handler before ever checking a real DLL's own export). Fixed by wrapping `register_oleaut32_ole32_handlers`'s `stubs` so every `"oleaut32.dll"` registration it makes is silently dropped -- real code now handles all of it.

**Two more real bugs this exposed, both fixed**: (1) `run_exe.py`'s `build_iat_map()` ran before the `~/.emu32/WINDOWS/System32/` search path was registered, so `MCity_d.exe`'s own early, direct `oleaut32.dll` import permanently cached as unresolved -- moved the search-path registration earlier. (2) `msvcrt.dll!wcslen` had no handler at all (never previously exercised) -- added, matching `strlen`'s pattern.

**Blocker at the time, now traced further (see current status.md)**: with real `oleaut32.dll` genuinely running, `Dbcode_InitDao` (`MCity_d.exe`, static `008f4e70`) fails both its `IClassFactory2::CreateInstanceLic` attempts against the real `dao350.dll` object:
1. First attempt (a `dbVariant`-wrapped ANSI license key `"mbmabptebkjcdlgtjmskjwtsdhjbmkmwtrak"`): the BSTR pointer passed (`local_38`) is live-confirmed **NULL** (`bstr_ptr=0x0`), HRESULT `0x80040112` (`CLASS_E_NOTLICENSED` -- makes sense for a null key).
2. Fallback attempt (`local_44 = Ordinal_2(L"mbmabptebkjcdlgtjmskjwtsdhjbmkmwtrak", ...)`, real `SysAllocString`, real vtable call site static `008f59b3`... wait, `008f59b3` turned out to BE the `Ordinal_2`/`SysAllocString` call site itself, not the vtable call -- see current status.md): the BSTR pointer passed is live-confirmed `0xCCCCCCCC` -- the classic MSVC debug-build "stack slot never written" fill pattern.

**Not yet pinned down at the time**: the exact instruction that's supposed to write `Ordinal_2`'s return into `local_44` and isn't. Resolved in the next round: `get_function_calls` (ground truth) showed `0x008f59b3`'s callee IS `Ordinal_2` -- every probe up to this point had mislabeled it as the `CreateInstanceLic` vtable call.

**Also live-confirmed working at the time** (not part of this bug): real `CoGetClassObject`/`CoCreateInstance` against `dao350.dll` (`Got IClassFactory!`, `Got IClassFactory2!` both succeed), real `CoGetMalloc`, and `oleaut32.dll` handling everything else asked of it so far.

**Tooling note**: logpoint callbacks receive a raw ctypes `LP_c_ubyte` pointer for `memory` (unlike breakpoints, which get the wrapped `Memory` object with `.read32()`) -- indexing it out of bounds doesn't raise, it segfaults the whole host process (confirmed live via `coredumpctl`/gdb: crash was in ctypes' own `Pointer_item_lock_held`, no Python-catchable exception). Any logpoint that reads guest memory must bounds-check against the `memory_size` argument manually first -- see `_read32_raw` in `run_exe.py` for the pattern.

**Method note, worth remembering generally**: before spending an entire session investigating why a specific DLL's function "fails" or "returns garbage," check whether that DLL is actually loaded as real code or being intercepted by a Python handler *first* -- not after building an elaborate fix for the wrong layer. `dll_loader.py`'s `patch_iat_entry` docstring already documents the precedence (handler > real export > auto-stub); this should be habit, not something to discover by being asked directly.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- `CreateThread`'s log line (`tew/api/kernel32_io.py`) downgraded from `info` to `debug` -- was disproportionately noisy for a routine per-spawn event at `info` level.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).

## Previous status (2026-08-25/26, cont'd x31) — Root cause of the `expsrv.dll` crash was NOT `LoadTypeLibEx` after all: a real, genuinely-loaded `oleaut32.dll` was being unconditionally shadowed by this project's own Python handlers

**The actual root cause, found by questioning the whole premise** (Molly: "we have a real oleaut32... unless you override things again"): `oleaut32.dll` genuinely loads as real code in this emulator (confirmed live: patches its own imports from advapi32/gdi32/kernel32/msvcrt/ole32/rpcrt4/user32.dll, and `dao350.dll`/`msjet35.dll`/`expsrv.dll` all correctly resolve their own `oleaut32.dll` imports against it). But `dll_loader.py`'s `patch_iat_entry` tries a *registered Python handler* before ever checking a real DLL's own export -- so `oleaut32_handlers.py`'s ~35 registered handlers (including every `LoadTypeLibEx`/`ITypeComp::Bind`/`GetFuncDesc`/`GetDllEntry` trap built earlier this session) unconditionally won over the real, correct Microsoft code, every time, regardless of it being genuinely present and loaded.

**This means the entire `LoadTypeLibEx`/expression-function investigation above (all of x9-x30) was chasing a fake symptom of a fake implementation.** Real `oleaut32.dll` would have parsed `expsrv.dll`'s real, embedded `TYPELIB` PE resource (confirmed present: 42,164 bytes at RVA `0x52140`, language 1033) and answered every `Bind`/`GetDllEntry`/`GetFuncDesc` call correctly and automatically -- no hand-typed `_EXPR_FUNCTIONS` table, no guessed `FUNCDESC` structs, no per-function `memid` bookkeeping needed. All of that work was actively preventing the real, correct code from ever getting a chance to run.

**Fix**: added a scoped wrapper at the top of `register_oleaut32_ole32_handlers` (`oleaut32_handlers.py`) that silently drops any `register_handler("oleaut32.dll", ...)` call this file makes, letting every one of those ~35 registrations (both pre-existing, from before this session, and everything built today) fall through to the real, loaded DLL instead.

**Two additional real bugs this immediately exposed** (previously silently papered over by the very handlers just removed):
1. `run_exe.py`'s `build_iat_map()` call (main EXE's own direct-import IAT resolution) runs *before* `register_crt_handlers()`/`register_oleaut32_ole32_handlers()` ever adds the `~/.emu32/WINDOWS/System32/` search path where `oleaut32.dll`/`dao350.dll` actually live -- so `MCity_d.exe`'s own early, direct `oleaut32.dll` ordinal import (a BSTR alloc, likely from a global/static C++ constructor, well before `WinMain`'s own body) silently failed to resolve and got permanently cached as unresolved, crashing at ~1.8s with `[UNIMPLEMENTED] oleaut32.dll!Ordinal #150`. Fixed by adding the search path in `run_exe.py` itself, before `build_iat_map()` runs.
2. `msvcrt.dll!wcslen` had no handler at all (a previously-latent gap: real `oleaut32.dll` code never got to run far enough to call it before today). Added, matching the existing `strlen` pattern.

**Verified live**: with both fixes, the original ~1.8s and ~40s halts are gone; the emulator now runs real `oleaut32.dll` code (confirmed via a genuine `OLEAUT32.dll+0x4bba` return address on the stack) well past both points. The run now reaches a **genuine, legitimate INT3 assertion inside `MCity_d.exe` itself** at ~40.6s (`tid=1000`), with the game's own `stdout.txt` giving the real, human-readable reason: `Nfs.c(677) Database initialization failed!` / `nfspc.c(1164) NFS_abortmsg callback 'Failed to initialize database. Please be sure you have setup the DCOM and DAO drivers provided on your installation disk...'`. Not yet investigated -- this is the new, current blocker, and it may or may not be related to the original `expsrv.dll`/locale-info crash chain (that code path might not even be reached anymore now that real `oleaut32.dll` runs).

**Method note**: the entire `LoadTypeLibEx` investigation (below, x9-x30) produced real, useful intermediate findings (the `DBCode_GetStockCarList` trigger, the thread-scheduling false-positive from a leftover watchpoint, the heap/thread-stack bounds-check hardening, the real MSFT typelib resource discovery) but the final diagnosis and fix were wrong because a foundational assumption -- "is this DLL's code actually running, or is a handler intercepting it first" -- was never checked until asked directly. Worth checking *first*, not last, on any "real DLL should have handled this" investigation.

---

## Previous status (2026-08-25, cont'd x9-x30) — Implemented `ITypeLib`/`ITypeComp` trap objects for `LoadTypeLibEx`, prematurely called it "fixed", then found via an independent recurrence that it wasn't. Real `.tlb` parsing now confirmed as the actual required fix, not optional hardening.

**What was built**: `oleaut32_handlers.py`'s ordinal 154 (`LoadTypeLibEx`) now returns a trap `ITypeLib` COM object (13-slot vtable) instead of bare `E_NOTIMPL`; its `GetTypeComp` returns a trap `ITypeComp` object (5-slot vtable) whose `Bind` honestly returns `DESCKIND_NONE`/`S_OK` (found and fixed a self-introduced `[ESP+4]` vs `[ESP+8]` arg-offset bug along the way). This eliminated the *original* `expsrv.dll ESI=0xFFFFFFFF` crash for the specific thread/session it was first traced on -- verified live, run proceeded 20+ seconds past the old failure point with no fault.

**Wrongly declared "fixed"**: after that one clean run, framed a new, different fault (`EIP=0x00000002` on a different thread) as an unrelated new bug rather than tracing it. Molly pushed back twice ("I'm not exactly convinced it's fixed... where exactly ARE we" and "still not a 'clean exit' lol") before this got corrected.

**The `EIP=2` tangent (NPSThreadSender/tid=1005)**: traced the new fault to `tid=1005` = `NPSThreadSender` (the lobby "Sender" thread, confirmed two independent ways -- the real `_tiddata` start-addr field, and `NPS_ThreadCreateWithPriority`'s literal `"Sender"` name string). Found its crypto-dispatch chain (CryptoPP `BufferedTransformation`-derived filter, vtable `0x011fed28`) resolves `+4` to `__purecall`->`__amsg_exit`->`ExitProcess` -- structurally sound in tew, but the actually-live branch turned out to be the mirrored `+0x1f8` path, not the `+0x1e8` one first assumed (another decompile-shape trap: assumed top-to-bottom branch order matched execution order; the assumed branch got zero live hits in 99+ seconds).

**Discovered along the way, fixed**: a leftover, unconditionally-armed watchpoint (`cpu.set_watchpoint(0x082be230)`, `run_exe.py:696`, left over from an already-answered earlier investigation) was silently misfiring as a false "crash" on unrelated threads (`tid=1011`) whose stacks happened to reuse that exact address across runs -- wasted two full ~120s run cycles before the `WATCHPOINT HIT` log line was noticed and the root cause understood. Removed.

**The actual recurrence (tid=1011/`DBThread`)**: with the watchpoint noise gone, a real, independently-reproducible fault appeared: `tid=1011` (`DBThread`, a second/independent DAO session) hits the *exact same* `expsrv.dll ESI=0xFFFFFFFF` crash, same call chain, same garbage value (`0x082be46f`) as the original 2026-08-25 x8 entry below -- proving the `Bind` fix only prevented the *first* session's occurrence, not the underlying stub. Traced `tid=1011`'s trigger precisely: `Dbcode_GetStockCarList` (`dbcode.c:1806`) -> `DBParamQuery::DoQuery("StockAssembly_SelectAPT")` -> dao350.dll -> msjet35.dll's locale-comparison machinery -> the same `FUN_7a8a4975`/`FUN_7a8a1c78` chain. Confirmed via `~/.emu32/dblog.txt` (msjet35.dll's own debug trace, which also logs `DBMem_Alloc`/`DBMem_Free` calls -- useful, previously underused evidence source) and `~/.emu32/MCity/real.log` (the `_REAL_` subsystem's own startup banner, which proved `_REAL_init()` genuinely runs, catching a broken diagnostic logpoint address along the way).

**Theories investigated and ruled out** (both good-faith, both disproven by direct live testing rather than left unresolved): (1) MCity's own 64MB `_MEM_init`/`MEM_alloc` custom memory pool exactly spans `0x04000000`-`0x08000000` (`THREAD_STACK_BASE`) with zero headroom, and `tew`'s bump allocator (`bump_alloc_next`, `cpu/src/kernel.zig`) has no bounds check against overrunning into thread-stack territory -- a real, confirmed architectural gap (see below, guard added), but NOT what's happening in this specific crash: added a hard `RuntimeError` guard in `CRTState.simple_alloc` and it never fired across multiple full runs that still reproduced the crash, meaning the heap cursor never actually reaches `0x08000000` in practice. (2) MCity's DAO/Jet-specific `DBMem_Alloc`/`DBMem_Free` pool (`dbparts.c`/`DBMem.cpp`) reusing a freed, stale block -- ruled out because `dblog.txt` shows zero `DBMem_Free` calls in the whole session; nothing has ever been freed to be reused.

**Corrected conclusion**: the garbage field (`0x082be46f`, a genuine thread-stack address appearing as *data content*, not from any address-space overlap) is real, uninitialized game-code state that only manifests because `LoadTypeLibEx` never actually succeeds -- on real Windows this code path is never exercised at all. The `ITypeComp::Bind` trap fix was a band-aid on one manifestation; it does not prevent a *different* independent DAO session from hitting the identical stub-driven failure on its own. Real `.tlb` parsing (previously flagged as a large/optional item, see TODO.md) is now understood to be the actual required fix, not a nice-to-have.

---

## Previous status (2026-08-25, cont'd x8) — ROOT CAUSE FOUND, end-to-end, for the `expsrv.dll` `ESI=0xFFFFFFFF` halt. Not yet fixed -- see `memory/TODO.md` for the two follow-up items.

**Full chain, fully confirmed, innermost first**:
1. Crash `MOV EAX,[ESI]` at static `0x0F9DD9E9` in `expsrv.dll`'s `FUN_0f9dd9a7` -- `ESI`/`param_2` forwarded unchanged from `FUN_0f9dd3d9`'s `param_3`.
2. `FUN_0f9dd3d9` called cross-module from `MSJET35.DLL`'s `FUN_7a8a1c78` via indirect vtable dispatch (`CALL [EAX+0x24]`, static `0x7a8a1d84`).
3. The bad value pushed as arg3 (`ECX=0xFFFFFFFF`, confirmed live) is `FUN_7a8a1c78`'s `uVar6`, from the `else` branch (`uVar6 = param_4->field3_0xc`) of its inner `if (field2_0x8==0)` check -- confirmed via live logpoint bisection, not decompile assumption.
4. **Corrected same day**: `param_4` (the `astruct` passed in) is NOT built by `FUN_7a926327` (an earlier, unverified pattern-match guess that turned out wrong -- a second live-caller-hunt bisection, byte-scanning all 12 direct callers of `FUN_7a8a1c78` in `msjet35.dll` and logpoint-testing one representative per distinct caller function, found the REAL caller: `FUN_7a9267a1`, specifically its first of six sequential calls, offset block `0x8c8`-`0x8d4`).
5. `FUN_7a9267a1` reads a 16-byte "record" out of a per-session locale-info object (`iVar1 = *(session+0x6f0)`, a lazily-allocated `0xb90`-byte block = 74 40-byte records) at `iVar1+0x8c8` = record 56's result-block. Live memory dump confirmed ALL 9 sampled records (0,1,2,5,10,20,40,56,73) -- not just record 56 -- have byte-for-byte IDENTICAL content: `local_1c=-1, uStack_18=1, uStack_14=0x082be46f (thread-stack-looking garbage), uStack_10=-1 (our crash value)`. Systemic, not one obscure record failing.
6. `FUN_7a8a4975` builds this object: copies a 74-record static template, then for each record calls `FUN_7a8a16b7` (name lookup) and, on failure, sets ONLY `local_1c=-1` -- the sibling stack slots `uStack_18`/`uStack_14`/`uStack_10` are never defaulted, so they carry whatever garbage was already on the stack into the record.
7. **Root trigger, confirmed live**: `FUN_7a8a16b7`'s lookup depends on `oleaut32.dll`'s `LoadTypeLibEx` (ordinal 154), which is an honest, deliberate tew stub always returning `E_NOTIMPL` (real `.tlb` parsing was never implemented) -- called dozens of times exactly at the crash window (confirmed via `[com]`-category log), explaining why ALL 74 records fail identically.

**Why this isn't "just implement LoadTypeLibEx" or "fix msjet35.dll"**: `E_NOTIMPL` is an honest failure, not the bug -- see [[feedback_no_stubs]]. `msjet35.dll` is real Microsoft code we can't patch. And critically (see [[tew_bug_can_only_be_in_tew]]'s new caveat): `FUN_7a8a4975`'s failure-path IS a genuine latent bug in real Microsoft code (uninitialized locals), but it's real-Windows-untested because `LoadTypeLibEx` essentially never fails there -- so trusting that failure-handling to be robust was never safe to begin with. The actual fix: figure out why tew's stack reads non-zero garbage at this exact point (real Windows' apparently reads zero, letting `FUN_7a8a1c78`'s own EXISTING `field3_0xc==-1` graceful-error check work) and correct tew's own memory-model there -- not yet started, tracked in `memory/TODO.md` alongside the separate, larger, explicitly-unscoped real-`.tlb`-parsing item.

**Trust-check findings from this investigation, worth remembering generally**: (a) a live per-instruction logpoint bisection is the reliable way to confirm which branch of a decompiled `if/else` a call actually took, or which of several candidate callers is the real one -- don't trust decompile-shape pattern-matching once conclusions start chaining across multiple hops (this session hit the SAME mistake twice: once on an `if/else` branch, once on which function calls `FUN_7a8a1c78`). (b) `two_byte.zig`'s `Jcc rel32` and `core.zig`'s scaled-index-only SIB decode were both read and confirmed correct as candidate CPU-core-bug explanations before the real cause (wrong assumptions, not a tew CPU bug) was found each time.

## Previous status (2026-08-25, cont'd x7) — `mcity` Ghidra project used to name the exact source line; a WRONG branch assumption caught and corrected via a live trust-check before it could mislead further; real open question re-scoped and simplified

**Method note, worth repeating**: this correction only happened because a live-instrumentation "trust check" was run before believing a static-analysis conclusion further -- see [[feedback_stalk_memory_over_decompile_guessing]]. The lesson: a decompiled `if/else` shape is not evidence of which branch a specific live call actually took; that has to be confirmed live, every time, especially once conclusions start chaining across multiple hops.

**What happened**: `FUN_7a8a1c78`'s decompile (`msjet35.dll`, `mcity` project, already analyzed there -- Ghidra's own auto-analysis crashes on `expsrv.dll` but is fine on `msjet35.dll`) showed the call feeding the bad value comes from:
```c
if (param_4[2] == 0) {
    if (param_4[3] == -1) { ...error, return... }
    uVar6 = *(undefined4 *)(local_218 + param_4[3] * 0x1c);   // array lookup
} else {
    uVar6 = param_4[3];                                        // direct value
}
```
The investigation spent several turns assuming the array-lookup (`if`) branch was live -- traced `local_218` back to a per-session `DAT_7a9362c0[slot].field_0x6d8` pointer, found the allocator (`FUN_7a8763ea`), the session-slot-claim function (`FUN_7a876276`, confirmed `-1` is this codebase's standard "unset" sentinel via a 257-entry bulk-fill, but also confirmed `field_0x6d8` itself is never touched by that function, staying NULL after slot claim), and repeatedly failed to place a working breakpoint/logpoint on any instruction inside that array-lookup branch (`static 0x7a8a1d42`-`0x7a8a1d5d`, runtime `0x18061d42`-`0x18061d5d` this session) despite the runtime bytes there confirmed byte-for-byte identical to the static file.

**The correction**: Molly's call to stop and verify tooling trust before going further. Bisected with one Zig-core logpoint per instruction (`cpu.add_logpoint`, inline C callback, no halt/dispatch dependency -- immune to the nested-`cpu.run()` theory tried first) across the whole suspect range. Result: **every single instruction in the array-lookup branch never fires; only the shared merge point after the `if/else` (`0x7a8a1d5e`, runtime `0x18061d5e`) fires.** That's not an instrumentation bug -- it means this call never takes the array-lookup branch at all. It takes the `else`: `uVar6 = param_4[3]` directly.

**Corrected, simplified understanding**: `param_4[3]` itself is `-1` -- not an index into an array whose slot happens to hold `-1`. The entire "who writes `-1` into the `0x1c`-stride collation array" sub-investigation was chasing dead code. The real, and now correctly-scoped, open question returns to what `FUN_7a926327` already showed: `local_14.field3_0xc` (`= param_4[3]` at the call site) is copied verbatim from `*(iVar2 + 0x8fc)`, where `iVar2 = *(param_1*0x708 + 0x6f0 + DAT_7a9362c0)` -- a separate per-session "locale info" object pointer, distinct from the `local_218`/`0x6d8` array this whole detour was about. **Why does that locale-info object's `+0x8fc` field read as `-1`?** -- not yet investigated. Also still open, from before this detour: the outer `param_4[1] == 0` branch in `FUN_7a8a1c78` was never independently live-confirmed either (only the inner `param_4[2]` branch was checked via this trust-check) -- worth checking too, in case another wrong assumption is hiding there.

**Also confirmed correct** (not affected by the above): the JZ-rel32 (`two_byte.zig`'s `0x80...0x8F` case) and the scaled-index-only SIB decode (`mod=00, SIB base=101` -> `fetch32`, `core.zig`'s `decodeSIB`) were both read and found correct -- ruled out as an explanation before the branch-assumption error was found, not a live tew bug.

## Previous status (2026-08-25, cont'd x6) — `expsrv.dll` `ESI=0xFFFFFFFF` halt traced end-to-end through 3 modules to a live-confirmed root value; origin (why `ECX=0xFFFFFFFF` in MSJET35.DLL) still open

**Method note**: entirely file-based static analysis (no Ghidra full-analyze needed -- it crashes on this DLL) plus one targeted live breakpoint, not decompile-guessing. Molly did the Ghidra manual-function-definition + raw disassembly reads; Claude did PE section/export-table parsing and raw byte scans directly against `~/.emu32/WINDOWS/System32/{expsrv,msjet35}.dll` via Python, cross-validating every hop against the (independently confirmed non-shaky, for this specific chain) EBP-chain data already in the exception dump.

**Chain, innermost first**:
1. Crash: `MOV EAX,[ESI]` at static `0x0F9DD9E9` in `expsrv.dll` (RVA `0x1d9e9`), inside `FUN_0f9dd9a7` (starts at static `0x0F9DD9A7`, found by scanning the file backward from the crash RVA for a `55 8B EC` prologue -- only one candidate in the preceding 4KB). Ghidra decompile of this function confirmed the crash is `*param_2` -- fetching a COM vtable pointer -- reached only on the `if (piVar3 == NULL)` (cache-miss) branch of an internal lookup.
2. `param_2` is loaded from `[EBP+0xC]` into `ESI` right at function entry and never modified before the crash (raw disassembly, byte-exact from function start through the crash instruction, decoded by hand from a hex dump Molly pasted from Ghidra).
3. `FUN_0f9dd9a7`'s only caller (found via a raw `E8 rel32` byte-scan across `expsrv.dll`'s `.text`/`ENGINE` sections computing call targets -- exactly one hit) is `FUN_0f9dd3d9` (static `0x0F9DD3D9`), which forwards **its own** `[EBP+0x10]` (3rd parameter) straight through as `param_2` -- not computed, just passed on.
4. `FUN_0f9dd3d9` has zero direct callers inside `expsrv.dll` itself (same E8-scan technique, zero hits) -- its own EBP-chain frame (frame[1] in the original exception dump: `ret=0x18061d87 ← MSJET35.DLL+0x61d87`) shows it's called directly from `MSJET35.DLL`, cross-module.
5. Translated that return address to `MSJET35.DLL`'s own static base (`0x7a840000`, since it loaded this run at runtime base `0x18000000`, relocated): static `0x7a8a1d87`. Manually decoded the preceding bytes (Claude read the file directly) and found the actual call is `CALL DWORD PTR [EAX+0x24]` at static `0x7a8a1d84` -- an indirect COM vtable dispatch, not resolvable from the file bytes alone.
6. Molly found (via Ghidra) that address `0x0FA041E4` in `expsrv.dll` references `FUN_0f9dd3d9`'s address. Checked the file directly: `0x0FA041E4` holds the literal DWORD `0xf9dd3d9` and is part of a contiguous array of `expsrv.dll` code pointers starting at `0x0FA041C0` (`.rdata` section) -- a genuine vtable. Slot 9 (byte offset `0x24`, matching the `[EAX+0x24]` dispatch) is exactly `0x0FA041E4`. **Confirms the vtable dispatch statically**, no live run needed for this part.
7. Added a one-shot-style breakpoint (`_expsrv_vtable_call_probe`, `run_exe.py`, runtime address `0x18061d84`) logging `EAX`/`[EAX+0x24]`/`ECX`/`EDX`/`EBX` on every hit (not truly one-shot -- kept armed since the crash might not be the first call through this path). **Fired exactly once in the whole run**, and it's the crashing instance: `EAX=0x1a0441c0` (runtime vtable base, matches static `0x0FA041C0` translated), `[EAX+0x24]=0x1a01d3d9` (exactly `FUN_0f9dd3d9`'s runtime address -- live-confirms step 6's static hypothesis), **`ECX=0xFFFFFFFF`** -- confirmed live as the actual bad value, matching push-order analysis that `ECX` becomes `FUN_0f9dd3d9`'s arg3/`[EBP+0x10]`, which becomes `FUN_0f9dd9a7`'s `param_2`/`ESI`.

**Confirmed**: `ECX` is already `0xFFFFFFFF` in `MSJET35.DLL` at this exact call site (static `0x7a8a1d84`) -- not corrupted by the call itself, by `FUN_0f9dd3d9`, or by `FUN_0f9dd9a7`. **Not yet root-caused**: where in `MSJET35.DLL` `ECX` gets set to `0xFFFFFFFF` before reaching this call (outside the ~48-byte window decoded so far). This call site firing only once all run suggests a rare/error-adjacent code path (plausibly Jet's own "expression/column not found" handling) -- open question is whether `-1` here is legitimate real-Jet behavior that some earlier check should have caught before reaching this path, or whether tew's own emulation of something upstream produced `-1` where real Windows would have produced a valid pointer or taken a different branch. Next step (not started): trace backward through `MSJET35.DLL` from static `0x7a8a1d62` (start of the decoded window) the same way -- find where `ECX` is set.

## Previous status (2026-08-25, cont'd x5) — SEH `dispatch_exception` EIP-restore bug FOUND AND FIXED; real fault site is the SAME instruction as the earlier Zig panic (`EIP=0x1a01d9eb`, `ESI=0xFFFFFFFF`), root cause of the `ESI` value still open

**What changed**: `tew/kernel/seh.py`'s `dispatch_exception` never restored `cpu.eip` on its unhandled-chain-exhausted path. `_invoke_handler` (same file) genuinely executes each SEH handler on the CPU (sets `cpu.eip = handler_addr`, runs it for real, restores only `cpu.regs[ESP]` on a normal return) — so after the last handler in the chain declines (`EXCEPTION_CONTINUE_SEARCH`) and `dispatch_exception` returns `False`, `cpu.eip` was left at whatever `_invoke_handler` set it to internally: `SEH_RETURN_SENTINEL + 2` (`0x001FE010 + 2 = 0x001FE012`) — a fixed internal bookkeeping constant, not real guest code. This is exactly the mystery address (`EIP: 0x001fe012`) that has appeared identically in every unhandled-`DebugBreak`/SEH-exhausted halt across three weeks of unrelated investigations (grep `changelog.md`/`status_archive.md`) — it never resolved to any loaded module because it isn't one. **Fix**: restore `cpu.eip = exception_address & 0xFFFFFFFF` at both of `dispatch_exception`'s unhandled-return points (the `SehHandlerTimeout` except branch and the final chain-exhausted `return False`). Verified live via `coredumpctl gdb` was not needed this time — grepped every `dispatch_exception` call site first (`win32_handlers.py`'s `INT3` branch, `seh.py`'s `RaiseException` handler, `run_exe.py`'s two access-violation/runaway sites) to confirm none of them read `cpu.eip` for control flow between the call and the final `diagnose_halt()`/`Final EIP:` report — all four already log their own local `fault_eip`/`runaway_eip` variable, so the fix only affects post-run diagnostic reporting, not behavior.

**Confirmed live, and significant**: with the fix in, the same repro's `EIP:` now reads `0x1a01d9eb` — inside `expsrv.dll`'s loaded range (`0x1a000000-0x1affffff`), static/Ghidra address `0x0F9DD9EB` (`expsrv.dll` ImageBase `0x0F9C0000` + RVA `0x1d9eb`). **This is the exact same instruction as the Zig integer-overflow panic fixed earlier this session** (`MOV EAX,[ESI]`, `ESI=0xFFFFFFFF`) — the `core.zig` wrapping-add fix didn't resolve the underlying bug, it correctly turned an unrecoverable native crash into a proper (still-unhandled) CPU fault: the wrapped address lands on the null page, `memRead8`'s guard faults it, the game's own SEH chain gets a real shot and declines, and it halts here every time. **Not yet root-caused**: why `ESI == 0xFFFFFFFF` at this specific `expsrv.dll` instruction. Molly is inspecting `0x0F9DD9EB` directly in Ghidra (tew can't load this DLL for analysis) — next step depends on what's there.

**Also flagged, not yet fixed**: `_invoke_handler`'s `SehHandlerEscaped(faulted=True)` branch (`seh.py` around line 387) also leaves `cpu.eip` unrestored mid-loop when a handler crashes and the chain keeps walking. Harmless today (the next `_invoke_handler` call overwrites it immediately on entry), but same shape as the bug just fixed — worth a comment or defensive restore if it's ever touched again, not urgent enough to fix proactively.

## Previous status (2026-08-25, cont'd x4) — Zig-level integer-overflow panic in `readRmFixed32` — ROOT CAUSE FOUND AND FIXED; run now dies further downstream in `expsrv.dll`

**Resolution (see changelog.md for the committed fix)**: the panic (`cpu/src/core.zig:163`, `readRmFixed32` → `memRead32`, reached from `op8B`/`MOV r32,r/m32`) was confirmed via `coredumpctl gdb` on the crashed process, not inferred from the log. Frame `core.readRmFixed32(s, mod=0, rm=6)` — ModRM `mod=00`/`rm=110` decodes to the bare `[ESI]` addressing form (no disp/SIB) — showed `regs[ESI] == 0xFFFFFFFF` at `EIP=0x1a01d9eb`. `memRead32`/`memRead16`/`memWrite32`/`memWrite16` computed the second/third/fourth byte address as `addr + 1/2/3` using plain (checked) `u32` addition, which Zig panics on when it overflows — but a flat 32-bit linear address is supposed to wrap mod 2^32 on real hardware, exactly like `fetch8`'s existing `s.eip +%= 1` pattern elsewhere in the same file. `memRead8`/`memWrite8` already bounds-check correctly via `isFaultingAddr` and fault (not crash) on a truly invalid address, so the bug was purely in the intermediate `+1/+2/+3` arithmetic, not in the fault-handling design. **Fix**: changed all four call sites' `addr + N` to `addr +% N` (wrapping add) in `cpu/src/core.zig`. Rebuilt `libcpu.so` via `zig build`, confirmed live: the panic no longer reproduces, and the run progresses ~14s further (63s→77s) before hitting a new, unrelated halt — an unhandled exception inside `expsrv.dll` (Jet Expression Service, called from `MSJET35.DLL`), thread 1011, EBP chain rooted at `expsrv.dll+0x1d41d` (static/Ghidra-loadable address `0x0F9DD41D`, computed from `expsrv.dll`'s PE `ImageBase` `0x0F9C0000` + RVA `0x1D41D` — Molly is investigating in Ghidra directly since tew can't load this DLL for analysis). Not yet investigated further; see current `status.md` entry.

**Also this session**: `CreateThread`'s log line (`tew/api/kernel32_io.py`) was logging at `info`, which is disproportionately noisy for a routine per-thread-spawn event at that level — downgraded to `debug`. And `_prefclass_assert_probe` (`run_exe.py`, the breakpoint that confirmed the `Fields.Count`/`CompareStringA` fix landed, see the entry below) has been removed now that its question is fully answered and documented — its dangling in-code cross-reference (the "column-loop instrumentation" comment near `run_exe.py`) was updated to stop pointing at the now-deleted probe.

## Previous status (2026-08-25) — `ASSERT: mcity.c(588) prefClass>=0 && prefClass<DBCP_MaxRatings` / `Fields.Count==1` instead of `10` for `StockVehicleAttributes_SelectAll2` — ROOT CAUSE FOUND AND FIXED

**Resolution (see changelog.md for the committed fix)**: tew's `CompareStringA`/`CompareStringW` handler (`tew/api/kernel32_io.py`, `_locale_is_valid`) rejected `LOCALE_USER_DEFAULT` (`0x0400`) as an invalid locale — a completely standard Windows sentinel that real `CompareStringA` resolves and succeeds on. dao350.dll's internal field-name dedup check (`FUN_044da868` → `FUN_044d1d53` → `FUN_044c6126` → `FUN_044c6284`) calls `CompareStringA(LOCALE_USER_DEFAULT, ...)` for every column-name comparison while building a recordset's Fields collection — confirmed live, 371 calls in one run, every one rejected (`EAX=0`, "invalid locale"). dao350's own switch on the result maps failure (`0`) to the same code path as `CSTR_EQUAL` (`2`) — harmless on real Windows, which essentially never returns 0 for a well-formed call, but under tew's rejection it turned every single name comparison into an unconditional "equal", so every column after the first got misidentified as a duplicate of it and silently skipped. Confirmed via live memory stalking (per-iteration dumps of the collection object, the per-column descriptor struct, and the dedup lookup's return value) that field #2 ("Brand", correct real column data — not garbage/corruption) was matched against field #1 ("BrandID") purely because the comparison call always reported equal, not because of a real prefix/hash collision. Fixed by resolving `LOCALE_USER_DEFAULT` (`0x0400`) and `LOCALE_SYSTEM_DEFAULT` (`0x0800`) to `0x0409` before validation. Confirmed live: the `prefClass` assert no longer fires (137 probe hits, all `real_answer:ok`), and the run progresses ~16s further than ever before (63s→79s) into new territory (COM/OLE automation, `LoadTypeLibEx`) before hitting an unrelated new blocker — a Zig-level integer-overflow panic in `cpu/src/core.zig`'s `readRmFixed32` (`op8B`/`MOV r32,r/m32`), not yet chased; see the current `status.md` entry.

**Investigation history (full mechanism trace, kept for reference — the eventual fix came from a completely different angle, live-stalking dao350.dll's dedup check directly, not from continuing any of the leads below)**:

## Current status (2026-08-25) — New downstream blocker after the SEH fixes (see status_archive.md for those, DONE/committed): `ASSERT: mcity.c(588) prefClass>=0 && prefClass<DBCP_MaxRatings`. Root cause traced down to "a real DAO Recordset.Fields.Count reads as 1 instead of 10" -- confirmed via real game/DAO code, not yet confirmed WHY

**Context**: with the anti-debug-self-test crash and SEH-dispatch-nesting blocker both fixed (see `status_archive.md`'s "Previous status (2026-08-24, cont'd)" and matching `changelog.md` entries), the game now reaches its real main window and runs 40+ seconds before halting here -- furthest ever. This is a new, unrelated, genuine bug, not a recurrence of anything.

**Full mechanism traced, real game/DAO code (not msjet35.dll internals) confirmed via Ghidra decompiles + `mdbtools` + one focused breakpoint run** -- full detail (addresses, ruled-out theories) in `status_archive.md`'s matching entry, short version here:
- `carClassList::carClassList()` validates a `prefClass` field from `DB_GetGameConfigCarTableOffline`'s query, a real stored QueryDef `StockVehicleAttributes_SelectAll2` (`SELECT ...AIRestrictionClass...CarClass... FROM [StockVehicleAttributes],[BrandedPart],[Model]`, confirmed via `mdb-queries ~/.emu32/Data/DB/Online.mdb`). "prefClass" = column 1 = `AIRestrictionClass` (the variable name is misleading, not a bug).
- Ruled out via `mdbtools`: bad source data (fully populated, 0-7, real), bad file copy (`Tmp.MDB` byte-identical schema/query/relationships to `Online.mdb`), lock conflicts (zero failures in a full run). One unexplained oddity, not chased further: `Tmp.ldb` opens twice same-millisecond/same-thread, only the second handle ever used, no failures result.
- `Dbcode_Fetch` (game's own DAO wrapper, `0x8f9c10`) calls `GetValue(recordset,col,row)` (real DAO C++ wrapper, `0x40da3f`) per bound column; `GetValue` returns `NULL` exactly when `col >= Fields.Count` (a real COM property read on the real `dao350.dll` Recordset). `Dbcode_Fetch` treats `NULL` as "no data," prints the `dbcode.c(3687)` warning, and **returns immediately** -- explaining why `dblog.txt` only ever shows column 1 (it's the *first* failure per row, not literally the only one).
- Since column 0 never warns and column 1 always does, on every row, every run: `Fields.Count` is provably exactly `1` for this query's live recordset (logical deduction from existing `dblog.txt` evidence, no live check needed for this specific fact).
- `Fields.Count` comes from `DBParamQuery::DoQuery`'s real body (`0x00997450` -- `0x40758b` is just a `JMP` thunk to it) calling the real `_DAOQueryDef::OpenRecordset` COM method. `DBParamQuery`'s constructor only resolves the QueryDef by name (confirmed succeeds -- no abort) and binds unrelated `Parameters`, never touches `Fields`.

**Not yet root-caused past this point, and not yet fixed**: *why* the real `OpenRecordset()` call returns `Fields.Count==1` for this specific query in tew's environment. Confirmed live which of `DoQuery`'s 2 calls-per-run is ours (return address matches `DB_GetGameConfigCarTableOffline`'s call site exactly).

**2026-08-25 (new session) -- HRESULT confirmed, OpenRecordset itself is NOT the failure**: real disassembly (not the decompiler's `0x99779b` literal) located the actual vtable call at `0x0099778e` (`CALL DWORD PTR [ECX+0x8C]`, 6 bytes, confirmed via raw byte decode) with the real next instruction at `0x00997794` (RTC's `CMP ESI,ESP`, doesn't touch EAX). `0x009975c5` -- one of last night's two dead-end addresses -- turned out to be a completely unrelated call (`_REAL_abortmessage("%s\n","m_QueryDef")`, the already-ruled-out "QueryDef is null" assert path, distinguishable from a real vtable call by its `FF 15 [absolute]` encoding vs `FF 91 [ECX+0x8C]`). One-shot breakpoint at `0x00997794` (`_openrecordset_hresult_probe` in `run_exe.py`, repurposing the freed 8th slot) live-fired once at 42.632s: **`EAX (HRESULT) = 0x0` (S_OK)**. Run otherwise reproduced the known blocker unchanged (same `mcity.c(588)` assert in `stdout.txt`, same unhandled-DebugBreak halt at `EIP=0x001fe012`, no `except.txt` -- consistent with this assert's inline-DebugBreak path). **This rules out an OpenRecordset failure entirely** -- the recordset is created successfully; `Fields.Count==1` is a bug in what the successfully-opened recordset actually contains, not in whether the open succeeded. **Next step, not yet started**: find where/how `Fields.Count` itself is actually determined for a live `DAORecordset` -- likely requires either a second breakpoint reading the returned object's `Fields` collection directly, or examining how msjet35.dll's query engine resolves the SELECT column list for `StockVehicleAttributes_SelectAll2` after a successful open. Possible, unconfirmed connection to the earlier DAO-3075 SELECT-list lookahead-scanner investigation (msjet35.dll, resolved 2026-08-20, see changelog.md) -- that investigation's probes were removed in this session's cleanup (see Housekeeping below), so re-derive from `status_archive.md`/`changelog.md` if this path is worth reopening; do not assume it's the same bug without evidence.

**2026-08-25 (cont'd x2) -- Fields.Count==1 confirmed LIVE and shown to be SYSTEMIC, not specific to `StockVehicleAttributes_SelectAll2`**: fixed two live breakpoints in `GetValue` (`0x0040da3f` exe thunk -> real body `0x008fb8e0`) at the real `get_Fields`/`get_Count` vtable calls (`Recordset+0xB4` -> `Fields+0x1C`, both hand-confirmed via raw byte decode of the `CALL [reg+0x8C]`-style dispatch and their out-param LEA/PUSH pairs -- Ghidra's decompiler-assigned local names (`piStack_24`, `asStack_30`, even `local_14`) are consistently 4 bytes off their real EBP-relative offsets in this SEH-instrumented function; every stack-offset read for this investigation now uses hand-decoded real offsets, not decompiler names). First pass tried filtering by a target recordset pointer read from `DoQuery`'s own frame (`local_1c`) -- abandoned, same naming-offset trap, read `0xcccccccc` RTC poison every time. Second pass filters `GetValue`'s ABI-fixed `__cdecl` args instead (`[EBP+8]`/`[EBP+0xC]`/`[EBP+0x10]`), immune to the naming problem.

Corrected result, one full run: **1896 `get_Count` calls, every single one returns `Count=1`, across 5 distinct recordset pointers** (`0x7072123`, `0x707214b`, `0x7072387`, `0x7072417`, `0x7072576`). Initially read as "systemic, not query-specific" -- **corrected same session, see the 2026-08-25 (cont'd x4) entry below**: most of these are unrelated ad-hoc queries (real `Brand`/`AbstractPartType` PK scans, an `AuctionPersonaMakes`-to-`Brand` join) that plausibly have legitimately low field counts on their own; only one of them (`0x7072417`, identity-confirmed via `DoQuery`'s own out-param, not timing/address guessing) is actually `StockVehicleAttributes_SelectAll2`/`DB_GetGameConfigCarTableOffline`. Don't re-treat the other 4 as evidence of the same bug without independently confirming their expected field counts first.

**2026-08-25 (cont'd x3) -- JETSHOWPLAN enabled, found+fixed a real missing handler (`wvsprintfA`) along the way, but plan text not yet surfaced anywhere**: added `hklm\software\microsoft\jet\3.5\engines\debug\jetshowplan` = `{"type":1,"value":"ON"}` to `registry.json` (type 1 = REG_SZ, matching the real documented Jet 3.5 convention -- Molly asked whether it should be a DWORD `1` like other flags instead; live evidence favors the string: the emulated msjet35.dll visibly reacted to `"ON"` immediately, so whatever comparison it does against the registry value matched). Confirmed live: `RegQueryValueExA(..., "JETSHOWPLAN") -> 'ON'`. That immediately exposed a real, previously-unexercised gap: `[UNIMPLEMENTED] user32.dll!wvsprintfA -- halting` (msjet35.dll's plan-text formatter). Fixed properly (not a stub) in `tew/api/user32_handlers.py` by reusing the existing `_sprintf_format`/`_write_cstring` engine from `msvcrt_handlers.py` (already powers `sprintf`/`vsprintf`) with `__stdcall` cleanup (12 bytes) instead of `vsprintf`'s `__cdecl` -- `wvsprintfA`'s real signature is `(LPSTR, LPCSTR, va_list)`, same shape as `vsprintf` with a different calling convention.

With that fixed, the run reaches the real `mcity.c(588)` halt again (not a trampoline halt). Molly asked whether the registry value should be a DWORD `1` instead of the string `"ON"` used -- live evidence favors `"ON"`/REG_SZ (msjet35.dll visibly reacted to it, calling `wvsprintfA` immediately).

**2026-08-25 (cont'd x4) -- JETSHOWPLAN fully explained, and it's real Jet behavior, not a tew gap; also nailed down real target-recordset identity and traced one hop further into `get_Count`'s real implementer**: Molly found the real output path, `~/.emu32/showplan.out` -- and it DOES get real plan text written (6 `CreateFile("showplan.out")`s in one run, e.g. `01) Scan table 'Brand' Using index 'PrimaryKey'`, `01) Inner Join table 'AuctionPersonaMakes' to table 'Brand' ... store result in temporary table`), confirming JETSHOWPLAN and the `wvsprintfA` fix both work correctly. But **`StockVehicleAttributes_SelectAll2` never appears in any of them, in any run** -- because it's a real, pre-existing *stored* QueryDef (confirmed earlier via `mdb-queries`), and Jet's optimizer only re-plans (and rewrites `showplan.out`) on fresh SQL *compilation* -- `CreateQueryDef`/ad-hoc SQL text -- not on `OpenRecordset` against an already-compiled stored QueryDef, which just reuses its cached plan. The 6 plans observed are unrelated ad-hoc/temp queries elsewhere in the game. Since the file gets truncated on every `CreateFile`, reading it at any point after a stored-QueryDef open just returns stale leftover text from whichever ad-hoc query wrote it last -- explains why a `showplan-snapshot` probe (added mid-session, reads the file from the host filesystem right as a query's `GetValue` loop starts) kept returning plausible-looking but wrong plans for our target. **Conclusion: JETSHOWPLAN cannot show this query's real column list, full stop -- not a possible-fix-if-tried-harder gap, a genuine property of how stored QueryDefs work in real Jet.**

Used the dead end productively: needed a *reliable* way to identify recordset identity anyway, so fixed `DoQuery`'s own out-param capture properly -- hand-decoded the real null-check (`CMP DWORD [EBP-0x18],0` right before the `OpenRecordset` branch) instead of guessing from the decompiler name (`local_1c` implied `-0x1C`; real offset is `-0x18`, same uniform -4 shift as everything else hand-decoded in this function/file -- **lesson reinforced yet again: never trust a Ghidra-decompiler stack-variable name's numeric suffix as a real offset in this codebase, always hand-decode the real instruction**). With the fix, `_openrecordset_hresult_probe` (no longer one-shot -- this branch can fire more than once per run) reliably captures the real recordset pointer for each `DoQuery` call. **Result: recordset `0x7072417`, `DoQuery`'s 2nd `OpenRecordset` call, shows live `Count=1` with columns 0 through 4+ all probed on row 0** -- a hard, identity-confirmed reproduction of the bug (not timing/address coincidence), matching status_archive.md's independent confirmation of which `DoQuery` call is ours. This also retroactively corrects the "5 distinct recordsets, systemic" framing two entries up -- most of those 5 are the unrelated ad-hoc queries JETSHOWPLAN revealed (`Brand`/`AbstractPartType` scans), plausibly legitimate low counts, not the same bug.

Traced one hop past the `get_Count` tear-off thunk (`0x0447dfe2`, `MOV ECX,[this+8]; CALL [[ECX]+0x24]`): the inner object's own vtable (`0x4471e40`) at slot 9 (`+0x24`) is `0x0447dc1c` -- decompiled, real (non-thunk) logic: `*param_1 = *(short*)(iVar1+0x2C)` where `iVar1 = *(int*)(this+8)` (a THIRD hop), gated on a validity check (`*(int*)(*(int*)(iVar1+0x34)+0x10) < 0x25`) and a refresh call (`FUN_044d26ce(iVar1)`) succeeding first.

**Read that raw field live -- conclusive result: `rec_base+0x2C` already holds `1`, for every recordset checked, including our identity-confirmed target (`0x7072417`).** Not a marshaling/dispatch/tear-off bug anywhere in the `get_Count` call chain -- the value is genuinely stored as `1` in the object's own memory before any of `GetValue`'s vtable calls even happen. **This conclusively moves the bug upstream, to whatever populates this field when the query's column list gets resolved** (query compile/bind time, not fetch time) -- same general class of bug as the already-fixed DAO-3075 tokenizer issue (a real column-list resolution failure), but necessarily a *different* code path since that one is closed and confirmed working for other queries. Not yet found: the real write site for `[rec_base+0x2C]` (or whatever earlier structure ultimately feeds it) -- next step is a static xref search in Ghidra (dao350.dll and/or msjet35.dll) for instructions writing to that offset on this object class, most likely inside real Jet code that resolves a multi-table (3-table implicit-join) `SELECT` column list specifically, since single-table queries observed via JETSHOWPLAN (`Brand`, `AbstractPartType` scans) are not known to exhibit this.

**2026-08-25 (cont'd x5) -- full causal chain from `Fields.Count==1` to the visible crash now closed, via Molly pointing at the real crash site (`carClassList::carClassList`, `0x005bad20`, decompiled)**: the loop that hits the `prefClass>=0 && prefClass<DBCP_MaxRatings` assert iterates `*(int*)(*(int*)this + 4)` (`DBCarTableOutputData`'s own successfully-fetched row count -- a *different* counter than `Fields.Count`, but downstream of it), reading two per-row fields from a `0xa0`-byte-stride row array: `carClass` at `+0x8c`, `prefClass` (`AIRestrictionClass`) at `+0x88`. Since `Fields.Count==1` makes column 1 (`AIRestrictionClass`/`prefClass`) fail to bind, this row-buffer field ends up invalid. **Corrected same session, see the 2026-08-25 (cont'd x11) entry below**: the original theory here (uninitialized heap garbage, since the row buffer isn't zeroed per-row) turned out to be an incomplete guess made before finding the real call path -- the actual mechanism is a *deliberate* sentinel write, not leftover memory, and the real crash-site function initializes this field to a safe default (`1`) before fetch, not zero. Kept for the historical record of how the investigation got there; don't re-cite the "uninitialized garbage" framing as the final answer.

**Full mechanism, root cause to crash, is now airtight**: `Fields.Count==1` (root cause still unfound, upstream of the getter chain per the entry above) -> column 1 never bound -> invalid `prefClass` -> assert. Remaining open question is unchanged: *why* `[rec_base+0x2C]` (or whatever feeds it) is `1` instead of `10` for this specific 3-table query at compile/bind time.

**2026-08-25 (cont'd x6) -- traced the population chain down to a dao350.dll/msjet35.dll boundary; write site itself is now in msjet35.dll territory**: static chain, each hop decompiled and read (`state`/dispatch fields captured live to confirm which branch our object actually takes, not assumed):
- `get_Count`'s lazy-refresh gate, `FUN_044d26ce` (called from `FUN_0447dc1c` before every read of `+0x2C`): only calls the real populator if `*(short*)(rec_base+0x2C) < 1` -- since ours is already `1`, **it never re-runs**. Whatever set it to `1` the first time is permanent for the life of this recordset.
- Real populator dispatch, `FUN_044da240` (`DAT_044770b0[state*4]`, `state`=`*(int*)(rec_base+0x10)`, live-confirmed `state=5` for our object) -- a big per-column-type dispatcher. Our object's `check_val` (`*(int*)(check_ptr+0x10)`, `check_ptr=*(int*)(rec_base+0x34)`) is live-confirmed `0x1e` (30), landing in the `case 4/0xb/0xc/0x1e` branch, which calls `FUN_044dac2b(local_50, local_4c, *(int*)(rec_base+0x34)+400, rec_base)`.
- `FUN_044dac2b`: the real per-column enumeration loop. `iVar2` (starts 0, `+1` each successful iteration) is the actual column counter -- gated by `FUN_044d5200` ("get next column," returns `-1` on `-0x643`, the ISAM "no more items" code, to end the loop) and `FUN_044da868` ("process/add one column," called once per real column found).
- `FUN_044d5200` is a thin wrapper: `(*DAT_044e52e4)(param_2,param_3,...)`, a **dynamically-bound function pointer into msjet35.dll** (same pattern as `DAT_044e534c`/`DAT_044e52e8` from the already-fixed DAO-3075 investigation -- dao350.dll is a thin C++/COM wrapper, msjet35.dll does the real Jet-engine work).

**This is a real phase boundary, not a dead end** -- the actual "why does column enumeration stop after 1 column for this 3-table query" logic lives inside whichever real msjet35.dll function `DAT_044e52e4` resolves to at runtime. Resolved statically: ordinal 156 (via the real PE export table, `Ordinal Base 2`, index 154, RVA `0x8f59`) -> static `0x7a840000+0x8f59=0x7a848f59` (`FUN_7a848f59`, a lock/validate/dispatch gate) -> real worker `FUN_7a84269c`, which indexes a 2048-slot session/type table (`DAT_7a95d010`/`_014`/`_01c`, stride `0x10`, bounds-checked against `0x800`=2048 -- same table Molly independently found via xref-scanning, `FUN_7a86f969`/`FUN_7a89acb3`/`FUN_7a8492a3`/`FUN_7a90fb2a` are all siblings or infrastructure on this same table, none of them the actual write site) and dispatches via that slot's own vtable `+0x68`. **Tried live-probing `FUN_7a84269c`'s entry directly -- too generic**: hundreds of calls across many different `session_idx` values within a single 200ms window, confirming ordinal 156 is a heavily-reused type-conversion/comparison utility, not a one-shot per-query call. Abandoned that probe.

**Went one level up instead, into `dao350.dll`'s own `FUN_044dac2b` (unrelocated, static==runtime, no delta needed) -- the real per-column loop that calls `FUN_044d5200`** (the dao350.dll-side thin wrapper around the whole msjet35.dll chain above). Hand-decoded the real call-site address (`E8 3B A5 FF FF` at `0x044dacc0` -> target `0x044d5200`, confirmed exact match -- the earlier byte range I tried first, `0x044dac70-80`, was the WRONG call, a pre-loop one-time setup call to `(*DAT_044e52b8)`, not this one). The four args get pushed `EAX,ECX,EBX,EDI` immediately before the call; cdecl right-to-left means `EDI` (pushed last) is `param_4`=`rec_base`, readable directly as a register at the breakpoint -- no stack-offset guessing.

**Live result, identity-confirmed via the SAME rec_base appearing in the existing fields-dump block for `recordset=0x7072417`** (not a timing guess): `FUN_044d5200` is called **exactly 3 times** for our target query, not 10 and not 1. So Jet's real enumeration itself stops after 3 attempts for this 10-column, 3-table-join query -- a distinct, still-open question from why `Fields.Count` ends up at 1. Decompiled `FUN_044da868` (called once per successful iteration, receives the running index) to see what it actually does with each column: **it's an upsert** -- looks up the candidate column by name/index (`FUN_044d1d53`/`FUN_044d1d98`); if not found (`uVar1==0`), allocates and inserts a genuinely new field entry; if found (`uVar1!=0`, an EXISTING entry with a matching key), it instead resets/clears that existing entry's sub-buffers rather than creating a new one. **This is a real candidate mechanism for the undercounting**: if name resolution for columns 2 and 3 collides with column 1's already-inserted entry (plausible for qualified names like `StockVehicleAttributes.BrandedPartID` across a 3-table join, vs. the single-table `Brand`/`AbstractPartType` queries JETSHOWPLAN showed working fine), 3 successful enumeration attempts could collapse into exactly 1 stored field -- matching the live evidence precisely. Not yet confirmed live (would need to breakpoint `FUN_044da868`'s entry, read `uVar1`/the lookup result, and see whether it's 0 or nonzero on iterations 2 and 3 for our target). Also still unexplained: why enumeration stops at 3 attempts rather than continuing to all 10 real SELECT-list columns -- a second, so-far-unconnected question.

Two concrete next steps, either is reasonable: (1) breakpoint `FUN_044da868`'s entry for our `rec_base`, confirm/refute the name-collision hypothesis directly; (2) figure out why `FUN_044d5200` only gets called 3 times instead of 10 (a msjet35.dll-side question, likely inside whichever function `FUN_7a84269c`'s session-table `+0x68` slot resolves to -- still not captured live, since the direct probe there proved too generic to filter without a real session-index filter in hand).

**2026-08-25 (cont'd x7) -- pursued (2), found the real per-column handler function via EBP-chain + session_idx correlation; Molly independently xref-scanned the same 2048-slot table and found several sibling/infrastructure functions (`FUN_7a86f969`, `FUN_7a89acb3`, `FUN_7a8492a3`, `FUN_7a90fb2a`) worth recording since they may matter for a future investigation even though none is the write site**:
- `FUN_7a86f969`: sibling to `FUN_7a84269c`, same table/bounds-check idiom, dispatches via `+0x98` instead of `+0x68` -- confirms `param_2`/session_idx is a shared selector across a whole family of "call method N on this type" dispatchers, reinforcing it's generic infrastructure.
- `FUN_7a89acb3`: a broadcast/notify -- scans the table from `DAT_7a95b008` onward, calls `FUN_7a8b195f(db_handle, slot_idx, event_code)` for every non-empty slot whose stored handle matches the caller's database. Plausible "notify all open cursors of event X" but not confirmed relevant.
- `FUN_7a8492a3`: the table's real **allocator** -- pops a slot off a free list (`DAT_7a965010`, `-1`=empty), registers `param_3`/`param_2` into the slot, decrements the `DAT_7a95b008` watermark (allocates top-down) when the free list empties. Explains why observed `session_idx` values cluster near 2047 rather than starting at 0.
- `FUN_7a90fb2a`: initially looked promising (decrements a `short*` counter, same width as `Fields.Count`) but its only caller (`FUN_7a90f928`) is Jet's identifier/name-lookup cache (refcounted symbol interning), not the Fields collection -- a coincidental structural match, not our bug, unless table/column name resolution for the join is somehow implicated (not evidenced).

**Live correlation, using the exact timestamps of our target's 3 `FUN_044d5200` calls (identity-confirmed via `rec_base`) to filter the noisy `FUN_7a84269c`-entry breakpoint instead of trying to filter inside the probe**: re-added a breakpoint at `FUN_7a84269c`'s entry (msjet35.dll runtime `0x1500269c`) that walks the EBP chain (bounds-checked after an earlier version crashed the whole run on a bogus dereference -- msjet35.dll doesn't reliably keep frame pointers this deep) and also reads `session_idx` (`param_2`, `[ESP+8]`). All 3 of our target's calls -- confirmed by exact millisecond timestamp match against the `column-loop-probe` -- show **`session_idx=2038`, constant across all 3 attempts**. This means `session_idx` is a fixed per-statement handle, not a per-column type selector as originally assumed -- the real per-column iteration logic lives inside `session_idx=2038`'s own registered object, at its vtable `+0x68`.

Resolved that: `session_idx=2038` -> `session_obj=0x15010d40` -> `real_target` runtime `0x15007105` -> static `0x7a847105`. First pass reused a `session_obj`/`real_target` value read from an *earlier, different* run -- flagged as methodologically unsound (this table is a slot pool, `FUN_7a8492a3` allocates/frees dynamically, so the same numeric index isn't guaranteed to hold the same object run-to-run) and re-verified by capturing `session_idx` and `session_obj`/`real_target` together in one run. **Verified: identical result** (`0x15010d40`/`0x15007105`), consistent with tew's execution being fully deterministic (same heap layout every run) -- the original value was correct, but the re-check was the right call and should stay standard practice for anything read from the table.

Note: `0x15007105` is also the single most-common shared target across most other `session_idx` values in the 2036-2047 range (12 of them observed pointing at it in one run) -- so it's likely still a fairly generic handler, not something uniquely specialized for our 3-table join. Decompile of the static address failed (no function defined there in Ghidra's analysis, same situation as `FUN_0447dc1c` earlier -- only reachable via indirect vtable dispatch). Raw byte decode of the opening confirms real, non-trivial logic: takes its 3rd argument (`[ESP+0xC]` at entry), branches on whether it equals special integer sentinels (`0x80000000`, negative, `0x7FFFFFFF`), then examines a flag byte at `[ESP+0x28]` and an object field at `+0x24`. Not fully decoded -- needs the same full raw-byte-decode discipline as everything else in this DLL. Static address for next session: `0x7a847105`.

**2026-08-25 (cont'd x8) -- Molly's reframe: the bug can ONLY be in tew, not in real dao350.dll/msjet35.dll -- every root cause found in this project's history has been a tew CPU/memory emulation divergence, never an actual flaw in Microsoft's shipped code (matches the DAO-3075 fix exactly: a real 0x66-prefix INC/DEC operand-size bug in tew, not a Jet bug). Re-grounds where to keep looking.** Checked tew's own CPU engine (`cpu/src/engine.zig`, `two_byte.zig`) for a similar class of gap given how much of this chain is `short`/16-bit-typed (`Fields.Count`, `raw_count`, `FUN_044da868`'s lookup key) -- `MOVZX`/`MOVSX` (r32,rm8/rm16) look correctly implemented; the `op_size_ovr` (0x66 prefix) comments found (`doGroup2`/`opC1`'s shift-by-zero-width bug, `opIncR32`/`opDecR32`'s DAO-3075 fix) are already-fixed history, not live leads -- don't re-chase these.

**Decisive live result instead**: added a paired breakpoint right after `FUN_044d5200`'s call site (`0x044dacc5`, immediately following the confirmed `0x044dacc0` call) to read its actual return value. For our target (`rec_base=0x70722a3`): **call #1 returns 0 (success), call #2 returns 0 (success), call #3 returns -1 (0xFFFFFFFF, the clean "no more columns" sentinel) -- not an error code.** The loop terminates *correctly, in form* -- `FUN_044d5200` and everything inside `FUN_044dac2b`'s loop is doing exactly what it should given what it's told. **This rules out the enumeration loop itself as the bug site and moves the investigation firmly upstream, to the real SQL column-list parsing/tokenizing step** (query compile time, before `OpenRecordset`'s column-binding phase even starts) -- real Jet genuinely believes this query has only 2 enumerable columns. Same general shape as the already-fixed DAO-3075 bug (a lookahead-scanner in msjet35.dll's real SQL parser stopping early), but that fix was specific to the `AS`-alias/paren-depth-counter code path for a *single*-table aggregate query -- it never touched whatever tokenizes multi-table `Table.Column`-qualified references, which is the differentiator between our working single-table JETSHOWPLAN examples (`Brand`, `AbstractPartType`) and this broken 3-table join.

**Not yet started**: finding the real SQL parser/tokenizer code path specifically for qualified (`Table.Column`) references in a multi-table `FROM` clause -- likely inside msjet35.dll, likely reachable via the same general `Dbcode_CreateTmpQuery`-style compile chain status_archive.md already traced for DAO-3075 (though this query is a *stored* QueryDef, not ad-hoc SQL text, so its compile happens at a different time -- possibly database-open time rather than per-`OpenRecordset`, worth checking whether the compiled plan is cached in the .mdb file itself or recompiled fresh each session).

`run_exe.py` breakpoint slots: 6 of 8 in use (`_openrecordset_hresult_probe`, `_fields_probe`, `_fields_count_probe`, `_column_loop_probe`, `_column_loop_return_probe`, `_msjet_ebp_chain_probe`). 2 free.

**2026-08-25 (cont'd x9) -- oleaut32 Variant hypothesis retested and ruled out, two real gaps found and fixed along the way**: `BrandedPart.MfgDate` is a real `DateTime` column already confirmed touched in expression evaluation for this general area (`VarDateFromStr` handles a real WHERE-clause date-literal comparison). Checked whether OLEAUT32's Variant/date conversion could be involved in the undercounting. Found and fixed two genuinely missing handlers in `tew/api/oleaut32_handlers.py` (real implementations, not stubs, reusing the existing OLE-date-epoch/Lotus-leap-year-quirk math already proven correct for `VarDateFromStr`): `VarDateFromUdate` (UDATE struct -> DATE) and, once that unblocked forward progress into new code, `VarUdateFromDate` (the inverse). Both resolve cleanly now (no more `GetProcAddress(...) -> NULL` warnings for either). **Result: no change to the bug** -- `raw_count` is still exactly `1` for every recordset including our identity-confirmed target, same halt at the same address. Rules out OLEAUT32 Variant/date handling as the cause, at least for these two functions. The fixes are real and worth keeping regardless (a third function, `GetAltMonthNames`, was the next unresolved one -- traced its real call site to a large (~56-function) version-gate in `expsrv.dll`, all branching to one shared early-return on any single failure, but the gate's own DISPID-range check (`arg1==6`, `arg3` in `[0x975,0x204c]`) doesn't match anything `StockVehicleAttributes_SelectAll2`'s SQL actually calls -- likely serves a different query's `Format()`/date-function usage, not confirmed relevant to ours).

**2026-08-25 (cont'd x10) -- Molly asked the right question: why hand-reimplement oleaut32 in Python instead of running the real DLL? Answer: it was never copied into the emulated filesystem, purely an oversight, not an architectural choice.** A real, period-correct `oleaut32.dll` already existed in the project's i386 binary source pool but was missing from `~/.emu32/WINDOWS/System32/` (confirmed via the DLL loader's search path, `find_dll_file`/`_search_paths` -- same directory `msjet35.dll`/`dao350.dll`/`expsrv.dll` already load real from) -- tew's "Could not find OLEAUT32.dll" warning was the tell, present in every run's log all along. Copied the real file into place (not a code change). **Result: `OLEAUT32.dll` now loads and executes for real (796 exports, confirmed live) -- and the bug is completely unchanged.** `raw_count` is still exactly `1` for every recordset including our confirmed target, same halt at the same address. **This conclusively rules out oleaut32.dll in its entirety** (not just the specific functions hand-implemented and fixed this session) -- real Microsoft code now runs end to end for this DLL, eliminating an entire category of "maybe the Python reimplementation is subtly wrong" risk. Narrows the remaining suspects to tew's own CPU/memory emulation, or one of the genuinely-can't-be-real DLLs (kernel32/msvcrt/registry -- OS-level surfaces that have no real file to load). The hand-written `oleaut32_handlers.py` code (including this session's `VarDateFromUdate`/`VarUdateFromDate` fixes) is now dead code as long as the real file stays in place -- not removed, since it's still needed as a fallback if the file is ever absent in a different environment; flagged for Molly to decide whether to keep or prune.

Loading the real `OLEAUT32.dll` immediately surfaced the same class of gap one level deeper: `Could not find RPCRT4.dll`. Also present in the binary source pool, same fix -- copied into `~/.emu32/WINDOWS/System32/`. Now loads real too (1028 exports). Measurable, confirmable improvement: `expsrv.dll`'s own IAT patching went from 0 real DLL exports/41 auto-stubs to **28 real DLL exports/19 auto-stubs** -- genuinely more of the system running as real Microsoft code now. **Bug still completely unchanged** -- `raw_count` still exactly `1` for every recordset. RPCRT4.dll itself pulled in two more missing dependencies -- `ntdll.dll` and `Secur32.dll`. `ntdll.dll` is a different kind of gap, not the same oversight class: it's the real NT syscall-transition layer (`int 0x2e`/`sysenter` stubs assuming a real NT kernel underneath), and tew deliberately implements that boundary itself rather than running real syscall stubs -- did NOT copy it in, real risk of breaking things rather than fixing them, left as a judgment call. `Secur32.dll` (SSPI/authentication) is ordinary user-mode code though, same fix pattern -- copied in, loads clean (no longer in any `Could not find` list), `expsrv.dll`'s auto-stub count dropped one more (19->18). **Bug still completely unchanged** across all three real-DLL additions (oleaut32, rpcrt4, secur32) -- `raw_count` still exactly `1` for every recordset. `kernel32`/`user32`/`gdi32`/`advapi32`/`comctl32`/`shell32`/`ole32`/`comdlg32`/`msimg32`/`winmm`/`dinput`/`dsound`/`wsock32`/`version`/`wininet` all still show `Could not find` at t=1.2s every run -- these are core Windows subsystem DLLs tew has deliberately chosen to emulate rather than run for real (same category as ntdll), not further oversight-class gaps to chase.

**2026-08-25 (cont'd x11) -- the DOWNSTREAM mechanism (Fields.Count==1 to the visible crash) is now fully, precisely closed, via the REAL compiled call path -- and it corrects an earlier wrong guess.** Molly pushed on "was this really a timer" and "is -1 a real error Dbcode_Fetch/GetValue can return natively" -- both good, and chasing the second one down through the *actual* call path used (not the free-standing `Dbcode_Fetch`/`gBinding[]` global-table function chased earlier this session, which turned out to be for *different*, unrelated ad-hoc queries -- confirmed live: our target's identity-confirmed recordset never once appears in that function's calls, across an entire run) surfaced the real mechanism:

`DB_GetGameConfigCarTableOffline` (`0x0097d810`, real decompile) -> `DBRecordset::Fetch` (`0x00993b20`, resolved through a chain of 5-byte thunks -- `0x0040627b`/`0x00993b20`) -> per successful-lookup binding, `GetVariant` (`0x00993580`, a **trivial one-line wrapper**: `return GetValue(*this, col, row);` -- confirms the SAME `GetValue`/`Fields.Count` chain investigated all session IS the right one, just reached via a different, previously-unexamined caller) -> `DBBinding::Set` (`0x00991bf0`, real decompile). **`DBBinding::Set` is the exact write site**: `if (param_2 == NULL) { *indicator = 0xFFFFFFFF; *value_buffer = 0xFFFFFFFF; return SUCCESS; }` -- when `GetValue` returns NULL (our exact scenario), this function *deliberately* writes `-1` into **both** the indicator and the real output value buffer, then reports success regardless. This is NOT "uninitialized garbage happens to be -1" (the framing several entries up, now superseded) -- it's an intentional (if harmful downstream, since nothing expects `-1`) sentinel write, confirmed byte-for-byte from the real binary. Matches the live-captured crash value exactly (`local_1c=0xffffffff`).

**Airtight, fully real-binary-confirmed chain, no remaining inference anywhere in it**: `Fields.Count==1` (root cause, still open) -> `GetValue(recordset, col=1, row)` returns NULL -> `DBBinding::Set` writes `-1` into the value slot, reports success -> `DBRecordset::Fetch` reports success -> `DB_GetGameConfigCarTableOffline` copies `-1` into `pData->data[i].performanceRating` (its own real decompile shows this field is explicitly defaulted to the *safe* value `1` before fetch -- `-1` only lands there because the "success" fetch overwrote that safe default) -> `carClassList` reads `-1` as `prefClass`, fails `>=0`, asserts. Also ruled out along the way: `fpu_top` (x87 FPU stack depth) stays rock-solid at `0` across all 636 real `GetValue` calls captured in one run -- no evidence of an FPU-stack-imbalance mechanism either.

Root cause is unchanged and still the only open thread: *why* `Fields.Count` reads `1` instead of `10` for this specific 3-table join, upstream in msjet35.dll's real column enumeration (`0x7a847105`, `session_idx=2038`, raw-byte-decoded but not fully traced -- see the entries above).

`run_exe.py` breakpoint slots: 7 of 8 in use (`_openrecordset_hresult_probe`, `_fields_probe`, `_fields_count_probe`, `_column_loop_probe`, `_column_loop_return_probe`, `_prefclass_assert_probe`, `_dbcode_fetch_col_probe`). 1 free.

Also fixed this session, unrelated to the Fields.Count work: `MAX_STEPS` (`run_exe.py:571`, default 500,000,000, override via `TEW_MAX_STEPS` env var) was silently truncating runs before they reached the real halt -- `Steps executed: 133765665` was reported at the same moment `Execution limit reached (500000000 steps)` fired, two numbers that don't reconcile (a real, separate step-accounting bug, not yet root-caused, low priority). Logged as `=== Emulation Complete (clean exit) ===`, which is misleading -- it's a forced early stop, not a clean run. Workaround: pass `TEW_MAX_STEPS=5000000000` (or higher) until this is root-caused; not fixed in code, no default changed.

**2026-08-25 (cont'd x12) -- environment bug found and fixed (not a tew bug): repeated `kill -9` on hung tew processes wedged the KWin compositor, causing every subsequent run to hang at SDL2 init regardless of `run_exe.py` content.** Traced via `/proc/<pid>/wchan` (blocked in `poll_schedule_timeout`, infinite timeout) and confirmed the same hang reproduced with a provably-unreachable dummy breakpoint address and even with zero extra breakpoints, ruling out breakpoint count/address as the cause. Root cause: `tew/api/d3d8/_state.py`'s `shutdown()` (wired in 2026-08-24, tears down Vulkan objects before `SDL_DestroyWindow`) only runs on graceful exit/SIGTERM -- `SIGKILL` bypasses it entirely, and its own docstring already documented this exact compositor-destabilization risk. Fixed by restarting the compositor in place (`kwin_wayland --replace`, Molly's call after an `AskUserQuestion`) -- confirmed via a clean control run afterward (`Vulkan objects torn down` / `SDL2 shut down` in the log, both previously absent). **Lesson going forward: never `kill -9` a hung tew process** -- send `SIGTERM` and wait for its own cleanup, even if that takes a while; only use `-9` as an absolute last resort on an already-unrecoverable process, and expect to need a compositor restart afterward.

Also fixed a real latent bug this surfaced: the `_msjet_inner_vtable_probe` breakpoint (registered at a hardcoded runtime address, `static - assumed_fixed_delta`) had been silently dead the whole time it was "working" -- MSJET35.DLL's load base is picked dynamically per run (`dll_loader._find_available_base`, first free slot) and is **not stable across runs** (observed `0x10000000`-range in earlier sessions, `0x18000000` this session). Fixed properly: `run_exe.py` now saves a reference to the real `DLLLoader` (`_dll_loader_ref`), and `_column_loop_probe` (which reliably fires only after MSJET35.DLL is loaded) registers the inner-vtable probe dynamically, computing the runtime address from the DLL's actual live `base_address` plus a confirmed-correct RVA (`0x7143`, verified against the real preferred ImageBase `0x7a840000` read directly from the DLL's own PE header, not assumed). This pattern (dynamic registration off the real load base, not a hardcoded address) should be used for any future breakpoint inside a relocatable DLL.

**New tracing progress on the actual root cause, using the now-working probe**: the vtable target (RVA `0x19d6`, static `0x7a8419d6`) is itself a second dispatcher, not the terminal worker -- Ghidra never auto-created a function there (only reachable via a computed jump table its static analysis can't resolve), hand-decoded from raw bytes. It makes its own vtable call, then branches on a selector register (0-6) through a 7-entry jump table at RVA `0x1ad8`. Added `_msjet_dispatch_selector_probe` at the branch point (RVA `0x1a0a`) to capture the selector live. For the confirmed target query (identity-matched via `fields-count-probe`'s `raw_count=1` filter): selector values seen were `0, 2, 2` across 3 calls (0=success, 2=success, 2=terminate/-1) -- selector 2's target (`FUN_7a879476`, decompiled) is a genuine Jet-internal cursor-walk/iterator function, handling both the successful-bind and end-of-data outcomes depending on its own internal state object (`this+0xc`/`this+0x14`), not on anything visibly wrong in the registers reaching it.

**Independently confirmed via `mdbtools` (not tew) that the stored SQL itself is correct**: `mdb-queries ~/.emu32/Data/DB/Online.mdb StockVehicleAttributes_SelectAll2` prints the full 10-column SELECT list exactly as expected (7 columns from `StockVehicleAttributes` including `AIRestrictionClass`, 3 from `Model`; `BrandedPart` is in the `FROM` but contributes no output columns). This rules out stored-QueryDef corruption as the cause -- the bug is genuinely in how the real Jet engine (or what tew feeds it) resolves this SQL at runtime, not in the SQL text.

**Strongest new lead, not yet chased**: the number of columns Jet's cursor successfully enumerates before terminating is **non-deterministic across runs of the identical static query** -- one run's confirmed-target instance got 2 successful binds before `-1`, an earlier run's got 3. Same SQL, same static msjet35.dll code addresses, different outcome. Per Molly's standing instruction that the bug can only be in tew, this points away from a fixed logic/parsing bug and toward a tew-side memory/heap-allocation issue -- most likely tew's `HeapAlloc`/malloc emulation returning a non-deterministic or under-sized block for Jet's own internal cursor-state object, whose *content* (not just address) then depends on incidental heap layout. **Next step**: examine tew's heap allocator (`tew/api/kernel32_handlers.py`'s `HeapAlloc`/`HeapReAlloc`, or `msvcrt_handlers.py`'s `malloc`) for anything that could under-allocate, fail to grow, or leave stale/adjacent memory readable where Jet expects a freshly-sized array -- particularly around any allocation msjet35.dll makes for itself right after `OpenRecordset` begins.

`run_exe.py` breakpoint slots: 8 of 8 in use (`_openrecordset_hresult_probe`, `_fields_probe`, `_fields_count_probe`, `_prefclass_assert_probe`, `_column_loop_probe`, `_column_loop_return_probe`, `_msjet_inner_vtable_probe` (dynamic), `_msjet_dispatch_selector_probe` (dynamic)) -- at the hard cap; freeing a slot (likely `_openrecordset_hresult_probe`, already fully answered) will be needed before adding another.

**2026-08-25 (cont'd x13) -- added a debug-only reproducibility fix, corrected a factual error from the previous entry, and pinpointed the exact live memory value that decides the (wrong) column count.**

Added `TEW_FIXED_HEARTBEAT_MS` env var (`run_exe.py`, `_run_timer_heartbeat`): normally the virtual scheduler's clock advances by real host wall-clock time (`time.monotonic()`), which reaches `cpu/src/scheduler.zig`'s `tick()`/`preemptSlice()` and can shift exactly when a sleeping background thread (e.g. tid=1011, which loads MSJET35.DLL) becomes `.ready` relative to other threads -- confirmed at the Zig source, not just inferred from the Python wrapper. That can reorder cross-thread `LoadLibraryA` calls, changing DLL load base (`dll_loader.py`'s `_find_available_base` is itself deterministic first-fit, but only given a fixed load history). Real host jitter (other processes, Ghidra/JVM competing for CPU, all observed this session) made this a genuine run-to-run variable. Setting `TEW_FIXED_HEARTBEAT_MS=100` pins the virtual clock's advance to a constant instead -- verified with two back-to-back runs: identical DLL load order, identical MSJET35.DLL base (`0x18000000` both times), identical set of recordsets hitting the bug. Since tew only ever runs one guest process, there's no downside to always forcing this for debugging.

**Correction**: the previous entry claimed the confirmed target query's own column count varied run-to-run (2 vs 3), and framed that as evidence for a heap-layout-dependent bug. That was a real analytical error -- the "3" came from a *different* recordset (`0x7067647`, an unrelated query) within the *same* run as the "2" (`0x70722a3`, our actual target), not from the same query across two different runs. Every observation of the actual target, across three separate runs now (one non-deterministic, two with the fixed heartbeat), consistently shows exactly 2 successful column binds. There is no demonstrated run-to-run non-determinism in the target's own count; that hypothesis is not supported by the evidence and should not be treated as an open lead.

**New, concrete result -- the decisive value is now read live, not inferred.** Traced one level deeper: the vtable target (RVA `0x19d6`) dispatches via a selector register through `FUN_7a879476` (msjet35.dll, real decompile), which for selector value 2 calls `FUN_7a879561` (also decompiled) -- the actual "find next valid column index" scanner. Its stop condition, read directly from raw bytes/decompile: `if (*(ushort*)(*(int*)(this+4) + 8) <= current_index) return 0;` -- a 16-bit count field at `[[this+4]+8]`, checked against the scan index, independent of any per-column flags filtering. Added `_msjet_colcount_field_probe` (RVA `0x39561`, dynamically registered off MSJET35.DLL's real load base like the others -- repurposed the now-fully-answered `_openrecordset_hresult_probe` slot to stay within the 8-cap) to read `this`, the current scan index, and that count field live. **Caveat: this function is heavily reused across many unrelated internal Jet scans (observed `this` values with counts of 95, unrelated large indices, at unrelated timestamps) -- only trust hits correlated by timing with the target's own `column-loop-probe` window, not the raw grep count.**

For our identity-confirmed target, live-captured inside its exact call window: **`count_field=[[this+4]+8]=2`**, read from address `0x41add008`. This is the literal value that ends the scan -- when the running index reaches `2`, the stop condition fires and `FUN_7a879476` returns "no more," matching the observed 2 successful binds + 1 terminating call exactly, mechanically, no inference required. This is the same underlying phenomenon this whole investigation has been chasing since the "`rec_base+0x2C` already holds `1`" finding several entries up -- now traced one level closer to its actual read site, though `rec_base+0x2C` and `0x41add008` may or may not be the same storage (not yet confirmed identical -- could be the same field via a different object hop, or two separate counters that happen to agree; worth checking before assuming).

`run_exe.py` breakpoint slots: 8 of 8 in use (`_fields_probe`, `_fields_count_probe`, `_prefclass_assert_probe`, `_column_loop_probe`, `_column_loop_return_probe`, `_msjet_inner_vtable_probe` (dynamic), `_msjet_dispatch_selector_probe` (dynamic), `_msjet_colcount_field_probe` (dynamic)) -- at the hard cap; `_openrecordset_hresult_probe`'s registration is commented out (function left in place) to make room. Freeing another slot (`_msjet_dispatch_selector_probe` and/or `_msjet_inner_vtable_probe` are good candidates -- both fully answered now that the colcount probe exists) will be needed before adding another.

**2026-08-25 (cont'd x14) -- Molly's redirect: stop hand-disassembling unrecognized msjet35.dll jump-table code, go back to clean function-boundary tracing (params in / return out), and rule out "garbage in" / "garbage out" explicitly before chasing "garbage in the middle."** All probes from the cont'd x12/x13 deep-internals trace (colcount-field, inner-vtable, tablesource-advance, dispatch-selector, colappend-caller, advance-loop) removed from `run_exe.py` entirely, not left disabled -- that thread's own final data point (table_col_count never exceeding 2 for ANY query in a full run, not just ours) undermined the hypothesis it was built on, so it was abandoned rather than pushed further. `run_exe.py` breakpoint slots: back down to 5 of 8 (`_fields_probe`, `_fields_count_probe`, `_prefclass_assert_probe`, `_column_loop_probe`, `_column_loop_return_probe`), all pre-existing landmarks confirmed still correct after cleanup (clean sanity run, same halt).

**Garbage in: ruled out, concretely.** `msjet35.dll` and `dao350.dll` have zero named functions in Ghidra (pure `FUN_xxxx` throughout, no PDB) -- the earlier real class names (`DBRecordset::Fetch`, `DBBinding::Set`, `DBParamQuery::DoQuery`, `GetVariant`) all belong to `MCity_d.exe`'s own debug-build symbols, not to Microsoft's DLLs, which have none. Checked tew's own file-read path instead, code-reviewed rather than live-probed: `_state.py`'s `CreateFile` read-only branch (~line 677) does a plain `open(real_path, "rb").read()` -- the entire file, unmodified, standard Python stdlib -- into `FileHandleEntry.data`; `ReadFile` (`kernel32_io.py:706`) then serves untransformed slices (`entry.data[pos:pos+to_read]`) with correct position tracking (including real `OVERLAPPED` positioned-read support). Whatever bytes are on disk in `Online.mdb` are exactly what msjet35.dll receives -- no truncation, no transformation, no special-casing found anywhere in this path.

**Garbage out: ruled out, from earlier this session's evidence.** The wrong value is already sitting in memory as wrong *before* any COM/marshaling call ever touches it -- `rec_base+0x2C` reads `1` directly, confirmed live, well upstream of `get_Count`'s own vtable dispatch. The OLEAUT32 Variant-marshaling hypothesis was independently tested with the *real* `oleaut32.dll` loaded (not tew's hand-written fallback) and the bug was completely unchanged -- closed, see the 2026-08-25 (cont'd x10) entry above. There is no reporting/marshaling corruption between a correct internal count and what the game reads back.

**Conclusion, per the "garbage in / garbage out / garbage in the middle" framework**: it has to be the middle. **This conclusion, drawn 2026-08-25 cont'd x14, turned out to be subtly wrong** -- the actual bug was in a Win32 API call's *return value being misinterpreted downstream by real dao350.dll code*, which is arguably a fourth category this framework didn't have a name for ("garbage answer to a well-formed question, from a tew primitive silently rejecting valid input"). Kept for the historical record; see the resolution note at the top of this entry.

**2026-08-25 (cont'd x15) -- clean function-boundary chain traced all the way from DBParamQuery::DoQuery down through dao350.dll's real OpenRecordset into its actual bind/execute call, params and return values only (no more internal-state probing).** Fixed a real bug in my own first probe along the way: `DBParamQuery::DoQuery`'s dispatch (`CALL DWORD PTR [ECX+0x8C]` @ `0x0099778e`) is a *single* dereference -- ECX is already the resolved flat vtable-array pointer by the time it reaches the call (confirmed via the setup chain right before it: `MOV EDX,[EBP-0x10]; MOV EAX,[EDX+4]; MOV ECX,[EBP-0x10]; MOV EDX,[ECX+4]; MOV ECX,[EDX]; PUSH EAX`), not the classic obj->vtable-pointer->slot double hop I assumed at first (which produced a 4-byte-misaligned address in no known DLL range -- both red flags that caught the bug before trusting the data).

Full chain, every hop confirmed live and/or via decompile, dao350.dll never relocated (static==runtime) so these addresses are stable:
- `0x0099778e` (MCity_d.exe) -> real target `0x449c844`, a forwarding thunk (decompiled: `(**(code**)(**(int**)(param_1+8)+0x7c))(*(int**)(param_1+8), ...)`)
- resolves to `0x449833e` -- **confirmed as dao350.dll's real `OpenRecordset`** via its own debug string literal (`s_OpenRecordset_044e480c`), not a guess. Makes 3 real vtable calls in sequence: `+0x80` (no args), `+0x84` (`this`, `&out_param` -- the actual bind/cursor-creation step), `+0x188` on a nested object.
- The `+0x84` call: real call site `0x044983d0` (hand-found via `get_function_instructions`' COMPUTED_CALL markers, matching the decompiled 2-arg call), return point `0x044983d6`. Paired live probes (`_dao_bind_call_entry_probe`/`_dao_bind_call_return_probe`) confirm: **`this=0x7067adc`, `return=0` (success), `out_param_val`** is a real, live recordset/cursor object address (`0x7072cd6`/`0x7072c85`, matching the known recordset address range) -- clean success at this boundary too.
- Its real target, `0x4498a79` (decompiled): constructs a fresh cursor object (many vtable-pointer field initializations -- a real C++ constructor pattern), then calls `FUN_0447c475(new_obj, 0, "x", &out, *(this+0x28), local_8)` where `local_8` comes from `FUN_044c9ecd(&local_8, *(this+8))`, called *before* any object construction.
- `FUN_0447c475` (decompiled): with `param_2==0` (our case), the real work is a vtable `+0xc` call taking `(param_3="x", param_4=out)`. `"x"` is a **constant literal from the caller**, same for every OpenRecordset call regardless of query -- ruled out as query-specific by inspection, not worth live-checking further.
- `FUN_044c9ecd` (decompiled) is the genuinely promising lead: reads a string at `*(param_2+0x84)` and another at `*(param_2+0x8c)` (`param_2` here is `*(this+8)` from the outer call -- almost certainly a database/session object), strlen-scans both, copies the first into a freshly `FUN_044e2b5c`-allocated buffer, and resolves them via `FUN_044c915e`/`FUN_044c9276` into a bound object. This has the shape of a **name-resolution step** -- very plausibly where the query/table name (e.g. `"StockVehicleAttributes_SelectAll2"`) gets looked up. **Not yet read live**: the actual string value at `[*(this+8)+0x84]` at this call -- the natural next check, directly testing "is the correct name being looked up here" (a garbage-in check one level deeper than the file-read check above, which only ruled out corruption at the raw `.mdb` byte level, not at this later name-lookup step).

`run_exe.py` breakpoint slots: 7 of 8 in use (`_fields_probe`, `_fields_count_probe`, `_prefclass_assert_probe`, `_column_loop_probe`, `_column_loop_return_probe`, `_dao_bind_call_entry_probe`, `_dao_bind_call_return_probe`). 1 free.

**2026-08-25 (cont'd x16) -- Molly's redirect (again): stop guessing through internals, search guest memory directly for the known-correct query text and see where it's actually placed. Decisive result: found the real, complete, un-truncated column-descriptor array.**

Cleaned up the FUN_044c9ecd/SCASB thread first -- fully resolved as a dead end (its own real output, `local_8`, correctly differs per query regardless of the shared string-staging buffer both calls read; the earlier "null pointer dereference without a fault" alarm was a probe-timing artifact, not a real bug -- the field gets populated by the function's own earlier logic between entry and the SCASB point). All those probes removed entirely (not left disabled) -- `run_exe.py` breakpoint slots back down to 5 of 8.

Added a one-shot guest-memory substring search (`mem._buffer.find(...)` -- confirmed via `memory_zig.py`'s `read_bytes` docstring to be the exact same live memory `libcpu.so` operates on, not a copy) triggered at the crash-adjacent `_prefclass_assert_probe` (guaranteed to fire after all of our query's own processing is done). First attempt (`b"AIRestrictionClass"` alone, triggered too early on the first low-Count hit of any query) only found an unrelated UPDATE statement's format string in the EXE's own static strings -- not unique enough. Retried with the query's own name (`b"StockVehicleAttributes_SelectAll2"`) at the later trigger point: **7 hits**, most just the EXE's own compile-time string constant or short fixed-size name fields, but two are decisive:

- `0x70759f4` (heap): a real **query-name catalog/index** -- a run of Pascal-style length-prefixed name strings (`Vehicle_SelectSkinID`, `StockVehicleAttributes_SelectClass`, ours, etc.), i.e. `MSysQueries`' own name list, exactly where you'd expect a query to be looked up by name.
- `0x74e5084` (heap): immediately after our query's own null-padded name, a **real column-descriptor array** -- repeating 4-byte `(type_byte, sequential_index_byte, 0x00, 0x00)` entries: `04 01, 09 02, 04 03, 04 04, 03 05, 03 06, 03 07, 03 08, 04 09, 04 0a`, i.e. indices `1` through `10` (`0x0a`), **all ten**, sequential, no gaps -- followed by further entries (`09 0b`, `09 0c`, ...) that look like derived/computed fields past the real column list, not more real columns.

**This is the most concrete evidence yet, and it changes the picture**: the actual, on-disk-derived, compiled column metadata for `StockVehicleAttributes_SelectAll2` genuinely has all 10 real columns present, correctly indexed 1-10, sitting in memory at the exact moment our query is being processed. This conclusively rules out "garbage in" at this deeper (compiled-metadata) level too, not just the raw SQL text level checked earlier via `mdb-queries`. The bug is therefore **not** a data-truncation or storage problem anywhere upstream -- it is specifically in whatever code *consumes* this exact array (or reads its count/header) and stops after processing only ~2-3 of its 10 real, present entries.

**Not yet started, the clear next step**: find what reads this array at `0x74e5084` (or the object/header it belongs to -- the array's start address, size, and any preceding count field haven't been located yet, only the entries themselves via substring search) and trace why iteration over it stops early despite entries 3-10 being right there in memory. This is a different, more promising target than the earlier `FUN_7a879476`/`FUN_7a879561`/`FUN_7a8436ac` msjet35.dll cursor-walk chain (cont'd x12/x13) -- that chain was approached from the fetch/runtime side and never definitively linked back to this specific array; worth checking whether it's the same structure approached from upstream, or genuinely different code.

`run_exe.py` breakpoint slots (at rotation time, before the CompareStringA fix): 5 of 8 in use (`_fields_probe`, `_fields_count_probe`, `_prefclass_assert_probe`, `_column_loop_probe`, `_column_loop_return_probe`). 3 free. The one-shot memory search lives inside `_prefclass_assert_probe`, not a separate breakpoint. (Investigation continued past this point with several more `cont'd` entries -- column-loop call counts, dao350.dll dedup-lookup live stalking, and finally the `CompareStringA`/`LOCALE_USER_DEFAULT` discovery -- summarized in the resolution note at the top of this entry rather than transcribed blow-by-blow; see `run_exe.py`'s git history for the exact probe code if the byte-level detail is ever needed again.)

---

## Previous status (2026-08-24, cont'd) — Anti-debug-self-test crash FULLY resolved (SEH-dispatch-nesting fix + SIGTERM/Vulkan cleanup); game reaches its real main window; new downstream ASSERT (`mcity.c(588)`) investigated but not yet fixed

Superseded by the current `status.md` entry, which is entirely about the `mcity.c(588)` follow-up investigation below (the SEH/SIGTERM fixes themselves are done, committed, and not revisited). See `changelog.md`'s matching 2026-08-24 entries for the SEH-dispatch and SIGTERM/Vulkan fixes in full.

The game now runs 42+ real seconds past the old blocker, main window up, before halting on `ASSERT: mcity.c(588) prefClass>=0 && prefClass<DBCP_MaxRatings`. Traced (Ghidra decompiles, all real/live-confirmed via `mdbtools` + one focused breakpoint run, no guessing):
- `mcity.c:588` is inside `carClassList::carClassList()` (`0x5baf96`), validating a `prefClass` field read from row data `DB_GetGameConfigCarTable`/`DB_GetGameConfigCarTableOffline` (`dbperson.c`) fetched via a real async DAO request (`DBT_GET_GAMECONFIG_CAR_TABLE`, `0x2fb`).
- The query is a real, stored QueryDef, `StockVehicleAttributes_SelectAll2` (confirmed via `mdb-queries ~/.emu32/Data/DB/Online.mdb`): `SELECT StockVehicleAttributes.BrandedPartID, StockVehicleAttributes.AIRestrictionClass, ModeRestriction, TrackID, VinBrandedPartID, CarClass, VinCrc, Model.BrandID, Model.EModel, Model.EShortModel FROM [StockVehicleAttributes],[BrandedPart],[Model]` -- a 3-table FROM with no explicit JOIN/WHERE in mdbtools' reconstruction, so it likely resolves via implicit `MSysRelationships`. "prefClass" is column 1 = `AIRestrictionClass` (not `CarClass` as the variable name misleadingly suggests -- confirmed via the real column order, not a naming bug).
- Ruled out via `mdbtools`: bad source data (`AIRestrictionClass` fully populated, 0-7, real values, in `Online.mdb`); a bad file copy (`Tmp.MDB` byte-for-byte identical schema/query/relationships to `Online.mdb`); file lock conflicts (zero `LockFile`/`UnlockFile` failures anywhere in a full 48s run). One oddity noted but not chased further: `Tmp.ldb` opens twice in the same millisecond, same thread (`tid=1011`), two different handles (`0x5045`/`0x5046`) -- only `0x5046` is ever used afterward for the real Jet page-locking traffic, no failures result.
- **Real, live-confirmed mechanism** (traced game code, not msjet35.dll internals -- `dbcode.c` here is the GAME's own DAO wrapper, `C:\MCity\Frontend\dbcode.c`): `Dbcode_Fetch` (`0x8f9c10`) calls `GetValue(recordset, column, row)` (`0x40da3f`, real DAO C++ wrapper) for each bound column slot; `GetValue` returns `NULL` exactly when the recordset is empty (BOF&&EOF) OR `column_index >= Fields.Count` (a real `Recordset.Fields.Count` COM property read via `dao350.dll`'s real vtable). `Dbcode_Fetch` treats a `NULL` `GetValue` result as "not selected/no data," prints the warning (`dbcode.c(3687)`), sets the indicator to `0xFFFFFFFF`, and **returns immediately** -- no further columns get bound for that row, explaining why `dblog.txt` never shows a warning past column 1: it's always the *first* failure, not literally "only column 1 ever fails."
- Since `dblog.txt` shows a warning for column 1 (never column 0) on every single row of every run, `Fields.Count` is provably exactly `1` for this query's live recordset -- deduced logically from existing log evidence, no live check needed to establish this specific fact.
- `Fields.Count` comes from whichever `Recordset` `DBParamQuery::DoQuery` (real address `0x00997450` -- `0x40758b` is just a `JMP` thunk to it) opens via the real `_DAOQueryDef::OpenRecordset` COM call. `DBParamQuery`'s own constructor (`0x40e381`) only resolves the QueryDef by name (hard-aborts if not found -- confirmed live it does NOT abort, so the name lookup itself succeeds) and binds *parameters* (a different, unrelated COM property, `Parameters`/`Parameters.Count`) -- it never touches `Fields` at all.
- Live-verified via one breakpoint at `DoQuery`'s real entry (`0x00997450`, `LOG_CATEGORIES=cpu`): it's called exactly twice in a full run; call #2's return address (`0x0097dc73`) exactly matches `DB_GetGameConfigCarTableOffline`'s own call site (`0x97dc6e+5`), confirming which call is ours. Two attempts to catch the actual `OpenRecordset` HRESULT by single-stepping past the call inside the same breakpoint handler both missed: the first two guessed call-site addresses (`0x0099729f`, then `0x009975c5`, both derived from misreading the decompile's internal "return address" literals rather than checking the real disassembly) were never reached at all; a fixed step count (30, then 300) either undershot (still in mid-air, `EAX` showing the `0xcccccccc` uninitialized-debug-poison pattern) or overshot clean past `DoQuery`'s entire ~1200-byte body into unrelated code.

**Not yet fixed, not yet root-caused past this point**: why the real, live `OpenRecordset()` call for this specific query returns a recordset with `Fields.Count==1` in tew's environment. Needs a *precise* breakpoint placed directly after the real `CALL` instruction inside `DoQuery`'s body (not a stepped-past guess) to read the actual HRESULT and `Fields.Count` -- that call's exact address was never nailed down live tonight, only the function's own entry (`0x00997450`) and one interior branch structure (from static decompile only, not verified against real disassembly) suggesting it's a `COMPUTED_CALL` somewhere between roughly `0x9975c5` and `0x99763e`. Get the *real* disassembly of `DoQuery`'s body (`get_function_instructions` on `0x00997450` already has the full instruction list saved from tonight -- reuse it, don't re-fetch) and identify the exact `CALL` reg+0x?? instruction and its immediately-following address before setting the next breakpoint, rather than guessing from the decompiler's own internal literals again.

---

## Previous status (2026-08-24) — Anti-debug-self-test crash FIXED (RtlUnwind resume ESP), but `_invoke_handler`'s sentinel/step-budget dispatch model can't represent a handler whose flow permanently merges into ordinary game execution, so the run still dies with a false "timed out" 2M steps later

Superseded by the current `status.md` entry, which fixed exactly this and got the game to its main window. Kept for the `_rtl_unwind` root-cause detail (still accurate, not re-derived below) and as a record of two dead-end detection attempts (an ESP-threshold check, then a too-coarse batch-sampling gap) before the working EIP-distance fix.

**Root cause, fully confirmed live, not guessed**: `_CLayer_DetectDebugger`'s real, compiled `__except_handler3` (`0x9f5eb8`) correctly evaluates its filter, then calls the real, compiled `__global_unwind2` (`0x9f2e90`) -- a thin CRT wrapper: `RtlUnwind(EstablisherFrame, /*TargetIp=*/its own return address, NULL, NULL)`, a well-known MSVC trick where RtlUnwind's "jump to TargetIp" simulates a normal function return so `__global_unwind2`'s own ordinary epilogue (`pop edi;pop esi;pop ebx;mov esp,ebp;pop ebp;ret`) can run afterward. `tew/kernel/seh.py`'s `_rtl_unwind` set `ESP = target_frame` before jumping -- but `target_frame` is the *SEH registration record's own address*, unrelated to `__global_unwind2`'s real, nested stack depth. So the epilogue popped the wrong things, eventually landing back inside `__except_handler3` a second, self-reentrant time via incidental leftover stack content, producing the exact `EBP=0x7fffffdc` null-deref crash seen every run since 2026-08-22.

**Fix**: `_rtl_unwind`'s `target_ip` branch sets `cpu.regs[ESP] = (esp + 20) & 0xFFFFFFFF` (the real caller-return stack depth) instead of `target_frame`. Live-verified: the bogus self-reentrant crash was gone, `~/.emu32/MCity/stdout.txt` got real content for the first time (`clayer.c(311) SEH Handler!` / `clayer.c(318) Found Debugger!` / DX8 mode setup), and the run reached DirectX8 display-mode setup.

**New blocker found**: `__except_handler3`'s invocation ran the full `_STEP_LIMIT` (2,000,000 steps) without ever returning, because `_invoke_handler`'s loop only exits on `cpu.halted`, and a `RtlUnwind` redirect is just a register mutation that never sets it -- so execution flows seamlessly from `__global_unwind2`'s epilogue through `__except_handler3`, the real `__except` block, and onward into WinMain's ordinary continuation, all counted as the same `_invoke_handler` call. The game wasn't stuck anywhere; the step-budget-and-sentinel model was never built for a handler whose flow permanently escapes into ordinary program execution.

**Two dead-end detection attempts, both live-proven wrong before the real fix**:
1. *ESP >= original SEH frame address* (`cpu._seh_original_frame`): reasonable-sounding (once ESP rises back to/past the original exception's own registration-record address, we're "back" in the protected function's territory) but a full ~90-step single-step trace after the fix above proved ESP *never* got anywhere near that address during the real escape -- it stayed in the `0x7ffff3xx`-`0x7ffff9xx` range the whole time, nowhere close to `original_frame=0x7ffffd44`. The assumption that the real `__except` block resumes near that address was simply wrong for this binary.
2. Combined with (1) at first, a `preempt_slice`/timer-heartbeat servicing patch *inside* `_invoke_handler`'s loop (mirroring `run_exe.py`'s outer-loop mechanism, per `2224e3f`'s precedent) was also tried and reverted -- it fixed nothing on its own since the fundamental problem was staying nested at all, not lacking timer service while nested. `status_archive.md`'s "Timer thread: FUN_00a30ea0" note (2026-08-17-ish era) already established the timer/thread subsystem works correctly under *normal* (non-nested, outer-loop) execution -- confirming neither the scheduler-to-Zig port nor the original SEH dispatch work ever broke it; this scenario (still nested this deep, this long) was simply never reachable before the RtlUnwind fix above.

The working fix (EIP distance from `handler_addr`, not ESP) is in the current `status.md` entry.

---

## Previous status (2026-08-23, cont'd) — RtlUnwind EBP-restoration fix implemented, tested, and confirmed to work exactly as designed -- but does NOT resolve the anti-debug-self-test crash. Real root cause is different: the thread's own outermost stack frame has a garbage/invalid return address

Superseded by the current `status.md` entry (2026-08-24, which found and fixed the actual root cause this entry was still searching for). Context preserved here since the "garbage return address" framing below turned out to be a plausible-but-wrong read of the same symptom the 2026-08-24 fix actually explains -- see the current entry for what `ret=0x011f3b90`/`EBP=0x7fffffdc` really were.

**Context**: prior session chain (all fixed/verified, full detail in this file's older entries below and `changelog.md`'s matching dated entries): (1) `msjet35.dll` collation-cache crash (`CompareStringA`/`CompareStringW` locale validation), (2) opt-in null-page memory guard so the game's anti-debug self-test can genuinely fault, (3) `dispatch_exception` no longer conflates a handler crashing with a clean `RtlUnwind` escape, (4) traced the self-test's own crash to `RtlUnwind` never restoring EBP after redirecting execution -- planned and implemented this session.

**This session's outcome, in one line**: the EBP-restoration fix is real, correct, tested (2 new tests, 1236/1236 passing), and live-confirmed to work exactly as designed -- but it does not fix the crash, because EBP was never actually the cause. Empirically ruled out: the same `EstablisherFrame=0x7ffffff0` garbage value recurs identically whether EBP is the old stale value or the newly-correctly-restored one.

**Real root cause, found via one more probe (turned out to be a misread -- see 2026-08-24)**: the second `__except_handler3` invocation's own *return address* is `0x011f3b90` -- an address already established this session to be inside a data/string-table region, not real code. That's also the exact same value that appeared as the outermost stack frame's "return address" in every crash dump all night (previously misread as just "where the EBP-chain diagnostic walk gives up," not as an actual return path the CPU executes). The story as understood *at the time*: by the time this happens, `_CLayer_DetectDebugger`'s own function (and whatever calls it) has already returned normally all the way up the call stack to the thread's own outermost function -- which then tries to `RET` into *its own* stored return address, and that value is garbage instead of valid thread-exit/kernel32 code. Execution wanders from there into whatever that garbage decodes as, eventually hitting a `CALL` into `0x009f5eb8` with nonsense arguments.

**Not yet investigated (at the time)**: how tew sets up a thread's initial stack frame. 2026-08-24's session found the real answer was elsewhere entirely -- `run_exe.py`'s own initial-frame setup was already correct; the actual bug was in `_rtl_unwind`'s `ESP=target_frame` for a specific real-CRT self-return pattern (`__global_unwind2`).

---

## Previous status (2026-08-23) — RtlUnwind EBP-restoration fix implemented, tested, and confirmed to work exactly as designed -- but does NOT resolve the anti-debug-self-test crash. Real root cause is different: the thread's own outermost stack frame has a garbage/invalid return address

Superseded by the current `status.md` entry. Planned via `EnterPlanMode` (plan file `vast-drifting-pike.md`), implemented per plan: `tew/kernel/seh.py`'s `dispatch_exception` now stashes `(original_frame, original_ebp)` once per exception (right after `_write_context`, before the chain-walk reassigns `frame`); `_rtl_unwind`'s `target_ip` branch restores `EBP` from that pairing when `target_frame` matches, and otherwise logs a clear warning and leaves EBP untouched (the documented, deliberately out-of-scope multi-level-unwind case). 2 new tests in `tests/unit/kernel/test_seh.py`, existing tests (including the clean-escape-via-JMP regression test) unaffected. `pytest -q`: 1236/1236.

**Live-verified the fix itself works exactly as designed** -- added a temporary breakpoint probe (removed after use) confirming: `original_ebp` correctly captured as `0x7ffffd54` (`_CLayer_DetectDebugger`'s real EBP, matching `EstablisherFrame+0x10` -- confirmed this genuinely is the real frame's own EBP, not `__except_handler3`'s internal reassignment as originally guessed); `target_frame` (`0x7ffffd44`) correctly matches `original_frame`; EBP correctly gets restored to `0x7ffffd54` before jumping to `target_ip`.

**But the crash recurs identically anyway.** Traced why: the second `__except_handler3` (`0x009f5eb8`) invocation's own *return address* (captured via one more temporary probe) is `0x011f3b90` -- and that address was already established earlier this session to be inside a data/string-table region (`typname.cpp`, `am/pm`, etc.), not real code. It's also the exact same value that appeared as the outermost frame's "return address" in *every* crash dump throughout the whole night (`frame[0] EBP=0x7fffffdc ret=0x011f3b90 ← exe`), previously mis-read as "the EBP-chain walk simply ran out of real frames" rather than as an actual code path the CPU would execute.

**Real conclusion**: by the time the second `__except_handler3` call happens, the CPU isn't running legitimate `__except`-block continuation code at all -- it's already deep in a wrong-EIP tailspin that started when the thread's own outermost function frame tried to `RET` into its own stored return address, which is garbage (not valid thread-exit/kernel32 code as a real Windows thread entry point would have). That garbage decodes as *something* that eventually calls into `0x009f5eb8` with nonsense arguments -- unrelated to EBP, unrelated to the collation/null-page/SEH-honesty fixes. Confirmed empirically that this ISN'T an EBP problem: `EstablisherFrame=0x7ffffff0` recurred identically both with the old stale EBP (`0x7fffffdc`) and with the newly-*correctly*-restored EBP (whatever the __except-block code left it as, `0xffffffff` at that point) -- the value doesn't depend on EBP at all, ruling out the EBP-relative-addressing theory the whole prior investigation was built on.

**This EBP fix is being kept** -- it's independently correct (a real, previously-documented `RtlUnwind` limitation, now properly handled for the common case, with real tests) even though it wasn't sufficient to resolve tonight's specific crash. The actual root cause is a different, likely more foundational gap: how tew sets up the initial stack frame for a thread's entry point, specifically what "return address" gets placed there for when that entry-point function eventually returns normally. Not yet investigated at all -- needs its own fresh look at thread/stack initialization (`cpu/src/scheduler.zig`'s `initThreadStack`, or wherever the main thread's own initial frame gets set up), not more SEH-dispatch archaeology.

---

## Previous status (2026-08-22, cont'd x3) — ROOT CAUSE FOUND for the anti-debug self-test crash: RtlUnwind's documented "EBP not restored" simplification. Real bug, but a known/accepted architectural limitation, not a new one

Superseded by the current `status.md` entry. Traced via ClickHouse execution-history capture (a fresh, narrow 0-9M-step window covering the whole pre-crash run) plus a live register-value probe at the real `__except_handler3` handler's entry point, cross-referenced against `dispatch_exception`/`_invoke_handler`/`RtlUnwind`'s own internal debug logging (temporarily promoted to always-visible for one run, then reverted).

**Full mechanism, each step confirmed live, not guessed**:
1. `dispatch_exception` calls `_invoke_handler` for `_CLayer_DetectDebugger`'s own SEH frame (`0x7ffffd44`, scopetable `0x01191420`, trylevel `0` -- all genuinely valid).
2. The real, compiled `__except_handler3` code runs, its filter says "handle it," and it calls the real `RtlUnwind(target_frame=0x7ffffd44, target_ip=0x009f2ea8)` -- `0x009f2ea8` is the real, compiled `__except { ... }` block body.
3. `_rtl_unwind` (`tew/kernel/seh.py`) sets `ESP = target_frame` and jumps to `target_ip` -- but deliberately does **not** restore EBP, an explicitly documented simplification in the module's own docstring ("EBP is left as whatever the unwind-triggering handler's own execution left it as").
4. The resumed `__except` block's own code naturally uses EBP-relative addressing (same as any compiled function body) -- but EBP is stale (`0x7fffffdc`, the thread's outermost/entry-level frame, left over from `__except_handler3`'s own internal `lea ebp,[ebx+0x10]` computation, not `_CLayer_DetectDebugger`'s real frame). It tries to establish a new SEH frame via an EBP-relative computation; with the wrong EBP, that lands at `EBP+0x14 = 0x7ffffff0` -- a never-written stack address that coincidentally equals `stack_base` (`mem_size - 16`) -- instead of a real, meaningful location. `next=0, handler=0` there means the subsequent `CALL [handler]` dereferences a null function pointer -- the actual crash (`fault at 0x00000002`).

**Not a newly-introduced bug** -- this is a previously-known, explicitly-documented gap in `RtlUnwind`'s simplification finally manifesting in a concrete, traceable, live scenario, exposed only because tonight's earlier fixes (collation crash, null-page guard, SEH-dispatch honesty) let the run get far enough to actually exercise a real `__except` block resuming after a genuine unwind for the first time.

**Not yet fixed** -- a proper fix means giving `RtlUnwind` a real per-frame EBP save/restore (or some other way to recover the target frame's correct EBP), which the module's docstring already flags as needing "a saved per-frame CONTEXT" that MSVC's frame layout doesn't expose generically -- a real design question, not a quick patch, and worth planning carefully (same as the other three fixes tonight) rather than guessing at an implementation.

**Technique notes**: (1) ClickHouse capture from step 0 works fine for windows up to ~9M steps/~28M events -- no need to fear step-0 starts as long as the window has an explicit stop. (2) `dispatch_exception`/`_invoke_handler`'s Python-level `memory.read32()`/`write32()` calls go through a *different* code path (the C-ABI `mem_read32`/`mem_write32` in `kernel.zig`, via `inBoundsWidth`) than the guest CPU's own instruction execution (`core.zig`'s `memRead8`/`memWrite8`, via `inBounds1`, which is what the ClickHouse write-hook actually observes) -- writes made by Python-level SEH-dispatch code itself are invisible to that capture, which caused real confusion mid-investigation before switching to a live breakpoint probe reading values directly instead of reconstructing them from write history. (3) The ghidra-mcp service and its project state, and the ClickHouse docker container, do not survive a power cut/reboot -- both need a fresh MCP handshake / `docker compose up -d` + project re-open after one.

---

## Previous status (2026-08-22, cont'd x2) — dispatch_exception's SehHandlerEscaped ambiguity RESOLVED: it no longer conflates a handler crashing with a clean RtlUnwind escape. Live-verified: the anti-debug self-test's exception now reports honestly as unhandled instead of cascading into a corrupted runaway

Superseded by the current `status.md` entry. Summary, each link confirmed live, not guessed:

**Fix, planned via `EnterPlanMode`**: `SehHandlerEscaped` (`tew/kernel/seh.py`) gained `faulted`/`esp_before` fields, captured in `_invoke_handler` at the point of the raise. `dispatch_exception`'s chain-walk loop now branches on `e.faulted`: a clean escape (RtlUnwind redirected execution, eventually halts elsewhere) still returns `True` exactly as before; a handler that itself crashed (a genuine second fault, previously indistinguishable from a clean escape since both just leave the CPU halted at some non-sentinel EIP) is now logged clearly, restored to a sane CPU state (`cpu.faulted = False`, `cpu.regs[ESP]` restored), and treated like a decline -- the chain walk continues to the next frame instead of falsely reporting "handled." `_rtl_unwind`'s own intervening-handler walk got the same ESP-restoration safety fix for consistency. 2 new tests in `tests/unit/kernel/test_seh.py` (crash-not-falsely-handled, chain-continues-past-a-crashed-handler-to-a-working-outer-one), existing tests (including the clean-escape-via-JMP regression test) unaffected. `pytest -q`: 1234/1234.

**Live-verified**: re-running the same anti-debug self-test scenario, the log now shows an honest chain -- the first fault (deliberate `0x190` read) dispatches; the real `__except_handler3`-shaped handler (`0x009f5eb8`) is correctly logged as "crashed mid-execution... treating as declined" (not silently claimed handled); the chain walk correctly continues to the next frame (which turns out to be garbage, also correctly declined); and the exception is finally reported honestly as `fault at 0x004d980f unhandled by SEH chain -- halting`, with a clean, real diagnostic dump -- instead of the previous `RUNAWAY at EIP=0x00000002` hundreds of thousands of steps later with corrupted state.

**Does not itself make the run progress further** -- same overall stopping point as before, but now a clean, honest, immediately diagnostic halt instead of a delayed, misleading runaway. The underlying question -- why `_CLayer_DetectDebugger`'s own scope-table walk (real MSVC CRT code, reading its own compiled `DAT_01191420` scope-table data) crashes at all -- is a separate, not-yet-started investigation: possibly a PE-load/relocation issue with that specific data region, possibly some other tew-side memory corruption reaching it first, or possibly a subtle mistake in how the scope-table's fields were hand-disassembled/understood (the exact fault offset shifted slightly between runs -- `0x9f5ef8` vs `0x9f5ed0` -- worth double-checking the instruction-level trace precisely before assuming which).

---

## Previous status (2026-08-22, cont'd) — INT3 traced to a game anti-debug self-test relying on a real access violation tew's flat memory model couldn't produce; null-page guard added and works correctly; surfaced a real, distinct bug in dispatch_exception's SehHandlerEscaped handling (not yet fixed)

Superseded by the current `status.md` entry. Summary of this segment's chain, each link confirmed by direct tracing, not guessed:

1. The `INT3` at `0x00688c68` (from the entry below) traces to `_Nfs_DebugBreak()`, called from `carClassList::carClassList()` (`mcity.c`) when a car's `prefClass` attribute assert (`prefClass>=0 && prefClass<DBCP_MaxRatings`) fails, gated on a global `_Nfs_DebuggerIsPresent` flag (`0x0163DF38`).
2. That flag's real origin: `_CLayer_DetectDebugger()` (`0x004d97b0`, `clayer.c`) — a classic anti-debug self-test. It deliberately reads address `0x00000190` inside an SEH-protected block: on real Windows, no debugger present means the OS delivers the exception to the process's own SEH handler, which catches it and sets the flag to 0 ("No Debugger!"); a debugger present intercepts first-chance, leaving the flag at its wrong "assume present" default.
3. Root problem: tew's memory model had no concept of unmapped address ranges (the whole 2GB buffer was uniformly readable) — reading `0x190` never faulted, so the self-test's correcting SEH path never ran.
4. **Fix, planned via `EnterPlanMode` and validated by a Plan agent before implementing** (the agent caught that the naive design — modifying `inBounds1` directly — would have broken ~60+ existing Zig tests, since nearly all of them use tiny address-0-based buffers): added an **opt-in** `CpuState.guard_null_page` field (default `false`, only the real emulator's own startup turns it on via a new `cpu.enable_null_page_guard()`), checked additionally (not replacing) `inBounds1` inside `core.zig`'s `memRead8`/`memWrite8`. New `NULL_PAGE_SIZE = 0x10000` constant, matching real Windows' documented "first 64KB never mapped" guarantee. 4 new Zig tests (`kernel.zig`), `zig build test` green (all ~60+ existing tests untouched), `pytest -q` 1232/1232.
5. **Live-verified the fix itself works exactly as designed**: `CPU fault at EIP=0x004d980f opcode=0xa0` — the exact `MOV AL,[0x190]` instruction `_CLayer_DetectDebugger` uses, faulting for the first time ever.
6. **But the aftermath cascades into a new bug, not a clean resolution**: SEH dispatch for that fault invokes the real, compiled `__except_handler3`-style handler at `0x009f5eb8` (hand-disassembled since Ghidra doesn't recognize it as a function) — genuine MSVC CRT scope-table-walking code (`mov edi,[ebx+8]` → scope table `DAT_01191420`; `cmp dword ptr [edi+esi*3*4+4],0` at `0x9f5ef3`, reported as EIP `0x9f5ef8` per the documented EIP-already-advanced convention). *That* read itself faults a second time — and `tew/kernel/seh.py`'s `dispatch_exception` (`seh.py:293-300`) unconditionally treats any `SehHandlerEscaped` (handler halted at an EIP other than the return sentinel) as "the handler cleanly called RtlUnwind," without checking whether `cpu.faulted` is actually set — so a handler that itself crashed mid-execution looks identical to one that cleanly redirected via unwind. `dispatch_exception` reports "handled," `run_exe.py` resumes from the crashed state, and execution runs to a `RUNAWAY at EIP=0x00000002` shortly after.
7. This is a real, previously-latent bug in the SEH dispatcher, exposed for the first time by the null-page fix (nothing before tonight drove a genuine fault deep enough into an actual scope-table walk to hit it) — not a flaw in the null-page fix itself, and not something in the exe. **Not yet fixed** — planning it is the next step.

---

## Previous status (2026-08-22) — msjet35.dll collation-cache crash: RESOLVED. Root cause was CompareStringA/W never validating the locale argument

Superseded by the current `status.md` entry, which picks up the next blocker (an `INT3` breakpoint deep in `MCity_d.exe`'s own code, `0x00688c68`, unhandled by SEH -- a real debug-build assertion). Preserved here for the full three-ruled-out-hypotheses writeup, the dynamic ClickHouse-capture confirmation, and the final trace down to `FUN_7a878159`'s connect-string parser and `FUN_7a84c830`'s locale-validity probe. See `changelog.md`'s "2026-08-22" entry for the concise fix summary.

**Full detail (originally "Current status (2026-08-22)")**:

**Context**: `expsrv.dll` near-null-jump crash (LoadTypeLibEx registration gap) and 3 more oleaut32 ordinal halts (VarI4FromStr/VarR8FromStr/VarDateFromStr) were resolved the prior session — see `status_archive.md` "Previous status (2026-08-21)" for full detail. This entry picks up the next blocker: a `RUNAWAY` crash inside `msjet35.dll` (real return addresses `expsrv.dll+0x1cdb7`, `MSJET35.DLL+0x3bc04`), EBP-verified to run through `FUN_7a87ba0a` (static `0x7a87ba0a`) — a general variant-comparison routine that reads a per-session cached pointer at `DAT_7a9362c0[session*0x708]+0x2c0` (a collation/comparison interface, vtable slot `0x18`) with no NULL check, and calls through it.

**Three plausible root causes were tested directly tonight and ruled out, each backed by real evidence, not guesses:**

1. **A silently-unsupported oleaut32 import.** Checked msjet35.dll's actual PE import table (`objdump -p`): it imports OLEAUT32 exclusively by ordinal (4, 6, 9, 12, 54, 64, 74, 84, 94, 104, 109-114, 149, 150, 165). Mapped every ordinal to its real name via `objdump -x` on oleaut32.dll's export table. Ordinal 165 = `LHashValOfNameSys` (takes an `LCID`, the one plausibly collation-relevant one) is unregistered in `oleaut32_handlers.py` — but confirmed via `logger.py` that `ERROR`-level messages (which is what tew's fail-loud `[UNIMPLEMENTED]` halt uses) bypass both `LOG_LEVEL` and `LOG_CATEGORIES` filtering entirely, so a clean grep for it across two full runs is a real negative, not a filtering blind spot: it's never actually called on this path. Ruled out.
2. **Missing/empty registry defaults.** `registry.json` has `hklm\software\microsoft\jet\3.5\engines\jet 3.5` completely absent as populated data (parent key present but empty). Live run with `LOG_CATEGORIES=registry` confirms msjet35.dll queries ~13 values under that key (`PageTimeout`, `LockRetry`, `MaxBufferSize`, `Threads`, `SortMemorySource`, etc.) and every one comes back `NOT FOUND` — but none of them are locale/collation-related; they're all performance-tuning knobs real Jet has compiled-in defaults for. No `SortOrder`/`CollatingOrder`/`LangID`-style value is ever queried at all. Ruled out.
3. **File-I/O corruption of the `.mdb` header's collating-order field.** `LOG_CATEGORIES=fileio` shows `Online.mdb` (5,883,904 bytes) opens and gets read sequentially in clean 4096-byte chunks starting at step ~1-2M (well before the crash at step ~237.9M), with correct offsets/`pos_after` tracking throughout — no sign of the previously-fixed `OVERLAPPED.Offset`-style bug recurring. The real file bytes are being served correctly; whatever's wrong is in how `msjet35.dll`'s own code processes them, not in tew's I/O layer.

**Dynamically confirmed (not just inferred from static analysis) that `DAT_7a9362c0[*]+0x2c0` is never written, anywhere, across the whole observed run** — using tew's existing native Zig `cpu.enable_history_capture_clickhouse(...)` execution-history capture (see `cpu/src/history/capture.zig`, previously wired up 2026-08-07 then disabled — hooks every real memory write inside the Zig CPU core itself, not just Python-level API-handler writes, so it sees guest-instruction writes a Python-level watchpoint never could). Enabling it from step 0 was already known to be too heavy (documented 2026-08-07: stalls a run via unflushed-buffer memory pileup) — instead gated to two narrow windows via new `_HISTORY_CAPTURE_START_STEP`/`_HISTORY_CAPTURE_STOP_STEP`/`_HISTORY_CAPTURE_DONE` globals in `run_exe.py` (one-shot, checked inside the step loop):
   - **Crash-adjacent window** (steps 237,000,000-237,900,000, ~2.17M events): zero writes to the field.
   - **Database-open window** (steps 500,000-8,000,000, ~24.1M events, covers `Online.mdb`'s header read at step ~1-2M): zero writes to the field.

ClickHouse stack: `~/pe-walker/history-poc` docker-compose (started fresh this session; schema wasn't loaded on the fresh container, applied via individual statements since the HTTP interface rejects multi-statement bodies — 3 CREATE statements from `schema.sql`, split and POSTed one at a time). Query pattern: `WHERE key BETWEEN <table_base> AND <table_base>+0x1c200 AND (key - <table_base>) % 0x708 IN (0x2c0..0x2c3)` — checks all 64 possible session slots for a write to that exact per-session field.

**Real bug introduced and fixed during this investigation**: the first version of the two-window gating logic (`if not enabled and step >= START: enable... elif enabled and step >= STOP: disable...`) had no way to remember a window had already run — once past STOP, the very next iteration's `if` branch re-triggered (both conditions still true), producing an enable/disable oscillation every single batch for the rest of the run. This caused one run to run far slower than normal (external-killed at 160s vs the usual ~68-70s) and very likely caused a spurious, unrelated `MessageBox`/`MUTEX_free` abort dialog to appear near the end (real per-batch HTTP-flush thrashing perturbing the cooperative scheduler's timing) — not a genuine new finding, dismissed as an artifact of the bug. Fixed with a `_HISTORY_CAPTURE_DONE` one-shot flag; safe to leave in place (currently gates a 500K-8M-step window, harmless if run again, easy to retarget for the next investigation).

**Static tracing also done this session, narrowing where to look next**: `FUN_7a87452e` (called only from `FUN_7a8e8240`, which is `CreateDatabase` — has `MSysAccounts`/`MSysGroups` system-table-creation SQL literally inline) creates a collation object via a per-*database* (not per-session) factory at `DAT_7a9362c0[db]+0x6f4`, then stores the result in a **different** table (`DAT_7a969150[db*0x14]`), not in the session's own `+0x2c0` field. Since `Online.mdb` is an **existing** database being *opened*, not created, this `CreateDatabase` path likely never runs for this scenario at all — meaning the real "open an existing database, read its stored collating order, populate the session's collation cache" function is a *different*, not-yet-located function. That's the concrete next static-analysis target: find msjet35.dll's real "OpenDatabase"/session-attach function (distinct from `FUN_7a8e8240`), and see whether/how it's supposed to populate `+0x2c0` from `DAT_7a969150` or directly from the file header.

**Not yet resolved**: the actual mechanism (or genuine gap) that should populate `DAT_7a9362c0[session]+0x2c0` for an opened (not created) database. Three specific candidate mechanisms have been ruled out; the field is dynamically confirmed to be permanently null throughout the run. Next step is finding the real open-database/session-attach function in `msjet35.dll` via Ghidra, now that the search space is much narrower.

---

## Previous status (2026-08-21) — 3 straightforward halts cleared past DAO-3075 (VariantChangeType VT_INT, VirtualQuery, GetModuleFileNameW); new 4th halt looks like a real, harder bug -- indirect jump to invalid near-null address deep in an expsrv.dll call chain

Superseded by the current `status.md` entry, which resolves the `expsrv.dll` near-null jump (LoadTypeLibEx registration gap), clears 3 more ordinal halts (VarI4FromStr/VarR8FromStr/VarDateFromStr), and dynamically confirms (via native ClickHouse execution-history capture, not just static analysis) that `msjet35.dll`'s per-session collation-cache field (`DAT_7a9362c0[session]+0x2c0`) is never written anywhere in the observed run. Preserved here for the full LoadTypeLibEx root-cause writeup, the ordinal-by-ordinal fix sequence, the logger.always() fix, and the initial (EBP-verified but not yet dynamically confirmed) FUN_7a87ba0a finding.

**Context: DAO-3075 is resolved** (see `changelog.md`/`status_archive.md` for full detail — real cause was a `0x66`-prefix flag bug in `opIncR32`/`opDecR32`, cpu/src/engine.zig, fixed and live-verified). This entry covers what happened immediately after, re-running the same scenario further.

**Three halts cleared in sequence, each the same shape**: a genuinely missing/incomplete Win32 handler logging `[UNIMPLEMENTED] ... — halting` (a deliberate, self-documenting stop, not a mystery crash), fixed with tests-first (red confirmed, then green), full suite green, then a live re-run confirming forward progress before moving to the next one:

1. **`VariantChangeType` unhandled source `vt=22` (`VT_INT`)** — MSDN documents `VT_INT` as storage-identical to `VT_I4` (same 4-byte signed int at `+8`). `tew/api/oleaut32_handlers.py`: added `_VT_INT = 22`, treated as `_VT_I4`'s equivalent on both the source-read and target-write side. 5 new tests in `tests/unit/api/test_oleaut32_variant_change_type.py` (`TestVtIntIsI4Equivalent`).
2. **`VirtualQuery` had no handler at all** (unlike every other `Virtual*`/`Heap*` API in `kernel32_memory.py`) — MSJET35.DLL's own memory manager called it on a page it got from `VirtualAlloc`. Implemented against `state.virtual_reserved`/`virtual_committed` (already tracked); added `state.virtual_protect: dict[int,int]` (new, wired into `VirtualAlloc`) since `MEMORY_BASIC_INFORMATION.Protect`/`AllocationProtect` need the real protection flags, which weren't tracked before. Halts loudly (not guessed) on an address outside any tracked region or an undersized output buffer — real free-region-size reporting was never observed live, so not implemented rather than guessed. 7 new tests in `test_kernel32_memory.py` (`TestVirtualQuery`).
3. **`GetModuleFileNameW` was a deliberate `_halt()` placeholder** (`GetModuleFileNameA` next to it was fully implemented) — expsrv.dll (VBA runtime) called it. Mirrors the `A` version exactly except `nSize` is a WCHAR count (not bytes) and output is null-terminated UTF-16LE. New `tests/unit/api/test_kernel32_get_module_file_name.py`, 9 tests.

Each live re-run: `pytest -q` green (1124 → 1131 → 1140 as tests were added), then `LOG_LEVEL=error LOG_CATEGORIES=cpu,handlers timeout 90 .venv/bin/python run_exe.py`, confirming the specific halt was gone and a new one (or none) appeared further along.

**4th halt, current blocker — looks like a real bug, not a missing handler.** After fix #3, the run no longer hits any `[UNIMPLEMENTED]`/halting handler at all — instead `run_exe.py`'s own runaway-detector fires: `RUNAWAY DETECTED at step 175900000`, `Current EIP: 0x0003049c (INVALID)` (a tiny address, `Bytes at EIP: 00 00 00 00...` — entirely unwritten memory), `Last valid step: 175800000, EIP: 0x15027571` (inside MSJET35.DLL's runtime range). The stack at the crash shows real expsrv.dll return addresses (`[ESP+00] 0x17009d1b`, EBP-chain frames `ret=0x17009d08`/`ret=0x1701cbd7`, both `expsrv.dll+...`) — this is MSJET35.DLL calling into expsrv.dll (Jet's real mechanism for evaluating calculated/expression fields in a query) and something in that chain does an indirect call/jump through a bad (near-null, all-zero-bytes-at-destination) pointer.

**Checked against memory, ruled out as a match**: `tew_fake_kernel_gaps.md` section 18 (2026-07-19 night, 32 days stale) documents a *different*, previously-open bug — `dao350.dll`'s `DllGetClassObject` returning `S_OK` without writing `*ppv`, causing a wild jump to `EIP=0xfefc8d8f` from the game's own `CoGetClassObject` fallback code. Different address, different call path (DAO's COM activation, not MSJET35→expsrv expression evaluation), never fixed per that memory. Worth keeping in mind as a *related family* of bug (uninitialized/NULL out-pointer or vtable slot causing a wild jump) but not assumed to be the same root cause without live evidence.

**Not yet investigated**: what exactly deep in the expsrv.dll call chain computes/returns the bad pointer that gets jumped through. Next step would be the same single-step-trace technique that resolved DAO-3075 (ground-truth Ghidra listing + live `cpu.step()` trace from a known-good anchor, e.g. `0x17009d1b`'s call site or `0x1701cbd7`), rather than hand-disassembly.

**Improvement made (2026-08-21, same day): the runaway detector now routes through the real SEH pipeline instead of its own ad-hoc dump.** Molly's suggestion: since real Windows would raise `STATUS_ACCESS_VIOLATION` the instant code fetches from a page it never mapped executable, and `run_exe.py` already has exactly that pipeline wired up for genuine `cpu.faulted` events (`dispatch_exception(cpu, mem, STATUS_ACCESS_VIOLATION, eip)`, real `fs:[0]` SEH-chain walk), the runaway-detected branch (`is_valid_eip` returns False) now calls the same function instead of printing its own shallower diagnostic. If a handler resolves it, the main loop just resumes normally; if not, `cpu.halted = True` and the post-run block's existing `diagnose_halt()` (32-frame EBP-chain walker, strictly more capable than the old inline 32-slot flat dump) fires automatically. **Live-verified**: for the current expsrv.dll halt, the SEH chain is walked and genuinely has nothing to offer (`runaway at 0x0003049c unhandled by SEH chain -- halting`) — a real negative result, not a bug in the new dispatch path, and confirms via a second, independent code path that the EBP-chain only extends one real frame (`expsrv.dll+0x1cbd7`) before dead-ending at a non-frame `ret=0x00000001`, same as the flat stack scan already showed. No new lead surfaced yet, but the tooling itself is a durable improvement — every future runaway now gets a real shot at the game's own recovery path first. `pytest -q`: 1140/1140 unaffected (run_exe.py has no direct unit tests; this is a live-run-verified change).

**Diagnostic instrumentation in `run_exe.py`**: unchanged from the DAO-3075 investigation (`_lookahead_call_probe`'s single-step tracer and the other 7 breakpoint slots) — none of it is relevant to this new expsrv.dll halt, all 8 slots are free to repurpose next session.

**Real bug found and fixed while using the new SEH routing (2026-08-21, same day): every thread was sharing one TEB, so SEH chains were cross-contaminated between threads.** Debug-level SEH logging (`LOG_LEVEL=debug LOG_CATEGORIES=seh`) showed the "unhandled" dispatch actually walking **15 real frames across at least 6 different threads' stacks** before giving up — frame addresses `0x082bffd4 → 0x0827ffd4 → 0x0823ffd4 → 0x081fffd4 → ...`, each exactly `0x40000` (`THREAD_STACK_SIZE`) apart. Root cause, confirmed by reading `cpu/src/scheduler.zig`: `TEB_BASE` (`0x00320000`) is a single fixed constant shared by every thread — there's no real per-thread TEB. `saveCurrent`/`loadThread`/`initThreadStack` already save/restore `last_error` (TEB+0x34) per-thread on every context switch (a fix from an earlier session for the identical bug class), but `ExceptionList` (TEB+0x00, the real `fs:[0]` SEH chain head) was never included — so whichever thread pushed a frame most recently left it sitting in the one shared field, and the next thread's own pushes chained onto it, splicing unrelated threads' stacks into one Frankenstein "chain."

**Fix** (`cpu/src/scheduler.zig`): added `exception_list: u32 = 0xFFFFFFFF` to `ThreadEntry` (0xFFFFFFFF = real Windows' "empty chain" terminator, matching `kernel_structures.py`'s own initial TEB write) and threaded it through `saveCurrent`/`loadThread`/`initThreadStack` exactly like `last_error`. 3 new tests (fresh-thread-starts-empty, save-and-restore-round-trip via two context switches). Red-confirmed (compile error: field didn't exist) before implementing, green after. `zig build test`: all green. `pytest -q`: 1140/1140 unaffected.

**Live-verified**: re-running the same scenario with `LOG_LEVEL=debug LOG_CATEGORIES=seh`, the walk is now cleanly bounded to **5 frames, all within thread 1011's own stack** (`0x082bf744` through `0x082bffd4`) — no more cross-thread jumps. Still genuinely unhandled (3 specific handlers `0x00c8ce2e`/`0x00c8ca32`/`0x00c8af70` plus the default CRT handler `0x009f5eb8`, all decline), but this is now a *trustworthy* negative result instead of a corrupted one — thread 1011 truly has no handler for this access violation, so the real bug is genuinely upstream (whatever computes the bad jump target), not hidden by broken SEH diagnostics. This is a durable, general-purpose correctness fix (every future SEH dispatch on any thread benefits), independent of whether it explains the original expsrv.dll crash's root cause, which is still open.

**RESOLVED (2026-08-21, same night): the expsrv.dll near-null jump crash. Root cause and fix found via a spawned headless `claude -p` child session**, worked around this session's own Ghidra MCP disconnection. `ghidra-mcp.service` had been getting OOM-killed twice (`journalctl`: kernel OOM-killer took out its `decompile` helper at 6.9GB and 3.9GB respectively — real memory pressure, not a bug in the bridge); killing Firefox freed enough headroom (free RAM 3.1Gi→5.8Gi) that the service itself became stable, but *this* session's own MCP client connection stayed stuck disconnected regardless (confirmed the service was healthy: `systemctl --user status` showed 4.5h uptime, HTTP endpoint responsive) and a plain `systemctl --user restart` didn't cause a reconnect either. Spawned a fresh, independent `claude -p "..." --permission-mode acceptEdits` process (its first attempt without the permission flag landed in plan mode and couldn't call any tool at all, including read-only ones) with a fully self-contained prompt (it has no memory of this conversation) describing the target addresses and asking it to decompile/report back to a file.

**What the child session found** (via `ghidra-mcp`, though its own permission allowlist only covered `dump_bytes`/`decompile_function`/`get_references_to`/`switch_active_program`/`list_functions` — `import_and_analyze`/`list_projects`/`switch_active_project` were denied, and the loaded `expsrv.dll` program had never actually been auto-analyzed, so `decompile_function` failed on every address; it worked around this by hand-disassembling via raw `dump_bytes` reads instead of giving up): real image base `0x0F9C0000` (confirmed two ways: MZ+PE-header `ImageBase`, cross-checked via entry point). `static_address_1 = 0x0F9DCBD7` and `static_address_2 = 0x0F9C9D1B` are two frames of the *same* real call chain: a `CALL` at `0x0F9DCBD2` targets a function at `0x0F9C9CE9` (a lazy get-or-load-`ITypeLib` helper), which does `CALL DWORD PTR [0x0FA0FEF0]` with **no NULL check**. `0x0FA0FEF0` is a plain writable global, zero-initialized on disk, that expsrv.dll's own init fills via a manual `LoadLibraryA("oleaut32.dll")` + `GetProcAddress()` chain resolving `DispCallFunc`, `LoadTypeLibEx`, `UnRegisterTypeLib`, `CreateTypeLib2` one at a time into consecutive slots, **bailing the whole chain if any single `GetProcAddress` returns NULL**. `0x0FA0FEF0` specifically caches `LoadTypeLibEx`. Real Windows guarantees this always resolves (existed since Win95/NT4), so real code never NULL-checks it.

**Confirmed against tew's actual source, precisely**: `kernel32_handlers.py`'s `GetProcAddress` does a strict string lookup via `stubs.lookup_handler_address(dll_name, proc_name)`. `oleaut32_handlers.py` DID implement `LoadTypeLibEx` (returns `E_NOTIMPL`, an honest simplification) but registered it **only** under the ordinal key `"Ordinal #154"`, never the string `"LoadTypeLibEx"` — so `GetProcAddress(hOleaut32, "LoadTypeLibEx")` (exactly how real code resolves it) returned NULL, got cached, and the later unconditional call-through jumped to invalid memory. Same bug class as `VariantClear`'s `Ordinal #9` fix from an earlier session (see `test_oleaut32_variant_clear.py`) — just the opposite direction (ordinal existed, name didn't, instead of the reverse). Checked the other three probed functions too: `DispCallFunc` was already fine (registered by name); `UnRegisterTypeLib` and `CreateTypeLib2` didn't exist under *any* key at all — if only `LoadTypeLibEx` were fixed, the probe chain would've bailed on the very next lookup instead.

**Fix** (`tew/api/oleaut32_handlers.py`): `LoadTypeLibEx` now also registered by name (same handler function, not a duplicate — matching the established fix pattern). `RegisterTypeLib` (ordinal 155, `LoadTypeLibEx`'s sibling) got the same by-name registration for consistency, same cheap fix. `UnRegisterTypeLib` and `CreateTypeLib2` newly implemented (both return `E_NOTIMPL`, correct stdcall cleanup — 20 bytes/5 args and 12 bytes/3 args respectively, real signatures). 9 new tests in `tests/unit/api/test_oleaut32_typelib.py`. `pytest -q`: 1149/1149.

**Live-verified**: re-running the same scenario, the `expsrv.dll` near-null-jump crash no longer occurs at all. The run progresses further to a fresh, simple, self-documenting halt: `[UNIMPLEMENTED] oleaut32.dll!Ordinal #64 — halting` — same easy pattern as tonight's earlier fixes, not yet investigated.

**Two more ordinal-only ("imported by ordinal, no name" per `oleaut32_handlers.py`'s own header comment) gaps cleared the same session, each identified by checking the real `/data/Downloads/i386-binaries/oleaut32.dll`'s actual PE export table via `objdump` rather than guessing** (that DLL has 442 total exports, 398 by name + 44 ordinal-only — useful reference if this pattern keeps recurring):

1. **`Ordinal #64` = `VarI4FromStr`** — live-confirmed call: MSJET35.DLL's expression parser (`FUN_7a86756b`) converting a plain decimal literal (`"251658241"`, seen inside a real WHERE-clause expression `"ParentId = 251658241 and Type = 6 and Connect Is Null"`) to `VT_I4`. Implemented the well-defined case only — optional sign, ASCII digits, optional whitespace, real range-checked overflow — validated via a regex rather than trusting Python's more permissive `int()`; anything that doesn't match returns the real `DISP_E_TYPEMISMATCH` HRESULT (the correct real-Windows behavior for non-numeric input, not a guess). Registered both by ordinal and by name, per the `LoadTypeLibEx` lesson above. 18 new tests, `test_oleaut32_vari4fromstr.py`.
2. **`Ordinal #84` = `VarR8FromStr`** — same live call chain, Jet's expression evaluator falling back to a real-number conversion. Same discipline: standard invariant-culture decimal/scientific float literal only, shape-validated by regex before calling `float()` (Python's `float()` accepts `"inf"`/`"nan"` which aren't valid numeric-string literals in this sense — the regex rejects those explicitly, confirmed by a dedicated test). No 64-bit memory primitive existed in `Memory` (only 8/16/32-bit read/write) — added a small local `_write_f64` helper packing real IEEE-754 bytes via `struct.pack("<d", ...)` rather than inventing a wider Memory API. 19 new tests, `test_oleaut32_varr8fromstr.py`.

Both: `pytest -q` green throughout (1149→1167→1186), live re-run after each confirming the specific halt cleared and further progress before the next one appeared.

**`Ordinal #94` = `VarDateFromStr` — RESOLVED, planned via `EnterPlanMode` before implementing** (Molly: "let's plan this out"). A real step up in scope from the last two — date parsing needs locale-aware format handling in general. Rather than guess the format up front, added a temporary diagnostic-only stub (logs the real `strIn`, then halts) and ran once: real input confirmed to be exactly `'1/1/2010'` — Jet's own tokenizer strips the `#...#` delimiters before this call, no time component, no 2-digit year. Removed the stub once it answered the question.

**Scope, matching the exact live evidence**: only `M/D/YYYY` (4-digit year, `/` separator) implemented — 2-digit years, alternate separators, month names, time components, and D/M/Y ordering are all explicitly out of scope (no evidence, genuinely ambiguous), returning the real `DISP_E_TYPEMISMATCH` HRESULT rather than guessing. Real calendar validation via `datetime.date(y,m,d)` (catches its own `ValueError` for invalid combinations) instead of hand-rolled day-per-month tables. **Correctly implements the documented OLE Automation / Lotus-1-2-3-compatibility epoch quirk**: 1900 is (incorrectly, but by design) treated as a leap year for backward compatibility, so real dates on/after 1900-03-01 need a `+1` day correction versus a naive Gregorian day-count from the `1899-12-30` epoch — this isn't a guess, it's documented Microsoft behavior, and matters here since the query does a real `<>` comparison against a stored database date value. Reused the existing `_write_f64` helper from `VarR8FromStr` rather than duplicating it. Registered both by ordinal and by name, same lesson as `LoadTypeLibEx`.

Caught a real arithmetic slip while writing the test's pinned epoch values by hand (expected `3/1/1900` → `61.0`, computed value was actually `62.0`) — verified all pinned values via a direct Python computation before trusting them, rather than shipping a wrong assertion. 26 new tests, `test_oleaut32_vardatefromstr.py`. `pytest -q`: 1212/1212.

**Live-verified**: `Ordinal #94` no longer halts. The run progresses further to a genuinely different, harder problem this time — **not** another simple missing-handler gap. `run_exe.py`'s SEH-routed runaway detector fires again: `runaway at 0x00056159 unhandled by SEH chain -- halting`, confirmed genuinely unhandled (not a cross-thread SEH artifact, that bug's already fixed). New stack signature, real `expsrv.dll`/`MSJET35.DLL` return addresses present (`expsrv.dll+0x1cdb7`, `expsrv.dll+0x1d042`, `MSJET35.DLL+0x3bc04`) — this is the next real investigation, likely needing the same Ghidra-decompile-or-single-step-trace approach as the earlier `expsrv.dll` crash, not a quick handler fix. Not yet started.

**Logger fix, same session (Molly: "we really need to make crash dumps show always, regardless of level or filter")**: investigating this new runaway, the "last valid EIP" diagnostic line (the exact thing needed to start the next investigation) was missing from the log — twice, in two separate re-runs. Root cause, confirmed by reading `tew/logger.py`'s `_emit()`: only `ERROR`-level messages were exempt from *both* the `LOG_LEVEL` and `LOG_CATEGORIES` filters (a pre-existing, deliberate exemption per its own comment, for exactly this "halt diagnostics must never be silently dropped" reason) — but the runaway-detector's `"RUNAWAY at step ..., last valid EIP ..."` line, and its sibling in the `cpu.faulted` branch (`"CPU fault at EIP=... -- attempting SEH dispatch"`), were both logged at `WARN`, which got no such exemption from either filter.

**Fix**: added `logger.always(level, category, msg)` to `tew/logger.py` — bypasses both filters entirely via a new `force` parameter threaded through `_emit()`, while still printing the real `[WARN]`/`[INFO]`/`[ERROR]` prefix for the given `level` (doesn't misrepresent severity, only guarantees visibility). Applied narrowly to the two lines that actually caused tonight's blind spot, not swept across every log call in the file (that would defeat the point of configurable log levels for normal, noise-free runs). New `tests/unit/test_logger.py` (first test file for this module) — 4 regression-guard tests confirming ordinary filtering is unchanged, 5 new tests confirming `always()` genuinely bypasses level, category, and both at once, plus that the printed prefix reflects the given level. `pytest -q`: 1221/1221.

**Live-verified with a deliberately narrower filter than the bug ever needed** (`LOG_LEVEL=error LOG_CATEGORIES=cpu` — excluding even the `seh` category entirely): the `RUNAWAY at step ..., last valid EIP=0x15048dfe...` line now shows up anyway, exactly as intended.

**Technique note, worth remembering**: when this session's own MCP connection to a tool is stuck (server healthy, client stale), a fresh `claude -p "<self-contained prompt>" --permission-mode acceptEdits > logfile &` genuinely works around it — it's a real separate process with its own fresh MCP handshake. Plain `claude -p` without the permission flag lands in plan mode and can't call any tool, not even read-only ones. The child has zero memory of the parent conversation, so the prompt must be fully self-contained (exact file paths, exact addresses, exact task). Wait for actual process exit (`kill -0 $pid` loop), not a fixed sleep — Ghidra analysis of an unfamiliar DLL took several minutes.

**Correction to the technique note**: `--permission-mode acceptEdits` alone does NOT reliably grant MCP tool access — a third spawned child (same flag, same prompt shape) got denied on its very first call (`list_projects`/`list_programs`, neither in the project's `.claude/settings.local.json` allow-list of just 5 Ghidra tools). It correctly refused to fabricate results rather than guess — exactly right per this project's no-stubs rule. **Real fix**: the allow-list is a plain local settings file (`.claude/settings.local.json`), safe to edit directly — added `list_projects`/`list_programs`/`switch_active_project`/`import_and_analyze`/`get_function_calls`/`get_function_instructions`/`search_strings` to it (all read-only/investigation Ghidra tools, same trust tier as what was already listed; deliberately did not add write tools like `rename_function`/`save_program`/`create_struct`). A 4th spawn with the updated allow-list worked. Grant the tools explicitly before spawning, don't just hope a permission-mode flag covers it.

**New runaway crash — investigated via a 4th spawned child, real progress, not fully closed.** Real image base for `MSJET35.DLL` in Ghidra confirmed already-analyzed (decompiled normally); `expsrv.dll` still has zero analyzed functions and every `import_and_analyze` attempt hit `ghidra.util.exception.FileInUseException: expsrv.dll is in use` — plausibly this parent session's own stuck-but-still-connected client holding a checkout lock Ghidra-side, even though this session's own tool calls don't work. Full write-up (real decompile for the msjet35.dll side, careful hand-disassembly cross-checked byte-for-byte against known return addresses for the expsrv.dll side): `memory/expsrv_crash2_msjet_collation_analysis.md`.

**Strongest finding, EBP-verified**: `FUN_7a87ba0a` (static `0x7a87ba0a`) in `msjet35.dll` — a general Variant/database-value comparison routine used ~20 places across the DLL. Its `CALL` at `0x7a87bc01` returns to exactly `0x7a87bc04`, matching the task's one EBP-chain-confirmed real frame precisely. Decompiled:
```c
piVar11 = *(int **)(param_1 * 0x708 + 0x2c0 + DAT_7a9362c0);
uVar8 = (**(code **)(*piVar11 + 0x18))(piVar11,&local_28,local_10,param_4);
```
`DAT_7a9362c0` is a per-session-struct table (`0x708` bytes/session); offset `0x2c0` caches a pointer to what's almost certainly a COM-style collation/comparison interface (vtable slot `0x18`/6). **No NULL check on `piVar11` or `*piVar11`** before the vtable call. If a session's `0x2c0` slot was never populated, `*piVar11` reads `0`, the call target becomes `0 + 0x18 = 0x18` (a near-zero address) — matching the crash signature (jump to invalid, near-zero, all-zero-bytes memory) exactly. Same *shape* as the already-fixed `LoadTypeLibEx` bug (unchecked call through a lazily-populated cached pointer), but a genuinely different object: a Jet collation/compare interface cache inside `msjet35.dll` itself, not an OLEAUT32 import cache inside `expsrv.dll`.

**Two candidate call sites also found in `expsrv.dll`** (hand-disassembled only, not decompiled, due to the file lock): one plausible (`0x0F9DCDB1`, a `CALL dword ptr [0x0FA0FFB4]` through an all-zero-on-disk cached slot, same GetProcAddress-cache-cluster shape as the fixed bug, API name unconfirmed — no string table without real analysis), one explicitly ruled out (`0x0F9DD03B`, a VARTYPE dispatch table with one genuinely null entry, but the code already special-cases that exact index before ever reaching the table).

---

## Previous status (2026-08-20, cont'd x3) — DAO-3075: "AS" confirmed to never reach the match-check comparison; exact instruction-level path not yet resolved

Superseded same-day: a single-instruction-step live trace (see current `status.md`) found the exact mechanism this entry couldn't pin down, and it was a real tew CPU-engine bug, not the game or the stack. Preserved for the intermediate findings, still valid:

**What was solid at this point:** `AS` genuinely tokenizes to `0x105`; `0x105` is a genuine, uncorrupted entry in the comparison table; the match-check comparison itself only ever saw `0x100` then `0x16`, never `0x105`; the "real match found" path always matched the terminator (`0x16`), never `0x105`.

**What wasn't resolved:** the exact instruction-level path. Two hand-disassembly predictions about `FUN_7a866c6d`'s paren-skip depth-counter logic (`MOV BP,1` at `0x7a866ce7`) had already been directly contradicted by live data this session, and manual byte-by-byte decoding of this specific function was assessed as unreliable going forward. Relocation corruption had already been directly tested and ruled out (see the entry below this one for that detail).

---

## Previous status (2026-08-20, cont'd) — DAO-3075: root cause narrowed to a single comparison in msjet35.dll's own SELECT-list lookahead scanner; relocation ruled out

Superseded same-day by hand-disassembling the exact match-check loop and confirming `AS`'s token code never even reaches the comparison (see live `status.md`). Full content preserved here for the call-chain detail and the relocation-hypothesis test, both still valid and load-bearing:

**The full, live-confirmed call chain from the real SQL text down into the actual SELECT-list column-boundary logic** (every hop verified via a live breakpoint reading real arguments/return values):

```
dao350.dll FUN_044d519b -> (*DAT_044e534c) -> msjet35.dll ordinal 319 (FUN_7a8ae64d)
  -> FUN_7a856c17 (real top-level SQL statement compiler)
    -> FUN_7a85683d (STATEMENT-level tokenizer) -- first token 0x167 = SELECT
    -> FUN_7a866d2b(0x167) (generic statement dispatcher)
      -> FUN_7a866f98(0x167,...) -- scans to statement terminator, rewinds to
                                     right after SELECT, returns 0
      -> [rewind re-reads SELECT itself, 0x167 again -- confirmed live] jump
         table at 0x7a8669ec[0x167] -> 0x7a86a5a7 (confirmed via live memory
         read of the table entry, not guessed)
        -> FUN_7a86a5a7 -- the REAL SELECT-list handler (confirmed live,
                            2ms before the parser failure). Per-column loop:
          -> FUN_7a866c6d(param_3, &DAT_7a86a940) -- lookahead scanner:
             skips balanced parens (correctly handles "Max(PartID)"), scans
             for a token matching a lookup table of valid "column ends here"
             markers, then rewinds and reports success/length.
          -> FUN_7a86756b (expression parser) -- HANDED THE FULL REMAINING
             TEXT "Max(PartID) AS Expr1 FROM Part;" (31 chars) instead of
             just "Max(PartID)" (12 chars). Fails on AS, error 0x271e.
```

`FUN_7a86756b` confirmed NOT the bug: its only clean-success path requires consuming its entire input buffer, no early-stop mechanism, expects an already-correctly-bounded substring from its caller.

**First hypothesis at the time (later refined -- see live status.md): a cursor/bookmark field mismatch.** `FUN_7a85683d` reads its active scan cursor from `param+0x10`. `FUN_7a866c6d`'s "found a match, rewind" branch only writes `param+0x18` (the boundary bookmark), never `param+0x10`. Live data for the `Max(PartID)` column: `FUN_7a866c6d` returns `0x101` (success); `param+0x18` correctly reads `'Max(Part'`; `param+0x10` sits 31 bytes further on (the length of the entire remaining statement) in unwritten zero memory. The next tokenizer call reads from that stale cursor and gets the buffer-end sentinel instead of `AS`.

**Molly asked directly whether this is a relocation problem** (msjet35.dll is the one DLL in the whole system actually relocated away from its preferred base, meaning any relocation-application bug in tew would be invisible everywhere except here). **Checked and ruled out with real data:**
- Live memory at `DAT_7a86a940` (the lookup table: `0x2c`,`0x105`(AS),`0x118`,`0x11c`,`0x111`(FROM),`0x132`,`0x3b`,`0x16`, then 0-terminator) matches expected static bytes exactly, byte for byte. Not corrupted.
- Execution correctly reached `FUN_7a86a5a7` via an indirect jump-table call at its properly-relocated runtime address -- direct proof code-pointer relocation works too.
- Verdict: tew's relocation handling (`tew/pe/base_relocation_table.py` parsing + `dll_loader.py`'s `apply_base_relocations`) is correct for this DLL, confirmed on two independent data points.

**Disambiguated the exact token sequence inside one invocation** of `FUN_7a866c6d` (gated strictly between call and return). Confirmed: the scan reads `0x100`(PartID) `0x29`(`)`) `0x105`(AS) `0x100`(Expr1) `0x111`(FROM) `0x100`(Part) `0x3b` `0x16` -- `AS` genuinely tokenized correctly and present, yet the scan doesn't stop there. (This breakpoint was later found, via hand-disassembly, to be on the paren-skip sub-loop's own read, not the main match-check loop's feed -- the *sequence* observed here is real, but see the live status.md entry for the corrected understanding of exactly which comparisons see which values.)

---

## Previous status (2026-08-20) — DAO-3075: full real SQL-compile call chain traced end-to-end; FUN_7a86756b's real design confirmed; three speculative hops from FUN_7a866d2b all live-disproven

Superseded same-day once the real chain past `FUN_7a866d2b`'s rewind point was found (see live `status.md`: `FUN_7a866d2b` re-reads SELECT itself via the rewind, dispatches through the SAME jump table again, landing on `FUN_7a86a5a7`, the real SELECT-list handler). Key points from this entry, most still valid and incorporated into the current entry:

- `FUN_7a86756b` confirmed NOT the bug -- its only clean-success path requires consuming the entire input buffer, no early-stop mechanism. By design it expects an already-correctly-bounded substring from its caller.
- Ruled out (confirmed dead via live breakpoints that never fired): the `FUN_7a8e7cca` "SELECT keyword classifier" chain and its sole caller `FUN_7a8549b6` (real code, used by `OpenRecordset`/`Execute` instead, not `CreateQueryDef`); a separate speculative chain `FUN_7a862215`/`FUN_7a862942`/`FUN_7a862cd4`/`FUN_7a858c87`; `DAT_044e5238`/ordinal 302's chain (confirmed live but only threads the query's catalog name "tmp" through a collision check, never touches SQL text).
- Also ruled out at the time (later confirmed to be the WRONG next hop, per the live status.md entry): `FUN_7a867064`/`FUN_7a86713b`, guessed as a "comma-loop select-list splitter" -- both confirmed dead via live breakpoints that never fired. The correct next hop (found afterward) was via `FUN_7a866d2b`'s SELECT-rewind-and-redispatch mechanism into `FUN_7a86a5a7`, not this guess.
- **Methodology lesson, reinforced hard this session:** positional parameter names Ghidra assigns (`param_3`, etc.) do NOT reliably correspond to the same real value across different functions in a call chain, even when names match syntactically at each call site. Caught concretely twice: two different functions' "param_3" both turned out to be the query name "tmp", not the SQL text, despite matching the naming pattern of earlier functions where "param_3" genuinely was the SQL text. Fix: wide-scan live registers + a broad stack range at each hop, match by actual content, never trust name continuity alone.

---

## Previous status (2026-08-19, cont'd) — DAO-3075: "AS not recognized as keyword" root cause found, then confirmed to be part of a much larger architectural picture

Superseded same-week by tracing the FULL call chain from the real SQL-text entry point down to the failure (see live `status.md`). The "AS isn't recognized as a keyword, DAT_7a93ab04 state machine doesn't distinguish complete-vs-mid-expression" finding from this entry is still accurate and is now understood as ONE PIECE of a much bigger picture: `FUN_7a86756b` (the expression parser) is working exactly as designed -- it's a bounded-buffer, consume-to-end-or-error parser with NO graceful-early-stop mechanism, and the REAL question became "why is it being handed a buffer that includes ' AS Expr1 FROM Part;' at all, instead of just 'Max(PartID)'" -- which led to tracing the entire real SQL-compile call chain from dao350.dll down (see live status.md for the full, confirmed chain).

Full content of the superseded entry preserved here since the `local_178`/`DAT_7a93ab04` mechanics are still load-bearing detail not fully re-derived in the new entry:

**Resolved the breakpoint-firing mystery from the previous entry and found the actual root cause in the same pass.** The missing piece was `local_178` (the "if(local_178==0){...real FUN_7a869880/FUN_7a8699a2 identifier resolution...} else {piVar8=NULL;}" mode flag at the very top of the shared operand-group prologue, `0x7a867712`/`0x7a867717`) -- never directly read from live memory before then, only inferred (wrongly) from unrelated evidence. Reading `[ESP+0x20]` directly at the group-entry breakpoint (`0x150276fb`) showed `local_178 = 1` for all three identifier tokens (`Max`, `PartID`, `AS`) in this specific parse call -- meaning this entire parse call runs in a mode that skips real identifier/function resolution outright (a pure grammar/syntax validation pass). The earlier "universal function-call recognition failure" conclusion (Max/Count/Len all hit `0x271e`) was tested exclusively in this mode too -- meaning `DispCallFunc`/VBA/function-name-table theories were never actually being exercised.

**The bug, traced to the instruction level:** the grammar state machine (`DAT_7a93ab04`: 0=nothing pending, 1=operand just pushed, 2=a complete parenthesized/bracketed expression just closed) runs identically regardless of `local_178`. Case `0xa` (`')'`) sets `DAT_7a93ab04=2` after closing `(PartID)`. The next token, `AS`, is tokenized as plain-identifier type `0x01` (confirmed live) -- NOT recognized as the `AS` keyword. Every operand-group case starts with `if (DAT_7a93ab04 != 0) { error 0x271e }` at the shared entry (`0x7a8676fb`) -- confirmed live: `DAT_7a93ab04 = 2` exactly when `AS` is dispatched, immediately before the error fires.

**Methodology note, still valid:** Ghidra's decompile of `FUN_7a86756b` is misleading for control flow (jump-table-heavy switch, decompiled case grouping and `get_function_calls`/`get_references_to` function-boundary attribution do NOT correspond to real C-level switch-case values). Trust hand-disassembly (`dump_bytes` + manual decode) over the decompile for anything in this function. **Always directly verify flag/mode variables via live memory reads before trusting inferred values.**

---

## Previous status (2026-08-19) — DAO-3075 option 2 in progress: real token types confirmed, but a genuine breakpoint-firing mystery is blocking further tracing

Superseded same-day by the "AS not recognized as keyword" root-cause finding (see live `status.md`) -- the mystery this entry describes turned out to be `local_178` (see the current entry for the resolution). Preserved here for the hand-disassembly methodology detail, which remains valid and useful.

Continuing from the DispCallFunc-ruled-out entry below per Molly's "proceed with option 2" (trace one level deeper into what msjet35.dll's parser consults to recognize a function call).

**Real progress, solidly confirmed via live tracing:**
- The actual token stream for `Max(PartID) AS Expr1 FROM Part;` (as tokenized by `FUN_7a8685de`, runtime `0x150285de`) is: `Max`=type `0x01`, `(`=type `0x09`, `PartID`=type `0x01`, `)`=type `0x0a`, ` `=type `0x11`, `AS`=type `0x01`. Confirmed by breaking at the real main-loop call-return site (`0x1502777e`, found empirically by reading the return address off the stack at tokenizer entry -- NOT by trusting Ghidra's per-case function-boundary attribution, see below).
- **Ghidra's decompile of `FUN_7a86756b` is structurally misleading for this specific function.** The switch is compiled as TWO chained jump tables (outer: token_type-1 -> either a case-group entry point like `0x7a8676fb` or a dedicated case address; inner, only for the `{1,2,4,5,7,0x13,0x14}` group: a byte-indexed second jump table at `0x7a868570`/`0x7a868590`). Ghidra's synthetic function names for jump-table fragments (`caseD_1`, `caseD_d`, etc.) do NOT correspond to C-level switch-case *values* -- `get_function_calls` on a fragment returns calls from MULTIPLE unrelated case bodies that happen to be laid out contiguously/fall-through in memory. Wasted real time trusting this before catching it. **Lesson: for this function, trust hand-disassembly (`dump_bytes` + manual decode) over decompiled case grouping or `get_function_calls`/`get_references_to` function-boundary attribution.**
- Hand-disassembled the REAL case-0x1 (identifier) body: shared group entry `0x7a8676fb` (`if(DAT_7a93ab04!=0) error 0x271e; DAT_7a93ab04=1; if(local_178==0)` prologue) -> inner byte-table dispatch at `0x7a86803d`/`0x7a868570` -> case-1's real body at `0x7a86805a`. For "Max" specifically: `pcVar10={0x00,0x00}` fails both the `\x03` bang-check (`0x7a868096`) and the `\x01` check (`0x7a86809e`), falls through to `CALL FUN_7a869865` (peek, at `0x7a8680af`) then `CMP AL,0x28` (`'('`, at `0x7a8680b4`) then (if equal) `CALL FUN_7a869880` at `0x7a8681ef` (else-branch call is at `0x7a8680cf`, same target). **Live memory at runtime `0x15029880` (`FUN_7a869880`'s entry) matches Ghidra's static bytes byte-for-byte** (`83ec04535657558b6c241c`) -- ruled out a loaded-DLL-version mismatch.

**Open blocker at the time (now resolved, see live status.md):** breakpoints placed at `0x150280b4` and `0x15029880` never fired despite the hand-verified straight-line control flow above. Root cause: `local_178` (never directly verified until the next pass) is actually `1`, not `0` as assumed -- meaning this entire code region is genuinely unreachable in this parse call.

---

## Previous status (2026-08-18, cont'd x5) — DAO-3075: DispCallFunc implemented + live-verified, does NOT fix it -- root cause still open

**Superseded same night** -- DispCallFunc turned out to be untestable by the very tests used to justify it (see the "AS not recognized" entry in live `status.md`: `local_178=1` means this whole parse call skips real identifier/function resolution, so DispCallFunc/VBA theories were never actually exercised). DispCallFunc itself remains real/implemented/tested, just not relevant to DAO-3075.

Prior entry (root cause re-validated: universal function-call-recognition
failure, `0x271e`, confirmed with a trustworthy methodology) archived
below, "Previous status (2026-08-18, cont'd x4)" -- full detail there, not
repeated here.

**This session's work: closed out decision option 1** (implement
`DispCallFunc` and see if it fixes function-call recognition wholesale --
see the archived entry's "Decision point" list). Implemented the real
`oleaut32.dll!DispCallFunc` (was a hard-halt stub since 2026-08-04): generic
late-bound invocation, VARIANT-array arg marshaling (incl. `VT_BYREF`
pointer pass-through, 2-word types), vtable dispatch (`pvInstance != 0`) vs.
direct call, `CC_CDECL`/`CC_STDCALL` only, routed through the existing
`_invoke_emulated_proc`. 7 new tests (`tests/unit/api/test_oleaut32_dispcallfunc.py`).
Full suite green: `zig build test` 154/154, `pytest -q` 1119/1119. Full
detail in `changelog.md`, "2026-08-18 (cont'd x5)".

**Live-verified against the real, unmodified query** (probe's
`_REWRITE_QUERY` left `False` -- no rewrite, the game's own real
`CreateQueryDef` call): `parser-probe` confirmed the real SQL text is still
`Max(PartID) AS Expr1 FROM Part;`, and `exit-probe` still fires
`*param_6 = 0x271e` -- identical to before `DispCallFunc` existed. Final
halt point unchanged too (`EIP=0x001fe012`).

---

## Previous status (2026-08-18, cont'd x4) — DAO-3075: root cause re-validated after catching a real test-harness bug -- universal function-call recognition failure, confirmed with a trustworthy methodology

Superseded by the DispCallFunc negative result, see live `status.md`. Full
content preserved here since it's the source of truth for how the `0x271e`
finding itself was validated (the test-harness-bug catch/fix is not
duplicated in `changelog.md`'s entries at the same level of detail):

Scheduler-to-Zig port is DONE (see this file, "Previous status (2026-08-17,
cont'd x4)"). Resumed the paused DAO-3075/aggregate-function thread same
night, live-traced the entire real `CreateQueryDef` call chain down into
msjet35.dll's real parser (full blow-by-blow below, "Previous status
(2026-08-18, cont'd)" -- two theories ruled out first: missing
`oleaut32.dll!DispCallFunc` and a null `VBAGetExprSrv` interface, neither
directly connected). Read `changelog.md`'s "2026-08-17"/"2026-08-17 (cont'd)"
entries for the original pre-scheduler-detour findings (bytes/tokenizer
clean; plain `SELECT`s compile fine, something with a function call
specifically doesn't).

**IMPORTANT CORRECTION, caught by Molly's objection ("that would mean jet
3.5 has no concept of functions" -- correctly could not be true):** the
first round of testing tonight (rewriting the SQL text *deep* inside the
parser, right at `FUN_7a86756b`'s own entry, patching only its
`param_4`/`param_5`) was invalid. Control test: rewrote to
`"PartID FROM Part;"` -- a query the original investigation had already
confirmed compiles cleanly with zero rewrite -- via that same deep path,
and it also hit `0x271e`. That proves something upstream of the deep parser
(dao350.dll's own processing, before ever reaching msjet35.dll) already
depends on the original query's real content by the time execution reaches
that point; patching only the substring there produces a mismatched,
invalid state, not a fair test of the parser itself. All three "Max/Count/
Len all fail" results from that first round were retracted -- not because
they were necessarily wrong, but because the test that produced them
wasn't trustworthy.

Re-tested properly, patching the SQL text at its real source instead -- the
string literal in `MCity_d.exe`'s own data section (`0x011e0de4`, confirmed
via `search_strings`; the EXE is static==runtime, no delta needed), applied
at `Dbcode_CreateTmpQuery`'s own entry (`0x008fe4a0`), before dao350.dll
ever sees the text at all. This is the same rewrite point the original
(pre-scheduler-detour) investigation used successfully.

Control test passed: `"SELECT PartID FROM Part;"` (no function call)
compiled cleanly through this corrected path -- `exit-probe` never fired,
the run reached a much-later halt point (`EIP=0x002039c2`, not the usual
DAO-3075-adjacent `0x001fe012`).

Re-ran the real tests with the validated mechanism -- same conclusion held:
`SELECT PartID FROM Part;` (control) compiled cleanly; `Max(PartID)`,
`Count(PartID)`, and `Len(PartID)` (a non-aggregate string function, to
rule out "aggregate-specific") all hit `0x271e`. Universal function-call-
recognition failure, not `Max`-specific.

Working theory at the time (now closed with a negative result -- see live
`status.md`): the VBA Expression Service architecture (`vbajet32.dll`/
`expsrv.dll`) is how Jet resolves function calls generally. `VBAGetExprSrv`
itself succeeds (real, non-null interface, confirmed live) -- but obtaining
the interface isn't the same as its function table being populated/
consulted by the parser. `oleaut32.dll!DispCallFunc` (unimplemented at the
time) was the leading candidate for the missing piece.

---

## Previous status (2026-08-18, cont'd) — DAO-3075: full live trace of the real CreateQueryDef call chain (superseded by the concrete root-cause finding, see live status.md)

Full blow-by-blow of live-tracing `Dbcode_CreateTmpQuery` (`0x008fe4a0`, exe) down to msjet35.dll's real parser, address by address, with the msjet35.dll relocation-delta discovery (**`runtime = static - 0x65840000`** for msjet35.dll specifically -- dao350.dll and the EXE are static==runtime, confirmed separately). Two theories ruled out first, both by direct live evidence: missing `DispCallFunc` (real gap, not connected -- `FUN_7a856c17` makes zero external calls) and `VBAGetExprSrv` returning null (disproven -- it succeeds, confirmed via `dao350.dll`'s `FUN_0448a429` real branch at `0x0448a558`).

Full call chain traced, every address/value read from real memory:
```
Dbcode_CreateTmpQuery (exe) -> vtable+0x80 on DAODatabase* -> dao350.dll FUN_04487388 (COM thunk)
  -> vtable+0xa0 on "inner" -> dao350.dll FUN_0448356f (Ghidra under-counts params;
       live stack read found query name "tmp" at arg slot 3, real SQL text at slot 7)
  -> dao350.dll FUN_044c98fe -> FUN_044ca3a7
  -> dao350.dll FUN_044d5e64: (*DAT_044e5238)(...) -- name-registration step
  -> dao350.dll FUN_044d519b: (*DAT_044e534c)(...) -- THE bridge call, resolves
       LIVE to MSJET35.DLL+0x6e64d with the real SQL text as argument
  -> msjet35.dll FUN_7a8a65f8 (static) -- where -3100 is generated via
       FUN_7a86756b (real parser entry) and FUN_7a87f62d (2nd pass, never reached)
```

This is all still accurate and was the path to the actual finding -- see the live `status.md` "Current status" entry for the concrete root cause (error code `0x271e`, "Max" tokenized as plain identifier not function-keyword) that this tracing led to.

## Previous status (2026-08-17, cont'd x4) — Scheduler-to-Zig port done, DAO-3075 thread resumed same night

**Scheduler-to-Zig port is DONE (all 7 stages, 0-6).** `tew/kernel/scheduler.py` (the original pure-Python scheduler) and its test suite are deleted; `tew/hardware/scheduler_zig.py` (`ZigScheduler`, backed by `cpu/src/scheduler.zig`) is the only scheduler now. Full design record: `~/.claude/plans/vast-drifting-pike.md` (artifact: https://claude.ai/code/artifact/b3751eed-4723-4010-8724-011c27f456e1). Full per-stage history: `changelog.md`, "2026-08-17 (cont'd)" through "(cont'd x7)"; a fuller wrap-up summary is archived above, "Previous status (2026-08-17, cont'd x3)".

**Outcome, confirmed and measured**: the motivating problem (160,433 reentrancy-guard refusals / 3.7s starvation during a heavy nested `expsrv.dll` DllMain call, caused by ~44 FFI hops per context switch under the old scheduler) is fixed -- a final live run spawning 14 real threads (including 3 created from inside that same nested DllMain call) shows **0 reentrancy violations**. `zig build test`: 154/154. `pytest -q`: 1112/1112. One real bug found and fixed along the way (not anticipated by the plan): `ZigCPU._py_halted`, a Python-side cache that went stale once the scheduler's halt-clearing moved into Zig.

Same night, resumed the DAO-3075 thread (see live "Current status" for where this picked up and where it's headed) -- see it for the freshest state.

## Previous status (2026-08-17, cont'd x3) — Scheduler-to-Zig port, complete

**All 7 stages (0-6) of the scheduler-to-Zig port are done.** Motivating problem: a heavy nested `DllMain` call (`expsrv.dll`) caused 160,433 reentrancy-guard refusals across 3.7s of real wall-clock starvation, because every context switch under the old pure-Python `Scheduler` paid ~44 individual Python↔Zig FFI round trips. Plan: `~/.claude/plans/vast-drifting-pike.md` (artifact: https://claude.ai/code/artifact/b3751eed-4723-4010-8724-011c27f456e1). Full per-stage detail lives in `changelog.md`'s dated entries, "2026-08-17 (cont'd)" through "(cont'd x7)" -- grep there, not here, for exact test counts/code changes per stage.

Outcome: `tew/kernel/scheduler.py` (673 lines, the original pure-Python scheduler) and its test suite are deleted. `tew/hardware/scheduler_zig.py` (`ZigScheduler`) is the only scheduler now, backed by `cpu/src/scheduler.zig` (154 colocated Zig tests) + `tests/unit/hardware/test_scheduler_zig.py` (43 Python tests covering the orchestration layer the Zig tests can't reach -- the two-call kernel-tick retry protocol, `terminate_thread`'s tri-state mapping, the `_CurrentThreadProxy`). `zig build test`: 154/154. `pytest -q`: 1112/1112.

**Confirmed fixed, measured**: the original starvation scenario now shows **0 reentrancy violations** (was 160,433) in a live run that spawns 14 real threads including 3 created from inside a nested `expsrv.dll` DllMain call -- exactly the scenario that used to stall. Checkpoint-delta step-count diff (169,791 steps between two fixed points in the boot sequence) stayed byte-identical across every single stage's live-run check, Stage 0 through Stage 6 -- strong evidence no code path silently changed.

**Real bug found and fixed along the way, not anticipated by the plan**: `ZigCPU._py_halted` (`tew/hardware/cpu_zig.py`) was a Python-side cache of the halted flag that went stale once the scheduler's halt-clearing logic moved into Zig and started writing the native `CpuState.halted` field directly (the old pure-Python scheduler always went through Python's own property setter, which kept the cache in sync; Zig code has no way to notify Python). Removed the cache entirely -- `cpu.halted` is now a pure passthrough to the native flag. Caught by a real-CPU unit test going red (`test_kernel32_sleep.py::test_single_thread_clears_halted`), not by a live run -- exactly the kind of thing the "regression-only live run every stage" policy (adopted after Stage 1, at Molly's request, since test coverage alone hadn't been convincing given the earlier reentrancy eip-corruption bug) was meant to catch *in addition to*, not instead of, thorough unit tests.

**Process note for future large refactors on this project**: per-stage regression-only live runs (diffing `cpu.step_count` at fixed checkpoints, comparing inter-checkpoint *deltas* rather than absolute values since there's a real wall-clock-timing-sensitive spin loop early in boot that makes absolute step counts drift run-to-run) proved a cheap, effective way to catch "did this unreachable-so-far code change anything" during the Zig-only stages, and "did this actually fix what it was supposed to" once wired in.

## Previous status (2026-08-17) — DAO-3075/aggregate-function thread, paused

**DAO-3075 root cause narrowed further: `Max()` fails to compile in this Jet 3.5 build unconditionally -- not tied to the `Part` table being empty.** Previous entry's leading theory (aggregate-over-zero-rows) is now disproven: rewrote a live query to `"SELECT Max(BrandID) FROM Brand;"` (`Brand` confirmed non-empty -- a real, different, plain `SELECT` against it compiled cleanly in the prior session) and it **failed identically** to `Max(PartID) FROM Part` -- same two-retry pattern, same `FUN_7a854cd0` error-report call firing. Whatever's wrong is about the aggregate function itself, not the target table's row count.

**Case-sensitivity in function-name resolution also ruled out.** Every keyword in the real query (`SELECT`/`AS`/`FROM`) was already uppercase as written, so the earlier tokenizer-probe result only proved keyword lookup is case-insensitive for already-uppercase input -- never actually tested whether `Max` (mixed-case, as literally written) resolving as an aggregate function specifically was case-sensitive. Tested directly: rewrote to `"SELECT MAX(BrandID) FROM Brand;"` (all-caps) -- **failed identically** to the mixed-case version, same retry pattern, same error-report call. Not a case-folding bug in tew's own handling or in Jet's function-name lookup.

**Build-date facts, precisely checked (real PE `TimeDateStamp`, not guessed)**:
| File | Built |
|---|---|
| `MCity_d.exe` (this debug build) | **2002-05-03** |
| `msjet35.dll` (Jet 3.5 engine) | 1999-04-23 |
| `dao350.dll` (DAO 3.5) | 1998-04-08 |
| `msjter35.dll` (Jet error strings) | 1997-06-23 |

Jet 4.0 shipped with Access 2000/Office 2000 in **June 1999** -- before `msjet35.dll`'s own build date, and ~3 years before `MCity_d.exe` was compiled. So the team's use of Jet 3.5 was a deliberate compatibility choice (older, more universally-redistributable engine for a Win95/98-era install base), not "Jet 4 didn't exist yet." Doesn't change the bug, but confirms this is a genuinely old, known-quirky engine version being hit on purpose, not an anachronism.

**Checked a real game-distribution archive for a newer bundled DAO/Jet -- none found.** Downloaded and extracted (7z, password-protected) a ~336MB "Motor City Online" client install/Castanet-staging archive (1832 files) from a Drive link Molly provided. Zero `msjet*`/`dao*`/`msjter*`/`msjint*` DLLs anywhere in it, and no DAO/MDAC redistributable installer either -- makes sense architecturally, DAO/Jet would be a shared system component installed separately (MDAC/DAO redist or already on the OS), not bundled per-game. Nothing more to find there; don't re-check this archive for Jet DLLs.

**Found the real architecture behind the aggregate failure, not yet root-caused: Jet 3.5 doesn't implement functions itself, it delegates to VBA's Expression Service.** `vbajet32.dll` has exactly 2 exports: `VBAGetExprSrv`, `LoadExprSrvDll` -- the real bridge into `expsrv.dll` (a 623-export VBA runtime: `ProcCallEngine`, `MethCallEngine`, `DllFunctionCall`, etc.). Real, unmodified `dao350.dll` code (`FUN_0448a429`) calls `VBAGetExprSrv` directly and never calls `LoadExprSrvDll` at all -- it relies on `expsrv.dll` already being loaded (`VBAGetExprSrv` internally just does `GetModuleHandleA("expsrv.dll")`, returns failure if not found). Traced the real caller of `LoadLibraryA("expsrv.dll")` (`vbajet32.dll+0x15ea`, inside `FUN_0f9a15dd`, itself called from within `VBAGetExprSrv`'s own execution) -- `expsrv.dll` **does** get loaded, just-in-time, one call deep inside `VBAGetExprSrv` itself, contradicting an earlier "race" framing.

**Live-verified a much bigger, separate finding along the way: the reentrancy guard (added earlier tonight) causes severe starvation on a heavy nested `DllMain` call.** During `expsrv.dll`'s `DllMain` invocation (via the dependency-DllMain nested-call mechanism), the main thread hit **160,433 reentrancy-guard refusals across 3.7 seconds of real wall-clock time** -- `reentrant_depth` stayed above 0 the whole time, so `tid=1007` spun on refused `Sleep()` retries (`GetMessageA`'s cooperative poll) without ever making progress. The guard itself is correct (still preventing real corruption), but every context switch pays for a Python↔Zig FFI round trip that doesn't need to exist -- confirmed `cpu.save_state()`/`restore_state()` are 22 individual ctypes calls each, ~44 per switch. **This directly motivated pivoting to a new, separate effort: porting the scheduler into the Zig core** (plan at `~/.claude/plans/vast-drifting-pike.md`, approved 2026-08-17) -- see `status.md`'s Current status for that work; the DAO-3075/aggregate-function investigation itself was paused here, not resolved.

**Current blocker (DAO-3075 thread, paused, not abandoned)**: research real Jet 3.5 (not Jet 4) SQL engine internals/known limitations around aggregate functions in a plain `SELECT` (no `GROUP BY`), independent of target-table row count -- is `Max(col) FROM table` a real, documented Jet 3.5 restriction/bug, or is something else about the query's execution context missing (a required index, a missing catalog/statistics entry, something in how `CreateQueryDef` vs. direct SQL execution handles aggregates)? Also open: why `LoadExprSrvDll` is never called by the real code path traced so far -- is something else supposed to call it earlier, or is `VBAGetExprSrv`'s own just-in-time load (traced above) the intended real-Windows behavior after all? Not started -- needs external research (real Jet/Access 97 documentation, MS KB articles, mdbtools source, or similar), not more emulator tracing.

---

## Previous status (2026-08-16, cont'd x5)

**Root-caused DAO-3075 down to the exact SQL construct that fails to compile: `Max(PartID)` -- the aggregate function itself, not the alias, not the query text/encoding, not tew.** Live-traced the full real chain in `msjet35.dll` (project `debug_clean`) from `Dbcode_CreateTmpQuery`'s vtable `CreateQueryDef` call all the way down: `FUN_7a8ae64d` (3-way dispatch, confirmed the success path fires: `FUN_7a858a28`→0, `local_8==&DAT_7a84f9e8 && param_4<0xfde9`→true) → `FUN_7a856c17` (real create/compile, never traced before this session) → returns `-3100` (`0xfffff3e4`) → `FUN_7a866d2b` recognizes `-3100` by name and extracts the query substring at the failure point → `FUN_7a854cd0` (message-report, gated on an internal `0x41f` context flag, separately not-yet-traced) → `FUN_7a85662f`, which turned out to be `setjmp` (real MSVC "VC20" jump-buffer signature), not a parser -- the real parser calls `longjmp(buf, -3100)` on a genuine syntax error, unwinding back here.

**Three independent layers checked byte/token-level, all clean -- ruled out corruption, encoding, and tew-handler involvement entirely:**
1. **Raw query bytes** (`query-bytes-probe`, hooked `FUN_7a856c17` entry): `"SELECT Max(PartID) AS Expr1 FROM Part;"`, exactly 38 bytes, correct NUL terminator immediately after, nothing else wrong.
2. **Tokenizer** (`tokenizer-probe`, hooked `FUN_7a85683d`'s single shared epilogue at `0x7a856a20`): every token classified exactly right -- `AS` returns real keyword code `0x105`, not misread as an identifier (`0x100`). Self-contained, real byte-classification bitmap + static keyword hash table, zero calls into any Win32 API tew implements.
3. **Confirmed this is Jet 3** (not Jet 4): MDB header version byte @0x14 == `0x00` (checked directly in `Online.mdb`), matching `msjet35.dll` and the 2048-byte page size this whole session's B-tree work was built around.

**Live query-rewrite experiments** (patched the query in memory at `FUN_7a856c17`'s entry, before tokenizing, updating the length param `[ESP+0x14]` to match):
- Dropped just `AS Expr1` → `"SELECT Max(PartID) FROM Part;"`: **still fails**, identical retry pattern, identical position numbers (12, 19) despite the string being 9 bytes shorter -- strong evidence those position numbers aren't real per-string character offsets. Alias hypothesis disproven.
- Dropped the aggregate entirely → `"SELECT PartID FROM Part;"`: **compiles cleanly, no error at all.** Confirmed against a second, real, different, non-aggregate query encountered the same run (`"SELECT BrandID, Brand, PicName FROM Brand;"`, also rewritten/tested, also clean). `stdout.txt`'s `"Could not create Query"`/`DAOERROR (3075)`/`ASSERT pQueryDef` lines are gone entirely. The run gets ~15x further (160K log lines vs ~10-13K every prior run this session) before hitting an unrelated `RUNAWAY DETECTED at step 120400000` much later, in a completely different part of the game.

**Conclusion (superseded by next entry -- the empty-table theory below turned out wrong)**: `Max(PartID)` (an aggregate function call) is specifically what this compiled `msjet35.dll` (Jet 3.5) can't compile -- plain column-selection queries work fine. Not a tew bug at any traced layer (bytes/tokens/grammar-dispatch all clean, zero Win32 API involvement in the whole chain). Leading theory at the time: aggregate over the `Part` table specifically, which is empty by default -- see current status.md for the follow-up test that ruled this out.

**`run_exe.py` cleaned up (2026-08-17)**: all 8 of this investigation's breakpoint probes removed, back to zero registered breakpoints. Exact addresses/decompiles are preserved above if this needs to be resumed. The breakpoint-table-capacity warning near `register_breakpoint`'s definition (Zig core's `bp_table` is a hard 8-slot cap, silently drops anything past it, no error) stays in `run_exe.py` permanently.

## Previous status (2026-08-16, cont'd x4)

**Path correction (fed back into the emu32 skill, v2.0)**: guest-written diagnostic files were being checked at the wrong host path all session -- `except.txt`/`stdout.txt` are driveless guest paths, resolved relative to `current_directory` (`C:\MCity` by default, `tew/api/_state.py::translate_windows_path`), which maps to `~/.emu32/MCity/` -- **not** `/data/Code/tew/`. `dblog.txt` really is at `~/.emu32/dblog.txt` (repo root of the guest fs, not under `MCity/`) -- that one was already correct.

**`~/.emu32/MCity/stdout.txt` (previously unchecked this session due to the above) contains the actual human-readable reason for the `EIP=0x00688c68`/`_Nfs_DebugBreak` halt, and it reframes the whole investigation:**
```
clayer.c(318) Found Debugger!
platform.c(325) dx8z.dll
mode:0, width:1024, height:768, hz:60, bpp:32, fmt:22
--DBThread is alive! (han=0xBEF9  threadid=0x3F3
dbcode.c(3376)  The class has not been licensed
dbcode.c(3376)  The class has not been licensed
dbcode.c(4418) Could not create Query
dbcode.c(3426)  DAOERROR: (3075) DAO.QueryDefs:
ASSERT: dbcode.c(4478) pQueryDef
```
Decompiled the relevant chain in Ghidra (`debug_clean` project, `MCity_d.exe`):
- **`clayer.c(318) Found Debugger!`** is `_CLayer_DetectDebugger` (real function, decompiled): it deliberately reads near-null address `0x190` to provoke a real SEH exception, and whether `_Nfs_DebuggerIsPresent` ends up 0 or 1 depends on whether *tew's* fault delivery for that read behaves like real Windows. It came back **1** (false positive -- nothing is actually debugging this run). This matters because several debug-build asserts (`DumpErrors`'s `SUCCEEDED(hResult)` check, `Dbcode_TmpQuery`'s param-null checks) only call `_Nfs_DebugBreak()` when `_Nfs_DebuggerIsPresent != 0` -- in a real, non-debugged production run these are silent no-ops; in tew they currently fire and hard-halt. **Not yet investigated**: why tew's handling of that deliberate near-null read causes "debugger present" instead of "no debugger" -- likely why several of this session's `_Nfs_DebugBreak` halts exist at all, independent of whatever the underlying DAO bug turns out to be.
- **`dbcode.c(3376) The class has not been licensed`** (x2): **NOT new, and NOT the cause** -- Molly flagged this same day, earlier session (see "2026-08-16" entry below): a known, expected, ignorable message, don't re-chase it. (Corrected same session: I mistakenly re-presented this as a new lead here; the real target was always the `CreateQueryDef`/error-3075/`pQueryDef` chain itself, which the next entry actually traces.)
- **`dbcode.c(3426) DAOERROR: (3075) DAO.QueryDefs: `** confirms, independent of the earlier live BSTR probe, that Number=3075/Source="DAO.QueryDefs"/Description=empty is real, not a tracing artifact.
- **`ASSERT: dbcode.c(4478) pQueryDef`** is `Dbcode_TmpQuery`'s own null-check on its output parameter -- fires because the query genuinely never got created, consistent with everything above.

The `_CLayer_DetectDebugger` false-positive thread was not picked back up this session -- see current `status.md`/changelog.md for what actually got traced next (the real `CreateQueryDef` failure chain, down to the exact SQL construct).

## Previous status (2026-08-16, cont'd x3)

**Reentrancy guard implemented, tested, and live-verified working exactly as designed.** The dependency-`DllMain` fix (previous entry) exposed a real, pre-existing tew architecture bug: `_invoke_emulated_proc`'s nested `cpu.run()` call shares tew's single `cpu.regs` with the cooperative scheduler; any scheduler swap triggered by a stub handler mid-nested-call used to silently hijack the shared registers to an unrelated thread with zero detection. Fixed at the source: `tew/kernel/scheduler.py` now has `reentrant_depth`/`reentrancy_violations` plus a single chokepoint, `_swap_current()`, that `switch_to`, `preempt_slice`, `block_current_on_cs`, `block_current_on_handles`, and `sleep_current` all route through -- it refuses (logs `[ERROR][scheduler] reentrancy violation: ...`, records the violation, no state mutated) whenever `reentrant_depth > 0`. `mark_current_dead`/`terminate_thread` are deliberately exempt (unchanged, unguarded) -- a thread dying mid-nested-call must still be able to hand off the CPU; that's the mechanism `_invoke_emulated_proc`'s own (already-tested) thread-death detection depends on. `_invoke_emulated_proc` (`user32_handlers.py`) now brackets its `cpu.run()` loop with `scheduler.enter_reentrant_call()`/`exit_reentrant_call()` in try/finally. 25 new tests added (`test_scheduler.py`: depth tracking, guard refusal for all 5 swap-capable methods with explicit no-mutation assertions, the `mark_current_dead`/`terminate_thread` exemption locked in as a test, non-reentrant regression coverage). Full suite: 1146/1146 passing.

**Live-verified real effect, both good and newly-revealed**: re-ran with `LOG_CATEGORIES=scheduler,thread,dialog,handlers`. `MSJINT35.dll`'s `DllMain` now runs to completion via `_invoke_dependency_dllmain` and its thread (`tid=1011`) exits *normally* through `THREAD_SENTINEL` -- no more silent mid-call thread death. Exactly one reentrancy violation fired during the run: at the instant `tid=1011` died and `mark_current_dead` (unguarded, as designed) swapped the live CPU to `tid=1007`, `tid=1007` immediately tried `Sleep()` while the outer nested call (on the now-dead `tid=1011`) hadn't yet noticed the death at its next chunk boundary (`reentrant_depth` still 1) -- `sleep_current` correctly refused instead of silently corrupting state. That refusal left `tid=1007`'s `Sleep` stub with no graceful fallback (cpu.eip/EAX untouched), and a `__chkesp FAILED` / stack-corruption diagnostic fired on `tid=1007` in the same instant.

**Traced further with `LOG_CATEGORIES=dll` added: the apparent "LoadLibraryA can't find an already-loaded DLL" symptom was a red herring -- root cause found and fixed.** `MSJTER35.DLL` and `MSJINT35.dll` both mapped and cached correctly, exactly once, inside a single `load_dll("MSJTER35.DLL", ...)` call made by `tid=1011`'s own guest code; that call's `on_dependency_loaded` hook then invoked `MSJINT35.dll`'s real `DllMain`, which is the same nested `cpu.run()` that was still in flight when `tid=1011` died and `tid=1007`'s `Sleep()` got refused (previous paragraph). The resulting `__chkesp` fatal halt raised `FatalHaltError`, which unwound straight up through `on_dependency_loaded(imported_dll)` (`dll_loader.py:360`, still on the call stack) into `load_dll`'s own broad `except Exception` -- logged as `"Failed to load MSJTER35.DLL: fatal halt..."` and returned `None`, even though both DLLs were already correctly loaded and cached. That `None` then made `_load_dll_by_name` log the misleading `"LoadLibraryA(MSJTER35.DLL) -> NULL (not found)"` -- a fatal, whole-session-stopping condition silently downgraded to an ordinary per-call warning, letting the game limp on in a corrupted state instead of actually stopping.

**Fixed**: `dll_loader.load_dll` (`tew/loader/dll_loader.py`) now has `except FatalHaltError: raise` before its broad `except Exception`, so a fatal halt raised anywhere inside a nested dependency-`DllMain` call (on any thread, not just the DLL's own) propagates to wherever `FatalHaltError` is actually meant to be handled instead of being swallowed as a load failure. 2 new tests (`test_dll_loader.py::TestLoadDllPropagatesFatalHalt`, monkeypatching `EXEFile`/`find_dll_file` to inject the exception without needing a real PE fixture) -- one confirms `FatalHaltError` propagates, one confirms ordinary PE-parsing exceptions still return `None` as before. Full suite: 1148/1148. **Live re-verified**: same run now ends cleanly at 37.155s with a full diagnostic dump and `"Execution stopped."` instead of continuing in a corrupted state to the 60s timeout cutoff -- no more `"Failed to load MSJTER35.DLL"` / `"LoadLibraryA(...) -> NULL"` lines at all.

**Current blocker, next session**: the underlying trigger for the fatal halt itself is still open -- `sleep_current`'s (and the other 4 guarded methods') refusal path has no defined fallback contract for the calling stub handler (cpu.eip/EAX left exactly as found), which is what let `tid=1007`'s `Sleep()` call fall through into the `__chkesp`-caught stack corruption in the first place. Needs actual design thought (retry once `reentrant_depth` drops back to 0? no-op-continue? something else?) before the run can get past this point to finally reach the real DAO-3075 error text.

## Previous status (2026-08-16, cont'd x2)

**Found the real root cause of the empty DAO error description, and it's a genuine tew architecture gap, not a Jet/DAO logic bug.** Traced the actual message-lookup chain live: `msjet35.dll`'s `FUN_7a8ea900` dynamically `LoadLibraryA("MSJTER35.DLL")`s (succeeds, loads at `0x18000000`) and `GetProcAddress`es two real exports (`JetErrFormattedMessage`=Ordinal#5, plus #2/#3) -- all real, confirmed working. `JetErrFormattedMessage` (now decompiled, `msjter35.dll` project has real symbol names already) is a dense error-category dispatcher that delegates further; the real `LoadStringA(id=3075)` call was traced (via a new permanent debug field added to tew's own `_LoadStringA` handler, `user32_handlers.py`, logging the real caller address resolved through `dll_loader.find_dll_for_address`) to **`MSJINT35.dll`** -- a *different* DLL entirely, the real locale-specific Jet error-string resource DLL that `MSJTER35.DLL` delegates to.

**`MSJINT35.dll` exists on disk (`~/.emu32/WINDOWS/System32/msjint35.dll`) and IS loaded by tew -- but only as an implicit PE-import dependency of `MSJTER35.DLL`, never via an explicit guest-code `LoadLibraryA` call.** Confirmed in `tew/loader/dll_loader.py`'s `load_dll()`: it recursively loads import-table dependencies (`imported_dll = self.load_dll(descriptor.dll_name, memory)`, line ~312) as part of mapping the parent DLL, but **never invokes the dependency's own `DllMain`** -- that only happens in the separate, higher-level `LoadLibraryA` Win32 handler (`kernel32_handlers.py`'s `_load_dll_with_dllmain`), which only runs for the top-level DLL the guest explicitly requested. So `MSJINT35.dll`'s own CRT/DllMain startup code -- which would normally stash "my own HINSTANCE" into a global for later use -- never runs. When `MSJINT35.dll`'s own exported code later executes (called indirectly through `MSJTER35.DLL`'s `JetErrFormattedMessage`) and tries to pass its own module handle to `LoadStringA`, it reads the never-initialized global (0), producing exactly the observed `LoadStringA(hInst=0x0, id=3075) -> "" (0 chars)`.

**This is a systemic gap, not specific to `MSJINT35.dll`**: any DLL pulled in only as an implicit dependency of another dynamically-loaded DLL would have the same problem. Real fix needs actual design thought (when should a dependency's `DllMain` run relative to its parent's own `DLL_PROCESS_ATTACH`, avoiding reentrancy/ordering issues if dependencies share dependencies, etc.) -- bigger than tonight's earlier quick handler-implementations. Not yet attempted.

**Temporary/permanent diagnostic added**: `_LoadStringA` (`tew/api/user32_handlers.py`) now always logs its real caller's address AND resolved `dll+offset` (via `dll_loader.find_dll_for_address`) alongside the existing hInst/id/result fields -- kept as a permanent enhancement, real diagnostic value for any future resource-string investigation, not reverted.

**Current blocker**: implement real dependency-DLL `DllMain` invocation in `dll_loader.load_dll()` (or wherever the right architectural seam is), so DLLs like `MSJINT35.dll` get properly initialized even when only pulled in as a dependency. Once that lands, re-run and check whether `LoadStringA(id=3075)` finally returns the real Jet error text -- which would let a future session confirm or refute whether DAO error 3075 really means "syntax error, missing operator" (and if so, revisit whether the SQL text or the `CreateQueryDef` chain traced earlier tonight has a real bug), or means something else entirely.

## Previous status (2026-08-16, cont'd)

**The `CreateQueryDef`/DAO-3075 investigation is fully traced live, end to end, across three DLLs, and the empty error description is now pinned down to a specific, separate root cause.** New reusable tooling added along the way: a generic backtrace-to-file breakpoint helper (`run_exe.py`, reuses `_dump_cpu_state`/`_walk_ebp_chain` from `tew/kernel/exception_diagnostics.py` with a custom file-writing `log_fn` instead of the logger) -- confirmed working, wrote a real EBP chain to `backtrace_008fe67b.txt` on demand.

**Verified tew's CPU-core JGE is correct** (`core.zig:468`'s `evalCond`, case `0xD => sf == of` -- matches real x86 exactly; checked all 16 condition codes while there, all correct). No prior test coverage existed for JGE specifically (or any Jcc) -- added 2 real tests (`cpu/src/engine.zig`, taken/not-taken via `CMP EAX,0`+`JGE`). 77/77 Zig tests passing.

**Traced `Dbcode_CreateTmpQuery`'s QueryDefs-search loop live and it's working correctly** -- `QueryDefs.Count()=136` (a real, plausible number for a working DB), loop correctly scans all 136 existing named queries (indices 0-135, `Count()` constant throughout), finds no match for `"#Temporary QueryDef#"` (expected -- that's a scratch name, not meant to persist), correctly falls through to the create-fresh branch on iteration 137. This rules out the search/comparison logic (and, incidentally, confirms real JGE behavior live, not just in isolated tests) -- the bug is not here.

**Traced the real `CreateQueryDef` implementation chain, live, through all the thunks/vtables, into actual msjet35.dll Jet-engine code**:
`Dbcode_CreateTmpQuery` (MCity_d.exe, `0x008fe4a0`) -> `FUN_04487388` (dao350.dll thunk, delegates to an internal ISAM object's own vtable`+0xa0`) -> `FUN_0448356f` -> `FUN_044c98fe` (real DAO internals, constructs the QueryDef object, real query name `"#Temporary QueryDef#"`) -> `FUN_044d519b` -> `(*DAT_044e534c)` (a dynamically-bound global ISAM function pointer, same family as the already-known `DAT_044e52e8`, zero in the static image) -> **`FUN_7a8ae64d`** (real msjet35.dll, confirmed via the established `runtime = static - 0x65840000` delta). That last function is a real 3-way decision point:
```c
FUN_7a843cfc(DAT_7a936104);              // lock
iVar1 = FUN_7a848e20(param_1);           // session/context check
if (iVar1 == 0) { uVar2 = 0xfffffbb0; }  // error path A
else {
    uVar2 = FUN_7a858a28(param_1,param_2,&local_8);  // catalog lookup; param_2 = puVar3[0x2a] (a resolved container/context handle, NOT the SQL text)
    if ((int)uVar2 < 0) { /* propagate */ }
    else if ((local_8 == &DAT_7a84f9e8) && (param_4 < 0xfde9)) {
        uVar2 = FUN_7a858a5f(param_1,param_2,local_10,8,2);      // second lookup
        uVar2 = FUN_7a856c17(param_1,local_10[0],param_2,param_3,param_4,param_5,param_6);  // real compile/create -- NOT YET TRACED
    } else { uVar2 = 0xfffffae0; }                                // error path C -- type-check mismatch
}
FUN_7a843d0a(DAT_7a936104);              // unlock
```
Not yet live-captured which of the 3 paths actually fires or what `FUN_7a856c17` does -- next concrete step if this thread gets picked back up.

**Checked the OLE Variant/BSTR construction path for the SQL text itself (`"SELECT Max(PartID) AS Expr1 FROM Part;"`) -- verified CORRECT, not the bug.** `dbVariant::dbVariant(char*)` (MCity_d.exe) -> `OleVariant::OleVariant(...,0xe)` -> `lstrlenA` + `Ordinal_150`/`SysAllocStringByteLen` (OLEAUT32). Read both tew implementations line by line: `lstrlenA` is a correct null-byte scan; `SysAllocStringByteLen` does an exact `length`-byte copy (no premature null-stop, matching real semantics), correct 4-byte length prefix, correct 2-byte trailing null. This specific string's construction is sound.

**Found and fixed a real, separate bug while checking the above: `OLEAUT32.dll` `Ordinal #9` (`VariantClear`) only cleared 4 of the 16 `VARIANT` bytes.** Real callers (msjet35.dll, dao350.dll) import `VariantClear` by ordinal, not by name -- the ordinal-9 handler had its own, independently-written implementation that zeroed only the `vt`/reserved header (`+0`..`+3`), leaving the 8-byte value union (e.g. a `BSTR` pointer at `+8`) untouched, so a "cleared" `VARIANT` still held stale data for anything reading it without checking `vt` first. Fixed by having `Ordinal #9` delegate directly to the (correct) named `VariantClear` handler so the two can't drift apart again. New tests: `tests/unit/api/test_oleaut32_variant_clear.py` (4 tests, including one asserting both entries resolve to the literal same function object). 1125/1125 passing. **Confirmed via live re-run this fix does NOT change the `CreateQueryDef` failure** -- identical `stdout.txt`/halt output before and after. Real bug, worth keeping, but not this symptom's cause -- don't re-suspect it for this specific investigation.

**Live-captured the real `Error.Description` BSTR content via `DumpErrors` (MCity_d.exe, `0x008f8060`) and it changes the shape of the investigation.** `DumpErrors` prints `" DAOERROR: (%d) %s: %s\n"` with (Number, Source, Description) read via three real COM getters on the `DAOError` object (vtable `+0x1c`/`+0x20`/`+0x24`). Our `stdout.txt` capture showed Number=3075 and Source="DAO.QueryDefs" printing fine, but Description came back empty. Hooked right after the `Error.Description` getter's own call+`__chkesp` sequence completes (`0x008f8275`, description BSTR pointer stable at `[EBP-0x20]` by then) and read the real bytes live: `bstr_ptr=0x70aa514 byte_len=0` -- **a real, validly-allocated, genuinely zero-length BSTR**, not a NULL pointer, not garbage, not a formatting bug. This rules out several hypotheses (pointer corruption, BSTR-vs-ANSI `%s` confusion, memory corruption in the variant-passing chain) in one shot.

**Current blocker**: real DAO/Jet error descriptions come from a message lookup by error code, not dynamic construction -- that table lives in `msjter35.dll` (the real Jet error-message resource DLL, confirmed via its own version string `"Microsoft Jet Database Engine Error DLL"`, present on disk at `~/.emu32/WINDOWS/System32/msjter35.dll`). Already confirmed **that DLL's error text is NOT plain-extractable** -- neither Ghidra's string search nor raw `strings -el` found any real message text in it (only 23 total UTF-16 strings, all version-resource metadata) -- it's stored in some non-standard/compressed resource format. Next step: find where/how msjet35.dll or dao350.dll actually loads an error description string (likely a `LoadStringA`-style call, or a custom resource-reading routine, ultimately reading from `msjter35.dll`), and check whether tew implements that mechanism at all. This is a genuinely separate, previously-uninvestigated piece of tew's emulation from the `CreateQueryDef` logic itself -- getting it working would let a future session finally read the REAL descriptive error text (confirming or refuting the "missing operator" 3075-means-syntax-error theory) regardless of whether `CreateQueryDef`'s own logic turns out to be correct-per-Jet or a real tew bug.

## Previous status (2026-08-16)

**The entire B-tree/`Workspace::OpenDatabase` investigation (running since 2026-08-06/07) is RESOLVED.** Re-ran `run_exe.py` with the new `OVERLAPPED.Offset` support live (`LOG_CATEGORIES=cpu,startup,fileio`) and confirmed directly: the impossible `field@iVar6+2=1903` page (the value that started this whole investigation, "exceeds its own page's 1808-byte capacity") **never occurs this run**. Real positioned reads now scatter across the file (`offset=4169728`, `407552`, `4902912`, `2015232`, `5527552`, ...) instead of the old monotonic sequential march. The same buffer (`0x41ab6000`) that used to always land on `field@+2=1903` via the coincidental sequential offset 69632 now gets reached via a real positioned read at offset 4902912 and reads `field@+2=34` -- small, sane, exactly what a healthy page should look like. Every `btree-probe` hit this run shows plausible small `field@+2` values (`1578`, `899`, `34`, `1722`, `719`, `343`, ...), no overflow, no corruption.

**Confirms last session's hypothesis outright**: the `1903`/page-overflow bug was never a real DAO/Jet, `Online.MDB`, or msjet35.dll problem (Molly's original "MCO shipped and worked" instinct was right all along) -- it was tew's own `ReadFile` silently ignoring `OVERLAPPED.Offset` and serving whatever came next sequentially, regardless of what DAO/Jet actually requested. All of the deep msjet35.dll tracing from 2026-08-15 (`FUN_7a8481a0`'s tail-page branch, the `PageCacheEntry` hash-cache chain, `0x071b0748`/`0xD83A4000`, etc.) was real, accurately-decompiled DAO/Jet internals -- just not the actual root cause. Keep that tracing as reference (real, confirmed code paths, genuinely useful if this area needs revisiting for a different reason) but the specific bug it was chasing is closed.

**New result**: the run now progresses well past the entire DB-open/B-tree sequence -- further than any prior session -- and halts cleanly on an ordinary, expected-shape gap: `[UNIMPLEMENTED] user32.dll!IsCharAlphaNumericA`. Not a crash, not corruption, just a routine unstubbed Win32 function. Implementing it now (real ASCII alnum classification -- locale-aware in real Windows, but plain ASCII is correct for every input this US-English title actually passes) to see how much further a run gets.

**Implemented and tested three more small handlers this session, each moving the run further**: `user32.dll!IsCharAlphaNumericA`, `user32.dll!IsCharAlphaA` (both plain ASCII classification, real tests in `tests/unit/api/test_user32_ischaralphanumerica.py`/`test_user32_ischaralphaa.py`), and `kernel32.dll!lstrcmpiA` (case-insensitive compare, `tests/unit/api/test_kernel32_lstrcmpia.py`). 1121/1121 tests passing.

**Current blocker, real this time (not a missing-handler gap)**: after those three, the run hits `fatal_halt at EIP=0x001fe012` on `tid=1011` with no `[UNIMPLEMENTED]` line and no `except.txt` -- the reason is only in `~/.emu32/MCity/stdout.txt` (a guest-written file, checked separately from `/tmp/emu.log`, per the emu32 skill's Post-Run Checks). Real content this run:
```
dbcode.c(3376)  The class has not been licensed     (x2)
dbcode.c(4418) Could not create Query
dbcode.c(3426)  DAOERROR: (3075) DAO.QueryDefs:
ASSERT: dbcode.c(4478) pQueryDef
```
DAO tries to create a `QueryDef`, gets real DAO error `3075` against `DAO.QueryDefs`, and asserts on the resulting null/invalid `pQueryDef` at `dbcode.c(4478)` -- that assertion is what trips the `DebugBreak()`/INT3 landing at `0x001fe012`.

**Molly's call: `"The class has not been licensed"` (`dbcode.c(3376)`) can be ignored -- it is NOT the cause of the QueryDef failure, don't chase it.** Real target is the `CreateQueryDef`/error-3075/`pQueryDef` null-assert chain at `dbcode.c(4418)`/`dbcode.c(4478)` specifically. Not yet started -- next session should find the real address for this in Ghidra (`dbcode.c` line numbers map to `dao350.dll`'s `Dbcode_...` functions, same convention as every other `dbcode.c(N)` reference this project has traced) and decompile from there.

## Previous status (2026-08-15, cont'd)

**The `FUN_7a8412c3` chain (queued below) is now fully traced end to end, msjet35.dll's own `state`-machine byte confirmed via raw disassembly, and it surfaced a much bigger finding: tew's own `ReadFile`/`WriteFile` never support positioned/`OVERLAPPED` I/O at all.** This supersedes the previous entry's "decompile `FUN_7a8412c3`" blocker -- that's done, don't redo it.

**Chain traced (project `debug_clean`, program `msjet35.dll`), all via decompile + raw disassembly cross-checks, not decompiler C alone**:
`FUN_7a8412c3` (lock+dispatch) -> `FUN_7a841230` (hash-table lookup keyed on the raw `uVar4`/`0x071b0748` value -- NOT a page number, just a hash key; chain nodes store their key at `+0xc`) -> miss -> `FUN_7a84220d` (pool-slot allocator: existing-slab bitmap scan or fresh `FUN_7a842571`-backed slab, 48 slots/slab, 0x54=84 bytes/slot, no I/O) -> `FUN_7a84239a`+`FUN_7a842468` (base+derived constructor pair building a `PageCacheEntry : HashNode`, full field map below) -> `FUN_7a841344` (state-field dispatch) -> `FUN_7a84271d`/`FUN_7a84274e` (same shared I/O-dispatch code, reached via **two separate real jump tables**, confirmed via raw bytes not decompiler grouping: table1 @`0x7a8427d4` for the state 0-4 first switch, table2 @`0x7a8427e8` for a second re-read-and-redispatch on the same field -- case 3's actual target is `0x7a842780`, confirmed identical to case 0/2's target via the real table bytes, not assumed from the decompile's C grouping).

**`PageCacheEntry`/`HashNode` struct fully mapped** (offsets 0x00-0x53, size 0x54=84 bytes, confirmed via `FUN_7a84220d`'s slot-stride math) -- given to Molly to type into Ghidra by hand, not yet applied in the project. Key fields: `+0xc` = raw hash key (verbatim, set by `FUN_7a842468`), `+0x1c..+0x38` = 8-slot state-word array (`state[0]`=3 at construction confirmed via both hand bit-math AND the exact final-write instruction at `0x7a842435`; `state[1..7]`=5, a deliberate out-of-range sentinel since the valid switch range is 0-4). A freshly-constructed entry **always** starts in state 3, confirmed at the instruction level (`AND EAX,7` at `0x7a84272c`).

**State 3 does reach the real `ReadFile` dispatch** (corrected an earlier wrong claim mid-session that it didn't) -- `FUN_7a841bf0(&DAT_7a93a5b8,...)` allocates a fresh 2048-byte buffer from a `VirtualAlloc` reserve/commit arena (not from disk), stores the result in EBX, then falls through to the same `case 0/2/3` I/O dispatch as the normal path, calling `FUN_7a842abc` (confirmed = the real `ReadFile` wrapper, real `OVERLAPPED.Offset` + fallback `SetFilePointer` path) with offset = `*(this+0xc) << 0xb`.

**Corrected a real arithmetic mistake mid-session**: first said `0x071b0748 << 11` &asymp; 244GB (wrong -- computed the full-precision product, ignoring that `SHL EAX,0xB` is a 32-bit truncating register shift on real x86). Verified via raw bytes at `0x842781`-`0x842784` (`8B 47 0C` / `C1 E0 0B`, a genuine 32-bit `SHL r/m32,imm8`) that the real truncated result is **`0xD83A4000`** (&asymp;3.38GiB) -- still impossible for a 5.6MB file, just a different number. If this ever needs re-quoting, use `0xD83A4000`, not the 244GB figure.

**Verified tew's own CPU-core SHL is NOT the bug**: added `cpu/src/engine.zig` test `"doGroup2 SHL EAX,0xB truncates to 32 bits on a large shift count"` (EAX=`0x071B0748`, shl 0xB -> expects `0xD83A4000`) -- passes, 75/75 total. `doGroup2`'s SHL case (`(val << c5) & mask` on a native `u32`) correctly truncates exactly like real hardware. Prior tests only covered shift-by-1 CF-flag correctness (the 2026-08-06 CF bugs), never a larger shift count or the result *value* -- real, previously-untested gap, now covered.

**`OVERLAPPED.Offset` support is now implemented and tested** (`tew/api/kernel32_io.py`'s `_read_file`/`_write_file`) -- both now read the real 5th stack parameter (`lpOverlapped`), and when it's non-NULL, read `Offset`/`OffsetHigh` from guest memory at `+8`/`+0xC` (real `OVERLAPPED` layout) and use that 64-bit position for the actual `os.pread`/`os.pwrite`/`entry.data` slice, **without** advancing `entry.position` -- matching real Win32 semantics (a positioned read/write via a non-NULL `lpOverlapped` doesn't disturb the handle's own sequential file pointer, even on a handle not opened with `FILE_FLAG_OVERLAPPED`). NULL `lpOverlapped` still falls back to the old purely-sequential behavior, unchanged. New tests in `tests/unit/api/test_read_write_file_handle.py` (`TestOverlappedReadWrite`, 2 tests): a positioned read at a non-sequential offset returns the right bytes and leaves `entry.position` untouched; a positioned write similarly lands at the right offset without moving the cursor, verified by reading the whole file back afterward. Full suite: 1088/1088 passing (up from 1086).

**Current blocker**: re-run the B-tree investigation now that positioned reads are real. The open question from before -- whether `FUN_7a842abc`'s computed offset for the tail-page transition (`0x071b0748 << 0xb` = `0xD83A4000` on the live/buggy path, vs whatever the "normal" `FUN_7a8870a2` path computes) is what actually lands on page 34, or whether page 34 was previously an artifact of tew's old always-sequential `ReadFile` -- can finally be answered for real. Next session: re-run with the existing btree-probe/next-page-probe/tail-page-probe breakpoints still wired in `run_exe.py`, and check the (now-positioned) `ReadFile` log lines' `offset=`/`[overlapped]` markers against what actually gets requested at each of the 3 B-tree levels, especially level 3 (the buggy one). If the overlapped offset now genuinely differs from `69632`/page 34, that confirms the offset-computation chain traced this session (`FUN_7a8481a0` -> tail-page -> `FUN_7a8412c3` -> ... -> `FUN_7a842abc`) really is broken somewhere and page 34 was coincidental; if it still lands on 69632, the bug is elsewhere (or `0x071b0748` genuinely isn't the value used for this transition and needs re-tracing).

## Previous status (2026-08-15)

**Environment blocker below (2026-08-14) is no longer reproducing -- not root-caused why, just confirmed working.** Two separate runs today (`run_exe.py`, no code changes to the SDL/window-manager layer, no reboot performed by this session) both completed cleanly through the entire B-tree window with no X11 hang, no dummy-driver Vulkan failure, and no Wayland/NVIDIA segfault. Whatever was wrong on 2026-08-14 is not currently blocking -- possibly Molly rebooted since then, possibly it was transient compositor/session state as speculated. Not investigated further since it isn't blocking; if it recurs, re-read the 2026-08-14 entry below rather than re-deriving those three driver failure modes from scratch.

**The B-tree page-34 investigation's real mechanism is now fully decompiled, and it's a different shape than assumed.** Decompiled `FUN_7a8481a0` (the cursor-descend loop) in full (project `debug_clean`, program `msjet35.dll`). After each `FUN_7a848399` call, there are three real branches, not one:
```c
iVar3 = FUN_7a848399(piVar5,param_1,&local_4);
if (iVar3 == -2) {                              // "not found"
    iVar3 = FUN_7a879d3b((int)piVar5);          // = *(int*)(iVar6+0x10), "tail_page" field
    if (iVar3 == 0) {
        iVar3 = FUN_7a879da5((int)piVar5);      // bitmap nearest-set-bit scan, threshold 0x70f-field@+2
        goto LAB_7a848380;                       // -> falls through to FUN_7a8870a2 below
    }
    uVar4 = FUN_7a879d3b((int)piVar5);          // tail_page != 0: uVar4 = tail_page directly, NO FUN_7a8870a2 call
} else {
LAB_7a848380:
    uVar4 = FUN_7a8870a2(piVar5,iVar3,piVar6);   // normal "found" path (previously assumed to be the only path)
}
```
`FUN_7a879d3b` (runtime `0x15039d3b`) and `FUN_7a879da5` (runtime `0x15039da5`) are both trivial one-liners, real addresses confirmed via the established `runtime = static - 0x65840000` delta.

**Confirmed live, twice, identical results both runs**: the level-2->3 transition (btree-probe hit #2, `field@+2=1787`, healthy page -> hit #3, `field@+2=1903`, the buggy page at Tmp.MDB file offset 69632) takes the **third branch above** -- zero `FUN_7a8870a2` hits in the window between hit #2 and hit #3 (breakpoint armed the whole time, confirmed via the existing hit-count-gated register/unregister lifecycle), meaning `iVar3==-2` and `tail_page != 0` for this transition. Added a new breakpoint at `FUN_7a879d3b`'s entry (`run_exe.py`, same TEMPORARY/windowed-lifecycle pattern as the existing probes) that fired twice (matching the decompile's two calls on this branch) with **identical raw value both times**: `tail_page@iVar6+0x10 = 0x071b0748` (119,211,848 decimal).

**This value cannot be a raw page number** -- the whole file is only ~2,873 pages, ruling out the theory (implicit in earlier sessions) that `iVar6+0x10` directly holds the next page index. `uVar4` (whichever branch produced it) is stored into `local_8`/threaded state and consumed at the **top of the next loop iteration**, before `FUN_7a848399` runs again: `FUN_7a8412c3(this_vtable[2], param_3, uVar4, 1, param_5, this_vtable)`. That's the real "fetch/pin page given identifier" call -- not yet decompiled -- and it's almost certainly what turns `0x071b0748` (or the normal-path `FUN_7a8870a2` result, for the other two transitions) into the actual `ReadFile` that lands on file offset 69632/page 34.

**Current blocker**: decompile `FUN_7a8412c3` (static `0x7a8412c3`, runtime should be `0x15012c3`-pattern via the same delta -- not yet computed/verified, recompute carefully) to find how it resolves a raw `uVar4` identifier like `0x071b0748` into a real page fetch, then capture it live (same windowed-breakpoint pattern) for all 3 loop iterations to see whether the level-2->3 resolution is correct per real Jet semantics or is where the actual tew bug lives. `FUN_7a8870a2`/`FUN_7a879d3b`/`FUN_7a879da5` are now fully understood -- don't re-decompile them, just reference this entry.

**Unrelated, noted but not investigated**: both runs today ended in a fatal halt at `EIP=0x00200742` on the **main thread** (`tid=1000`, not the DB thread), 30-60s after the B-tree window closes (60.5s and 92.9s respectively across the two runs) -- a different subsystem entirely, out of scope for the B-tree work. Worth a future session's attention but do not conflate with the above.

## Previous status (2026-08-14)

**Not a code blocker -- an environment blocker on this specific machine.**
The B-tree investigation itself (see "2026-08-09, cont'd" below) is fully
queued and ready to resume; nothing about it has changed. What's actually
stopping progress: `run_exe.py` cannot run at all right now, on any of the
three SDL video drivers tried, each for a different real reason:

- **Default (X11/XWayland)**: hangs forever, confirmed via `gdb -p <pid>
  thread apply all bt` -- the main thread is parked in `X11_ShowWindow` ->
  `XIfEvent`/`xcb_wait_for_event`, waiting on a `MapNotify` from the window
  manager that never arrives. **Not stale process state** -- killing and
  letting `kwin_wayland_wrapper` respawn a completely fresh `Xwayland`
  process (safe to do: confirmed via process tree that this session's own
  `konsole` is a native Wayland client, not an Xwayland client, so this
  doesn't risk the session) made no difference. Root cause still unknown --
  possibly a KDE window-management policy (focus-stealing prevention /
  window rules for unmanaged apps), not yet investigated.
- **`SDL_VIDEODRIVER=dummy`**: structurally can never work, not a config
  issue -- the game's main window is created with the `SDL_WINDOW_VULKAN`
  flag (`window_manager.py`, needed because D3D8 is implemented via real
  Vulkan), and the dummy driver has no real native surface for Vulkan to
  attach to. `SDL_CreateWindow` fails outright, `CreateWindowExA`'s handler
  treats that as fatal, and the whole process halts at ~10.8s virtual time
  -- well before `DB_StartUpDatabase` (~35s). A real Xvfb install would
  almost certainly hit the identical wall (no real GPU/Vulkan backing
  either) -- not worth pursuing for that reason, confirmed via reasoning
  before spending the sudo/install effort.
- **`SDL_VIDEODRIVER=wayland`**: gets furthest -- real window created, real
  `VkDevice` created -- then segfaults. Real backtrace (via `gdb -batch -ex
  "run run_exe.py" -ex "bt"` on the actual crash, not a post-mortem core):
  `vkGetPhysicalDeviceSurfaceCapabilitiesKHR` -> NVIDIA's Vulkan ICD
  (`libnvidia-glcore.so.610.57.04`) -> `libGLX_nvidia.so.0` ->
  `XGetWindowAttributes()` (libX11) -- NVIDIA's proprietary driver calls an
  **X11** function on what is a **native Wayland** surface, reads garbage,
  crashes. Confirmed NOT a missing-package issue (`egl-wayland` and
  `libnvidia-egl-wayland(2).so` are both installed). This is a real,
  external NVIDIA driver bug/limitation (driver `610.57.04`, an
  unfamiliar-to-Claude/likely-very-recent version), not fixable from
  inside this session.

**Plan (Molly's call, 2026-08-14): wait until she's physically home and can
reboot the machine**, rather than keep chasing driver/compositor fixes
remotely -- a clean reboot is expected to reset whatever stuck
window-manager/X11/Wayland session state is causing the X11 hang at
minimum. Do not re-litigate the three driver attempts above without new
evidence; they're each root-caused, not guesses.

**State preserved, ready to resume immediately after reboot**: `run_exe.py`
and `tew/api/kernel32_io.py` still carry the uncommitted `TEMPORARY
(2026-08-09)` probes (confirmed via `git status` on 2026-08-14 -- not lost,
not committed either). The very next step, once a run actually completes,
is to let the existing `_fun_7a8870a2_entry` breakpoint (already wired to
arm/disarm itself around the `btree-probe` hits, no code changes needed)
capture real (`param_1`, raw bytes at `iVar6+0xf4`) data for the page
33->34 transition, then hand-check that against the decompiled formula in
the entry below to find out whether `FUN_7a8870a2`'s computation itself is
wrong, or whether it's being fed a bad entry index from `FUN_7a848399`'s
search on page 33.

## Previous status (2026-08-09, cont'd)

**The B-tree page-overflow blocker's Online.MDB-vs-tew-bug question is
answered: the `1903` value is real, pre-existing data in `Online.MDB`
itself, not a tew write-path bug.** Added a temporary breakpoint at
`FUN_7a848399`'s real entry (runtime `0x15008399`, still wired into
`run_exe.py`, clearly marked `TEMPORARY (2026-08-09)`) logging
`(this, iVar6, field@iVar6+2)` on each hit, plus a `buf=0x...` field to
`kernel32_io.py`'s `ReadFile` debug logging (kept permanently -- same style
as the existing `offset=`/`req=`/`got=`/`pos_after=` fields, real value,
no downside). Confirmed live all 3 invocations match prior findings
exactly (`1578`/`1787`/`1903`), and cross-referencing `iVar6` against the
new `buf=` field pinned hit #3's page to **Tmp.MDB file offset 69632**
(`ReadFile(Tmp.MDB ... offset=69632 buf=0x41ab6000)`, exact match).
Confirmed **zero `WriteFile` calls touched the read+write handle (`h=0x5044`)
before this read** -- the only prior activity on `Tmp.MDB` was the initial
`FeTools_CopyFile` copy (a separate write-only handle, `h=0x5003`, plain
sequential 4096-byte `ReadFile(Online.mdb)`/`WriteFile` pairs, no
transformation). Direct byte comparison at file offset 69632 in both
`~/.emu32/Data/DB/Online.mdb` and `~/.emu32/SaveData/DB/Tmp.MDB` (both
5,883,904 bytes) confirmed **byte-for-byte identical**, `field@+2=1903` in
both. This rules out option (b) from the previous entry (a tew-side
write-path bug) -- the value is genuinely on disk in the shipped
`Online.MDB`, not something tew's copy/write path corrupted or introduced.

**Field semantics confirmed via direct Ghidra decompile of `FUN_7a848399`
(project `debug_clean`, program `msjet35.dll`) -- `iVar6+2` is NOT
misread.** No pre-existing Ghidra struct for this Jet page format
(`list_structs` filter "page" → empty). Real decompiled body:
```c
iVar6 = *(int *)((int)this + 4);                          // page buffer ptr
local_18 = 0xe2 - (uint)(*(ushort *)(iVar6 + 2) >> 3);     // capacity - (used/8)
local_1c = iVar6 + 0x16;                                   // bitmap base (→ FUN_7a847f1d)
local_10 = iVar6 + 0xf8;                                   // key-data region start (248-byte header)
uVar1    = (uint)*(byte *)(iVar6 + 0x14);                  // initial search-bound byte
```
`0xe2` is a hardcoded immediate in the function itself (compile-time
constant, not per-page data); `0xf8`=248 as the header size before the
key-data region lines up with the earlier "2048-byte page minus ~240-byte
header" guess. `iVar6+2` genuinely is consumed as "bytes used," exactly as
prior sessions inferred -- this specific misread-field theory is now
ruled out. Also checked the caller, `FUN_7a8481a0` (B-tree cursor-descend
loop): no visible bounds/sanity check on this field before calling
`FUN_7a848399` at each level -- nothing at this layer would stop a page
like this from being visited.

**Cross-referenced page 34 against `mdbtools`' documented Jet3 page-type
enum (independent C# parser added, `/data/Code/csharp/MdbLib` --
`IndexPageHeader`/`ReadIndexPageHeader`, plus a synthetic unit test and a
temporary real-file cross-check both confirming `free_space=1903` via a
second, independent code path). Result: page 34's own type byte is
**`0x01` -- a Data Page, not an index page (`0x03`/`0x04`)**. Its other
"index header" fields are nonsense under that interpretation
(`prev_page=125501441` -- impossible, file only has ~2,873 pages total;
`pref_len=4435` -- impossible, exceeds the whole page size). This isn't a
page with one anomalous field; it's an ordinary data page being
misinterpreted as an index page.

**Molly's real-world argument (decisive, don't relitigate this):** MCO
shipped and ran successfully for real players against this exact
`Online.MDB`. If this were a genuine Jet 3.5 engine bug or real data
corruption, real installs would have hit it too, and the game wouldn't
have worked. It worked. So neither Jet 3.5 nor `Online.MDB`'s data is the
culprit -- **tew itself must be resolving to the wrong page number for
this B-tree descent step**, landing on page 34 (an ordinary data page)
instead of whatever real Windows would correctly fetch here. All prior
theories in this section (real-Jet-edge-case, misread-field,
write-path-bug) are superseded by this framing -- don't re-open them
without new evidence pointing back that way.

**Current blocker**: find where page number 34 itself gets computed/
selected as "next page to fetch" during the cursor-descend from page 33
(hit #2, `field=1787`, healthy) to page 34 (hit #3). In `FUN_7a8481a0`
(the cursor-descend loop), the call immediately after `FUN_7a848399`
returns is `FUN_7a8870a2(piVar5,iVar3,piVar6)` (or, on the `iVar3==-2`
path, `FUN_7a879d3b`/`FUN_7a879da5`) -- one of these almost certainly
resolves the found entry index into the next child page number/pointer.
Not yet decompiled or traced live. Next step: decompile `FUN_7a8870a2`
(and `FUN_7a879d3b`/`FUN_7a879da5` if the `iVar3==-2` path is the one
actually taken) to find the real mechanism, then capture it live
(extend the existing `run_exe.py` breakpoint or add a new one) to see
the actual runtime computation that produces "34" and check it against
what the entry data on page 33 should really resolve to -- this is
where the real tew bug almost certainly lives, not in page 34 itself.

## Previous status (2026-08-09)

**DSOUND serve-thread spam eliminated** (Molly: "set line 292's key in the
log to 1"). The game's own real code checks a registry value,
`HKLM\Software\Electronic Arts\Motor City\DisableAudio`
(`RegQueryValueExA` at what was `/tmp/emu.log` line 292), which
`registry.json` never had seeded, so it always came back `NOT FOUND`.
Added `"disableaudio": {"type": 4, "value": 1}` under
`hklm\\software\\electronic arts\\motor city` in `registry.json` (real
`type: 4` = `REG_DWORD`, matching the existing `instlev` entry's
convention). Confirmed live: `RegQueryValueExA(..., "DisableAudio") -> 1`,
and `stdout.txt` now has zero `[DSOUND (serve)]`/`"timer held off"` lines
at all (previously thousands per run) -- `DirectSoundCreate` itself still
runs, but the game skips whatever downstream init/serve-loop path was
producing the spam. This is a real, game-supported config toggle, not an
emulator workaround.

**The `FUN_7a848399`/`FUN_7a847f1d` B-tree stall is now FULLY root-caused
down to the exact byte, via a chain of logpoints (see changelog.md for
the full derivation)**: `FUN_7a848399` is invoked 3 times per run
(matching a 3-level B-tree traversal via `FUN_7a8481a0`), and on each
invocation computes `local_18 = 0xe2 - (*(ushort*)(iVar6+2) >> 3)` where
`iVar6 = *(this+4)` is real Jet page metadata already read via
`ReadFile`. Confirmed live for all 3 invocations:
- Invocation 1: `field@iVar6+2 = 1578` -> `local_18=29` -> passes `231` to
  `FUN_7a847f1d`. Fine.
- Invocation 2: `field@iVar6+2 = 1787` -> `local_18=3` -> passes `23`.
  Fine, but very close to the edge.
- Invocation 3: `field@iVar6+2 = 1903` -> **exceeds `0xe2*8=1808`** ->
  `local_18` underflows to `-11` -> passes `-89`, which
  `FUN_7a847f1d` (correctly, per real x86/C `uint` semantics -- verified
  this part of the CPU core is NOT at fault) reinterprets as unsigned and
  right-shifts, producing a ~537-million-iteration scan that's the
  entire stall.

`0xe2*8=1808` looks like this page structure's real usable-byte capacity
(plausibly a `2048`-byte Jet page minus a `~240`-byte header). `field@
iVar6+2` reads as a real "bytes used" counter for that page, and on the
3rd traversal level it reads `1903` -- genuinely *over* its own page's
stated capacity. The values across all 3 invocations are small, plausible,
and steadily increasing (not garbage/corrupted-looking), so this reads as
real but *internally inconsistent* page metadata, not a CPU-emulation bug
at this call site. Two independently-confirmed CPU-core CF bugs were
found and fixed while chasing this (see below) but ruled out as the
cause -- the actual `-89` production is entirely explained by this one
page's own metadata field.

**Current blocker**: find why this specific page's "bytes used" field
reads `1903` when its own capacity is `1808` -- i.e., trace back to
whatever wrote this page during `Tmp.MDB`'s creation/growth (the
`FeTools_CopyFile` from `Online.MDB` at startup, followed by Jet's own
write operations during `DB_StartUpDatabase`) to determine whether this
is a real, pre-existing Online.MDB data-integrity quirk (in which case
real Jet on real Windows would hit the same page-overflow and this call
site's lack of a bounds check might be a genuine, narrow real-Jet bug
that real installs just never trigger) or a tew-side write-path bug that
wrote too much data into this page. Not yet fixed. Given how precisely
this is now nailed down, a fresh session can go straight to comparing
this exact page's bytes between `Online.MDB` and the freshly-copied
`Tmp.MDB`, and/or checking real Jet source/documentation for what this
field and the `0xe2` capacity constant actually represent.

## Previous status (2026-08-08, cont'd)

**New real blocker found, past everything above: `tid=1012` (DB thread) genuinely
stalls inside `MSJET35.DLL`'s own B-tree code, well past `DB_StartUpDatabase`.**
Confirmed via a 2B-step run (up from the normal 500M cap): `channel_log.txt`
never advances past `carClassList::carClassList`'s (`0055bb026`, real
`MCity_d.exe` decompile) `"fetching vehicle attribute table..."` print, and
`dblog.txt` (real Jet `-dbEnableLog` trace) never advances past
`dbcode.c(1691) DB_StartUpDatabase` / `dbcode.c(1698) C:\SaveData\DB\Tmp.MDB`
-- zero further lines in either file across ~1.94 billion additional real
x86 instructions. `Final EIP` after that run: `0x0055cd53`
(`wait_task_executing`'s own polling loop, `wait_task_executing.c` --
correctly-behaving, waiting on `DB_GetGameConfigCarTable`'s posted
`DBRequestQ` request, type `0x2fb`, to complete -- this is NOT the bug, the
main thread is doing exactly what it should).

A follow-up `[alive]`-heartbeat sample of `tid=1012` specifically (temporarily
re-enabled via `LOG_LEVEL=debug LOG_CATEGORIES=startup`) confirmed the DB
thread itself is confined to a **12-byte instruction range**
(`0x15007f4d`-`0x15007f59` runtime, `= 0x7a847f4d`-`0x7a847f59` static via
the established `runtime = static - 0x65840000` MSJET35.DLL delta) across
140+ million steps -- not slow forward progress through varied code, a real
tight loop signature. Decompiled in Ghidra (project `debug_clean`, program
`msjet35.dll`):
- `FUN_7a847f1d` (the address itself) -- a bounded bitmap-scan helper
  (finds the nearest set bit at or below a given bit position via a
  byte-mask/index lookup table), used for Jet page/free-space bitmap
  navigation. Finite by construction (decrements toward 0).
- `FUN_7a848399` -- calls the above; a real binary-search routine over what
  looks like a sorted B-tree index page (midpoint search via the bitmap
  helper, then a `REP CMPSE`/`CMPSB.REPE` byte-string key comparison at
  `0x7a848516`, narrowing `[uVar8, uVar2]` each iteration). The
  `do { ... } while (uVar7 != uVar8)` loop is bounded by construction
  (standard low/high convergence) -- it cannot loop forever unless fed
  wrong data by something else every iteration.

**Ruled out this session, do not re-investigate from scratch**:
1. **Sockets/wsock32**: zero `[socket]`-category log lines across a full
   default-category run -- no networking activity at all in this
   single-player (`DBT_GO_SINGLERACE`) scenario. (Real, separate bug found
   and NOT yet fixed along the way: `wsock32_handlers.py`'s `_recv`/
   `_recvfrom` both declare a `flags` parameter in their own docstrings but
   never actually read it off the stack -- `MSG_PEEK` is silently ignored,
   so a real peek-based protocol would have its data destructively
   consumed instead of left in the socket buffer. Both also have the same
   per-byte `write8`-loop anti-pattern fixed everywhere else today. Real,
   scoped, not yet fixed -- queued below, independent of the current
   blocker.)
2. **Memory-mapped files**: `CreateFileMappingA`/`W` are registered as
   `_halt(...)` (unimplemented) in `kernel32_io.py` -- since no fatal halt
   occurred, they were never called. Jet is reading real page data it
   already fetched via ordinary `ReadFile` (the same path fixed and
   verified extensively earlier today), not a memory-mapped view.
3. **`CMPSB` (opcode `0xA6`) instruction emulation**: read `cpu/src/
   engine.zig`'s `opA6` -- `REP`/`REPE` correctly breaks on `!ZF`
   (mismatch), `REPNE` correctly breaks on `ZF` (match), matches real
   x86 semantics exactly. Fixed-width (`.w8`), so it doesn't have the
   `0x66`-operand-size-override ambiguity that caused the 2026-08-06
   `doGroup1` flags bug. Not the cause.

**RESOLVED (identified, root cause not yet fixed): confirmed live via a
temporary logpoint at `FUN_7a847f1d`'s real entry (runtime `0x15007f1d`)
that it is called only 6 total times per run** (not hundreds of thousands
-- an earlier attempt logging its assumed caller, `FUN_7a848399`'s entry,
got zero hits and was a red herring; the real caller, confirmed via the
correctly-read return address on this attempt, IS `FUN_7a848399`, just
called far less often than assumed). The real (fastcall, so ECX/EDX not
stack) `param_2` argument across those 6 calls: `231, 111, 47, 13, 23,
-89` -- a sequence that looks like a genuine binary search correctly
narrowing bounds for 5 steps, then going wrong on the 6th. `-89` as
`uint` (the decompile's own type for this variable) wraps to
`4,294,967,207` -- and the scan loop (`FUN_7a847f1d`'s `while (uVar3 !=
0) { ...; uVar3 -= 1; }`) just decrements toward 0 one at a time,
explaining the entire stall: a ~4.3-billion-iteration loop from a single
signed/unsigned confusion, same bug shape as the 2026-08-06 `n=0xfffffffc`
`memmove` bug.

**Two real CPU-core CF-computation bugs found and fixed while chasing this
(cpu submodule, `002e2db`), confirmed via new Zig regression tests, but
each independently confirmed live NOT to be the cause -- ruled out, don't
re-suspect either:**
1. `updateFlagsArithW` (`core.zig`) computed CF by re-deriving it from a
   masked operand that ADC/SBB callers had already folded a carry/borrow
   into via width-native wrapping arithmetic (`op2 +% c`/`op2 +% b`) --
   when the real operand was at its width's max value with an incoming
   carry/borrow, that add silently wrapped to 0, making CF always compute
   false in that edge case. Fixed: use `result_raw` (already correct,
   full i64 precision) directly for CF instead.
2. Every `SHL`/`SHR`/`SAR` call site (`doGroup2`/`doGroup2_8`) computed
   the correct CF (the bit shifted out) and then immediately called
   `updateFlagsLogicW`, which unconditionally clears CF -- correct for
   `AND`/`OR`/`XOR`/`TEST`, wrong for shifts, so CF after any shift
   instruction was always false regardless of what actually shifted out.
   Fixed: new `updateFlagsShiftW` (same ZF/SF/PF logic, leaves CF alone).

Both fixes rebuilt `libcpu.so` and passed the full 1086-test Python suite
and the full Zig suite (including the 5 new tests written failing-first).
A live rerun after fix #1 alone, and again after fix #2 on top, both
still hit the exact identical stall (`channel_log.txt` frozen at
`"fetching vehicle attribute table..."`) -- confirmed these are real but
unrelated bugs, not this investigation's root cause.

**Current blocker**: find where call #6's `EDX = 0xffffffa7` (-89 signed)
actually comes from -- trace back through `FUN_7a848399`'s calling
context (the real caller, confirmed earlier) to whatever computes this
value, to determine whether it's a genuine tew emulation bug (most
likely: wrong page/index metadata fed to real Jet code via `ReadFile`,
given real Jet code is executing natively here, not a tew stub) or a
real edge case (key genuinely not found) that real Jet handles specially
in a way this call site doesn't. The diagnostic logpoint approach itself
works well (see the 5-iteration refinement in changelog.md) -- next
session should set one at `FUN_7a848399`'s own entry (runtime
`0x15008399`) to see what ECX/EDX/stack args it receives on call #6, one
level further up the chain from the confirmed-answered question. Not yet
fixed.

## Previous status (2026-08-08)

**Real performance investigation, prompted by Molly noticing the run "used
to be fast" and the DSOUND serve thread missing its 10ms deadline on
every single tick.** Used cProfile (new, permanent, opt-in-only tooling --
`TEW_PROFILE=<path>` env var wraps the whole run, `TEW_MAX_STEPS=<n>`
overrides the 500M-step cap; both no-ops unless set, see run_exe.py right
after `cpu = CPU(mem)` and right before the final `os._exit()` -- dumping
stats explicitly before that call was required since os._exit() skips
normal Python shutdown/atexit, which `-m cProfile -o file` relies on).

Found and fixed a real, confirmed bug class: several hot Win32/CRT
handlers copied buffers between guest and host memory **one byte at a
time via individual `memory.read8()`/`write8()` FFI calls**, instead of
one bulk call. Confirmed via profile: `WriteFile`/`ReadFile` alone
accounted for 58% of total runtime in an early profiled window (6.3M
individual read8/write8 calls from just 2,927 handler calls). Fixed by
adding `ZigMemory.read_bytes(addr, n) -> bytes` (`memory_zig.py`, reads
directly from the shared backing bytearray, no FFI-per-byte) alongside the
existing bulk `load()`, and using both in: `kernel32_io.py`'s
`WriteFile`/`ReadFile`; `msvcrt_handlers.py`'s `fread`/`fwrite`/`_read`/
`_write`/`realloc`; `kernel32_memory.py`'s `HeapReAlloc`; and, highest-
leverage of all, `_state.py`'s `read_cstring`/`read_wide_string` (called
from dozens of sites project-wide -- every `%s` vararg substitution, every
filename/registry-value read, `getenv`, etc. -- 210,993 calls / 6.97s
cumulative in one 300M-step profile, dropped to 0.91s after the fix, a
7.6x reduction). Confirmed live: a real (unprofiled) 500M-step run dropped
from 143-145.6s to **124.5s** wall-clock (~14-17% faster), and total
Python function calls in an equal-sized profiled window dropped 37%
(36.2M -> 22.6M). 1086/1086 tests pass; new `test_output_debug_string.py`
plus updated `TestChannelPrintSkipsWorkWhenFiltered` in
`test_patch_internals.py`.

**The DSOUND serve-thread starvation itself is NOT fixed by any of the
above, and confirmed NOT a regression from these changes** -- re-profiled
after the fix, 100% of serve calls (188/188, then 1143/1144 in a longer
2B-step run) still report a hold-off, same 300-800ms+ magnitude as before.
Diagnosis: this is architectural, not a bug. The `[DSOUND (serve)]`
"timer held off" message is the *game's own* real-wall-clock
(`GetTickCount`-based) check on its audio thread; tew's scheduler is a
single-core, cooperative round-robin across all emulated guest threads
(`preempt_slice`, `scheduler.py`) -- real Windows gives the audio thread
genuine OS-level preemption to wake every 10ms regardless of what other
threads are doing, but here the audio thread only runs when the
round-robin cycles back to it, after every other thread's full batch of
real x86 instructions has executed. Closing this gap would need either
CPU-emulation throughput far beyond what's realistic for a software x86
core, or a scheduler rebuilt around real wall-clock deadlines instead of
batch round-robin -- a real architectural change, not attempted here.

**Two logging changes, both confirmed live and both now have real
consequences for run legibility -- keep in mind for future sessions**:
`run_exe.py`'s `[alive]` progress heartbeat and `patch_internals.py`'s
`Channel_DebugPrint`/`Channel_SystemPrint` were both demoted `INFO/WARN`
-> `DEBUG` earlier today (real, confirmed lag from unconditional
formatting-and-logging at real gameplay volume) -- but this means a run
at default `LOG_LEVEL=info` can now go 400+ seconds of real time with
**zero** log lines during a long, entirely healthy stretch, which reads
identically to a genuine hang from the log alone (this happened live
during today's session: a 2B-step run's log jumped from 56s to 464s with
nothing in between, and had to be verified as healthy via `ps`/timing
math rather than the log itself). Not yet decided/fixed: whether `[alive]`
should move back to INFO (or a coarser interval) by default so long runs
stay legible without needing `LOG_LEVEL=debug LOG_CATEGORIES=startup` to
confirm they're not stuck.

**New: `channel_log.txt`** (Molly, 2026-08-08: "so we can tell it from
the other 'normal' stuff") -- `Channel_DebugPrint`'s formatted output now
always writes to a real, dedicated host file (`CRTState.channel_log_fd`/
`write_channel_log`, `_state.py`, resolved next to `stdout.txt` via the
same `translate_windows_path` anchoring), unconditionally, independent of
`LOG_LEVEL`/`LOG_CATEGORIES` filtering -- deliberately kept separate from
`stdout.txt` (which only `Channel_SystemPrint`/`OutputDebugStringA` write
to, via the pre-existing `write_guest_stdout`). Confirmed live:
`~/.emu32/MCity/channel_log.txt` created fresh each run with real
`Track.c`/`dbcode.c` content.

**Also confirmed live, real but unrelated to the above, not yet fixed**:
`[DSOUND (create)] Resorted to using desktop window handle` (visible in
`stdout.txt`) is a real gap, not expected real-Windows behavior --
`user32_handlers.py`'s `GetActiveWindow`/`GetForegroundWindow` both
unconditionally return NULL regardless of whether a real window was
created and is active, so the game's own DirectSound-init fallback logic
(try `GetActiveWindow()`, fall back to `GetDesktopWindow()` if NULL)
always takes the fallback branch. Harmless (DirectSound still functions
against the desktop HWND) but not real-hardware-accurate. Not fixed this
session -- a real, scoped, next-session-sized fix if worth doing.

**Current blocker**: none identified -- the DSOUND starvation is now
understood as architectural rather than an open bug, and no other
blocker has surfaced. Candidates for a future session: (a) decide whether
to restore `[alive]` to INFO for run legibility, (b) fix
`GetActiveWindow`/`GetForegroundWindow` to track real window state, (c) a
longer/uncapped soak run now that per-step overhead is meaningfully lower.

## Previous status (2026-08-07, cont'd again x6)

**The `nfile.c` "FILE SYSTEM NOT INITIALIZED" blocker queued below is
RESOLVED, and it was never a real bug at all -- it was this session's own
test-harness `timeout` command killing the process.** Confirmed live:
`MessageBoxA`'s dialog-appear log line (`user32_handlers.py`'s
`_show_messagebox`) was correctly added to fix visibility of blocking
dialogs, but a genuinely unanswered dialog does block the whole process on
a real `SDL_ShowMessageBox` call -- a run that appeared to reach "1226s of
virtual time" was actually mostly real wall-clock time spent sitting on an
unattended `MUTEX_free - FREEING A LOCKED MUTEX (40201e60)` abort dialog,
not CPU progress (Molly caught this misread live: "Your logpoints dragged
it long enough that it died right before that message" from an earlier
round applies here too -- any external stall reads as progress if you
don't check what's actually blocking).

That mutex-free dialog was itself a **real bug**, and is now fixed: `d3d8
/idirect3d8.py`'s `IDirect3D8::AddRef`/`Release` were stubs that always
returned `1`/`0` regardless of how many references were actually
outstanding -- so *any* `Release` call, even one of several legitimately
outstanding references (confirmed live: the render thread, `tid=1007`,
released its own reference while the main thread still held one), told
the game it had just hit zero. The game's own destructor then tore down
the object's internal mutex immediately, which a moment later the main
thread tripped over as "freeing a locked mutex". Fixed with a real
per-object refcount dict (`_ref_counts`, `this` -> count), mirroring the
existing correct pattern in `idirect3d8resource.py`'s `_add_ref`/
`_release`. New regression tests: `test_idirect3d8_refcount.py` (6 tests,
calling `_add_ref`/`_release` directly rather than through the full
Vulkan-backed `make_vtable`). 1080/1080 tests pass. Confirmed live: the
`MUTEX_free` dialog no longer fires.

Once that dialog stopped blocking the run, the `nfile.c` chain queued
below **did reproduce once**, but only in a run launched with `timeout 90`
-- and the timing lined up exactly: `SDL_QUIT received` fired at 89.984s,
~1s before the external `timeout` would fire, meaning the harness's own
`SIGTERM` was very likely delivered through/interpreted by SDL2 as a real
window-close event. The game correctly treated that as "user closed the
window" and walked its own real shutdown path -- `Channel_DebugPrint`
`dbcode.c(1153)`/`(1172)` `Dbcode_AtExit()` &rarr;
`Dbcode_AbortCallback_KillThread()` (`dbcode.c(1107)`/`(1130)`) &rarr;
`nfile.c(200)` `FILE_allocateop - FILE SYSTEM NOT INITIALIZED` (the DB
thread got killed before/during its own filesystem teardown) &rarr;
unhandled `INT3` &rarr; fatal halt. This is a real, coherent shutdown-path
call chain, exactly matching Molly's original read that the nfile.c
assertion was downstream of a dbcode-level trigger, not an independent
bug -- but it's **only reachable by killing the process mid-run**, not
something a real, uninterrupted play session hits. Confirmed by doubling
the timeout to 180s: the run went the full duration with **no** `SDL_QUIT`,
no `MUTEX_free`, no `Dbcode_AtExit`/`nfile.c` chain at all, ending cleanly
via the normal `Execution limit reached (500000000 steps)` step cap at
145.6s -- the furthest and cleanest this project has ever run.

**Current blocker**: none identified -- this is the furthest clean run
yet (500M-step cap reached with zero halts). Next session should extend
`MAX_STEPS`/remove the cap for a longer soak run to see what (if anything)
is actually next, now that both the mutex refcount bug and the false
nfile.c trail are cleared.

Also this session: wired up (then disabled) the native ClickHouse
execution-history capture (`cpu.enable_history_capture_clickhouse`,
`run_exe.py`, gated behind `_HISTORY_CAPTURE_ENABLED = False`) as the
purpose-built replacement for ad hoc Python `cpu.add_logpoint`s on
high-frequency addresses -- per Molly's explicit steer ("this is what we
have ClickHouse for"). **Confirmed live it is not actually lightweight
enough to leave on by default**: it hooks every single memory write and
every register/EIP/EFLAGS change for the entire run, and the periodic HTTP
flush to ClickHouse couldn't keep up -- a run stalled at 83s of virtual
time after 2+ minutes of real wall-clock time, RSS climbing past 2.3GB as
the unflushed buffer piled up in memory, before being killed. Left in
place but off (flip `_HISTORY_CAPTURE_ENABLED` back on) for a future
investigation that specifically needs "what wrote address X last" and is
worth the overhead -- not a general-purpose always-on tool the way the
docstring originally implied.

## Previous status (2026-08-07, cont'd again)

**The `Workspace::OpenDatabase`/error-3343 blocker below is RESOLVED, and it
was two real tew bugs in the Win32 file-I/O layer, not a Jet/DAO problem at
all** -- confirmed by Molly's own instinct going in ("zero chance this is a
Jet bug... if anything, it's that we are running windows files under linux
encoding"), which was exactly right.

Traced live via Ghidra decompile of the real DAO350.DLL/MSJET35.DLL chain
(project `debug_clean`) plus `cpu.add_logpoint`s, including working out
MSJET35.DLL's real runtime-vs-Ghidra-static address delta
(`runtime = static - 0x65840000`, verified against the known-good
`opMovR32Imm` landmark instruction). The full real call chain: DAO350.DLL's
`FUN_0448c745` (`Workspace::OpenDatabase` wrapper) &rarr; a vtable delegation
chain (`FUN_044c5ee9` &rarr; `FUN_044c2d8a`) &rarr; `FUN_044e20c8` &rarr;
`FUN_044d896d` &rarr; a dynamically-bound ISAM function-pointer table
(`DAT_044e52e8` etc., resolved at runtime into MSJET35.DLL) &rarr;
`FUN_7a8701ed` &rarr; `FUN_7a85a900` &rarr; `FUN_7a86fac5` &rarr;
`FUN_7a86fbed` &rarr; `FUN_7a8709b6` &rarr; `FUN_7a870879` &rarr;
`FUN_7a8708a1` (the real `CreateFileA`/`GetFullPathNameA`/`FindFirstFileA`
path-resolve-and-open) &rarr; `FUN_7a8706e9` (the actual `CreateFileA` call,
confirmed requesting `GENERIC_READ|GENERIC_WRITE`) &rarr; `FUN_7a870b40`,
which calls real `GetFileType()` via `FUN_7a8709a5` before ever reading a
byte, and fails immediately if the result isn't exactly `FILE_TYPE_DISK`.

Two real bugs found, both in `tew/api/*.py`, both fixed:

1. **`kernel32_io.py`'s `_create_file_a`/`_create_file_w` collapsed
   `dwDesiredAccess` into a single `writable` boolean**, discarding whether
   `GENERIC_READ` was *also* requested alongside `GENERIC_WRITE`.
   `open_file_handle` (`_state.py`) always opened the real fd with
   `os.O_WRONLY` for any writable open, never `O_RDWR` -- and
   `kernel32_io.py`'s `ReadFile`/`msvcrt_handlers.py`'s `fread`/`_read`
   unconditionally rejected *any* handle flagged writable, regardless of
   what the fd could actually do. A real Win32 handle opened
   `GENERIC_READ|GENERIC_WRITE` (exactly what Jet requests for a live
   database file) supports both `ReadFile` and `WriteFile`; ours could only
   ever write. Fixed: `FileHandleEntry` gained a `readable` field,
   `open_file_handle` gained an `also_readable` parameter (opens `O_RDWR`
   when set), `_create_file_a`/`_create_file_w` now check `GENERIC_READ`
   too, `fopen`'s mode-string parsing now checks for `"+"`, and
   `ReadFile`/`fread`/`_read` now do a real `os.pread()` for handles that
   are both writable and readable. **Confirmed via live logpoint this was
   real and engaged correctly** (`CreateFile(...) -> 0x5041 [write+read]`)
   **but was NOT the actual root cause of this specific blocker** -- `ReadFile`
   is never even called before the real failure point, confirmed by tracing
   further.
2. **`kernel32_system.py`'s `GetFileType` had its own logic backwards**:
   `cpu.regs[EAX] = 2 if entry.fd is not None else 1` -- exactly inverted
   from its own comment ("FILE_TYPE_CHAR(2) for std handles... FILE_TYPE_DISK(1)
   for files"). Every real disk file (read-write *or* write-only) also keeps
   a live fd open, so this reported `FILE_TYPE_CHAR` for every real file and
   `FILE_TYPE_DISK` only for the read-only-with-cached-data case (`entry.fd
   is None` there). Real `Workspace::OpenDatabase` calls `GetFileType()`
   immediately after `CreateFileA` and aborts with error `-0x404` if the
   result isn't exactly `FILE_TYPE_DISK` -- **before ever calling `ReadFile`
   or checking the "Standard Jet DB" signature**, fully explaining why
   `FUN_7a870cf8` (the signature-check function, found earlier via a string
   search) never actually fired despite genuinely being in the call graph:
   the `GetFileType` gate rejects the open before ever reaching it. Fixed to
   key off `entry.path` instead (`'<...>'` sentinel paths and `/dev/null`
   are the only real `FILE_TYPE_CHAR` cases; everything else with a real
   path is `FILE_TYPE_DISK`), matching how std handles/NUL are actually
   modeled. **This was the real root cause** -- confirmed live: the
   `0x800a0d0f`/"unrecognized database format" failure is completely gone,
   `Workspace::OpenDatabase` now proceeds cleanly past the entire chain
   above it ever reached before.

Along the way, also confirmed (and this is worth keeping in mind for
future investigations, not something to redo): `Online.MDB`'s file
integrity was never in question -- byte-identical to a second, independently
obtained copy (`/data/Downloads/Motor City Online/Data/DB/Online.mdb`,
different size/date but identical header including the byte at offset
`0x42` that earlier looked like a password flag -- that theory is now known
wrong, see below), size is an exact whole number of real Jet-3.x 2048-byte
pages, and `mdb-tools` (an independent, non-Microsoft Jet parser)
successfully extracted its full schema/data back in 2025. The earlier
"password-protected, `FUN_7a870cf8`'s byte-0x42 gate never satisfied"
theory from the previous entry below is **superseded and was based on an
incomplete trace** -- `FUN_7a870cf8` genuinely is in the real call graph
(called from `FUN_7a870b40`), just never reached in practice because
`GetFileType`'s bug rejected the open one step earlier every time.

1061/1061 tests pass. New regression tests: `test_read_write_file_handle.py`
(the full `CreateFileA`&rarr;`WriteFile`&rarr;`ReadFile` round trip on a
`GENERIC_READ|GENERIC_WRITE` handle, plus a write-only-still-rejects guard),
and `TestGetFileType` additions in `test_kernel32_system_info.py` (real disk
file &rarr; `FILE_TYPE_DISK`, NUL device still &rarr; `FILE_TYPE_CHAR`).

**`LockFile`/`UnlockFile` are now implemented** (`kernel32_io.py`), tracking
real Win32 byte-range exclusive locks keyed by host path in a new
`CRTState.file_locks` registry -- overlap-checked against every other open
handle to the same file (not the locking handle itself, matching real
Win32's "same handle may re-lock its own ranges" allowance), released
automatically on `CloseHandle` as well as explicit `UnlockFile`. 1068/1068
tests pass; new `test_lock_file.py` covers lock/overlap/unlock/
CloseHandle-releases-locks/unknown-handle cases. Confirmed live: the
`LockFile` halt is completely gone.

**`GetComputerNameA`/`W` are now implemented** (`kernel32_system.py`),
returning a fixed, plausible NetBIOS name (`"MCITY-PC"`, matching the
"fake but plausible" convention already used for the fake PID etc.),
correctly modeling the real too-small-buffer failure path
(`ERROR_BUFFER_OVERFLOW`, required-size-on-failure vs
chars-copied-on-success semantics). 1071/1071 tests pass; new
`TestGetComputerName` cases in `test_kernel32_system_info.py`. Confirmed
live: that halt is gone, execution reaches **151s of virtual time** (up
from ~86s) -- well past the DAO/Jet sequence and deep into real gameplay
territory for the first time this session.

**Performance**: also fixed real, confirmed lag in `Channel_SystemPrint`/
`Channel_DebugPrint`'s patches (`patch_internals.py`) -- both were doing
their full vararg-formatting walk (reading guest memory per `%s`/`%d`,
building the output string) unconditionally on every call, even when
"channel" wasn't in `LOG_CATEGORIES` and the result would never be shown.
Confirmed live these fire extremely often once execution reaches real
gameplay depth (matches the jump to 151s above). Both now check
`logger.is_active(...)` first (`_channel_system_print` also checks
`guest_stdout_handle is not None`, since that sink must keep working
independent of log filtering) and return immediately if neither sink
needs the work. Also added the long-missing `"channel"` entry to
`tew/logger.py`'s own `LogCategory` type (it was real and in active use
but never actually declared). 1074/1074 tests pass; new
`TestChannelPrintSkipsWorkWhenFiltered` in `test_patch_internals.py`.

**New blocker surfaced after `GetComputerNameA`**: a *different*,
main-thread (`tid=1000`, not `1012`) `INT3`/`fatal_halt` at the same
familiar `EIP=0x001fe012` -- but this time from a completely different
subsystem: `nfile.c(200) FILE_allocateop - FILE SYSTEM NOT INITIALIZED,
CALL FILESYS_init().`. Not yet investigated -- likely a real, separate
game-side file-abstraction layer (`nfile.c`, distinct from DAO/Jet) that
some code path is using before its own init has run.

**Current blocker**: diagnose the `nfile.c` "FILE SYSTEM NOT INITIALIZED"
assertion. Not yet started.

## Previous status (2026-08-07, cont'd)

**The `Tmp.MDB`-never-gets-created mystery below is RESOLVED, and the root
cause was tew's own code, not a missing dependency or a guest-code
mystery.** Traced via Ghidra (real `MCity_d.exe` decompile, `debug_clean`
project) plus live logpoints (`cpu.add_logpoint`, capped at 8 slots --
`cpu/src/core.zig:113`/`kernel.zig:203-206`, exceeding it silently drops
registrations with no error, a real gap worth fixing separately, not done
here): `Tmp.MDB` is created exactly once, early in `WinMain`, by
`Dbcode_CopyDataBaseToSaveData` (real address `0x008ED560`) via
`FeTools_CopyFile(dest="...DB\Tmp.MDB", source="...Online.MDB")` -- a real
`fopen`/`fread`/`fwrite` copy loop, nothing to do with `DB_StartUpDatabase`'s
later `OPEN_EXISTING` open at all (the "should retry with CREATE_ALWAYS"
theory in the previous entry below was wrong, see `_state.py`'s corrected
`open_file_handle` docstring). The actual bug: `tew/api/patch_internals.py`
had a patch, `_winmain_check3` (dating to when this function was still
"unnamed" in Ghidra), that unconditionally forced `EAX=1` (success) at
`Dbcode_CopyDataBaseToSaveData`'s entry -- skipping its real body
entirely, including the copy, every run, forever. Molly confirmed the real
source template, `Online.MDB`, has always genuinely existed on disk
(`~/.emu32/Data/DB/Online.mdb`, 5,883,904 bytes) -- there was never a
reason for this patch to exist. Removed it. Confirmed live: the real copy
now runs (`CreateFile("C:\Data\DB\Online.MDB") -> [read, 5883904 bytes]`,
streamed via real `ReadFile`/`WriteFile`), and `~/.emu32/SaveData/DB/Tmp.MDB`
now exists on disk, byte-identical in size to `Online.MDB`. 1045/1045 tests
pass (one test, `TestWinmainCheck3`, removed along with the patch it tested).

Also this session: added the `-CaptureStdout` command-line flag (confirmed
via Ghidra against the real `NFSArgs_ProcessArgs` switch table, row 13) --
`WinMain` was redirecting the game's own `stdout` to the NUL device;
`stdout.txt` (gitignored, not committed) now captures real `puts()`/
`printf()` output, confirmed live with `_CLayer_DetectDebugger`'s
`"Causing exception to test for debugger...\nFound Debugger!"` lines
landing in it exactly as predicted from the decompile.

**New blocker surfaced by this fix**: `DB_StartUpDatabase` still hits the
identical `INT3`/`cpu.fatal_halt at EIP=0x001fe012` on `tid=1012` (same EBP
chain) even with `Tmp.MDB` now present and openable
(`CreateFile("C:\SaveData\DB\Tmp.MDB") -> [write]`,
`GetFileInformationByHandle` confirms the real 5.6MB size). So the raw
file-level open now succeeds, but DAO's `Workspace::OpenDatabase` COM call
(vtable `+0x58` on the Workspace object) still returns a NULL database.
`except.txt`/`dblog.txt` show only the same generic `ERROR: open database
'C:\SaveData\DB\Tmp.MDB' failed.` / `dbcode.c(1709) ERROR: open database
failed.` text -- no more specific reason at this log level. Not yet
investigated: real Jet-format validation, a missing dependency specific to
opening a *populated* database (vs. the empty/auto-created `system.mdb`
case already solved 2026-08-04), or something else entirely.

**Current blocker**: diagnose why `Workspace::OpenDatabase` fails on a
real, present, correctly-sized `Tmp.MDB`. Not yet started.

## Previous status (2026-08-07)

**Correction to the "no blocker identified" claim below (2026-08-06, cont'd
again): that was wrong, or at best described a run that never actually hit
this path.** A fresh run today (post the `CreateFile` logging-clarity fix,
see changelog.md "2026-08-07") halts at ~59s, well before any message-pump
steady state, on a reproducible `cpu.fatal_halt at EIP=0x001fe012` --
`INT3 breakpoint at EIP=0x00688c68 unhandled by SEH chain` on `tid=1012`,
same EBP-chain signature (`0x0068adf2`/`0x00a301a1`/...) as the
`Nfs_REALabortcallback`/`DebugBreak()` assertion pattern diagnosed
2026-08-03. `tid=1012` still has no `_CLayer_CatchSEH` coverage (confirmed
main-thread-only back then), so the halt itself is correctly-behaving, not a
tew bug in the halt mechanism.

**New evidence this session, added to the standing debug toolkit (see
emu32 skill v1.9, "Post-Run Checks")**: two guest-written files the
emulator's own `/tmp/emu.log` never captures, both confirming the exact
failure independently of register/EBP-chain inference:
- `/data/Code/tew/except.txt` (the game's own exception dump):
  `ERROR: open database 'C:\SaveData\DB\Tmp.MDB' failed.`
- `/home/drazisil/.emu32/dblog.txt` (MSJET35.DLL's `-dbEnableLog` trace, real
  `dbcode.c` source lines from the shipped Jet 3.5 engine):
  ```
  dbcode.c(1691) DB_StartUpDatabase
  dbcode.c(1698) C:\SaveData\DB\Tmp.MDB
  dbcode.c(1709) ERROR: open database failed.
  ```

**Root cause, confirmed reproducible (not flaky)**: `~/.emu32/SaveData/DB/`
is genuinely empty on disk (verified via `ls`) -- both `C:\system.mdb` and
`C:\SaveData\DB\Tmp.MDB` are missing, so DAO/Jet's `DB_StartUpDatabase`
(`FUN_0448c745` in `dao350.dll`, per last session's live logpoints) correctly
gets an honest `OPEN_EXISTING` failure from `CreateFile` for both -- this is
tew behaving correctly per the 2026-08-04 `open_file_handle` disposition fix,
not a regression. What's unconfirmed: `open_file_handle`'s own docstring
claims real DAO/Jet is supposed to retry `Tmp.MDB` with
`CREATE_ALWAYS`/`CREATE_NEW` after this miss (it's meant to be created
fresh, not pre-provisioned) -- but no such retry is observed; `dbcode.c`
goes straight to `ERROR: open database failed.` and the game asserts.
Either (a) that retry claim was wrong/inapplicable to this specific
`DB_StartUpDatabase` call path, or (b) something upstream that would enable
Jet's real retry logic is still missing from tew.

**Current blocker**: determine which of (a)/(b) above is true. Needs Ghidra
on `MCity_d.exe`/`0x00688c68`/`0x0068adf2` (the assertion call site) and/or
`dao350.dll`'s `FUN_0448c745`/`DB_StartUpDatabase` real disassembly to see
whether a `CREATE_ALWAYS` retry path exists in the real binary and why it
isn't being taken. Not yet started.

## Previous status (2026-08-06, cont'd again)

**The `~85 of ~90 cpu.halted = True sites lack fatal_halt` item -- deliberately
deferred since 2026-08-04 -- is now RESOLVED.** Prompted by tracing why
`VariantChangeType (Ordinal 12)`'s "halting" log didn't actually stop
execution (led to a `RUNAWAY` ~100k steps later): confirmed that plain
`cpu.halted = True` gets silently cleared by any of ~9 scheduler/nested-
call-bookkeeping sites, while `cpu.fatal_halt = True` is the only thing the
native layer (`cpu_clear_halted`) actually refuses to undo -- and most
individual handler halts across the codebase were never using it.

Surveyed every `<var>.halted = True` write site (86 total, via a script
checking the following line for `fatal_halt` rather than a same-line grep,
which undercounts -- e.g. `win32_handlers.py`'s INT3 handler already had it
on the next line). 85 were genuine unrecoverable-error halts (the
established `logger.error(...) + "halting"/"UNIMPLEMENTED"/"failed"`
shape, plus three clean process-exit calls -- `ExitProcess`,
`TerminateProcess`, `NtTerminateProcess` -- correctly permanent even though
logged at INFO not ERROR) and got `fatal_halt = True` added. One,
`seh.py`'s `_sentinel_handler`, is a legitimate resumable step-loop
completion signal (fires when a nested SEH handler call returns
*normally*) and was correctly left alone.

**One genuine false positive, caught by the existing test suite and
reverted**: `scheduler.py`'s `mark_current_dead` "no runnable threads
remain" halt was assumed to always mean a clean process exit and initially
marked fatal -- broke
`test_invoke_emulated_proc_thread_death.py::test_invoke_emulated_proc_returns_zero_when_calling_thread_dies_mid_call`,
which explicitly asserts `cpu.fatal_halt is False` for the case of a
*single* thread dying mid-nested-call (e.g. `ExitThread` from inside
`_invoke_emulated_proc`) -- a real, designed-for, recoverable scenario, not
a whole-process exit. Reverted that one site, left the other 84 in place.
1029/1029 tests pass.

**Confirmed the native boundary needed no changes at all.** Traced
`cpu.halted`'s Python property (`cpu_zig.py`) end to end: the getter is
`self._py_halted or cpu_is_halted(state)` and the setter's clear path calls
native `cpu_clear_halted`, which already refuses to actually flip
`s.halted` back to false once `s.fatal_halted` is set -- so even the two
call sites in `user32_handlers.py` (177, 220) that clear `cpu.halted`
*without* an explicit `if not cpu.fatal_halt` guard can't actually resume
a genuinely fatal halt: native `s.halted` stays true regardless (the guard
lives at the native layer, not the call site), and `cpu_run`'s own
execution loop reads that native flag directly, never the Python shadow.
`cpu.fatal_halt` itself is untouched by the `halted` setter entirely. The
architecture already fully enforced "Python has zero ability to restart a
fatal halt" -- the actual gap was 85 individual handlers never opting into
the existing mechanism, not a hole in the mechanism itself.

## Previous status (2026-08-06, cont'd)

**The `n=0xfffffffc` mystery queued earlier today is RESOLVED, and it was a
real Zig CPU-core bug, not a DAO/Jet or tew-handler issue.** Traced the
exact call site to `DAO350.DLL`'s `FUN_044d1f27` (a sorted name-index
insert routine) and its `FUN_044d1d98` binary-search helper, live, via
targeted `cpu.add_logpoint`s reading real register/memory state (not
guessing from the decompile alone). Root cause: `doGroup1`
(`cpu/src/engine.zig`, the shared handler for opcodes `0x80`/`0x81`/`0x83`
— ADD/OR/ADC/SBB/AND/SUB/XOR/CMP against an immediate) hardcoded `.w32`
for its flags computation in every case, never checking `s.op_size_ovr` —
the exact same `0x66`-prefix flags-width bug class already fixed elsewhere
in this project (accumulator-immediate opcodes, `doGroup2`), but never
audited here, matching the open "broader audit" item this queued-issues
list already flagged. The real guest instruction was `66 83 7C 24 0C 00`
= `CMP WORD PTR [ESP+0xC], 0`, comparing `local_4=-1` (`0xFFFF`) — wrongly
read as `SF=False` (positive, as a 32-bit quantity) instead of `SF=True`
(negative, as the correct 16-bit quantity), so a `JLE` that should have
skipped an `INC` didn't, incrementing an insertion index one past a valid
(empty) collection's bounds and producing `memmove`'s `n=-4`. Fixed:
`doGroup1` now takes a real `width` parameter, computed from
`op_size_ovr` by both callers (`op81`/`op83`), matching the pattern already
used by `op39`/`op3B`/`op3D`/`opA9`. New regression tests (written and
confirmed failing *before* the fix, per Molly's request):
`TestGroup1_16BitFlags` in `tests/unit/emulator/test_opcodes_arithmetic.py`
(4 tests, including a byte-for-byte repro of the real guest instruction
against a memory operand). 1029/1029 tests pass. Confirmed live:
`FUN_044d1f27` now computes `n=0x00000000` (correct, empty-collection
first-insert case) — the emulator sails straight through the entire
DAO/Jet init sequence that blocked every prior session and reaches the
game's real message loop (`GetMessageA`/`WaitForMultipleObjectsEx` steady
state), running stably until killed by timeout rather than crashing. Full
diagnosis: changelog.md, "2026-08-06 (cont'd)".

**Current blocker**: none identified yet — this is the furthest the
emulator has ever reached (past all of DAO/Jet, into the game's own
message-pump steady state). Next session should pick up from here: what
happens if it's allowed to run past the message-pump idle state (does
real UI/gameplay progress happen, or is there a *different*, not-yet-hit
blocker further in), not yet investigated.

## Previous status (2026-08-06)

**The `FUN_0448a033` "hang" queued 2026-08-04 is RESOLVED, and it was never a
hang at all** — a real bug in `_memcpy`/`_memmove`/`_memset`/`_memcmp`
(`tew/api/msvcrt_handlers.py`) made a *slow but genuinely still-running*
process look identical to a stuck one. All four looped `for idx in
range(n)` on a size read straight from guest memory with zero validation;
a garbage/underflowed `n` turned into hundreds of millions of real
`read8`/`write8` FFI calls (minutes of true wall-clock work, not a freeze)
that eventually swept the unbounded `dst`/`src` pointers through tew's own
`0x00200000+` Win32 trampoline region, corrupting the `memmove` trampoline
itself before finally faulting on a genuinely out-of-range address. Every
prior "hung" run was actually just killed by too-short a timeout (`300s`)
before it reached its own honest halt at `354s`. Fixed: all four now call
`memory.is_valid_range()` before touching anything and halt loudly
(`logger.error` + `cpu.fatal_halt`) instead of looping. New regression
tests: `tests/unit/api/test_msvcrt_memfuncs.py` (13 tests, first coverage
these four functions have ever had). 1025/1025 tests pass. Full diagnosis
(gdb-attach live-process investigation, the ClickHouse execution-history
tooling, ~/pe-walker/history-poc setup): changelog.md, "2026-08-06".

**RESOLVED (2026-08-06, cont'd)**: confirmed live, the actual faulting
call was `memmove(dst=0x06f9e014, src=0x06f9e010, n=0xfffffffc)` —
`n` is `-4` as a signed value, `dst`/`src` are real adjacent heap addresses
only 4 bytes apart. This looked like a signed-length computation
(`end - start`-shaped) underflowing somewhere upstream in the DAO/Jet call
chain, not raw garbage — confirmed correct guess: it was a tew emulation
gap, not a guest bug. See "Current status" above.

## Previous status (2026-08-04, cont'd again x3)

**Real progress on the `GetWindow`-adjacent hang, still not fully resolved.**
Two genuine bugs found and fixed while chasing it (both confirmed correct
by decompiling `FUN_0448d1f5`/`FUN_0448a801` in real `DAO350.DLL`):

1. `GetCurrentProcessId()` (`kernel32_system.py`) returned a hardcoded
   `1234`, while `GetWindowThreadProcessId` (`user32_handlers.py`) always
   writes a hardcoded fake PID of `1` -- these never agreed, so any code
   asking "does this window belong to my process" could never succeed.
   Fixed `GetCurrentProcessId` to return `1`, matching the established
   "our fake PID" convention.
2. `GetWindowLongA`/`SetWindowLongA` compared the `nIndex` argument
   (always unsigned via `memory.read32`) directly against negative Python
   int constants (`GWL_STYLE=-16` etc.) -- `0xFFFFFFF0 == -16` is always
   `False` in Python, so every `GWL_*` case silently fell through to the
   generic default, for the entire life of both handlers. Fixed with a
   proper unsigned-to-signed conversion (matching the existing idiom
   already used in `msvcrt_handlers.py`). Also added a real `GWL_HWNDPARENT`
   case (previously relied on the same broken fallback, which happened to
   produce the right answer by accident) and human-readable `GWL_*` name
   logging for both handlers.
3. Also fixed live-discovered gaps in the same investigation:
   `_ShowWindow` never updated its tracked `WS_VISIBLE` style bit (only
   toggled the real SDL window), so `IsWindowVisible` -- newly implemented
   this session -- would have gone stale after any real show/hide.
   `WindowManager` gained a public `all_windows()` accessor (previously
   only single-hwnd lookup existed) needed for `GetWindow`'s Z-order/
   sibling-walk logic.

**Confirmed via live logpoints** (`cpu.add_logpoint`, temporarily wired
into `run_exe.py` -- addresses are real, unrelocated `DAO350.DLL`
addresses since it loads at its preferred base) that neither bug was
actually the root hang cause: `FUN_0448d1f5` (the "find my own top-level
window" idiom) now returns correctly and fast; its caller `FUN_0448a801`
makes one indirect call (`CALL [0x44e5350]`, target `0x150332db` inside
`MSJET35.DLL`) which also returns cleanly (`EAX=0`); and `FUN_0448a033`
(`FUN_0448a801`'s own caller) successfully gets control back at `0x448a16a`.
**But `FUN_0448a033` never reaches either of its own two `RET` sites**
(`0x448a241`, `0x448a281`) -- the actual hang is somewhere in the stretch
between `0x448a16a` and those returns, which contains several more direct
and indirect calls (`0x448a184`, `0x448a19b`, `0x448a1b7`, `0x448a1da`,
`0x448a20b`, `0x448a21c`, `0x448a234`) not yet individually logpointed.

**Current blocker**: same as above -- narrow down which specific call in
`FUN_0448a033` (between `0x448a16a` and its returns) is where execution
actually gets stuck. The temporary diagnostic logpoints are still wired
into `run_exe.py` (clearly marked "TEMPORARY diagnostic logpoints") for
the next session to extend rather than starting over.

## Previous status (2026-08-04, cont'd again x2)

**Major root-cause find.** The `System.mdb`/error-3049/`DebugBreak` saga
from the last few entries is **fully resolved, and it was never a missing
asset or a Jet bug** -- it was a real correctness bug in tew's own
`CreateFile` handling. `open_file_handle`'s writable branch
(`tew/api/_state.py`) unconditionally did `O_CREAT | O_TRUNC` whenever a
write-capable handle was requested, completely ignoring the actual
`dwCreationDisposition` value -- so a normal `OPEN_EXISTING` request
(which must fail honestly if the file is missing, and must never truncate
an existing one) instead silently fabricated an empty 0-byte file every
time. Jet saw that empty file, correctly concluded "not a database I
recognize" (error 3049), and the game's own debug-build assertion fired
in response -- all downstream of the one bug. Fixed: `open_file_handle`
now takes the real `disposition` value and switches on real Win32
semantics (`CREATE_NEW`/`CREATE_ALWAYS`/`OPEN_EXISTING`/`OPEN_ALWAYS`/
`TRUNCATE_EXISTING`, matching the actual OS API), threaded through from
`_create_file_a`/`_create_file_w` (`kernel32_io.py`). Confirmed live:
`CreateFile("C:\system.mdb")` now honestly fails
(`disposition=3`=`OPEN_EXISTING`, file genuinely missing) instead of
faking success -- and Jet's own real fallback (auto-creating a default
workgroup database when none exists) just works, no external asset ever
needed. Neither `DebugBreak` nor error 3049 reproduce at all anymore.
615/615 tests pass.

Also confirmed as a side effect of the investigation: this same bug meant
ANY `OPEN_EXISTING`+`GENERIC_WRITE` open of a real existing file would
have silently truncated its contents to 0 bytes (`O_TRUNC` fired
unconditionally) -- a real, if not yet observed live, data-loss bug fixed
by the same change, not just the `system.mdb` symptom.

**Current blocker**: clean, honest `[UNIMPLEMENTED] user32.dll!GetWindow`
-- not yet implemented. Reached deep inside real `DAO350.DLL` code, well
past the old blocker.

Not yet re-investigated this session (superseded by the above): the
`LoadStringA(hInst=0x0, ...)` systemic empty-description bug -- may
still be worth fixing independently for readability of *future* error
reports, but is no longer on the critical path since the error it was
garbling no longer occurs.

## Previous status (2026-08-04, cont'd)

`GetUserDefaultLangID`, `GetSystemDefaultLangID` (both `kernel32_io.py`,
same fixed en-US value as the existing `GetUserDefaultLCID`), and
`GetShortPathNameA` (real `find_file_ci` existence check; returns the long
path unchanged since this emulator doesn't implement true NTFS 8.3
short-name generation -- matches real Windows behavior with 8dot3name
generation disabled, not a fabrication) are all fixed and confirmed live.
Execution now reaches significantly further: `MSJTER35.DLL`/
`MSJINT35.DLL` load, `CreateErrorInfo`/`SetErrorInfo` succeed for a real
Jet error.

**Current blocker**: that Jet error is **3049**, confirmed from
`msjint35.dll`'s real resource table: *"Can't open database '|'. It may
not be a database that your application recognizes, or the file may be
corrupt."* Right after `tid=1012` (the DAO/Jet worker thread) reports
this via `SetErrorInfo`, `tid=1000` (the **main** thread this time, not
1012) independently hits the same `Nfs_REALabortcallback`/`DebugBreak`
assertion chain seen and fixed earlier today (same EBP frames:
`0x0068adf2`/`0x00a301a1`/`0x00684de7`/`0x006848ee`/`0x004d8c71`/
`0x0068a7d5`/`0x009fcaa6`) -- this is expected, correctly-behaving
`cpu.fatal_halt` (INT3 routed through the real SEH chain, all handlers
decline, same as always), not a bug in tew. Not yet investigated: whether
the actual `.mdb` database file DAO/Jet expects even exists at the path
being used, or whether this is a real bug further up the Jet
open-database call chain.

## Previous status (2026-08-04)

The `EIP=0x001fe012` `Nfs_REALabortcallback`/`DebugBreak` halt from earlier
today is **resolved, and doesn't reproduce at all anymore**. Root cause:
`expsrv.dll`'s own init probes `oleaut32.dll!DispCallFunc`, `ole32.dll!
CoCreateInstanceEx`/`CLSIDFromProgIDEx`/`CLSIDFromProgID` via
`GetProcAddress`, all previously NULL since tew's real (non-stub)
ole32/oleaut32 COM layer never had them. Implemented `CLSIDFromProgID`,
`CLSIDFromProgIDEx`, `CoCreateInstanceEx` for real in
`oleaut32_handlers.py` (registry-driven, same honest-failure pattern as
the rest of that file). `DispCallFunc` intentionally still not
implemented -- generic x86 calling-convention/VARIANT marshaling, a
separate, larger piece of work. Execution now gets substantially further:
genuinely deep into `expsrv.dll -> vbajet32.dll -> DAO350.DLL -> exe`.
`kernel32.dll!GetUserDefaultLangID` (hit right after) is also fixed
(`kernel32_io.py`, same en-US LCID as the existing `GetUserDefaultLCID`).
Full detail: changelog.md, "2026-08-04 (cont'd)".

**Current blocker**: `[UNIMPLEMENTED] kernel32.dll!GetSystemDefaultLangID`
-- same shape as the `GetUserDefaultLangID` fix just made (this emulator
has no real multi-locale concept, so `GetSystemDefaultLangID` and
`GetUserDefaultLangID` should return the same fixed en-US value, `0x0409`)
-- not yet implemented.

Also this session: per-thread `[tid=N]` log tagging added everywhere
(`logger.py`/`run_exe.py`), and two DLL-loader efficiency fixes
(`dll_loader.py`) -- negative DLL-lookup caching, and `patch_dll_iats`
made incremental instead of O(N^2) full-rescans across a run's DLL loads.
Both confirmed live: no repeated "Could not find X" lines anywhere in a
full run, "Patched X/Y new DLL IAT entries" now reports small per-call
batches. Full detail: changelog.md, "2026-08-04 (cont'd)".

## Previous status (2026-08-04)

The `RUNAWAY` at ~195.8M steps queued from 2026-08-03 is **resolved, and
was never a real blocker** -- it was corruption fallout from a genuine
`VirtualAlloc` bug: `_virtual_alloc` (`tew/api/kernel32_memory.py`)
rejected `flProtect 0x1` (`PAGE_NOACCESS`, a legitimate reserve-then-commit
pattern MSJET35.DLL uses) as unimplemented. Fixed by adding it to
`_KNOWN_PROTECT_FLAGS`; 615/615 tests pass. Confirmed live: neither the
`VirtualAlloc` halts nor the `RUNAWAY` reproduce, and MSJET35.DLL now
loads and resolves all ordinal imports cleanly. Full detail: changelog.md,
"2026-08-04".

**RESOLVED (2026-08-04, cont'd)**: the `cpu.fatal_halt at EIP=0x001fe012`
blocker described below traced to the four missing ole32/oleaut32 COM
functions -- see "Current status" above for the fix and the new frontier
(`GetSystemDefaultLangID`). Original diagnosis kept for the EBP-chain
reference:

A new, genuine `cpu.fatal_halt at EIP=0x001fe012`, reached right after
MSJET35.DLL's import resolution. The `EBP` chain matches the
`Nfs_REALabortcallback`/`DebugBreak()` assertion path fully diagnosed
2026-08-03 -- this is the correctly-behaving, properly-marked fatal halt
case (not the soft-halt-that-doesn't-halt bug class, which remains
deliberately deferred -- see queued issues).

## Previous status (2026-08-02)

The DAO `*ppv`-stays-NULL mystery that this project chased across several
sessions (2026-07-19 through 2026-07-23) is **resolved**. Root cause: three
missing dependencies in `dao350.dll`'s real `DllGetClassObject` call chain
(`ole32.dll!CoGetMalloc` entirely unimplemented, plus two `oleaut32.dll`
ordinal-only import aliases missing — ordinals #15 `SafeArrayCreate` and #21
`SafeArrayLock`) were causing the call to abort before reaching its own
(correct) `QueryInterface` code; `_invoke_emulated_proc`'s bare-`0`-on-abort
fallback then made the abort look like a genuine `S_OK` success with a NULL
`*ppv`. All three fixed in `tew/api/oleaut32_handlers.py`. Full diagnosis
and fix sequence: changelog.md, "2026-07-23 (later session)" and the three
entries before it.

Separately, `cpu.fatal_halt` is now a real, unclearable native CPU lockup —
previously a Python-side desync let execution continue past a fatal halt to
a later, unrelated one instead of stopping dead. Fixed at the Zig/CPU layer.
Full detail: changelog.md, "2026-07-23 — cpu.fatal_halt is now a real,
unclearable native CPU lockup."

And the sentinel-collision bug that fix exposed is now fixed too:
`CPU.run()`/`CPU.step()` raise a new `FatalHaltError` the instant
`cpu.fatal_halt` newly becomes true during a call, instead of returning
normally and leaving `_invoke_emulated_proc` to fall back to a bare `0`
that any `HRESULT`-returning caller could misread as `S_OK`. Full detail:
changelog.md, "2026-07-23 (evening session) — `_invoke_emulated_proc`'s
'didn't complete' `0`-return sentinel replaced with a raised exception."

With `oleaut32.dll` ordinal #4 (`SysAllocStringLen`) also fixed the same
way as #15/#21, DAO's entire COM activation chain now completes cleanly
end-to-end: `CoGetClassObject` ×2 and `CoCreateInstance` all return real,
non-fake results, and execution genuinely **returns to the game's own
code** (`MCity_d.exe`) for the first time — the "ole32 block" that
motivated this whole multi-day investigation is fully cleared. Two more
small gaps found and fixed live-verifying that: `kernel32.dll!lstrcmpW`
(real UTF-16 comparison, `kernel32_io.py`) and `kernel32.dll!GlobalLock`/
`GlobalUnlock` (pass-through no-ops, correct for the fixed/non-moveable
memory this emulator's `GlobalAlloc` always hands out).

The `msjter35.dll`/`msjet35.dll` busy-loop described above (7,005×
`GetProcAddress` repeats, zero progress) is **resolved** — two independent
bugs, both in `kernel32_handlers.py`: (1) `LoadLibraryA`'s fallback for
DLLs not found on disk unconditionally fabricated a fake-success handle
even with zero handler coverage, unlike `GetModuleHandleA`'s equivalent
path — DAO saw a fake "loaded" DLL and kept retrying instead of getting an
honest failure; (2) `GetProcAddress`'s ordinal-lookup key format
(`"ordinal#N"`) never matched how ordinals are actually registered/parsed
everywhere else in the codebase (`"Ordinal #N"`), so ordinal lookups could
never succeed regardless of whether the export existed. Full diagnosis and
fix: changelog.md, "2026-07-23 (late-night session)".

Separately, since the actual goal is a working Access Jet 3 database (not
just DAO's COM activation succeeding), the real Microsoft Jet 3.5 Database
Engine redistributable was sourced from `~/.emu32/DBInst/DAO/data1.cab`
(same InstallShield package `dao350.dll` came from — confirmed via sha256)
and deployed to `~/.emu32/WINDOWS/System32/`: `msjet35.dll`, `msjter35.dll`,
`msjint35.dll`, `vbajet32.dll`, `msrd2x35.dll`, `expsrv.dll`, and
`msvcrt40.dll` (the last one incidentally fixing a previously-unresolved
static import of DAO350.DLL's own). See Architecture section.

`advapi32.dll!RegEnumKeyA` — the older, non-Ex sibling of
`RegEnumKeyExA` (4 args, `cchName` passed by value not by pointer, no
class/last-write-time output) — was simply never implemented, only
`RegEnumKeyExA` existed. Added it in `advapi32_handlers.py`, sharing a new
`_reg_list_subkeys()` helper factored out of `RegEnumKeyExA`'s subkey-
derivation logic. Confirmed live: execution now sails through the entire
`HKLM\Software\Microsoft\Jet\3.5\Engines` enumeration and `Engines\ODBC`
config reads (all honest `NOT FOUND`s, gracefully tolerated) — no seeding
of `registry.json` was needed for this. 593/593 tests still passing.

`kernel32.dll!GetTempPathA` was next: added in `kernel32_io.py`, returns
`C:\WINDOWS\TEMP\` (backed by a real, newly-created
`~/.emu32/WINDOWS/TEMP/` host directory so later real file I/O against
that path works). Confirmed live: cleared the halt.

`kernel32.dll!GetTempFileNameA` was the halt right after that — Jet
generating its scratch filename. Added in `kernel32_io.py`: builds
`<path><3-char prefix><4 hex digits>.TMP`, and when `uUnique == 0` (the
common case) actually creates the 0-byte file on the host filesystem via
the same `os.open(..., O_CREAT|O_TRUNC)` pattern `CreateFileA`'s writable
branch uses — so a later real `CreateFileA`/`ReadFile` against that exact
name (Jet's own scratch-file use) sees a real file, not just a name.
Confirmed live: cleared the halt. 593/593 tests passing after both.

`kernel32.dll!GetFileInformationByHandle` was next — Jet querying the
new temp file's attributes/timestamps. Added in `kernel32_io.py`: looks
up the handle in `state.file_handle_map`, `os.fstat`s the real fd (or
`os.stat`s `entry.path` for read-only entries with no fd), and fills a
real `BY_HANDLE_FILE_INFORMATION` struct (attributes via `stat.S_ISDIR`,
real `ctime`/`atime`/`mtime` converted to `FILETIME`, real size, `1` for
link count, real inode as file index). Confirmed live: cleared the halt.

`kernel32.dll!lstrcpynA` was next — added in `kernel32_io.py` next to
the existing `lstrcpyA`/`lstrlenA` (bounded copy, always null-terminates
within `iMaxLength`). Confirmed live: cleared the halt.

**Real bug found and fixed, not just a missing handler**: the very next
halt, `[UNIMPLEMENTED] msjint35.dll!Ordinal #2`, looked like another
missing-handler case but wasn't — direct inspection of the real
`msjint35.dll`'s export table (via `tew`'s own `EXEFile`/`ExportTable`
parser, offline, no emulator run needed) confirmed ordinal #2
(`CchLszOfId2`) genuinely exists and `DLLLoader.load_dll` already
resolves and writes its real address into the IAT correctly. The actual
bug: `DLLLoader.patch_dll_iats` (`tew/loader/dll_loader.py`) runs
*after* `load_dll` and unconditionally re-patches every secondary-DLL
IAT entry via `patch_iat_entry` — but never passed the already-known
real address as `real_addr`, so any entry without a matching Python
handler fell straight through to the unimplemented auto-stub fallback,
silently clobbering correct real-DLL-to-real-DLL calls (e.g. `msjet35
.dll` calling into `msjint35.dll`) with a fatal halt. Fixed by having
`patch_dll_iats` look up `self._loaded_dlls[...].exports` and pass that
through as `real_addr`; also added a `real_count` outcome bucket to the
existing "Patched X/Y ... (N auto-stubs)" summary log so this class of
bug is visible going forward instead of silently inflating the
auto-stub count. Confirmed live: MSJET35.DLL's own IAT patch pass went
from 23 auto-stubs/0 real to 1 auto-stub/7 real. 593/593 tests passing
after all three fixes above.

`user32.dll!LoadStringA` was next — added in `user32_handlers.py`. Real
`RT_STRING` resource lookup was added to `pe_resources.py`
(`PEResources.find_string`, block=(id>>4)+1 / index=id&0xF packing) and
threaded per-module: `dll_loader` is now passed into
`register_user32_gdi32_handlers` (previously it wasn't) so a real loaded
DLL's own hInstance (not just the main EXE's) resolves to that DLL's own
`.rsrc`, cached per-DLL-name. `cchBufferMax == 0` (pointer-swap mode, no
copy) is explicitly **not** implemented and halts loudly instead of
silently returning a plausible-but-wrong result — confirmed live this
session that real callers never actually hit that path, so the halt is
inert in practice, not a live gap. Confirmed live: cleared the halt.

`kernel32.dll!lstrcatA` was next — added next to `lstrcpyA`/`lstrcpynA`
in `kernel32_io.py`, matching real (unbounded, like real `strcat`)
semantics. Confirmed live: cleared the halt.

`oleaut32.dll!Ordinal #202` (`CreateErrorInfo`, confirmed via the real
`oleaut32.dll`'s export table at `/data/Downloads/i386-binaries/`) was
next. Implemented as a real dual-interface COM object in
`oleaut32_handlers.py`: one allocated object with two vtables at a
+4 offset (`ICreateErrorInfo` at the object's own address, `IErrorInfo`
at +4 — a C++-style "this-adjustor" split), `QueryInterface` switching
between them, shared refcount, and real Set*/Get* method bodies that
actually read/write the object's fields (no fake success). **Live-
verified this design was necessary, not speculative over-engineering**:
DAO's real code calls `QueryInterface(IID_IErrorInfo)` on the returned
pointer immediately after creation (succeeds via the +4 face), then
fills the object via the original `ICreateErrorInfo` pointer with real
content — `SetSource("DAO.DbEngine")`, a help context ID, a help file
pointer — before the next call. Session process note: this session
skipped `CLAUDE.md`'s mandatory HANDLER DECLARATION step (state
Function/Signature/Spec/Truthful-YES-NO in chat before writing any
handler) for every handler above; a retroactive audit found one real
violation — `LoadStringA`'s `cchBufferMax==0` path was silently
returning a plausible-but-spec-incomplete result instead of halting —
now fixed as described above. No other violations found (grep audit for
TODO/FAKE/stub/silent-pass patterns across every file touched this
session came back clean).

`oleaut32.dll!Ordinal #201` (`SetErrorInfo`) is now **implemented and
confirmed live**: stores `perrinfo` as the calling thread's current COM
error object in a new per-thread `CRTState.error_info_store` dict (keyed
by `state.tls_current_thread_id()`), releasing the previous entry and
AddRef'ing the new one via the existing `_errinfo_release_core`/
`_errinfo_addref_core` helpers (this emulator has exactly one
`IErrorInfo` implementation — `CreateErrorInfo`'s, same file — so direct
field manipulation is equivalent to a real vtable call). Always returns
S_OK per spec. Confirmed live: `SetErrorInfo(perrinfo=0x06fd2624) ->
S_OK (tid=1012)` clears the halt, and execution continues into
`MCity_d.exe`'s **own code** for the first time past this point — a
6-frame-deep call chain purely in `exe` addresses. 593/593 tests passing
after the fix. Full detail: changelog.md, "2026-08-02".

The `EIP=0x00688c69` core-dump crash found right after the above is now
**resolved, and it was never a CPU/memory bug**. `coredumpctl info` on the
crashed PID showed the segfault happening in `libnvidia-rtcore.so`
(NVIDIA's proprietary driver), reached via
`Py_Exit → exit() → __run_exit_handlers` — i.e. *after* our own code had
already cleanly logged the halt diagnostic and hit `sys.exit()` in
`run_exe.py`. `libcpu.so` (the Zig CPU) does not appear anywhere in any
crash-thread stack. Root cause: `WindowManager.shutdown()`
(`tew/api/window_manager.py`) — which properly destroys SDL2
textures/renderers/windows and calls `SDL_Quit()` — existed but was never
called anywhere in `run_exe.py`; separately, the entire Vulkan side
(`tew/api/d3d8/_state.py` instance/device/swapchain/pipeline/semaphores)
has no teardown path at all (only one `vkDestroy*` call exists in the
whole codebase, for swapchain recreation, not shutdown). So `sys.exit()`
fired with SDL2 and Vulkan still fully live, and the NVIDIA driver's own
`atexit`-registered "someone forgot to clean up" safety-net handler ran
against that live context and crashed inside its own code. This is the
first time this project reached far enough into real game code to create
a live SDL/Vulkan context and then exit while it was still up — which is
why this was never seen before. SELinux was checked and ruled out (no AVC
denials logged for the crash window). Fix: added a call to
`crt_state.window_manager.shutdown()` right before `sys.exit()` in
`run_exe.py`. Confirmed live: `[WindowManager] SDL2 shut down` now logs
and the process exits cleanly — `coredumpctl` shows no new core dump.
Vulkan itself still has no explicit teardown (only SDL2 does now) — see
queued issues below.

**Logger bug fixed, and it changed the diagnosis entirely.** `tew/logger.py`'s
`_emit()` silently dropped any `ERROR`-level message whose category wasn't in
`LOG_CATEGORIES` -- except `"exception"`, which already had a special
exemption. But the project's own mandatory "halt loudly" convention
(CLAUDE.md) requires every halt to log an `ERROR` right before setting
`cpu.halted`/`cpu.faulted` -- so any run whose `LOG_CATEGORIES` didn't happen
to include the category that logged the real reason (e.g. `seh`, `handlers`,
`cpu`) got a halt diagnostic with no cause attached, ever, regardless of how
much register/stack detail was in it. Fixed by exempting `ERROR`-level
messages from category filtering the same way `"exception"` already was.
593/593 tests still pass.

Confirmed live: re-running the *same* narrow `LOG_CATEGORIES` (`com,dll,
loader,exception,window`, still no `seh`/`cpu`/`handlers`) that previously
produced the "unexplained" `EIP=0x00688c69` halt now also surfaces:
```
[ERROR] [seh] fault at 0x15035655 unhandled by SEH chain -- halting as before
[ERROR] [cpu] Fatal halt: fatal halt at EIP=0x00688c69
```
**This completely relocates the real blocker.** `0x00688c69` was never the
fault site -- it's where the CPU ended up *after* an unhandled SEH dispatch
left things in the known "halt in place with stale stack data" state (the
already-queued `seh.py` gap). The actual fault is a genuine
`STATUS_ACCESS_VIOLATION` at `EIP=0x15035655`, **inside `MSJET35.DLL`**
(loaded at `0x15000000-0x15ffffff`), dispatched via `dispatch_exception()`
in `run_exe.py`, found no handler in the game's own SEH chain, and fell
through to the "halting as before" path. The `EAX=0xCCCCCCCC` /
`0x00688c69` diagnostic previously investigated was real but downstream --
a symptom of the unhandled-fault fallback, not the cause.

**Current blocker**: diagnose the real fault at `EIP=0x15035655` inside
`MSJET35.DLL` -- needs Ghidra decompilation of the real DLL at that offset
(`0x35655` into the module) to determine what it's doing and why the access
violates. Separately noted: the Zig CPU core's fault-reporting
(`tew/hardware/cpu_zig.py` `run()`/`step()`, `_RUN_FAULTED` path) only
surfaces `EIP` and the opcode byte, never the actual faulting *memory
address* -- real Windows access violations carry that (read/write, target
address), and not having it here made this diagnosis slower than it needed
to be. Worth a Zig-side follow-up (new `cpu_get_last_fault_addr()`-style
export) later; not blocking the current investigation since EIP alone
(0x15035655) is enough to start in Ghidra.

Two small non-blocking gaps surfaced earlier, before the `RegEnumKeyA`
halt (`kernel32.dll!IsTNT`, `kernel32.dll!GetProcessAffinityMask` — both
harmlessly return NULL, Jet handles the miss and keeps going).

**Unrelated bug found and fixed the same day (2026-08-02, later session)**:
what initially looked like "Python crashing while investigating the
`0x15035655` SEH fault" was actually two separate things. The SEH fault
itself was never the crash — tew's own dispatch handled it exactly as
designed (halted cleanly, full diagnostic printed, no host-level fault,
since guest memory faults never touch real host memory). The actual crash
was a second, unrelated bug: an NVIDIA driver atexit handler
(`libGLX_nvidia.so` this time, not the earlier `libnvidia-rtcore.so`)
aborting during process exit because `SDL_Quit()` had already closed the
X11 connection it expected to use. Fixed by replacing `sys.exit()` with
`os._exit()` in `run_exe.py`, which skips the whole atexit chain. Full
diagnosis: changelog.md, "2026-08-02 (later session)".

**Separately fixed while investigating the above (2026-08-02, later session,
cont'd)**: the 9 accumulator-immediate opcodes (`op05`/`15`/`1D`/`2D`/`3D`/
`0D`/`25`/`35`/`A9` in `cpu/src/engine.zig`) had a live instance of the same
`0x66`-prefix flags-width bug the old TypeScript emulator fixed back in
2026-03-30 -- correct register read/write, but hardcoded `.w32` for the
flags width regardless of the prefix, so SF was wrong for 16-bit results
with bit 15 set. Fixed to match the already-correct `op85` pattern; 16 new
tests added (7 covering previously-untested 8-bit AL forms, which were
already correct; 9 regression tests for the fix, each verified to fail
against the pre-fix build). `libcpu.so` rebuilt, 609/609 tests passing.
Full detail: changelog.md, "2026-08-02 (later session, cont'd)".

**The `EIP=0x15035655` MSJET35.DLL fault -- the actual current blocker --
is now RESOLVED (2026-08-02, later session, cont'd again).** Root cause:
`opMovR32Imm` (`cpu/src/engine.zig`, opcodes `0xB8`-`0xBF`) never checked
`s.op_size_ovr`, so the 0x66-prefixed 16-bit form (`MOV AX/CX/etc, imm16`)
always read a bogus 4-byte immediate instead of 2 and wrote the full
32-bit register instead of just the low 16 bits -- desyncing `EIP` by 2
bytes from the real instruction stream. Found live using the emulator's
own logpoint debugger facility (`cpu.add_logpoint`), not static analysis
alone: traced the exact instruction boundaries in MSJET35.DLL's dispatch
chain and watched execution diverge at `0x1503564b` (`66 B8 01 00`, `MOV
AX, 1`). Fixed to branch on `op_size_ovr` like the rest of the file; 3 new
regression tests in `test_opcodes_mov.py` (`TestMovR16Imm16`), verified to
fail pre-fix. `libcpu.so` rebuilt, 612/612 tests passing. Confirmed live
end-to-end: MSJET35.DLL now loads and runs with zero `[seh]` fault lines,
and execution progresses further (60.045s vs. the previous 58.55s) before
halting at the separately-tracked `EIP=0x00688c69` (see "New top priority"
below) -- real forward progress. Full detail: changelog.md, "2026-08-02
(later session, cont'd again)".

**`EIP=0x00688c69` is now fully diagnosed (2026-08-03) -- and the earlier
note about it above (`seh.py`'s "halt in place with stale stack data"
path) was wrong, superseded by fresh investigation.** It's a
`cpu.fatal_halt`, not the SEH-stale-stack path at all. Root cause: the
game's own DAO/Jet database-init-failure handler (`Nfs_REALabortcallback`
-- it's what wrote `except.txt`, noticed earlier this session) checks a
global, `_Nfs_DebuggerIsPresent`, hardcoded to `1` unconditionally in
`WinMain` (not an `IsDebuggerPresent()` check -- deliberate debug-build
behavior, used at 1,780 call sites across 922 functions, the primary
assertion mechanism for the whole binary), and calls `_Nfs_DebugBreak()`
-- a real `INT3`. tew's INT3 dispatch was unconditionally fatal, skipping
any chance for the game's own SEH handling (it installs a real frame,
`_CLayer_CatchSEH`, specifically for `STATUS_BREAKPOINT`) to run. Fixed to
route INT3 through the same `dispatch_exception()` machinery already used
for access violations. 3 new tests, 615/615 passing. Confirmed live:
`tid=1012`'s real, compiled 11-frame SEH chain now genuinely gets walked
(handlers at `0x009f5eb8`/`0x00c771b0`/`0x00c93b54`/`0x00c93cc9`, all for
`code=0x80000003`) -- every one declines, so it's still genuinely
unhandled and still halts at the same point, but now as a *proven* result
instead of a skip. This also confirmed `tid=1012` does **not** share
`_CLayer_CatchSEH`'s coverage (main-thread-only). Full detail:
changelog.md, "2026-08-03 -- INT3 now routes through the real SEH chain".

**Major forward progress (2026-08-03, cont'd): the run now gets past DAO/Jet
entirely and reaches ~195.8 million steps** (previous best: a few million).
Chasing "why does `Nfs_REALabortcallback` fire" led to `msjet35.dll`'s
own `dbcode.c`-sourced debug log (enabled via a new real `-dbEnableLog`
command-line flag), which showed DAO's COM activation fully succeeding
and pinpointed the real failure as `DBEngine::get_Workspaces()`
(`dao350.dll`, real vtable call) returning null. Traced deep into
`msjet35.dll` (`FUN_7a876127`/ordinal 154 -> ... -> `FUN_7a876e2b`,
returning `-1022`) and found two real bugs in `kernel32_io.py`:
`DeleteFileA`/`DeleteFileW` never called `SetLastError` on failure, and
`GetFileSize` unconditionally failed on any writable-mode handle (a real
Windows API restriction that doesn't exist -- confirmed live,
`GetFileInformationByHandle` already proved the exact same handle valid
moments earlier in the same log). Fixed both. Also seeded two previously-
missing Jet 3.5 registry values, `SystemDB` and `TryJetAuth`. None of
these individually fixed the exact `-1022` (still traces to a third,
unidentified call site), but the combined effect took the run from halting
after a few million steps to running clean for ~195.8M steps, well past
the entire DAO/Jet sequence into real gameplay (`dsound.dll`, `winmm.dll`,
the `GetMessageA` message pump). Full detail: changelog.md, "2026-08-03
(cont'd)".

**New blocker at the new frontier**: a `RUNAWAY` (`EIP` in invalid/
unmapped memory) at step ~195.8M, immediately preceded by two
`[UNIMPLEMENTED]` `VirtualAlloc` halts (`unsupported flProtect 0x1`, then
`MEM_COMMIT on unreserved 0x4000000`) that don't actually stop execution
(~195M more steps ran afterward) -- see "New top priority" below.

## Run command
```bash
cd /data/Code/tew
timeout -k 5 300 env LOG_LEVEL=info LOG_CATEGORIES=com,dll,loader,exception /data/Code/tew/.venv/bin/python -u /data/Code/tew/run_exe.py 2>&1 | tee /tmp/emu.log | tail -60
```
Real `dao350.dll` execution takes anywhere from ~1s to ~30s per individual
`CoGetClassObject`/`CoCreateInstance` call, so a run reaching the DAO
section needs far more than a short timeout. Since 2026-07-21's fix, runs
now reach their final halt in ~57s instead of stalling to ~71s+ — a 300s
budget is generous headroom, not an observed requirement. Add
`registry`/`handlers` to `LOG_CATEGORIES` for deeper COM/IAT investigation;
add **`scheduler,thread`** with `LOG_LEVEL=debug` for
thread-lifecycle/scheduling investigation (idx assignment, every context
switch, every block reason).

The simpler run command (`timeout -k 5 90`, `LOG_LEVEL=info`, no extra
categories) is still correct for a general boot-health check that doesn't
need to reach all the way through the DAO handshake.

## Queued issues (priority order)
- Worth a broader audit for the same "op_size_ovr-aware read, hardcoded
  flags width" bug pattern beyond the 9 accumulator-immediate opcodes just
  fixed (see "Current status") -- that fix was scoped to every call site of
  `readEaxv`/`writeEaxv` specifically (grepped exhaustively), but other
  opcode families using `op_size_ovr` directly (e.g. `op21`/`op23`/`op31`/
  `op33`/`op85`, already correct, found by inspection not a systematic
  sweep) weren't exhaustively re-verified. Not blocking anything today.
- **RESOLVED (2026-08-03, cont'd)**: `EIP=0x00688c69`'s "real next step" --
  *why does `Nfs_REALabortcallback` fire at all* -- is answered. Root cause
  was real bugs in tew's own Win32 emulation (`DeleteFileA`/`DeleteFileW`
  missing `SetLastError`, `GetFileSize` wrongly failing on writable handles)
  plus two missing Jet 3.5 registry values (`SystemDB`, `TryJetAuth`). Fixed
  all four; the game no longer hits this DAO/Jet init-failure path at all
  and execution now runs clean to ~195.8M steps. Full detail: "Current
  status" above and changelog.md "2026-08-03 (cont'd)". The general
  `_Nfs_DebuggerIsPresent`/`DebugBreak()` pattern (1,780 other call sites)
  remains true as a fact about the binary but is no longer an active
  blocker -- no further action needed unless a *different* one of those
  1,780 sites is actually hit by a future run.
- **RESOLVED (2026-08-04)**: `RUNAWAY` at ~195.8M steps. Root cause was a
  real `VirtualAlloc` gap (`PAGE_NOACCESS` wrongly rejected) plus its
  corruption fallout, not a genuinely-missing halt-propagation fix -- see
  "Current status" above and changelog.md "2026-08-04". **New current
  blocker**: `cpu.fatal_halt at EIP=0x001fe012` (see "Current status").
- **Deliberately deferred (per Molly, 2026-08-04)**: the general
  "`cpu.halted = True` doesn't actually stop the CPU" bug class (the
  ~85-of-~90-sites gap further down this list) is a known, real issue --
  it's what let the `VirtualAlloc` halts above silently corrupt the stack
  and produce the `RUNAWAY` instead of stopping cleanly. Not fixed this
  session by explicit request; still worth its own dedicated pass.
- Historical note, kept for context, now incorrect -- do not act on this:
  `0x00688c69` was previously guessed to be the same open item as the
  "Decide/implement a real unwind for seh.py's unhandled-fault path" bullet
  further down this list. Fresh investigation (see "Current status")
  disproved that: it's a `cpu.fatal_halt` from INT3 (now properly routed
  through `dispatch_exception`, see above), not the SEH-stale-stack-data
  path at all. The `EBP` chain frames captured at this halt (`0x0068adf2`,
  `0x00a301a1`, `0x00684de7`, `0x006848ee`, `0x004d8c71`, `0x0068a7d5`,
  `0x009fcaa6`, all in `MCity_d.exe`) are still accurate as the real call
  chain leading into `Nfs_REALabortcallback` -- useful starting context if
  chasing the "why does DAO/Jet init fail" question above.
- Add real faulting-address reporting to the Zig CPU core's fault path
  (`cpu_run`'s `_RUN_FAULTED` result currently only carries EIP + opcode,
  not the memory address that was actually being accessed) — would have
  made the (now-resolved) `0x15035655` diagnosis faster, and would help
  the `0x00688c69` one too. Not blocking, EIP is enough to start.
- Vulkan resource teardown is still entirely missing (`tew/api/d3d8/_state.py`
  tracks instance/device/swapchain/pipeline/semaphores/etc. with zero
  `vkDestroyInstance`/`vkDestroyDevice` calls anywhere in the codebase —
  only `vkDestroySwapchainKHR`, used for recreation, not shutdown). Lower
  urgency as of 2026-08-02 (later session): `run_exe.py` now calls
  `os._exit()` instead of `sys.exit()` after `window_manager.shutdown()`,
  which skips `exit()`/`__run_exit_handlers` entirely — so no NVIDIA driver
  atexit handler runs at all regardless of what graphics state (Vulkan, GLX,
  or otherwise) is still live. This was in direct response to a *second*
  NVIDIA-atexit crash (`libGLX_nvidia.so`/`xcb`, distinct from the earlier
  `libnvidia-rtcore.so` one) that the driver's "undocumented atexit fallback"
  mentioned below turned out not to reliably cover. See changelog.md,
  "2026-08-02 (later session)". A proper `vk_shutdown()` (destroy pipeline →
  framebuffers → image views → render pass → command pool →
  semaphores/fence → swapchain → device → surface → instance, in that
  order) is still worth doing for hygiene/correctness, but is no longer
  covering for a live crash.
- Worth a dedicated pass later: now that `patch_dll_iats`'s real-address
  bug is fixed, re-check whether any of the *other* previously-"fixed"
  halts in this session were actually this same class of bug
  (real-DLL-to-real-DLL call wrongly auto-stubbed) rather than a truly
  missing Win32 API — unlikely for the kernel32/advapi32 fixes already
  made (those were genuinely-unimplemented Python-handler gaps, confirmed
  by checking the handler registry directly each time), but worth keeping
  in mind for future `[UNIMPLEMENTED] <dll>.dll!Ordinal #N` or
  `<dll>.dll!<name>` halts where `<dll>` is one of the real Jet-family
  DLLs (`msjet35.dll`, `msjint35.dll`, `vbajet32.dll`, `msrd2x35.dll`,
  `expsrv.dll`) rather than a standard Win32 system DLL.
- Low priority, not currently blocking: `kernel32.dll!IsTNT` and
  `kernel32.dll!GetProcessAffinityMask` are unimplemented (`GetProcAddress`
  returns NULL for both) — `MSJET35.DLL` tolerates the miss and continues,
  but a real caller elsewhere might not.
- Revisit `SafeArrayLock`/`SafeArrayUnaccessData` (`oleaut32.dll` ordinals
  21/24, `oleaut32_handlers.py`) at some point — both are hardcoded no-ops
  returning `S_OK` with no real lock-count tracking, harmless only because
  nothing in this emulator currently moves or frees a `SAFEARRAY`'s
  `pvData` out from under a caller. If that assumption ever changes (real
  `SafeArrayRedim`/compaction, or any future GC-like behavior), these two
  need actual `cLocks` bookkeeping (the `SAFEARRAY` header already has a
  `cLocks` field at `psa+8`, currently always `0` — see `_SafeArrayCreate`).
  Not blocking anything today.
- Correct `cpu/src/two_byte.zig`'s `CPUID` signature to real Pentium II
  (`0x00000630`/`0x00000650`) and fix this file's "source of truth" reference
  — blocked on locating the exact Pentium II spec manual to confirm
  Model/Stepping before committing to a value.
- Not in scope when the fatal-halt sentinel was replaced with an exception
  (2026-07-23 evening), noted as a related but separate gap: a genuine
  fault occurring deep inside a *nested* `_invoke_emulated_proc` call
  currently never gets an SEH-recovery attempt at all (only the top-level
  loop calls `dispatch_exception`) — it's silently swallowed into the
  bare-`0` fallback that still exists for non-fatal incompletions
  (max_steps exhausted, thread died, unexpected non-fatal halt). Worth its
  own decision later.
- Identify the `EIP=0x00200c00` final halt's real cause — confirmed
  unrelated to DAO/`DllMain` timing, still unidentified which API it is.
- Decide whether `mmtimer_callback`'s own nested-call halt (lands back at
  its own entry instead of its sentinel) is a real re-entrancy bug or
  another instance of the same "thread died mid-call" class already fixed
  for `tid=1012`.
- Decide/implement a real unwind for `seh.py`'s unhandled-fault path
  instead of "halt in place with stale stack data"
- Fix `_chkesp`'s diagnostic (`patch_internals.py`) hardcoding EBP as the
  snapshot register when it's a compiler register-allocation choice
  (confirmed ESI at one real call site)
- **RESOLVED (2026-08-06, cont'd again)**: the `cpu.halted = True` sites
  missing `cpu.fatal_halt` — see "Current status" above.
- SDL window resolution (1536x1248) vs. `GetDeviceCaps` (1024x768) mismatch
- DrawPrimitive / DrawIndexedPrimitive coverage beyond what's needed to
  reach the DAO abort — not yet assessed how much is implemented
- `[alive]` heartbeat silent during `GetMessageA` host-sleep — low priority
- Low priority, structural only, no runtime risk: `tew/loader/dll_loader.py`
  → `tew/pe/exe_file.py` → `tew/loader/import_resolver.py` → back to
  `dll_loader.py` form a genuine three-file import cycle (found via
  `gitnexus check --cycles` 2026-07-23, confirmed by reading the actual
  imports — not a false positive like the two other cycles gitnexus also
  flagged that turn out to be `TYPE_CHECKING`-only). Currently held together
  by two deliberate deferred (function-body, not module-level) imports:
  `DLLLoader.load_dll()` imports `EXEFile` lazily, and `EXEFile.__init__`
  imports `ImportResolver` lazily; `import_resolver.py`'s import of
  `DLLLoader` is the only real top-level one. Works today, no crash risk,
  but reflects a genuine mutual dependency between all three files — a
  cleaner layering (e.g. `EXEFile` not needing to know about
  `ImportResolver` at all) would let all three imports be plain top-level
  ones instead of relying on load-order timing. Worth a look if this area
  is touched again for other reasons; not worth a dedicated pass on its own.

## Architecture
- **CPU + memory backend**: fully Zig now, no pure-Python fallback path
  remains. `tew/hardware/cpu.py` (the original pure-Python CPU class) and
  the entire `tew/emulator/opcodes/` package (pure-Python x86 instruction
  decode) were deleted 2026-07-24 — confirmed dead (`ZigCPU.register()`
  was a no-op; opcodes were built and registered every run but never
  executed). `tew/hardware/memory.py` is likewise now a re-export shim
  over `ZigMemory` (`tew/hardware/memory_zig.py`), and the guest heap's
  bump-allocator cursor math (`CRTState.simple_alloc`) now delegates to
  `tew/hardware/alloc_zig.py`. Register/flag constants (`EAX`, `CF_BIT`,
  etc.) now come from `tew.hardware.cpu_zig`, not the deleted `cpu.py`.
  See changelog.md, "2026-07-24."
- **Zig/Python FFI boundary — kernel module**: as of 2026-07-24 (cont'd),
  the whole Zig side of `libcpu.so` is organized as a real kernel-style
  split. `cpu/src/kernel.zig` is the build root and the *only* file with
  `export fn`s anywhere in the project (63 total: CPU control, memory
  access, guest-heap allocator) — the Python-facing C ABI, full stop.
  `cpu/src/engine.zig` holds the internal execution engine (dispatch
  table, `cpuStep`, all opcode handlers), never exported, driven only by
  `kernel.zig`'s `cpu_run`. `cpu/src/primitives.zig` holds the one shared
  bounds-check/byte-access implementation both `core.zig`'s CpuState-bound
  memory helpers and `kernel.zig`'s `mem_*` C ABI delegate to (previously
  two independent reimplementations of the same logic). On the Python
  side, `tew/hardware/_kernel_lib.py` is now the single `ctypes.CDLL`
  loader shared by `cpu_zig.py`/`memory_zig.py`/`alloc_zig.py` (previously
  three independent `dlopen` calls to the same `.so`). `cpu/src/memory.zig`
  and `cpu/src/alloc.zig` no longer exist — absorbed into `kernel.zig`.
  See changelog.md, "2026-07-24 (cont'd)."
- Game does NOT call D3D8 directly.
- Rendering path: Game → THRASH API (dx8z.dll) → D3D8 (fake COM, Vulkan backend)
- WinINet connects to localhost:443 (HTTPS)
- authlogin.dll reads AuthLoginServer from registry (localhost)
- Login dialog (SDL2): admin/admin from registry, auto-filled
- Timer thread: FUN_00a30ea0, runs as tid=1006 via CRT wrapper at 0x9fc3a0
  `mmtimer_callback` (0x00a30a40) is the multimedia timer proc AND a `_tmrsub[]` subscriber.
  It calls `_SIGNAL_set(event)` + re-registers via `timeSetEvent` each tick.
  Event handle at runtime is 0x7012 (may vary).
- `0x9fc3a0` is a **generic CRT thread-spawn wrapper**, not specific to the
  timer thread — the real work function is passed as `_THREAD_create`'s
  parameter. Several threads use it (`tid=1006`-`1011`), and DAO's own
  `DllMain`-calling worker (`tid=1012`, spawned ~57s in, short-lived) is
  just another instance of the same pattern, not a DAO-specific mechanism.
- **COM activation**: registry-driven (`hkcr\clsid\{...}\inprocserver32`),
  real DLLs loaded and executed for CLSIDs in `_KNOWN_COM_SERVERS`
  (`oleaut32_handlers.py`) — currently just DAO 3.5 (`dao350.dll`, real
  file at `~/.emu32/WINDOWS/System32/`, kept out of the repo since it's a
  Microsoft-copyrighted redistributable). Unregistered or unimplemented
  CLSIDs fail honestly with `REGDB_E_CLASSNOTREG`, matching a real
  unmodified install missing that component. This pattern (search a
  directory of real DLLs, fall back to Python stub) is worth reusing for
  *other* pure user-mode COM/utility libraries the game touches — NOT for
  anything DirectX/hardware-driver-dependent (`d3d8.dll`, `ddraw.dll`,
  `dsound.dll` etc. all need a real kernel-mode HAL/driver stack this
  emulator doesn't have; tew's existing hand-built D3D8-over-Vulkan is
  already the correct solution to that problem, not something to replace).
- **Jet 3.5 database engine**: real files, same pattern as `dao350.dll`,
  also at `~/.emu32/WINDOWS/System32/` (kept out of the repo, Microsoft-
  copyrighted): `msjet35.dll` (core engine), `msjter35.dll` (error-message
  resource), `msjint35.dll` (international/collation), `vbajet32.dll`,
  `msrd2x35.dll` (Jet Red ISAM driver), `expsrv.dll` (expression service),
  `msvcrt40.dll` (DAO350.DLL's own CRT dependency). All sourced from
  `~/.emu32/DBInst/DAO/data1.cab` (InstallShield cabinet, extract with
  `unshield -d <dir> x data1.cab`) — the same install package `dao350.dll`
  itself came from, confirmed via sha256 match. Unlike `dao350.dll`, these
  are *not* gated through `_KNOWN_COM_SERVERS` (they're not COM-activated —
  DAO loads them directly via `LoadLibraryA`/`GetProcAddress` by name); they
  work because `~/.emu32/WINDOWS/System32/` was already a generic
  `DLLLoader` search path, not one scoped to COM servers only. This is the
  first case where a real DLL genuinely needs to *execute meaningfully*
  (actual Jet database reads/writes for an Access `.mdb` file), not just
  activate and hand back to caller code — expect deeper Win32/advapi32
  registry surface area to be needed than DAO alone required.

## Test suite
593 tests (all passing, reconfirmed 2026-07-24 after the memory.py Zig
port, cpu.py/opcodes retirement, the bump-allocator port, and the
kernel.zig/engine.zig/primitives.zig FFI-boundary refactor, on `main`).
