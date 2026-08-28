# TODO

Durable follow-up work, checked/updated as picked up. Distinct from status.md
(current session's active blocker) and changelog.md (completed work) --
items here are queued but not yet started, or started and paused.

---

## UPDATED (2026-08-28, cont'd x38): DAO/Jet query-parameter gap -- `StockAssembly_SelectAPT` "could not get param count" -- traced deep into real Jet SQL-compiler internals, still open

Full call chain now confirmed live via probes correlated against the exact
failing call (see status.md "cont'd x38" for the complete trace with
addresses): `DBParamQuery::get_Count` -> `dao350.dll`'s real `get_Count`
implementer (`FUN_0447dc1c`) -> type-indexed refresh gate (`FUN_044d26ce`,
Parameters type) -> per-type populate handler (`FUN_044c69bc`, its own
allocation confirmed to succeed) -> name-based lookup (`FUN_044d525b`,
given `"StockAssembly_SelectAPT"` directly) -> dynamically-bound call into
real `msjet35.dll` (confirmed: two earlier calls through the identical
pointer succeeded for other queries this same run, ruling out a structural
code-path bug) -> real msjet35.dll dispatch (`FUN_7a89ff40` ->
`FUN_7a89fd45`) -> the real Jet SQL execution-plan compiler
(`FUN_7a862215`, same `JETSHOWPLAN` code path the earlier
`StockVehicleAttributes_SelectAll2`/`Fields.Count` investigation also
reached) -> raw internal error `-3100`, translated into DAO error 3075.

Ruled out along the way (all confirmed live, not guesses): the pool
allocator returning NULL (false lead, itself caused by the logpoint-cap
bug below); the `CreateErrorInfo`/`SetErrorInfo`/`GetErrorInfo` plumbing
(already implemented/working from an earlier session -- `Error.Description`
is a real, validly-allocated, genuinely-zero-length BSTR, not a plumbing
gap); two real, independently-fixed bugs found while investigating
(`GetEnvironmentStrings(W)`'s garbage pointer, `DllMain`-before-IAT-patch
ordering, `WriteFile`/`_write`'s wrong-fd-position bug) -- none of them
change the outcome.

**Not yet located**: `FUN_7a862215`'s return traces to
`local_44 = FUN_7a85e7e1(local_18, local_1c, local_14[0x1f])`, not yet
live-probed. Genuine, deep, undocumented Jet SQL-compiler internals.
`StockAssembly_SelectAPT` never appears as its own top-level plan in
`showplan.out`, consistent with compilation failing before a plan gets
written. Same shape as the still-unresolved Fields.Count investigation's
own conclusion (multi-table `Table.Column`-qualified-reference tokenization,
upstream of where either investigation has reached) -- may be the same
underlying compile-time defect manifesting on parameters instead of
columns; not confirmed.

## NEW (2026-08-28, cont'd x38): `cpu_add_logpoint` silently drops registrations past its 8-slot cap -- violates this project's own fail-loudly standard

`cpu/src/core.zig`: `lp_eip: [8]u32`/`lp_cb: [8]?LogpointFn`, fixed-size
arrays in the FFI-shared `CpuState` struct (same pattern as the breakpoint
table, `bp_table: [8]u32` -- not derived from any real hardware limit,
just a round number picked when this was built). `kernel.zig`'s
`cpu_add_logpoint` loops the 8 slots looking for an empty one and just
`return`s with no signal at all if none is free -- the new registration is
silently discarded, and the caller has no way to know. Bit this
investigation live 2026-08-28: 9-10 active logpoints (several stale, from
already-resolved earlier investigations) meant two newly-added probes
never fired, producing a real false lead (see the DAO/Jet entry above)
before the cap was noticed and probes were pruned. Not yet fixed --
`cpu_add_logpoint` should return a bool (or otherwise signal) on failure,
and the Python `add_logpoint` wrapper (`cpu_zig.py`) should raise/log
loudly when registration fails, matching this project's own "fail loudly
or not at all" standard. Deferred by Molly ("stay on the trace, come back
to it after") -- pick this up next.

## NEW (2026-08-28): `WaitForMultipleObjects(Ex)`'s `bAlertable` param is a no-op -- fine today, must be wired in if APCs ever get modeled

`_wait_for_multiple_common` (`kernel32_io.py`) reads `WaitForMultipleObjectsEx`'s
trailing `bAlertable` arg but never uses it -- a wait can never be interrupted
early by a pending APC. Verified this is currently harmless, not just
unimplemented: `QueueUserAPC`, `ReadFileEx`, and `WriteFileEx` (the only three
real Win32 APIs that can ever queue an APC to a thread) are not implemented
anywhere in `tew/api/*.py` -- with no APC source, there is no pending-APC
state `bAlertable` could ever act on. If any of those three are implemented
later, `bAlertable` (and the plain, non-Ex `SleepEx`'s alertable semantics --
same gap, same cause) need to be wired in at the same time, or an alertable
wait/sleep will silently never wake early for a queued APC.

## NEW (2026-08-26): `THREAD_SENTINEL` collision between `_call_guest_void` (static initializers) and real thread completion -- currently harmless, likely to bite later

`_call_guest_void` (`msvcrt_handlers.py:272`, used by `_initterm` to invoke a
DLL's C++ static initializers) pushes `THREAD_SENTINEL` (`0x001FE000`) as its
own inner-call return address and steps until it returns there. But
`THREAD_SENTINEL` has a real `INT 0xFE` trampoline permanently wired to
`_make_thread_return_handler` (`crt_handlers.py`) -- the same handler used
for a real spawned thread's natural completion, which calls
`scheduler.mark_current_dead()` unconditionally, with no way to tell "an
initializer just returned" apart from "this thread just died".

`_call_guest_void`'s own docstring documents the precondition this
violates: "this helper is only called from main-thread context ... so
hitting the sentinel does not corrupt cooperative-thread bookkeeping." That
was true until 2026-08-26's DllMain-for-static-imports fix (see changelog
x34/x35): `oleaut32.dll`'s real `DllMain` has a genuine `_initterm` static
initializer, and calling it now happens *nested inside* an in-flight
`_invoke_emulated_proc` call (tracking `OLEAUT32.dll`'s own `DllMain` as
`started_thread_idx`). The initializer returning to `THREAD_SENTINEL`
spuriously marks that thread dead mid-call
(`/tmp/emu.log`: "Thread 1000 returned normally" immediately followed by
"[_invoke_emulated_proc] thread idx=0 ... has died"). This run happened to
still produce the correct result (`_invoke_emulated_proc`'s
`genuinely_completed` check apparently still passed despite the spurious
death flag) -- not verified whether that holds in general, or just got
lucky this once.

Not yet fixed -- flagging so it doesn't get rediscovered from scratch next
time a DLL's `DllMain` (real or via `_ensure_dll_ready`) has static
initializers AND is invoked through `_invoke_emulated_proc`'s nested-call
path. Fix would need `_call_guest_void` to use its own dedicated
sentinel/return address (like `_invoke_emulated_proc`'s own
`_get_dialog_sentinel`-allocated one) instead of sharing `THREAD_SENTINEL`
with real thread completion.

## RESOLVED (2026-08-26): 101 `test_oleaut32_*.py` unit tests and dead `oleaut32_handlers.py` cleaned up

Deleted the 7 obsolete `tests/unit/api/test_oleaut32_*.py` unit test files that tested Python stubs now handled by real `oleaut32.dll`. Removed all dead Python `oleaut32.dll` stubs and trap objects (~1,100 lines), removed the temporary `_NoOleaut32Stubs` shim, migrated active `ole32.dll` COM handlers to `tew/api/ole32_handlers.py` (`register_ole32_handlers`), and updated callers in `crt_handlers.py` and `test_ole32_com.py`.

## RESOLVED (2026-08-26): statically-imported DLLs' `DllMain` now runs; original DAO license-key BSTR bug confirmed fixed

Fixed `build_iat_map`/`run_exe.py` so `d3d8.dll`/`oleaut32.dll`/`rpcrt4.dll`/
`secur32.dll` (MCity_d.exe's own direct imports) actually run their real
`DllMain(DLL_PROCESS_ATTACH)` now, matching real Windows loader ordering.
Working through the resulting wave of newly-exercised missing handlers
(GetSystemTimeAsFileTime, LoadLibraryExW, InitializeSListHead, CreateEventW,
several ntdll.dll Rtl* primitives, wsprintfA, RegisterClipboardFormatA,
GetSystemDirectoryA, CoSetState) confirmed the original bug fixed
end-to-end: `SysAllocString` now returns a real BSTR, and the game runs
real single-race gameplay DB traffic instead of halting on
`Database initialization failed!`. Full writeup: status.md "cont'd x35",
status_archive.md "cont'd x34" for the root-cause trace.

## RESOLVED (2026-08-26): `kernel32.dll!SearchPathA` and `SearchPathW` implemented

Implemented standard Win32 file search sequence and custom path search for `SearchPathA` and `SearchPathW` (`kernel32_io.py`). Live run confirmed `SearchPathW("expsrv.dll")` resolves cleanly to `C:\WINDOWS\SYSTEM32\expsrv.dll`.

New blocker opened immediately downstream: `msvcrt.dll!wcsncpy`, called by `OLEAUT32.dll` at ~61.3s to copy the found typelib/DLL path.

## OBSOLETE (2026-08-26): real `.tlb` type-library parsing for `LoadTypeLibEx`

**Superseded, do not pick this up** -- the entire premise was wrong. `LoadTypeLibEx`
never actually needed a hand-built parser: `oleaut32.dll` genuinely loads as
real code in this emulator, but was being unconditionally shadowed by
`oleaut32_handlers.py`'s own registered Python handlers (`dll_loader.py`'s
`patch_iat_entry` tries a registered handler before ever checking a real
DLL's export). Fixed 2026-08-26 by dropping every `"oleaut32.dll"`
registration that file makes -- real `oleaut32.dll` now parses `expsrv.dll`'s
real, embedded `TYPELIB` PE resource itself and answers `Bind`/`GetDllEntry`/
`GetFuncDesc` correctly and automatically. See changelog.md 2026-08-26.

**Follow-up cleanup (RESOLVED 2026-08-26)**: dead `oleaut32_handlers.py` code removed and active `ole32.dll` handlers moved to `ole32_handlers.py`.

## RESOLVED (2026-08-27/28, cont'd x36/x37): "Database initialization failed!" cleared -- DB init now runs for real

Was caused by the chain of missing handlers fixed across x36/x37 (`_llseek`/`_lread`,
`LoadLibraryA` stub-DLL fallback, `RegNotifyChangeKeyValue`, `WaitForMultipleObjects`,
`GetStringTypeExW`, `wcsncmp`, etc.) -- DB init itself now completes and the game reaches
real query execution. New, deeper blocker opened immediately downstream: see the
`StockAssembly_SelectAPT` DAO/Jet query-parameter entry above.
