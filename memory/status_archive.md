# Emulator Session Status — Archive

Rotated-out `## Previous status` entries from `status.md`, oldest history preserved verbatim (not summarized) since some entries have detail not duplicated in `changelog.md`. Newest-first, same as before. `status.md` itself now holds only the single most-recent `## Current status` section — this file is the full backlog behind it. Rotated 2026-08-16 (file had grown to 1756 lines); grep here for anything not found in `changelog.md` or the live `status.md`.

---

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
