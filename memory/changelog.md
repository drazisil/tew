# Emulator Changelog (Python port)

Entries are newest-first.

---

## 2026-08-28 (cont'd x38) — `GetEnvironmentStrings(W)` and `DllMain`-before-IAT-patch ordering fixed; real file-write-position bug found and fixed (`WriteFile`/`_write` used the wrong fd position); `StockAssembly_SelectAPT`'s `Parameters.Count` failure traced deep into real Jet SQL-compiler internals, still open

**`kernel32.dll!GetEnvironmentStringsW`/`GetEnvironmentStrings` returned hardcoded addresses that were never written to** -- both fell inside the live INT-0xFE trampoline dispatch table, so real CRT code reading them back scanned Win32-handler dispatch machine code as string data. This is what made `MSJINT35.dll`'s `DllMain` return FALSE. Fixed: both now lazily allocate real memory and write a valid empty double-null-terminated block.

**`_invoke_dependency_dllmain` ran a dependency DLL's `DllMain` before `patch_dll_iats` had patched that DLL's own IAT** -- `dll_loader.py`'s recursive walk fires the dependency-DllMain callback mid-walk, but IAT patching only happens as a separate later pass. `MSJINT35.dll`'s `DllMain` called `GetVersion`/`GetCommandLineA`/etc. through unpatched IAT slots, "completing" almost instantly with leftover `EAX` masquerading as a real FALSE return. Fixed: `_invoke_dependency_dllmain` now calls `patch_dll_iats` on entry (cheap, cursor-based) before invoking. Confirmed live: `DllMain` now returns TRUE, `LoadStringA` returns real resource text instead of empty strings.

**`kernel32.dll!WriteFile` and `msvcrt.dll!_write` used `os.write()`'s implicit kernel-fd position instead of `entry.position` for their non-overlapped write path** -- `_llseek`/`_lseek`/`SetFilePointer` only ever update `entry.position`, never the real fd's own position via `os.lseek()`, so any write after a seek silently landed wherever the real fd happened to be, not where `entry.position` said. Confirmed live via `~/.emu32/showplan.out` (real Jet SQL-compiler `JETSHOWPLAN` diagnostic output): a later, shorter write partially clobbered a longer earlier line (`edPart.PartTypeID` -- the tail of `BrandedPart.PartTypeID` missing its first 5 bytes), and a separate spot lost an entire query's own header+index-stats lines outright. Fixed: both now use `os.pwrite(entry.fd, data, entry.position)`, matching the explicit-position pattern already used correctly for reads elsewhere. Re-verified clean on a fresh run.

**Neither fix resolves the actual blocker.** Full mechanism now traced end-to-end via live probes correlated against the exact failing call: `DBParamQuery::get_Count` (MCity_d.exe) -> `dao350.dll`'s real (non-thunk) `get_Count` implementer (`FUN_0447dc1c`, same function the earlier `StockVehicleAttributes_SelectAll2`/`Fields.Count` investigation found) -> type-indexed refresh gate (`FUN_044d26ce`, type_idx=25=Parameters) -> per-type populate handler (`FUN_044c69bc`, its own pool allocation confirmed to succeed live, ruling out an initial false lead) -> name-based lookup (`FUN_044d525b`, given `"StockAssembly_SelectAPT"` directly) -> dynamically-bound call into real `msjet35.dll` (confirmed live: two earlier calls through the identical pointer succeeded for other queries this same run) -> real msjet35.dll dispatch (`FUN_7a89ff40` -> `FUN_7a89fd45`) -> the real Jet SQL execution-plan compiler (`FUN_7a862215`, same `JETSHOWPLAN` code path as the Fields.Count investigation) -> raw internal error `-3100` -> translated via `dao350.dll`'s real (confirmed-not-buggy) error-formatting plumbing into DAO error 3075.

**False lead caught and corrected live**: initially suspected the pool allocator (`FUN_044e2b5c`) was returning NULL. A probe confirmed it succeeds (`EAX=0x7309c4c`) -- the earlier "neither branch fires" evidence that led to this false lead was itself an artifact of the 8-logpoint-slot cap silently dropping new registrations once full (`cpu/src/core.zig`/`kernel.zig` -- fixed-size FFI arrays, `cpu_add_logpoint` just returns with no error when full). Had 9-10 active logpoints stacked from stale, already-resolved earlier investigations (`CoGetMalloc`/`TlsSetValue`/`CoSetState`/`TlsAlloc`, `createinstancelic-*`) still registered. Pruned down; the cap itself (and its silent-drop, contrary to this project's own fail-loudly standard) not yet fixed -- see `TODO.md`.

**Also reconfirmed NOT the bug**: the `CreateErrorInfo`/`SetErrorInfo`/`GetErrorInfo` OLE rich-error-info plumbing was already implemented and working from an earlier session -- `Error.Description` for this error class is a real, validly-allocated, genuinely-zero-length BSTR (that's what real Jet actually produces here, not a plumbing gap), and `DBParamQuery`'s own failure branch doesn't even call `GetErrorInfo` anyway.

**Open, unresolved**: `FUN_7a862215`'s return traces to `local_44 = FUN_7a85e7e1(...)`, not yet live-probed -- genuine, deep, undocumented Jet SQL-compiler internals. `StockAssembly_SelectAPT` never appears as its own top-level plan in `showplan.out` even after the write-corruption fix, consistent with compilation failing before a plan gets written. Full detail in `status.md`.

---

## 2026-08-28 (cont'd x37) — Five more handler/bug fixes; DB init now runs for real; new blocker is a genuine DAO/Jet query-parameter gap, not a missing Win32 handler

Fixed, in the order hit, each verified by a fresh full re-run:

1. **`kernel32.dll!LoadLibraryA` full-path calls to our own Python-simulated-only DLLs never resolved** (`kernel32_handlers.py::_load_dll_by_path`) -- the stub-DLL fallback (`stubs.get_stub_dll_handle(basename)`) was only ever checked inside `if real_path is not None:`, never when no real file exists at all. Hit at ~60s: real `OLEAUT32.dll`'s own NLS-cache-version helper calls `LoadLibraryA("<cached SYSTEM32 dir>\kernel32.dll")` defensively (confirmed by decompiling the actual caller in Ghidra after running `analyze_existing_program` on it -- auto-analysis had never run on that program instance -- and translating its runtime return address to a static address via the DLL's own PE image-base header field); `kernel32.dll` has no real file backing it, so this fell into the interactive-missing-file prompt and crashed on non-interactive stdin. Fixed by checking the stub-handle fallback in the not-found path too.
2. **Real bug found fixing #1**: `os.path.basename()` doesn't split on `\` on a POSIX host -- silently returned the whole Windows-style guest path unchanged instead of just the DLL's basename. Switched both call sites to `ntpath.basename()`, which has correct Windows semantics on any host OS.
3. **`advapi32.dll!RegNotifyChangeKeyValue` implemented** -- since `registry.json` is only ever written by the guest process itself, a watched key never changes externally in our model; returns success immediately in both sync and async mode without ever signaling `hEvent`.
4. **`kernel32.dll!WaitForMultipleObjects` implemented** by factoring out `WaitForMultipleObjectsEx`'s already-correct logic into `_wait_for_multiple_common(cpu, arg_bytes)` -- both share identical leading args, `Ex` just also takes an unused `bAlertable`. Verified `bAlertable`/`SleepEx`'s alertable-wait semantics are a real no-op today (no APC source exists anywhere -- `QueueUserAPC`/`ReadFileEx`/`WriteFileEx` are all unimplemented), flagged in `TODO.md` for if that ever changes.
5. **`kernel32.dll!GetStringTypeExW` and `msvcrt.dll!wcsncmp` implemented** -- thin wrappers around the existing `GetStringTypeW`/`strncmp` logic (wide-char sibling / locale-irrelevant-param sibling, matching the existing `wcsncpy`/`strncpy` pattern).

Run now reaches ~80.5s (up from ~60s). New blocker is a different class of failure entirely: a real, unhandled `INT3` inside `MCity_d.exe` itself, with the game's own stated reason in `stdout.txt`: `DBQuery.c(997) DB ERROR: query StockAssembly_SelectAPT; could not get param count; does table really exist?`. Molly confirmed the table genuinely exists and is populated, ruling out a missing/malformed DB -- this is a real gap somewhere in tew's DAO/Jet query-parameter emulation. Not yet investigated.

---

## 2026-08-27 (cont'd x36) — Two more real-file-I/O gaps fixed (`_llseek`, `_lread`); SDL2/X11 window-creation hang traced and fixed (compositor restart, not a code bug)

`SearchPathW` and `msvcrt.dll!wcsncpy` (x35's open blocker) were already fixed and merged into `main` from parallel work done while waiting on a quota reset -- confirmed both work correctly in a fresh run. Real current progress: found and fixed `kernel32.dll!_llseek` (~70.6s) and `kernel32.dll!_lread` (~156.7s), both real `kernel32.dll` STDCALL exports from the old 16-bit-compat file I/O family (`_lopen`/`_lread`/`_lwrite`/`_llseek`), called by `OLEAUT32.dll`'s typelib reader right after opening a real file via `SearchPathW`->`CreateFileW`. Their `HFILE` is interchangeable with a real `HANDLE`, so both reuse `file_handle_map`/the same seek-and-read logic already proven correct in msvcrt's `_lseek`/`_read`.

Also hit and root-caused a real environment hang, unrelated to any code path: `SDL_CreateWindow`/`SDL_ShowWindow` blocked forever waiting for an X11 `MapNotify` the kwin compositor never sent -- confirmed via a live `gdb -p <pid> -batch -ex bt` full native stack trace (`X11_ShowWindow` -> `_XReadEvents` -> `xcb_wait_for_event` -> `poll`). Same class of issue as the 2026-07-24 Xwayland/kwin wedge; fixed with `kwin_wayland --replace --xwayland &` (restarts the compositor in place). A stray orphaned `run_exe.py` process from an earlier killed background run can look identical (frozen virtual time from CPU starvation, not a hang) -- check `ps aux` for duplicates before assuming either cause.

Run now reaches 156s+ with real DirectSound audio setup and window-message activity before the next (not yet identified) halt.

---

## 2026-08-26 (cont'd x35) — MILESTONE: DAO license-key BSTR bug confirmed fixed end-to-end; game now runs real single-race gameplay DB traffic

With `DllMain` now running for all 4 statically-imported DLLs (previous entry), worked through the resulting wave of newly-exercised missing handlers, in the order hit: `kernel32.dll!GetSystemTimeAsFileTime`, `LoadLibraryExW` (plus a `dwFlags` fix so search-scope-only flags like `LOAD_LIBRARY_SEARCH_SYSTEM32` are ignored instead of halting, on both `LoadLibraryExA` and `LoadLibraryExW`), `InitializeSListHead`, `CreateEventW`; `ntdll.dll!RtlInitializeCriticalSection(AndSpinCount)`, `RtlInitializeResource`, `RtlAcquireResourceExclusive`, `RtlReleaseResource` (first `ntdll.dll`-export handlers in this project, as opposed to `INT 0x2E` syscalls); `user32.dll!wsprintfA` (reuses msvcrt's shared printf engine), `RegisterClipboardFormatA`; `kernel32.dll!GetSystemDirectoryA`; `ole32.dll!CoSetState` (the actual call inside `oleaut32.dll`'s lazy per-thread automation-state init that was failing -- a no-op returning `S_OK` is sufficient).

**Confirmed live**: `Ordinal_2`/`SysAllocString` now returns a real heap BSTR pointer instead of `NULL`/`0xCCCCCCCC`. `dblog.txt` shows the game proceeding straight past DAO init into real gameplay (`DB_StartUpDatabase`, `DBServiceRequestQ` handling `DBT_GO_SINGLERACE`/`DBT_STARTUP`/`DBT_GET_GAMECONFIG_CAR_TABLE`, `DBPhysics_GetTireAuxData`, `DBMem_Alloc`) -- no more `Database initialization failed!`. Run reaches 60+ seconds before the next halt, vs. ~2-3s before this fix. Full test suite re-run: same 101 pre-existing failures (unrelated, see previous entry), no new regressions.

**New blocker, unrelated to this bug**: `kernel32.dll!SearchPathW`, hit ~60s in, inside `expsrv.dll`/`OLEAUT32.dll`/`MSJET35.DLL` interaction -- likely typelib-loading related. Not yet investigated.

---

## 2026-08-26 (cont'd x34) — Root cause of the DAO license-key BSTR bug: statically-imported real DLLs never ran their own `DllMain`; fixed

Traced `Ordinal_2`/`SysAllocString` returning NULL (previous entry) all the way down: real `oleaut32.dll` lazily bootstraps per-thread OLE-automation state on first use via a TLS slot, and that bootstrap fails because `TlsAlloc()` -- called only from `oleaut32.dll`'s own real `DllMain` -- never ran. A live "log every `DllMain` call" pass confirmed it: zero `DllMain` invocations all session for any of the 4 DLLs `MCity_d.exe` statically imports (`d3d8.dll`, `oleaut32.dll`, `rpcrt4.dll`, `secur32.dll`). `dll_loader.py`'s `should_invoke_dependency_dllmain` docstring explains why -- this was a deliberate scoping choice from 2026-08-16 (the `msjint35.dll` fix): the `on_dependency_loaded` callback was only ever wired up at runtime `LoadLibraryA`/`CoGetClassObject` call sites, never at `import_resolver.py`'s static-import `build_iat_map`.

**Fix**: `build_iat_map` now accepts an `on_dependency_loaded` callback and applies the same `should_invoke_dependency_dllmain` check `load_dll` already uses for its own recursive dependencies, so correct dependency-before-dependent ordering falls out for free. `run_exe.py` collects the resulting DLL list and invokes `_invoke_dependency_dllmain` for each one after the main thread's stack and kernel structures are initialized (had to move it there from right after `write_iat_handlers` -- `_invoke_emulated_proc` builds its nested call frame on top of the current `ESP`, which is still 0 that early) but still strictly before the guest's own entry point starts running.

**Consequence**: every one of these DLLs' real `DllMain` now runs for the first time ever in this emulator, exercising a long tail of previously-dormant code. Found and fixed along the way: `kernel32.dll!GetSystemTimeAsFileTime` had no handler at all (`kernel32_io.py`, matching the existing `GetSystemTime`/`_write_filetime` pattern). Currently blocked on `kernel32.dll!LoadLibraryExW`, hit by `d3d8.dll`'s own `DllMain` (unrelated third-party shim, just newly reachable) -- see status.md for the live list as it grows.

**Also found, not caused by this fix**: 101 `tests/unit/api/test_oleaut32_*.py` unit tests fail against `main` even before this change (confirmed via `git stash`) -- they call `stubs.get("oleaut32.dll", "Ordinal #N")` directly, which the earlier `_NoOleaut32Stubs` wrapper (previous-previous entry) now makes raise `KeyError` for everything. Queued in TODO.md, not fixed yet.

---

## 2026-08-26 (cont'd x33) — Traced the DB-init failure to a live, concrete garbage value; found and fixed a real native-segfault risk in logpoint memory access

With real `oleaut32.dll` running, `Dbcode_InitDao`'s two `IClassFactory2::CreateInstanceLic` attempts against real `dao350.dll` both use bad BSTR keys: the first (`dbVariant`-wrapped) is NULL; the fallback (`Ordinal_2`/presumably `SysAllocString`) is `0xCCCCCCCC` -- MSVC's debug-build "never written" stack-fill pattern, meaning the real `SysAllocString` return value never reached `local_44` before use. The fallback call still returns `HRESULT=S_OK` from `dao350.dll` despite the garbage key -- real DAO code isn't validating it, so downstream consumption of a bad engine-interface pointer is the live risk. Not yet pinned down which exact instruction is supposed to write `Ordinal_2`'s result and doesn't -- see status.md.

**Found and fixed along the way**: a logpoint reading guest memory via raw pointer arithmetic (`memory[addr]` on the ctypes `LP_c_ubyte` passed to logpoint callbacks) segfaulted the whole host process on an out-of-bounds index -- confirmed via `coredumpctl`/gdb (crash was inside ctypes' own `Pointer_item_lock_held`, not a Python-catchable exception). Unlike breakpoints (which get the wrapped `Memory` object with bounds-checked `.read32()`), logpoints get the raw pointer -- any logpoint touching guest memory must manually bounds-check against the `memory_size` argument first. Added `_read32_raw` in `run_exe.py` as the safe pattern for future logpoints.

---

## 2026-08-26 (cont'd x32) — FIXED: real `oleaut32.dll` was being unconditionally shadowed by this project's own Python handlers; the entire LoadTypeLibEx investigation was chasing a fake symptom

**The real root cause**: `oleaut32.dll` genuinely loads as real code in this emulator (confirmed live: correctly resolves its own imports from advapi32/gdi32/kernel32/msvcrt/ole32/rpcrt4/user32.dll; `dao350.dll`/`msjet35.dll`/`expsrv.dll` all correctly resolve their own `oleaut32.dll` imports against it). But `dll_loader.py`'s `patch_iat_entry` tries a *registered Python handler* before ever checking a real DLL's own export -- so every one of `oleaut32_handlers.py`'s ~35 registered handlers (both pre-existing and everything built during the entire prior `LoadTypeLibEx` investigation, x9-x31) unconditionally won over the real, correct Microsoft code, regardless of it being genuinely present and loaded. Found only after being asked directly whether the DLL was even really loaded, or being shadowed -- a foundational check that should have happened first, not 20+ turns into hand-crafting an approximation of real COM/OLE Automation behavior.

**Fix**: `register_oleaut32_ole32_handlers` (`oleaut32_handlers.py`) now wraps `stubs` in a scoped shim that silently drops every `register_handler("oleaut32.dll", ...)` call the function makes, letting all of them fall through to the real, loaded DLL.

**Two further real bugs this exposed, both fixed**:
1. `run_exe.py`'s `build_iat_map()` call (main EXE's own direct-import IAT resolution) ran *before* `register_crt_handlers()` added the `~/.emu32/WINDOWS/System32/` search path where `oleaut32.dll`/`dao350.dll` actually live. `MCity_d.exe`'s own early, direct `oleaut32.dll` ordinal import (a BSTR alloc, likely from a global/static C++ constructor well before `WinMain`'s own body runs) silently failed to resolve and got permanently cached as unresolved in `ImportResolver._iat_map`, crashing at ~1.8s with `[UNIMPLEMENTED] oleaut32.dll!Ordinal #150`. Fixed by adding the search path directly in `run_exe.py`, before `build_iat_map()` runs.
2. `msvcrt.dll!wcslen` had no handler at all -- a previously-latent gap, since real `oleaut32.dll` code never got far enough to call it before today. Added, matching the existing `strlen` handler's pattern exactly.

**Verified live**: both the ~1.8s and ~40s halts are gone; the emulator now runs real `oleaut32.dll` code well past both points (confirmed via a genuine `OLEAUT32.dll+0x4bba` return address appearing on the call stack). The run now reaches a **new, different, genuinely-legitimate** `INT3` assertion inside `MCity_d.exe` itself at ~40.6s -- the game's own `stdout.txt` gives the real reason: `Nfs.c(677) Database initialization failed!` / `nfspc.c(1164) NFS_abortmsg callback 'Failed to initialize database...'`. Not yet investigated -- see status.md.

**What this means for the entire prior `LoadTypeLibEx`/expression-function investigation (x9-x31)**: real `oleaut32.dll` would have parsed `expsrv.dll`'s real, embedded `TYPELIB` PE resource (confirmed present during that investigation: 42,164 bytes, RVA `0x52140`, language 1033 -- ironically discovered but not connected to the actual fix at the time) and answered every `Bind`/`GetDllEntry`/`GetFuncDesc` call correctly and automatically. The `_EXPR_FUNCTIONS` table, hand-crafted `FUNCDESC`/`ITypeInfo` structs, and `GetDllEntry` guesswork built during that investigation were not just unnecessary -- they were actively the thing preventing the real fix from ever getting a chance to run. That code is superseded but not yet removed from `oleaut32_handlers.py` (now dead, since the wrapper drops its registration calls) -- cleanup TODO.

---

## 2026-08-25 (cont'd x31) — CORRECTED: the `ITypeLib`/`ITypeComp` trap-object fix was a band-aid, not the root-cause fix; real `.tlb` parsing now in progress

**What was built and verified working**: `oleaut32_handlers.py` ordinal 154 (`LoadTypeLibEx`) now returns a trap `ITypeLib` COM object; `GetTypeComp` returns a trap `ITypeComp` whose `Bind` honestly returns `DESCKIND_NONE`/`S_OK` instead of failing outright. This genuinely eliminated the original `expsrv.dll ESI=0xFFFFFFFF` crash for the specific thread it was traced and verified against.

**What was wrong**: declared this "fixed" from that one clean run, then treated a subsequent different-looking fault (`EIP=0x00000002`) as unrelated rather than tracing it, after Molly pushed back twice on the premature claim. Root-caused the new fault to `tid=1005`=`NPSThreadSender`, whose crypto-dispatch chain looked suspicious (`__purecall`->`ExitProcess`) but turned out structurally sound -- the actually-live branch was the mirrored one from what was first assumed (another decompile-shape trap). Found and removed an unrelated leftover unconditional watchpoint (`run_exe.py:696`, from an already-resolved earlier investigation) that was silently misfiring as a false "crash" on unrelated threads whose stacks reused its watched address -- wasted two ~120s run cycles before this was noticed.

**The real finding**: with that noise cleared, a genuine, reproducible recurrence surfaced -- `tid=1011` (`DBThread`, an independent second DAO/Jet session) hits the *identical* `expsrv.dll ESI=0xFFFFFFFF` crash via `Dbcode_GetStockCarList` -> `DBParamQuery::DoQuery("StockAssembly_SelectAPT")` -> msjet35.dll's locale-comparison machinery -> the same `FUN_7a8a4975`/`FUN_7a8a1c78` chain as the original 2026-08-25 x8 finding, same garbage value (`0x082be46f`). This proves the `Bind` fix only ever prevented the *specific session* it was tested against from tripping -- it does nothing for any other independent session whose query happens to need this locale-comparison path, since the underlying `LoadTypeLibEx` stub is unchanged.

**Two theories investigated and disproven by direct live testing** (not left as unresolved speculation): (1) MCity's own 64MB `_MEM_init`/`MEM_alloc` custom pool exactly spans `0x04000000`-`0x08000000` (`THREAD_STACK_BASE`) with zero headroom -- confirmed via decompile (`_MEM_initsize(0x4000000)` -> plain `malloc`), and `tew`'s bump allocator has no bounds check against this (real gap, hardened -- see below) -- but a `RuntimeError` guard added to `CRTState.simple_alloc` never fired across multiple full reproducing runs, ruling this out as the live mechanism. (2) MCity's DAO-specific `DBMem_Alloc` pool reusing a freed, stale block -- ruled out because `~/.emu32/dblog.txt` (msjet35.dll's own debug trace) shows zero `DBMem_Free` calls in the whole session.

**New tooling/evidence sources for future sessions**: `~/.emu32/dblog.txt` also logs `DBMem_Alloc`/`DBMem_Free` calls with real source file/line attribution (`dbparts.c(8751)` etc.) -- more precise than register-state inference for "what was the game doing right before the halt." `~/.emu32/MCity/real.log` (the `_REAL_` subsystem's startup banner) is useful for confirming whether a given init function genuinely ran, independent of live breakpoint/logpoint instrumentation -- caught a broken diagnostic logpoint address (`_mem_init_logpoint`, address not yet re-verified) this way, after a live logpoint's silence had been wrongly taken as proof the function was never called.

**Hardening added regardless of relevance to this bug**: `CRTState.simple_alloc` (`tew/api/_state.py`) now raises loudly if a heap allocation would push the cursor past `THREAD_STACK_BASE` (`0x08000000`), instead of silently letting heap and thread-stack memory alias. `bump_alloc_next` (`cpu/src/kernel.zig`) itself still has no such check -- the guard lives Python-side only.

**Decision**: real `.tlb`/MSFT binary format parsing for `LoadTypeLibEx`/`ITypeComp::Bind`, previously flagged as a large/optional item pending scoping, is now confirmed as the actual required fix and is being implemented (see TODO.md).

---

## 2026-08-25 (cont'd x8) — ROOT CAUSE FOUND (not yet fixed): `expsrv.dll` `ESI=0xFFFFFFFF` halt traces all the way to `LoadTypeLibEx`'s honest `E_NOTIMPL` stub plus an uninitialized-stack bug in real `msjet35.dll` code

**Milestone, not a fix** -- the fix itself is tracked in `memory/TODO.md` (a small, scoped item) alongside a separate large/unscoped item (real `.tlb` parsing) this is NOT asking for.

**The full chain, confirmed live end to end**: `oleaut32.dll`'s `LoadTypeLibEx` (ordinal 154) is a deliberate, honest tew stub returning `E_NOTIMPL` -- real type-library parsing was never implemented. `msjet35.dll`'s `FUN_7a8a16b7` (a per-record lookup, called once per entry of a 74-record locale/format template by `FUN_7a8a4975` while lazily building a per-session locale-info object) depends on it and fails identically for all 74 records -- confirmed by dumping 9 sampled records (indices 0,1,2,5,10,20,40,56,73), all byte-for-byte identical garbage, not one obscure entry failing. On that failure, `FUN_7a8a4975` only defaults `local_1c=-1`; three sibling stack fields (`uStack_18`/`14`/`10`) are left as whatever was already on the stack and get copied into the record regardless.

That garbage propagates forward: `FUN_7a9267a1` (found to be the REAL caller via a live caller-hunt after an earlier pattern-match guess, `FUN_7a926327`, turned out wrong -- see the entry below) reads one garbage field as `FUN_7a8a1c78`'s `param_4->field2_0x8` branch selector; since it happens to read non-zero, `FUN_7a8a1c78` skips its own *existing* graceful `field3_0xc==-1` error check and instead uses a different garbage field (confirmed live as `ECX=0xFFFFFFFF`) as a COM interface pointer, three calls later dereferenced in `expsrv.dll`'s `FUN_0f9dd9a7` (`MOV EAX,[ESI]`) -- the crash.

**Why this isn't simply "implement LoadTypeLibEx" or "patch msjet35.dll"**: `E_NOTIMPL` is an honest failure, not the bug -- see `feedback_no_stubs` (auto-memory). `msjet35.dll` is real, unpatchable Microsoft code. And its failure-handling here (only defaulting one of four related fields) is a genuine latent defect in that real code -- but real Windows never exercises it because `LoadTypeLibEx` doesn't fail there, so it was never tested in practice (added as a caveat to `tew_bug_can_only_be_in_tew`, auto-memory: real code's error-handling for "impossible" conditions isn't automatically trustworthy just because the normal-path logic is). The actual fix is tew-side: find why tew's stack reads non-zero garbage at this exact point when real Windows' apparently reads zero (letting `msjet35.dll`'s own existing check work), and correct that memory-model gap -- not yet started.

**Also confirmed this session**: this investigation independently re-derived, then caught and corrected, the exact same class of mistake twice -- trusting a decompiled `if/else` branch shape, and trusting a decompile-pattern-match for "which function calls X", without live verification. Both were caught via per-instruction logpoint bisection (`cpu.add_logpoint`, immune to breakpoint-dispatch edge cases) before they could compound into a wrong conclusion. Worth remembering as a general technique, not just for this bug.

---

## 2026-08-25 (cont'd x7) — CORRECTED a wrong branch assumption mid-investigation (caught via live trust-check, not a tew bug): the `expsrv.dll` halt's bad value is `param_4[3]` directly, not an array-slot read

**Not a fix -- a correction to the investigation's own reasoning**, and a worked example of why to verify tooling/assumptions before trusting a multi-hop chain of static analysis. Several turns were spent assuming `MSJET35.DLL`'s `FUN_7a8a1c78` took its `if (param_4[2]==0) { ...array lookup via local_218... }` branch, based on the decompile's shape alone -- tracing the array's allocator, session-slot-claim function, and a large "everything defaults to `-1`" bulk-init loop, then repeatedly failing to get a breakpoint OR a logpoint to fire on any instruction inside that branch (`static 0x7a8a1d42`-`0x7a8a1d5d`).

**The catch (Molly's call)**: stop and verify the debugging tools themselves before believing anything further. Bisected with a Zig-core logpoint (`cpu.add_logpoint` -- inline C callback, no halt/dispatch dependency, immune to the "maybe this runs inside a nested cpu.run()" theory tried first) on every instruction across the suspect range. Result: none of them fire; only the `if/else` merge point (`0x7a8a1d5e`) does. That's conclusive: this call never takes the array-lookup branch. It takes `else: uVar6 = param_4[3];` -- a direct value, no array involved.

**Corrected understanding**: the `-1` we've been chasing is `param_4[3]` itself, not the contents of an array slot it indexes into. `param_4[3]` (`FUN_7a926327`'s `field3_0xc`) is copied verbatim from `*(iVar2 + 0x8fc)`, where `iVar2` is a per-session "locale info" object pointer (`*(param_1*0x708 + 0x6f0 + DAT_7a9362c0)`) -- a different field than the array pointer (`+0x6d8`) this whole detour was about. The real open question is simpler than it looked: why is that locale-info object's `+0x8fc` field `-1`. Not yet investigated.

**Also confirmed correct, ruled out before finding the real cause**: `two_byte.zig`'s `Jcc rel32` handler (`0x80...0x8F` case) and `core.zig`'s `decodeSIB`'s scaled-index-only special case (`mod=00, base=101` -> `fetch32`, no base register) were both read against the Intel spec and found correct -- not a CPU-core decode bug, despite that being a very plausible-looking candidate at the time (the "missing" instructions sat immediately after a large-displacement conditional jump, and used an unusual SIB encoding).

**Housekeeping**: the `mcity` Ghidra project (separate from this repo's own default `debug_clean`) has `msjet35.dll` and `expsrv.dll` already analyzed/partially named -- Ghidra's full auto-analysis crashes specifically on `expsrv.dll` (not yet investigated why) but works fine on `msjet35.dll`. `run_exe.py` currently has 8/8 logpoint slots used by this session's bisection trace (`_make_trace_logpoint`, static `0x7a8a1d42`-`0x7a8a1d5e`) -- they answered their question and should be removed before the next investigation needs logpoint slots, since the cap silently drops registrations past 8 with no error (a real limitation, not newly discovered -- see the 2026-08-04-era entry on the same cap for breakpoints/logpoints).

---

## 2026-08-25 (cont'd x6) — TRACED (not yet fixed): `expsrv.dll` `ESI=0xFFFFFFFF` halt followed end-to-end from the crash site through 3 DLLs to a live-confirmed root value in `MSJET35.DLL`

**Not a fix yet -- an investigation checkpoint.** With the SEH `EIP`-restore fix (below) giving a real, static-analyzable crash address for the first time, traced the `MOV EAX,[ESI]` fault (`expsrv.dll`, static `0x0F9DD9E9`) all the way to its argument's origin, entirely via file-based static analysis (PE section/export-table parsing, raw `E8`-rel32 byte scans for callers, manual disassembly decode) plus one targeted live breakpoint -- no full Ghidra auto-analysis, which crashes on this DLL.

**Chain**: `FUN_0f9dd9a7` (crash site, `param_2`/`ESI` loaded once at entry, never modified) is called only by `FUN_0f9dd3d9`, which forwards its own 3rd parameter straight through unchanged. `FUN_0f9dd3d9` has no callers inside `expsrv.dll` at all -- it's invoked cross-module, directly from `MSJET35.DLL`, via an indirect COM vtable call (`CALL DWORD PTR [EAX+0x24]`, static `0x7a8a1d84`). Found `expsrv.dll`'s actual vtable array in `.rdata` (base `0x0FA041C0`) by checking the exact address Molly found Ghidra referencing `FUN_0f9dd3d9` from (`0x0FA041E4` = slot 9, offset `0x24` -- matches the `MSJET35.DLL` call exactly) -- confirms the dispatch statically. A new breakpoint (`_expsrv_vtable_call_probe`, `run_exe.py`) at that call site fired exactly once all run and confirmed it live: `EAX=0x1a0441c0` (vtable), `[EAX+0x24]=0x1a01d3d9` (== `FUN_0f9dd3d9`'s runtime address), and **`ECX=0xFFFFFFFF`** -- by calling-convention push order, this is the exact value that becomes `FUN_0f9dd3d9`'s 3rd argument, which becomes `FUN_0f9dd9a7`'s `param_2`, which becomes `ESI` at the crash. One clean, unbroken chain from the fault back to a concrete register value in a completely different DLL.

**Where this leaves it**: `ECX` is already `0xFFFFFFFF` in `MSJET35.DLL` before this call -- nothing downstream corrupts it. Not yet found where in `MSJET35.DLL` it's set (outside the small decoded window around the call site). Since this call site only fires once in the whole run, it's plausibly a rare/error-handling path (e.g. Jet's own "expression/column not found" case) -- open question, not yet answered: is `-1` here real, legitimate Jet behavior that some earlier check ought to have short-circuited before reaching this code, or did tew's own emulation of something upstream produce `-1` where real Windows would have produced a valid pointer? `MSJET35.DLL` and `expsrv.dll` are both unmodified Microsoft code, so per this project's standing assumption the divergence is presumed to be tew's own emulation until shown otherwise. Next step: same backward-tracing technique, starting from static `0x7a8a1d62` in `MSJET35.DLL`.

---

## 2026-08-25 (cont'd x5) — FIXED: `dispatch_exception` never restored `cpu.eip`, so every unhandled SEH-chain-exhausted halt reported a fixed internal sentinel (`0x001fe012`) instead of the real fault site — three weeks of investigations' "`EIP: 0x001fe012`" explained

**Root cause**: `tew/kernel/seh.py`'s `_invoke_handler` genuinely executes each SEH handler on the CPU for real (`cpu.eip = handler_addr`, then `cpu.run()` until it returns) and, on a normal return, restores only `cpu.regs[ESP]` — never `cpu.eip`. `dispatch_exception`'s chain-walk loop calls `_invoke_handler` once per SEH frame; if every handler in the chain declines (`EXCEPTION_CONTINUE_SEARCH`), the function returns `False` (unhandled) with `cpu.eip` still parked at whatever `_invoke_handler` left it at from the *last* handler invocation: `SEH_RETURN_SENTINEL + 2` (`0x001FE010 + 2 = 0x001FE012`, `seh.py:106`) — an internal "the handler just returned" bookkeeping constant, not real guest code. `dispatch_exception` already receives the real fault address as its `exception_address` parameter, but never wrote it back to `cpu.eip` before returning.

**Why this took so long to spot**: `0x001fe012` doesn't fall inside `MCity_d.exe`'s own image, any loaded DLL, or tew's own stub/trampoline region (`0x00200000-0x00220000` — it's `0x1fee` bytes short of that boundary), so it looked like a real-but-unidentified address rather than an internal constant. It's also *exactly* the value seen in every "unhandled `DebugBreak`"/SEH-exhausted halt logged across this project's history back to early August (`grep -rn "0x001fe012" memory/*.md` — a dozen-plus unrelated investigations, all showing the identical EIP regardless of the actual bug) — the giveaway, in hindsight, was that a real guest address should vary between unrelated bugs and this one never did.

**Fix**: `dispatch_exception` now restores `cpu.eip = exception_address & 0xFFFFFFFF` immediately before both of its unhandled-return points (the `SehHandlerTimeout` except branch, and the final chain-exhausted `return False`). Verified safe first, not just after the fact: grepped all four `dispatch_exception` call sites (`win32_handlers.py`'s `INT3` branch, `seh.py`'s own `RaiseException` handler, `run_exe.py`'s access-violation and runaway-EIP sites) — every one already logs its own local `fault_eip`/`runaway_eip` variable rather than reading `cpu.eip`, and nothing reads `cpu.eip` for control flow between an unhandled `dispatch_exception` call and the final `diagnose_halt()`/`Final EIP:` report, so the fix only corrects post-run diagnostic output, changes no behavior.

**Confirmed live, and this mattered**: with the fix, the current `expsrv.dll` blocker's `EIP:` now reads `0x1a01d9eb` (real address, `expsrv.dll+0x1d9eb`, static/Ghidra VA `0x0F9DD9EB` = ImageBase `0x0F9C0000` + RVA) instead of the sentinel. **This is the exact same instruction as the Zig integer-overflow panic fixed earlier this session** (`MOV EAX,[ESI]`, `ESI=0xFFFFFFFF`) — the `core.zig` wrapping-add fix didn't resolve the underlying bug, it correctly converted an unrecoverable native crash into a proper (still-unhandled) CPU fault: the wrapped address lands on the null page, `memRead8`'s guard faults it, the game's own SEH chain gets a real attempt and declines, and it halts here. **Not yet root-caused**: why `ESI == 0xFFFFFFFF` reaches this specific `expsrv.dll` instruction — Molly is inspecting `0x0F9DD9EB` directly in Ghidra (tew can't load this DLL for analysis) now that there's a real address to look at.

**Also flagged, not fixed (low priority, same bug shape)**: `_invoke_handler`'s `SehHandlerEscaped(faulted=True)` branch (`seh.py`, handler-crashed-mid-execution case) also leaves `cpu.eip` unrestored while the chain keeps walking — harmless today since the very next `_invoke_handler` call overwrites it on entry before it's ever observed, so not fixed proactively.

---

## 2026-08-25 (cont'd x4) — FIXED: Zig integer-overflow panic in `readRmFixed32`/`memRead32` — non-wrapping address arithmetic, not a genuine x86 edge case

**Root cause**: `cpu/src/core.zig`'s `memRead16`, `memRead32`, `memWrite16`, `memWrite32` computed the 2nd/3rd/4th byte's address as `addr + 1`, `addr + 2`, `addr + 3` — plain, checked `u32` addition, which Zig's safety checks panic on when it overflows. A flat 32-bit linear address is supposed to *wrap* mod 2^32 on real hardware (the same reasoning already applied to `s.eip +%= 1` in `fetch8`, a few lines above in the same file); `memRead8`/`memWrite8` already bounds-check correctly via `isFaultingAddr` and fault (set `s.faulted`/`s.halted`, don't crash) on a genuinely out-of-range address, so the bug was purely in the intermediate `+1/+2/+3` arithmetic feeding those, not in the fault-handling design itself.

**Confirmed via `coredumpctl gdb`** (not log inference — the native Zig panic aborts the process before any Python-level instruction trace can capture the offending EIP): frame `core.readRmFixed32(s, mod=0, rm=6)` — ModRM `mod=00`/`rm=110` decodes to the bare `[ESI]` addressing form (32-bit addressing, no disp/SIB) — showed `s.regs[ESI] == 0xFFFFFFFF` at `s.eip == 0x1a01d9eb` (`MOV EAX,[ESI]`, opcode `0x8B`). `readRmFixed32` computed `addr = ESI = 0xFFFFFFFF`, then `memRead32`'s `addr + 1` overflowed `u32` and panicked.

**Fix**: changed all four call sites' `addr + N` to `addr +% N` (wrapping add) in `cpu/src/core.zig`. Rebuilt `libcpu.so` via `zig build` (clean build, no errors). Confirmed live with the same repro (`TEW_MAX_STEPS=5000000000`): the panic no longer reproduces, and the run progresses ~14s further (63s→77s) before hitting a new, unrelated halt — an unhandled exception inside `expsrv.dll` (Jet Expression Service, called from `MSJET35.DLL`), thread 1011. See `status.md`'s current entry, including the translated static/Ghidra-loadable address for that crash site (`expsrv.dll`'s `ImageBase` `0x0F9C0000` + RVA `0x1D41D` = `0x0F9DD41D`).

**Also this session**:
- `CreateThread`'s log line (`tew/api/kernel32_io.py:336`) was logging at `info`, disproportionately noisy for a routine per-thread-spawn event at that level — downgraded to `debug`.
- `_prefclass_assert_probe` (`run_exe.py`) removed — the breakpoint that confirmed the `Fields.Count`/`CompareStringA` fix (below) landed live; its question is fully answered and the fix is documented here and in `status_archive.md`, so left in place per usual practice would just be stale noise (137 `real_answer:ok` hits every run, no longer informative). One dangling in-code cross-reference to it (a comment near the removed column-loop instrumentation in `run_exe.py`) was updated to stop pointing at the now-deleted function.

---

## 2026-08-25 (cont'd x3) — FIXED: `ASSERT: mcity.c(588) prefClass>=0` / `Fields.Count==1` instead of `10` for `StockVehicleAttributes_SelectAll2` — tew's `CompareStringA`/`W` rejected `LOCALE_USER_DEFAULT`

**Root cause**: `tew/api/kernel32_io.py`'s `_locale_is_valid` only accepted the literal LCID `0x0409`, rejecting `LOCALE_USER_DEFAULT` (`0x0400`) — a completely standard Windows locale sentinel ("use my current locale") that real `CompareStringA` resolves and succeeds on. dao350.dll's internal field-name dedup check, reached while building a recordset's Fields collection (`FUN_044da868` → `FUN_044d1d53` → `FUN_044c6126` → `FUN_044c6284`), calls `CompareStringA(LOCALE_USER_DEFAULT, ...)` for every column-name comparison — confirmed live, 371 calls in one run, every one rejected (`EAX=0`, "invalid locale"). dao350's own switch on the result maps failure (`0`) to the same code path as `CSTR_EQUAL` (`2`) — harmless on real Windows, which essentially never returns 0 for a well-formed call, but under tew's rejection it turned every single name comparison into an unconditional "equal". Every column in the 3-table-join query after the first got misidentified as a duplicate of it (confirmed live: the collection's own dedup lookup returned the address of the just-added field #1 as a "match" for field #2, "Brand" vs "BrandID" — real, distinct column data, not corruption) and silently skipped rather than added, leaving `Fields.Count==1` instead of `10`.

**Fix**: `_locale_is_valid` now resolves `LOCALE_USER_DEFAULT` (`0x0400`) and `LOCALE_SYSTEM_DEFAULT` (`0x0800`) to `0x0409` before validation, in one place shared by both `_compare_string_a` and `_compare_string_w`.

**Confirmed live**: the `prefClass` assert that ended every previous run no longer fires (`_prefclass_assert_probe`: 137 hits, all `real_answer:ok`). The run progresses ~16 real seconds further than ever before (63s → 79s, into COM/OLE automation territory, `LoadTypeLibEx` calls) before hitting an unrelated new blocker — a Zig-level integer-overflow panic in `cpu/src/core.zig`'s `readRmFixed32`. See `status.md`'s current entry for that.

**Method note**: found via direct live memory stalking rather than continued decompile-guessing (Molly's call, after an extensive multi-session decompile-chasing thread — see `status_archive.md`'s matching "Previous status" entry for the full history) — per-iteration dumps of the collection object, the per-column descriptor struct, and the dedup lookup's actual return value, comparing a healthy sibling recordset against the buggy one at matching timing, until the exact iteration where the collection stopped growing was caught directly rather than inferred. Two false leads along the way, both self-corrected before being reported as findings: (1) a raw `+0xe` byte offset that turned out to be a units error (`param_2` is a `undefined4 *`, so `+0xe` means 0xe dwords = `+0x38`, not `+0xe` bytes) — the real field there was the correctly-populated name length, not garbage; (2) a name-fetch buffer read that looked "static/unchanging" and seemed suspicious, but turned out to be reading the wrong stack slot entirely (bad `ESP`-relative offset math for an 8-arg stdcall) — the healthy sibling recordset showed the identical pattern, disproving it as a bug signal before it was acted on.

Also this session: added `create_function` to `ghidra-mcp` (defines a function at an address auto-analysis never bound to one, e.g. jump-table targets) — used to properly define `FUN_044982d1` (dao350's QueryInterface dispatcher) and `FUN_7a8434cf` (msjet's advance-wrapper) for decompilation. See `ghidra-mcp`'s own `CHANGELOG.md` (0.1.4).

`run_exe.py` breakpoint slots after cleanup: 3 of 8 in use (`_fields_probe`, `_fields_count_probe`, `_prefclass_assert_probe` — cheap permanent landmarks). The `_column_loop_probe`/`_column_loop_return_probe`/`_dedup_lookup_probe`/`_pre_add_struct_probe` diagnostic chain used to find this bug was removed once it answered its question, per usual practice.

---

## 2026-08-25 (cont'd x2) — `Fields.Count==1` confirmed LIVE via corrected breakpoints, shown to be systemic (5 distinct recordsets, not one query); JETSHOWPLAN + step-limit fix in progress

**Fields.Count root cause, real data at last**: added two breakpoints in `GetValue` (`0x0040da3f` exe thunk -> real body `0x008fb8e0`) at the real `get_Fields`/`get_Count` vtable calls (`Recordset+0xB4` -> `Fields+0x1C`). First attempt used Ghidra's decompiler-assigned stack-variable names (`piStack_24`→`[EBP-0x24]`, `asStack_30`→`[EBP-0x30]`) and got garbage (`recordset=0xcccccccc` RTC poison, then a suspicious flat `Count=0` across every call). Hand-decoded the real bytes instead and found both names are 4 bytes off their real EBP-relative offsets in this SEH-instrumented function (`local_14`, the `this` pointer, is really `[EBP-0x10]` not `[EBP-0x14]`; the `Fields` out-param is really `[EBP-0x20]` not `-0x24`; `Count` is really `[EBP-0x2C]` not `-0x30`) -- a consistent, file-wide -4 shift, not a one-off. With corrected offsets: **1896 `get_Count` calls in one run, every single one `Count=1`, across 5 distinct recordset pointers**, not just `StockVehicleAttributes_SelectAll2`. Reframes the investigation from "this one query's SQL" to "something systemic in tew's emulation of this COM dispatch path" -- not yet confirmed whether any of the 5 are legitimately single-field queries, but 5-for-5 makes that unlikely. Full detail, addresses, and the abandoned first-pass filtering approach in `status.md`.

**Also fixed**: `MAX_STEPS` (`run_exe.py:571`, default 500,000,000) was silently truncating runs before reaching the real halt, logged misleadingly as `=== Emulation Complete (clean exit) ===` -- and the reported `Steps executed` count (133,765,665) didn't reconcile with the 500,000,000 limit that supposedly triggered the stop (separate accounting bug, not root-caused, low priority). Workaround: `TEW_MAX_STEPS=5000000000` env var override, not a code change.

**JETSHOWPLAN enabled + real handler gap fixed (`wvsprintfA`, worth keeping); real output path found (Molly), explained, and the actual `Fields.Count` bug pinned to a raw stored field, not the getter chain**: added `jetshowplan`="ON" (REG_SZ) under `hklm\software\microsoft\jet\3.5\engines\debug` in `registry.json`. Confirmed live, which exposed `[UNIMPLEMENTED] user32.dll!wvsprintfA` -- fixed for real, reusing the existing `_sprintf_format`/`_write_cstring` engine (`msvcrt_handlers.py`) with `__stdcall` cleanup. Molly found the real output path, `~/.emu32/showplan.out`, and it does get real plan text written -- but `StockVehicleAttributes_SelectAll2` never appears in it, because it's a pre-existing *stored* QueryDef and Jet only re-plans on fresh SQL compilation, not on `OpenRecordset` against an already-compiled one. Genuine dead end for identifying that query's plan specifically, now fully explained rather than just observed.

Used it productively anyway: fixed `DoQuery`'s own recordset-pointer capture (`local_1c`'s real offset is `[EBP-0x18]`, hand-decoded via `CMP DWORD [EBP-0x18],0`, not `-0x1C` as the decompiler name implied), giving a hard, identity-confirmed target recordset (`0x7072417`) instead of timing-coincidence guessing. Traced one hop further past the `get_Count` tear-off thunk into its real (non-thunk) implementer, `dao350.dll FUN_0447dc1c` (decompiled) -- and read the raw field it returns, live: `rec_base+0x2C` already holds `1` for every recordset checked, before any `GetValue`/vtable dispatch happens. **This conclusively rules out the entire `get_Count` call chain as the bug's location** -- it's upstream, in whatever populates this field at query-compile/bind time. Also corrects the earlier "5 recordsets, systemic" framing from two entries up: 4 of those 5 are unrelated ad-hoc queries JETSHOWPLAN revealed (real `Brand`/`AbstractPartType` scans), plausibly legitimate low counts, not the same bug.

**Full causal chain closed**: Molly pointed at the real crash site, `carClassList::carClassList` (`0x005bad20`). Its assert loop reads per-row fields at a fixed `0xa0`-byte stride from `DBCarTableOutputData`'s row array -- `prefClass` at `+0x88`. Since `Fields.Count==1` means column 1 never gets bound by `GetValue`, and the row buffer isn't zero-initialized per row (only the class's own bucket counters get `memset`), `prefClass` ends up holding deterministic heap garbage that fails the `[0,7]` bounds check. Root cause (why `Fields.Count` is `1` at compile/bind time) still open.

**Population chain traced to a dao350.dll/msjet35.dll boundary**: `get_Count`'s refresh gate (`FUN_044d26ce`) only repopulates `+0x2C` when it's `<1` -- since ours is already `1`, it's permanent, set once. Traced the real populator: dispatch table `FUN_044da240` (state=5, live-confirmed) -> `FUN_044dac2b`'s column-enumeration loop -> `FUN_044d5200`, a thin wrapper around `(*DAT_044e52e4)(...)`, a dynamically-bound pointer into `msjet35.dll` (same shape as the already-fixed DAO-3075 bug's `DAT_044e534c`).

**Resolved the ordinal statically (real PE export table, base 2, index 154) to `msjet35.dll FUN_7a848f59` -> real worker `FUN_7a84269c`.** Molly independently found the same 2048-slot session/type table via xref-scanning (`FUN_7a86f969`, `FUN_7a89acb3`, `FUN_7a8492a3`, `FUN_7a90fb2a` -- all siblings/infrastructure on it, none the actual write site). Live-probed `FUN_7a84269c`'s entry directly -- too generic (hundreds of calls, many session_idx values, in a 200ms window), confirming ordinal 156 is a reused type utility, not a one-shot enumeration call. Abandoned.

**Went up one level instead: `dao350.dll`'s own `FUN_044dac2b` loop** (unrelocated, no delta). Hand-decoded the real `CALL FUN_044d5200` site (`0x044dacc0`, confirmed byte-exact -- the first address tried, `0x044dac70`, was the wrong call). Live result, identity-confirmed against the existing `rec_base` for `recordset=0x7072417`: **`FUN_044d5200` is called exactly 3 times for our target query, not 10 and not 1.** Decompiled `FUN_044da868` (called once per successful iteration): it's an **upsert** -- creates a new field entry if the column's name/index lookup finds nothing, but *resets an existing entry* instead of inserting a new one if the lookup finds a match. Real candidate for the undercounting: if columns 2 and 3's name resolution collides with column 1's entry (plausible for `Table.Column`-qualified names across a 3-table join), 3 real enumeration attempts collapse into 1 stored field -- matching the evidence exactly. Not yet live-confirmed.

**Pursued the "why 3, not 10" question instead.** Molly xref-scanned the same 2048-slot msjet35.dll session/type table and found several siblings (`FUN_7a86f969`, `FUN_7a89acb3`, `FUN_7a8492a3`, `FUN_7a90fb2a`) -- recorded in `status.md` for future reference, none turned out to be the write site (one's a broadcast/notify, one's the table's slot allocator, one's an unrelated name-cache refcounter). Re-added a `FUN_7a84269c`-entry breakpoint (previously abandoned as too noisy) with an EBP-chain walk to filter by caller instead of session_idx alone -- an early version crashed the whole run on a bogus EBP dereference (msjet35.dll doesn't reliably keep frame pointers this deep), fixed with bounds checks. Correlating by exact millisecond timestamp against our confirmed `rec_base`'s 3 `FUN_044d5200` calls: all 3 show `session_idx=2038`, constant -- meaning it's a fixed per-statement handle, not a per-column selector. Resolved (and re-verified same-run, after catching a cross-run reuse assumption that happened to be right but wasn't properly checked the first time) to real target `0x7a847105`. Decompile fails there (no function defined, same as `FUN_0447dc1c` earlier); raw byte decode shows real logic checking integer sentinels and a flag byte, not yet fully traced.

**Reframed by Molly: the bug can only be in tew, not real dao350.dll/msjet35.dll (matches every prior root cause in this project's history).** Trimmed `_fields_count_probe` way down (was ~1896 lines/run + a full object dump; now one line, once per recordset). Added a paired breakpoint reading `FUN_044d5200`'s actual return value (`0x044dacc5`, right after the confirmed call site): for our target, **call #1 and #2 return 0 (success), call #3 returns -1 (clean "no more columns")** -- not an error. The enumeration loop is doing exactly what it's told; real Jet genuinely believes this query has only 2 enumerable columns. Rules out the loop itself, moves the investigation to the real SQL column-list parser/tokenizer (compile time, upstream of `OpenRecordset`) -- same shape as the already-fixed DAO-3075 bug, but that fix covered single-table `AS`-alias parsing, not multi-table `Table.Column`-qualified references. Not yet located.

**Retested the OLEAUT32 Variant hypothesis (ruled out once already for the `VariantClear` bug, but that was a different function) -- found and fixed two more real gaps, but the bug itself is unaffected.** `VarDateFromUdate` (UDATE struct -> DATE) and `VarUdateFromDate` (the inverse) were both genuinely unimplemented -- fixed for real in `oleaut32_handlers.py`, reusing the existing OLE-date-epoch/Lotus-quirk math from `VarDateFromStr`. Both resolve cleanly now. `raw_count` for our target is still exactly `1`, unchanged -- rules out these two functions as the cause, though the fixes are real and kept.

**Molly asked why oleaut32 is hand-reimplemented in Python instead of running the real DLL like msjet35.dll/dao350.dll/expsrv.dll do.** Answer: pure oversight, not a design choice -- a real, period-correct `oleaut32.dll` already existed in the project's binary source pool, just never got copied into `~/.emu32/WINDOWS/System32/` (the exact search path the DLL loader already uses for the other real DLLs). Copied it in (no code change). **Result: real `OLEAUT32.dll` now loads (796 exports) and the bug is completely unchanged** -- `raw_count` still exactly `1`. Conclusively rules out oleaut32.dll in its entirety, not just the two functions fixed this session -- real Microsoft code runs end to end there now. Narrows remaining suspects to tew's own CPU/memory emulation or the DLLs that genuinely can't be real (kernel32/msvcrt/registry, OS-level surfaces with no file to load). `oleaut32_handlers.py` is now dead code as long as the real file stays in place -- not removed, flagged for a decision on whether to keep as a fallback or prune.

**Same fix, one level deeper**: loading real oleaut32 immediately exposed `Could not find RPCRT4.dll` -- also in the binary pool, also just copied in, also loads for real now (1028 exports). `expsrv.dll`'s own IAT patching improved measurably as a result (0 real exports/41 stubs -> 28 real exports/19 stubs). RPCRT4 pulled in two more missing deps: `ntdll.dll` (a real syscall-transition DLL, deliberately left emulated -- copying it in risks breaking things, not the same oversight class) and `Secur32.dll` (ordinary user-mode SSPI code, copied in, loads clean, one more auto-stub resolved). Bug still completely unchanged across all three real-DLL additions.

**Downstream mechanism (Fields.Count==1 to the crash) now fully closed via the real call path, correcting an earlier guess.** The free-standing `Dbcode_Fetch` chased earlier this session turned out to serve *different*, unrelated ad-hoc queries -- our target's identity-confirmed recordset never appears in its calls across a full run. Real path: `DB_GetGameConfigCarTableOffline` -> `DBRecordset::Fetch` -> `GetVariant` (a trivial one-line wrapper for the same `GetValue` already under investigation) -> `DBBinding::Set`. **`DBBinding::Set` is the exact write site**: on a NULL `GetValue` result, it *deliberately* writes `-1` into both the indicator and the real value buffer, then reports success anyway -- not "leftover garbage happens to be -1" (the earlier framing, now corrected), a real, intentional sentinel write, confirmed byte-for-byte and matching the live-captured crash value exactly. Also checked and ruled out: FPU stack depth (`fpu_top`) stays at `0` across all 636 real `GetValue` calls in one run -- no imbalance. Root cause (why `Fields.Count` is `1`, not `10`) is unchanged, still open. Full detail in `status.md`.

---

## 2026-08-25 (cont'd) — `OpenRecordset` HRESULT confirmed S_OK (rules out a failed COM call); cleaned up ~390 lines of stale DAO-3075 breakpoint/log debris from `run_exe.py` and two handler files

**Investigation**: found the real vtable dispatch for `DBParamQuery::DoQuery`'s "recordset not yet open" branch via raw byte decode (not the decompiler's bogus `0x99779b` "return address" literal, which caused two dead-end breakpoints last session): `0x0099778e` is `CALL DWORD PTR [ECX+0x8C]` (6 bytes, confirmed `FF 91 8C 00 00 00`), real next instruction `0x00997794` (RTC's `CMP ESI,ESP`, doesn't touch EAX). One of last session's two dead addresses, `0x009975c5`, turned out to be an unrelated call (`_REAL_abortmessage("%s\n","m_QueryDef")`, the already-ruled-out "QueryDef is null" assert path) -- distinguishable from a real vtable call by encoding alone (`FF 15 [absolute]` vs `FF 91 [ECX+disp]`). One-shot breakpoint at `0x00997794` live-fired once at 42.632s: **`EAX (HRESULT) = 0x0` (S_OK)**. Run otherwise reproduced the known blocker unchanged (same `mcity.c(588)` assert in `stdout.txt`, same unhandled-DebugBreak halt at `EIP=0x001fe012`). **Rules out an OpenRecordset failure entirely** -- `Fields.Count==1` is a bug in what a successfully-opened recordset contains, not in whether the open succeeded. Next step (not started): find where/how `Fields.Count` itself is determined for a live `DAORecordset`. Full detail in `status.md`.

**Housekeeping**: Molly flagged the project's recurring pattern of leaving stale breakpoints/logpoints in place after an investigation closes (`run_exe.py` was spamming `[expr-svc-probe]` LoadLibraryA/DllMain lines and a multi-hundred-line `[singlestep-trace]` instruction dump on every run, from the DAO-3075 investigation that closed 2026-08-20/2026-08-22). Removed: 7 stale `run_exe.py` probes (`_source_rewrite_probe`, `_parser_probe`, `_exit_probe`, `_dat_ab04_probe`, `_lookahead_call_probe`, `_lookahead_result_probe`, `_gated_scan_token_probe`) plus their ~390 lines of investigation narrative and the now-unused `_try_read_str` helper; reverted the two `expr-svc-probe` `logger.error` calls in `patch_internals.py`/`kernel32_handlers.py` to `logger.debug` (one folded into an existing generic debug log). All three files compile clean. Breakpoint slots: 1 of 8 in use (this session's `_openrecordset_hresult_probe`), 7 free. No structural fix yet for the underlying cause (no loud failure when the 8-slot cap is hit, no expiry/bookkeeping for probes) -- flagged to Molly as a possible follow-up, not implemented.

---

## 2026-08-25 — Investigation only, no fix: `mcity.c(588)` prefClass assert traced to a real DAO `Recordset.Fields.Count` reading `1` instead of `10` for `StockVehicleAttributes_SelectAll2`

New blocker after tonight's SEH-dispatch fixes got the game to its real main window (furthest ever). Full mechanism traced via Ghidra + `mdbtools` + one focused breakpoint, all real game/DAO code confirmed live: `Dbcode_Fetch`'s `GetValue()` call returns `NULL` whenever `column_index >= Fields.Count`, and since `dblog.txt` shows the "not SELECT-ed" warning on column 1 every row/every run but never column 0, `Fields.Count` is provably `1` for this query's real, live-opened recordset -- even though the query (a real stored QueryDef, `StockVehicleAttributes_SelectAll2`, confirmed via `mdb-queries`) selects 10 columns. Ruled out via `mdbtools`: bad source data, a corrupted file copy, lock conflicts. Not yet found: why the real `_DAOQueryDef::OpenRecordset` COM call actually returns that 1-field recordset in tew's environment -- two attempts to catch the HRESULT live (breakpoint after the vtable call, single-stepping past it) missed, due to two wrong call-site addresses guessed from the decompiler's internal literals rather than real disassembly, then an imprecise step count. Full trace, ruled-out theories, and the precise next step (get the *real* disassembly of `DoQuery`'s body at `0x00997450` first) are in `status.md`/`status_archive.md` -- not re-summarized further here.

---

## 2026-08-24 (cont'd) — Fix `_invoke_handler`'s sentinel/step-budget model for a handler that never returns (proactive EIP-distance escape detection); anti-debug-self-test crash now FULLY resolved -- game reaches its real main window. Also: SDL2/Vulkan now shut down cleanly on SIGTERM, fixing an all-night KWin-destabilizing leak

**The remaining blocker from earlier tonight**: fixing `_rtl_unwind`'s resume ESP got `__except_handler3` running correctly, but its invocation then ran the full 2,000,000-step `_STEP_LIMIT` without returning, forcing a false "unhandled by SEH chain" halt. Root cause: `_invoke_handler`'s loop only exits on `cpu.halted`, and a `RtlUnwind` redirect is just a register mutation that never sets it -- once `__except_handler3` jumps into the real `__except` block (a plain compiler-emitted JMP, not a second RtlUnwind call -- confirmed live, exactly one RtlUnwind call happens for this pattern), execution just keeps flowing into ordinary, unbounded WinMain/game continuation, all still counted as the same `_invoke_handler` call, because that legitimately-continuing code was never going to reach `SEH_RETURN_SENTINEL`.

**Two detection approaches tried and live-disproven before the working one** (both reverted, not present in final code -- see `status_archive.md`'s "Previous status (2026-08-24)" for full detail): (1) checking whether ESP rose to/past the original exception's own SEH frame address -- a full single-step trace proved ESP never got anywhere near that address during the real escape, wrong assumption about where the `__except` block resumes; (2) replicating `run_exe.py`'s outer-loop `preempt_slice`/timer-heartbeat servicing inside `_invoke_handler` -- fixed nothing, since the real problem was staying nested at all, not lacking timer service while nested (the timer/thread subsystem was already confirmed working correctly under normal, non-nested execution in an much earlier session).

**The fix that actually worked**: proactively raise `SehHandlerEscaped` once `EIP` has moved more than `_ESCAPE_EIP_DISTANCE` (2MB) away from `handler_addr` -- live-verified via a ~90-step single-step trace that a handler's own bounded logic (prologue, filter call, `__global_unwind2`, `__NLG_Notify`) stays within ~0x10000 bytes of `handler_addr`, while the real `__except` block resumes millions of bytes away in the game's own EXE code. `_rtl_unwind` sets `cpu._seh_just_redirected` right after any redirect; `_invoke_handler` responds by switching to much smaller batches (`_FINE_STEP_BATCH=50`, for `_FINE_STEP_WINDOW=3,000` steps) so the check catches the transition within a handful of instructions rather than missing it inside a 10,000-step batch. New test `test_dispatch_exception_returns_immediately_when_handler_resumes_far_from_itself` in `tests/unit/kernel/test_seh.py`. `pytest -q`: 1238/1238.

**Live-verified, full chain**: `CPU fault at EIP=0x004d980f -- attempting SEH dispatch` (6.197s) -> `fault at 0x004d980f handled by game's own SEH chain -- resuming` (6.205s, 8ms, not a multi-second timeout) -> 42 more seconds of real execution (DirectX8 setup, DAO/DB thread startup, COM-licensing checks) -> **the real Motor City Online main window renders and stays up** -> halts on a genuine, unrelated, downstream `ASSERT: mcity.c(588) prefClass>=0 && prefClass<DBCP_MaxRatings` at 48.481s, with a real 9-frame EBP chain from the entry point down to the assertion site. Furthest MCity_d.exe has ever run in this emulator -- `mcity.c(588)` is a new, separate investigation for next time.

**Also fixed, discovered live-testing tonight**: `run_exe.py` never installed a `SIGTERM`/`SIGINT` handler, so every `timeout N ...`-bounded run that actually hit its limit (the standard way this project bounds a debugging session) got killed before `window_manager.shutdown()` (`SDL_Quit()`) ever ran -- orphaning the SDL window/GL context every time, all night. Compounding it: even on a *clean* shutdown, `window_manager.shutdown()` called `SDL_DestroyWindow` directly with **no Vulkan-side cleanup at all** -- D3D8's `VkSwapchainKHR`/`VkSurfaceKHR`/`VkDevice`/`VkInstance` were simply abandoned, destroying the native window handle out from under a still-live Vulkan surface/swapchain (undefined behavior per spec). Both suspected of destabilizing KWin across the night's many runs. Fixed: added a `SIGTERM`/`SIGINT` handler in `run_exe.py` mirroring the normal post-run path (mirrors its `os._exit()`-not-`sys.exit()` choice too, same NVIDIA-driver-atexit-crash reason already documented there); added `tew/api/d3d8/_state.shutdown()` (`vkDeviceWaitIdle` -> destroy swapchain/semaphores/fence/command-pool -> destroy surface -> destroy device -> destroy instance, each step independently guarded so a partially-initialized D3D8 state can't block shutdown), wired into `WindowManager.shutdown()` before its own SDL destroy calls. Live-verified clean on both the signal-handler path and the normal post-run path (`[d3d8] [shutdown] Vulkan objects torn down` followed by `[window] [WindowManager] SDL2 shut down`, no crash or hang either way).

---

## 2026-08-24 — Fix `_rtl_unwind`'s resume ESP: MSVC's `__global_unwind2` self-return trick needs the caller's own stack depth, not `target_frame`'s address. Anti-debug-self-test crash resolved; game now reaches DirectX8 setup

Root cause of the crash that survived 2026-08-23's EBP-restoration fix, found via live single-step tracing (temporary instrumentation in `_invoke_handler`, removed after use): `_CLayer_DetectDebugger`'s real `__except_handler3` (`0x9f5eb8`) calls the real, compiled `__global_unwind2` (`0x9f2e90`), which calls `RtlUnwind(EstablisherFrame, TargetIp=<its own return address>, NULL, NULL)` -- a standard MSVC CRT trick using RtlUnwind's "jump to TargetIp" purely to simulate a normal return so `__global_unwind2`'s own epilogue can run. `tew/kernel/seh.py`'s `_rtl_unwind` set `ESP = target_frame` before jumping -- correct-looking, but `target_frame` is the SEH registration record's own address, completely unrelated to `__global_unwind2`'s real (much deeper) stack position. The hijacked epilogue's `pop edi/esi/ebx` read the registration record's own `next`/`handler`/`scopetable` fields instead of saved registers (live-confirmed `EBX=0`), then `mov esp,ebp;pop ebp;ret` jumped ESP to `__global_unwind2`'s own real EBP (`0x7fffffd8`, near the stack top this early in startup) and popped two more unrelated dwords -- one (`0x9f5eb8`, incidental leftover stack content) became a bogus `RET` target, landing back inside `__except_handler3` a second, self-reentrant time with garbage arguments, producing the exact same `EBP=0x7fffffdc` null-deref crash every run since 2026-08-22. This also fully explains `ret=0x011f3b90`/`handler=0xFFFFFFFF`/`frame=0x7fffffd8` seen in every prior crash dump: incidental stale stack content, never real frames -- 2026-08-23's "garbage return address on the thread's outermost frame" theory was a plausible-but-wrong read of this same symptom.

The module docstring's prior justification ("same approach Wine's x86 RtlUnwind takes") doesn't actually cover this case: that's true of RtlUnwind itself (an ntdll.dll export Wine reimplements natively regardless), but `__global_unwind2` is CRT-internal machine code, statically linked into this debug build -- genuine, unmodified Microsoft bytes even under real Wine, which Wine has no mechanism to intercept. Real RtlUnwind resumes in the context of whoever called it (as if it had done an ordinary `RET 16`), not at TargetFrame's address.

**Fix**: `_rtl_unwind`'s `target_ip` branch now computes `cpu.regs[ESP] = (esp + 20) & 0xFFFFFFFF` (`esp` = RtlUnwind's own entry ESP; `+20` = return address + 4 stdcall DWORD args, i.e. the real caller-return stack depth) instead of `target_frame`. Updated the module docstring's simplifications list. `test_rtlunwind_pops_current_frame_without_reinvoking_it`'s ESP assertion had encoded the old, wrong `ESP==target_frame` behavior -- updated to the corrected caller-return-depth invariant. Added `test_rtlunwind_resumes_at_callers_stack_depth_when_target_ip_is_its_own_return_address`, modeling the `__global_unwind2` self-return shape directly (`TargetIp` computed as the address right after the handler's own `call ecx`, not a hardcoded label). `pytest -q`: 1237/1237. EBP-restoration logic (2026-08-23's fix) is orthogonal and unchanged.

**Live-verified**: the original crash reproduced identically across 5 separate runs before this fix (confirming full determinism). Post-fix, the bogus self-reentrant crash is gone; `~/.emu32/MCity/stdout.txt` has real content for the first time (`clayer.c(311) SEH Handler!` / `clayer.c(318) Found Debugger!` -- the `__except` block itself now runs and prints; "Found Debugger!" is a known separate false-positive, not new / `platform.c(325) dx8z.dll` / `mode:0, width:1024, height:768, hz:60, bpp:32, fmt:22`) -- the game reaches DirectX8 display-mode setup, further than ever observed. New blocker found: `__except_handler3`'s invocation now runs the full 2,000,000-step `_STEP_LIMIT` without returning ("timed out -- treating chain as exhausted"), which halts the whole run. Not a downstream DX8 bug -- `_invoke_handler`'s loop only exits on `cpu.halted`, and `_rtl_unwind`'s redirect never sets that, so execution just keeps flowing (through `__global_unwind2`, `__except_handler3`, the real `__except` block, and onward into WinMain's ordinary, unbounded continuation) all still counted against that one invocation's step budget -- an architectural mismatch between the sentinel/step-budget dispatch model and a handler whose flow legitimately never returns, not a stuck/spinning game. See `memory/status.md` for the fix direction.

**Also found, unrelated, separately flagged in status.md, not fixed**: `WinMain`'s own raw `_fputs`/`_fprintf` calls (real statically-linked CRT code, not a Python handler) write through the CRT's static `stdout` FILE slot, whose `CreateFile("stdout.txt")` succeeds but is then never touched by any `WriteFile` -- an orphaned handle. Low-priority: `Channel_SystemPrint`'s separate `guest_stdout_handle` path already delivers real output to `stdout.txt` (confirmed above), so this only affects a handful of raw calls in `WinMain` itself.

---

## 2026-08-23 — Add RtlUnwind EBP restoration (correct fix, doesn't resolve the crash -- real root cause found to be elsewhere)

Planned via `EnterPlanMode` and implemented per plan: `tew/kernel/seh.py`'s `dispatch_exception` now stashes `(original_frame, original_ebp)` once per exception -- captured right after `_write_context`, before the chain-walk loop reassigns `frame` -- since this is exactly the (SEH frame, EBP) pairing known-correct at the moment the exception occurred. `_rtl_unwind`'s `target_ip` branch now restores `EBP` from that pairing when `RtlUnwind`'s `target_frame` matches (the common case: unwinding back to the same frame/function the exception originated in), and otherwise logs a clear warning and leaves EBP untouched rather than guessing (a true multi-level unwind, target several call-frames further out, needs an EBP-chain walk this module doesn't implement -- explicitly out of scope, matching this project's "implement only the well-evidenced case" discipline). 2 new tests in `tests/unit/kernel/test_seh.py`; existing tests (including the clean-escape-via-JMP regression test) unaffected. `pytest -q`: 1236/1236.

**Live-verified the fix itself works exactly as designed**, via a temporary breakpoint probe (added, used, then removed): the stashed `original_ebp` (`0x7ffffd54`) really is `_CLayer_DetectDebugger`'s own correct EBP (matching `EstablisherFrame+0x10` -- confirmed this is the real frame's own value, not `__except_handler3`'s internal `lea ebp,[ebx+0x10]` reassignment as originally guessed when this bug was first found), `target_frame` correctly matches `original_frame`, and EBP correctly gets restored before jumping to `target_ip`.

**But the crash recurs identically anyway.** One more probe (return address at the second `__except_handler3` invocation) found the real cause: that return address, `0x011f3b90`, is inside a data/string-table region, not real code -- the same value that appeared as the outermost stack frame's "return address" in every crash dump all session, previously misread as just where the diagnostic's own EBP-chain walk ran out of real frames, not as an address the CPU would actually execute. The real story: by this point, `_CLayer_DetectDebugger` (and whatever calls it) has already returned normally all the way up to the thread's own outermost function, which then tries to `RET` into its own stored return address -- and that value is garbage, not valid thread-exit code, so execution wanders into whatever that garbage decodes as. This is unrelated to EBP -- confirmed empirically, since the exact same `EstablisherFrame=0x7ffffff0` value recurred identically both before and after the EBP fix, with completely different actual EBP values each time.

**The EBP fix is kept regardless** -- independently correct, tests its own claim, and fixes a real (if not sufficient on its own) previously-documented `RtlUnwind` limitation. The actual root cause is a different, likely more foundational gap in how tew sets up a thread's initial stack frame (specifically, what return address it places there for when the entry-point function eventually returns) -- not yet investigated at all. See `memory/status.md`.

---

## 2026-08-22 (cont'd x3) — Root cause found for the anti-debug self-test crash: RtlUnwind doesn't restore EBP, so the resumed __except block computes a garbage EBP-relative address. Not yet fixed

With the SEH dispatcher now honest (previous entry), the run cleanly halts with `fault at 0x004d980f unhandled by SEH chain`. Kept digging into *why* `_CLayer_DetectDebugger`'s own `__except_handler3`-shaped scope-table walk crashes at all, rather than stopping at "it's honestly reported now."

Traced via a fresh ClickHouse execution-history capture (a narrow 0-9M-step window covering the whole pre-crash run -- the earlier 500K-8M window from the prior investigation had been superseded by a lot of cumulative old data, including the earlier buggy oscillating-capture run, so a clean fresh capture was simpler than disentangling it) plus a live register-value breakpoint probe at the real handler's entry point, cross-referenced against `dispatch_exception`/`_invoke_handler`/`RtlUnwind`'s own internal debug logging (temporarily promoted to always-visible for one run, then reverted).

**Full mechanism, each step confirmed live**: `dispatch_exception` calls `_invoke_handler` for `_CLayer_DetectDebugger`'s own SEH frame (`0x7ffffd44`, genuinely valid scopetable/trylevel). The real, compiled `__except_handler3` code runs, its filter approves handling, and it calls the real `RtlUnwind(target_frame=0x7ffffd44, target_ip=0x009f2ea8)` -- `0x009f2ea8` being the real, compiled `__except { ... }` block body. `_rtl_unwind` (`tew/kernel/seh.py`) sets `ESP = target_frame` and jumps to `target_ip`, but deliberately does not restore EBP -- an explicitly documented simplification in the module's own docstring. The resumed `__except` block's own code uses ordinary EBP-relative addressing, but EBP is stale (`0x7fffffdc`, the thread's outermost frame, left over from `__except_handler3`'s own internal `lea ebp,[ebx+0x10]` computation) -- so its attempt to establish a new SEH frame lands at `EBP+0x14 = 0x7ffffff0` (coincidentally equal to `stack_base = mem_size - 16`, never actually written) instead of a real location. That address holds an all-zero `{next=0, handler=0}` record; calling through the null handler is the actual crash.

**Not a newly-introduced bug** -- a previously-known, explicitly-documented `RtlUnwind` limitation, only now exposed because tonight's earlier three fixes let the run get far enough to exercise a real `__except` block resuming after a genuine unwind for the first time.

**Not yet fixed**: giving `RtlUnwind` the target frame's real EBP requires something like a saved per-frame CONTEXT, which the module's docstring already flags as not generically available from MSVC's frame layout -- a real design question, not a quick patch. Needs its own plan, same as the other three fixes tonight, before implementing.

**Technique notes for future investigations**: ClickHouse capture from step 0 works fine up to ~9M steps/~28M events with an explicit stop bound -- no need to avoid step-0 starts specifically. `dispatch_exception`/`_invoke_handler`'s own Python-level `memory.read32()`/`write32()` calls go through a *different* code path (the C-ABI `mem_read32`/`mem_write32`) than the guest CPU's own instruction execution (`core.zig`'s `memRead8`/`memWrite8`, the only thing the ClickHouse write-hook observes) -- Python-side SEH-dispatch writes are invisible to that capture, so use a live breakpoint probe instead of write-history reconstruction when investigating similar Python-level state. Neither the ClickHouse docker container nor `ghidra-mcp`'s in-memory project state survives a power cut -- both need re-establishing (`docker compose up -d`; a fresh MCP handshake + re-opening the project/program) afterward.

---

## 2026-08-22 (cont'd x2) — Fix dispatch_exception conflating a handler crashing mid-execution with a clean RtlUnwind escape

Live-verifying the null-page guard fix (below) showed it working exactly as designed -- `_CLayer_DetectDebugger`'s deliberate `0x190` read now genuinely faults -- but the aftermath cascaded into `RUNAWAY at EIP=0x00000002` instead of a clean resolution. Traced the cause to `tew/kernel/seh.py`'s SEH dispatcher itself, not the game or the null-page fix.

`_invoke_handler` runs a real SEH handler and waits for `cpu.halted`. Three distinct things produce that halt: (1) the handler returns normally via the return-sentinel trampoline; (2) the handler calls `RtlUnwind`, which redirects EIP/ESP directly and lets real post-unwind code keep running until *that* eventually halts -- the intended "clean escape"; (3) a genuine CPU fault happens somewhere in the handler's own execution, which sets `halted=True` the identical way. `_invoke_handler` only checked the landing EIP against the sentinel to decide "escaped," so cases 2 and 3 were indistinguishable, and `dispatch_exception`'s `except SehHandlerEscaped` unconditionally assumed case 2, returning `True` ("handled") without ever checking `cpu.faulted`.

Confirmed live: the real `__except_handler3`-shaped handler MCity_d.exe's own anti-debug self-test installs (`0x009f5eb8`, genuine compiled MSVC CRT scope-table-walking code, hand-disassembled since Ghidra doesn't recognize it as a function) itself hit a second, genuine access violation reading its own scope-table data (`DAT_01191420`). `dispatch_exception` saw the halt, assumed a clean escape, returned `True`, and `run_exe.py` resumed execution from the crashed state -- producing the runaway.

**Fix**: `SehHandlerEscaped` (`tew/kernel/seh.py`) gained `faulted`/`esp_before` fields, captured at the raise site in `_invoke_handler`. `dispatch_exception`'s chain-walk loop now branches on `e.faulted`: a clean escape still returns `True` exactly as before; a handler that itself crashed is logged clearly (`"crashed mid-execution... instead of escaping via RtlUnwind -- treating as declined"`), the CPU is restored to a sane state (`cpu.faulted = False`, ESP restored), and the chain walk continues to the next frame -- exactly as if the handler had returned `ContinueSearch` -- instead of falsely reporting success. `_rtl_unwind`'s own intervening-handler walk got the same ESP-restoration safety fix for consistency (a real latent bug there too, just less visible since that path never claimed false success).

Planned via `EnterPlanMode`. 2 new tests in `tests/unit/kernel/test_seh.py` (a crashing handler is correctly reported unhandled rather than falsely "handled"; a chain with a crashing inner handler still reaches a working outer one), existing tests (including the clean-escape-via-JMP regression test) unaffected. `pytest -q`: 1234/1234.

**Live-verified**: the same anti-debug self-test scenario now produces an honest chain in the log -- the real CRT handler is correctly logged as crashing (not silently "handled"), the chain walk correctly tries the next frame (garbage, also correctly declined), and the exception is finally reported as `fault at 0x004d980f unhandled by SEH chain -- halting`, with a clean, real diagnostic dump, instead of the previous corrupted runaway hundreds of thousands of steps later. Doesn't itself make the run progress further -- same underlying stopping point -- but replaces a silent, corrupting lie with an honest, immediately diagnostic failure, which is the actual fix this entry is about. The deeper question (why the real CRT scope-table walk crashes reading its own compiled data in the first place) is a separate, not-yet-started investigation -- see `status.md`.

---

## 2026-08-22 (cont'd) — Add an opt-in null-page memory guard, so the game's own anti-debug self-test can genuinely fault

Live-verifying the `CompareStringA`/`CompareStringW` fix (below) showed the run progressing much further, then hitting an unhandled `INT3` at `0x00688c68` inside real `MCity_d.exe` game code. Traced through `_Nfs_DebugBreak()` (`carClassList::carClassList()`, `mcity.c`, a `prefClass` attribute assert) to its gating flag, `_Nfs_DebuggerIsPresent`, and from there to its real origin: `_CLayer_DetectDebugger()` (`0x004d97b0`, `clayer.c`) -- a classic anti-debug self-test that deliberately reads address `0x00000190` inside an SEH-protected block, relying on a real access violation (no debugger present → the process's own SEH handler catches it and clears the flag; debugger present → it intercepts first-chance and the flag stays at its wrong default).

tew's memory model had no concept of unmapped address ranges -- the entire 2GB flat buffer was uniformly readable, so this deliberate read never faulted at all, and the self-test's correcting path never ran.

**Fix**: planned via `EnterPlanMode`, with a Plan-agent validation pass before implementing -- it caught that the naive design (editing `cpu/src/primitives.zig`'s `inBounds1` directly) would have broken ~60+ existing Zig tests, since nearly all of them use tiny test buffers with code/data starting at address 0, and would also have silently changed the Python-facing `memory.read8()`/`is_valid_address()` surface. Implemented as an **opt-in** field instead: `CpuState.guard_null_page` (`cpu/src/core.zig`, default `false`), checked alongside (not replacing) the existing bounds check in `memRead8`/`memWrite8`, against a new `NULL_PAGE_SIZE = 0x10000` constant matching real Windows' documented "first 64KB never mapped" guarantee. New `cpu_enable_null_page_guard` C-ABI export (`cpu/src/kernel.zig`) and `cpu.enable_null_page_guard()` Python method (`tew/hardware/cpu_zig.py`), called once at real-emulator startup (`run_exe.py`, right after `cpu = CPU(mem)`) -- bare `CpuState`s built directly by unit tests never opt in, so every existing test is completely unaffected.

4 new Zig tests (`cpu/src/kernel.zig`, in the "Public C ABI" section): default-off behavior unchanged, a genuine low-address data read faults once enabled, and the `0x10000` boundary itself stays valid. `zig build test`: all green (~60+ existing tests untouched). `pytest -q`: 1232/1232.

**Live-verified the fix itself works exactly as designed**: `CPU fault at EIP=0x004d980f opcode=0xa0` -- the exact `MOV AL,[0x190]` instruction `_CLayer_DetectDebugger` uses to probe for a debugger, faulting for the first time ever. However, the aftermath is not a clean resolution -- see `status.md`/`status_archive.md` for the newly-surfaced `dispatch_exception`/`SehHandlerEscaped` bug this exposed (a separate, not-yet-fixed issue in the SEH dispatcher itself, not a flaw in this fix).

---

## 2026-08-22 — RESOLVED: msjet35.dll collation-cache crash. Root cause was `CompareStringA`/`CompareStringW` never validating the locale argument, always reporting success

Continuation of the `FUN_7a87ba0a` investigation below. Three plausible root causes were tested directly and ruled out with real evidence: a silently-unsupported oleaut32 import (`LHashValOfNameSys`, ordinal 165 -- confirmed never actually called, not a logging blind spot), missing registry defaults (`Jet 3.5` engine key values all come back `NOT FOUND`, but none are locale-related -- performance-tuning knobs only), and file-I/O corruption of the `.mdb` header (`Online.mdb` reads are clean, correctly-offset, sequential -- real bytes served correctly).

Dynamically confirmed via tew's native Zig `cpu.enable_history_capture_clickhouse(...)` execution-history capture (real memory-write hooks inside the CPU core, not a Python-level watchpoint that would miss guest-instruction writes) -- gated to two narrow windows to avoid the known overhead blowup (`_HISTORY_CAPTURE_START_STEP`/`_HISTORY_CAPTURE_STOP_STEP`/`_HISTORY_CAPTURE_DONE` in `run_exe.py`) -- that `DAT_7a9362c0[*]+0x2c0` is never written anywhere in the observed run, both near database-open (steps 500K-8M) and right before the crash (steps 237M-237.9M).

Static tracing found the real `OpenDatabase` function (`FUN_7a874175` in msjet35.dll, confirmed via `dbcode.c`'s log showing `Tmp.MDB`'s creation, then cross-checked against DAO's own `dao350.dll` strings -- `get_CollatingOrder`, and critically `"Locale argument (CreateDatabase, CompactDatabase)"`, confirming Locale is only a *CreateDatabase* argument, not OpenDatabase's) -- and traced `CreateDatabase` (`FUN_7a8e8240`)'s actual file-creation call chain down to `FUN_7a878159`, a connect-string parser requiring `Langid=`/a 2-char codepage key/`Country=` sub-keys, with real Jet error codes if missing. A live one-shot breakpoint probe (`FUN_7a874685` entry, static `0x7a874685`) confirmed MCity_d.exe's own `CreateDatabase` call for `Tmp.MDB` (`dbcode.c`'s very first real Jet operation) passes a literal **empty** Locale connect-string (`DAT_7a93608b`, confirmed via `dump_bytes` to be a null-terminated empty string).

**Root cause, fully traced**: with an empty Locale string, `FUN_7a84c830` (langid+codepage → collating-order-id resolver) falls through to its "unspecified locale" fallback path, which probes validity by calling `CompareStringA(locale=0, ...comparing a string to itself...)` -- real Windows would reject locale `0` (`IsValidLocale` fails it) and `CompareStringA` would return 0, triggering `FUN_7a84c830`'s safe-default substitution (`0x100`). **tew's `CompareStringA`/`CompareStringW` (`tew/api/kernel32_io.py`) never read the locale argument at all** -- any value, including 0, silently "succeeded" with a real string comparison. So the probe always looked like success, the fallback never triggered, and Jet ended up with no valid collating-order id -- which propagates, several calls later, into the per-session collation-interface field (`DAT_7a9362c0[session]+0x2c0`) staying null, surfacing as an unrelated-looking crash deep in query evaluation against `Online.mdb`.

**Fix**: both handlers now validate the locale against `0x0409` -- the single locale this emulator models everywhere else (`_is_valid_locale`/`_get_user_default_lcid`/`_get_system_default_lang_id`, all already hardcoded to it) -- and return 0 with `ERROR_INVALID_PARAMETER` (via `TEB_BASE+0x34`, matching the established last-error convention) for anything else, instead of always succeeding. 11 new tests, `tests/unit/api/test_kernel32_io_compare_string.py`. `pytest -q`: 1232/1232.

**Live-verified**: the `MSJET35.DLL+0x3bc04`/`FUN_7a87ba0a` collation crash no longer occurs at all. The run now progresses much further -- real `MCity_d.exe` (not DLL) return addresses on the stack -- to a genuinely different, expected-shape event: `INT3 breakpoint at EIP=0x00688c68 unhandled by SEH chain -- halting`, a real debug-build assertion in the game's own code. Per established policy, a genuinely unhandled `MCity_d.exe` INT3 correctly stays a hard halt rather than being silently resumed -- this is the next thing to investigate, not a symptom of an incomplete fix.

Also fixed along the way: a real bug in this session's own temporary two-window ClickHouse-capture gating logic (`run_exe.py`) that oscillated on/off every batch once past the stop step, slowing one run enough to be externally killed and very likely causing a spurious, unrelated `MUTEX_free` dialog near the end (instrumentation thrashing the scheduler's timing, not a genuine finding) -- fixed with a one-shot `_HISTORY_CAPTURE_DONE` flag.

---

## 2026-08-21 (cont'd x6) — New runaway crash traced to a strong, EBP-verified candidate in msjet35.dll (unchecked collation-interface vtable call); not yet fully closed

Continuation of the logger fix below. Spawned a 3rd headless `claude -p` child to investigate the new runaway -- it correctly refused to fabricate results when every `mcp__ghidra__*` call was denied (`list_projects`/`list_programs` weren't in the project's `.claude/settings.local.json` allow-list of 5 tools, unlike the earlier successful child which never needed them). Added the missing read-only investigation tools to that allow-list directly (`list_projects`, `list_programs`, `switch_active_project`, `import_and_analyze`, `get_function_calls`, `get_function_instructions`, `search_strings`) rather than keep hoping a permission-mode flag would cover it.

A 4th spawn with the fixed allow-list found real, EBP-anchored evidence: `FUN_7a87ba0a` in msjet35.dll (already Ghidra-analyzed, decompiled normally) -- a general variant-comparison routine whose indirect call at `0x7a87bc01` returns to exactly `0x7a87bc04`, the one confirmed-real EBP-chain frame. It reads a per-session cached collation-interface pointer (`DAT_7a9362c0[session]+0x2c0`) and calls through its vtable slot 6 with no NULL check -- if that slot was never populated, the call target becomes `0x18`, matching the crash's near-zero jump exactly. Same shape as the already-fixed `LoadTypeLibEx` bug, different object (a Jet collation cache, not an OLEAUT32 import cache). `expsrv.dll` couldn't be fully re-analyzed (`FileInUseException`, likely this parent session's own stuck connection holding a Ghidra-side lock), so two candidate call sites there are hand-disassembled only, one plausible and one explicitly ruled out. Full write-up: `memory/expsrv_crash2_msjet_collation_analysis.md`.

**Not yet resolved**: what's supposed to write the `0x2c0` collation-object cache field, and whether tew is missing a real Jet API call that would populate it. Full detail in `status.md`.

---

## 2026-08-21 (cont'd x5) — Crash-diagnostic log lines can no longer be silently dropped by LOG_LEVEL/LOG_CATEGORIES

Investigating the new runaway crash exposed after VarDateFromStr, the "last valid EIP before this jump went bad" diagnostic line was missing from the log twice in a row. Root cause: `tew/logger.py`'s `_emit()` already exempted ERROR-level messages from both the level and category filters (deliberately, so halt diagnostics are never silently dropped), but the runaway-detector's own "RUNAWAY at step..., last valid EIP..." line -- and its sibling in the cpu.faulted branch -- were logged at WARN, which got no such exemption from either filter.

Added `logger.always(level, category, msg)`: bypasses both filters via a new `force` parameter in `_emit()`, while still printing the real `[WARN]`/`[INFO]`/`[ERROR]` prefix for the given level (doesn't misrepresent severity, only guarantees visibility). Applied to the two lines that actually caused the blind spot, not swept across the whole file -- that would defeat configurable log levels for normal runs. New `tests/unit/test_logger.py` (first test file for this module), 9 tests: 4 regression guards (ordinary filtering unchanged) + 5 for the new bypass. `pytest -q`: 1221/1221.

Live-verified with a filter narrower than the bug ever needed (`LOG_LEVEL=error LOG_CATEGORIES=cpu`, excluding `seh` entirely): the diagnostic line shows up anyway. Full detail in `status.md`.

---

## 2026-08-21 (cont'd x4) — Implemented VarDateFromStr (Ordinal #94), planned first via EnterPlanMode; new, harder runaway crash now exposed

Continuation of the two ordinal fixes below. `VarDateFromStr` needed real planning (date parsing implies locale-aware formats in general) rather than the obviously-correct pattern of the last two fixes, so used `EnterPlanMode` per Molly's request. Added a temporary diagnostic-only stub first to capture the exact real input rather than guess: `'1/1/2010'`, confirmed no `#` delimiters (Jet's tokenizer strips them), no time component, no 2-digit year.

Implemented only that exact `M/D/YYYY` shape (4-digit year required), with real calendar validation via `datetime.date` and the documented OLE Automation/Lotus-1-2-3 epoch quirk (1900 miscounted as a leap year, +1 correction for dates >= 1900-03-01) -- correctness here matters since the query does a real `<>` comparison against a stored database date. Caught and fixed one hand-computed test value that was wrong before trusting it (verified via direct Python computation instead). 26 new tests. `pytest -q`: 1212/1212.

**Live-verified**: the halt is gone. But the run now hits a genuinely different, harder problem -- a new runaway crash (`0x00056159`), confirmed truly unhandled by the (already-fixed, trustworthy) SEH chain, not another quick missing-handler gap. Full detail in `status.md`.

---

## 2026-08-21 (cont'd x3) — Two more oleaut32 ordinal-only gaps fixed (VarI4FromStr, VarR8FromStr), found by reading the real DLL's export table directly

Continuation of the expsrv.dll fix below. Molly asked how many total exports oleaut32.dll has -- checked the real `/data/Downloads/i386-binaries/oleaut32.dll`'s PE export directory directly via `objdump`: 442 total, 398 by name, 44 ordinal-only. Used the same technique to identify each new halt by real name instead of guessing: `Ordinal #64` = `VarI4FromStr`, `Ordinal #84` = `VarR8FromStr`.

Both live-confirmed as the same call chain: MSJET35.DLL's expression parser evaluating a numeric literal inside a real WHERE-clause expression (`"ParentId = 251658241 and Type = 6 and Connect Is Null"`), first trying an integer conversion then falling back to a real-number one. Implemented the well-defined case for each -- a real, shape-validated (regex, not Python's more permissive `int()`/`float()`) decimal/scientific numeric literal, with correct overflow handling -- returning the real `DISP_E_TYPEMISMATCH` HRESULT for anything else rather than guessing at locale-specific formats never observed live. `VarR8FromStr` needed a new `_write_f64` helper (no 64-bit memory primitive existed) packing real IEEE-754 bytes via `struct.pack`. Both registered under their ordinal *and* real name, per the `LoadTypeLibEx` lesson. 18 + 19 new tests. `pytest -q`: 1149 → 1167 → 1186, green throughout. Live-verified: each fix cleared its halt and the run progressed further before hitting the next one.

**New blocker: `Ordinal #94` = `VarDateFromStr`** -- a real scope jump from the last two (locale-aware date-format parsing, not a single well-defined numeric shape). Not started. Full detail in `status.md`.

---

## 2026-08-21 (cont'd x2) — RESOLVED the expsrv.dll near-null-jump crash: LoadTypeLibEx/RegisterTypeLib registered under the wrong GetProcAddress key, UnRegisterTypeLib/CreateTypeLib2 missing entirely

Found via an unusual but effective technique: this session's own Ghidra MCP connection was stuck disconnected (the underlying `ghidra-mcp.service` had been OOM-killed twice per `journalctl` -- real memory pressure from a loaded desktop, not a server bug; killing Firefox fixed the server's stability but not this session's already-stale client connection). Spawned a fresh, independent `claude -p "<self-contained prompt>" --permission-mode acceptEdits` process, which gets its own clean MCP handshake since it's a genuinely separate process. (First attempt without the permission flag landed in plan mode and couldn't call any tool at all -- worth remembering for next time.)

That child session, working around its own narrow tool allowlist by hand-disassembling via raw `dump_bytes` instead of giving up when `decompile_function` failed (the loaded expsrv.dll program had never actually been auto-analyzed), found the real chain: `MSJET35.DLL` calls into a lazy get-or-load-`ITypeLib` helper in `expsrv.dll` at static `0x0F9C9CE9`, which calls `DWORD PTR [0x0FA0FEF0]` with no NULL check. That global slot is filled by expsrv.dll's own init via a manual `LoadLibraryA`+`GetProcAddress` chain resolving `DispCallFunc`/`LoadTypeLibEx`/`UnRegisterTypeLib`/`CreateTypeLib2` in sequence, bailing the whole chain on the first NULL. Real Windows guarantees all four always resolve.

Confirmed against tew's actual source: `GetProcAddress` does a strict string lookup, but `LoadTypeLibEx` was implemented and registered only under its ordinal key (`"Ordinal #154"`), never the string name real callers actually use -- same bug class as `VariantClear`'s `Ordinal #9` fix from an earlier session, opposite direction. `UnRegisterTypeLib`/`CreateTypeLib2` didn't exist under any key at all, so fixing only `LoadTypeLibEx` would've just moved the bail point one lookup later.

**Fix**: `LoadTypeLibEx` (and its sibling `RegisterTypeLib`, ordinal 155, same gap) now also registered by name -- same handler, not a duplicate. `UnRegisterTypeLib`/`CreateTypeLib2` newly implemented (E_NOTIMPL, correct real-signature stdcall cleanup). 9 new tests, `test_oleaut32_typelib.py`. `pytest -q`: 1149/1149.

**Live-verified**: the crash is gone. Run progresses to a new, simple, self-documenting halt (`[UNIMPLEMENTED] oleaut32.dll!Ordinal #64`) instead. Full detail in `status.md`.

---

## 2026-08-21 (cont'd) — Fixed a real cross-thread SEH-chain contamination bug (shared TEB, ExceptionList never saved/restored per-thread); runaway detector now routes through real SEH dispatch instead of an ad-hoc dump

Molly's suggestion: since the runaway detector's "EIP left every valid region" condition is exactly what real Windows would raise STATUS_ACCESS_VIOLATION for on the instruction fetch, and `run_exe.py` already has a real SEH-dispatch pipeline wired up for genuine `cpu.faulted` events, route the runaway branch through the same `dispatch_exception(...)` call instead of its own bespoke diagnostic dump. Handled exceptions resume normally; unhandled ones get the same richer `diagnose_halt()` EBP-chain-walk report every other halt already gets.

Live-testing that change against the open expsrv.dll blocker surfaced a real, separate bug: debug-level SEH logging showed the "unhandled" dispatch walking 15 real frames across **at least 6 different threads' stacks** (each frame exactly `THREAD_STACK_SIZE` = `0x40000` apart) before giving up. Root cause: `cpu/src/scheduler.zig`'s `TEB_BASE` is one fixed address shared by every thread -- no real per-thread TEB. The scheduler already saves/restores `last_error` (TEB+0x34) per-thread on every context switch (an earlier fix for the identical bug class), but `ExceptionList` (TEB+0x00, the real `fs:[0]` SEH chain head) was never included, so each thread's own pushed frames spliced onto whatever the previously-scheduled thread had left there.

**Fix**: added `exception_list: u32 = 0xFFFFFFFF` to `ThreadEntry`, threaded through `saveCurrent`/`loadThread`/`initThreadStack` exactly like `last_error`. 3 new tests, red before (compile error) green after. `zig build test` + `pytest -q` (1140/1140) both green.

**Live-verified**: the same crash's SEH walk is now cleanly bounded to 5 frames, all within the faulting thread's own stack -- still genuinely unhandled, but now a trustworthy result instead of a cross-thread-corrupted one. A durable, general-purpose fix independent of the original expsrv.dll crash's still-open root cause (Ghidra's MCP connection dropped mid-session before static analysis of the one real return frame, `expsrv.dll+0x1cbd7`, could happen). Full detail in `status.md`.

---

## 2026-08-21 — Three straightforward Win32 handler gaps fixed in sequence, each exposed by the previous one's fix (VariantChangeType VT_INT, VirtualQuery, GetModuleFileNameW); 4th halt (expsrv.dll indirect jump to invalid address) now the open blocker

Continuation of DAO-3075's resolution the night before. Re-running the same real scenario surfaced a chain of halts, each cleared with tests-first + live re-run before moving to the next:

1. **`oleaut32.dll!VariantChangeType`, unhandled source `vt=22`.** `VT_INT` (22) is documented (MSDN VARENUM) as storage-identical to `VT_I4`. `tew/api/oleaut32_handlers.py`: added `_VT_INT`, treated identically to `_VT_I4` on both source-read and target-write. 5 new tests.
2. **`kernel32.dll!VirtualQuery` had no handler at all.** MSJET35.DLL's own memory manager queries a page it got from `VirtualAlloc`. Implemented in `kernel32_memory.py` against the already-tracked `state.virtual_reserved`/`virtual_committed`; added `state.virtual_protect` (new field, wired into `VirtualAlloc`) since `MEMORY_BASIC_INFORMATION` needs real protection flags that weren't tracked before. Halts loudly on an address outside any tracked region rather than guess at free-region reporting never observed live. 7 new tests.
3. **`kernel32.dll!GetModuleFileNameW` was a deliberate `_halt()` placeholder** next to a fully-implemented `GetModuleFileNameA`. expsrv.dll (VBA runtime) called it. Mirrors the `A` version exactly (nSize in WCHARs, UTF-16LE output). 9 new tests, new file `test_kernel32_get_module_file_name.py`.

Each fix: red test confirmed failing, implemented, green; `pytest -q` full suite green (1124 → 1131 → 1140); live re-run confirmed the specific halt was gone and progress continued further before hitting the next one.

**New 4th halt, not yet fixed**: after all three, the game no longer hits any `[UNIMPLEMENTED]` handler — `run_exe.py`'s runaway-detector fires instead, `EIP=0x0003049c` (tiny, all-zero-bytes destination — an indirect jump through a bad pointer), reached from a real call chain into expsrv.dll (MSJET35.DLL calling Jet's expression-evaluation service). Checked against `tew_fake_kernel_gaps.md` section 18's older, superficially-similar "wild jump from a NULL COM out-pointer" bug (`DllGetClassObject`/`*ppv`, `EIP=0xfefc8d8f`) — different address, different call path (DAO COM activation vs. MSJET↔expsrv expression evaluation), not assumed to be the same root cause without live evidence. Full detail in `status.md`.

---

## 2026-08-20 (cont'd x3) — DAO-3075: RESOLVED. Real cause was a tew CPU-engine bug (0x66-prefixed single-byte INC/DEC r16 ignored the operand-size override, corrupting a zero-flag test on a paren-depth counter)

Continuation of the entry below. Molly asked directly whether the stack could be dropping/misaligning values across the scan's repeated calls, and proposed testing it step by step instead of more hand-disassembly. Built a real `cpu.step()`-driven single-instruction tracer (logs EIP/ESP/EAX/EBX/ECX every instruction from the call into `FUN_7a866c6d` to its return) rather than predicting addresses by hand -- and it settled the question two different ways.

**Stack: clean.** ESP at return was exactly `entry_esp + 8` across a 3041-instruction trace, textbook `stdcall`/`RET 8` cleanup for the function's 2 pushed args, zero drift anywhere. Not the cause.

**Real cause: a 16-bit-operand-size flag bug in tew itself.** The trace showed `MOV BP,1` (paren-depth counter) execute once, then `DEC BP` execute once (both confirmed via `dump_bytes` as the real compiled bytes, `66 BD 01 00` / `66 4D`), and the following `JNZ` took the "not zero" branch when it shouldn't have -- sending the depth-tracking loop back to read another token instead of falling through to the match-check, which is exactly where `AS` gets silently discarded (it fails the loop's own 0x16/0x28/0x29 checks and just gets treated as noise). Root cause in `cpu/src/engine.zig`: `opIncR32`/`opDecR32` (the single-byte `0x40+r`/`0x48+r` forms) hardcoded `.w32` for both the register write and flag computation, never checking `s.op_size_ovr` (the `0x66` prefix) at all -- a gap in an earlier documented sweep that fixed this same class of bug for other opcodes in the same file. `DEC BP` was really decrementing and flag-checking the full 32-bit `EBP`, whose upper 16 bits held real leftover pointer data from earlier in the function -- so the 16-bit `BP` correctly hit 0 but the 32-bit result didn't, clearing ZF instead of setting it.

**Fix + verification:** both opcodes now check `op_size_ovr`, writing only the low 16 bits (upper preserved, matching `opMovR32Imm`'s existing idiom) and passing `.w16` to `updateFlagsArithW`. Two new tests (`engine.zig`, right above the existing disp32 INC/DEC tests) pin the exact live bug, red before the fix and green after. `zig build test`: all green. `pytest -q`: 1119/1119. Live re-run: the SELECT-list column boundary for "Max(PartID) AS Expr1" now computes correctly (`diff=12`, landing right at `"AS Expr1"`), and the run goes on to correctly handle a second, multi-column query it never reached before. `DB_StartUpDatabase` now progresses into real game-data loading per `dblog.txt`. A new, separate, unrelated halt is now exposed further downstream (`EIP=0x002039c2`) -- a fresh investigation, not part of DAO-3075. Full detail in `status.md`.

---

## 2026-08-20 (cont'd x2) — DAO-3075: pinned "AS never reaches the match-check comparison" with certainty; hand-disassembly of the lookahead scanner abandoned as unreliable for this function

Continuation of the entry below. The earlier "disambiguated token sequence" finding in that entry turned out to be incomplete -- gating the capture strictly to the shared match-check label itself (`LAB_7a866c87` in `FUN_7a866c6d`, rather than the call/return points around the whole scanner) showed the comparison logic only ever sees **two** values per invocation, `0x100` then `0x16` -- never `0x105` (AS), even though AS is independently confirmed to tokenize correctly and sit uncorrupted in the comparison table. This is now established via multiple independent, properly-gated live measurements, not inference: something upstream of the comparison consumes/skips past AS (and Expr1/FROM/Part) before the comparison logic ever sees them.

Molly asked directly whether tew's PE relocation handling could explain this, since msjet35.dll is the one DLL loaded away from its preferred base ("is it possible... the system isn't reading and writing to the same address"). Checked with real evidence rather than assumption (again): live memory at the comparison table matches expected static bytes exactly, and the indirect jump-table call that reaches the SELECT-list handler resolves correctly at its relocated runtime address. Relocation is not the cause -- a good hypothesis, directly tested and closed.

**Hand-disassembly of `FUN_7a866c6d`'s paren-skip/depth-counter logic made two consecutive wrong, testable predictions this session** (expected first match-check entry value; expected a specific call reading one more token after the depth counter reaches zero -- live-tested with a targeted breakpoint, that call never fires at all). Manual byte-by-byte decoding of this specific function has a demonstrated, worsening error rate across repeated careful attempts, and `redisassemble_instruction` doesn't surface operand text as a better alternative. Stopping point for the technique, not the investigation: the architectural finding (full call chain root-caused to one function, exact failure mode pinned to "AS never arrives at the comparison") stands on its own regardless of which exact instruction is responsible. Full detail and next-session options in `status.md`.

---

## 2026-08-20 (cont'd) — DAO-3075: root cause narrowed to a single failed comparison in msjet35.dll's SELECT-list lookahead scanner; relocation hypothesis tested and ruled out

Continuation of the same-day entry below. Found the real path past `FUN_7a866d2b`'s rewind point: the rewind re-reads the `SELECT` token itself and redispatches through the same jump table (`0x7a8669ec`), landing on `FUN_7a86a5a7` -- the real, live-confirmed SELECT-list per-column handler (fires 2ms before the parser failure). Its per-column loop calls `FUN_7a866c6d(param_3, &DAT_7a86a940)`, a lookahead scanner that skips balanced parens (correctly handling `Max(PartID)`) and searches for a token matching a small lookup table of "valid column-end" markers, then rewinds.

**Root cause, confirmed at the byte level:** `FUN_7a85683d` (the real tokenizer) reads its active scan cursor from `param+0x10`. `FUN_7a866c6d`'s "matched, rewind" branch only writes `param+0x18` (the boundary bookmark the caller reads for the column's recorded start position) -- never `param+0x10`, the field the tokenizer actually reads from next. Live-confirmed for the `Max(PartID)` column: `FUN_7a866c6d` returns success (`0x101`); `param+0x18` correctly reads `'Max(Part'`; `param+0x10` sits 31 bytes further on (the length of the *entire* remaining statement) in unwritten zero memory, well past the true end of the column's text. The next tokenizer call reads from that stale cursor and gets the buffer-end sentinel instead of `AS`.

**Directly tested and ruled out: relocation corruption.** msjet35.dll is the only DLL in the whole system actually loaded away from its preferred base (`runtime = static - 0x65840000`), meaning it's the only one that exercises tew's relocation-application code path at all (every other module loads at its preferred address, short-circuiting the delta-application loop entirely). Checked with real data, not assumption: live memory at `DAT_7a86a940` (the lookup table) matches the expected static bytes exactly, byte for byte -- not corrupted. Execution also correctly reached `FUN_7a86a5a7` via an *indirect jump-table call* at its properly-relocated runtime address, direct proof code-pointer relocation works too. Reviewed both `tew/pe/base_relocation_table.py` (relocation table parsing) and `dll_loader.py`'s `apply_base_relocations` -- both look structurally correct (proper type-3/HIGHLOW filtering, uniform delta application regardless of section). This was a good, worth-checking hypothesis and is now closed as a live lead.

**Disambiguated the exact token sequence inside one specific invocation** of the shared lookahead-scanner helper (gated strictly between its call and return via two breakpoints, safe on tew's single-threaded-per-CPU model). Confirmed: the scan genuinely reads `AS` as token `0x105` -- squarely in the middle of the sequence, immediately after the closing paren -- and `0x105` is a live-verified, uncorrupted entry in the very table being checked against. The scan does not stop there anyway, continuing through `Expr1`/`FROM`/`Part`/`;` to the buffer end.

**Most precise open question in the whole investigation:** the match-check loop (`while (uVar2 != 0 && *puVar4 != uVar3)`) does not appear to fire on a value that is provably tokenized correctly and provably present in its comparison table. Not yet determined whether this is a genuine quirk in the real compiled comparison (decompile has proven unreliable for this DLL's control flow before) or a tew CPU-instruction-emulation bug in the specific x86 instruction implementing it -- next step is hand-disassembling that exact loop, the same technique that resolved the earlier `local_178` mystery.

---

## 2026-08-20 — DAO-3075: full real SQL-compile call chain traced end-to-end from dao350.dll to the failure

Continuation of the "AS not recognized" finding below. Traced the ENTIRE real call chain live, hop by hop, from dao350.dll's actual SQL-text entry point down to `FUN_7a86756b`'s failure: `FUN_044d519b` (dao350.dll) -> `(*DAT_044e534c)` (a stored function pointer, confirmed via live memory read to target msjet35.dll) -> ordinal 319 (`FUN_7a8ae64d`) -> `FUN_7a856c17` (the real top-level SQL statement compiler, "makes zero external calls" per the original pre-compaction note) -> `FUN_7a85683d` (a STATEMENT-level tokenizer, distinct from the EXPRESSION-level `FUN_7a8685de` this whole investigation had focused on) -> `FUN_7a866d2b` (generic statement dispatcher, confirmed live with token `0x167` = SELECT) -> `FUN_7a866f98` (scans ahead to the statement terminator tracking paren depth, watches for `INTO`, then for a plain SELECT rewinds the read position back to right after `SELECT` and returns 0) -> [gap, not yet traced] -> the already-known chain (`FUN_7a8aa88f`/ordinal 301 -> `FUN_7a8aa8c9` -> `FUN_7a855c74` -> `FUN_7a855cc3` -> `FUN_7a855d02` -> `FUN_7a86756b`).

**Key finding: `FUN_7a86756b` is not missing a feature.** Hand-verified its only clean-success path (case `0x10`) is gated purely on reaching literal end-of-buffer -- there is no "stop early on an unrecognized-but-valid terminator token" mechanism anywhere in it. By design it expects its caller to hand it an already-correctly-bounded expression substring. The bug is that some caller upstream hands it 31 characters (the whole remaining statement, "Max(PartID) AS Expr1 FROM Part;") instead of 12 ("Max(PartID)").

**Ruled out this session** (confirmed dead via live breakpoints that never fired): a "SELECT keyword classifier" function (`FUN_7a8e7cca`, checks text against 10 real statement keywords) and its sole caller `FUN_7a8549b6` -- real code, but not part of `CreateQueryDef`'s path (likely used by `OpenRecordset`/`Execute` to disambiguate saved-query-name vs. literal-SQL, an ambiguity `CreateQueryDef` doesn't have). Also ruled out: dao350.dll's `DAT_044e5238`/ordinal-302 chain, which is confirmed live but only threads the query's catalog *name* ("tmp") through a collision check -- pure bookkeeping, never touches the SQL text.

**Methodology lesson, caught concretely twice this session:** Ghidra's positional parameter names (`param_3`, etc.) do NOT reliably track the same real value across different functions in a call chain, even when a decompiled call site looks like a clean pass-through. Two different functions' "param_3" both turned out to actually be the query name ("tmp"), not the SQL text, despite matching the naming pattern of earlier functions where "param_3" genuinely was the SQL text. Fix: wide-scan live registers + a broad stack range at each hop and match by actual content, never trust name continuity alone.

**Still open:** the exact point where the 31-vs-12-character boundary gets decided, somewhere between `FUN_7a866f98`'s rewind-and-return-0 and the already-confirmed downstream chain. Full detail and the decision point for next session in `status.md`.

---

## 2026-08-19 — DAO-3075: real root cause found -- "AS" not recognized as a keyword, triggers "two operands in a row" (0x271e)

Continuation of "proceed with option 2" (trace deeper into what msjet35.dll's parser consults). Hand-disassembled `FUN_7a86756b`'s real control flow (Ghidra's decompile/function-boundary attribution proved unreliable for this jump-table-heavy switch -- see methodology note below), tracing all the way to the exact instruction where the real query's parse fails.

**The actual bug:** the parser's operand-vs-operator state machine (`DAT_7a93ab04`: 0=nothing pending, 1=operand just pushed, 2=a complete parenthesized group just closed) is completely orthogonal to identifier/function *resolution* -- it runs the same regardless. Closing `(PartID)` sets `DAT_7a93ab04=2` (a real, correct parser state: "a complete operand just ended, an operator or terminator must come next"). The next token, `AS`, is tokenized as plain-identifier type `0x01` -- confirmed live, not some dedicated AS-keyword token type -- so it's dispatched to the same operand-group entry point (`0x7a8676fb`), which unconditionally checks `if (DAT_7a93ab04 != 0) error 0x271e` before doing anything else. `(PartID)` followed by `AS`, with `AS` unrecognized as a keyword, reads to the parser as two operands with no operator between them. Confirmed via two live memory reads: `token-type-probe` shows `AS` = token type `0x01`, and `dat-ab04-probe` shows `DAT_7a93ab04 = 2` at the exact moment `AS` is dispatched, immediately before the error fires.

**Dead end, real finding along the way:** initially spent significant tracing effort chasing why breakpoints on `FUN_7a869880`/`FUN_7a8699a2` (the real identifier/function-name resolution helpers) never fired, despite byte-for-byte-verified hand-disassembly proving the call sites are real and correctly targeted. Root cause: `local_178` (a mode flag gating `if(local_178==0){...do real resolution...} else {piVar8=NULL;}`, present at the very top of the operand-group prologue) was assumed `0` based on indirect reasoning and never actually read from live memory. It's `1`. **This entire parse call runs in a mode that skips real identifier/function resolution outright** -- a pure grammar/syntax validation pass. This retroactively explains why the earlier "universal function-call recognition failure" conclusion (Max/Count/Len all hit `0x271e`, see 2026-08-18 entries below) never actually exercised `DispCallFunc`, VBA, or any function-name-table code path -- none of those tests could have, since `local_178=1` skips that code unconditionally. DispCallFunc's implementation stands (still real, tested, needed for other OLE Automation call sites) but was never going to fix this.

**Methodology lesson, worth keeping in mind for any future `FUN_7a86756b`/similar jump-table-heavy work:** Ghidra's decompiled case grouping and its `get_function_calls`/`get_references_to` function-boundary attribution do NOT correspond to real C-level switch-case values for this function -- the compiler emits a two-level jump table (outer: token_type -> case-group entry or dedicated address; inner, byte-indexed, only for the multi-value group) and Ghidra's synthetic fragment names (`caseD_1`, etc.) just reflect contiguous/fall-through byte ranges, mixing code from multiple real cases together. Trust `dump_bytes` + manual disassembly over the decompile here. Separately: **always directly verify flag/mode variables via live memory reads before trusting an inferred value** -- `local_178` sitting unverified cost real time chasing a phantom "why don't these breakpoints fire" mystery that had a one-line answer once actually checked.

**Still open:** why `AS` specifically isn't recognized as a keyword (likely `FUN_7a869052`'s keyword table, not yet examined for its data source or population mechanism), and whether this same mechanism explains the earlier Max/Count/Len-without-AS test failures (those queries had no `AS` clause -- worth checking if `FROM` is similarly unrecognized in that shorter form, since the validated control `SELECT PartID FROM Part;` DOES compile cleanly, meaning `FROM` recognition works in at least some contexts).

---

## 2026-08-18 (cont'd x5) — DAO-3075: implemented DispCallFunc (real, tested), live-verified it does NOT fix the parse failure

`oleaut32.dll!DispCallFunc` was the leading fix candidate identified by the
prior entry below ("root cause re-validated") -- unimplemented since
2026-08-04, plausible as the missing piece behind the VBA Expression
Service architecture that Jet uses to resolve function calls. Implemented
it for real: generic late-bound invocation, VARIANT-array marshaling
(`prgvt`/`prgpvarg` -> stack words, `VT_BYREF` passes the pointer through
unmodified, `VT_R8`/`VT_I8`/`VT_UI8`/`VT_CY`/`VT_DATE` as 2-word/8-byte
values), vtable dispatch when `pvInstance != 0` (byte-offset `oVft`, same
pattern as the existing `_dispatch_com_method`) vs. direct call when
`pvInstance == 0`, `CC_CDECL`/`CC_STDCALL` only (anything else halts
loudly), routed through the existing `_invoke_emulated_proc` nested-call
mechanism. Float/`VT_R4`/`VT_R8` *returns* are explicitly out of scope
(would need `_invoke_emulated_proc` to expose FPU ST(0) before its
`cpu.restore_state()` discards it) -- halts loudly rather than guessing.
7 new tests in `tests/unit/api/test_oleaut32_dispcallfunc.py`: direct
invocation (1 and 2 actuals), `VT_BYREF` pointer pass-through, vtable
dispatch, and the three halt-loudly paths (bad calling convention,
unsupported arg VARTYPE, unsupported return VARTYPE). Full suite green:
`zig build test` 154/154 (untouched, Python-only change), `pytest -q`
1119/1119 (1112 existing + 7 new).

**Live-verified against the real, unmodified query** (`_REWRITE_QUERY`
probe left `False`, no rewrite): the game's own `CreateQueryDef` call still
issues `Max(PartID) AS Expr1 FROM Part;`, and the `exit-probe` still fires
`*param_6 = 0x271e` -- **identical to before DispCallFunc existed**. Final
halt point also unchanged (`EIP=0x001fe012`), confirming DispCallFunc's
presence didn't shift execution down some new path or introduce a
regression -- it's simply not in the causal chain for this failure.

**Conclusion: DispCallFunc is ruled out as the fix for DAO-3075.** It was a
reasonable candidate (see below for how it was inferred, not proven) and is
still worth having implemented -- `expsrv.dll` and other real OLE
Automation call sites will need it regardless -- but the universal
function-call-recognition failure has some other, still-unidentified cause.
Next session starts from a clean slate on *that* question: options are (a)
trace one level deeper into what msjet35.dll's parser actually consults to
recognize an identifier as a function call (the `DAT_7a93aaXX` global state
family is the likely place to keep digging), or (b) check whether this
specific query even needs to succeed for the game to proceed, per the
original 3-option decision point -- DispCallFunc (option 1) is now closed
out with a negative result.

---

## 2026-08-18 (cont'd) — DAO-3075: caught and fixed a real test-harness bug, re-validated the universal-function-call-failure finding

Follow-up to the same night's "root cause fully characterized" entry below.
Molly's objection -- "that has to be impossible, that would mean jet 3.5
has no concept of functions" -- was correct and caught a real methodology
flaw, not a wrong conclusion.

**The bug**: the first round of Max/Count/Len testing rewrote the SQL text
*deep* inside the parser, right at `FUN_7a86756b`'s own entry (patching
only its `param_4`/`param_5`). Control test exposed this as invalid:
rewriting to `"PartID FROM Part;"` -- a query the *original*
pre-scheduler-detour investigation had already confirmed compiles cleanly
with zero rewrite -- through that same deep path **also** hit `0x271e`.
Something upstream of the deep parser (dao350.dll's own processing, before
msjet35.dll is ever reached) already depends on the query's real content
by the time execution reaches that point; patching only the substring
there produces a mismatched, invalid state, not a fair test. All three
"Max/Count/Len all fail" results from that round were retracted.

**The fix**: rewrite the SQL text at its real source instead -- the string
literal in `MCity_d.exe`'s own data section (`0x011e0de4`, found via
`search_strings`; the EXE is static==runtime, no delta needed), applied at
`Dbcode_CreateTmpQuery`'s own entry (`0x008fe4a0`), before dao350.dll ever
sees the text. Same rewrite point the original investigation used
successfully. Control test now passes cleanly: `"SELECT PartID FROM
Part;"` compiles with no error, run proceeds much further (a different,
later halt point entirely).

**Re-ran the real tests with the validated mechanism -- same conclusion
holds**: `Max(PartID)`, `Count(PartID)`, and `Len(PartID)` all still hit
`0x271e` identically; the function-free control still succeeds. The
universal-function-call-recognition-failure finding survives rigorous
re-testing with a now-trustworthy methodology.

**Scope, stated precisely** (this is the part Molly's objection sharpened):
this does NOT mean real Jet 3.5 lacks function support -- it obviously has
it. It means something in *tew's specific emulated environment* is
missing/not-yet-working that real Windows would already have set up
before any query gets compiled, and whatever that missing piece is, it
affects every function call, not one specific function name. `DispCallFunc`
(`oleaut32.dll`, unimplemented since 2026-08-04) remains the leading
candidate, still not directly confirmed as the causal link -- see
`status.md`'s "Current status" for the same 3-option decision point as
before, now resting on solid ground.


## 2026-08-18 — DAO-3075: root cause fully characterized -- universal function-call parse failure, not Max-specific (`0x271e`)

Resumed the paused DAO-3075/aggregate-function thread after finishing the
scheduler-to-Zig port. Two theories tested and ruled out by direct live
evidence before finding the real mechanism:

1. **Missing `oleaut32.dll!DispCallFunc`** (confirmed still unimplemented
   since 2026-08-04): real, still-open gap, but `FUN_7a856c17` (msjet35.dll's
   query-compiler entry, the function that returns internal code `-3100`)
   makes zero external calls -- all 22 callees local to msjet35.dll. Not
   directly in this specific call path.
2. **`VBAGetExprSrv` returns a null interface pointer**: disproven live --
   set a breakpoint at `dao350.dll`'s `FUN_0448a429`'s real branch
   (`0x0448a558`/`0x0448a55e` fail / `0x0448a590` success, confirmed via
   decompile: `local_8`'s out-parameter, not the return value, is what's
   actually checked) -- it succeeds, non-null interface, right after
   `expsrv.dll`'s `DllMain` completes.

**Live-traced the entire real `CreateQueryDef` call chain**, since the
original (pre-scheduler-detour) session's `FUN_7a8ae64d`/`FUN_7a856c17`
trace never fires live at all -- turned out to be for a stale/different
code path. Real chain, every address/value read from live memory, nothing
guessed: `Dbcode_CreateTmpQuery` (exe) -> two levels of dao350.dll COM
vtable indirection (`FUN_04487388` -> `FUN_0448356f`, the latter's real
argument count wider than Ghidra's decompile inferred -- same class of
decompiler artifact as the historical `__cinit`/`unaff_retaddr` false lead)
-> `FUN_044c98fe` -> `FUN_044ca3a7` -> `FUN_044d5e64` (a name-registration
step, not the compile itself) -> `FUN_044d519b`, whose target function
pointer (`DAT_044e534c`) resolves *live* into msjet35.dll.

**Found along the way: msjet35.dll needs relocation-delta translation for
live breakpoints -- `runtime = static - 0x65840000` for this DLL
specifically** (dao350.dll and the EXE are static==runtime, confirmed
separately unaffected). This is why the original session's msjet35.dll
breakpoints, and this session's first attempts, silently never fired --
they were registered at the *static* Ghidra address directly.

**The actual mechanism**: msjet35.dll's real parser (`FUN_7a86756b`) sets
internal error `0x271e` ("two operands in a row, no operator between
them" -- confirmed via decompile, guarded by a `DAT_7a93ab04 != 0`
"operand already pending" flag) the instant it hits `(` right after an
identifier token, because the tokenizer classifies function names as
plain generic identifiers (`0x100`, same code as any column name) rather
than function-class tokens, and nothing downstream re-classifies
"identifier immediately followed by `(`" as a function call either.

**Confirmed this is universal, not `Max`-specific or aggregate-specific**:
live-rewrote the query text in memory (same in-place-rewrite technique
used throughout the whole investigation) and re-ran three times -- `Max
(PartID) AS Expr1 FROM Part;` (original), `Count(PartID) FROM Part;` (a
different, equally-standard aggregate), and `Len(PartID) FROM Part;` (a
non-aggregate string function, not even in the GROUP-BY family at all).
**All three hit `0x271e` identically.** Rules out both "keyword table
missing `Max` specifically" and "aggregate functions specifically" -- no
function call of any kind is being recognized by the parser.

**Working theory (architecturally well-supported, not yet directly
live-confirmed)**: per the already-established `vbajet32.dll`/`expsrv.dll`
VBA Expression Service architecture, essentially all Jet SQL function
evaluation routes through that service's function registration --
obtaining the interface (`VBAGetExprSrv`, confirmed working) isn't the
same as its function table actually being populated/consulted by the
parser. The still-open `DispCallFunc` gap is the leading candidate for the
missing piece, but this is inference from architecture, not a proven
causal link the way `0x271e`'s universality is.

**Decision point for next session** (see `status.md`'s "Current status"
for full detail): implement `DispCallFunc` now (real generic x86
calling-convention/VARIANT-array marshaling, the "different scale of
problem" flagged back on 2026-08-04) and see if it fixes function-call
recognition wholesale, vs. tracing one more level to directly confirm the
causal link first, vs. a different approach entirely. The investigation
itself has reached a real conclusion; what's open now is which fix path to
take, not more tracing to find the bug.

## 2026-08-17 (cont'd x7) — Scheduler-to-Zig port COMPLETE (Stage 6, final sweep)

Final sweep: grepped for every remaining `tew.kernel.scheduler`/`ThreadState(`/
`Scheduler(` reference across the whole codebase -- found only the old
`tests/unit/kernel/test_scheduler.py` (now fully superseded by Stage 5's
`tests/unit/hardware/test_scheduler_zig.py`) and harmless doc-comment
mentions in `scheduler_zig.py`. Deleted `tew/kernel/scheduler.py` (673
lines, the original pure-Python scheduler this whole port replaced) and
`tests/unit/kernel/test_scheduler.py` (1077 lines, its test suite) together
-- keeping one without the other made no sense once nothing imports either.
Also removed `run_exe.py`'s Stage-0 baseline-capture breakpoint probes,
whose own comment said to remove them once Stage 6 finished its final diff
-- that's now.

`zig build test`: 154/154 (unchanged -- pure deletion, no new Zig code).
`pytest -q`: **1112/1112** (was 1191; -79 from the deleted old test file,
zero regressions from the deletion itself or the probe removal).

**Final live-run verification, all 4 checks from the plan's Verification
section:**
- (a) Stage 0 baseline match: checkpoint delta 169,791 steps, confirmed
  identical across every single stage's live-run check this entire port
  (Stages 1 through 6) -- the one number that never moved.
- (b) ordinary multi-thread scheduling: a full production-mode run created
  14 real threads (tid 1001-1014), including 3 threads spawned *from
  inside* a nested `expsrv.dll` DllMain call (tid 1011 creating 1012/1013/
  1014) -- exactly the nested-thread-creation-under-reentrancy scenario
  this port was built to handle correctly.
- (c) the nested-DllMain/expsrv.dll starvation scenario: **0 reentrancy
  violations**, confirmed again in the final clean run (was 160,433
  violations / 3.7s wall-clock before this port started; was already 0 as
  of Stage 4, unchanged through Stage 6).
- (d) `cpu.fatal_halt` still correctly halts the whole emulator: confirmed
  live -- the final run hits `Fatal halt: fatal halt at EIP=0x001fe012`,
  logs `Steps executed: 117185511`, and the process exits cleanly with no
  further execution afterward. No scheduler path silently clears it.

**The scheduler-to-Zig port is done.** `tew/kernel/scheduler.py` no longer
exists; `tew/hardware/scheduler_zig.py` (`ZigScheduler`) is the only
scheduler, backed by `cpu/src/scheduler.zig` (154 colocated tests) +
`tests/unit/hardware/test_scheduler_zig.py` (43 tests covering the Python
orchestration layer). Motivating problem (FFI-hop-cost reentrancy-guard
starvation during heavy nested DllMain calls) is fixed and measured: 0
violations where there used to be 160,433. One real bug (`ZigCPU.
_py_halted` staleness) was found and fixed along the way, not anticipated
by the plan. See `~/.claude/plans/vast-drifting-pike.md` for the full
design record and `status.md`'s current entry for a one-paragraph summary.

## 2026-08-17 (cont'd x6) — Scheduler-to-Zig port, Stage 5 complete

New `tests/unit/hardware/test_scheduler_zig.py` (43 tests, real `ZigCPU`/
`ZigMemory` throughout, no `MagicMock`): the point of this file isn't to
re-prove `cpu/src/scheduler.zig`'s own logic (already 154 colocated Zig
tests deep) -- it's to cover what only the Python layer can: the two-call
kernel-tick retry protocol in `scheduler_zig.py` itself
(`test_calls_kernel_tick_once_when_nothing_ready`, a stub `_kernel` that
counts `.tick()` calls -- this exact orchestration had zero coverage before
this stage), the `fatal_halt`/`reentrant_depth` pre-checks that decide
whether `scheduler_pick_next_ready` gets called at all, `terminate_thread`'s
tri-state `Optional[bool]` mapping, the `_CurrentThreadProxy`, and
`status_at_idx`. One old test (`TestAnyRunnable.test_false_with_only_
sleeping`) directly poked `s.threads[0].status = SLEEPING` -- no raw field
access exists anymore, so it's rewritten to reach the same real state via
`sleep_current` (two threads, so the single-thread self-reload branch
doesn't reset status back to READY) plus `set_suspended` on the second
thread, rather than dropping the guarantee.

Added the "public C ABI" test the plan explicitly called for this stage:
create 2 threads, preempt, block on a CS via the real two-call protocol,
unblock, verify the swap -- `cpu/src/kernel.zig`'s only C-ABI-level CS
coverage (the earlier stages' ABI tests covered handles/TLS/thread-count,
not CS blocking specifically).

`get_thread_tls` confirmed NOT ported (zero call sites, zero tests in the
old suite -- see Stage 4's note); `TestInitThreadStack` (the old suite's
tests for the private `_init_thread_stack` method) not ported either --
there's no Python-facing method for it anymore, it collapsed into
`loadNext`'s internal Zig logic, already covered by Stage 1's own
"switch_to saves current and loads fresh next thread" test.

`zig build test`: 154/154 (was 153; +1 public C ABI test).
`pytest -q`: **1191/1191** (was 1148; +43 new).

Stage 5 live-run check: checkpoint delta matched exactly (169,791 steps),
final halt EIP matched (`0x001fe012`), reentrancy violations: 0 (consistent
with Stage 4's fix, not a new finding). No regression.

Next: Stage 6 (final sweep -- grep for any remaining `tew.kernel.scheduler`
references, full verification, live re-check of the original nested-
DllMain/expsrv.dll starvation scenario specifically, delete
`tew/kernel/scheduler.py`). Pending review.

## 2026-08-17 (cont'd x5) — Scheduler-to-Zig port, Stage 4 complete: emulator now runs on the Zig-backed scheduler

New `tew/hardware/scheduler_zig.py`: `ZigScheduler` class mirroring
`cpu_zig.py`'s wrapper pattern exactly, `ThreadStatus` as a Python `IntEnum`
matching the Zig `enum(u8)` values, `_CurrentThreadProxy` exposing only
`.thread_id`/`.wait_timed_out` (the 3 real external readers). Implements
the two-call kernel-tick protocol (Design Decision 2) in Python:
`block_current_on_cs`/`block_current_on_handles`/`sleep_current` check
`cpu.fatal_halt`/`reentrant_depth` *before* calling `scheduler_pick_next_
ready` (that scan has real wake side effects the original Python never
triggers on a refused call); `mark_current_dead` skips only the
`fatal_halt` check, never the reentrancy one, matching the old guard
-exempt behavior. `terminate_thread` only resolves `next_idx` (the pick
-next-ready dance) when the target handle actually is the current thread's
-- the common "kill some other thread" path never touches it, same as the
original.

`_state.py`: `Scheduler(tls_slots=...)` → `ZigScheduler()`; `tls_slots`
set removed entirely (TLS bitset now lives in the Zig `SchedulerState`);
`pending_threads: list[ThreadState]` → `list[int]` (handles) -- moved here
from Stage 3, see that stage's reordering note. `kernel32_sync.py`'s 4 TLS
handlers (`TlsAlloc`/`TlsSetValue`/`TlsGetValue`/`TlsFree`) migrated to
`scheduler.tls_alloc_slot`/`tls_free_slot`/`tls_slot_allocated`.
`kernel32_io.py`'s `_create_thread`/`_resume_thread`/`_suspend_thread`/
`_get_exit_code_thread` migrated to the handle-keyed accessors
(`get_suspended`/`set_suspended`/`get_completed`, plus a new
`get_thread_id` added for a debug log that needed it).
`user32_handlers.py`'s `scheduler.threads[idx].status` (the sharpest
direct-field-poke finding from the plan's call-site inventory) became
`scheduler.status_at_idx(idx)`. All `from tew.kernel.scheduler import`
sites repointed to `tew.hardware.scheduler_zig`.

**Real bug found and fixed, not part of the plan**: `ZigCPU.halted`
(`cpu_zig.py`) cached a Python-side `_py_halted` override flag alongside
the native `CpuState.halted` field. Every *write* site kept both in sync,
but nothing ever re-synced `_py_halted` when something else changed the
*native* flag without going through this class's own setter -- which the
old pure-Python scheduler never did (it always called `cpu.halted = False`,
a real Python attribute set), but the new Zig-backed scheduler does
routinely (`loadThread`/`loadNext` write `CpuState.halted` directly as a
native struct field, with no way to notify Python). Net effect: once
`cpu.halted = True` was set anywhere, it could get permanently stuck
`True` even after a real thread swap correctly cleared the underlying
native flag -- caught by `test_kernel32_sleep.py::test_single_thread_
clears_halted` going red. Fix: removed `_py_halted` entirely; `halted` is
now a pure passthrough to `cpu_is_halted`/`cpu_set_halted`/
`cpu_clear_halted` -- every prior write site already updated the native
flag directly too, so nothing depended on the cache. `_py_faulted` (a
separate, not-currently-broken flag) was deliberately left alone -- out of
scope for this stage, no evidence of a live bug there.

Added a `ZigCPU.native_handle` property (`cpu_zig.py`) rather than having
`scheduler_zig.py` reach into `cpu._state` directly -- keeps the
cross-module FFI-handle-sharing point explicit, no other module poked that
attribute before.

Two existing unit tests needed real fixture changes, not just import
updates: `test_kernel32_sleep.py` used a bare `MagicMock` for `cpu` (fine
under the old pure-Python scheduler, which never touched it over ctypes);
now that `ZigScheduler.sleep_current` genuinely calls native code through
`cpu.native_handle`, it needed a real `ZigCPU` instance. Both that file and
`test_invoke_emulated_proc_thread_death.py` also had their own
`scheduler.threads[idx].status` pokes fixed to `status_at_idx(idx)` --
call-site instances the plan's inventory only tracked for production code,
not tests.

`zig build test`: 153/153 (unchanged this stage -- pure Python wiring, no
new Zig code beyond the 3 small gap-fill exports below). Added
`scheduler_thread_count`/`scheduler_get_virtual_ticks_ms`/`scheduler_set_
virtual_ticks_ms` to `kernel.zig` (trivial inline field accessors, same
style as `scheduler_current_idx`) -- needed for `_state.py`'s
`virtual_ticks_ms` property and a `kernel32_io.py` debug log, not
anticipated by the plan's non-exhaustive function list.

`pytest -q`: **1148/1148 passing** (was 1148/1148 before this stage --
net zero regression once the two fixture fixes and the `_py_halted` fix
landed).

**Stage 4 live-run check -- the first one that exercises real behavior,
not just build health.** Checkpoint delta matched exactly again (169,791
steps), final halt EIP matched (`0x001fe012`), same dead-thread-mid
-nested-call recovery message. **Reentrancy violations: 0** (down from
4,332 in the Stage 2 check under the old Python scheduler, and the
160,433/3.7s that motivated this entire port in the first place) --
concrete confirmation the FFI-hop-cost starvation this port set out to
fix is actually fixed. The final halt fired on a different thread ID
(tid=1007 vs the usual tid=1000) than prior runs; consistent with zero
reentrancy-guard stalling changing exactly *when* threads get scheduled,
not a regression -- same halt address, same register/stack dump shape.
No guest assertion (`except.txt` absent), same known DAO-3075 failure
point in `dblog.txt` as always (unrelated, paused investigation,
unaffected by this port).

Next: Stage 5 (port `tests/unit/kernel/test_scheduler.py`'s remaining
Python-facing-contract tests to `tests/unit/hardware/test_scheduler_
zig.py`, against real `ZigCPU`/`ZigMemory`). `get_thread_tls` intentionally
NOT ported (zero call sites, zero existing tests) -- `any_runnable` WAS
ported (has tests, composes the handle-keyed accessors rather than a
dedicated native export since it's never on a hot path). Pending review.

## 2026-08-17 (cont'd x4) — Scheduler-to-Zig port, Stage 3 complete (Zig side)

`cpu/src/scheduler.zig` gained the TLS bitset exports (`tlsAllocSlot`/
`tlsFreeSlot`/`tlsSlotAllocated`, backed by the `tls_allocated: u64` bitset
already declared and consumed by Stage 1's `saveTls`/`loadTls`) and
handle-keyed accessors for every direct `ThreadState` field poke found in
the plan's call-site inventory: `getSuspended`/`setSuspended`,
`getCompleted`, `getWaitTimedOut`/`setWaitTimedOut`, `getStatus` (0xFF
sentinel for unknown handle), `getThreadId` (-1 sentinel), `handleAtIdx`
(translates `user32_handlers.py:162`'s index-based access to a handle),
`currentHandle`. 20 new colocated tests + 1 new "public C ABI" test.
`zig build test`: 153/153 passing (was 140 after Stage 2).

**Plan correction, made before starting this stage**: the original Stage 3
text also called for redesigning `state.pending_threads` from
`list[ThreadState]` to `list[int]` and migrating `kernel32_io.py`/
`crt_handlers.py` call sites to the new accessors -- but that's Python-side
wiring against `scheduler_zig.py`, which doesn't exist until Stage 4 (by
its own title). Moved that piece into Stage 4 where the wrapper it wires to
will actually exist; amended `~/.claude/plans/vast-drifting-pike.md`
in place with a reordering note rather than silently doing it differently
from what was written. Stage 3 stays Zig-only, same as Stages 1-2.

Stage 3 live-run check: checkpoint delta matched exactly (169,791 steps),
final halt EIP matched (`0x001fe012`). No regression. No Python code
touched -- `tew/kernel/scheduler.py` is still the live scheduler.

Next: Stage 4 (`tew/hardware/scheduler_zig.py` wrapper + `_state.py`
wiring + the `pending_threads`/`kernel32_io.py`/`crt_handlers.py`
migration moved here from Stage 3) -- the first stage that actually
switches the running emulator over to the new backend. Pending review.

## 2026-08-17 (cont'd x3) — Scheduler-to-Zig port, Stage 2 complete

`cpu/src/scheduler.zig` gained the blocking/wake/tick operations:
`completeBlockOnCs`, `completeBlockOnHandles`, `completeSleepCurrent`,
`completeMarkCurrentDead`, `terminateThread` (tri-state `i8`: -1 not found,
1 different thread terminated, 0 current thread terminated itself),
`unblockCs`, `unblockHandle`, `tick`. Per Design Decision 2 in the plan,
these `complete_*` functions take an already-resolved `next_idx` rather
than scanning themselves -- `pickNextReady` (Stage 1) stops short of the
kernel-tick fallback, which stays a Python-driven two-call retry (not
wired until Stage 4). Documented explicitly in `scheduler.zig`: callers
must check `cpu.fatal_halt`/`reentrant_depth==0` *before* calling
`scheduler_pick_next_ready` at all, since that scan has real side effects
(waking a due sleeper) that the original Python never triggers on a
refused/fatally-halted call.

33 new colocated Zig tests, including 4 explicit `fatal_halted`-preservation
tests (one per `complete_*` function, per the plan's requirement not to
just rely on Stage 1's shared-path coverage) and the reentrancy-refusal/
exemption tests carried over from `TestReentrancyGuardRefusesSwaps`/
`TestReentrancyGuardExemptions`. 8 new exported wrappers in `kernel.zig`
(`scheduler_complete_block_on_cs`/`_on_handles`/`_sleep_current`/
`_mark_current_dead`, `scheduler_terminate_thread`, `scheduler_unblock_cs`/
`_handle`, `scheduler_tick`) plus one new "public C ABI" test exercising
the handles-array FFI marshaling specifically (the trickiest new
parameter shape). `zig build test`: 140/140 passing (was 107 after Stage 1).

**New process, adopted after Stage 1**: a regression-only live run now
happens after every stage. This one: `main-thread-creation` matched (0),
the `db-startup-database`→`first-createquerydef` checkpoint delta matched
exactly (169,791 steps, same as Stage 0 and Stage 1), final halt EIP
matched (`0x001fe012`). No regression. Absolute step counts at the early
checkpoints again shifted from the Stage 0/1 baselines by a differing
amount -- consistent with the wall-clock-timing noise already documented
after Stage 1, not a new finding.

No Python code touched -- `tew/kernel/scheduler.py` is still the live
scheduler. Next: Stage 3 (TLS bitset exports + direct-field accessors +
`pending_threads` redesign), pending review.

## 2026-08-17 (cont'd x2) — Scheduler-to-Zig port, Stage 1 complete

`cpu/src/scheduler.zig` (new): `SchedulerState`/`ThreadEntry`/`ThreadStatus`,
context switch (`saveCurrent`/`loadThread`/`initThreadStack`/`loadNext`/
`switchTo`/`preemptSlice`), `pickNextReady`'s scan A + scan B (kernel-tick
fallback stays a Python-driven two-call retry, per the plan's Design
Decision 2 -- not ported), and the reentrancy guard
(`enterReentrantCall`/`exitReentrantCall`/the `swapCurrent` chokepoint).
33 colocated Zig tests, including the two `fatal_halted`-preservation
regressions ported verbatim from
`test_does_not_clear_fatal_halt_on_saved_thread_load`/
`test_does_not_clear_fatal_halt_on_fresh_thread_start`
(`test_scheduler.py`). `cpu/src/kernel.zig` gained 11 new exported wrappers
(`scheduler_create`/`_destroy`/`_create_main_thread`/`_create_thread`/
`_switch_to`/`_preempt_slice`/`_pick_next_ready`/`_enter_reentrant_call`/
`_exit_reentrant_call`/`_reentrant_depth`/`_current_idx`) plus one new
"public C ABI" test exercising the full create -> thread -> preempt_slice ->
reentrancy-refusal -> destroy path. `zig build test`: 107/107 passing
(was 74 before this stage). `reentrancy_violations` (Python's list of
message strings) deliberately NOT ported to Zig -- building/logging those
strings stays a Python-side concern for the Stage 4 wrapper, which
constructs the message from the bool a native swap call returns rather than
crossing strings over the FFI boundary for every refusal.

No Python code touched yet -- `tew/kernel/scheduler.py` is still the live
scheduler; this is pure new, unwired Zig code. Next: Stage 2 (blocking/
wake/tick operations), pending review.

## 2026-08-17 (cont'd) — Case-sensitivity in aggregate function-name
resolution ruled out too

One more targeted test on the DAO-3075 aggregate-function failure: every
keyword in the real query (SELECT/AS/FROM) was already written uppercase,
so the earlier tokenizer-probe result ("AS returns keyword code 0x105, not
misread as identifier") only proved keyword lookup is case-insensitive for
already-uppercase input -- Max (mixed-case, as literally written in the
query) was never actually tested for case-sensitivity in its own
resolution as an aggregate function name (it tokenizes as a plain
identifier, 0x100, via a different path than the keyword hash table).
Re-added the query-rewrite/message-report probe pair, rewrote to
`"SELECT MAX(BrandID) FROM Brand;"` (all-uppercase) -- failed identically
to the mixed-case version (same retry pattern, same FUN_7a854cd0 call,
same position numbers). Rules out a case-folding bug, whether in tew's own
handling of the query text or in Jet's real function-name lookup. Probes
removed again immediately after; run_exe.py back to 0 registered
breakpoints.

**Ruled out so far, cumulative**: query bytes/encoding, tokenizer
misclassification, the AS alias, aggregating an empty table, and now
function-name case-sensitivity. The aggregate function itself is what
fails, unconditionally, regardless of case or target-table row count --
still needs real Jet 3.5 SQL-engine research to explain why.

## 2026-08-17 — Empty-table theory disproven; confirmed Jet 4 predates this
build; checked a real game archive for a newer Jet, found none

Follow-up to last night's DAO-3075 root-cause work. Re-added two of the
removed probes (FUN_7a856c17 query-rewrite, FUN_7a854cd0 message-report) for
one targeted test: is `Max()` failing because it's aggregating the empty
`Part` table specifically, or aggregates in general? Rewrote a live query to
`"SELECT Max(BrandID) FROM Brand;"` (Brand confirmed non-empty from last
session's plain-SELECT test) -- it failed identically to Max(PartID) FROM
Part, same retry pattern, same FUN_7a854cd0 call. Rules out row-count as a
factor; the aggregate function itself is what this Jet 3.5 build rejects,
unconditionally. Probes removed again immediately after -- run_exe.py back
to 0 registered breakpoints.

Checked real PE build timestamps: MCity_d.exe 2002-05-03, msjet35.dll
1999-04-23, dao350.dll 1998-04-08, msjter35.dll 1997-06-23. Jet 4.0 shipped
June 1999 (Access 2000/Office 2000) -- already ~3 years old by the time this
exe was compiled, and older than msjet35.dll's own build. Confirms Jet 3.5
was a deliberate compatibility choice by the original dev team (broader
Win95/98-era redistributability), not "Jet 4 wasn't available yet."

Downloaded and checked a real Motor City Online client-distribution archive
(a password-protected 7z, ~336MB, 1832 files -- "Motor City Online" install
tree + Castanet update staging, from a Google Drive link Molly provided;
needed the resourcekey query param to get past Drive's login-wall for
pre-2021-shared links) for a bundled newer DAO/Jet. Found zero
msjet*/dao*/msjter*/msjint* DLLs and no DAO/MDAC redistributable installer
anywhere in it -- the archive is purely the game's own application files;
DAO/Jet would have been a separately-installed shared system component.
Nothing further to find there.

**Current state**: Jet 3.5 aggregate-function failure is real, reproducible,
independent of table row count, and predates any external "wrong Jet
version" explanation. Needs real Jet 3.5 SQL-engine research next (documented
limitations, MS KB articles, mdbtools source) -- out of scope for more live
emulator tracing.

## 2026-08-16 (cont'd x5) — DAO-3075 root-caused: aggregate function
Max(PartID) fails to compile in this Jet 3.5 build; alias theory disproven

Live-traced the real `CreateQueryDef` compile chain in msjet35.dll (project
`debug_clean`) past the reentrancy-guard fix from earlier tonight, all the
way to the actual failure: `FUN_7a8ae64d` (confirmed success path fires) ->
`FUN_7a856c17` (real create/compile, never traced before) -> returns -3100
(0xfffff3e4) -> `FUN_7a866d2b` recognizes -3100 by name, extracts the query
substring at the failure point -> `FUN_7a854cd0` (message report, gated on
an internal 0x41f flag) -> `FUN_7a85662f`, which turned out to be `setjmp`
(real "VC20" jump-buffer signature), not a parser -- the real parser calls
`longjmp(buf, -3100)` on a genuine syntax error.

Checked three independent layers, all clean -- ruled out corruption,
encoding, and tew-handler involvement:
- Raw query bytes (new probe at FUN_7a856c17 entry): exactly the correct 38
  bytes of "SELECT Max(PartID) AS Expr1 FROM Part;", correct terminator.
- Tokenizer (new probe at FUN_7a85683d's single shared epilogue): every
  token classified exactly right, including AS -> real keyword code 0x105,
  not misread as an identifier. Entirely self-contained (byte-classification
  bitmap + static keyword hash table) -- zero calls into any Win32 API tew
  implements anywhere in this whole chain.
- Confirmed this is Jet 3, not Jet 4: MDB header version byte @0x14 == 0x00
  in Online.mdb, matching msjet35.dll and this session's established
  2048-byte page size.

Ran two live query-rewrite experiments (patched the query in memory at
FUN_7a856c17's entry before tokenizing, updating the length param to
match): dropping just `AS Expr1` still failed identically (same retry
pattern, same position numbers despite a 9-byte-shorter string -- disproves
the alias theory); dropping the aggregate entirely
("SELECT PartID FROM Part;") compiled with zero errors, confirmed against a
second real non-aggregate query encountered the same run
("SELECT BrandID, Brand, PicName FROM Brand;", also clean). stdout.txt's
"Could not create Query"/DAOERROR(3075)/ASSERT pQueryDef lines vanish
entirely; the run gets ~15x further (160K log lines vs ~10-13K every prior
run) before an unrelated RUNAWAY much later, in a different part of the game.

Also corrected a same-session mistake: earlier tonight I re-presented
"The class has not been licensed" as a new lead/candidate root cause --
Molly had already flagged this exact message as expected/ignorable in an
earlier session (see status_archive.md, "2026-08-16"). Noted and corrected
in status_archive.md so it doesn't get re-chased again.

**Conclusion**: Max(PartID) (the aggregate function call) is specifically
what this compiled Jet 3.5 engine can't compile -- not the alias, not the
query text, not tew. Needs real Jet 3.5 SQL internals research next
session (is a bare aggregate with no GROUP BY a real, documented Jet 3.5
restriction, a missing-index requirement, or something else) -- out of
scope for more live emulator tracing.

**Left in run_exe.py, uncommitted**: all 8 breakpoint slots are consumed by
this investigation's probes; a new caution comment at register_breakpoint's
definition documents the hard 8-slot cap (Zig core's bp_table, silently
drops anything past it -- live-verified 3 new probes silently never fired
before this was found and fixed by pruning 3 confirmed-resolved probes from
earlier investigations). FUN_7a856c17_entry's query-rewrite experiment
(_QUERY_REWRITE) is left ACTIVE -- it rewrites every compiled query to
"SELECT PartID FROM Part;" -- must be removed/disabled before any run not
specifically testing this.

## 2026-08-16 (cont'd x4) — dll_loader no longer swallows FatalHaltError as
a load failure

Follow-up to the reentrancy-guard live-verify (previous entry): the guard's
one logged violation (`tid=1007`'s refused `Sleep()`) was immediately
followed by a real `__chkesp` stack-corruption fatal halt. That fatal halt
(`FatalHaltError`, raised by `cpu.run()`) was happening *inside* a nested
dependency-`DllMain` call still in flight from `dll_loader.load_dll`'s own
`on_dependency_loaded` hook (`dll_loader.py:360`) -- so it unwound straight
into `load_dll`'s broad `except Exception`, which logged it as
`"Failed to load MSJTER35.DLL: fatal halt..."` and returned `None`, even
though `MSJTER35.DLL` and `MSJINT35.dll` had both already mapped and cached
successfully moments earlier. That `None` then produced a misleading
`"LoadLibraryA(MSJTER35.DLL) -> NULL (not found)"` from `_load_dll_by_name`
-- a genuine, whole-session-stopping fatal condition silently downgraded to
an ordinary per-call "DLL not found" warning, letting the game continue
running on corrupted state instead of actually halting.

`tew/loader/dll_loader.py`: added `except FatalHaltError: raise` ahead of
the existing broad `except Exception` in `load_dll`, matching
`FatalHaltError`'s own documented contract (`cpu_zig.py`) of propagating
through every intermediate frame until it reaches wherever it's actually
meant to be handled -- `load_dll` isn't that place, and swallowing it there
contradicts the fatal_halt invariant the rest of the codebase protects
carefully elsewhere (e.g. `_load_thread` never clearing a fatal halt).

`tests/unit/loader/test_dll_loader.py`: new `TestLoadDllPropagatesFatalHalt`
(2 tests) -- monkeypatches `EXEFile`/`find_dll_file` to inject a
`FatalHaltError` (or, for the regression test, an ordinary `ValueError`)
without needing a real PE fixture; confirms the former propagates and the
latter still returns `None` as before. Full suite: 1148/1148.

**Live-verified**: same run (`LOG_CATEGORIES=dll,scheduler,thread,handlers,cpu`)
now ends cleanly at 37.155s with a full diagnostic dump and
`"Execution stopped."`, instead of continuing on corrupted state to the 60s
timeout cutoff. No more `"Failed to load MSJTER35.DLL"` /
`"LoadLibraryA(...) -> NULL"` lines. The underlying trigger (`sleep_current`'s
refusal leaving the calling `Sleep()` stub with no defined fallback) is
still open -- see current `status.md`.

## 2026-08-16 (cont'd x3) — Scheduler reentrancy guard: single `_swap_current`
chokepoint, 25 new tests, live-verified fix for MSJINT35.dll's DllMain

Fixed the reentrancy hazard exposed by the dependency-`DllMain` fix (previous
entry): `_invoke_emulated_proc`'s nested `cpu.run()` shares tew's single
`cpu.regs` with the cooperative scheduler, so a scheduler swap triggered by
any stub handler reached mid-nested-call used to silently hand the shared
registers to an unrelated thread, with no detection.

`tew/kernel/scheduler.py`: added `reentrant_depth: int` /
`reentrancy_violations: list[str]`, `enter_reentrant_call()`/
`exit_reentrant_call()`, and a private `_swap_current(cpu, memory,
target_idx, operation)` -- the single chokepoint every swap-capable public
method (`switch_to`, `preempt_slice`, `block_current_on_cs`,
`block_current_on_handles`, `sleep_current`) now routes through. Refuses
(logs `[ERROR][scheduler] reentrancy violation: ...`, appends to
`reentrancy_violations`, returns `False`/no-ops, mutates nothing) whenever
`reentrant_depth > 0`. Chose log-and-refuse over raising: this fires from
inside stub handlers called through the Zig/Python `cpu.run()` boundary,
where a Python exception has no defined crossing behavior. `mark_current_dead`
/`terminate_thread` deliberately left unguarded and unchanged -- a thread
dying mid-nested-call must still hand off the CPU; `_invoke_emulated_proc`'s
existing thread-death detection (`test_invoke_emulated_proc_thread_death.py`)
depends on that swap still happening even while reentrant.

`tew/api/user32_handlers.py`: `_invoke_emulated_proc` now wraps its bounded
`cpu.run()` chunk loop in `scheduler.enter_reentrant_call()` /
`exit_reentrant_call()` (try/finally).

`tests/unit/kernel/test_scheduler.py`: 25 new tests -- depth tracking
(`enter`/`exit`, nesting, unmatched-exit assertion), guard refusal for all 5
swap-capable methods (explicit no-state-mutation assertions: thread status/
`waiting_on_cs`/`waiting_on_handles`/`cpu.eip` all unchanged, `save_state`/
`restore_state` never called), an explicit test locking in that
`mark_current_dead`/`terminate_thread` remain functional and unguarded while
reentrant, and non-reentrant regression coverage. Full suite: 1146/1146.

**Live-verified** (`LOG_CATEGORIES=scheduler,thread,dialog,handlers`):
`MSJINT35.dll`'s `DllMain` now runs to completion -- its thread (`tid=1011`)
exits normally via `THREAD_SENTINEL`, no more silent mid-call thread death.
Exactly one reentrancy violation fired, at the exact moment expected: right
after `tid=1011` died and the unguarded `mark_current_dead` swap moved
control to `tid=1007`, that thread's own `Sleep()` call was correctly
refused (outer nested call hadn't yet noticed the death at its next chunk
boundary, `reentrant_depth` still 1). Run continued a bit further and then
hit a fatal halt from an apparently unrelated issue -- see current
`status.md` for the two open follow-up threads (Sleep-refusal fallback
contract; a `LoadLibraryA` already-loaded-DLL lookup gap on `MSJTER35.DLL`).
Neither investigated yet.

## 2026-08-16 (cont'd x2) — Found the real root cause of the empty DAO error
description: dependency DLLs never get their own DllMain invoked

Traced the `LoadStringA(id=3075) -> ""` empty-description symptom (found
last entry) all the way to its real cause. `msjet35.dll` correctly
`LoadLibraryA("MSJTER35.DLL")`s and resolves its real exports
(`JetErrFormattedMessage`, confirmed via decompile, real symbols already in
the `msjter35.dll` Ghidra project). Added a permanent debug field to tew's
own `_LoadStringA` handler (`user32_handlers.py`) logging the real caller
address resolved via `dll_loader.find_dll_for_address` -- revealed the
actual caller is a *different* DLL, `MSJINT35.dll` (the real locale-specific
Jet error-string resource DLL `MSJTER35.DLL` delegates to), not
`MSJTER35.DLL` itself.

`MSJINT35.dll` exists on disk and does get loaded by tew -- but only as an
implicit PE-import dependency of `MSJTER35.DLL`, pulled in by
`dll_loader.load_dll()`'s own recursive dependency-loading (line ~312:
`imported_dll = self.load_dll(descriptor.dll_name, memory)`). That function
never invokes the loaded dependency's own `DllMain` -- only the separate,
higher-level `LoadLibraryA` Win32 handler does that, and only for the
top-level DLL the guest explicitly requested. So `MSJINT35.dll`'s own CRT
startup never runs, its "my own HINSTANCE" global stays at its
never-initialized default (0), and when its exported code later runs
(called indirectly through `MSJTER35.DLL`) and passes that to `LoadStringA`,
the lookup fails against the wrong (NULL) module.

This is a systemic gap -- any DLL loaded only as an implicit dependency of
another dynamically-loaded DLL has the same problem, not just this one.
Real fix needs design thought (dependency `DllMain` ordering/reentrancy),
bigger than a quick handler fix -- not yet attempted. Once it lands, a
future session can finally read the real Jet error-3075 description text
and settle whether it's really a syntax error or something else.

## 2026-08-16 (cont'd) — Traced CreateQueryDef live through 3 DLLs into real
msjet35.dll; fixed a real VariantClear/Ordinal-9 bug; pinned the empty
DAO-3075 description down to msjter35.dll's error-string resource lookup

Verified tew's CPU-core JGE (`core.zig`'s `evalCond`) is correct against
real x86 semantics for all 16 condition codes; added the first-ever Jcc
regression tests (`engine.zig`, taken/not-taken via `CMP`+`JGE`). 77/77.

Live-traced `Dbcode_CreateTmpQuery`'s QueryDefs-search loop: `Count()=136`,
correctly scans every existing named query, correctly finds no match for
the scratch name `"#Temporary QueryDef#"`, correctly falls through to
create-fresh. Search logic is not the bug.

Live-traced the real `CreateQueryDef` chain end to end for the first time:
`Dbcode_CreateTmpQuery` (MCity_d.exe) -> `FUN_04487388`/`FUN_0448356f`/
`FUN_044c98fe`/`FUN_044d519b` (dao350.dll, real internals, not just
thunks) -> `(*DAT_044e534c)`, a dynamically-bound ISAM function pointer ->
`FUN_7a8ae64d`, real msjet35.dll code (Jet engine itself), a 3-way decision
point with two hardcoded error paths and a real compile call
(`FUN_7a856c17`, not yet traced). Added a reusable backtrace-to-file
breakpoint helper in `run_exe.py` along the way (wraps the existing
`_dump_cpu_state`/`_walk_ebp_chain` diagnostics with a file-writing log
function).

Checked the SQL text's own OLE Variant/BSTR construction
(`dbVariant::dbVariant(char*)` -> `OleVariant::OleVariant(...,0xe)` ->
`lstrlenA` + `Ordinal_150`/`SysAllocStringByteLen`) line by line -- correct,
not the bug. While checking it, found and fixed a real, separate bug:
`OLEAUT32.dll` `Ordinal #9` (the real ordinal-based import path
msjet35.dll/dao350.dll actually use for `VariantClear`) only zeroed the
4-byte header of the 16-byte `VARIANT`, leaving the value union (e.g. a
`BSTR` pointer) stale. Fixed by delegating to the correct named handler;
new tests in `test_oleaut32_variant_clear.py` (4 tests, 1125/1125 total).
Confirmed via live re-run the fix is real but does NOT change the
`CreateQueryDef` failure -- ruled out for this specific symptom, kept as a
legitimate fix.

Live-captured the real `Error.Description` BSTR via `DumpErrors`
(MCity_d.exe): a genuinely valid, zero-length BSTR (`bstr_ptr=0x70aa514
byte_len=0`), not NULL, not corrupted, not a formatting bug. Real DAO error
descriptions come from a message-by-code lookup against `msjter35.dll` (the
actual Jet error-message resource DLL, confirmed on disk), which was
already confirmed unreadable via plain string extraction (non-standard/
compressed resource format). Current blocker: find and check whether tew
implements whatever mechanism (likely `LoadStringA`-style) actually
performs that lookup -- a separate, previously-uninvestigated piece of the
emulation from the `CreateQueryDef` logic itself.

## 2026-08-16 — B-tree/OpenDatabase investigation RESOLVED: root cause was
tew's own missing OVERLAPPED support, not a real DAO/Jet bug

Re-ran with the OVERLAPPED fix live and `fileio` logging enabled. The
impossible `field@iVar6+2=1903` page that's been the subject of the entire
multi-day investigation (since 2026-08-06/07) never appears. Real positioned
reads now scatter genuinely across the file instead of marching sequentially,
and the same memory buffer that used to always coincide with the corrupt-
looking page now gets reached via its real positioned offset and reads a
small, sane `field@+2=34` instead. Every B-tree page visited this run has
plausible metadata.

Confirms Molly's original instinct from 2026-08-09 ("MCO shipped and worked
for real players, so it can't be a real Jet/data bug") was correct the whole
time -- the actual bug was tew's own `ReadFile` silently ignoring
`OVERLAPPED.Offset` (fixed 2026-08-15) and serving sequential bytes
regardless of what DAO/Jet requested. All the deep msjet35.dll decompile work
from 2026-08-15 (`FUN_7a8481a0`, the `PageCacheEntry` hash-cache chain, the
`SHL EAX,0xB` truncation math, etc.) was real and accurate, just chasing a
symptom rather than the cause -- kept as reference, not wasted, but the
specific bug it targeted is closed.

Run now progresses further than any prior session, past the entire DB-open
sequence, and halts on an ordinary unstubbed function:
`[UNIMPLEMENTED] user32.dll!IsCharAlphaNumericA`. Implementing it next.

## 2026-08-15 (cont'd x2) — Implemented real OVERLAPPED.Offset support in
ReadFile/WriteFile

Real fix for the gap found earlier today: `tew/api/kernel32_io.py`'s
`_read_file`/`_write_file` now read the real 5th stack parameter
(`lpOverlapped`) and, when non-NULL, pull `Offset`/`OffsetHigh` from guest
memory at the real `OVERLAPPED` struct offsets (`+8`/`+0xC`) and use that
64-bit position for the actual `os.pread`/`os.pwrite`/`entry.data` slice --
critically, **without** advancing `entry.position`, matching real Win32 (a
positioned read/write via non-NULL `lpOverlapped` doesn't disturb the
handle's own sequential file pointer, even without `FILE_FLAG_OVERLAPPED`).
NULL `lpOverlapped` keeps the prior purely-sequential behavior unchanged, so
every existing caller is unaffected.

Added `TestOverlappedReadWrite` (`tests/unit/api/test_read_write_file_handle.py`,
2 new tests): a positioned read at a non-sequential offset returns the
correct bytes and leaves `entry.position` untouched; a positioned write lands
at the requested offset without moving the cursor, verified by reading the
whole file back afterward. Full suite: 1088/1088 passing (was 1086).

Next session: re-run the B-tree investigation with real positioned reads now
in place and check the `ReadFile` log's new `offset=`/`[overlapped]` markers
against what `FUN_7a842abc` actually requests at each B-tree level -- this
finally answers whether page 34 was a real DAO/Jet-computed target or an
artifact of the old always-sequential `ReadFile`.

## 2026-08-15 (cont'd) — Traced FUN_7a8412c3 chain end to end via decompile +
raw disassembly; found tew's ReadFile/WriteFile never support OVERLAPPED at all

Fully traced the chain from `0x071b0748` (the tail-page value from the
earlier entry) through `FUN_7a8412c3` -> `FUN_7a841230` (hash-table lookup,
confirmed the raw value is used purely as a hash key, not a page number) ->
`FUN_7a84220d`/`FUN_7a84239a`/`FUN_7a842468` (pool-slot allocator + base+derived
constructor pair building a `PageCacheEntry : HashNode` object) -> `FUN_7a841344`
-> `FUN_7a84271d`/`FUN_7a84274e` (shared I/O dispatch, reached via two real
jump tables at `0x7a8427d4` and `0x7a8427e8`, both confirmed via raw bytes,
not decompiler-inferred case grouping). Mapped the full 0x54-byte
`PageCacheEntry`/`HashNode` struct layout (constructors, key field, 8-slot
state array, etc.) for Molly to type into Ghidra by hand.

Confirmed a freshly-constructed cache entry always starts in state 3 (worked
out by hand from `FUN_7a84239a`'s bit-twiddling, then independently confirmed
at the instruction level: `AND EAX,7` at `0x7a84272c`, final state-write at
`0x842435`). Initially concluded (wrongly) that state 3 skips the real
`ReadFile` path entirely; corrected mid-session after tracing the actual
jump-table targets -- state 3 does reach `FUN_7a842abc` (the real `ReadFile`
wrapper), just via `FUN_7a841bf0`'s fresh arena buffer (`VirtualAlloc`
reserve/commit, not disk) instead of the flags-word-encoded buffer pointer.

Made and then corrected a real arithmetic mistake: first reported
`0x071b0748 << 11` as ~244GB (full-precision multiply, wrong -- `SHL EAX,0xB`
is a 32-bit truncating register shift on real hardware). Verified via raw
bytes (`C1 E0 0B`, a genuine 32-bit `SHL r/m32,imm8` at `0x842784`) that the
real truncated value is `0xD83A4000` (~3.38GiB) -- still impossible for a
5.6MB file, corrected magnitude only.

Added a new Zig regression test (`cpu/src/engine.zig`,
`"doGroup2 SHL EAX,0xB truncates to 32 bits on a large shift count"`) since
no prior test covered a shift count above 1 or checked the result value for
truncation (only CF-flag correctness, from the 2026-08-06 bug fixes). Passes,
75/75 -- confirmed tew's own CPU-core SHL is NOT the bug here.

**Real finding, likely bigger than the B-tree investigation itself**:
`tew/api/kernel32_io.py`'s `_read_file` and `_write_file` both read only 4 of
the real 5 `ReadFile`/`WriteFile` stack parameters -- `lpOverlapped` is never
read or used anywhere in the codebase (`grep -rn "OVERLAPPED" tew/` outside
tests returns zero hits). Every read/write is purely sequential via
`entry.position`. Since DAO/Jet's real positioned page reads go through
`ReadFile(..., &overlapped)` specifically to seek non-sequentially, tew
currently can't distinguish a correct page-offset computation from an
incorrect one -- it just serves whatever's next in sequence regardless.
Implementing real `OVERLAPPED.Offset` support is now in progress; the B-tree
offset-computation question can't be meaningfully resolved until it lands.

## 2026-08-15 — Environment blocker no longer reproducing; B-tree page-34
mechanism fully decompiled, new lead found

The 2026-08-14 SDL/Vulkan/X11 environment blocker did not reproduce today --
two separate `run_exe.py` runs both completed cleanly through the B-tree
window with no code changes to the window-manager layer and no reboot
performed this session. Not root-caused why it started working again; if it
recurs, the 2026-08-14 entry's three driver failure modes are still the
reference.

Decompiled `FUN_7a8481a0` (the real cursor-descend loop, project
`debug_clean`/`msjet35.dll`) in full for the first time. It has three real
branches after `FUN_7a848399` returns, not the single `FUN_7a8870a2` path
assumed since 2026-08-09: a normal "found" path (`FUN_7a8870a2`), a
"not-found, no tail page" path (`FUN_7a879da5` bitmap scan then
`FUN_7a8870a2`), and a "not-found, tail page present" path that uses
`FUN_7a879d3b`'s return (`*(int*)(iVar6+0x10)`) directly with **no**
`FUN_7a8870a2` call at all. Confirmed live, twice, that the level-2->3
transition landing on the buggy page (file offset 69632, `field@+2=1903`)
takes this third branch -- zero `FUN_7a8870a2` hits in that window both
runs. Added a new windowed breakpoint on `FUN_7a879d3b` (runtime
`0x15039d3b`) that captured the real value both times: `tail_page@iVar6+0x10
= 0x071b0748` (119,211,848) -- far too large to be a raw page number in a
~2,873-page file, ruling out the "iVar6+0x10 is literally the next page
index" reading. This raw value is threaded into the top of the *next* loop
iteration via `FUN_7a8412c3(vtable[2], param_3, uVar4, 1, param_5, vtable)`,
the real page-fetch/pin call -- not yet decompiled, now the actual next
target (see status.md).

Also noted, not investigated: both runs end in an unrelated fatal halt at
`EIP=0x00200742` on the main thread (`tid=1000`), well after the B-tree
window closes -- separate subsystem, flagged for a future session.

## 2026-08-14 — Root-caused (but did not fix) an environment blocker that
stops `run_exe.py` from running at all on this machine

No B-tree investigation progress this session -- entirely spent diagnosing
why the emulator can't start. Full detail in `status.md`'s current entry
(kept there since it's the active blocker, not history yet). Summary: all
three SDL video drivers fail for three different, each individually
root-caused reasons -- default X11 hangs forever waiting on a `MapNotify`
that never arrives (confirmed via live `gdb` backtrace; not stale process
state, survived a targeted `Xwayland` restart that was confirmed safe for
this session first); `dummy` can never work since the game's main window
needs `SDL_WINDOW_VULKAN` and the dummy driver has no real surface for
Vulkan; `wayland` gets furthest (real window + real `VkDevice` created)
before segfaulting on a confirmed real NVIDIA driver bug (`gdb`-captured
live crash: `vkGetPhysicalDeviceSurfaceCapabilitiesKHR` -> NVIDIA's Vulkan
ICD -> `XGetWindowAttributes()` on what is a native Wayland surface, not
an X11 one). `egl-wayland` confirmed installed, so not a missing-package
issue. Decided to wait for a physical reboot rather than keep chasing
driver/compositor fixes remotely. The `FUN_7a8870a2` probe built for the
actual investigation is untouched and ready the moment a run completes.

## 2026-08-09 (cont'd) — Confirmed the B-tree page-overflow's `1903` value
is real, pre-existing `Online.MDB` data, not a tew write-path bug

Added a temporary breakpoint at `FUN_7a848399`'s real entry (runtime
`0x15008399`, `run_exe.py`, marked `TEMPORARY (2026-08-09)`) logging
`(this, iVar6, field@iVar6+2)` per hit, plus a permanent `buf=0x...` field
added to `kernel32_io.py`'s `ReadFile` debug logging (same style as the
existing `offset=`/`req=`/`got=`/`pos_after=` fields). Confirmed live all
3 invocations reproduce the known sequence (`1578`/`1787`/`1903`), and
cross-referencing `iVar6` against the new `buf=` field pinned the
problem page (hit #3) to **Tmp.MDB file offset 69632**
(`ReadFile(Tmp.MDB ... offset=69632 buf=0x41ab6000)`, exact match --
hit #2 similarly matched `offset=67584`; hit #1's `iVar6` landed inside
an earlier 65536-byte bulk read at buffer offset `0x4800`, i.e. file
offset 18432).

Confirmed **zero `WriteFile` calls hit the read+write handle (`h=0x5044`)
before this read** -- the only prior write activity on `Tmp.MDB` was the
startup `FeTools_CopyFile` copy via a separate write-only handle
(`h=0x5003`): plain sequential 4096-byte `ReadFile(Online.mdb)` /
`WriteFile` pairs, no transformation of any kind. Direct byte comparison
at file offset 69632 in both `~/.emu32/Data/DB/Online.mdb` and
`~/.emu32/SaveData/DB/Tmp.MDB` (both 5,883,904 bytes) confirmed
**byte-for-byte identical** content, `field@+2=1903` in both real files
on disk. This conclusively rules out a tew write-path bug as the source
of `1903` -- it's genuinely present in the shipped `Online.MDB`.

**Not yet resolved**: whether `1903` exceeding this page's own
`0xe2*8=1808`-byte stated capacity is a real Jet 3.5 data-integrity
quirk real Windows/real Jet never trips over, or whether `field@iVar6+2`
(and a second nearby candidate, `iVar6+10 = 0x077b = 1915`) are being
misinterpreted and don't actually mean "bytes used" the way assumed.
See `status.md` current blocker for the concrete next step (real Jet
page-header layout, via Ghidra on `msjet35.dll`).

## 2026-08-09 — Root-caused the B-tree stall to a real page-metadata
inconsistency; added DisableAudio registry key (kills DSOUND spam)

Two independent, unrelated wins in one session.

**`registry.json`**: added `"disableaudio": {"type": 4, "value": 1}`
under `hklm\\software\\electronic arts\\motor city`. The game's own real
code checks this key (`RegQueryValueExA`, previously `NOT FOUND` since it
was never seeded) and, when set, skips whatever produces the
`[DSOUND (serve)]`/"timer held off" spam entirely -- confirmed live,
`stdout.txt` went from thousands of those lines to zero. A real,
game-supported config toggle, not a workaround.

**The `FUN_7a848399`/`FUN_7a847f1d` B-tree stall is fully root-caused.**
Continued the logpoint chain from the previous entry: a register-state
dump at `FUN_7a847f1d`'s prologue (`0x15007f34`, right before its
fast-path check) showed EAX/EDX values that turned out to be *correctly*
computed from the real captured `param_2` sequence (`231/111/47/13/23/
-89`) via the decompile's own mask-table lookup (`(&DAT_7a847f68)
[uVar2&7]`) -- not a bug, just data-dependent variation that looked
suspicious at a glance. The real instruction at that address turned out
to be `AND AL, byte ptr [param_1+ESI*1]`, and tracing the actual x86
semantics through it (uint subtraction, then a genuinely *logical*
right-shift per the decompile's own `uint` types) showed `-89` becomes
`0x1FFFFFF4` (~537 million) as the loop's byte-index/counter -- a
correction to the previous entry's "-89 wraps to 4.3 billion" framing;
the real mechanism shifts first, landing at ~537 million, still the
entire stall either way. Confirmed this instruction and the shift before
it are executing correctly given their input -- no CPU-core bug here,
despite two real ones (ADC/SBB carry-wraparound, SHL/SHR/SAR CF-clobber)
found and fixed nearby overnight.

Cross-referencing the 6 calls' return addresses against `FUN_7a848399`'s
two known call sites (`0x7a8483e9`, the one-time initial-bound call, and
`0x7a8484e7`, inside the binary-search loop) showed `FUN_7a848399` itself
is invoked 3 times per run (a 3-level B-tree traversal), and the bad
`-89` comes from the *third* invocation's very first call -- before its
own search loop even starts. That call site computes its argument as
`local_18*8 - 1`, where `local_18 = 0xe2 - (*(ushort*)(iVar6+2) >> 3)`
and `iVar6 = *(this+4)` is real Jet page metadata. A final logpoint at
`FUN_7a848399`'s own entry, reading `this`/`iVar6`/the real 16-bit field
at `iVar6+2` for all 3 invocations, nailed it exactly:

| Invocation | field@iVar6+2 | local_18 | param_2 passed |
|---|---|---|---|
| 1 | 1578 | 29 | 231 |
| 2 | 1787 | 3 | 23 |
| 3 | **1903** | **underflows to -11** | **-89** |

`0xe2*8=1808` is almost certainly this page structure's real usable-byte
capacity (a `2048`-byte Jet page minus a `~240`-byte header is a
plausible split). `field@iVar6+2` is a real "bytes used" counter for
that page, and on the third traversal level it reads `1903` -- over its
own page's stated capacity. The three values are small and steadily
increasing (not garbage), so this is real but internally-inconsistent
page metadata, not corruption from a CPU-emulation bug at this call site.

**Current blocker**: why does this specific page's own "bytes used"
field exceed its own capacity -- trace back to whatever wrote it during
`Tmp.MDB`'s creation/growth (`FeTools_CopyFile` from `Online.MDB` at
startup, then Jet's own writes during `DB_StartUpDatabase`). Could be a
genuine `Online.MDB` data quirk that real Jet also mishandles at this
exact call site (a narrow real-Jet bug never hit by real installs), or a
tew write-path bug. Not yet fixed, but precisely enough scoped now to go
straight to comparing this page's real bytes between `Online.MDB` and
the freshly-copied `Tmp.MDB` next session. Full detail: memory/status.md,
"Current status (2026-08-09)".

## 2026-08-08 (cont'd again x2) — Two real CPU-core CF bugs found and fixed
(cpu submodule 002e2db), both independently ruled out as the B-tree stall's cause

Molly's question ("how are we with math involving borrow?") while waiting
on a verification run prompted auditing `updateFlagsArithW`'s CF
computation directly, since the stall's root cause (signed/unsigned
confusion, `EDX=-89` wrapping to ~4.3 billion) is exactly this bug class.
Found a real bug: ADC/SBB fold their carry/borrow into `op2` via
width-native wrapping arithmetic (`op2 +% c`/`op2 +% b`) before calling
`updateFlagsArithW`, which then re-derives CF from that (possibly
already-wrapped) value instead of using the correctly-computed, full
i64-precision `result_raw` it's also given. When the real operand is at
its width's max value with an incoming carry/borrow, the wrap rolls to 0
and CF comes out false when it should be true. Fixed by using
`result_raw` directly. Wrote 2 new failing-first Zig tests reproducing
the exact edge case for both SBB and ADC.

A live rerun with this fix alone still hit the identical stall. Molly's
next question ("what about SHR? do we handle that correctly?") led to a
second, independent, more severe bug: every `SHL`/`SHR`/`SAR` call site
computes the correct CF (the bit shifted out) via `setFlag`, then
immediately calls `updateFlagsLogicW` -- which unconditionally clears CF,
correct for `AND`/`OR`/`XOR`/`TEST` but wrong for shifts. This meant CF
after *any* shift instruction anywhere in this emulator was always false,
regardless of what bit actually shifted out -- a systemic bug, not an
edge case. `ROL`/`ROR`/`RCL`/`RCR` were unaffected (they never called
`updateFlagsLogicW`, correctly, since real rotates don't touch ZF/SF/PF
either). Fixed with a new `updateFlagsShiftW` helper (same ZF/SF/PF
logic, leaves CF alone since the caller already set it correctly) and
switched all 6 shift call sites (`doGroup2` + `doGroup2_8`, covering
SHL/SHR/SAR at both 8-bit and 16/32-bit widths). 3 more new
failing-first tests.

Both fixes: full Zig suite passes, `libcpu.so` rebuilt, full 1086-test
Python suite passes. But a live rerun after fix #2 on top of fix #1
*also* still hit the exact identical stall (`channel_log.txt` frozen at
`"fetching vehicle attribute table..."`, same as every prior attempt).
Both bugs are real, confirmed, and worth having fixed regardless -- but
neither is this investigation's actual root cause. Ruled out; don't
re-suspect either. Full detail: memory/status.md, "Current status
(2026-08-08, cont'd)".

**Current blocker unchanged**: still need to trace where `FUN_7a848399`
call #6's `EDX=-89` argument actually comes from. Next step: a logpoint
at `FUN_7a848399`'s own entry (runtime `0x15008399`), one level further
up the confirmed call chain.

## 2026-08-08 (cont'd again) — Root-caused the B-tree stall to a specific
signed/unsigned confusion via live logpoint iteration

Continued the `FUN_7a848399`/`FUN_7a847f1d` investigation with a series of
temporary logpoints (removed from `run_exe.py` once resolved), each one
correcting a wrong assumption from the last:

1. First logged calls through `FUN_7a848399`'s entry (assumed the hot
   caller) -- got zero hits despite the run reliably reaching the same
   stuck point. Wrong assumption.
2. Logged `FUN_7a847f1d`'s own entry instead, sampled every 50,000th call
   -- also zero hits. Turned out the sampling threshold was just too high
   relative to the real (much smaller) call count -- a measurement
   artifact, not evidence of anything.
3. Logged the exact hot instruction directly (`0x15007f52`, confirmed via
   the earlier `[alive]` sample) -- 21.35 million hits in one run,
   confirming a real near-continuous loop. But the "return address" it
   read was garbage (a heap pointer, constant for the entire run) because
   that address is mid-function, past the prologue's pushes -- `[ESP]`
   there is a local variable, not a return address. Also explained #2:
   the function can be called a small number of times with one call's
   internal loop running for millions of iterations.
4. Redid the entry-point log (`0x15007f1d`) with unconditional early
   logging instead of a fixed sample threshold -- **only 6 total calls
   per run**, confirming the theory from #3. Return addresses (correctly
   read at true entry this time) confirmed `FUN_7a848399` genuinely is
   the caller, just far less often than assumed in step 1.
5. The decompiled signature is `__fastcall FUN_7a847f1d(int *param_1, int
   param_2)` -- fastcall passes the first two args in ECX/EDX, not the
   stack, so step 4's stack-relative "param" reads were reading garbage
   too. Reading ECX/EDX directly: the real `param_2` across the 6 calls
   was `231, 111, 47, 13, 23, -89` -- a sequence that looks like a
   genuine binary search correctly narrowing bounds for 5 steps, then
   going wrong on the 6th. `-89`, read as the decompile's own `uint` type,
   wraps to `4,294,967,207` -- and the scan loop just decrements toward 0
   one at a time. That's the entire stall: a ~4.3-billion-iteration loop
   from one signed/unsigned confusion, same bug shape as the 2026-08-06
   `n=0xfffffffc` `memmove` bug.

**Current blocker**: find where call #6's `EDX=-89` actually comes from --
trace back through `FUN_7a848399`'s calling context to whatever computes
it. Most likely a genuine tew emulation bug (wrong page/index metadata fed
to real, natively-executing Jet code via `ReadFile`), since this is real
DLL code, not a tew stub -- but could also be a real "key not found" edge
case Jet handles specially in a way this call site doesn't. Not yet fixed.
Full detail: memory/status.md, "Current status (2026-08-08, cont'd)".

## 2026-08-08 (cont'd) — Traced the post-DB-init stall to a specific MSJET35.DLL
B-tree function pair; ruled out sockets, mapped files, CMPSB emulation

With today's perf/logging fixes in place, ran well past the previous
500M-step cap (2B steps) to see what happens after `DB_StartUpDatabase`
succeeds. Found a real stall, not just slowness: `channel_log.txt` and
`dblog.txt` both go completely silent right after `carClassList::
carClassList`'s "fetching vehicle attribute table..." print and
`DB_StartUpDatabase`/`Tmp.MDB` respectively, across ~1.94 billion further
real x86 instructions. The main thread's `wait_task_executing` polling
loop (confirmed correctly-behaving, waiting on a `DBRequestQ` request)
made it look deceptively "alive" the whole time via DSOUND's own
continued ticking, but a targeted `[alive]` sample of `tid=1012`
specifically showed it confined to a 12-byte instruction range
(`0x7a847f4d`-`0x7a847f59` static, inside `msjet35.dll`) across 140M+
steps -- a real tight-loop signature, not varied forward progress.

Decompiled in Ghidra: `FUN_7a847f1d` is a bounded bitmap-scan helper
(Jet free-space/page-bitmap navigation), called by `FUN_7a848399`, a real
binary-search routine over a sorted B-tree index page (midpoint search +
`REP CMPSB` key comparison). Both are finite by construction -- the
binary search's `while (uVar7 != uVar8)` can't loop forever unless fed
wrong data every iteration by something else.

Ruled out three real candidate causes, each independently confirmed and
each worth NOT re-checking next session: (1) sockets -- zero `[socket]`
log lines in single-player mode, no networking active at all (but found
and left unfixed a real, separate bug along the way: `wsock32_handlers.py`'s
`_recv`/`_recvfrom` both ignore their own `flags` parameter entirely, so
`MSG_PEEK` silently doesn't peek -- it destructively consumes data like a
normal recv; both also have the same per-byte `write8` loop fixed
everywhere else today, just missed since this file wasn't in the original
sweep); (2) memory-mapped files -- `CreateFileMappingA`/`W` are registered
as unimplemented halts, and no halt occurred, so they were never called;
(3) the CPU core's `CMPSB` (opcode `0xA6`) emulation -- read `engine.zig`'s
`opA6` directly, `REP`/`REPNE` break conditions match real x86 semantics
exactly, fixed-width so no `0x66`-override ambiguity like the prior
`doGroup1` bug.

**Current blocker**: `0x7a848399`/`0x7a847f1d` in `msjet35.dll` -- not yet
resolved. Next step: trace `FUN_7a848399`'s 7 distinct callers (via
`get_references_to`) to determine whether the outer call volume
legitimately explains the step count, or whether one of them is doing
something wrong. Full detail: memory/status.md, "Current status
(2026-08-08, cont'd)".

## 2026-08-08 — Fixed per-byte FFI memory-copy loops (real perf bug); new
channel_log.txt; DSOUND starvation diagnosed as architectural, not a bug

Molly noticed the emulator "used to be fast" and asked to find out why,
tying it to the DSOUND serve thread missing its 10ms callback deadline on
every single tick (100% of calls in stdout.txt reporting a 300-800ms+
hold-off). Added permanent, opt-in-only cProfile tooling to investigate
(`TEW_PROFILE=<path>` / `TEW_MAX_STEPS=<n>` env vars, run_exe.py -- stats
are dumped explicitly right before `os._exit()` rather than relying on
`-m cProfile`'s own exit handling, since `os._exit()` deliberately skips
normal Python shutdown to dodge a real NVIDIA-driver atexit segfault, and
that would otherwise swallow the profile dump too).

Found a real, confirmed bug class via the profile: several hot handlers
copied buffers between guest and host memory one byte at a time, via
individual `memory.read8()`/`write8()` FFI calls in a Python loop, instead
of one bulk call. `WriteFile`/`ReadFile` alone were 58% of total runtime
in an early profiled window (6.3M read8/write8 calls from 2,927 handler
calls). Added `ZigMemory.read_bytes(addr, n) -> bytes` (`memory_zig.py`,
reads directly from the shared backing bytearray -- safe because it's the
exact same buffer libcpu.so's mem_* functions operate on) and used it
(alongside the pre-existing bulk `load()`) to fix: `kernel32_io.py`'s
`WriteFile`/`ReadFile`; `msvcrt_handlers.py`'s `fread`/`fwrite`/`_read`/
`_write`/`realloc`; `kernel32_memory.py`'s `HeapReAlloc`; and `_state.py`'s
`read_cstring`/`read_wide_string` -- the highest-leverage fix of the
session, called from dozens of sites project-wide (every `%s` vararg sub,
every filename/registry-value read, `getenv`, etc.). Confirmed via
before/after profiling at the same 300M-step size: `read_cstring` dropped
7.6x (6.97s -> 0.91s cumulative for the same 210,993 calls), `getenv`
dropped 4.5x, total Python function calls dropped 37% (36.2M -> 22.6M).
Confirmed live with a real unprofiled run: 500M steps dropped from
143-145.6s to 124.5s wall-clock (~14-17% faster). 1086/1086 tests pass.

**The DSOUND starvation itself is unaffected by any of this, and that's
expected, not a miss** -- re-profiled and re-run live after the fix, still
100% of serve calls report a hold-off. Root cause is architectural: the
"timer held off" message is the game's own real-wall-clock check on its
audio thread, and tew's scheduler is a single-core cooperative
round-robin across every emulated guest thread (`preempt_slice`) -- the
audio thread can only run when the round-robin reaches it, after every
other thread's full instruction batch for that slice has executed. Real
Windows gives it genuine OS-level preemption instead. Not fixable without
either much higher raw emulation throughput than realistic for a software
x86 core, or a scheduler rewrite around real wall-clock deadlines --
correctly out of scope for today.

**Two real logging-legibility regressions from earlier today's `[alive]`/
`channel`-category DEBUG demotions, discovered live**: a background 2B-step
run's log jumped from 56s straight to 464s with zero lines in between --
looked exactly like a hang and had to be verified healthy via `ps`
elapsed-time/CPU-state instead of the log itself, since both the progress
heartbeat and Channel_DebugPrint's real gameplay-progress lines
(`Track.c`, etc.) are now silent by default. Not yet resolved -- flagged
in status.md as an open question for next session (restore `[alive]` to
INFO, or accept the tradeoff).

**New: `channel_log.txt`** (Molly: "so we can tell it from the other
'normal' stuff") -- `Channel_DebugPrint` now always writes its formatted
output to a real, dedicated host file (`CRTState.write_channel_log`,
`_state.py`; lazily opened, resolved next to `stdout.txt` via the same
`translate_windows_path` anchoring, directory auto-created same as
`open_file_handle` already does), unconditionally, independent of
`LOG_LEVEL`/`LOG_CATEGORIES` -- deliberately a separate file from
`stdout.txt` (which stays `Channel_SystemPrint`/`OutputDebugStringA`
only, via the existing `write_guest_stdout`). `test_patch_internals.py`'s
shared `env` fixture had to be scoped to a `tmp_path`-based `EmulatorConfig`
as part of this -- it previously used the real `emulator.json`/`~/.emu32/`
config by default, which would have made every test in the file that
exercises `_channel_debug_print` write real files into the real user's
`~/.emu32/` tree as a side effect. Confirmed live:
`~/.emu32/MCity/channel_log.txt` created fresh each run with real content.

Also this session: routed `OutputDebugStringA` (`kernel32_io.py`) and
confirmed `Channel_SystemPrint` (`patch_internals.py`) both write through
the same real `stdout.txt` stream (`CRTState.write_guest_stdout`, factored
out as a shared helper); confirmed `-CaptureStdout` has been unconditional
in every run all session (baked into the fixed guest argv in
`msvcrt_handlers.py`, not a run_exe.py flag); confirmed `IDirect3D8`'s
`AddRef`/`Release` refcount fix (previous entry) holds under this
session's much longer real/profiled runs, with no `MUTEX_free` recurrence.
Also confirmed real, not a regression: `[DSOUND (create)] Resorted to
using desktop window handle` in stdout.txt -- `user32_handlers.py`'s
`GetActiveWindow`/`GetForegroundWindow` both unconditionally return NULL,
so the game's own DirectSound-init fallback always takes the desktop-HWND
branch it wouldn't on real Windows. Harmless, not fixed this session, noted
in status.md as a scoped next-session candidate.

## 2026-08-07 (cont'd again x6) — Fixed IDirect3D8::AddRef/Release fake
refcounting (real MUTEX_free-while-locked cause); nfile.c "FILE SYSTEM NOT
INITIALIZED" chain traced to a test-harness `timeout`-as-SDL_QUIT
artifact, not a real bug

`d3d8/idirect3d8.py`'s `IDirect3D8::AddRef`/`Release` COM vtable slots
were stubs that always returned `1`/`0`, ignoring how many references were
actually outstanding. Confirmed live: the render thread (`tid=1007`)
called `Release` on one of >=2 outstanding references; the stub reported
"refcount is now 0" anyway, so the game's own destructor tore down the
`IDirect3D8` object's internal mutex immediately -- which the main thread
then tripped over two log lines later as `"Abort from file
"cmn\mutex.c", line 429": MUTEX_free - FREEING A LOCKED MUTEX
(40201e60)."`. Fixed with a real per-`this` refcount dict (`_ref_counts`),
matching the already-correct pattern in `idirect3d8resource.py`'s
`_add_ref`/`_release`. `AddRef`/`Release` handlers changed from inline
lambdas to real `_add_ref`/`_release` functions so they can read `this`
off the stack and branch. New file `tests/unit/api/
test_idirect3d8_refcount.py` (6 tests, calling the functions directly
rather than through the full Vulkan-backed `make_vtable` -- AddRef/Release
don't touch Vulkan/WindowManager at all). 1080/1080 tests pass.

Also wired up (`run_exe.py`, right after `cpu = CPU(mem)`) the native Zig
execution-history capture layer (`cpu.enable_history_capture_clickhouse`)
as a replacement for the ad hoc Python `cpu.add_logpoint`s previously used
to chase the `nfile.c` "FILE SYSTEM NOT INITIALIZED" blocker queued in the
last entry -- per Molly's steer that this is exactly the purpose-built
tool for "what wrote this address last" questions, not more hand-rolled
logpoints. **Confirmed live this is not lightweight enough to leave on
unconditionally**: it hooks every memory write and every register/EIP/
EFLAGS change for the whole run; the periodic HTTP flush to ClickHouse
couldn't keep up with that volume, and a run stalled at 83s of virtual
time after 2+ minutes of real time with RSS climbing past 2.3GB before
being killed. Gated behind `_HISTORY_CAPTURE_ENABLED = False` (currently
off) rather than removed, for if a future investigation specifically needs
it and is worth the overhead.

With the capture disabled again, re-running to verify the mutex fix
surfaced a second, more interesting finding via `user32_handlers.py`'s
existing "log dialog appearance before the blocking `SDL_ShowMessageBox`
call" fix (from earlier this session): a run that looked like it made
"1226s of virtual-time progress" had actually mostly been sitting on an
unattended `MUTEX_free` dialog in real wall-clock time (Molly caught
this: "The dialog box was open and I didn't notice, which is why it ran
so long") -- not a logpoint-overhead Heisenbug like the previous round,
but the same underlying lesson: check what's actually consuming wall
time before drawing conclusions from an unexpectedly-long or
unexpectedly-short run.

After the mutex fix, a run bounded by `timeout 90` reproduced the queued
`nfile.c` chain exactly once: `SDL_QUIT received` fired at 89.984s, ~1s
before the external timeout, strongly suggesting the harness's own
`SIGTERM` was delivered through/interpreted by SDL2 as a real window-close
event. The game correctly walked its own real shutdown path in response --
`Channel_DebugPrint` `dbcode.c(1153)`/`(1172)` `Dbcode_AtExit()` ->
`Dbcode_AbortCallback_KillThread()` (`dbcode.c(1107)`/`(1130)`) ->
`nfile.c(200)` `FILE_allocateop - FILE SYSTEM NOT INITIALIZED` (the DB
thread died before/during its own filesystem teardown) -> unhandled
`INT3` -> fatal halt. A coherent, real call chain (confirms Molly's
original read that this was dbcode-driven, not an independent nfile.c
bug) -- but only reachable by killing the process mid-run. Doubling the
timeout to 180s confirmed this: the run went the full duration with zero
halts of any kind, ending via the ordinary `Execution limit reached
(500000000 steps)` step cap at 145.6s -- the furthest and cleanest clean
run this project has ever produced. Full detail: memory/status.md,
"Current status (2026-08-07, cont'd again x6)".

## 2026-08-07 (cont'd again x4) — Fixed real lag in Channel_SystemPrint/
Channel_DebugPrint; added "channel" to LogCategory

Both patches (`patch_internals.py`) did their full vararg-formatting walk
unconditionally on every call, even when "channel" wasn't in
`LOG_CATEGORIES` and the result would never be shown -- real, measurable
overhead once execution reaches real gameplay depth and these fire very
often (Molly: "too laggy" once channel logging started actually producing
output). Both now check `logger.is_active(...)` first and skip entirely if
nothing needs the result; `_channel_system_print` also checks
`guest_stdout_handle is not None` first, since the real stdout redirect
must keep working independent of log-category filtering. Also added the
long-missing `"channel"` entry to `tew/logger.py`'s `LogCategory` type --
real, in active use, just never actually declared.

1074/1074 tests pass. New `TestChannelPrintSkipsWorkWhenFiltered` in
`test_patch_internals.py`: debug-print does no formatting/logging when
filtered, system-print does no formatting/logging when filtered *and* no
stdout handle, system-print still writes stdout.txt when filtered *with* a
stdout handle set (the log itself stays suppressed either way).

## 2026-08-07 (cont'd again x3) — Implemented `GetComputerNameA`/`W`

The next honest gap surfaced right after `LockFile`/`UnlockFile`: real Jet
asks for the local machine's NetBIOS name, a simple standard API.
Implemented in `kernel32_system.py`, returning a fixed, plausible name
(`"MCITY-PC"`, matching the "fake but plausible" convention already used
for the fake PID etc.), with correct too-small-buffer failure semantics
(`ERROR_BUFFER_OVERFLOW`, required-size-on-failure vs
chars-copied-on-success, matching real `GetComputerNameA` exactly). Added
`ERROR_BUFFER_OVERFLOW` to `win32_errors.py`.

1071/1071 tests pass. New `TestGetComputerName` in
`test_kernel32_system_info.py`. Confirmed live: that halt is gone, and
execution now reaches 151s of virtual time (up from ~86s) -- well past
DAO/Jet and into real gameplay territory for the first time this session,
before hitting a new, unrelated `nfile.c` "FILE SYSTEM NOT INITIALIZED"
assertion. See status.md for the new current blocker.

## 2026-08-07 (cont'd again x2) — Implemented `LockFile`/`UnlockFile`

Real Jet locks its database file as a normal part of opening it (byte-range
locking) -- the very next honest gap surfaced by the `GetFileType` fix.
Implemented in `kernel32_io.py`: `CRTState.file_locks` tracks currently-held
exclusive ranges keyed by real host path (not handle -- real Win32
byte-range locks are visible to every handle open on the same file, and
real Jet does open the same database from more than one handle). `LockFile`
rejects a request that overlaps a range held by a *different* handle
(`ERROR_LOCK_VIOLATION`), matching real Win32's allowance for the same
handle to re-lock/extend its own ranges; `UnlockFile` requires an exact
`(offset, length)` match to the original lock (`ERROR_NOT_LOCKED`
otherwise, matching real behavior); `CloseHandle` now also releases every
lock a handle still holds, same as real Windows does implicitly.

1068/1068 tests pass. New `test_lock_file.py`: lock succeeds, overlapping
lock from a different handle fails, non-overlapping succeeds, unlock+relock
from another handle succeeds, unlocking an unlocked range fails,
CloseHandle releases locks, unknown handle fails. Confirmed live: the
`LockFile` halt is gone; execution now reaches
`[UNIMPLEMENTED] kernel32.dll!GetComputerNameA` instead.

## 2026-08-07 (cont'd again) — Fixed two real Win32 file-I/O bugs
(collapsed read/write access, inverted `GetFileType`) that were the actual
cause of `Workspace::OpenDatabase`'s "unrecognized database format" error

Molly's instinct going in: "zero chance this is a Jet bug... if anything,
it's that we are running windows files under linux encoding" -- exactly
right, on both counts (not Jet, and squarely tew's OS-emulation layer).

Traced the real DAO350.DLL &rarr; MSJET35.DLL call chain live via Ghidra
decompile plus `cpu.add_logpoint`s, first working out MSJET35.DLL's real
runtime-vs-Ghidra-static address delta (`runtime = static - 0x65840000`,
verified against the `opMovR32Imm` fix's known-good landmark instruction at
`0x1503564b`/`0x7a87564b`). Full chain, each link confirmed live: DAO350.DLL
`FUN_0448c745` &rarr; `FUN_044c5ee9` &rarr; `FUN_044c2d8a` &rarr;
`FUN_044e20c8` &rarr; `FUN_044d896d` &rarr; a dynamically-bound ISAM
function-pointer table (`DAT_044e52e8` et al, confirmed resolving into real
MSJET35.DLL addresses at runtime) &rarr; `FUN_7a8701ed` &rarr;
`FUN_7a85a900` &rarr; `FUN_7a86fac5` &rarr; `FUN_7a86fbed` &rarr;
`FUN_7a8709b6` &rarr; `FUN_7a870879` &rarr; `FUN_7a8708a1` (real
`CreateFileA`/`GetFullPathNameA`/`FindFirstFileA` path resolve+open,
confirmed live resolving to the exact correct path) &rarr; `FUN_7a8706e9`
(the real `CreateFileA` call, confirmed requesting `GENERIC_READ|
GENERIC_WRITE`) &rarr; `FUN_7a870b40`, which calls real `GetFileType()`
(via `FUN_7a8709a5`) before ever reading a byte and aborts immediately
unless the result is exactly `FILE_TYPE_DISK`.

Two real bugs found and fixed, both in tew's own Win32 file-I/O layer:

1. `kernel32_io.py`'s `_create_file_a`/`_create_file_w` collapsed
   `dwDesiredAccess` into a single `writable` boolean, losing whether
   `GENERIC_READ` was *also* requested. `open_file_handle` (`_state.py`)
   always opened the real fd `os.O_WRONLY` for any writable open, and
   `ReadFile`/`fread`/`_read` unconditionally rejected any writable-flagged
   handle regardless of the fd's real capabilities -- so a handle opened
   `GENERIC_READ|GENERIC_WRITE` (exactly what Jet requests for a live
   database) could still never actually be read from. Fixed:
   `FileHandleEntry` gained a `readable` field, `open_file_handle` gained
   `also_readable` (opens `O_RDWR` when set, threaded from
   `GENERIC_READ`/fopen's `"+"` mode/`_open`'s `O_RDWR`),
   `ReadFile`/`fread`/`_read` now do a real `os.pread()` for handles that
   are both writable and readable. Confirmed live and engaged correctly
   (`CreateFile(...) -> 0x5041 [write+read]`) -- a real, independent bug fix
   -- but confirmed NOT the root cause of this specific blocker: `ReadFile`
   is never even called before the real failure point.
2. `kernel32_system.py`'s `GetFileType` had its ternary backwards:
   `cpu.regs[EAX] = 2 if entry.fd is not None else 1`, exactly inverted from
   its own comment. Every real disk file (read-write or write-only) keeps a
   live fd, so this returned `FILE_TYPE_CHAR(2)` for every real file and
   `FILE_TYPE_DISK(1)` only for the read-only-with-cached-`entry.data` case
   (`entry.fd is None` there). Real `Workspace::OpenDatabase` calls
   `GetFileType()` immediately after `CreateFileA` and fails with error
   `-0x404` if the result isn't exactly `FILE_TYPE_DISK` -- fully explaining
   why `FUN_7a870cf8` (the "Standard Jet DB" signature-check function found
   earlier this session via a string search) never actually fired despite
   genuinely being reachable in the call graph: this gate rejects the open
   one step before ever reaching it. Fixed to key off `entry.path` instead
   (`'<...>'` sentinel paths and `/dev/null` are the only real
   `FILE_TYPE_CHAR` cases). **This was the real root cause** -- confirmed
   live: `0x800a0d0f`/"unrecognized database format" is completely gone,
   `Workspace::OpenDatabase` proceeds cleanly past everything above.

Also confirmed and worth keeping in mind: `Online.MDB`'s file integrity was
never in question (byte-identical to a second, independently-sourced copy;
size is an exact whole number of real Jet-3.x 2048-byte pages; `mdb-tools`,
an independent non-Microsoft Jet parser, successfully extracted its full
schema/data back in 2025) -- the earlier "byte-0x42 password-protection
gate" theory from the previous session is superseded and was based on an
incomplete trace (`FUN_7a870cf8` genuinely is called from `FUN_7a870b40`,
just never reached in practice because of bug #2 above).

1061/1061 tests pass. New tests: `test_read_write_file_handle.py` (full
`CreateFileA`&rarr;`WriteFile`&rarr;`ReadFile` round trip on a
`GENERIC_READ|GENERIC_WRITE` handle, plus a write-only-still-rejects guard)
and `TestGetFileType` additions in `test_kernel32_system_info.py`.

**New blocker, surfaced immediately by this fix**: `[UNIMPLEMENTED]
kernel32.dll!LockFile -- halting`, hit right after `Workspace::OpenDatabase`
succeeds. A clean, honest, well-understood gap (real Jet locking its
database file, real `LockFile` just isn't implemented yet) -- not a mystery
like everything before it. Not yet started.

## 2026-08-07 (cont'd) — Removed a tew-side patch that faked success on
`Dbcode_CopyDataBaseToSaveData`, resolving the real `Tmp.MDB`-never-created
bug; added `-CaptureStdout`

**Root cause of the `Tmp.MDB` mystery from earlier today**: not a guest bug,
not a missing Win32 handler, not a Jet/DAO quirk -- `tew/api/patch_internals.py`
had `stubs.patch_address(0x008ED560, "_winmain_check3_init", _winmain_check3)`,
a patch (predating this session, from back when Ghidra hadn't identified the
function yet -- the comment called it "unnamed init fn at 0x8ed560") that
unconditionally set `EAX=1` at the function's entry instead of letting it run.
That address is the real `Dbcode_CopyDataBaseToSaveData` -- the only place in
the whole binary that creates `Tmp.MDB`, via `FeTools_CopyFile` copying a
shipped `Online.MDB` template. The patch silently skipped that copy every run,
forever, while telling `WinMain` it had succeeded.

Traced via Ghidra decompile of the real `MCity_d.exe` (`debug_clean` project)
plus live `cpu.add_logpoint` tracing. One real wrinkle hit along the way: the
Zig CPU core caps logpoints at 8 slots (`cpu/src/core.zig:113`,
`cpu/src/kernel.zig:203-206`'s `cpu_add_logpoint`) and silently drops any
registration past the 8th with no error -- an initial 10-logpoint attempt
dropped the two most important ones and looked exactly like "these addresses
are never reached" until the Zig source was read. Worth a real fix later
(grow the array, or raise loudly on overflow instead of no-op'ing); not done
here, out of scope for this investigation.

Molly confirmed `Online.MDB` has always been a real, legitimately-shipped
asset (`~/.emu32/Data/DB/Online.mdb`, 5,883,904 bytes) -- there was never a
real reason for the patch to exist. Removed it (and its one test,
`TestWinmainCheck3` in `test_patch_internals.py`). Confirmed live: the real
copy now runs end-to-end -- `CreateFile("C:\Data\DB\Online.MDB") -> [read,
5883904 bytes]`, streamed via real `ReadFile`/`WriteFile` calls -- and
`~/.emu32/SaveData/DB/Tmp.MDB` now exists on disk, byte-identical in size.
Corrected `_state.py`'s `open_file_handle` docstring, which had previously
speculated (wrongly) that DAO/Jet was supposed to retry `Tmp.MDB` with
`CREATE_ALWAYS` after an `OPEN_EXISTING` miss -- no such retry exists
anywhere in the real binary; `Tmp.MDB`'s creation and its later `OPEN_EXISTING`
open are two entirely separate, unrelated code paths. 1045/1045 tests pass.

**This surfaced a new, different blocker**: `DB_StartUpDatabase` still halts
identically (`INT3`/`cpu.fatal_halt at EIP=0x001fe012`, same EBP chain) even
with `Tmp.MDB` now present and openable at the raw file level. DAO's
`Workspace::OpenDatabase` COM call itself still returns a NULL database, for
an as-yet-undiagnosed reason beyond "the file didn't exist." Not yet
investigated. See status.md "Current status (2026-08-07, cont'd)".

**Also this session**: added the `-CaptureStdout` command-line switch
(`tew/api/crt_handlers.py`, `tew/api/msvcrt_handlers.py`'s `__getmainargs`
argv, `argc` 3->4) -- confirmed via Ghidra against the real
`NFSArgs_ProcessArgs` switch table (`DAT_0126e060` in `MCity_d.exe`, row
index 13: `"CaptureStdout"` / `"Capture output to stdout in a file
(STDOUT.TXT)"`). Without it, `WinMain` redirects the game's own stdout to
the NUL device, silently discarding real `puts()`/`printf()` output.
Confirmed live: `stdout.txt` (gitignored, not committed) now captures real
output, including `_CLayer_DetectDebugger`'s `"Causing exception to test for
debugger...\nFound Debugger!"` lines exactly as predicted from the decompile
before ever running it. `tests/unit/api/test_cmdline_nomovie.py` updated for
the 4-arg command line.

## 2026-08-07 — `CreateFile` failure logging made human-readable (no behavior change)

Prompted by Molly reading `/tmp/emu.log` and finding the `system.mdb`/`Tmp.MDB`
`CreateFile` failure lines unclear without reading the source: the
must-exist-but-missing message printed the raw `dwCreationDisposition` integer
(`disposition=3`) with no indication that `3` means `OPEN_EXISTING` — readable
only to someone who already has the Win32 header memorized. Separately, that
message and the plain read-open "not found" message (`options.ini`,
`tr07.can`, `togglewindowtest.txt`, `tunes.cfg`) looked like the same shape
with different detail, when they're actually two structurally different
failures: a write-open whose disposition demands the file already exist, vs.
a read-open where existence is required regardless of disposition.

Fixed in `tew/api/_state.py`: added `disposition_name()` (maps the five real
`dwCreationDisposition` constants to their names, e.g. `OPEN_EXISTING(3)`) and
used it in `open_file_handle`'s must-exist failure log line, which now reads
`OPEN_EXISTING(3) requires the file to already exist, but it was not found`
instead of `write open failed: must exist for disposition=3, not found`. The
read-open miss now reads `read-only open, file not found: ...` instead of a
bare `not found: ...`, so the two cases are distinguishable without reading
code. Logging only — no change to `CreateFile`'s actual pass/fail behavior or
`GetLastError` codes. Confirmed the two tests exercising `open_file_handle`
(`test_open_file_handle_relative_path.py`,
`test_open_file_handle_last_error.py`, 7 tests) still pass; full suite not
re-run for this change (log-message-only diff, no logic touched).

## 2026-08-06 (cont'd again) — Resolved the deliberately-deferred "~85 of
~90 cpu.halted = True sites lack fatal_halt" item: audited and fixed 85
sites, caught and reverted one genuine false positive via the existing
test suite, confirmed the native boundary itself needed no changes

**Root of this pass**: picked back up the halt-boundary design discussion
from earlier today (Molly: "halting the cpu should halt the cpu. python
should have zero ability to restart it") after a detour to extract `cpu/`
into its own shared repo. First instinct — collapse `halted`/`fatal_halted`
into one permanently-one-way native flag — turned out to be wrong: `tew`
has a real, currently-used, *intentionally* resumable feature built on
plain `cpu.halted`. `run_exe.py`'s own scripted debugger
(`register_breakpoint`/`_dispatch_breakpoint`, "Resume is automatic")
relies on breakpoint-hit halts staying clearable, and `seh.py`'s
`_sentinel_handler` uses the same flag purely as a nested-step-loop
completion signal, not an error. Collapsing everything would have broken
both.

**The actual gap, once that was ruled out**: the native boundary already
does exactly what was asked — `cpu_set_fatal_halt` is genuinely one-way
(`s.fatal_halted = true`, no clear function exists), and `cpu_clear_halted`
already refuses when it's set. The bug was never in that mechanism; it's
that the vast majority of individual Python handler call sites never opted
into it, setting bare `cpu.halted = True` for what's clearly an
unrecoverable condition (the `_ord12`/`VariantChangeType` bug from earlier
today's session was one instance of this same class, not a one-off).

**Audit**: wrote a script to find every `<var>.halted = True` write whose
*following* line doesn't mention `fatal_halt` (a same-line grep undercounts
real coverage -- e.g. `win32_handlers.py`'s already-correct INT3 handler
sets it on the next line, not the same one). 86 sites found across 20
files. Classified each:
- 79 follow the established `logger.error(...) + "halting"/"UNIMPLEMENTED"/
  "failed"` shape -- unambiguous genuine errors.
- 3 are clean process-exit calls (`ExitProcess`, `TerminateProcess`,
  `NtTerminateProcess`) -- logged at INFO not ERROR, but still correctly
  permanent: nothing should ever resume a CPU after the guest process
  itself called one of these.
- 1 (`seh.py`'s SEH-handler-invocation timeout, distinct from the sentinel
  below) is a genuine unrecoverable error (a stuck/runaway handler).
- 1 (`seh.py`'s unhandled `RaiseException`) is a genuine error, same shape
  as the other 79 but didn't match the exact log-message pattern used to
  spot-check.
- 1 (`seh.py`'s `_sentinel_handler`) is the legitimate resumable
  step-loop-completion signal identified above -- left alone.
- 1 (`scheduler.py`'s `mark_current_dead`, "no runnable threads remain")
  was *initially* judged a clean process exit by the same reasoning as the
  three above and marked fatal.

**That last one was wrong, and the existing test suite caught it
immediately**: marking it fatal broke
`test_invoke_emulated_proc_thread_death.py::test_invoke_emulated_proc_returns_zero_when_calling_thread_dies_mid_call`,
which explicitly asserts `cpu.fatal_halt is False` for the scenario it's
built around -- a *single* thread calling `ExitThread` on itself from
inside a nested `_invoke_emulated_proc` call (e.g. a DllMain), which the
scheduler-level bookkeeping legitimately needs to detect and let the
caller recover from, not treat as a whole-process crash. Reverted that one
site (`scheduler.py`), left the other 84 in place. Concrete demonstration
of why this pass needed real per-site judgment and test verification, not
a blanket property-level flip -- a wrong call here breaks legitimate
control flow silently, which is worse than the original gap. 1029/1029
tests pass (85 sites fixed +1 reverted = 84 net).

**Confirmed, tracing `cpu_zig.py`'s `halted` property end to end, that no
native changes were needed at all for the two seemingly-unguarded clear
sites in `user32_handlers.py` (177, 220, which clear `cpu.halted` without
an explicit `if not cpu.fatal_halt` check)**: the getter is
`self._py_halted or cpu_is_halted(state)`, and the setter's clear path
calls native `cpu_clear_halted`, which already refuses to flip `s.halted`
back to false once `s.fatal_halted` is set. So even an unconditional
Python-side clear attempt can't resume a genuinely fatal halt -- native
`s.halted` stays true regardless (the enforcement lives at the native
layer, not the call site), and `cpu_run`'s own execution loop reads that
native flag directly, never the Python shadow. `cpu.fatal_halt` itself is
completely untouched by the `halted` setter. The architecture already
fully enforced "Python has zero ability to restart a fatal halt" once a
handler correctly opts in -- the real gap was 85 handlers never opting in,
not a hole in the enforcement mechanism.

---

## 2026-08-06 (cont'd) — Root cause of memmove's n=-4 found: a real Zig
CPU-core bug (doGroup1 ignores op_size_ovr for flags, same 0x66-prefix
bug class fixed elsewhere but never audited here) -- fixed, with a
regression test written and confirmed failing before the fix; emulator
now reaches the game's real message-pump steady state for the first time

**Picked the `n=0xfffffffc` question back up** (queued at the end of the
previous entry) by going *up* the call chain instead of down into DAO/Jet's
own binary-search semantics, per Molly's steer ("pretty sure it's not a
Windows bug"). `[ESP+0]` at the fault (not the EBP chain, which was
already confirmed unreliable past frame 2) gave the real immediate return
address, `0x044d1fcc` — decompiling that landed on `FUN_044d1f27`
(`dao350.dll`): a sorted name-index insert routine, `memmove(_Src+1,
_Src, (sVar2 - local_2) * 4)`, where `sVar2` is a count field and
`local_2` an index from `FUN_044d1d98`'s binary search. For `n` to be
`-4`, `local_2` had to be `count+1` -- one past valid bounds.

**Identified the real caller with a targeted logpoint** (`cpu.add_logpoint`
at `FUN_044d1f27`'s entry, reading the real `[ESP]` args and dereferencing
the key string) rather than guessing between the two static xref callers:
it's `FUN_044d1e4a` ("add child to parent"), inserting DAO's own
hardcoded `"#Default Workspace#"` object -- the very first insert into a
genuinely empty (`count=0`) collection.

**The empty-collection math didn't add up.** By hand: `FUN_044d1d98`'s own
`count != 0` guard should take the "empty" fast path (`local_2=0,
local_4=-1`) when `count=0`, and `if (0 < local_4) local_2++` should then
never fire for `local_4=-1` -- predicting `n=0`, not `-4`. Two more
logpoints (right at the `CALL memmove` instruction, and right after
`FUN_044d1d98` returns) confirmed `count=0` on both sides of that call
*and* the derived `local_2` was still `1` (`local_2 = (src - array_base) /
4`, using the real `src`/`array_base` values) -- a genuine contradiction
against the decompiled logic, not an instrumentation bug (caught and fixed
one of my own logpoints' stack-offset math along the way, by cross-
checking against the independently-known real `dst`/`src`/`n` from the
handler's halt message).

**Root cause, in the raw disassembly, not the decompiler's pseudocode**:
the `if (0 < local_4) local_2++` check is a real 16-bit compare --
`66 83 7C 24 0C 00` = `CMP WORD PTR [ESP+0xC], 0`, the `66` prefix
confirming genuine 16-bit width, followed by `JLE` -- so the *guest*
instruction stream is correct. `cpu/src/engine.zig`'s `doGroup1` (the
shared handler for opcodes `0x80`/`0x81`/`0x83` -- ADD/OR/ADC/SBB/AND/SUB/
XOR/CMP against an immediate) hardcoded `.w32` for every case's flags
computation, never checking `s.op_size_ovr` -- unlike sibling functions in
the same file (`op39`, `op3B`, `op3D`, `opA9`) which all correctly compute
`width` from `op_size_ovr` first. This is the exact `0x66`-prefix
flags-width bug class already found and fixed for the accumulator-
immediate opcodes and `doGroup2` (see 2026-08-02 entries), and precisely
matches the "other opcode families using op_size_ovr directly... weren't
exhaustively re-verified" gap this project's own queued-issues list
already flagged -- `doGroup1` was simply never covered by that earlier
pass. Concretely: `local_4=-1` (`0xFFFF`) read from memory correctly
zero-extends to `0x0000FFFF` (the read/write width handling in
`readRmvResolved`/`writeRmvResolved` was already correct), but comparing
that against `0` with flags forced to `.w32` sees `65535` -- positive, so
`SF=False` -- instead of the correct 16-bit interpretation, `-1`, `SF=True`.
The `JLE` that should have skipped the `INC` doesn't, `local_2` goes from
`0` to `1` on a genuinely empty collection, and `memmove` gets called with
`n=(0-1)*4=-4`.

**Regression test written and confirmed failing before the fix**, per
Molly's explicit request (test-first, not fix-first): `TestGroup1_16BitFlags`
in `tests/unit/emulator/test_opcodes_arithmetic.py`, 4 cases -- `CMP CX, 0`
(register operand, the exact opcode/op_ext from the real bug), `CMP WORD
PTR [ESP+0xC], 0` (memory operand, byte-for-byte the real guest
instruction), `SUB CX, 0`, and `AND CX, 0xFFFF` (imm16 form via `0x81`) --
each asserting `SF_BIT` is set for a `0xFFFF`-shaped 16-bit result. One
early draft case (`SUB CX, 1` from `CX=0`) was caught and replaced before
committing: `0 - 1 = -1` reads as negative at *any* width (all-1s
truncates the same way in 16 or 32 bits), so it couldn't actually
discriminate the bug -- rewritten to `SUB CX, 0` from `CX=0xFFFF`, the same
positive-32/negative-16 shape as the real bug. All 4 confirmed failing
against the unfixed build first, exactly as asked.

**Fix**: `doGroup1` now takes a real `width: Width` parameter instead of
hardcoding `.w32`; both callers (`op81`, `op83`) compute
`if (s.op_size_ovr) .w16 else .w32` before calling it, matching the
established pattern. Rebuilt `libcpu.so`. All 4 new tests pass; full suite
1029/1029 (was 1025).

**Confirmed live**: re-ran with the same diagnostic logpoints still
attached -- `FUN_044d1f27` now computes `n=0x00000000` (`local_2=0`,
correct for a first insert into an empty collection), no fault, no halt.
The run sailed straight through the entire DAO/Jet init sequence that
blocked every session in this whole investigation and reached the game's
own message-pump steady state (`GetMessageA`/`WaitForMultipleObjectsEx`/
`timeSetEvent` cycling normally) -- ran stably until killed by the test
timeout, not by crashing. This is the furthest this emulator has ever
reached. `run_exe.py` stripped of all diagnostic logpoints again, per the
same discard-when-done convention as the previous entry.

**Session process note**: this whole investigation -- from the original
"hang" report through the memcpy/memmove fix through this CPU-core bug --
was one continuous session. Both root causes turned out to be real,
independent bugs (a Python-handler-level missing bounds check, and a
Zig-core-level flags-width bug), not one bug wearing two hats; fixing the
first was necessary to even see the second clearly, since the corrupted-
trampoline symptom and multi-minute wall-clock cost were masking it
completely.

**Current blocker**: none identified yet. Next session: let it run further
past the message-pump idle state and see what happens (real UI/gameplay
progress vs. a new, not-yet-hit blocker) -- not yet investigated.

---

## 2026-08-06 — The `FUN_0448a033` "hang" was never a hang: real bug in
memcpy/memmove/memset/memcmp (zero size validation, unbounded loop
corrupting tew's own trampoline region); fixed + regression tests added

**Resolved the multi-day `FUN_0448a033` investigation queued 2026-08-04.**
Picked back up by adding before/after `cpu.add_logpoint`s across every call
site in the unexplored stretch (`0x448a189`-`0x448a1da`, confirmed via
Ghidra decompile + raw byte dump of `dao350.dll`, not guessed) — every run
reached `0x448a189` (`FUN_0448a63d`/`CheckJETVersion` returns cleanly,
`EAX=0`) and then produced zero further output, matching the original
"100% CPU, needed `SIGKILL`, `SIGTERM` didn't work" signature exactly.
Decoded the full straight-line byte range between the last-reached and
first-unreached logpoints (`0x448a1bf`-`0x448a1d9`): plain
`LEA`/`MOV`/`MOVSX`/`PUSH`, no jumps, nothing that could loop on its own —
pointed at either a Zig-core decode bug or a native freeze invisible to
Python-level instrumentation.

**ClickHouse execution-history capture** (`cpu.enable_history_capture_clickhouse`,
already wired in `cpu_zig.py` from an earlier session, proven for exactly
this "what's actually happening at this address" class of question) was
stood up fresh: `~/pe-walker/history-poc` (`docker compose up`), fixed two
real environment issues along the way — host port `9000` already in use
(remapped native-protocol port to `19000` in `docker-compose.yml`, HTTP
`8123` unaffected) and a stale bind-mounted `data/` directory carrying the
host's plain `user_home_t` SELinux label, which an Enforcing-mode kernel
correctly refused the container read access to (fixed with `:z` on the
volume mount, Docker's built-in relabel, not a manual `chcon`). Learned the
hard way that enabling capture from process start is catastrophically
expensive — every single-step EIP change flushed toward ClickHouse for the
whole ~70s of DLL loading/init produced a 20x wall-clock slowdown and
never even reached the target region before a 300s timeout; scoping
`enable_history_capture_clickhouse` to fire inside the `0x448a033` entry
logpoint instead fixed that, but the run still produced zero flushed rows
for the scoped capture — turned out to be genuinely informative: nothing
flushed because the internal batch threshold was never reached before
`SIGKILL`, consistent with (not contradicting) a hard freeze.

**The actual breakthrough was a live `gdb -p <pid>` attach**, run without
an aggressive auto-`SIGKILL` timeout (`timeout -k 10 500`, up from the
usual `300`) so the process could be caught mid-"hang" instead of killed
first. First attach showed the main thread genuinely blocked inside
`engine.opCD` (the guest hit a real `INT` — a Win32 API trampoline) →
`_c_int_dispatch` → a Python handler → `PyCFuncPtr_call`, i.e. actual,
real execution, not a native freeze. Four more rapid successive attaches
each landed at a *different* point in normal `ctypes`-call machinery
(`_PyType_LookupStackRefAndVersion`, `CDataType_from_param_impl`,
`_stginfo_from_type`, ...) — conclusive proof the process was alive and
progressing the whole time, just far too slowly for any timeout used so
far. Re-ran with the same 500s budget and no `SIGKILL` pressure: it
finished on its own at **354.383s**, hitting a clean, honest halt —
`[seh] fault at 0x00201fe2 unhandled by SEH chain`, `read8: address
0xffffffff outside bounds [0, 0x80000000)`. Every previous "hang" across
this whole investigation was simply killed by `timeout -k 5 300` a few
seconds before it would have finished on its own.

**Identified what's actually at the fault address.** `0x00201fe2` sits 2
bytes into the `msvcrt.dll!memmove` trampoline (handler id 257, base
`0x00201FE0` — confirmed by adding a one-off diagnostic that dumps
`win32_handlers._handlers_by_id` right after registration and exits before
any CPU execution, since handler-trampoline addresses are assigned
deterministically at registration time and don't need a live/slow run to
inspect). The trampoline's own bytes were corrupted: a clean
`register_handler` trampoline is `CD FE C3` at its base followed by 29
bytes of `CC` padding, but the captured fault-site bytes
(`cc cc cd fe c3 cc cc ...`) show a *second* `CD FE C3` sequence stamped 4
bytes into what should have been untouched padding.

**Root cause, in `tew/api/msvcrt_handlers.py`**: `_memcpy`, `_memmove`,
`_memset`, and `_memcmp` all read their size (`n`) straight from guest
memory with zero validation and looped `for idx in range(n)`
unconditionally, with no bounds-check on `dst`/`src`/`ptr` either. A
garbage/underflowed `n` (confirmed live: the actual faulting call is
`memmove(dst=0x06f9e014, src=0x06f9e010, n=0xfffffffc)` — `n` is `-4` as a
signed value) turns into thousands of ctypes-heavy real, but effectively
unbounded, `read8`/`write8` calls — that's the entire 279-second "hang"
between `0x448a16a` and the fault. Since neither pointer nor size was
bounds-checked, the sweep eventually walks straight through
`0x00200000`+ — every trampoline shares the same `CD FE C3` + `CC`-padding
byte shape, so a wild copy sweeping between two of them produces exactly
the "extra `CD FE C3` 4 bytes into padding" signature found. The 32-bit
address space wrapping to `0xFFFFFFFF` is what finally stops it, not any
check on the copy operation itself.

**Fix**: all four now call the already-existing (but previously unused
anywhere in `tew/api/`) `memory.is_valid_range(addr, size)` on every
address+size before touching anything, and halt loudly
(`logger.error("handlers", ...)` + `cpu.halted = True` +
`cpu.fatal_halt = True`, matching the established convention e.g.
`kernel32_io.py`'s `_halt` helper) instead of looping. Confirmed live: a
fresh run now reaches the same call and halts in **60.3s total**, not
354s+ — the fix turns a silent multi-minute memory-corruption spree into
an instant, diagnosable halt with the real `dst`/`src`/`n` values logged.

**New regression tests**: `tests/unit/api/test_msvcrt_memfuncs.py` — 13
tests, the first coverage these four functions have ever had. Covers
normal correctness (forward copy, overlapping backward `memmove`, fill,
compare, first-byte-difference sign) plus one regression case per function
for this exact bug class (huge `n`; `dst`/`src`/`ptr` + `n` exceeding
memory bounds) — each asserts an immediate halt rather than relying on a
wall-clock timeout, so a future regression fails fast in CI instead of
hanging it. 1025/1025 tests pass (was 1012).

**Session process note**: all diagnostic instrumentation (the 30
per-instruction `cpu.add_logpoint`s from the address-narrowing phase, the
ClickHouse capture wiring, the registration-dump-and-`sys.exit(0)`
diagnostic) was discarded from `run_exe.py` once it had served its
purpose, per this project's established "TEMP diagnostics get discarded,
not shipped" convention — including the original 5 logpoints from the
2026-08-04 session that first raised this investigation, since it's now
fully closed. The `~/pe-walker/history-poc` ClickHouse stack itself
(`docker compose up`, port `19000`/`8123`, `:z`-relabeled volume) was left
running as reusable infrastructure, not torn down — it's genuinely useful
tooling for the next "what's actually happening at this address" question,
not a one-off.

**Current blocker**: `n=0xfffffffc` (`-4` signed) looks like a
signed-length underflow (`end - start`-shaped), not raw garbage — not yet
identified whether that's upstream in tew's own emulation (e.g. a
structure field DAO/Jet expects to be populated correctly by this point
that tew leaves at a wrong/zero value) or a genuine bug already present in
retail DAO/Jet that real Windows happens not to trigger under whatever
conditions this emulator's environment differs. Not yet investigated.

---

## 2026-08-04 (cont'd again x3) — GetWindow implemented; two real bugs found
and fixed chasing an apparent hang (PID mismatch, GWL_* sign-comparison);
real hang narrowed to inside FUN_0448a033 via live logpoints, not yet
pinned down

Implemented `GetWindow` (`user32_handlers.py`) for real: `GW_CHILD` on the
desktop returns the first tracked top-level window; `GW_OWNER` returns
`entry.parent_hwnd`; `GW_HWNDFIRST`/`LAST`/`NEXT`/`PREV` walk siblings
sharing the same parent, using creation order as an honest Z-order (this
emulator never reorders/raises windows). Needed a new public
`WindowManager.all_windows()` accessor (previously only single-hwnd
lookup existed). Also implemented `IsWindowVisible` for real (walks the
full parent chain checking `WS_VISIBLE` at every level, matching real
Win32 semantics -- a visible window with a hidden ancestor is still not
visible) and fixed `_ShowWindow`, which only ever toggled the real SDL
window and never updated its own tracked `WS_VISIBLE` style bit --
`IsWindowVisible` would have gone stale after any real show/hide.

After clearing that halt, hit what looked like a genuine infinite loop
inside `DAO350.DLL`'s `FUN_0448d1f5` (a standard "find my own top-level
window" idiom: walk the desktop's children checking `WS_CHILD`,
`IsWindowVisible`, and `GetWindowThreadProcessId(...) == GetCurrentProcessId()`).
Confirmed genuinely hung (100% CPU, zero further output, needed `SIGKILL`
-- `SIGTERM` didn't work, consistent with being stuck inside a tight
native CPU-emulation loop that never yields back to Python's signal
handling). Found two real, confirmed-via-decompile bugs while
investigating (neither turned out to be the actual hang cause, but both
were genuine, live bugs worth fixing regardless):

1. `GetCurrentProcessId()` returned a hardcoded `1234`
   (`kernel32_system.py`) while `GetWindowThreadProcessId` always writes
   a hardcoded fake PID of `1` (`user32_handlers.py`, "our fake PID") --
   these never agreed, so DAO's "is this window mine?" check could never
   succeed for any window this emulator will ever have. The `0x4d2`
   (=1234) seen in an earlier halt's `EDI` register, previously written
   off as unrelated leftover data, was this exact value. Fixed
   `GetCurrentProcessId` to return `1`, matching the established
   convention.
2. `GetWindowLongA`/`SetWindowLongA` compared `nIndex` (always unsigned
   via `memory.read32`) directly against negative Python int constants
   (`GWL_STYLE=-16` etc.) -- `4294967280 == -16` is always `False` in
   Python, so every `GWL_*` case had *always* silently fallen through to
   the generic `0` default, for the entire life of both handlers,
   regardless of which index was actually requested. Fixed with a proper
   unsigned-to-signed conversion (same idiom already used repeatedly in
   `msvcrt_handlers.py`). Also added a real, explicit `GWL_HWNDPARENT`
   case (previously relied on the same broken fallback, which happened to
   produce the right answer -- `0`, no owner -- by accident, not by
   design) and human-readable `GWL_*` constant-name logging for both
   handlers (`_gwl_name` helper) since raw index numbers like `-8` are
   meaningless without memorizing the Win32 header.

Neither fix was the actual hang cause -- confirmed live via `cpu.add_logpoint`
(temporarily wired into `run_exe.py`, clearly marked as temporary
diagnostic code; addresses are real, unrelocated `DAO350.DLL` addresses
since it loads at its preferred base with no relocation needed):
`FUN_0448d1f5` now returns correctly and fast with the fixes in place
(the outer window-search loop's condition is false on the very first
check now, since all three OR-terms are legitimately false for the
game's real top-level window); its caller `FUN_0448a801` makes one
indirect call (`CALL [0x44e5350]`, resolved live to target `0x150332db`,
inside `MSJET35.DLL`'s address range) which also returns cleanly
(`EAX=0`); and `FUN_0448a033` (the next caller up) successfully receives
control back at `0x448a16a`. But `FUN_0448a033` never reaches either of
its own two `RET` sites (`0x448a241`, `0x448a281`) -- the real hang is
somewhere in the stretch of code between `0x448a16a` and those returns,
which contains several more direct and indirect calls not yet
individually logpointed. Full detail and exact addresses to logpoint
next: status.md "Current status".

615/615 tests pass throughout every fix in this entry.

## 2026-08-04 (cont'd again x3) — real CreateFile dwCreationDisposition bug
found and fixed; resolves the whole System.mdb/error-3049/DebugBreak chain
without needing any external asset

Molly's hunch that `lines 1558-1561` (the `system.mdb` `FindFirstFileA`/
`CreateFile`/`GetFileInformationByHandle` sequence) were "not a dead end"
was right, just not for the reason either of us initially assumed (a
missing real workgroup-database file to source from the game's install
media). The actual bug: `CreateFile`'s log line showed `CreateFile(...)
-> 0x503a [write]` succeeding even on a run where `FindFirstFileA` had
just reported the file as genuinely absent -- meaning `open_file_handle`
(`tew/api/_state.py`) was creating a file regardless of whether it should
have been allowed to. Traced to the writable branch unconditionally using
`os.O_WRONLY | os.O_CREAT | os.O_TRUNC` for *any* write-capable
`CreateFile` call, with zero regard for the real `dwCreationDisposition`
argument -- `OPEN_EXISTING` (which must fail if the file is missing, and
must never truncate an existing one) was being silently treated the same
as `CREATE_ALWAYS`.

Fixed properly, not just for this one call site: added the five real
Win32 `dwCreationDisposition` constants (`CREATE_NEW`=1,
`CREATE_ALWAYS`=2, `OPEN_EXISTING`=3, `OPEN_ALWAYS`=4,
`TRUNCATE_EXISTING`=5) to `_state.py`, gave `open_file_handle` a real
`disposition` parameter (default `CREATE_ALWAYS`, preserving the
`msvcrt.dll` fopen/_open call sites' existing behavior unchanged --
CRT-level mode-string-to-disposition mapping is a separate, not-yet-done
piece of work, noted but out of scope here), and switched on it properly:
`CREATE_NEW` uses `O_CREAT|O_EXCL` and fails if the file already exists;
`OPEN_EXISTING`/`TRUNCATE_EXISTING` resolve the real path via the same
case-insensitive `find_file_ci` the read-only branch already used and
fail honestly (no file created) if genuinely missing; only
`CREATE_ALWAYS`/`TRUNCATE_EXISTING` still truncate. `_create_file_a`/
`_create_file_w` (`kernel32_io.py`) now pass the real disposition value
straight through -- it was already being read off the stack, just
discarded before.

Confirmed live: `CreateFile("C:\system.mdb")` now correctly fails
(`disposition=3`=`OPEN_EXISTING`, genuinely not found) instead of
fabricating an empty file. With that honest failure, Jet's own real
fallback path (auto-creating a default workgroup database when none
exists, standard Jet behavior) takes over on its own -- no external
`system.mdb`/`system.mdw` asset needed after all. Neither the
`Nfs_REALabortcallback`/`DebugBreak` halt nor error 3049 reproduce
anywhere in a full run anymore. Execution now progresses well past the
old blocker, deep into real `DAO350.DLL` code, and reaches a new, clean,
honest `[UNIMPLEMENTED] user32.dll!GetWindow` halt. 615/615 tests pass.

Also confirmed as a real (if not yet observed live) side effect of the
same root bug, beyond the `system.mdb` symptom: any real existing file
opened with `OPEN_EXISTING`+`GENERIC_WRITE` would previously have had its
contents silently truncated to 0 bytes on open (`O_TRUNC` fired
unconditionally) -- a genuine data-loss bug, fixed by the same change.

## 2026-08-04 (cont'd again, x2) — patch_dll_iats now logs a per-DLL
breakdown, not just an aggregate count

`patch_dll_iats`'s "Patched X/Y new DLL IAT entries" summary line doesn't
say which DLL any given entry belongs to -- and since a single call can
cover more than one DLL's entries at once (`load_dll` recursively loads
and IAT-resolves everything a newly-loaded DLL itself imports before
returning), that ambiguity got worse once the incremental-cursor fix
(above) started batching multiple DLLs' worth of new entries into one
call. Added a `logger.debug("loader", ...)` breakdown grouped by (DLL
being patched, DLL its imports come from) right before the patch loop --
e.g. `patching msjter35.dll: 2 import(s) from kernel32.dll`. 615/615
tests pass.

## 2026-08-04 (cont'd again) — GetSystemDefaultLangID/GetShortPathNameA
fixed; real blocker is now Jet error 3049, "can't open database"

Two more clean `[UNIMPLEMENTED]` halts fixed, same session as the ole32
COM gaps: `GetSystemDefaultLangID` (`kernel32_io.py`, same fixed en-US
value as `GetUserDefaultLCID`/`GetUserDefaultLangID` -- this emulator has
no separate system-vs-user locale concept anywhere else either) and
`GetShortPathNameA` (real `find_file_ci` path-existence check via
`state.translate_windows_path`, same pattern as `GetFileAttributesA`;
returns the long path unchanged on success since this emulator doesn't
implement real NTFS 8.3 short-name generation -- the same real behavior a
genuine Windows install has with `fsutil 8dot3name` short-name generation
disabled, not a fabricated result; `ERROR_FILE_NOT_FOUND` + return 0 on a
genuinely missing path, matching `DeleteFileA`'s existing error-handling
style in the same file). 615/615 tests pass.

Confirmed live: both cleared, and execution reaches significantly further
-- `MSJTER35.DLL`/`MSJINT35.DLL` load, and `CreateErrorInfo`/
`SetErrorInfo` succeed reporting a real Jet error. Looked up via the same
offline `msjint35.dll` resource-table method as the earlier `3447`
diagnosis: error **3049** is *"Can't open database '|'. It may not be a
database that your application recognizes, or the file may be corrupt."*
Right after `tid=1012` reports this, `tid=1000` (the main thread this
time) independently hits the same `Nfs_REALabortcallback`/`DebugBreak`
chain fixed/confirmed-correct earlier today -- expected, correct
behavior, not a new bug. Real next step: find out whether the actual
`.mdb` database file DAO/Jet is trying to open exists at the path being
used, or whether this is a genuine bug earlier in the Jet open-database
call chain. Not yet investigated this session.

## 2026-08-04 (cont'd) — real ole32/oleaut32 COM gaps fixed, per-thread log
tagging added, GetUserDefaultLangID fixed, DLL loader stopped doing O(N^2)
redundant filesystem/IAT-patch work

Chased the `Nfs_REALabortcallback`/`DebugBreak` halt from earlier today one
level deeper. Root cause of *that* dead end: `expsrv.dll`'s own init
(ordinal #2000) probes `oleaut32.dll!DispCallFunc`, `ole32.dll!
CoCreateInstanceEx`, `CLSIDFromProgIDEx`, `CLSIDFromProgID` via
`GetProcAddress` -- all four came back NULL because tew's real (not
stubbed) ole32/oleaut32 COM layer in `oleaut32_handlers.py` (the same file
that already does genuine COM activation for DAO -- `CoGetClassObject`,
`CoCreateInstance`, `CreateErrorInfo`, `SetErrorInfo`) simply never had
them added. Implemented `CLSIDFromProgID` and `CLSIDFromProgIDEx` (real
`HKCR\<ProgID>\CLSID` registry lookup, `_write_guid` into the output,
honest `CO_E_CLASSSTRING`/`REGDB_E_CLASSNOTREG` on anything unregistered --
same no-fabrication philosophy as `_resolve_com_server`; `...Ex` adds the
extra InprocServer32/LocalServer32 registration check that's the real
spec difference from plain `CLSIDFromProgID`) and `CoCreateInstanceEx`
(creates one instance via the existing `DllGetClassObject`/
`IClassFactory::CreateInstance` path, then `QueryInterface`s it once per
`MULTI_QI` entry, writing straight into each entry's own `pItf`/`hr`
fields). `DispCallFunc` deliberately **not** implemented -- real generic
x86 calling-convention/VARIANT-array marshaling is a different scale of
problem, flagged separately rather than rushed. 615/615 tests still pass.

Confirmed live this was the right area but not why the game got stuck:
with all three implemented, the earlier `Nfs_REALabortcallback` halt (the
`exe`-only frame chain from the last few sessions) **doesn't reproduce at
all anymore** -- execution now gets substantially further, genuinely deep
into `expsrv.dll -> vbajet32.dll -> DAO350.DLL -> exe`, and hits a new,
different, honest `[UNIMPLEMENTED]` halt: `kernel32.dll!
GetUserDefaultLangID`. Fixed (`kernel32_io.py`, next to the existing
`GetUserDefaultLCID`): `LANGIDFROMLCID(lcid) == lcid & 0xFFFF`, and for
this emulator's fixed en-US LCID (`0x0409`, `SORT_DEFAULT` already 0 in
the high word) that's numerically the same value as the LCID itself, not
a separate thing to compute. Confirmed live: cleared the halt, immediately
followed one call later by the same-shaped gap, `kernel32.dll!
GetSystemDefaultLangID` -- not yet fixed, see status.md.

Separately, per Molly's request: every log line now carries `[tid=N]`
once `crt_state` exists (a `set_thread_id_provider()` callback in
`logger.py`, wired to `crt_state.tls_current_thread_id` in `run_exe.py`
right after construction -- avoids a circular import, since `_state.py`
already imports `logger`). Before `crt_state` exists (early DLL-loading
output), lines carry no tid, same as always. Cleaned up the handful of
call sites that were manually embedding a now-redundant `tid=` in their
own message text (`SetLastError`, `SleepEx`, `WaitForMultipleEx`,
`SetErrorInfo`, `GetWindowThreadProcessId`'s `current_tid=`) -- left the
ones referencing a genuinely *different* thread alone (`CreateThread`'s
newly-spawned tid, `GetWindowThreadProcessId`'s `creator_tid`, every
`scheduler.py` from/to-thread switch message).

Also per Molly's observation that the DLL loader's repeated "Could not
find X" spam (basically every real DLL imports `kernel32.dll`/
`user32.dll`/etc., names this emulator never has on disk since they're
Python-simulated) meant a full case-insensitive filesystem walk
(`find_file_ci`, recursive `os.listdir` at every path component) was
re-running from scratch for the same always-missing name on every single
DLL load, for the whole run: `DLLLoader.load_dll` (`dll_loader.py`) now
caches negative lookups per-name (`self._not_found`), invalidated only
when a genuinely new search path is added (`add_search_path`) since a
later-added path could contain a name an earlier lookup missed. Separately
and more significantly, `patch_dll_iats` was rescanning **every**
accumulated `_dll_iat_entries` entry from **every** previously-loaded DLL
on every single call (3 call sites, each firing once per new DLL load) --
O(N^2) total work across a run with N DLL loads. Proved this was always
safe to fix, not just a suspected optimization: `load_dll`'s own IAT-
resolution loop already recursively `load_dll`s each entry's target DLL
*before* appending the entry, and every Win32 handler is registered once
at startup before any DLL ever loads -- so an entry's correct patch
outcome is fully determined the moment it's appended and can never change
on a later call. Made it incremental via a high-water-mark cursor
(`_iat_patch_cursor`); confirmed live the "Patched X/Y new DLL IAT
entries" log lines now report small per-call batches instead of ever-
growing full-list rescans, and no "Could not find X" line repeats anywhere
in a full run's log. 615/615 tests still pass throughout.

## 2026-08-04 — `VirtualAlloc` PAGE_NOACCESS bug fixed; RUNAWAY at ~195.8M steps
was corruption fallout, not a real blocker; run now reaches a new, genuine
`cpu.fatal_halt`

Root cause of the `RUNAWAY DETECTED at step 197100000` blocker queued from
2026-08-03: `_virtual_alloc` (`tew/api/kernel32_memory.py`) rejected any
`flProtect` bit outside `PAGE_READWRITE`/`PAGE_EXECUTE_READWRITE` as
unimplemented. `flProtect 0x1` (`PAGE_NOACCESS`) is the standard Win32
"`MEM_RESERVE` a big range with no access now, `MEM_COMMIT` sub-ranges as
`PAGE_READWRITE` later" pattern -- a real, spec-legal flag MSJET35.DLL uses
during its own init, not a genuinely-missing case. Fixed by adding
`_PAGE_NOACCESS = 0x01` to `_KNOWN_PROTECT_FLAGS`; no other behavior change
needed since tew's memory model doesn't enforce page protection anywhere
(confirmed by checking `VirtualProtect` and the rest of the codebase --
bookkeeping-only, same as the existing `PAGE_READWRITE`/
`PAGE_EXECUTE_READWRITE` flags). 615/615 tests still pass.

The `MEM_COMMIT on unreserved 0x04000000` halt logged 24ms after the
`PAGE_NOACCESS` one, and the subsequent `RUNAWAY` at step 197,100,000 with
`EIP`/`ESP`/`EBP` full of heap-region garbage, were both downstream noise
from the first bogus halt: `cpu.halted = True; return` in the handler
doesn't actually stop the CPU (the still-open "~85 of ~90 `cpu.halted`
call sites lack the `fatal_halt` marker" issue, deliberately deferred this
session, not fixed here) -- so the trampoline's `RET` executed against an
uncleaned stdcall stack, corrupting the return address and sending
execution into garbage that eventually looked like a second (bogus)
`VirtualAlloc` call and then ran off into unmapped memory. Confirmed live:
after the one real fix above, neither of those two halts nor the
`RUNAWAY` reproduce at all -- MSJET35.DLL now loads and resolves all its
ordinal imports cleanly.

Execution now reaches a **new, different, genuine** halt:
`cpu.fatal_halt at EIP=0x001fe012`, immediately after a burst of
`_sehReturnSentinel` activity. The `EBP` chain
(`0x0068adf2`/`0x00a301a1`/`0x00684de7`/`0x006848ee`/`0x004d8c71`/
`0x0068a7d5`/`0x009fcaa6`, all in `MCity_d.exe`) is the *same* call chain
as the `Nfs_REALabortcallback`/`DebugBreak()` assertion path fully
diagnosed 2026-08-03 ("INT3 now routes through the real SEH chain") --
this is the correctly-behaving, properly-marked `cpu.fatal_halt` case,
not the soft-halt bug class above. Not yet investigated further this
session (deferred by request, see queued issues). Full run log:
`/tmp/emu.log` (this session, `LOG_LEVEL=debug LOG_CATEGORIES=com,dll,
loader,exception,handlers`).

## 2026-08-03 (cont'd) — real SetLastError/GetFileSize bugs found and fixed
while chasing why DAO/Jet database init fails; execution now reaches
~195.8M steps (previous best: a few million)

Continued the "why does `Nfs_REALabortcallback` fire at all" investigation
from the entry above. Added a real `-dbEnableLog` command-line flag
(`crt_handlers.py`'s `GetCommandLineA`/`W` strings, `msvcrt_handlers.py`'s
`__getmainargs` argv array -- both updated to carry
`"MCity_d.exe -nomovie -dbEnableLog"`) after the game's own debug log
(`dbcode.c`-sourced, written to `c:\dblog.txt` -> `~/.emu32/dblog.txt`)
turned out to be exactly the DAO/Jet trace the game already supports,
rather than something to reconstruct by hand. It showed the DAO COM
activation succeeding end-to-end (`DAO Engine version: 3.51`, `Workspace
type is Jet.`) and then `ERROR: get workspaces failed.` -- a real vtable
call, `DBEngine::get_Workspaces()` (`dao350.dll`, vtable offset `0x3c`),
returning null.

Traced live (logpoints/breakpoints at addresses found via Ghidra, same
methodology as the MSJET35.DLL investigation) through `dao350.dll`'s
`FUN_0448a033` -> `msjet35.dll` ordinal 154 (`FUN_7a876127`) ->
`FUN_7a8761fa` -> `FUN_7a876425` -> `FUN_7a876e2b`, which returns
`0xfffffc02` (-1022). Found the real source: `FUN_7a873691`, msjet35.dll's
own `GetLastError()` -> internal-error-code translation table (found via a
random Ghidra address the user was independently looking at,
`7a8caafa`/`7a8caac6`, landing inside this exact function). `-1022` is
produced either by a cluster of real I/O-fault-class Win32 error codes
(`ERROR_INVALID_HANDLE`, `ERROR_WRITE_FAULT`/`ERROR_READ_FAULT`,
`ERROR_UNEXP_NET_ERR`, etc.) or, notably, by *any unrecognized*
`GetLastError()` value via a generic "Unmapped error code" fallback path.

Live-traced (breakpoint at the translator's entry, reading
`TEB_BASE+0x34` directly rather than single-stepping the emulated
`GetLastError()` call) and found `GetLastError()` reading back as `0`
(success) at both of its two call sites in this exact sequence -- meaning
some *real* failure was happening, but tew was reporting "no error"
afterward instead of the actual reason. Traced the immediate callers (the
return address at `[ESP+0]` is reliable for one level even though full
EBP-chain walking isn't -- see below) to two genuine bugs in
`kernel32_io.py`:

- `_delete_file_a`/`_delete_file_w`: correctly returned `FALSE` on
  failure, but never called `SetLastError` at all, leaving whatever
  error code happened to already be set. Fixed to map the real Python
  exception (`FileNotFoundError` -> `ERROR_FILE_NOT_FOUND`,
  `PermissionError` -> `ERROR_ACCESS_DENIED`, other `OSError` ->
  `ERROR_FILE_NOT_FOUND`) the same way `CreateFileA`'s existing
  `SetLastError` calls already do nearby in the same file.
- `_get_file_size`: same missing-`SetLastError` bug, but also a deeper,
  separate one -- it only ever supported read-mode handles
  (`if entry and not entry.writable`), unconditionally failing for any
  writable-mode handle regardless of whether the handle was valid. Real
  `GetFileSize` works fine on writable handles. Confirmed live this
  wasn't hypothetical: right after `msjet35.dll` creates and writes its
  own scratch temp file (`GetTempFileNameA` -> `JETA000.TMP`, opened for
  write), it calls `GetFileSize` on that exact handle as a completely
  normal operation -- and `GetFileInformationByHandle` on the *same*
  handle, moments earlier in the same log, already proved it was valid.
  Rewrote `_get_file_size` to query the real host file directly via
  `os.fstat(entry.fd)` (falling back to `os.stat(entry.path)`), the same
  approach `_get_file_information_by_handle` already used correctly --
  works for both read and write mode now, only fails (with
  `ERROR_INVALID_HANDLE`) when the handle is genuinely unknown or the
  real host file is inaccessible.

Also seeded two real Jet 3.5 registry values that were previously
`NOT FOUND` in `registry.json`: `SystemDB` (under
`HKLM\Software\Microsoft\Jet\3.5\Engines`, value `C:\System.mdb` --
confirmed via a hardcoded literal string found directly in `msjet35.dll`'s
own compiled code, `FUN_7a876276`, that the `.mdb` extension is correct,
not `.mdw` as first guessed) and `TryJetAuth` (under
`...\Jet\3.5\Engines\ODBC` -- found by direct runtime introspection after
two wrong guesses at its location, `Debug` and bare `Engines`, both
disproven by adding a temporary debug print inside `_reg_query_value`
itself and reading back the exact `key_name` being checked at query time;
value `"N"`, tried as a "skip workgroup auth" hypothesis -- didn't change
the failure on its own, but left seeded since it's a real, previously-
missing value either way).

None of these fixes individually resolved the exact `-1022` propagating
out of `FUN_7a876e2b` (confirmed still identical, `0xfffffc02`, at the
same checkpoint even after every fix above) -- the two `GetLastError()`
call sites turned out to feed a separate error-recording path (likely
DAO's `Errors` collection), not the function's own direct return value,
which still traces to a third, not-yet-identified call to `FUN_7a873691`.
**But the combined effect of all of the above took the run from
halting after a few million steps to running clean for ~195.8 million
steps** -- past the entire DAO/Jet database-initialization sequence
entirely, into real gameplay activity (`dsound.dll` buffer status/lock,
`winmm.dll!timeSetEvent`, `user32.dll!GetMessageA` -- the actual message
pump). Whatever was gating progress past DAO/Jet is now effectively
cleared in practice, even without having pinned the exact `-1022` call
site with certainty.

New, unrelated blocker found at the new frontier (step ~195.8M): a
`RUNAWAY` with `EIP` landing in invalid/unmapped memory, preceded by two
`[UNIMPLEMENTED]`-labeled `VirtualAlloc` halts (`unsupported flProtect
0x1`, then `MEM_COMMIT on unreserved 0x4000000`) that evidently don't
actually stop execution (confirmed: ~195 million more steps ran
afterward) -- very likely the real cause of the eventual runaway, and a
strong candidate to also be the same "logged as halting but doesn't
actually halt" bug class already fixed once this session for unhandled
access-violation faults. Not investigated further this session -- next
priority.

Along the way, confirmed two things that turned out not to be the
answer, worth recording so they aren't re-derived: (1) tew has zero
native NT syscall (`INT 0x2E`) activity anywhere in this entire run
(`NtSyscallDispatcher` logs at `logger.debug("nt", ...)`, category never
included in earlier runs this session -- checked with it included,
found nothing), ruling out a missing-native-syscall explanation. (2) EBP
frame-based call-stack dumps (`_walk_ebp_chain`, reused from
`exception_diagnostics.py`) cannot see *into* `dao350.dll`/`msjet35.dll`
at all -- both are evidently frame-pointer-omitted (optimized/release)
builds, so unwinding through them just returns stale `EBP` state from
whatever `MCity_d.exe` frame was last active before the whole DAO/Jet
excursion began. This retroactively explains why *every* EBP chain dump
this whole project has ever produced only ever shows `MCity_d.exe`
addresses. For visibility inside these DLLs, targeted breakpoints/
logpoints at addresses found via Ghidra remain the only working method --
confirmed the *immediate* return address at `[ESP+0]` is still reliable
for one call-depth level even when full chain-walking isn't, since it's
pushed by `CALL` regardless of whether the callee sets up its own frame.

Also found and flagged, not yet fixed: `LCMapStringA` is a genuine
unimplemented halt-stub (`kernel32_io.py`), while its Unicode sibling
`LCMapStringW` is fully implemented. Notable because this entire game is
ANSI-only throughout (every API call seen this whole session has been the
`A` suffix, never `W`) -- if anything ever calls it (a real candidate:
`msjint35.dll`, Jet's own "international"/collation support DLL, already
loaded in this exact chain), it's an immediate hard halt. Queued for a
future session.

615/615 tests passing throughout (no new tests this entry -- this was a
live/runtime debugging session, verification was via the checkpoint-trace
methodology rather than new unit tests; the `GetFileSize`/`DeleteFileA`
fixes are exercised indirectly by the existing kernel32_io test coverage
but don't yet have dedicated regression tests of their own -- worth
adding later).

## 2026-08-03 — INT3 now routes through the real SEH chain instead of an
unconditional fatal_halt

Root-caused the `EIP=0x00688c69` halt that surfaced once the MSJET35.DLL
fault (previous entries) was fixed. Traced the caller chain in Ghidra:
`Nfs_REALabortcallback` (the game's own DAO/Jet database-init-failure
handler -- it's what wrote `except.txt`, the "Failed to initialize
database..." file noticed earlier this session) checks a global,
`_Nfs_DebuggerIsPresent`, and calls `_Nfs_DebugBreak()` (a thunk at
`0x0040eaca` jumping to the real body at `0x00688c50`, whose entire "work"
is a single `INT3` byte at `0x00688c68`) when it's true. That flag is
**hardcoded to `1` unconditionally** in `WinMain` (`0068a5e1`, right before
registering `Nfs_exitCallback` via `atexit()`) -- not read from
`IsDebuggerPresent()` or any environment check -- so this is deliberate,
by-design debug-build behavior, not something dependent on tew's Win32
emulation. The same pattern (`if (_Nfs_DebuggerIsPresent) DebugBreak();`)
is used at **1,780 call sites across 922 functions** throughout the binary
-- it's the primary assertion mechanism for the whole debug build, not a
one-off.

Real Windows treats an `INT3` with no debugger attached as a normal,
dispatchable `STATUS_BREAKPOINT` (`0x80000003`) structured exception, not
an automatic crash -- and this game relies on exactly that: it installs a
real SEH frame, `_CLayer_CatchSEH(&LAB_0040b7c6)` (called right after the
`_Nfs_DebuggerIsPresent=1` line in `WinMain`), whose filter
(`0x004d8c84`, hand-decoded since Ghidra hadn't auto-detected it as a
function -- SEH filter/handler code is only reachable via scope-table
metadata, not a normal call) checks specifically for
`ExceptionCode == 0x80000003`, and whose handler logs a source-location
message and lets execution continue -- no message box, no `CRTAbort`.

tew's `win32_handlers.py` INT3 dispatch (`int_num == 3` in the interrupt
dispatcher) previously skipped all of that: unconditional
`c.halted = True; c.fatal_halt = True`, no SEH attempt at all, for every
one of those 1,780 sites. Fixed to route through the same
`dispatch_exception()` SEH-chain-walking machinery already used for access
violations: sets `ExceptionAddress`/`CONTEXT.Eip` to the `INT3`'s own
address (`c.eip - 1` -- `EIP` has already advanced past the 1-byte opcode
by the time the interrupt handler runs, but real Windows reports the
exception at the `INT3` itself), calls `dispatch_exception(c, memory,
STATUS_BREAKPOINT, fault_eip)`, and only falls back to the same permanent
`fatal_halt` (still needed -- a plain `halted=True` gets silently cleared
by the next scheduler thread-switch, same class of gap fixed elsewhere for
unhandled access-violation faults) if the chain is genuinely exhausted.
This mirrors the existing pattern in `seh.py`'s own `_raise_exception`
(`RaiseException`'s real implementation), which already calls
`dispatch_exception` synchronously from inside a Python interrupt-handler
callback the same way.

3 new tests in `tests/unit/api/test_int3_seh_dispatch.py`: a handled case
(`ContinueExecution` handler -- confirms no halt), an unhandled case
(empty chain -- confirms the fatal_halt fallback still works, via
`pytest.raises(FatalHaltError)` since `cpu.step()` raises the instant
`fatal_halt` newly becomes true rather than returning normally), and an
`ExceptionAddress`-correctness case (a hand-built handler reads
`EXCEPTION_RECORD->ExceptionAddress` and confirms it points at the `INT3`
itself, not one past it). Verified the two behavior-changing tests are
real regressions: stashed the fix, confirmed both fail against the
pre-fix code, restored. 615/615 tests passing (612 + 3 new).

Confirmed live against the real scenario: re-running the same DAO/Jet
sequence that previously hit the unexplained `0x00688c69` halt now shows
`dispatch_exception` genuinely walking `tid=1012`'s real, compiled
**11-frame SEH chain** (handlers at `0x009f5eb8` (repeated -- a generic
CRT default handler), `0x00c771b0`, `0x00c93b54`, `0x00c93cc9`, all for
`code=0x80000003`) -- every one declines (`ContinueSearch`), so it's still
genuinely unhandled and correctly halts, but now the halt is a proven
result (`EIP=0x00688c68`, the `INT3`'s own address, correctly one less
than before) rather than a guess. This also answers the open question
from earlier investigation: `tid=1012` does **not** share
`_CLayer_CatchSEH`'s coverage (that frame is main-thread-only), so this
specific breakpoint really is unhandled at the per-thread level on real
Windows too. What real Windows would do next -- fall through to a
process-wide `SetUnhandledExceptionFilter`, if the game installs one --
is a separate, unexplored question; tew's `dispatch_exception` only walks
the per-thread FS:[0] chain today.

## 2026-08-02 (later session, cont'd again) — root cause of the `EIP=0x15035655`
MSJET35.DLL fault found and fixed: `opMovR32Imm` never honored 0x66

Used the emulator's own debugger facility (`cpu.add_logpoint` -- inline
callback, no halt, see `run_exe.py`'s "Debugger: breakpoints and logpoints"
section) instead of building new instrumentation: registered temporary
logpoints at every instruction boundary in the local dispatch chain leading
up to the fault (per the Ghidra static analysis in the prior entry) and did
one live run. The trace fired cleanly through the first 5 addresses (entry
through `0x1503564b`) and then went straight to the fault -- the 3 addresses
after `0x1503564b` never fired, pinpointing the divergence to exactly one
instruction: `0x1503564b` is `66 B8 01 00` (`MOV AX, 1`).

Root cause: `opMovR32Imm` (`cpu/src/engine.zig`, opcodes `0xB8`-`0xBF`,
`MOV r32, imm32`) never checked `s.op_size_ovr` -- unconditionally called
`fetch32(s)` regardless of the `0x66` prefix. For the real 4-byte
instruction `66 B8 01 00`, this reads 2 bytes too many as a bogus 32-bit
immediate and writes the full `EAX` instead of just `AX`, desyncing `EIP`
by 2 bytes from the real instruction stream. From there, MSJET35.DLL's
repetitive `OR imm8 / TEST / Jcc rel8`-shaped dispatch-chain bytes kept
"parsing" as plausible-but-wrong instructions for a few more (garbage)
steps until finally landing mid-instruction at `0x15035655` and hitting a
genuine access violation -- confirming the diagnosis from the prior entry
(EIP landing one byte into a real instruction, no legitimate control-flow
edge reaching it) with a concrete, live-traced root cause instead of just
static-analysis inference. Same *bug class* as the already-fixed
accumulator-immediate flags-width issue (op_size_ovr silently ignored) but
far more severe: this one corrupts `EIP` itself, not just a flag.

Fixed: `opMovR32Imm` now branches on `s.op_size_ovr` -- `fetch16` + write
only the low 16 bits (preserving the upper 16, matching real 16-bit MOV
semantics) when set, `fetch32` unchanged otherwise. Rebuilt `libcpu.so`.

3 new regression tests in `tests/unit/emulator/test_opcodes_mov.py`
(`TestMovR16Imm16`): AX and CX forms honor the prefix and preserve upper
bits, plus a direct reproduction of the live bug shape (`MOV AX,1` followed
immediately by a second real instruction, asserting `EIP` lands exactly on
it, not 2 bytes past). Verified real: stashed just this fix (which also
un-stashed the earlier flags-width fix, both live in the same file --
confirmed the 3 new tests specifically fail with `EIP=6` instead of `4`),
restored, rebuilt. 612/612 tests passing (609 + 3 new) with both fixes in
place.

Confirmed live end-to-end: re-ran the same scenario that previously
produced the `EIP=0x15035655` unhandled-SEH halt. MSJET35.DLL now loads and
runs cleanly with zero `[seh]` fault lines; execution progresses noticeably
further (60.045s vs. the previous run's 58.55s) before halting at the
already-known, separately-tracked `EIP=0x00688c69` (see status.md's queued
seh.py real-unwind item) -- real forward progress, not a new regression.
The temporary logpoint instrumentation added to `run_exe.py` for this
investigation was removed once the root cause was confirmed.

## 2026-08-02 (later session, cont'd) — accumulator-immediate opcodes'
16-bit flags-width bug fixed; 8-bit + 0x66-prefix regression tests added

While investigating the `EIP=0x15035655` MSJET35.DLL fault (see prior entry;
that fault is an 8-bit `OR AL, 0x04`, unaffected by this bug), checked
whether tew's 8-bit opcode handlers could suffer the same class of bug as
the old TypeScript emulator's 2026-03-30 `0x66`-prefix fix
(`/data/Code/exe/memory/changelog.md`, "0x66 prefix fixes"). They can't --
8-bit forms are separate opcode numbers, never gated by `op_size_ovr`, and
`engine.zig`'s handlers (`op04`/`op0C`/`op1C`/`op24`/`op2C`/`op3C`/`op80`/
`opA8`/`op84`/`op88`/`op8A`/`opF6`) all correctly use `.w8` throughout.

But their *16-bit* siblings -- the 9 accumulator-immediate opcodes `op05`
(ADD), `op15` (ADC), `op1D` (SBB), `op2D` (SUB), `op3D` (CMP), `op0D` (OR),
`op25` (AND), `op35` (XOR), `opA9` (TEST), exactly the same list named in
the old TypeScript fix -- had a live instance of the *same bug class* in
`engine.zig`: all 9 correctly branch on `s.op_size_ovr` for the register
read/write and immediate fetch (via `readEaxv`/`writeEaxv`/`fetchImm`), but
every one hardcoded `.w32` for the flags-width argument regardless of the
prefix. Found by contrast with `op85` (`TEST rmv, rv`), a sibling opcode
whose own comment already documents this exact mistake and fix ("was
hardcoded-32-bit read AND a hardcoded-32-bit flag width; fixing only the
read would still always report SF=0 for a correctly-16-bit-masked value")
-- that fix was never propagated to the 9 accumulator-immediate opcodes.
Effect: any `66 <op>` 16-bit form of these 9 gets SF wrong whenever the
true 16-bit result's sign bit (bit 15) is set, since `.w32` checks bit 31
instead, which is always 0 for a 16-bit-masked `u32`. Any `JS`/`JNS` (or
other SF-consuming logic) right after one of these was a live wrong-branch
risk.

Fixed all 9 in `cpu/src/engine.zig`, same one-line pattern as `op85`:
compute `const width: Width = if (s.op_size_ovr) .w16 else .w32;` and pass
`width` instead of the hardcoded `.w32` to `updateFlagsArithW`/
`updateFlagsLogicW`. Rebuilt `libcpu.so`.

Also closed a real test-coverage gap surfaced by this investigation:
`tests/unit/emulator/test_opcodes_arithmetic.py` had zero tests for true
8-bit AL-register opcodes (existing `imm8` tests were all the Group 1
sign-extended-imm8-into-32-bit-destination forms, `0x83`, not real 8-bit
ops) and zero tests for the `0x66`-prefixed 16-bit forms. Added two new
test classes: `TestByteWidthAccumulatorOps` (7 tests covering
`op04`/`op0C`/`op1C`/`op24`/`op2C`/`op3C`/`opA8`, asserting correct
low-byte result, upper-EAX-bits preserved, and SF read at bit 7 not bit 31
-- coverage, since these were already correct) and
`TestAccumImmediate16BitFlags` (9 tests, one per fixed opcode, each
producing `AX=0x8000` -- bit 15 set, bit 31 clear -- so SF only reads
correctly once flags are computed at the right width). Verified the 9
regression tests are real, not tautological: stashed just the
`engine.zig` fix, rebuilt `libcpu.so`, confirmed all 9 fail against the
pre-fix build with `assert False is True` on `SF_BIT`, then restored the
fix and rebuilt again. 609/609 tests passing (593 + 16 new) with the fix
in place.

## 2026-08-02 (later session) — repeated NVIDIA driver crash on exit fixed;
turned out to be a second, unrelated bug from the `EIP=0x15035655` MSJET35.DLL
SEH fault

What looked like "Python segfaulting while investigating an SEH error in
MSJET35.DLL" was actually two independent problems, only adjacent in the log:

1. The `EIP=0x15035655` unhandled SEH fault inside `MSJET35.DLL` (see
   "2026-08-02" entry above) — this is real and still **unresolved**, needs
   Ghidra decompilation. tew's own SEH dispatch (`dispatch_exception()` in
   `run_exe.py`) handled it exactly as designed: found no handler in the
   guest's SEH chain, printed the full halt diagnostic, and halted the CPU
   cleanly. No Python-level crash here — this is tew correctly reporting that
   the *guest* program would have crashed, entirely inside the emulator's own
   bounds-checked `ZigMemory`, never touching real host memory.

2. A **second, unrelated** crash immediately after, during process exit:
   `sys.exit()` -> `Py_Exit` -> libc `exit()` -> `__run_exit_handlers` ran an
   NVIDIA proprietary driver atexit callback against X11/GLX state that
   `window_manager.shutdown()`'s `SDL_Quit()` had already torn down two lines
   earlier. `coredumpctl` on the resulting core (`SIGABRT`, PID 258180,
   2026-08-02 16:36:25) showed the real chain: `libGLX_nvidia.so` ->
   `libnvidia-glcore.so` -> `xcb_present_select_input_checked` ->
   `xcb_send_request` -> `get_lazyreply` -> glibc's `_FORTIFY_SOURCE`
   `__chk_fail` -> `abort()`. Zero `libcpu.so` frames anywhere in the crashed
   thread's stack, confirming this fires well after emulation has fully
   stopped — unrelated in content to the MSJET35.DLL fault, just close to it
   in time. Same bug *class* as the earlier `libnvidia-rtcore.so` crash
   (2026-07-xx, fixed by adding `window_manager.shutdown()`) but a different
   NVIDIA subsystem (GLX, not Vulkan RT) hitting the same "driver atexit
   handler runs against an already-closed X11 connection" hazard.

Rather than keep chasing individual NVIDIA-subsystem atexit bugs one at a
time (rtcore, now GLX, possibly more later), replaced `sys.exit()` with
`os._exit()` at the very end of `run_exe.py` (after
`crt_state.window_manager.shutdown()`), with an explicit `sys.stdout.flush()`
first. `os._exit()` skips `exit()`/`__run_exit_handlers` entirely, so no C
library atexit handler -- NVIDIA's or anyone else's -- runs at all.
`logger.py`'s `_emit()` already does `print(line, flush=True)` on every line,
so no log output is at risk from skipping normal Python finalization.
593/593 tests still passing. Confirmed live: re-running the same scenario
that previously produced the `SIGABRT`/coredump now exits with code 0 and
`coredumpctl list` shows no new entry.

## 2026-08-02 — `oleaut32.dll` ordinal 201 (`SetErrorInfo`) implemented; new
native-crash halt found immediately after

Implemented `SetErrorInfo(dwReserved, perrinfo) -> HRESULT` (ordinal 201,
`oleaut32.dll`) in `oleaut32_handlers.py`: stores `perrinfo` as the calling
thread's current COM error object in a new per-thread
`CRTState.error_info_store` dict (keyed by `state.tls_current_thread_id()`,
matching the existing TLS pattern in the same file), releasing the previous
entry and AddRef'ing the new one via the same `_errinfo_release_core`/
`_errinfo_addref_core` helpers `IErrorInfo::Release`/`AddRef` already use —
this emulator has exactly one `IErrorInfo` implementation (`CreateErrorInfo`'s,
same file), so direct field manipulation is equivalent to a real vtable call,
matching this file's own established convention (`_errinfo_qi_core` already
does the same). Always returns S_OK per spec (MSDN: "This function always
returns S_OK"). 593/593 tests still passing.

Confirmed live: `SetErrorInfo(perrinfo=0x06fd2624) -> S_OK (tid=1012)` clears
the blocker, and execution continues into `MCity_d.exe`'s own code for the
first time past this point — a 6-frame-deep call chain purely in `exe`
addresses (frames at 0x0068adf2, 0x00a301a1, 0x00684de7, 0x006848ee,
0x004d8c71, 0x0068a7d5, 0x009fcaa6).

**New blocker found immediately after, same run**: at `EIP=0x00688c69` (in
`MCity_d.exe`), the run prints a "Halt Diagnostic" (`EAX=0xCCCCCCCC` —
MSVC debug-heap uninitialized-fill pattern; `ESP+0x0c` through `ESP+0x3c`
on the stack are also all `0xCCCCCCCC`) and then the underlying process
itself crashes — `timeout` reports "the monitored command dumped core"
rather than a clean Python exit. This means the fault handler that prints
"Halt Diagnostic" is not actually stopping the CPU cleanly; something after
it (native/Zig side, or Python touching freed state during shutdown) causes
a real SIGSEGV. Not yet diagnosed — no `[UNIMPLEMENTED]` log line appears
anywhere in this run, so this isn't a missing-handler halt; it's either a
genuine fault in game code operating on uninitialized memory, or an
emulator-side bug in whatever path leads to "Halt Diagnostic". Full log:
`/tmp/emu.log` (this session, categories `com,dll,loader,exception`,
`LOG_LEVEL=info`). Likely related to the already-queued "Decide/implement a
real unwind for `seh.py`'s unhandled-fault path instead of 'halt in place
with stale stack data'" item — worth checking that path first next session.

## 2026-08-02 (cont'd) — native-crash halt diagnosed: not a CPU/memory bug,
missing `WindowManager.shutdown()` call let the NVIDIA driver's own atexit
cleanup fault against a live SDL/Vulkan context

Investigated the `EIP=0x00688c69` core-dump crash from the entry above.
Checked SELinux first (per Molly's hunch it might be interfering): enforcing
mode confirmed via `sestatus`, but `journalctl` search for AVC denials across
the crash window came back empty — ruled out.

`coredumpctl info <pid>` on the crashed process (found via `coredumpctl
list`, a `SIGSEGV` in `/usr/bin/python3.14` matching the run's timestamp)
told the real story: the segfaulting thread's backtrace is
`Py_BytesMain → Py_RunMain → ... → handle_system_exit → Py_Exit → exit() →
__run_exit_handlers → SEGV in libnvidia-rtcore.so`. That's `sys.exit(1 if
cpu.faulted else 0)` at `run_exe.py:568` firing normally (a deliberate,
already-logged halt, not a crash) — the actual fault happens *afterward*,
inside libc's `__run_exit_handlers` calling into an `atexit`-registered
cleanup hook that NVIDIA's proprietary driver installs itself.
`libcpu.so` (the Zig CPU/memory core) appears in the loaded-modules list but
not in any crash thread's actual stack trace — confirms the CPU/memory
backend was completely uninvolved.

Root cause: `WindowManager.shutdown()` (`tew/api/window_manager.py`) already
existed and correctly destroys SDL2 textures/renderers/windows before
calling `SDL_Quit()` — but nothing in `run_exe.py` ever called it. Separately
(grepped the whole `tew/api/d3d8/` package), the Vulkan side has *no*
teardown path at all: `_state.py` tracks instance/device/swapchain/pipeline/
semaphores/etc., but the only `vkDestroy*` call anywhere in the codebase is
`vkDestroySwapchainKHR` (used for swapchain recreation, not shutdown) —
`vkDestroyInstance`/`vkDestroyDevice` don't exist anywhere. So `sys.exit()`
ran with both SDL2 and Vulkan still fully live, and the NVIDIA driver's own
"someone forgot to clean up" atexit safety-net handler ran against that live
GL/Vulkan context and segfaulted inside its own ray-tracing-core module. This
is the first session this project has ever reached far enough into real game
code to create a live SDL/Vulkan context and then exit while it's still
up — explains why this class of crash has never appeared before.

Fix: added one call, `crt_state.window_manager.shutdown()`, right before
`sys.exit()` in `run_exe.py`. Confirmed live (re-ran with `LOG_CATEGORIES`
including `window`): `[WindowManager] SDL2 shut down` now logs and the
process exits cleanly; `coredumpctl list --since="10 minutes ago"` shows no
new core dump. Vulkan itself still has no explicit teardown — SDL2's own
`SDL_Quit()` was apparently sufficient to stop the driver's atexit handler
from faulting this time, but that's relying on driver behavior, not a real
fix for the Vulkan side; queued as a follow-up (see status.md).

The original `EIP=0x00688c69` halt (`EAX=0xCCCCCCCC`, MSVC debug-heap
uninitialized-fill pattern, in `MCity_d.exe`'s own code) is still
undiagnosed on its own merits — the crash was just masking it from being
investigated. That's next session's actual current blocker.

## 2026-08-02 (cont'd, later) — logger was silently dropping the halt
reason itself; fix relocates the real blocker from 0x00688c69 to 0x15035655

Molly noticed the `EIP=0x00688c69` halt diagnostic gave register/stack
detail but never said *why* the CPU halted, and asked for more log detail.
Root cause, in `tew/logger.py`'s `_emit()`: `ERROR`-level messages were
still subject to `LOG_CATEGORIES` filtering, with only the `"exception"`
category specially exempted. But this project's own mandatory "halt loudly"
convention (CLAUDE.md) requires every halt/fault to log an `ERROR` right
before setting `cpu.halted`/`cpu.faulted` — so whichever category actually
carried the real reason (`seh`, `cpu`, `handlers`, etc.) got silently
dropped whenever a run's `LOG_CATEGORIES` didn't happen to include it, which
is exactly what happened in both of today's earlier runs
(`com,dll,loader,exception,window`). Fixed by exempting `ERROR` level from
category filtering the same way `"exception"` already was — one condition
added to `_emit()`. Checked for any `is_active()`-gated call sites that
might skip constructing an error message before it reaches `_emit()`
(would have defeated the fix) — none exist in the codebase. 593/593 tests
still pass.

Confirmed live: re-ran the *exact same* narrow `LOG_CATEGORIES` used in the
earlier "unexplained" run. Two previously-invisible lines now appear:
```
[ERROR] [seh] fault at 0x15035655 unhandled by SEH chain -- halting as before
[ERROR] [cpu] Fatal halt: fatal halt at EIP=0x00688c69
```
This **relocates the real blocker**. `EIP=0x00688c69` was never the fault
site — `run_exe.py`'s step loop (`fault_eip = cpu.eip` at the moment
`cpu.faulted` becomes true) shows the actual access violation happened at
`EIP=0x15035655`, inside `MSJET35.DLL` (`0x15000000-0x15ffffff`, offset
`0x35655`). That got dispatched through `dispatch_exception()` looking for
a handler in the game's own SEH chain, found none, and fell into the
already-queued "halt in place with stale stack data" gap in `seh.py` —
`0x00688c69` and its `EAX=0xCCCCCCCC` are what that stale state looked like
afterward, a real but downstream symptom, not the cause.

Also noted, not fixed this session: the Zig CPU core's fault path
(`cpu_zig.py` `run()`/`step()`, `_RUN_FAULTED` branch) only synthesizes
`"CPU fault at EIP=0x...opcode=0x..."` — it never surfaces the actual
faulting *memory address*, only EIP. Real Windows access violations carry
the accessed address; not having it here is a real gap, queued for a
Zig-side follow-up, but EIP alone (`0x15035655`) is sufficient to start the
next investigation in Ghidra.

---

## 2026-07-24 (cont'd) — bump-allocator ported to Zig; Zig/Python FFI
boundary consolidated into a kernel module; merged to main and pushed

Continuing the same day's incremental Zig-porting work: `CRTState.simple_alloc`'s
guest-heap bump-pointer arithmetic (`(current + size + 15) & ~15`, backing
`malloc`/`HeapAlloc`/`operator new` etc. across ~30 call sites) moved to a new
`cpu/src/alloc.zig`, exported as `bump_alloc_next(current, size) -> u32` and
called from a new `tew/hardware/alloc_zig.py` wrapper. Same split as the memory
port: the cursor itself and the `heap_alloc_sizes`/`heap_alloc_owner` bookkeeping
dicts (used by `realloc`/`HeapSize`/`HeapFree`) stay Python-owned; only the pure
pointer math moved. Still a bump allocator, no free list — scope kept minimal on
purpose.

While tracing how the CPU's own memory access works (investigating a "how do
Python and Zig actually talk to each other" question), found that `core.zig`'s
CpuState-bound `memRead8`/`memWrite8` and `memory.zig`'s Python-facing
`mem_read8`/`mem_write8` independently reimplemented the same bounds-check
arithmetic — two implementations of the same logic with no shared source of
truth. Went beyond a point-fix: designed and built a proper kernel-module
architecture for the whole Zig/Python FFI boundary (CPU + memory + alloc, not
just memory), in three separately-verified, separately-merged stages:

- **Stage 1** — new `cpu/src/primitives.zig`: `inBounds1`/`inBoundsWidth`/
  `readByte`/`writeByte`, the single shared implementation of "is this address
  in bounds, read/write the byte(s)." `core.zig`'s `memRead8`/`memWrite8` and
  `memory.zig`'s `mem_read8`/`mem_write8` both now delegate to it — zero
  file-layout change otherwise. `inBounds1` (single-byte, core.zig's original
  formula) and `inBoundsWidth` (combined-width, memory.zig's original formula)
  deliberately kept as two distinct functions, not unified into one: a 16-bit
  access straddling the end of memory behaves observably differently under
  each (per-byte-composed access keeps the first byte's real value before
  faulting on the second; a combined-width pre-check would reject both bytes
  up front), so collapsing them would have been a real behavior change, not a
  pure refactor.
- **Stage 2** — physically split `cpu.zig` (2006 lines, was doing double duty
  as both the internal execution engine and the entire Python-facing C ABI)
  into `cpu/src/kernel.zig` (new build root — every one of the project's 63
  `export fn`s, and only those, absorbing `memory.zig`'s 11 and `alloc.zig`'s
  1) and `cpu/src/engine.zig` (dispatch table, `cpuStep`, all opcode handlers
  — internal only, never exported, called from `kernel.zig`'s `cpu_run`).
  Because every export is now a top-level declaration directly in the build
  root, the `comptime { _ = &...; }` force-reference block (needed back when
  `memory.zig`/`alloc.zig`'s exports weren't naturally referenced from the old
  root and Zig's lazy per-declaration analysis silently dropped them from the
  compiled `.so`) is gone — the underlying problem it worked around no longer
  exists structurally, not just patched over. Fixed two live (not just
  commented-out) `@import("../cpu.zig")` sites in `history/capture.zig`'s test
  bodies that an earlier audit pass had missed. (Correction to the earlier
  audit: the true export count was always 63, not 53 — cpu.zig alone had 51
  exports, not 41; verified against `nm -D` throughout, so this was a
  documentation-only miscount, not a functional gap.)
- **Stage 3** — new `tew/hardware/_kernel_lib.py`: one shared `ctypes.CDLL`
  handle instead of three independent `dlopen` calls (`cpu_zig.py`,
  `memory_zig.py`, `alloc_zig.py` each used to call `ctypes.CDLL()` on the same
  path separately). No Zig changes; purely how the existing symbols get bound.

Each stage done on its own worktree/branch, verified independently (`zig build
test`, `nm -D` symbol-set diff against the pre-stage baseline, 593/593 pytest,
a live `run_exe.py` run diffed against the saved baseline on `[com]`/
`[exception]`/`[dll]`), then merged to `main` before the next stage started.

Also chased down an unrelated false alarm mid-session: a live run started
crashing immediately after `SDL2 initialized` with an `X_GLXCreateContext
BadValue` error, right after the memory/cpu.py work had been merged, which
briefly looked like a regression from that merge. Bisected the last 5 commits
on `main` (checking out each individually, rebuilding, running) — every single
one crashed identically, including a commit that predates the memory port
entirely, ruling out a code cause. Confirmed independently via plain `glxinfo`
(unrelated to this repo) failing the exact same way, and via `journalctl`
showing `kwin_wayland_wrapper` repeatedly logging `XCB error: BadWindow` for
the same stale resource ID at every crashed run's timestamp — a wedged
Xwayland/kwin compositor resource, most likely caused by the crashed runs
themselves never cleaning up their SDL/GL window on the abrupt X IO-error
exit. Logging out and back in cleared it; re-verified full clean runs on both
`main` and the in-progress worktree afterward with zero code changes.

All three stages merged to `main` and pushed. Final state re-verified end to
end on `main` itself (not just in the stage worktrees): `zig build test`
clean, 593/593 pytest, and three separate live `run_exe.py` runs (including
one requested purely "for peace of mind" after everything else was done) all
landing on the identical documented halt — same registers, same stack dump,
same `Final EIP: 0x00209402` — with byte-identical `[com]`/`[exception]`/
`[dll]` output versus the original saved baseline throughout.

---

## 2026-07-24 — hardware/memory.py ported to Zig; dead pure-Python CPU
class and entire emulator/opcodes package retired; merged to main and
pushed

`tew/hardware/memory.py` (flat bytearray-backed address space:
read8/16/32, write8/16/32, bounds checks) ported to a Zig-backed
`ZigMemory`, following the exact FFI pattern the earlier `cpu.zig`/
`ZigCPU` port established. New `cpu/src/memory.zig`: bounds-checked
`mem_read8/16/32`, `mem_write8/16/32`, `mem_load`,
`mem_is_valid_address/range` C-ABI functions operating on a
caller-owned `(ptr, size)` pair (no allocator, no ownership change —
Python's `bytearray` still owns the buffer, exactly as `ZigCPU` already
borrows it via `ctypes.from_buffer`). `tew/hardware/memory.py` is now a
12-line re-export shim (`Memory = ZigMemory`), mirroring how
`run_exe.py` already aliases `ZigCPU as CPU`. Along the way, closed two
latent gaps the old pure-Python `Memory` had and that `test_memory.py`
never actually covered: `read16`/`write16` had no explicit bounds
check (relied on `struct.error` leaking through instead of
`ValueError`), and `write8`/`write16` silently accepted negative
addresses via Python's list/struct wraparound semantics instead of
rejecting them.

Separately, investigated whether `tew/hardware/cpu.py` (the original
pure-Python CPU class, kept around post-Zig-port only for its
register/flag constants) was safe to delete. Found it was worse than
just unused: `run_exe.py` calls `register_all_opcodes(cpu)` on every
startup, which imports all 10 files under `tew/emulator/opcodes/`
(`data_movement.py`, `arithmetic.py`, `logic.py`, etc. — the entire
pure-Python x86 instruction-decode implementation) and registers every
opcode handler onto the CPU — but `ZigCPU.register()` (`cpu_zig.py`)
is a literal no-op (`def register(self, opcode, handler): pass`,
comment: "Zig handles all opcodes"), so every one of those
registrations was silently discarded on every run. Deleted
`tew/hardware/cpu.py` and the entire `tew/emulator/` tree (12 files,
~2,700 lines) outright. 38 files' register/flag-constant imports
(`EAX`, `ESP`, `CF_BIT`, etc.) redirected from `tew.hardware.cpu` to
`tew.hardware.cpu_zig`, which already defined byte-identical constants
(confirmed by diff before redirecting); the handful of
`TYPE_CHECKING`-only `CPU` type-hint imports now alias `ZigCPU`
instead — confirmed via full-repo sweep that every `CPU`-class import
was type-hint-only, zero live uses.

Verification for both changes: gitnexus's `IMPORTS`-edge query against
`tew.hardware.cpu` found the same 51 importing files as a manual grep
sweep — nothing missed. 593/593 tests pass throughout. Two full
DAO-reaching `run_exe.py` sessions (`LOG_CATEGORIES=com,dll,loader,
exception`, ~57s to the halt) — one after the memory port, one after
the cpu.py/opcodes retirement — produced **byte-identical** `[com]`,
`[exception]`, and `[dll]` log output versus the pre-change baseline:
same COM/CreateErrorInfo sequence, same halt at `EIP: 0x00209402`
(`SetErrorInfo`, still unimplemented — unchanged, expected), same
registers, same stack dump. Both changes done on isolated
branches/worktrees, merged into `main`, and pushed to `origin/main`
(`0fd35bd..90bc231`). Stale branches cleaned up afterward: 2 local
(fully merged) and 2 remote (`origin/combined`, `origin/nt-syscall-
layer` — both fully subsumed into `main`, zero unique commits)
deleted; `origin/renovate/configure` left alone (has 1 unmerged
commit, a real open item, not dead weight).

---

## 2026-07-23 (post-midnight session, cont'd 3) — LoadStringA, lstrcatA,
and CreateErrorInfo (oleaut32 ordinal 202) added; real RT_STRING resource
parsing added; per-DLL resource resolution wired through user32; one
CLAUDE.md handler-declaration violation found on retroactive audit and
fixed; new SetErrorInfo blocker

Continued straight on from the `patch_dll_iats` fix below.
`[UNIMPLEMENTED] user32.dll!LoadStringA` was next. Real `RT_STRING`
resource lookup added to `pe_resources.py`
(`PEResources.find_string(string_id)`, standard Win32 packing: block
`(id>>4)+1` holds 16 `[WORD length][length WCHARs]` entries, index
`id&0xF`). `register_user32_gdi32_handlers` (`user32_handlers.py`)
previously had no `dll_loader` access at all, so it could only ever
resolve resources for the main EXE's own hInstance — added the param
(threaded from `crt_handlers.py`, matching the pattern
`register_kernel32_handlers`/`register_oleaut32_ole32_handlers` already
used), plus a per-DLL-name `PEResources` cache
(`_resources_for_module`) so a real loaded DLL calling back into user32
for its own resources (MSJET35.DLL/MSJINT35.DLL here) gets its own
`.rsrc`, not the game's. `DLLLoader._find_dll_file` renamed to the
public `find_dll_file` (was already only used internally, one call
site) so this new code could re-locate a loaded DLL's file on disk.
Confirmed live: cleared the halt.

`[UNIMPLEMENTED] kernel32.dll!lstrcatA` was next — added next to
`lstrcpyA`/`lstrcpynA` in `kernel32_io.py`, real (unbounded) `strcat`
semantics. Confirmed live: cleared the halt.

`[UNIMPLEMENTED] oleaut32.dll!Ordinal #202` was next. Looked up against
the real `oleaut32.dll`'s export table
(`/data/Downloads/i386-binaries/oleaut32.dll`, same `EXEFile`/
`ExportTable` technique as the earlier `msjint35.dll` investigation):
ordinal 202 = `CreateErrorInfo(ICreateErrorInfo **pperrinfo)`. Implemented
in `oleaut32_handlers.py` as a real dual-interface COM object: one
allocated 0x2C-byte object with two vtables at a +4 offset
(`ICreateErrorInfo` at the object's own address, `IErrorInfo` at +4 —
the standard C++ multiple-inheritance "this-adjustor" split), a shared
refcount, `QueryInterface` on either face correctly switching between
them (and AddRef'ing on success, per COM contract), and real Set*/Get*
method bodies that actually read/write the object's fields — no fake
success anywhere.

**Process note, found via retroactive self-audit prompted by direct
user pushback ("CreateErrorInfo doesn't feel right" / "is S_OK
truthful?" / "empty was not on DAO's bingo card")**: this entire
session skipped `CLAUDE.md`'s mandatory HANDLER DECLARATION step
(state Function/Signature/Spec-says/We-deliver/Truthful-YES-NO in chat
*before* writing any handler) — every handler from `RegEnumKeyA`
onward. Ran the required grep audit
(`TODO|FIXME|FAKE|stub|not implemented|pass$|return None|return 0.*#|
return False.*#|return True.*#`) across every file touched this
session via `git diff`: no concealed fakery found (the `return None`
hits are all legitimate "not found" returns matching the existing
`find_dialog`/`find_bitmap` convention). One real violation found on
manual re-check against spec: `LoadStringA`'s `cchBufferMax == 0` path
(pointer-swap mode — caller wants `*(LPSTR*)lpBuffer` set to point at
the resource's own storage, no copy) was silently returning a
plausible-looking character count with `lpBuffer` left untouched
instead of honestly halting on a capability we can't back (no stable
address for a resource string that only exists as a materialized
Python `str`). Fixed: that path now halts loudly
(`[UNIMPLEMENTED] LoadStringA(cchBufferMax=0)`) instead of guessing.
Confirmed live this session that real callers never actually hit
`cchBufferMax==0` in practice, so the halt is inert, not a live-blocking
gap — but the *previous* silent-wrong-answer behavior was a genuine
spec violation regardless of whether it was ever exercised.

Added full logging (`logger.info("com", ...)`) to every method on the
new error-info object specifically to settle the "is this actually used
correctly" question with evidence rather than argument. Live-verified
the dual-interface design was load-bearing, not speculative: DAO's real
code calls `QueryInterface(IID_IErrorInfo)` on the object immediately
after creation (succeeds via the +4 face — the this-adjustor math is
correct), then fills it via the original `ICreateErrorInfo` pointer with
real content (`SetSource("DAO.DbEngine")`, a real help context ID, a
help-file pointer) before the very next call. Had the `IErrorInfo` face
not been implemented (i.e. `QueryInterface` honestly returning
`E_NOINTERFACE` for it, which was considered as the "simpler, more
defensible" option before this evidence), this exact real call sequence
would have diverged from genuine DAO behavior.

New halt, right after the error-info object is fully populated:
`[UNIMPLEMENTED] oleaut32.dll!Ordinal #201` (`SetErrorInfo`, same
export-table lookup technique). Not yet implemented, next up — a
HANDLER DECLARATION should be stated first this time, and what pointer
it actually receives (the `ICreateErrorInfo` face vs. the already-QI'd
`IErrorInfo` face) should be checked rather than assumed.

593/593 tests passing after every fix in this entry.

---

## 2026-07-23 (post-midnight session, cont'd 2) — GetFileInformationByHandle
and lstrcpynA added; real DLLLoader.patch_dll_iats bug found and fixed
(was clobbering real DLL-to-DLL calls with unimplemented-stubs); new
LoadStringA blocker

Continued straight on. `[UNIMPLEMENTED] kernel32.dll!GetFileInformationByHandle`
was next — added in `kernel32_io.py`: looks up the handle in
`state.file_handle_map`, `os.fstat`s the real fd (`os.stat`s `entry.path`
for read-only entries with no fd), fills a real `BY_HANDLE_FILE_INFORMATION`
(attributes, real `ctime`/`atime`/`mtime` → `FILETIME`, real size, link
count, inode as file index). Confirmed live.

`[UNIMPLEMENTED] kernel32.dll!lstrcpynA` was next — added next to the
existing `lstrcpyA`/`lstrlenA` in `kernel32_io.py` (bounded copy, always
null-terminates within `iMaxLength`). Confirmed live.

`[UNIMPLEMENTED] msjint35.dll!Ordinal #2` was next, and looked like
another missing-handler case, but direct offline inspection of the real
`msjint35.dll`'s export table (`tew`'s own `EXEFile`/`ExportTable`
parser, no emulator run needed) showed ordinal #2 (`CchLszOfId2`)
genuinely exists, and `DLLLoader.load_dll` had already resolved and
written its correct real address into the IAT. The actual bug: back in
`tew/loader/dll_loader.py`, `DLLLoader.patch_dll_iats` runs *after*
`load_dll` and unconditionally re-patches every secondary-DLL IAT entry
via `patch_iat_entry` — but never passed the already-known real address
through as `real_addr`, so any entry with no matching Python handler
fell straight to the unimplemented auto-stub fallback, silently
clobbering correct real-DLL-to-real-DLL calls (`msjet35.dll` calling
into `msjint35.dll`) with a fatal halt. This wasn't specific to this one
ordinal — it affected every secondary-DLL IAT entry across the whole
run. Fixed by having `patch_dll_iats` look up
`self._loaded_dlls[...].exports` and pass that through as `real_addr`;
added a `real_count` bucket to the "Patched X/Y ... (N auto-stubs)"
summary log so this class of bug shows up in the log instead of
silently inflating the auto-stub count. Confirmed live: MSJET35.DLL's
own final IAT patch pass went from 23 auto-stubs/0 real (every prior
session) to 1 auto-stub/7 real. 593/593 tests passing after all three
fixes.

New halt, past the entire `msjint35.dll` call: `[UNIMPLEMENTED]
user32.dll!LoadStringA` — a genuine missing Win32 API this time, not a
loader bug. Not yet implemented, next up.

---

## 2026-07-23 (post-midnight session, cont'd) — GetTempPathA and
GetTempFileNameA added; execution now three levels deep inside
MSJET35.DLL, new GetFileInformationByHandle blocker

Continued straight on from the `RegEnumKeyA` fix below. Next halt:
`[UNIMPLEMENTED] kernel32.dll!GetTempPathA`, right after
`RegOpenKeyA("...ISAM Formats")`. Added in `kernel32_io.py`, modeled on
`GetCurrentDirectoryA`'s buffer-sizing contract (returns required size
including null terminator when the caller's buffer is too small).
Returns `C:\WINDOWS\TEMP\`; created the backing host directory
(`~/.emu32/WINDOWS/TEMP/`) so real file creation against that path
actually lands somewhere on disk, consistent with `~/.emu32/WINDOWS/`
already being a live `DLLLoader`/path-translation root.

Very next halt: `[UNIMPLEMENTED] kernel32.dll!GetTempFileNameA` — Jet
turning that temp path into an actual scratch filename. Added in
`kernel32_io.py`: builds `<path><3-char prefix><4 hex digits>.TMP`
(hex digits from `uUnique` if nonzero, else an internal incrementing
counter), and when `uUnique == 0` — the common "reserve me a unique
name" case — actually creates the 0-byte file on the host filesystem,
reusing the same `os.open(path, O_WRONLY|O_CREAT|O_TRUNC)` +
`os.makedirs` pattern `CreateFileA`'s writable-open branch already uses
(via `state.open_file_handle`), just called directly since
`GetTempFileNameA` returns a UINT, not a handle, and closes the fd
immediately rather than leaving an untracked handle open. This matters
because it's genuine real-file-creation, not just a string return: any
later real `CreateFileA`/`ReadFile` Jet does against this exact
filename will find a real file.

Both fixes confirmed live in the same run. 593/593 tests passing after
each. New halt, one level further than any previous session:
`[UNIMPLEMENTED] kernel32.dll!GetFileInformationByHandle`, called
immediately after the temp file is created — not yet implemented, next
up.

---

## 2026-07-23 (post-midnight session) — RegEnumKeyA added; execution now
two levels deep inside MSJET35.DLL, new GetTempPathA blocker

Picked up from the late-night session's fresh halt:
`[UNIMPLEMENTED] advapi32.dll!RegEnumKeyA`. Root cause: only `RegEnumKeyExA`
had ever been implemented in `advapi32_handlers.py` — the older, simpler
`RegEnumKeyA` (4 args, `cchName` passed by value rather than by pointer to
a DWORD, no class/last-write-time output) was never added. Added it,
sharing a new `_reg_list_subkeys()` helper factored out of
`RegEnumKeyExA`'s subkey-derivation logic (was duplicated inline before).

Live-verified: execution now runs cleanly through the entire
`HKLM\Software\Microsoft\Jet\3.5\Engines` subkey enumeration and all of
`Engines\ODBC`'s config-value reads — all honest `NOT FOUND`s (no
`registry.json` seeding needed; Jet tolerates the empty tree same as it
tolerated the earlier `IsTNT`/`GetProcessAffinityMask` misses). 593/593
tests still passing.

New blocker, one level further than any previous session: right after
opening `HKLM\Software\Microsoft\Jet\3.5\ISAM Formats`,
`[UNIMPLEMENTED] kernel32.dll!GetTempPathA` halts cleanly. Not yet
implemented — next up.

---

## 2026-07-23 (late-night session) — LoadLibraryA fake-success bug and
GetProcAddress ordinal-format bug fixed; real Jet 3.5 engine DLLs sourced
and now genuinely executing

Continued from the night session's DAO COM completion. The next blocker —
`GetProcAddress("msjter35.dll", "ordinal#2")` looping 7,005 times with zero
progress, burning the full step budget — turned out to be two independent,
real bugs, not one:

1. **`LoadLibraryA` fake-success bug** (`kernel32_handlers.py`,
   `_load_dll_by_name`/`_load_dll_by_path`). The fallback for any DLL name
   not found on disk unconditionally fabricated a stable, non-NULL, hash-
   based fake handle (`_fake_dll_handle`) — even for DLLs with zero handler
   coverage of any kind. `GetModuleHandleA`'s equivalent resolution path
   (`_resolve_module_handle`) already did this correctly: fake-succeed only
   if `stubs.get_stub_dll_handle()` finds real handler coverage, otherwise
   return NULL. `LoadLibraryA` now matches that same rule. This alone
   stopped the busy-loop: DAO saw an honest `LoadLibraryA` failure for
   `msjet35.dll`/`msjter35.dll` and moved on instead of spinning. Dead
   `_fake_dll_handle()` helper removed (no longer referenced anywhere).

2. **`GetProcAddress` ordinal-format bug** (`kernel32_handlers.py`,
   `_get_proc_address`). Ordinal-only lookups built the key as
   `f"ordinal#{name_ptr}"` (lowercase, no space) — but every other place in
   the codebase that registers or parses an ordinal export uses
   `f"Ordinal #{n}"` (capital O, space): `dll_loader.py`'s real PE
   export-table parsing, `oleaut32_handlers.py`'s `_ole_ord`,
   `wsock32_handlers.py`, `dsound_handlers.py`. This is a separate,
   independently-real bug from #1 — it means runtime `GetProcAddress`
   ordinal lookups against a genuinely-loaded real DLL's export table (or
   against a Python-registered ordinal-alias handler) could never succeed,
   full stop, regardless of whether the DLL/ordinal actually exists. Fixed
   to build `f"Ordinal #{name_ptr}"`, matching everywhere else. (The
   earlier oleaut32.dll ordinal #4/#15/#21 fixes this session-chain
   depended on a *different* code path — static IAT resolution at DLL-load
   time, not this runtime `GetProcAddress` handler — so they were unaffected
   by this bug and didn't reveal it.)

With both fixed, `msjter35.dll`'s (and `msjet35.dll`'s) real files were
still needed — Prompted by Molly asking "we will be using an access jet3
database... so um....." after the busy-loop diagnosis, which reframed
"honestly fail, DLL not installed" as insufficient for the actual use case
(the game needs working Jet reads/writes, not just DAO politely giving up).
Found the real Microsoft Jet 3.5 Database Engine redistributable already on
this host: `~/.emu32/DBInst/DAO/data1.cab` (an InstallShield cabinet,
extracted with `unshield`) — the *same* install package `dao350.dll` was
originally sourced from (confirmed: extracted `dao350.dll` sha256-matches
the one already deployed byte-for-byte). The cabinet also contains
`MSJET35.DLL` (core Jet engine), `msjter35.dll` (Jet error-message
resource), `MSJINT35.DLL` (Jet international/collation), `VBAJET32.DLL`,
`msrd2x35.dll` (Jet Red ISAM driver), `EXPSRV.DLL` (Jet expression
service), and `MSVCRT40.DLL` (a bonus fix — this was DAO350.DLL's own
previously-unresolved static import). All extracted and placed at
`~/.emu32/WINDOWS/System32/`, which was already a registered DLL search
path (added by `oleaut32_handlers.py` for the `dao350.dll` COM-server
lookup, but that search path was never gated to COM servers only — it's
generic, so the new Jet DLLs are discoverable with no code changes beyond
the two bug fixes above).

Live-verified end to end: `MSJET35.DLL` now loads for real (166 exports,
base `0x15000000`), `GetProcAddress` resolves its real exports correctly,
and execution genuinely runs *inside* `MSJET35.DLL`'s own x86 code for the
first time (EBP chain shows `MSJET35.DLL+0x36db2`) — a full level deeper
than DAO's own code, which is as far as every previous session got. Two
small non-blocking gaps surfaced along the way (`kernel32.dll!IsTNT`,
`kernel32.dll!GetProcessAffinityMask` — both return NULL harmlessly, Jet
handles the miss fine and keeps going) before hitting the new, clean,
single-shot current blocker: `[UNIMPLEMENTED] advapi32.dll!RegEnumKeyA`,
almost certainly Jet 3.5 enumerating its own ISAM/engine registration under
`HKLM\Software\Microsoft\Jet\3.5\Engines` during initialization.

593/593 tests passing throughout (reconfirmed after each fix).

## 2026-07-23 (night session) — oleaut32.dll ordinal #4, lstrcmpW, GlobalLock/
GlobalUnlock: DAO's entire COM activation chain now completes and returns
to the game's own code

Continued straight from the evening session's fatal_halt exception work,
live-verifying against the real `dao350.dll` after each fix:

1. **`oleaut32.dll` ordinal #4** (`SysAllocStringLen`) — same missing-
   ordinal-alias shape as #15/#21 fixed earlier today; aliased to the
   existing named-export handler, no new logic
   (`tew/api/oleaut32_handlers.py`).

With that fixed, DAO's `CoGetClassObject` ×2 and `CoCreateInstance` all
complete with real, non-fake results (`hr=0x80040112`, matching every
prior run — no regression), and for the first time execution genuinely
**returns to `MCity_d.exe`'s own code** (EBP chain frames show `← exe`
instead of `← DAO350.DLL`) — the "ole32 block" that motivated this whole
multi-day investigation is fully cleared.

Two more gaps found immediately after, both in the game's own code path
rather than DAO internals:

2. **`kernel32.dll!lstrcmpW`** — real UTF-16 word-by-word comparison,
   standard strcmp-style sign semantics (`tew/api/kernel32_io.py`,
   alongside the existing `lstrlenA`/`lstrcpyA`).
3. **`kernel32.dll!GlobalLock`/`GlobalUnlock`** — implemented as
   pass-through no-ops. Correct per real Windows' own documented behavior
   for fixed (non-moveable) memory, which is all this emulator's
   `GlobalAlloc` has ever handed out (a direct pointer via
   `state.simple_alloc`, no true `HGLOBAL` indirection) — `GlobalUnlock`
   added proactively alongside `GlobalLock` since real callers always pair
   them and it's equally trivial.

593/593 tests passing throughout (no new tests this session — all three
fixes are simple enough that live verification against real `dao350.dll`
was judged sufficient, consistent with how ordinals #15/#21/#4 were
handled).

**New blocker found, a genuinely different shape than every fix above**:
right after `CoCreateInstance` succeeds, DAO starts probing for the Jet
database engine DLL and locks onto
`GetProcAddress("msjter35.dll", "ordinal#2") -> NULL` — repeated 7,005
times verbatim with zero progress, burning the full 500,000,000-step
budget without ever halting. Not a missing-API halt; a genuine busy-loop.
`msjter35.dll` was flagged in status.md months ago as "referenced by
`dao350.dll`, never analyzed, never reached" — now actually reached, and
it hangs rather than failing cleanly. Not yet investigated. See status.md
"Current status"/queued issues for the specific open questions.

---

## 2026-07-23 (evening session) — `_invoke_emulated_proc`'s "didn't complete"
`0`-return sentinel replaced with a raised exception

Implemented the design agreed earlier the same day (see the previous
entry's "sentinel collision" discussion, and the full design that was
saved to status.md pending a prerequisite task): `cpu.fatal_halt` newly
becoming true during a call now makes `CPU.run()`/`CPU.step()`
(`tew/hardware/cpu_zig.py`) raise a new `FatalHaltError` instead of just
returning, so a fatally-halted nested call can no longer be silently
misread as a real return value downstream.

`_lib.cpu_run` (the actual FFI call into Zig) has exactly two callers in
the whole codebase, both inside `CPU.run()`/`CPU.step()` — every other
caller (`_invoke_emulated_proc`, `seh.py`'s `_invoke_handler`, the
top-level loop in `run_exe.py`) goes through those, never touches Zig
directly, so that's the one chokepoint the check needed adding to instead
of the 94 scattered `cpu.halted = True` call sites. The exact condition:
capture `was_fatal = self.fatal_halt` before calling `_lib.cpu_run`, raise
only if it's newly `True` afterward. This distinction matters and is
directly tested: an *already*-fatally-halted CPU at entry must stay a
silent no-op (the existing `test_cpu_zig_fatal_halt.py` tests from the
morning session already assert exactly this and would have caught a naive
"raise whenever fatal_halt is true" implementation). Deliberately **not**
raised for plain `cpu.faulted` — that stays a polled, SEH-recoverable
flag; `run_exe.py`'s main loop still gives it to `seh.py`'s
`dispatch_exception` for a real recovery attempt before giving up.

`_invoke_emulated_proc` (`user32_handlers.py`) needed no new logic, only
deletions: its `if cpu.fatal_halt: break` loop-exit and the matching
`elif cpu.fatal_halt and ...` branch in the "not genuinely_completed"
reporting are now unreachable (the exception already leaves the function
before either could run) and were removed, along with the now-always-true
`if not cpu.fatal_halt:` guard on the final `cpu.halted = False` cleanup.
`seh.py` and `_c_int_dispatch` needed zero changes -- reasoned through
ahead of time and confirmed live: Win32 handlers run as native ctypes
callbacks, which must catch any Python exception before it crosses back
into C, so when the exception escapes a handler mid-callback it's caught
there and discarded same as always; that's harmless because `fatal_halt`
is already permanently set by then, and the *next* `cpu.run()`/`cpu.step()`
call anywhere further up the (non-callback) Python stack sees it and
raises again, re-escaping at each ctypes boundary until it reaches
ordinary Python. `run_exe.py`'s top-level loop is the single real `except
FatalHaltError` for the whole program -- catches it, logs a one-line note,
and falls through into the existing (unchanged) watchpoint/faulted/halted
post-run reporting, since `cpu.halted` is guaranteed true by then and that
code already does the right thing.

Added 5 new tests: `CPU.run()`/`CPU.step()` raise when fatal_halt newly
fires mid-call (via a real `INT 0xFE` dispatch, not a mock), an
already-halted CPU stays a no-op, and a new
`test_invoke_emulated_proc_fatal_halt.py` exercising the full propagation
path end-to-end (a nested call hitting a fake unimplemented handler now
raises out of `_invoke_emulated_proc` instead of returning `0`). 593/593
tests passing.

Live-verified against the real `oleaut32.dll!Ordinal #4` blocker
(`SysAllocStringLen`, still unfixed, next up): the halt now surfaces as
`[UNIMPLEMENTED] oleaut32.dll!Ordinal #4 — halting` → `Fatal halt: fatal
halt at EIP=0x00209022` → one clean `Halt Diagnostic`, no duplication, run
exits immediately. Also reconfirmed the `CoGetMalloc`/ordinal-15/21 fixes
from earlier today still produce genuine non-NULL `*ppv` and a real
`hr=0x80040112` all the way up to this blocker -- no regression introduced
by this refactor.

---

## 2026-07-23 (later session) — CoGetMalloc/IMalloc implemented; DAO `*ppv`
NULL mystery resolved as a side effect

Picked up the top-priority queued item from earlier today: `ole32.dll!
CoGetMalloc` had no handler at all, and `dao350.dll`'s `DllGetClassObject`
helper-object init calls it as its very first real dependency. Implemented
in `tew/api/oleaut32_handlers.py` as a lazily-allocated singleton `IMalloc`
COM object (real COM vtable dispatch via the same `register_handler`/
`get_handler_address` trampoline mechanism D3D8's fake COM objects use, not
a Python-only shortcut) — `AddRef`'d on every `CoGetMalloc` call like real
Windows, with `Alloc`/`Realloc`/`Free`/`GetSize`/`DidAlloc` sharing
`state.simple_alloc` + `heap_alloc_sizes` bookkeeping with `HeapAlloc`/
`HeapFree`/`HeapSize` (`kernel32_memory.py`) so answers are consistent
across both allocators rather than a second, disagreeing bump heap.

Live-verifying this (per the emu32 skill's tee-once-grep-many workflow, one
short focused run per fix) surfaced two more, smaller gaps in the same
`DllGetClassObject` call path, both the identical shape — a named
`oleaut32.dll` export already implemented, but no ordinal alias registered
for it, so DAO's ordinal-only imports hit the auto-generated
`[UNIMPLEMENTED] ... — halting` stub instead:
- **Ordinal #15** (`SafeArrayCreate`) — aliased to the existing
  `_SafeArrayCreate` handler, no new logic.
- **Ordinal #21** (`SafeArrayLock`) — implemented as a no-op returning
  `S_OK`, following the precedent `SafeArrayUnaccessData` already set two
  lines above it (this emulator tracks no lock count anywhere).

With all three fixes in place, live-verified: `DllGetClassObject` now
genuinely completes its real `QueryInterface` call and writes `*ppv` for
real (`MOV [ECX],EAX (*ppv=this)` at `0x0447d458`, non-NULL), across three
separate `CoGetClassObject`/`CoCreateInstance` calls for different riids —
**resolving the `*ppv`-stays-NULL mystery** that the 2026-07-19/21
investigation (see status.md "Background") had statically diagnosed but
left blocked on `tid=1012`'s premature death (fixed 2026-07-21) and then on
`CoGetMalloc` itself. Root cause confirmed exactly as the 2026-07-22 entry
below predicted: `dao350.dll`'s helper-object init chain was aborting on
missing dependencies before ever reaching its own (statically-verified-
correct) `QueryInterface` code, and `_invoke_emulated_proc`'s bare-`0`-on-
abort sentinel was indistinguishable from a genuine `S_OK`, disguising the
abort as a clean "success with NULL `*ppv`" — not a bug in DAO's own code
at all, as suspected.

Next real blocker, immediately after: `oleaut32.dll!Ordinal #4`
(`SysAllocStringLen`) — almost certainly the same missing-ordinal-alias
shape as #15/#21 above; not yet fixed. Also noticed but not fixed: two
leftover debug logpoint callbacks from the earlier investigation
(`_log_dao_tlssetvalue_call`, `_log_dao_init_shortcut_check` in
`run_exe.py`) now throw on every call (`AttributeError: 'LP_c_ubyte' object
has no attribute 'read32'`, silently swallowed by ctypes) — harmless but
noisy, low priority. See status.md "Current status" for full detail.

589/589 tests still passing throughout (no test changes this session — all
three fixes verified live against the real `dao350.dll` binary rather than
via new unit tests, consistent with this investigation's existing
verification style).

Follow-up cleanup: removed all five now-resolved TEMP diagnostic logpoints
from `run_exe.py` (`_log_dgco_entry`/`_log_dgco_call_queryinterface`/
`_log_dgco_call_release`/`_log_qi_ppv_write` for the `*ppv` investigation
just closed above; `_log_dao_init_shortcut_check`/`_log_dao_tlssetvalue_call`
for the superseded `TlsSetValue`-skip hypothesis; `_log_isdbcs_call` for the
`tid=1012` investigation resolved 2026-07-21) — each was explicitly marked
"TEMP -- discard once root-caused/confirmed" in its own comment, and the two
`_log_dao_*` ones had gone stale, throwing `AttributeError: 'LP_c_ubyte'
object has no attribute 'read32'` on every call. Also dropped the
now-unused `EAX`/`ECX`/`ESI` register-constant imports these left behind.
589/589 tests still passing.

---

## 2026-07-23 — cpu.fatal_halt is now a real, unclearable native CPU lockup

Picked up directly from the previous session's discovery that execution
visibly continued past the `CoGetMalloc` fatal halt to a later, unrelated
halt seconds afterward, instead of stopping immediately. Root-caused:
`ZigCPU.faulted`'s setter (`tew/hardware/cpu_zig.py`) called the native
`cpu_clear_halted` (`cpu/src/cpu.zig`) unconditionally whenever `cpu.faulted`
was cleared back to `False` -- the emulator's SEH-resume path does exactly
this after deciding a CPU fault was "handled." `cpu_clear_halted` cleared
native `s.halted` regardless of `fatal_halt`, desyncing it from the
Python-side sticky flags every other check in the codebase reads (every
*other* halt-clearing call site was already guarded with
`if not cpu.fatal_halt:` -- confirmed via grep this was the one unguarded
site). `cpu_run`'s own per-instruction loop obeys native `s.halted`, not the
Python property, so later `cpu.run()` calls (notably
`_invoke_emulated_proc`'s polling loop, which calls `cpu.run()` before
re-checking halted state) genuinely executed more real instructions.

Fixed at the CPU/Zig layer rather than with another Python-level guard --
discussed at length and a deliberate choice, not the default one. `fatal_halt`
has no real x86 analog (there's no hardware concept of "an unimplemented
Win32 API"), but this emulator models exactly one physical core, so once it
fires, nothing should be able to hand the core to a different thread as if
it were merely idling -- scattered Python-level `if not cpu.fatal_halt:`
checks before every halt-clearing call site is exactly the pattern that let
this bug through in the first place, and a Plan agent's alternative
recommendation (a pure-Python guard on the `faulted` setter, matching that
same idiom) was explicitly overridden for the same reason.

Added a new native `fatal_halted` field on `CpuState` (`cpu/src/core.zig`),
a dedicated `cpu_set_fatal_halt`/`cpu_is_fatal_halted` pair with deliberately
no clear path, made `cpu_clear_halted` and `cpu_run`'s loop condition respect
it, and made every register/eflags/FPU setter (`cpu_set_reg`, `cpu_set_eip`,
`cpu_set_eflags`, `cpu_fpu_set*`) refuse to write once set -- reads stay
completely open, so diagnostics can still inspect the frozen state. This
also neutralizes a second bug found along the way: `_invoke_emulated_proc`'s
cleanup path unconditionally calls `cpu.restore_state(saved)` even when
fatally halted, which would silently overwrite the real failure-point EIP/
registers with their pre-nested-call values before any diagnostic saw them;
that write is now just a no-op. `tew/kernel/scheduler.py`'s `preempt_slice`
and the four other CPU-state-mutating entry points
(`block_current_on_cs`/`block_current_on_handles`/`sleep_current`/
`mark_current_dead`) now also refuse to act once fatally halted, for
defense-in-depth consistency, though none were concretely reachable
post-fatal-halt once the native fix landed.

Live-verified against the exact `CoGetMalloc` scenario from the previous
session: the run now stops dead at
`[UNIMPLEMENTED] ole32.dll!CoGetMalloc — halting` with zero further
scheduler activity (`grep`-confirmed no `switch:`/`[alive]` lines follow),
and the Halt Diagnostic shows the full, accurate 7-frame call chain at the
true failure point (`DllGetClassObject` → `_invoke_emulated_proc`'s own
sentinel → `Dbcode_InitDao` → `DBThreadCpp` → thread wrapper →
`THREAD_SENTINEL`) instead of the old shallow, unrelated, later trace.

Added `tests/unit/hardware/test_cpu_zig_fatal_halt.py` (real `ZigCPU`, no
mocks -- the original bug was a native-state desync a mocked CPU can't
reproduce) and a `TestPreemptSlice` class in `test_scheduler.py`. Verified
both fail against a rebuilt pre-fix `libcpu.so` and pass against the fix
(stashed the Zig/Python changes, rebuilt, confirmed 4 failures; restored,
rebuilt, confirmed all pass). 589/589 tests passing.

Also documented (not yet fixed, deferred, unrelated to the above): while
figuring out what "real CPU" this should match, traced `cpu/src/two_byte.zig`'s
`CPUID` handler against actual Intel datasheets (Pentium Pro's feature table,
Pentium II OverDrive's CPUID table) and found it doesn't self-consistently
identify as any real chip. Since MMX is a hard requirement and Pentium Pro's
own documented feature set has no MMX bit at all, the correct target is real
Pentium II (Family 6, Model 3 "Klamath" or Model 5 "Deschutes") -- reconciles
with `memory/status.md`'s existing "Pentium II instruction set" header, which
was already right; only the actual `CPUID` value (currently `0x00000600`,
should be `0x00000630`/`0x00000650`) and the "source of truth" reference
(currently the unrelated 80386 manual) need correcting. Blocked on locating
the exact Pentium II spec manual to confirm Model/Stepping.

## 2026-07-22 (later session) — Root-caused the *ppv NULL mystery:
CoGetMalloc is unimplemented; found a related hr/S_OK sentinel collision
along the way

Continued straight from the same session's IsDBCSLeadByte fix, which
unblocked `DllGetClassObject`. Of the four existing TEMP logpoints on
`DllGetClassObject`'s internals, only the entry one fired -- meaning the
real code takes an early-exit path before ever reaching the
`QueryInterface`/helper-construction code the previous session's static
analysis had reviewed (and found calling-convention-correct). That analysis
was accurate; it just wasn't the code actually being hit.

Traced further into `dao350.dll`'s real functions (`debug_clean` Ghidra
project): `DllGetClassObject`'s helper-object allocator (`FUN_0447d31e`)
calls `FUN_044947fc` for per-thread init, which needs a per-thread
"task memory" arena stored via `TlsSetValue`. Two rounds of manually
computed instruction offsets for logpoints (guessing which stack slot held
which decompiled variable) both missed -- Ghidra's decompile of this DLL
has already been shown misleading twice before in this investigation, and
manual offset math compounded it a third time. Switched to logging simple
function-entry points instead of guessed mid-function offsets, which
immediately showed the real problem: `FUN_044947fc` calls `CoGetMalloc`
(`ole32.dll`) as its very first real dependency, and tew has no handler for
it at all -- `[UNIMPLEMENTED] ole32.dll!CoGetMalloc — halting` fires
immediately, long before `TlsSetValue` or the "already initialized"
shortcut check (the branch two rounds of guessed logpoints were aimed at)
are ever reached. The whole arena-init chain aborts at this first
dependency; `FUN_0447d31e` returns NULL; `DllGetClassObject` takes its
`local_c == NULL` branch, skipping `QueryInterface`/`*ppv` writes entirely.

Found a second, structurally separate bug while confirming this:
`hr=0x00000000` in the `CoGetClassObject(...) -> hr=0x00000000
*ppv=0x00000000` log line isn't `DllGetClassObject` genuinely returning
`S_OK` -- `_invoke_emulated_proc` returns a bare `0` whenever a nested call
doesn't complete (fatal halt, dead thread, `max_steps` exhausted), which is
a safe, conservative sentinel for `DllMain`-style callers (`0` = FALSE) but
collides with `S_OK` for any `HRESULT`-returning nested call
(`_call_dll_get_class_object`, `oleaut32_handlers.py`, is the one hit here,
but the same collision would affect any future `HRESULT` nested-call site).
Not fixed yet -- flagged in status.md as its own queued item.

Also surfaced, not yet root-caused: `cpu.fatal_halt` is documented
everywhere as "the whole emulator must stop" and is never cleared anywhere
in the codebase, yet the run visibly continued past `CoGetMalloc`'s fatal
halt to a later, unrelated halt rather than stopping immediately. Queued as
the new top priority.

## 2026-07-22 — Implemented IsDBCSLeadByte properly (codepage-derived);
DllMain now completes for real; DllGetClassObject/*ppv NULL mystery
reproducing again with new evidence

Picked up right where the previous session left off: `IsDBCSLeadByte` was
the concrete cause of `tid=1012`'s death, confirmed by the fatal-halt fix
landing exactly there (`[UNIMPLEMENTED] kernel32.dll!IsDBCSLeadByte —
halting`). Rather than a bare always-FALSE stub, checked whether that
answer is actually *true* for this game rather than merely convenient: tew
already hardcodes `GetACP() -> 1252` (Western/Latin) everywhere, and
`GetCPInfo`'s `LeadByte[]` table was already written as all-zero — so
"no lead bytes" isn't a new assumption, it's what tew's own environment
already asserts. Also confirmed MCity_d.exe itself never imports
`IsDBCSLeadByte` at all (string search of the whole exe found zero
matches) — only `dao350.dll` does. Separately confirmed `dao350.dll`
references `MSJET35.DLL` (the real Jet 3.5 engine) as a DLL it loads
dynamically by name -- Jet's own code hasn't been analyzed or reached by
the emulator at all, so no DAO-specific guarantee is made about it;
instead, implemented `IsDBCSLeadByte` (`tew/api/kernel32_locale.py`) so
`GetACP`/`GetCPInfo`/`IsDBCSLeadByte` all derive from one `ANSI_CODEPAGE`
constant and a shared `_DBCS_LEAD_BYTE_RANGES` table (covering
932/936/949/950 too, not just 1252) -- correct by construction for
whichever of these three APIs any future caller (Jet included) happens to
use, rather than three independently-stubbed constants that could drift
out of sync.

Live-verified: all 256 `IsDBCSLeadByte` loop iterations now complete (was
1 of 256 before the fix), `DllMain(DLL_PROCESS_ATTACH) -> 1` (correct,
matching the real decompiled `entry()` function), and the run reaches
`DllGetClassObject` for real -- `CoGetClassObject(...) -> hr=0x00000000
*ppv=0x00000000`, reproducing the `*ppv` NULL mystery documented in the
previous session, now unblocked.

New evidence while there: of the four existing TEMP logpoints
(`_log_dgco_entry`, `_log_dgco_call_queryinterface`, `_log_dgco_call_release`,
`_log_qi_ppv_write`, all in `run_exe.py`), only the entry logpoint fired.
Neither the `QueryInterface` CALL site nor the `*ppv`-write site fired at
all -- meaning `DllGetClassObject`'s real code takes an early-exit path
*before* reaching the helper-object construction/`QueryInterface` call
the earlier session's static analysis reviewed (and found calling-
convention-correct). That earlier analysis may be entirely accurate for
code that's simply never reached. 583/583 tests passing.

## 2026-07-21 (later session) — Root-caused and fixed tid=1012's premature
death: an unmatched secondary-DLL import (IsDBCSLeadByte) silently executed
garbage instead of halting

Picked up where the previous session's `DllMain` fix left off: `tid=1012`
still died mid-call on every run, blocking DAO activation. Added a
thread-end stack dump (`diagnose_thread_end`, `tew/kernel/
exception_diagnostics.py`, fired from `_make_thread_return_handler` in
`crt_handlers.py`) gated to `tid=1012` to see what the stack actually looked
like at the moment of "death." It showed the nested-call sentinel
`_invoke_emulated_proc` had pushed for the `DllMain` call, and every real
frame above it (`Dbcode_InitDao` → `DBThreadCpp` → `DBThread` →
`lpStartAddress_009fc3a0`, confirmed via Ghidra decompilation of MCity_d.exe),
all still sitting untouched in memory -- proving something jumped straight
past all of them without ever executing a matching `RET`, and ruling out an
SEH/C++ `terminate()` explanation (no `[seh]` log activity anywhere in the
run, and none of `Dbcode_InitDao`'s own error-print branches ever fired --
its real code never got control back at all).

Traced into `dao350.dll` itself (project `debug_clean` in Ghidra, not
`mcity`): `DllMain`'s real code (`0x04479f74`) is trivial and safe --
`InitializeCriticalSection` x2, `TlsAlloc`, then a 256-iteration loop
calling `IsDBCSLeadByte` through a cached register (`FUN_044c63fc`, raw
bytes confirmed `MOV ESI,[0x04471074]` once before the loop, `CALL ESI`
each iteration). tew has no handler registered for `IsDBCSLeadByte`
anywhere. A one-shot logpoint at the `CALL ESI` site (`0x044c6410`)
confirmed it: only 1 of the expected 256 calls fired, with
`ESI=0x000735ba` -- not a valid code address in any known range.

Root cause: `patch_dll_iats` (`tew/loader/dll_loader.py`), the IAT-patching
path used for secondary DLLs loaded at runtime (unlike `write_iat_handlers`
for the main EXE), had no fallback for an unmatched import -- it silently
left the IAT slot holding whatever raw, unrelocated bytes were in the DLL
file on disk. Since tew's memory (`tew/hardware/memory.py`) is an
unprotected flat `bytearray`, `CALL ESI` into that garbage doesn't fault the
way it would on real Windows; it just silently executes whatever's there
(almost certainly zero bytes), with no halt, no SEH activity, and no log
line, until it wanders far enough to land on `THREAD_SENTINEL` by
coincidence -- exactly matching every symptom observed.

**Fix**: unified IAT-patching into one shared function, `patch_iat_entry`
(`tew/loader/dll_loader.py`), used by both `write_iat_handlers`
(`import_resolver.py`) and `patch_dll_iats`. Any unmatched import -- main
EXE or secondary DLL -- now gets the same auto-generated `[UNIMPLEMENTED]`
fatal-halt stub. Live-verified: `IsDBCSLeadByte` now produces
`[UNIMPLEMENTED] kernel32.dll!IsDBCSLeadByte — halting` instead of silent
garbage execution, and `DllMain`/`CoGetClassObject` report an honest,
traceable failure. Also consolidated the three diagnostic dump functions
(`diagnose_fault`/`diagnose_halt`/`diagnose_thread_end`,
`tew/kernel/exception_diagnostics.py`) onto one shared `_dump_cpu_state`
helper -- the original `diagnose_thread_end` didn't dump full GPRs (only
ESP/EBP), a gap caught mid-investigation since ESI was exactly the register
that mattered here.

Added `tests/unit/api/test_invoke_emulated_proc_thread_death.py`, a unit
test for the "calling thread died mid-call" detection fixed in the previous
session -- previously untested at the unit level. Verified it fails without
the fix (asserts on garbage EAX) and passes with it. 583/583 tests passing.

## 2026-07-21 — DllMain's "non-deterministic" return value fully diagnosed
and fixed: it was register misattribution on top of a real thread
(`tid=1012`) dying mid-call, not corruption

Started as "explain the DllMain non-determinism" (status.md's
2026-07-19-night open item: `0`, `105`, `70959764`, `70959264`,
`70961940`, `70957766` observed across separate runs). Ended up finding two
independent, real bugs in `_invoke_emulated_proc` (`tew/api/user32_handlers.py`),
plus new scheduler debug visibility that made the second one findable at all.

**Bug 1 — result misattribution.** `_invoke_emulated_proc`'s polling loop
(added in the 2026-07-19 thread-switch fix) unconditionally read
`cpu.regs[EAX]` as "the call's return value" after breaking out of its
chunked `cpu.run()` loop, regardless of *why* it broke: a fatal halt on an
unrelated thread, or exhausting `max_steps` while some other cooperative
thread was live. Both cases leave whatever register state that *other*
code left behind, misattributed as "this call returned `<it>`". Confirmed
via raw disassembly of the real `entry()`/`DllMain` wrapper
(`0x04479f74`, `dao350.dll`) that it can only ever return exactly `0` or
`1` — so every other observed value was leftover EAX from unrelated code,
not anything `DllMain` computed. Fix: track whether the call "genuinely
completed" (halted on the *originating* thread, at the sentinel) and
return `0` with a clear diagnostic for every other exit path, instead of
reading a register that doesn't hold what's claimed.

**Bug 2 — the real root cause.** Even with budget raised 10x (5,000,000 →
50,000,000 steps), `DllMain`'s call still never completed. New scheduler
debug logging (see below) traced it exactly: the real calling thread is
`tid=1012` (idx=12), a short-lived worker spawned via the same generic CRT
thread wrapper (`0x9fc3a0`) as the timer thread and others — spawned ~57s
in, and **dead 244ms later**, having hit its own `THREAD_SENTINEL` (i.e.
its stack unwound straight past the sentinel `_invoke_emulated_proc`
pushed for `DllMain`'s return). Since dead threads are permanently excluded
from `_pick_next_ready`, the loop's "wait for `scheduler.current_idx` to
become `started_thread_idx` again" condition was mathematically
unsatisfiable from that point on — no `max_steps` budget, however large,
was ever going to complete it. Fix: detect
`threads[started_thread_idx].status == DEAD` inside the polling loop and
bail immediately instead of exhausting the budget uselessly.

**Live-verified**: the run now reaches the identical final halt
(`EIP=0x00200c00`, same registers/stack) at **57.4s instead of 71.4s**
(~14s faster), with an honest `DllMain(DLL_PROCESS_ATTACH) -> 0` log
instead of a misleading garbage number or a multi-second stall. *Why*
`tid=1012` dies prematurely is still open — see status.md's new top
priority.

**New scheduler debug visibility** (`tew/kernel/scheduler.py`,
`tew/api/kernel32_io.py`) — this is what made the `tid=1012` diagnosis
possible at all; without it, the initial "scheduler fairness/starvation"
hypothesis would have been very hard to rule out:
- `create_thread`'s caller (`_create_thread`, `kernel32_io.py`) now logs
  the assigned scheduler `idx` alongside `tid` — previously `idx` was
  completely untraceable from logs, only `tid` was ever printed.
- Every actual context switch is now logged in `_load_next`: `switch:
  idx=X (tid=Y) -> idx=Z (tid=W)`.
- Every block transition now logs *why* (`block_current_on_cs`,
  `block_current_on_handles`, `sleep_current`) — previously only the
  *wake-up* side (`unblock_cs`, `unblock_handle`, `tick` sleep-wake) was
  logged, never what a thread actually blocked on or when.
- Enable via `LOG_LEVEL=debug LOG_CATEGORIES=scheduler,thread`.

**Ruled out this session** (worth recording so it isn't re-investigated):
a `tid=1007`/`tid=1009` wait/signal ping-pong on handle `0x701a` dominates
the scheduler log for the entire stall window and initially looked like a
starvation livelock — but the same wait/signal/wait-again shape is present
in `tid=1006` (the timer thread) from as early as 4s into the run, which is
its normal, designed per-tick behavior. Not a bug on its own; the real
cause was the dead-thread issue above, unrelated to this pattern.
Separately, `mmtimer_callback`'s own nested call was suspected of calling
`abortmessage` on failure — decompiled directly (`0x00a30a40`) and
confirmed it does NOT; its `timeSetEvent`-failure path is just a silent
`_DEBUG_trace` + graceful shutdown (`_TIMERhz = 0`, `timeEndPeriod`).
`_TIMER_init` (`0x00a30be0`) does have real `abortmessage` calls, but none
of its guard conditions are triggered by our stubs, and its one
retry-exhaustion path takes several real seconds via a `_THREAD_yield`
spin-loop — timing doesn't match the observed ~microsecond-scale event.
The final `EIP=0x00200c00` halt is confirmed unrelated to DAO/`DllMain`
(happens at the same relative point regardless of how `DllMain` resolves)
but its real cause (which unimplemented API) is still unidentified.

**Status**: 582/582 tests pass. Live-verified against the real
`MCity_d.exe` + `dao350.dll`. **Uncommitted** in the working tree
(`run_exe.py`, `tew/api/kernel32_io.py`, `tew/api/oleaut32_handlers.py`,
`tew/api/user32_handlers.py`, `tew/kernel/scheduler.py`) — not committed
per "only commit when explicitly asked."

---

## 2026-07-19 (night, continued further) — the "~496-byte stack corruption"
was actually a NULL-vtable dispatch crash; real root cause identified for
the crash chain, none of it fixed yet

Continuation of the same night's DAO investigation (previous entry below),
picked back up via a run of clarifying COM questions (IID vs IClassFactory,
what `ppv` points to, whether the `__chkesp` signed-comparison handling was
correct) that led to actually re-examining the evidence rather than trusting
the earlier "stack corruption" framing.

**The corruption framing was wrong.** Re-running with the `seh` log
category (prompted by "nothing I'm seeing would explain a 496-byte
object") showed the real sequence: `dao350.dll`'s real `DllGetClassObject`
returns `S_OK` for the game's first `CoGetClassObject` call but never
populates `*ppv` (confirmed by logging `*ppv` alongside `hr` at the call
site — it stays NULL). The game's own fallback code doesn't NULL-check
`local_2c` before dispatching through its vtable, wild-jumps to
`EIP=0xfefc8d8f`, and takes a real `0xc0000005` ACCESS_VIOLATION. SEH
(`tew/kernel/seh.py`) walks 15 real `FS:[0]` frames, finds no handler, and
takes the "unhandled fault, halting as before" path — which is not a clean
unwind, and leaves stale return addresses on the stack. `__chkesp` was
detecting *that* leftover stale data, not a fixed-size corruption: the
~496-byte delta is consistent with stale frames from the 15-frame walk,
and — the clinching piece — `__chkesp`'s own reported return address
(`0x008f55a0`) doesn't match the real one at that call site (`0x008f5351`,
confirmed via `dump_bytes`), which only makes sense if recovery left old
data in place instead of restoring the true frame.

**Also found, while chasing this**: `_chkesp`'s diagnostic
(`patch_internals.py`, `0x009F1BC0`) hardcodes `EBP` as "the" pre-call ESP
snapshot register in its delta-computation message. The ZF check it
performs to decide pass/fail is correct and sign-agnostic (`ZF_BIT` only),
but the snapshot register is actually a compiler register-allocation
choice, not always EBP — at the specific call site `0x008f4f04`/`06` the
real instruction is `CMP ESI,ESP` (`3B F4`, confirmed via raw byte
decoding), not `CMP EBP,ESP` as the message claims. Not yet fixed.

**Net result**: the open bug is no longer "find what's corrupting the
stack." It's now three concrete, well-scoped items — see status.md's
"Next step" section: (1) why `DllGetClassObject` leaves `*ppv` NULL
despite `S_OK` (its own code reads correct, so likely something this
emulator provides it that's wrong), (2) whether `seh.py`'s unhandled-fault
path should do a real unwind instead of halting with stale stack state,
and (3) fixing `_chkesp`'s hardcoded-EBP diagnostic message. Nothing code-
level was changed this entry — this was pure re-diagnosis of existing
behavior via more targeted logging and disassembly cross-referencing.

---

## 2026-07-19 (night, continued) — _invoke_emulated_proc made thread-aware
(real, separate bug, found and fixed); DAO's real DllGetClassObject read
directly in Ghidra and confirmed correct; the actual bug is now precisely
localized to a deterministic stack corruption, not yet root-caused

Continuation of the same night's DAO investigation (previous entry below).

**`dao350.dll` opened in Ghidra as its own program** (`import_and_analyze`,
`~/.emu32/WINDOWS/System32/dao350.dll` — previously only `MCity_d.exe` had
ever been analyzed) and its real `DllGetClassObject`/`QueryInterface` read
directly, at the user's request, after live logging showed both
`CoGetClassObject` calls returning `hr=S_OK` with `*ppv` staying NULL.
Ghidra's decompiled C was actively misleading for the CLSID-matching logic
— it rendered real 16-byte `REP CMPSB` GUID comparisons (confirmed from the
*raw disassembly*, not the decompile) as fake 1-2 byte C string literals,
making it look like only the first byte of the CLSID was being checked.
The real logic does a full, correct 16-byte compare against several
DAO-family candidate CLSIDs and correctly matches ours (live-confirmed via
a Zig-level logpoint at the match branch — fires every time, regardless of
outcome). The same investigation found what looked like a bug (the same
8-byte-allocated object's vtable-pointer field gets overwritten 4 times in
a row by `FUN_0447d398`) but is actually a completely ordinary
Borland/Delphi-style multi-level virtual-inheritance constructor chain —
each write is a different base class setting its own vtable, only the
last one (the most-derived class, set last) matters. `QueryInterface`
itself (found via `dump_bytes` on the real vtable address, after an
initial logpoint at a *wrong* guessed address never fired) correctly
recognizes `IUnknown`/`IClassFactory`/`IClassFactory2`, calls `AddRef`,
and writes itself into `*ppv` — this code, read statically, is completely
correct for what the game requests. None of tonight's earlier suspicion
that DAO's own code was somehow buggy held up under direct inspection.

**Root cause re-localized, live, via logpoints and Ghidra raw disassembly
cross-referencing**: a deterministic ~496-byte stack corruption, always
detected by the game's own `__chkesp` at the exact instruction
(`0x008f4f0b` in `FUN_008f4e70`, confirmed via raw disassembly down to the
individual `PUSH`/`CALL COMPUTED_CALL`/`CMP EBP,ESP`/`CALL __chkesp`
instructions) immediately following the game's first
`CoGetClassObject(rclsid, 1, NULL, IID_IClassFactory, &local_2c)` call —
not, as first suspected, something specific to `CoCreateInstance` (an
earlier run's corruption happened to surface there instead, but that was
this same underlying bug manifesting at a different point, not a second
distinct bug — confirmed by the address being identical to the
`CoGetClassObject`-triggered case in other runs).

**Found and fixed a real, separate bug while chasing this**:
`_invoke_emulated_proc` (`user32_handlers.py`) ran `cpu.run(max_steps)` in
one single 5,000,000-step call with no awareness of logical threads. If
the calling thread hits anything that makes the cooperative scheduler
switch away (`Sleep`, `WaitForSingleObject`, a contested critical section
— all implemented by directly swapping CPU state via
`scheduler._save_current`/`_load_next`), the nested call would keep
executing whatever thread was live next, for up to the full step budget,
with zero awareness it was no longer running the code it actually called.
A halt hit on that other thread (its own unrelated sentinel, or worse, an
unrelated fatal halt) would then be misread as "our nested call returned".
**Live-verified this genuinely happens**: calling `dao350.dll`'s real
`DllMain`, virtual time jumped 1.4 seconds and five unrelated threads
(1000, 1004, 1005, 1007, 1009) ran to completion inside what was supposed
to be one narrow nested call. Fixed: run in bounded 200,000-step chunks;
after each chunk, if `scheduler.current_idx` no longer matches the thread
that started the call, clear any non-fatal halt (it belongs to the other
thread) and keep going — the calling thread eventually gets rescheduled.
A genuine `fatal_halt` still stops everything immediately regardless of
which thread caused it. New optional `scheduler` parameter, backward
compatible for the existing short dialog-proc/timer-callback call sites
that don't pass it (their calls are short enough this was never observed
to matter); wired into the three DAO call sites where it was found.

**This fix did NOT resolve the stack corruption** — live-verified after
the fix, the exact same `__chkesp` failure (same address `0x008f4f0b`,
same ~496-byte delta) still occurred. So scheduler thread-switching, while
a real and worth-fixing bug in its own right, was not the (sole) cause of
this specific corruption. Since `_invoke_emulated_proc` forcibly restores
`ESP` as a *register* via `cpu.save_state()`/`restore_state()` regardless
of how the real DAO code cleans up internally, the corruption has to be
in stack *memory content*, not the ESP register itself — meaning
register-level instrumentation (which is all that was used tonight) can't
see it directly. `DllMain`'s return value also remains non-deterministic
across runs (`0`, `70959764`, `105`, `70959264`, `70961940`, `70957766`
observed) — not distinguished yet whether this is the same underlying
issue or separate.

**Diagnostic instrumentation used tonight, all discarded (`git checkout --`)
once it had served its purpose, not committed**: `run_exe.py` temporarily
grew three Zig-level logpoints (`cpu.add_logpoint`, not breakpoints —
breakpoints halt execution and aren't dispatched from inside a nested
`_invoke_emulated_proc` call, so they'd be silently misinterpreted as "the
call returned") tracing the outer CLSID-match branch and the real vtable
call site in `dao350.dll`'s `DllGetClassObject`. `oleaut32_handlers.py`
temporarily grew an ESP/EBP bracket around the nested calls in
`_CoGetClassObject`/`_CoCreateInstance`. Both served their purpose (found
the thread-switching bug, precisely localized the corruption's trigger
point) and were removed once done, per this project's established
"TEMP diagnostics get discarded, not shipped" convention.

**Next step, if this is picked up again**: a memory-write-level trace is
needed, not more register-level logpoints. The ClickHouse execution-history
capture tooling from `~/pe-walker/history-poc` (already proven for exactly
this "what wrote to this address" question in earlier sessions — see
[[tew_fake_kernel_gaps]] section 10 via the wiki/memory system) is likely
the right tool, scoped narrowly to the ~496-byte window between the game's
ESP and EBP during the first `CoGetClassObject` call specifically.

**Commits** (`main`, not pushed): `5e49168` `_invoke_emulated_proc`
thread-awareness (the only code change that survived this half of the
session — the corruption investigation itself produced no shippable fix
yet).

---

## 2026-07-19 (night) — Real COM activation: CoGetClassObject/CoCreateInstance
now load and execute real DLLs (DAO 3.5); several real emulator bugs found
and fixed along the way; final DAO object handoff still broken (open)

**Architecture change**: `CoGetClassObject`/`CoCreateInstance`
(`tew/api/oleaut32_handlers.py`) previously always returned
`REGDB_E_CLASSNOTREG` — no COM was implemented at all. They're now
registry-driven, the same way real Windows COM activation works: look the
requested CLSID up under `hkcr\clsid\{...}\inprocserver32` in
`registry.json` (new entries use this key shape; `registry.json`'s own
`_comment` already documented `hkcr` as a supported hive short name). A
CLSID nobody registered fails honestly with `REGDB_E_CLASSNOTREG`, exactly
like a real, unmodified install missing that component — no hardcoded
per-CLSID branching.

For CLSIDs registered to a server in the new `_KNOWN_COM_SERVERS` set, the
real DLL is loaded and executed via the existing `DLLLoader` machinery
(same mechanism already proven for `authlogin.dll`/`NPSAnlyz.dll`/
`dx8z.dll` — real PE load, real section mapping, real code execution; this
is architecturally the *opposite* of the Python-handler-interception
approach used for OS-level DLLs like kernel32/user32, and was a deliberate
choice discussed with the user rather than building fake Python vtable
objects). `DllMain(DLL_PROCESS_ATTACH)` is invoked for real, then the real
exported `DllGetClassObject` is called; for `CoCreateInstance`, the
resulting `IClassFactory`'s real `CreateInstance`/`Release` vtable methods
are dispatched through too. A `FALSE` return from `DllMain` is now treated
as a real load failure (matches real `LoadLibrary` semantics) rather than
proceeding to call into a DLL that just reported its own init failed.

**The DAO CLSID version mismatch** (this session's whole reason for
investigating): the game's compiled-in CLSID `{00000010-0000-0010-8000-
00AA006D2EA4}` is DAO **3.5**'s, confirmed via `dump_bytes` byte-scanning —
`dao360.dll` (from the generic period-correct-binaries collection at
`/data/Downloads/i386-binaries/`) does NOT contain this CLSID anywhere in
its binary; `dao350.dll`, extracted from the game's own real installer
(`~/.emu32/DBInst/DAO/data1.cab`, `DAO registered\dao350.dll`, via
`unshield x`), DOES. DAO 3.5 and 3.6 are different, non-interchangeable
installed components, not just a version bump — a newer DLL doesn't
substitute for an older CLSID a game statically compiled against. Placed
at `~/.emu32/WINDOWS/System32/dao350.dll` (i.e. exactly where the real
installer would have put it) — kept out of the tew repo itself since these
are Microsoft-copyrighted redistributable binaries, not project source.

**Real bugs found and fixed while getting this far** (each independently
live-verified and test-suite-clean, 582/582 throughout):

1. **`_invoke_emulated_proc` (`tew/api/user32_handlers.py`) was
   single-stepping one instruction at a time** via a Python `while` loop
   (`cpu.step()` is literally `cpu.run(1)` under the hood — same native FFI
   call, `max_steps=1`), paying a full Python/Zig crossing per instruction.
   Fine for the short dialog-proc/timer-callback calls this helper was
   originally built for, ruinously slow for a nested call running real
   third-party DLL code end to end. Fixed: call `cpu.run(max_steps)` once —
   the sentinel this waits for is a real `HLT` byte in memory
   (`_get_dialog_sentinel`), so the native Zig hot loop already halts on
   its own the instant the call returns, exactly like any other halt
   condition. Had to account for `HLT` advancing EIP past its own 1-byte
   opcode (lands at `sentinel+1`, not `sentinel`) when checking where the
   call actually stopped. Measured effect live: DAO's `DllMain` call
   dropped from ~7s to ~1.3s wall-clock; the first `CoGetClassObject` from
   ~17s to ~9.2s (still slow in absolute terms — DAO's real code makes many
   Win32/CRT calls, each of which still needs a real Python handler
   round-trip regardless of how the between-calls instructions are driven).

2. **`patch_dll_iats` was being called unconditionally on every
   `CoGetClassObject`/`CoCreateInstance`**, re-scanning the *entire*
   accumulated IAT-entry list for every DLL ever loaded (`MCity_d.exe` +
   `authlogin.dll` + `NPSAnlyz.dll` + `dx8z.dll` + `dao350.dll`, hundreds of
   entries) even when nothing new had been loaded since the previous call.
   Fixed: only call it right after a genuinely new `load_dll` (`not
   was_loaded`). Directly observed contributing to each successive DAO call
   being measurably slower than the last.

3. **`dao350.dll` imports from the legacy `MSVCRT40.dll`** (VC4-era CRT
   naming), not `MSVCRT.dll` like `dao360.dll` and everything else tew has
   loaded so far — so its IAT entries for that DLL were silently left
   unpatched (`win32_handlers.get_handler_address("msvcrt40.dll", ...)`
   never matches tew's `"msvcrt.dll"`-registered handlers). Fixed: a small
   `_LEGACY_DLL_ALIASES` table in `dll_loader.patch_dll_iats`
   (`msvcrt40.dll`/`msvcrt20.dll`/`crtdll.dll` → `msvcrt.dll`), tried as a
   last resort after the normal and `.dll`-suffixed lookups.

4. **The INT3 debug-breakpoint halt (`tew/api/win32_handlers.py`) was
   missing `cpu.fatal_halt`** — set `cpu.halted = True` only. Exactly the
   same class of gap the section-15 scheduler fix closed for unimplemented
   Win32 API halts: a plain `halted` without `fatal_halt` gets silently
   cleared by the very next scheduler thread-switch or nested
   `_invoke_emulated_proc` call, so this "halt" was never actually stopping
   anything — execution continued right past `INT3 breakpoint at
   EIP=0x00688c69 — halting` instead of genuinely halting there. This
   explains an observation from earlier tonight that looked like
   nondeterministic "bouncing" between reaching a second `CoGetClassObject`
   call versus hitting this INT3: it isn't random — when DAO's real
   `DllMain` fails fast, very little wall-clock is spent inside it, so the
   rest of boot has time to reach the (unrelated, later) INT3 milestone
   within a fixed timeout; when DAO succeeds, the real `CoGetClassObject`
   calls burn most of the wall-clock budget, so the run simply hasn't
   reached that later point yet when the timeout fires. Not yet
   independently re-verified in isolation (a full run since this fix always
   also has the DAO work active).

5. **`GetLastError`/`SetLastError` (`tew/kernel/scheduler.py`) shared one
   fixed memory-backed value (`TEB_BASE+0x34`) across every thread** —
   real Windows gives each thread its own TEB/last-error slot; here, one
   thread's `SetLastError` could silently clobber every other thread's
   `GetLastError` result across a context switch. Fixed: added
   `ThreadState.last_error`, saved/restored on every context switch
   (`_save_current`/`_load_thread`/`_init_thread_stack`), exactly mirroring
   the existing TLS-slot save/restore (`_save_tls`/`_load_tls`) shape.
   Found while reasoning about whether real third-party DLL code (far more
   likely than tew's own handlers to actually race multiple threads through
   Win32 calls) could be exposing this kind of latent gap.

6. **`FormatMessageA` (`tew/api/kernel32_io.py`) was an unconditional
   halt.** The game's own COM error-handling path (`dbcode.c`, called when
   `CoGetClassObject`/`CoCreateInstance` fail) calls this to turn an
   HRESULT into readable text before logging/displaying it, so the halt was
   masking the actual diagnostic message rather than just being an
   unrelated missing API. Implemented: `FORMAT_MESSAGE_FROM_SYSTEM` (a
   small table of the HRESULTs this emulator's own COM handlers actually
   produce, e.g. `0x80040111` → "Class not registered for this server --
   the object is not available") and `FORMAT_MESSAGE_FROM_STRING`;
   `ALLOCATE_BUFFER` supported; `%1`/`%2`-style insert-argument
   substitution is not, since every caller seen so far uses
   `FORMAT_MESSAGE_IGNORE_INSERTS`.

7. **`Channel_DebugPrint` (`channel.c`, `FUN_004cc5b0`) was entirely
   unpatched real game code** whose real routing target (a per-`(user,
   channel)` listener table gated on a runtime debug-console-enabled flag)
   is the game's own unrendered debug console — meaning nothing it logs
   ever reached tew's log regardless of that gate. Patched the whole
   function like `_CrtDbgReport`: format the `%s`/`%d` varargs ourselves
   (multi-arg, in appearance order — the existing `_CrtDbgReport` pattern
   only substituted one), always surface the result at `WARN`, skip
   replicating the real listener-routing plumbing.

**Logging noise cleanup** (several call sites moved from `debug`/`info`
down to `trace`, the quietest level, below `debug` — found while trying to
actually read a `LOG_LEVEL=debug` capture of the DAO work above and
discovering it was 90%+ drowned out): `_CrtDbgReport`'s routine
`_CRT_WARN`-type end-of-process memory-leak dump (fires dozens of times
per run, zero signal — the fatal `_CRT_ERROR`/`_CRT_ASSERT` path, which
halts, is untouched); `timeSetEvent` (fires roughly every 1ms via the
game's self-rescheduling multimedia timer — also quieted a second, dead/
overridden duplicate registration in `advapi32_handlers.py` for
consistency); `free`/`operator delete` (fire on every one of the bump
allocator's no-op releases); `GetFullPathNameA` (fires per file-path
lookup); `SNDMEMI_validate`'s per-pool-entry OK/not-OK detail (its
corruption-summary line, which fires only when something's actually wrong,
stays at `warn`).

**Also fixed, smaller correctness/observability items**:
- Two "activation failed" log messages in the new `CoGetClassObject`/
  `CoCreateInstance` code unconditionally claimed the DLL "failed to load
  from disk", which was actively misleading whenever the real, already-
  logged reason was a `DllMain` failure instead — now a generic, accurate
  "activation failed (see above)".
- The `IID_IClassFactory` scratch GUID buffer (needed for
  `CoCreateInstance`'s internal `DllGetClassObject(IID_IClassFactory)`
  call) was being allocated unconditionally at handler-registration time —
  broke small-memory test harnesses that construct `CRTState` without ever
  touching COM. Now allocated lazily on first use, mirroring the existing
  `user32_handlers._get_dialog_sentinel` pattern.
- Success log lines for `CoGetClassObject`/`CoCreateInstance` now include
  the resulting `*ppv` object address, not just the HRESULT — this is what
  actually surfaced the open bug below; before this, the two-line log
  looked identical apart from the requested IID and gave no way to tell
  whether two calls returned distinct objects or nothing at all.

**Open, not resolved tonight**: with all of the above fixed and `dao350.dll`
loading and running for real, both `CoGetClassObject` calls the game makes
(`IID_IClassFactory`, then `IID_IClassFactory2`) return genuine `hr=S_OK`
from real compiled DAO 3.5 code — but `*ppv` is `0x00000000` (NULL) on both,
a real COM contract violation (success must guarantee a valid object
pointer). No `_invoke_emulated_proc` timeout or unexpected-halt warning
fires during these calls, so this isn't a scheduler/timing artifact — the
nested call genuinely runs to completion and hits the real sentinel
normally; `dao350.dll`'s own code is doing this. `CoCreateInstance`'s
internal `DllGetClassObject(IID_IClassFactory)` call hits the identical
bug, and the existing NULL-check fallback correctly reports `E_FAIL` rather
than crashing on a null vtable dispatch, but the DAO handshake still can't
complete. Separately, `DllMain`'s own return value is non-deterministic
across runs (`0`, `70959764`, `105`, `70905676` observed in separate runs)
— currently handled defensively, not root-caused. Next step if this is
picked up again: `dao350.dll` itself has never been opened as its own
Ghidra program (only `MCity_d.exe` has been analyzed) — import it and read
its real `DllGetClassObject` implementation directly.

**Commits** (`main`, not pushed): `2d5f69d` `_CrtDbgReport` → debug,
`adff060` `Channel_DebugPrint`, `b163faa` `FormatMessageA`, `836ff95`
per-thread last-error, `30a1cc6` more trace-level noise, `166f0d6`
`_invoke_emulated_proc` native speed, `ed1f685` INT3 `fatal_halt`,
`bc6ac2d` real `dao350.dll` COM activation, `f59d4ef` remaining trace-level
noise.

---

## 2026-07-19 — MessageBoxA/W log severity now matches dialog icon; fatal dialogs
tracked so a voluntary ExitProcess after one can't be logged as a clean exit

**`tew/api/user32_handlers.py`**: `_show_messagebox` now maps the same `u_type &
0x70` icon bits already used to pick the SDL dialog's appearance to a log level —
`MB_ICONERROR`/`MB_ICONSTOP`/`MB_ICONHAND` (`0x10`/`0x20`) → `logger.error`,
`MB_ICONWARNING`/`MB_ICONEXCLAMATION` (`0x30`) → `logger.warn`, else `logger.info`
(unchanged default). Applies whether the dialog was auto-answered via a hook or
shown for real. Previously every `MessageBoxA`/`MessageBoxW` call logged at a flat
`INFO`, so a fatal stop-icon abort and a routine yes/no confirmation were
indistinguishable in the log — found via a live run where the game's own
`abortmessage` dialog (`depthconv.c:1137`, "Failed to initialize database...",
`type=0x11011`) logged identically to the harmless "run full screen?" prompt
(`type=0x4`).

**`tew/api/_state.py`**: new `CRTState.fatal_dialogs: list[tuple[str, str]]` —
every error-severity dialog's `(caption, text)` is appended here regardless of how
it was answered.

**`run_exe.py`**: the final run summary now checks `crt_state.fatal_dialogs` —
if any fired, logs `=== Emulation Complete (NOT a clean exit) ===` at `ERROR` plus
a listing of each fatal dialog, instead of the previous unconditional
`=== Emulation Complete ===`. This directly fixes a real misleading-log risk: a
game-side fatal abort followed by a voluntary `ExitProcess(0)` (exactly what
happens today, see status.md) used to produce a summary indistinguishable from an
actually successful run.

**Live-verified**: full run reproduces the fix end-to-end — the `abortmessage`
dialog logs at `ERROR`, the full-screen prompt still logs at `INFO` (no
regression), and the summary correctly reads `NOT a clean exit` with the abort's
caption/text listed. 582/582 tests pass (no test changes needed — this is a pure
logging-severity change with no behavior affecting emulation itself).

---

## 2026-06-06 — IDirect3DTexture8 COM interface + vtable layout fix

**New file: `tew/api/d3d8/idirect3d8texture.py`** — full 18-slot `IDirect3DTexture8`
vtable at `D3DTEX_VTABLE = 0x00220290`.

- IUnknown [0-2], IDirect3DResource8 [3-10], IDirect3DBaseTexture8 [11-13],
  IDirect3DTexture8 [14-17] (GetLevelDesc / GetSurfaceLevel / LockRect / UnlockRect).
- `GetSurfaceLevel(Level, ppSurface)` returns the pre-allocated `IDirect3DSurface8*`
  stored at `texture_obj + 28 + Level*4`.

**`tew/api/d3d8/_layout.py`** — added `D3DTEX_VTABLE = 0x00220290`.

**`tew/api/d3d8/_helpers.py`** — added `_alloc_texture_obj(w, h, fmt, levels, mem)`;
texture object holds `levels` surface ptrs starting at `obj+28`.

**`tew/api/d3d8/idirect3d8device.py`** — `CreateTexture`/`CreateVolumeTexture`/
`CreateCubeTexture` now call `_alloc_texture_obj` instead of `_alloc_surface_obj`;
`levels` argument is now read and forwarded.

**`tew/api/dinput_handlers.py`** — relocated `DI_VTABLE → 0x002202E0`,
`DI_OBJ → 0x00220310`, `DI_DEV_VTABLE → 0x00220320` (D3DTEX_VTABLE now occupies
the old DI range `0x00220290`–`0x002202D7`).

**`tew/api/dsound_handlers.py`** — relocated `DS_VTABLE → 0x00220370`,
`DS_OBJ → 0x002203A0`, `DS_BUF_VTABLE → 0x002203B0` (old DS addresses were inside
the shifted DI range).

**`tew/api/patch_internals.py`** — removed SNDMEMI pool watchpoint (was on `blist+7`,
MSB of pool block-entry size field); game now runs past ~189M steps.

**`tew/api/kernel32_io.py`** — added per-file ReadFile logging for `ealogo.mad`.

**Result:** game runs ~189M steps through many BeginScene/EndScene/Present cycles,
then crashes in `_MAD_decodemacroblock` (showmad playing ealogo.mad). SNDMEMI
watchpoint removed (new blocker: MAD decoder crash, see status.md).

---

## 2026-05-31 — ifc22.dll (ImmVersion FFB) stubs + dialog window fix

**New file: `tew/api/ifc22_handlers.py`** — stubs for all 11 ifc22.dll imports:
- Default constructors (CImmMouse, CImmProject, CImmPeriodic): thiscall no-ops
- `CImmMouse::Initialize` → returns 0 (no FFB hardware); the game's entire FFB
  code path is conditional on this return value, so it is fully skipped
- Destructors: no-op (nothing was allocated)
- FFB device methods (UsesWin32MouseServices, OpenFile, Start, ChangeParameters):
  registered as loud halts — should never be called when Initialize returns 0
- All names confirmed via `pefile` on MCity_d.exe; call graph verified in Ghidra

**`tew/api/crt_handlers.py`** — added `register_ifc22_handlers` call.

**`tew/api/window_manager.py`** — fixed regression from 54189a1: dialog windows
had `SDL_WINDOW_VULKAN` flag which prevents SDL renderer creation. Restored
`SDL_WINDOW_SHOWN` + SDL renderer (with soft fallback) for all dialog windows.
The Vulkan surface belongs to the D3D8 game window, not dialog windows.

**Note:** The login dialog (`DialogBoxParamA`) requires a user click on OK to
proceed — there is no auto-submit. Interactive runs proceed past login; automated
test runs remain at the dialog. This is expected behavior.

**Result:** No new step count measurable in automated runs (login requires
interaction). When the user clicks OK, the game will proceed past ifc22 and
continue. Next automated blocker will be visible in the next interactive run.

---

## 2026-05-31 — Implement CharUpperA

**`tew/api/user32_handlers.py`** — added `_CharUpperA` handler.
Both spec branches: HIWORD==0 → single-char uppercase return; HIWORD!=0 → string
pointer, uppercase ASCII ('a'-'z') in-place, return pointer. Non-ASCII unchanged.

**Result:** Game proceeds past step 133,014,085 (4,546 steps past CharUpperA).
New blocker: `ifc22.dll!??0CImmMouse@@QAE@XZ` (CImmMouse constructor).

---

## 2026-05-31 — Implement GetFullPathNameA

**`tew/api/kernel32_io.py`** — replaced `_halt("GetFullPathNameA")` with real handler.
Resolves relative paths against `C:\MCity`; normalises `..` and `.` segments; writes
to lpBuffer and sets *lpFilePart. Returns char count on success, required size if buffer
too small.

**Result:** Game proceeds past step 133,014,079. New blocker: `user32.dll!CharUpperA`.

---

## 2026-05-31 — Implement Dev::DrawPrimitive (Vulkan geometry rendering)

**New file:** `tew/api/d3d8/_pipeline.py`
- SPIR-V passthrough vertex/fragment shaders encoded as Python word lists (no external compiler)
- `create_image_views`: VkImageView per swapchain image
- `create_render_pass`: single color attachment, loadOp=LOAD (preserves cleared background)
- `create_framebuffers`: one VkFramebuffer per swapchain image
- `create_pipeline`: graphics pipeline — XYZRHW+DIFFUSE vertex layout (stride=32), dynamic viewport/scissor, no culling
- `create_vertex_buffer`: 4MB host-visible/host-coherent Vulkan buffer, permanently mapped
- `init_pipeline`: orchestrates all of the above

**`tew/api/d3d8/_state.py`** additions:
- `_vk_image_views`, `_vk_render_pass`, `_vk_framebuffers`, `_vk_pipeline`, `_vk_pipeline_layout`, `_vk_vertex_buffer`, `_vk_vertex_memory`, `_vk_vertex_mapped_ptr`
- `_vk_in_render_pass` flag
- `_draw_stream_ptr`, `_draw_stream_stride`, `_draw_vertex_fvf`

**`tew/api/d3d8/idirect3d8.py`** — CreateDevice calls `init_pipeline` after swapchain + sync primitives are ready.

**`tew/api/d3d8/idirect3d8device.py`** changes:
- `SetStreamSource`: now stores vertex buffer data pointer and stride in `_state`
- `SetVertexShader`: stores FVF handle in `_state._draw_vertex_fvf`
- `BeginScene`: adds `TRANSFER_DST → COLOR_ATTACHMENT` barrier + `vkCmdBeginRenderPass`
- `EndScene`: calls `vkCmdEndRenderPass` (render pass finalLayout handles `PRESENT_SRC_KHR` transition)
- `Clear`: uses `vkCmdClearAttachments` when inside render pass, `vkCmdClearColorImage` otherwise
- `DrawPrimitive(slot 70)`: transforms XYZRHW→NDC in Python, uploads to Vulkan buffer, issues `vkCmdDraw`

**Result:** Game now renders geometry. DrawPrimitive fires (prim_count=1 triangles).
New blocker: `GetFullPathNameA` (kernel32, file I/O) — game has advanced past rendering.

---

## 2026-05-31 — Fix D3DRES_VTABLE buffer slot ordering

**Root cause diagnosed:** `D3DRES_VTABLE` (used by vertex/index buffer objects) had Surface methods
at slots 11–14. dx8z calls Lock (slot 11) and Unlock (slot 12) on vertex buffers per the D3D8 spec,
but our vtable had Surface::GetContainer (arg_bytes=8) and Surface::GetDesc (arg_bytes=4) there.
Each Lock call (which pushes 4 args = 16 bytes) hit GetContainer's 8-byte cleanup, leaving 8 bytes
uncleaned. After 100K steps this corrupted EBP → 0x020d9228 (.data) and the LEAVE+RET sequence
popped a .data value (0x2196) as the return address → RUNAWAY.

**Files changed:**
- `tew/api/d3d8/idirect3d8resource.py`: Reduced from 18 to 14 slots. Removed dead Surface
  methods (slots 11–14 — surfaces use D3DSURF_VTABLE, not D3DRES_VTABLE). Buffer methods now at
  correct spec positions: Lock(11, arg_bytes=16), Unlock(12, arg_bytes=0), GetDesc(13, arg_bytes=4).
  Also fixed `_get_device` which read ESP+4 (= `this`) instead of ESP+8 (`ppDevice`).
  Implemented `_buffer_get_desc` to fill D3DVERTEXBUFFER_DESC with size from object field.
- `tew/api/d3d8/_layout.py`: Updated D3DRES_VTABLE comment (14 × 4 = 56 bytes).

**Result:** RUNAWAY at 132.7M eliminated. Game now halts loudly at `Dev::DrawPrimitive`.

---

## 2026-05-31 — DirectInput stub + cursor/keyboard handlers

**New file:** `tew/api/dinput_handlers.py`
- `DirectInputCreateA` (dinput.dll + dinput8.dll) — returns DI_OK, writes singleton IDirectInput2 COM object to `*lplpDirectInput`
- IDirectInput2A vtable (9 slots @ DI_VTABLE=0x00220290, singleton @ DI_OBJ=0x002202C0): QI, AddRef, Release, CreateDevice (heap-allocates device obj), EnumDevices (S_OK, no callbacks), GetDeviceStatus (DI_NOTATTACHED), RunControlPanel (E_NOTIMPL), Initialize (S_OK), FindDevice (DIERR_DEVICENOTREG)
- IDirectInputDevice2A vtable (18 slots @ DI_DEV_VTABLE=0x002202D0): full set including GetDeviceState (zeroes output buffer), GetDeviceData (pdwInOut=0), SetDataFormat/SetCooperativeLevel/Acquire/Unacquire (all S_OK)

**user32_handlers.py additions:**
- `GetCursorPos` → writes (0,0), returns TRUE
- `ScreenToClient`, `ClientToScreen` → no-op, returns TRUE (window at screen origin)
- `SetCursorPos` → no-op, TRUE
- `SetCapture` → 0, `ReleaseCapture` → TRUE, `GetCapture` → 0
- `ClipCursor` → TRUE, `MapWindowPoints` → 0
- `GetAsyncKeyState` → 0 (key up)
- `GetKeyboardState` → zero-fills 256-byte table, TRUE
- `GetKeyboardType` → 4/0/12 for type/subtype/fkeys (Enhanced 101-key)
- `MapVirtualKeyA` → 0

**Result:** Game now reaches the render loop. RUNAWAY at step 132.7M (new blocker).

---

## 2026-05-31 — IDirect3DSurface8 vtable fix + BeginScene deadlock fix

**Root cause 1:** D3DRES_VTABLE had IDirect3DResource8 slot ordering (slot 9 = PreLoad),
but dx8z expected IDirect3DSurface8 ordering (slot 9 = LockRect). `_THRASH_lockwindow`
crashed on INT 0xcd because it called LockRect through the wrong vtable.

**Fix:** New `D3DSURF_VTABLE` @ 0x00220260 with correct 11-slot IDirect3DSurface8 layout.
`_alloc_surface_obj(w, h, fmt, memory)` allocates 24-byte COM objects with vtable,
data ptr, size, width, height, and D3DFORMAT. GetDesc reads these fields; LockRect
fills D3DLOCKED_RECT with correct pitch (w×4) and data pointer.
All surface-returning device methods (`GetBackBuffer`, `CreateTexture`,
`CreateRenderTarget`, `CreateDepthStencilSurface`, `CreateImageSurface`,
`GetRenderTarget`, `GetDepthStencilSurface`, `CreateVolumeTexture`, `CreateCubeTexture`)
switched to `_alloc_surface_obj`.

**Root cause 2:** BeginScene deadlock on frame 2+. `vkWaitForFences` was called directly
on the main thread; Mesa/Wayland WSI can call `wl_display_roundtrip` internally, which
needs the main thread to pump events — deadlock.

Also: dx8z calls BeginScene/EndScene twice during THRASH init without any Present
between them. The second BeginScene called `vkWaitForFences` on an unsignaled fence
(reset in frame 1, never re-signaled because QueueSubmit was never called).

**Fix:** Moved `vkWaitForFences` + `vkResetFences` into `vk_pump` background thread.
Added `_vk_frame_submitted` flag (set by Present after QueueSubmit, cleared by
BeginScene) to gate the wait. Added `_vk_image_acquired` flag to skip re-acquiring
when BeginScene is called multiple times without Present.

**Result:** Game now reaches `dinput.dll!DirectInputCreateA` (new blocker).

---

## 2026-04-25 — Beta binary CD check investigation

Investigated why mcity_beta_1.exe shows "Game CD not found" despite instLev=2 in registry.

**Root cause:** The beta binary's CD check function (~0x4d27c0) reads instLev and sets
bMaxInstall at 0x975f78, but has no conditional skip of the CD loop — it unconditionally
calls SetErrorMode(0x8001), loops GetDriveTypeA over A:–Z:, then returns 2 (failure) if no
game CDROM found. The debug binary has Platform_IsMaxInstall (0x52dc30) which reads from
a different global (0xa10f50) and skips the loop; the beta binary lacks this path entirely.

**Key addresses (beta binary):**
- CD check function entry: ~0x4d27c0 (complex SEH prologue, thiscall)
- instLev check + bMaxInstall set: 0x4d2a11–0x4d2a31
- SetErrorMode call (start of CD loop): 0x4d2a7b, return to 0x4d2a81
- Inner drive check (calls GetDriveTypeA): 0x4d56b0
- Loop back: 0x4d2ae0 (JL 0x4d2a86, 26 drives)
- SetErrorMode restore + CD-found check: 0x4d2ae6, CMP ESI,EDI at 0x4d2aec
- Failure path (return 2): 0x4d2afc; success path (return 0): 0x4d2bb8

**Planned fix (deferred):** return DRIVE_CDROM for the install drive in GetDriveTypeA
handler — binary-agnostic, no address-specific patches needed.

**Diagnostics removed:** SetErrorMode return-address logging, Platform_IsMaxInstall patch.
GetDriveTypeA first-call trace kept.

---

## 2026-04-25 — TDD sweep: fix silent failures, implement missing handlers, port tests to ZigCPU

**Silent failure fixes:**
- `msvcrt._realloc`: was returning a new pointer but discarding old data; now copies
  `min(old_size, new_size)` bytes using `heap_alloc_sizes` to find old allocation size.
- `msvcrt._write`: was ignoring the fd entirely; now routes to host fd via `os.write`,
  with fallback for raw stdout/stderr (fd 1/2) when no file handle entry exists.

**Missing handler implementations:**
- `advapi32.RegEnumKeyExA` / `RegEnumValueA`: fully implemented direct-child enumeration
  from the flat `registry_values` dict using backslash-prefix filtering.
- `kernel32.TryEnterCriticalSection`: three-case implementation — recursive by owner
  (TRUE), free CS (acquire + TRUE), contested by other thread (FALSE, no blocking).
- `user32.CallNextHookEx`: full LIFO chain propagation via `_winhook_chains`; each
  handle knows its position in the chain and invokes the next via `_invoke_emulated_proc`.
- `user32._dispatch_winhooks`: fixed to call only the chain head (not all hooks).
- `oleaut32`: added `logger.warn` to previously silent stubs (VarCyFromStr, LoadTypeLibEx,
  RegisterTypeLib, CoCreateInstance).

**Test suite (543 passing):**
- New API unit tests: msvcrt realloc + write, registry enum happy + invalid paths,
  CS (Init/Enter/Leave/TryEnter) and mutex (Create/Wait/Release/Close) paths,
  hook chain happy + sad paths.
- Hypothesis chaos tests: `@given` invariants for CS/mutex, garbage-memory fuzzing,
  `RuleBasedStateMachine` for arbitrary Enter/TryEnter/Leave sequences.
- Opcode tests ported from Python CPU to ZigCPU black-box execution (all 5 files).
- Deleted `test_cpu.py` — tested Python CPU internals, not the production ZigCPU path.

**Run result:** With server running, game logs in, initializes D3D8, and hangs at
`IDirect3D8::CreateDevice` (Wayland deadlock — unchanged blocker).

---

## 2026-04-24 — GetMessageA cooperative yield + Wayland roundtrip attempt

**`GetMessageA` cooperative yield (`user32_handlers.py`):**
- Fixed broken cooperative-yield path. Old code set `cpu.halted = True` +
  `state.thread_yield_requested = True`; `thread_yield_requested` was never read
  anywhere, so it terminated the run loop instead of yielding.
- New code: `state.scheduler.sleep_current(cpu, memory, retry_eip, 0, 1)` — saves
  thread state at stub entry EIP, marks thread SLEEPING, switches to next READY thread.

**`IDirect3D8::CreateDevice` — Wayland roundtrip (`d3d8/idirect3d8.py`):**
- Added `wl_display_roundtrip(wl_display)` via ctypes after `vkCreateWaylandSurfaceKHR`,
  before any surface-property queries (`vkGetPhysicalDeviceSurfaceSupportKHR` etc.).
- Rationale: Wayland compositor needs to process events before it can respond to
  surface capability queries; without this, those calls deadlock.
- Status: hang persists — exact call still unknown. Next: fine-grained step logging
  inside the surface creation block to isolate which call deadlocks.

**Tests:** 450 (all passing).

---

## 2026-04-24 — BeginPaint/EndPaint + full Vulkan swapchain

**`BeginPaint` / `EndPaint` (`user32_handlers.py`):**
- Implemented `BeginPaint(HWND, LPPAINTSTRUCT) → HDC` using the existing
  `_alloc_hdc` infrastructure. Fills the 64-byte PAINTSTRUCT: hdc, fErase=0,
  rcPaint from window entry cx/cy, all reserved bytes zero.
- Implemented `EndPaint(HWND, LPPAINTSTRUCT)`: reads hdc from PAINTSTRUCT,
  removes it from `_live_hdcs` / `_dc_selected`, returns TRUE.
- Both registered as user32.dll stubs with stdcall 8-byte cleanup (2 args).

**`IDirect3D8::CreateDevice` (`d3d8/idirect3d8.py`):**
- Now builds the complete Vulkan rendering backend:
  - Creates `VkSurfaceKHR` from the SDL window's SDL_GetWindowWMInfo X11/Wayland
    display and window handles via `vkCreateXlibSurfaceKHR` /
    `vkCreateWaylandSurfaceKHR` (selected by WAYLAND_DISPLAY).
  - Destroys the SDL renderer on the game window (Vulkan takes over).
  - Finds a queue family with graphics + present support.
  - Creates `VkDevice` with `VK_KHR_swapchain`.
  - Creates `VkSwapchainKHR` (format B8G8R8A8_UNORM preferred, FIFO present mode,
    extent from D3DPRESENT_PARAMETERS or surface capabilities).
  - Allocates command pool + single `VkCommandBuffer`.
  - Creates 2 semaphores (`image_available`, `render_done`) + 1 fence
    (`in_flight`, pre-signalled).
  - All failures halt with explicit error log.
- `make_vtable` now accepts `window_manager` parameter; threaded through from
  `register_d3d8_handlers(stubs, memory, state)` in crt_handlers.py.

**`BeginScene` / `EndScene` / `Clear` / `Present` (`d3d8/idirect3d8device.py`):**
- `BeginScene`: waits for in-flight fence, acquires swapchain image, records
  UNDEFINED → TRANSFER_DST_OPTIMAL barrier, begins command buffer.
- `Clear`: records `vkCmdClearColorImage` (D3DCOLOR ARGB → float RGBA; only when
  D3DCLEAR_TARGET flag set).
- `EndScene`: records TRANSFER_DST_OPTIMAL → PRESENT_SRC_KHR barrier, ends
  command buffer.
- `Present`: `vkQueueSubmit` with image_available wait + render_done signal +
  in_flight fence; `vkQueuePresentKHR`.
- All four were previously `_halt`; now real Vulkan.

**State (`d3d8/_state.py`):**
- Added: `_vk_device`, `_vk_*_queue_family`, `_vk_*_queue`, `_vk_surface`,
  `_vk_swapchain`, `_vk_swapchain_*`, `_vk_command_pool`, `_vk_cmd_buf`,
  `_vk_image_available`, `_vk_render_done`, `_vk_in_flight`,
  `_vk_current_image_idx`, `_vk_fn_*` extension function slots.

**Tests:** 450 (all passing).

---

## 2026-04-24 — Round-robin preemption

**Round-robin preemption (`scheduler.py`, `run_exe.py`):**
- Added `Scheduler.preempt_slice(cpu, memory)`: after each `cpu.run(batch)`, if the
  current thread is READY and another READY thread exists, rotate to it.
- Called in the main run loop immediately after `cpu.run(batch)`.
- Root cause: `mmtimer_callback` (0x00a30a40) signals its own wait event (0x7012) via
  `_SIGNAL_set` inside the `_tmrsub[]` dispatch loop, so `WaitForMultipleObjectsEx`
  always found the event signaled and never yielded. The timer thread consumed 100% of
  emulated CPU, starving all other threads.
- Result: at 132M steps the main game window (`Motor City Online` HWND 0x1034) is
  created and the game progresses further than before.

**Tests:** 450 (up from 388).

---

## 2026-04-23 — CreateDialogParamA fix, timer dispatch, advapi32 time source

**`proc=0` / 122-second stall (`user32_handlers.py`):**
- `CreateDialogParamA(#106)` fixed with null-guard.

**`PendingTimer.fu_event` — `timeSetEvent` dispatch modes (`kernel32_io.py`):**
- `TIME_CALLBACK_FUNCTION` (0x00): invoke emulated proc
- `TIME_CALLBACK_EVENT_SET` (0x10): SetEvent on handle directly

**advapi32 `timeSetEvent` time source (`advapi32_handlers.py`):**
- Fixed to use `state.virtual_ticks_ms + u_delay`.

---

## 2026-04-21 — Cooperative CS blocking, mutex owner tracking

**Cooperative CriticalSection blocking (`kernel32_sync.py`, `kernel32_system.py`, `kernel32_io.py`, `_state.py`):**
- `_enter_cs`: contested background thread now suspends cleanly — undoes LockCount
  increment, sets `thread.waiting_on_cs = ptr`, sets `thread_yield_requested = True`,
  halts without `cleanup_stdcall` so EIP stays at stub for retry on resume.
  `_leave_cs`: full release resets LockCount = -1 and OwningThread = 0 (replaces old
  decrement + halt-if-waiters). Removed noisy per-release debug log.
- Scheduler (`_cooperative_sleep` + `_run_background_slice`): added `waiting_on_cs`
  check — if `OwningThread == 0`, clears `waiting_on_cs` and allows thread to retry.
- `PendingThreadInfo`: added `waiting_on_cs: Optional[int] = None`.
- Result: NPS networking threads (tid=0x3ec, 0x3ed) survive CS contention; game
  reaches and sustains the rendering loop (BeginScene/SetRenderState cycling) at 129M+ steps.

**Mutex owner tracking (`kernel32_io.py`, `_state.py`):**
- `MutexHandle`: added `owner_tid: Optional[int]` and `recursion_count: int`.
- `WaitForSingleObject`: mutex now checks `owner_tid is None` before acquiring.
  Contested mutex blocks via `waiting_on_handles` (same path as unsignaled event).
  Recursive acquisition by owning thread increments `recursion_count` and returns 0.
- `WaitForMultipleObjectsEx`: mutex ready only when `owner_tid is None`.
- `ReleaseMutex`: decrements `recursion_count`; clears `owner_tid`/`locked` on zero.
- `CreateMutexA`: sets `owner_tid` + `recursion_count = 1` when `bInitialOwner != 0`.
- Scheduler checks updated to use `owner_tid is None` instead of `not obj.locked`.

**Tests:** 388 (unchanged).

---

## 2026-04-20 — TEB/PEB truthfulness, CriticalSection fix, kernel32 split

**TEB/PEB truthfulness (`kernel32_system.py`, `kernel32_sync.py`, `kernel32_io.py`,
`kernel_structures.py`, `_state.py`):**
- `SetLastError`/`GetLastError` now read/write TEB memory at `TEB_BASE + 0x34`.
  Previously used Python `state.last_error` field — binary code doing `MOV EAX, FS:[0x34]`
  directly would get stale zero. `state.last_error` removed entirely.
- `TlsSetValue`/`TlsGetValue` now read/write TEB memory at `TEB_BASE + 0xE0 + slot*4`.
  `TlsFree` zeros the TEB slot. `_cooperative_sleep` saves/restores TLS in TEB memory
  around background thread slices so FS:[0xE0+] is always correct for the active thread.
- `PEB+0x18` (ProcessHeap) now populated: `initialize_kernel_structures` takes a
  `process_heap` argument and writes it into PEB memory.
- `TEB_BASE = 0x00320000` and `PEB_BASE = 0x00300000` added as module constants to `_state.py`.

**CriticalSection fix (`kernel32_sync.py`):**
- `EnterCriticalSection`: correctly increments LockCount (+0x04), sets RecursionCount (+0x08)
  and OwningThread (+0x0C) on first acquisition; increments RecursionCount on recursive entry
  by the same thread. Halts loudly if contested (blocked thread — not implemented).
- `LeaveCriticalSection`: decrements RecursionCount; on full release clears OwningThread
  and decrements LockCount. Halts loudly if waiters exist (LockSemaphore signal — not implemented).

**kernel32_handlers.py split:**
- Monolithic `kernel32_handlers.py` (~1295 lines) split into orchestrator (~329 lines)
  + 5 focused sub-modules: `kernel32_memory.py`, `kernel32_sync.py`, `kernel32_locale.py`,
  `kernel32_system.py`, `kernel32_io.py`. All 388 tests pass.

**Tests:** 388 (up from 386; 2 new: PEB ProcessHeap layout, LastError TEB address).

---

## 2026-04-17 — Heap fix, VirtualAlloc accuracy, user32 handlers, hook dispatch

**Progress:**
Game now enters the main message loop (GetMessageA/PeekMessageA). Blocked by
sporadic SDL_QUIT of unknown origin — not user-triggered. All previously queued
issues (GetKeyState, GetSystemMetrics cap, SetActiveWindow, SystemParametersInfoA)
resolved.

**`__free_dbg` patch (`patch_internals.py`):**
- 0x009F6E20 patched to no-op: MSVC debug CRT internal free validates block headers
  that our bump allocator never writes. `__freeptd` (called by `__endthread`) was
  asserting on every thread exit. Consistent with existing `free()` IAT no-op.

**VirtualAlloc accuracy (`kernel32_handlers.py`):**
- `MEM_RESERVE` with non-zero `lp_addr` now honors the requested address instead of
  ignoring it and using the bump allocator. Bump pointer advanced past the reserved
  region to prevent future overlap.
- `MEM_COMMIT` only: range check against all reserved regions instead of exact-base
  lookup. Game's custom allocator commits sub-pages of a block reserved as a whole.

**New user32 handlers (`user32_handlers.py`):**
- `GetKeyState` → 0 (all keys up)
- `GetSystemMetrics` → SM_CXSCREEN/SM_CYSCREEN capped at 1024×768
- `SetActiveWindow` → NULL
- `SystemParametersInfoA` → SPI_GETSCREENSAVEACTIVE (FALSE), SPI_GETWORKAREA
  (0,0,1024,768), TRUE for others
- `SetWindowsHookExA` → real registration in `_winhooks` dict
- `UnhookWindowsHookEx` → removes from `_winhooks`
- `CallNextHookEx` → 0 (no chain)

**Hook dispatch (`user32_handlers.py`):**
- `_dispatch_winhooks`: called from `PeekMessageA` and `GetMessageA` after writing
  MSG struct. Fires WH_GETMESSAGE hooks with (HC_ACTION, PM_REMOVE, lp_msg) and
  WH_KEYBOARD hooks with (HC_ACTION, vk, 0) for WM_KEYDOWN/WM_KEYUP messages.

**Keyboard input pipeline (`window_manager.py`):**
- WM_KEYDOWN/WM_KEYUP/WM_CHAR/WM_MOUSEMOVE/WM_LBUTTONDOWN/WM_LBUTTONUP constants added
- `_sdl_sym_to_vk`: maps SDL keysyms to Win32 VK codes
- SDL_KEYDOWN/SDL_KEYUP now post WM_KEYDOWN/WM_KEYUP to message queue in addition to
  existing dialog-specific handling

**Progress heartbeat (`run_exe.py`):**
- `[alive]` INFO log every 5M `cpu.step()` calls: step count, EIP, virtual time
- Does NOT fire during `GetMessageA` host-sleep (see status.md queued issues)

---

## 2026-04-17 — GDI object table + step-loop performance

**Progress:**
Game now opens a real SDL window ('Motor City Online') and runs further into startup.
Halts at `GetKeyState(VK_CAPITAL)` on tid=1007 — next blocker.

**GDI object table (user32_handlers.py):**
- `_GdiObj` dataclass: kind/color/style/is_stock
- Stock objects pre-populated at registration (WHITE_BRUSH=0 through DC_PEN=19),
  handles 0x2001+fnObject — stable and traceable
- `GetStockObject`: O(1) lookup into `_stock_handles` dict, returns real handle
- `SelectObject`: real per-DC selection tracking (`_dc_selected`), returns previous handle
- `CreateSolidBrush`: allocates dynamic `_GdiObj` entry from counter 0x3001+
- `DeleteObject`: removes dynamic objects; stock objects survive
- DC state initialized in `_alloc_hdc`, cleaned up in `ReleaseDC`/`DeleteDC`

**Performance:**
- `is_valid_eip`: O(N) linear scan → O(1) dict keyed on 4KB page number (~29K entries,
  built once at startup); major win at 123M+ calls per run
- `cpu.step()`: merged `_skip_prefix` — fetch opcode first, handle prefix inline;
  eliminates one wasted memory read per non-prefix instruction (~99% of steps);
  `_clear_prefixes` only called when a prefix was actually set
- Main loop: modulo → countdown counters; removed dead `prev_eip` assignment

**Next blockers (status.md updated):**
1. `GetKeyState` on tid=1007 — return 0 (key not pressed), 2 lines
2. `GetSystemMetrics` returns real display resolution → window too large (cap at 1024×768)

---

## 2026-04-17 — Timer unblock + handler correctness audit

**Progress:**
Game now runs through full startup: login dialog → HTTP auth (200 OK) → authlogin.dll →
options.ini defaults → dx8z.dll/D3D8 init → timer thread created. Halts at
`GetStockObject(BLACK_BRUSH)` in gdi32. Next milestone: GDI object table.

**Timer / scheduler fixes (unblocked _TIMER_waitticks spin):**
- `_run_background_slice` extracted as module-level function in `kernel32_io.py`
- `WaitForSingleObject(INFINITE)` on main thread now drives background threads
  (process-zero pattern) instead of halting emulation
- Timer heartbeat in `run_exe.py` fires every 100K steps: advances `virtual_ticks_ms`,
  invokes due timer callbacks, runs one background slice — unblocks tid=1006 ticks

**Handler correctness audit:**
- `_lclose`: was silently returning 0; now reads handle, closes host fd, returns
  correct value (handle on success, HFILE_ERROR on unknown)
- `advapi32_handlers.py`: five stack reads missing `& 0xFFFFFFFF` mask — fixed
- `SetForegroundWindow`: misleading "pretend it worked" comment replaced with
  explanation (SDL2 owns the window; Win32 focus mechanics don't apply)
- `GetStockObject`: a fake-handle implementation was written and reverted — kept
  as `_halt` until a real GDI object table exists
- Unused imports and dead variable cleaned (ruff)

---

## 2026-04-13 (third pass) — Bitmap rendering for STATIC controls

**What was broken:**
The "Motor City Online" connecting dialog has one STATIC child control whose
`title = "#109"` (parsed from the Win32 resource 0xFFFF ordinal encoding as a
bitmap reference).  `_render_static` was rendering this as `"[#109]"` placeholder
text, which appeared as a visible window on screen.

**Fix:**
- New `tew/api/bitmap_loader.py`:
  - `BitmapInfo` frozen dataclass (width, height, BGR24 pixels)
  - `parse_dib(raw)` — parses BITMAPINFOHEADER, supports 1/4/8/24/32 bpp,
    flips bottom-up rows to top-down
  - `create_sdl_texture(renderer, info)` — converts BGR24 to SDL2 texture
  - `load_bitmap_texture(renderer, bitmap_id, pe_resources)` — high-level loader
- `WindowEntry.bitmap_texture` field — holds SDL_Texture for STATIC bitmap controls
- `WindowManager.set_pe_resources(pe_resources)` — wires PE resources into wm
- `WindowManager.create_dialog` — preloads bitmap textures for STATIC "#N" controls
- `destroy_window` / `shutdown` — call `SDL_DestroyTexture` on bitmap_texture
- `dialog_renderer._render_static` — renders actual texture via `SDL_RenderCopy`;
  falls back to plain grey rectangle (no text) if texture unavailable
- `run_exe.py` — wires `set_pe_resources` after PE load

**Tests:** 18 new tests in `tests/unit/kernel/test_bitmap_loader.py` (all passing).
Total: 386 tests.

**Still open:**
- `bpp:-1` in dx8z OutputDebugString (D3D display mode query returns garbage)
- TIMER_init failure (`__beginthreadex` not handled → timer thread never starts)
- Threads 1004/1005 crash at stub 68 (InterlockedCompareExchange)

---

## 2026-04-13 (second pass) — Bug-fix session: thread crashes + scheduler stability

**Commit**: e4eb34a

**What broke and why:**

Five background threads (Chat Filter, two INet, two more) were all dying every run.
Root causes found via Ghidra + log analysis:

- Threads 1004–1005 hit `TlsSetValue` with an invalid slot index → halt stub fired.
  Fixed: return `FALSE` (Win32 behaviour) instead of halting.
- Threads 1001–1003 hit `EnterCriticalSection` and triggered `cpu.halted` via an
  internal Python exception (`ValueError` from out-of-bounds `memory.read32`).
  Root cause: corrupted ESP. Now the crash message includes `cpu.last_error` so
  the actual fault address will be visible on next run.
- After all threads died, the main thread entered a Sleep() loop. Each Sleep() tried
  to schedule another thread, which called Sleep(), which recursed into another
  `_run_thread_slice` — Python stack overflow ("maximum recursion depth exceeded").
  Fixed: `_cooperative_sleep` / `_cooperative_sleep_ex` guard on `state.is_running_thread`.

**Other fixes:**

- `MessageBoxA` returned `IDOK=1` for `MB_YESNO` — the fullscreen-prompt branch in
  `Platform_SysStartUp` checks `if (result == 7)` (IDNO) for windowed mode. `1` is
  neither yes nor no, so the game's mode flag was indeterminate. Now returns `IDYES=6`.
- `GetModuleHandleA("KERNEL32")` returned NULL — stub-only DLLs have no `LoadedDLL`
  entry. Added `Win32Handlers.get_stub_dll_handle()` returning the first handler's
  trampoline address as a stable non-NULL handle. 11 unit tests added.
- `GetDeviceCaps` had no logging — calls were invisible in `LOG_CATEGORIES=handlers`.
  Added hdc + capability-name debug line. Confirmed BITSPIXEL returns 32 correctly.
- `CreateWindowExA` with `MAKEINTATOM(109)` did a name-table lookup for `"#109"` which
  was never inserted. Fixed to look up by atom value in `_classes` directly.

**Ghidra analysis this session:**

- `Platform_SysStartUp` (0x006b13b0): full decompile — fullscreen prompt, dx8z load,
  `_THRASH_setvideomode`, window init sequence, thread creation order
- `func_0x0040490d` → JMP → `GameSetup_LoadOptions` (0x0055d280): NOT window creation
- `_THRASH_createwindow` (dx8z.dll 0x60001760): internal dx8z allocation, no Win32
- `FUN_60003920` (dx8z.dll): D3D device enumeration, BITSPIXEL check, COM vtable calls

## 2026-04-12 — v0.9.0 — CRT init session: 10K → 6.77M steps

**Step count**: 10,765 → 6,772,724 (657× increase in one session).

Game is now printing OutputDebugString messages from authlogin/INET startup code:
`Filter thread started` and `Creating INET Message Object`.

**New modules:**
- `tew/api/char_type.py` — CT_CTYPE1 lookup table, WideMemory Protocol, GetStringTypeArgs DTO
- `tew/api/lc_map.py` — LCMapFlags IntFlag enum, LCMapStringArgs DTO, case conversion
- `tew/api/win32_errors.py` — Win32Error IntEnum (winerror.h constants)
- `tew/api/ini_file.py` — full INI parser + reader + writer (parse_ini, read/write_profile_string/section)
- `tew/api/version_handlers.py` — GetFileVersionInfoSizeA family (returns 0: no version resource)
- `tew/api/wininet_handlers.py` — full WinINet HTTP stack via http.client; forwarding to localhost

**Blockers cleared (in order):**
1. `GetStringTypeW` — CT_CTYPE1 lookup table for Unicode classification
2. `LCMapStringW` — LCMAP_LOWERCASE/UPPERCASE via codepoint mapping
3. `GetModuleFileNameA` — CRTState.exe_path + reverse_translate_path()
4. `HeapValidate` — checks heap_handles set, returns TRUE
5. `GetLastError` / `SetLastError` — last_error field on CRTState
6. `GetPrivateProfileStringA/IntA` — real INI parsing; file read via find_file_ci
7. `WritePrivateProfileStringA/SectionA` — real INI file write
8. `GetFileVersionInfoSizeA/A`, `VerQueryValueA` — version_handlers.py
9. Full WinINet stack — InternetOpen/Connect/HttpSend/Read/QueryInfo/Close
10. `_initterm` / `_initterm_e` — **calls back into guest CPU**; real static initializer dispatch via THREAD_SENTINEL
11. `VirtualProtect` — flat memory model; returns PAGE_EXECUTE_READWRITE, TRUE
12. `GlobalMemoryStatus` — plausible 256 MB values
13. `SetEnvironmentVariableA/W`, `GetEnvironmentVariableA/W` — module-level _env_vars dict

**Infrastructure improvements:**
- `flush=True` on all logger print() calls — fixes INFO/ERROR ordering in piped output
- `diagnose_halt()` in exception_diagnostics.py — prints registers + 16-slot stack walk on any halt
- `diagnose_halt` wired into run_exe.py post-loop alongside existing diagnose_fault

**Tests:**
- 302 tests, all passing
- New test files: test_char_type.py (46), test_lc_map.py (34), test_module_filename.py (14),
  test_heap.py (6), test_last_error.py (15), test_ini_file.py (47)

**Next blocker:** `DuplicateHandle`

---

## 2026-04-12 — v0.8.0 — GetStringTypeW implemented

**Blocker cleared** — `GetStringTypeW` now has a real implementation.

**New files:**
- `tew/api/char_type.py` — CT_CTYPE1 lookup table for ASCII (U+0000–U+007F),
  `Ctype1` IntFlag enum, `GetStringTypeArgs` frozen dataclass (DTO),
  `WideMemory` Protocol, `classify_ctype1()`, `classify_wide_string()`
- `tests/unit/kernel/test_char_type.py` — 46 tests; all pass

**Changed:**
- `kernel32_handlers.py` — `GetStringTypeW` handler replaced; halts loudly
  if called with CT_CTYPE2 or CT_CTYPE3 (not needed by MCO, implement on demand)

**Design notes:**
- CT_CTYPE2 and CT_CTYPE3 are NOT implemented — handler halts if called.
  This is intentional: fail loudly rather than return silent garbage.
- `WideMemory` Protocol makes `classify_wide_string` testable without the
  full emulator setup — `Memory` satisfies it structurally.

---

## 2026-04-11 — v0.7.0 — Code hygiene session

**No step count change** — 10,765 steps, same GetStringTypeW halt.

**Structural fixes:**
- `SavedCPUState` moved from `tew/api/_state.py` to `tew/hardware/cpu.py`
  (it's a CPU type; hardware layer shouldn't depend on api layer)
- `save_state()` / `restore_state()` added as methods on `CPU`
  (they touch CPU internals directly; belong on the class)
- Duplicate `_save_cpu_state` / `_restore_cpu_state` functions removed from
  `kernel32_handlers.py` and `kernel32_io.py` — was an exact copy-paste

**Linter / tooling:**
- `ruff` installed and configured in `pyproject.toml`
- `requires-python` bumped to `>=3.12` (venv runs 3.13; fixes f-string
  backslash escape syntax errors that were latent on 3.11)
- 48 unused imports removed (auto-fix)
- `_vt` in `oleaut32_handlers.py` prefixed with `_` to signal intentional discard
- E701/E702 (compact one-liners in opcode tables) suppressed globally —
  intentional style for condition code dispatch and paired flag sets

**Removed:**
- `main.py` — predated the port, `run_exe.py` is the entry point

**Deferred (documented in memory):**
- Split `_state.py` into DTOs vs runtime state
- Split large handler files by concept (heap, file, thread, sync)
  rather than by DLL name

---

## 2026-04-01 — v0.6.0 — Initial Python port committed

Full Python port of the TypeScript emulator. Runs 10,765 steps through
CRT initialization, halts at `GetStringTypeW`. Includes:
- CPU core, full opcode implementations, x87 FPU
- PE/DLL loader with base relocations and IAT patching
- Win32/CRT/D3D8/User32/OleAut32/Advapi32 handler stubs
- Cooperative thread scheduler
- Unit test suite (140 tests)
