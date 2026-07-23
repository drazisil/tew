# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here.*
---

### Current status (2026-07-22): `IsDBCSLeadByte` implemented (codepage-
derived, not a hardcoded constant) — DAO's `DllMain` now genuinely completes
(`DllMain(DLL_PROCESS_ATTACH) -> 1`, correct per the real decompiled code)
and the run reaches `DllGetClassObject` for real. The previously-diagnosed
`*ppv` stays-NULL mystery (see "Background" below) is now **fully
root-caused**.

**Root cause**: `CoGetMalloc` (`ole32.dll`) has no handler registered in
tew at all. Traced live via targeted logpoints at `dao350.dll`'s real
addresses (`FUN_0447d31e`, the `DllGetClassObject` helper-object allocator;
`FUN_044947fc`, its per-thread init routine): `FUN_044947fc` calls
`CoGetMalloc` as its very first real dependency and gets an
`[UNIMPLEMENTED] ole32.dll!CoGetMalloc — halting` fatal halt immediately —
long before ever reaching `TlsSetValue` or the "already initialized"
shortcut check that earlier static analysis (correctly, in hindsight) had
flagged as the interesting branch. That's why none of the four existing
`DllGetClassObject`-internals logpoints (`_log_dgco_call_queryinterface`,
`_log_dgco_call_release`, `_log_qi_ppv_write`) ever fired: the whole
per-thread-arena chain aborts at the first dependency, `FUN_0447d31e`
returns NULL, and `DllGetClassObject` takes its `local_c == NULL` branch,
skipping `QueryInterface`/`*ppv` entirely.

**Second, connected bug found in the same investigation**: `hr=0x00000000`
in the `CoGetClassObject(...) -> hr=0x00000000 *ppv=0x00000000` log line is
*not* the real `DllGetClassObject` returning `S_OK`. `_invoke_emulated_proc`
returns a bare `0` whenever a nested call doesn't genuinely complete (a
fatal halt, a dead thread, `max_steps` exhausted) — a safe sentinel for
`DllMain`-style callers where `0` means FALSE, but `_call_dll_get_class_object`
(`oleaut32_handlers.py`) treats that same `0` as the return `HRESULT`, where
`0` *is* `S_OK`. So an aborted call is structurally indistinguishable from a
genuine success to any `HRESULT`-returning nested-call site, not just this
one. Not yet fixed -- needs its own decision (a distinguishable sentinel
value, or having `_invoke_emulated_proc` signal "didn't complete" out of
band rather than through the return value).

### Current status (2026-07-23): `cpu.fatal_halt` is now a real, unclearable
native CPU lockup -- fully fixed, live-verified. Root cause (found while
chasing why execution continued past the `CoGetMalloc` fatal halt to a
later, unrelated halt): `ZigCPU.faulted`'s setter (`tew/hardware/cpu_zig.py`)
called the native `cpu_clear_halted` unconditionally whenever `cpu.faulted`
was cleared (the SEH-resume path does this after deciding a fault was
"handled") -- `cpu_clear_halted` (`cpu/src/cpu.zig`) cleared native `s.halted`
regardless of `fatal_halt`, desyncing it from the Python-side sticky flags
that every other check in the codebase reads. `cpu_run`'s own per-instruction
loop obeys native `s.halted`, not the Python property, so later `cpu.run()`
calls (notably `_invoke_emulated_proc`'s polling loop) genuinely executed
more real instructions after what was supposed to be a permanent stop.

**Fixed at the CPU/Zig layer, not Python orchestration** -- deliberate,
discussed and agreed choice: `fatal_halt` has no real x86 analog (there's no
hardware concept of "an unimplemented Win32 API"), but this emulator models
exactly one physical core, so once it fires, nothing should be able to hand
the core to a different thread as if it were merely idling -- a genuine
single-core lockup, not something Python-level scheduling can be trusted to
enforce (scattered `if not cpu.fatal_halt:` checks before every halt-clearing
call site is exactly the pattern that let this bug through in the first
place). Added a new native `fatal_halted` field on `CpuState`
(`cpu/src/core.zig`), a dedicated `cpu_set_fatal_halt`/`cpu_is_fatal_halted`
pair with no clear path, made `cpu_clear_halted` and `cpu_run`'s loop respect
it, and made every register/eflags/FPU setter refuse to write once set
(reads stay fully open) -- this also neutralizes `_invoke_emulated_proc`'s
cleanup path, which unconditionally calls `cpu.restore_state(saved)` even
when fatally halted; that write is now silently a no-op instead of clobbering
the real failure-point state before any diagnostic sees it. `tew/kernel/
scheduler.py`'s `preempt_slice` and the four other CPU-state-mutating entry
points (`block_current_on_cs`/`block_current_on_handles`/`sleep_current`/
`mark_current_dead`) now refuse to act once fatally halted too, for the same
reason, though none were concretely reachable post-fatal-halt once the
native fix landed.

Live-verified against the exact `CoGetMalloc` scenario: the run now stops
dead at `[UNIMPLEMENTED] ole32.dll!CoGetMalloc — halting` with zero further
scheduler activity, and the Halt Diagnostic shows the full, accurate 7-frame
call chain at the true failure point (`DllGetClassObject` → our own
`_invoke_emulated_proc` sentinel → `Dbcode_InitDao` → `DBThreadCpp` → thread
wrapper → `THREAD_SENTINEL`) instead of a shallow, unrelated, later trace.
New tests (`tests/unit/hardware/test_cpu_zig_fatal_halt.py`,
`TestPreemptSlice` in `test_scheduler.py`) confirmed to fail against the
pre-fix build and pass against the fix. 589/589 tests passing.

**Side investigation, deferred, not yet fixed**: while figuring out what
"real CPU" `fatal_halt`/`HLT` should match, found `cpu/src/two_byte.zig`'s
`CPUID` handler doesn't self-consistently identify as any real chip (current
`EAX=0x00000600` doesn't match Pentium Pro, Pentium II, or Pentium II
OverDrive's real documented signatures). Since MMX is a hard requirement
(tew's `cpu/src/mmx.zig` opcodes are load-bearing) and Pentium Pro's own
feature table has no MMX bit at all, the correct target is real Pentium II
(Family 6, Model 3 "Klamath" or Model 5 "Deschutes" -- identical feature
sets) — corrected `EAX` would be `0x00000630` or `0x00000650`. This also
reconciles with this file's own "Pentium II instruction set" header, which
was already correct; only the actual `CPUID` value and the "source of truth"
reference below (currently the unrelated, far earlier 80386 manual) need
correcting. Blocked on locating the exact Pentium II spec manual to confirm
Model/Stepping before committing to a value -- not blocking anything else.

**Fixed 2026-07-22**: `IsDBCSLeadByte` (`tew/api/kernel32_locale.py`) --
previously had no handler at all (see "Background" below for the tid=1012
bug this caused). Implemented properly rather than a bare always-FALSE
stub: `GetACP`/`GetCPInfo`/`IsDBCSLeadByte` now all derive from one
`ANSI_CODEPAGE` constant (1252, Western/no lead bytes) and a shared
`_DBCS_LEAD_BYTE_RANGES` table (also covering 932/936/949/950 for
correctness if the codepage constant is ever changed) -- confirmed via
direct evidence (`GetACP`/`GetCPInfo`'s `LeadByte[]` array were already
hardcoded for 1252 everywhere else in tew) rather than assumed, and
confirmed MCity_d.exe itself never imports `IsDBCSLeadByte` (string search
of the whole exe found zero matches) so this only affects `dao350.dll`'s
own init path. Jet (`MSJET35.DLL`, confirmed by string search of
`dao350.dll` as a real DLL it loads dynamically) has NOT been analyzed and
hasn't been reached by the emulator yet, so no guarantee is made about it
specifically -- only that whatever it asks GetACP/GetCPInfo/IsDBCSLeadByte
will get an answer consistent with the rest of the environment, by
construction, not by DAO-specific luck.

### Background (2026-07-21, later session): `tid=1012`'s premature death,
fully root-caused and fixed. Real cause: `dao350.dll`'s `IsDBCSLeadByte`
import (called from `DllMain`, `0x044c63fc`) had no registered handler, and
`patch_dll_iats` (secondary-DLL IAT patching) had no fallback for an
unmatched import — it silently left the IAT slot holding raw, unrelocated
bytes from the DLL file. `CALL ESI` jumped into that garbage; since tew's
memory is an unprotected flat `bytearray`, executing garbage doesn't fault
the way it would on real Windows, so nothing halted or logged anything —
execution just wandered until it happened to land on `THREAD_SENTINEL`,
skipping every real stack frame above it without ever executing a matching
`RET`. Confirmed directly via a logpoint at the `CALL ESI` site
(`0x044c6410`): only 1 of the expected 256 loop iterations fired, with
`ESI=0x000735ba` (not a valid code address).

Fixed by unifying IAT-patching into one shared function, `patch_iat_entry`
(`tew/loader/dll_loader.py`), used by both `write_iat_handlers` (main EXE,
`import_resolver.py`) and `patch_dll_iats` (secondary DLLs). Any unmatched
import now gets the same auto-generated `[UNIMPLEMENTED]` fatal-halt stub
regardless of which loading path found it missing.

Also added while investigating: `_invoke_emulated_proc`'s "calling
thread died mid-call" detection (the 2026-07-19/21 fix) now has a dedicated
unit test (`tests/unit/api/test_invoke_emulated_proc_thread_death.py`) —
previously untested at the unit level. A thread-end stack dump
(`diagnose_thread_end` in `tew/kernel/exception_diagnostics.py`, fired from
`_make_thread_return_handler` in `crt_handlers.py`) is what made this
investigation possible; the three diagnostic dump functions
(`diagnose_fault`/`diagnose_halt`/`diagnose_thread_end`) now share one
`_dump_cpu_state` helper instead of three copy-pasted register/stack loops,
so a register added to one (this investigation needed ESI, which the
original `diagnose_thread_end` didn't dump) reaches all three automatically.

**Root cause of `DllMain`'s "non-deterministic" return value, fully
diagnosed and fixed** (see changelog.md for the fix sequence): it was never
really about `DllMain` at all. `_invoke_emulated_proc` (`user32_handlers.py`)
ties a nested call's completion to a specific `scheduler` thread idx staying
alive and eventually becoming `current_idx` again. Two real bugs compounded:
(1) it unconditionally read `cpu.regs[EAX]` as "the result" even when the
loop exited via an unrelated thread's fatal halt or a `max_steps` timeout —
explaining the garbage-looking values (`70959764` etc., all leftover EAX
from whatever else was executing, not anything real `DllMain` computed —
confirmed via raw disassembly that real `DllMain`/`entry()` can only ever
return 0 or 1). (2) More fundamentally: the real calling thread for DAO's
`DllMain` (`tid=1012`, a short-lived worker thread spawned via the generic
CRT thread wrapper `0x9fc3a0`, same pattern as `mmtimer_callback`'s thread)
**dies mid-call** — its stack unwinds straight past the sentinel return
address `_invoke_emulated_proc` pushed for `DllMain`'s return, landing back
at its own `THREAD_SENTINEL` instead. Once dead, that thread idx can never
become `current_idx` again (`_pick_next_ready` permanently excludes DEAD
threads), so the old code's "wait for our thread to come back" was
mathematically unsatisfiable — no `max_steps` budget, however large
(tested up to 50,000,000), was ever going to complete. `_invoke_emulated_proc`
now detects `threads[started_thread_idx].status == DEAD` and bails
immediately with a clear diagnostic instead of burning the whole budget —
live-verified: the run now reaches the same final halt at 57.4s instead of
71.4s (~14s faster), with an honest `DllMain(...) -> 0` log instead of a
misleading garbage number.

**New/still-open blocker**: DAO's `DllMain` now fails *correctly and
honestly*, but it still fails every run (`tid=1012` dies every time
observed so far) — meaning `CoGetClassObject`/`CoCreateInstance` for DAO
never even get called anymore (`_ensure_dll_ready` treats the FALSE return
as a load failure and returns `None`). The `*ppv` NULL investigation below
is now blocked behind root-causing **why `tid=1012`'s stack unwinds past
our sentinel** instead of returning normally through `DllMain`'s real code.
Not yet investigated: whether this is a genuine SEH/exception-driven
non-local exit inside DAO's or the CRT's own init path, or an emulator gap
(e.g. `_endthread`/thread-exit being reached from somewhere unexpected).

**Separately, still unexplained**: a `mmtimer_callback` (`0x00a30a40`)
nested call halts at its own entry address instead of its sentinel shortly
after the `DllMain` failure, and the run's final stop (`EIP=0x00200c00`,
identical registers/stack every run) is confirmed via timing to be
**unrelated to DAO/DllMain** — it happens at the same relative point
regardless of how `DllMain` resolves. Next lead if picked up: `mmtimer_callback`
decompiles cleanly (`0x00a30a40`, see architecture section) and does NOT call
`abortmessage` on its own `timeSetEvent` failure path (just a silent
`_DEBUG_trace` + graceful shutdown) — ruled out as the cause of the final
halt. `_TIMER_init` (`0x00a30be0`) does have real `abortmessage` calls, but
none of its guard conditions are actually triggered by our stubs (all report
success), and its one retry-exhaustion abort path takes several real
seconds via a `_THREAD_yield` spin-loop — timing doesn't match what's
observed. The `0x00200c00` halt is most likely a plain, unrelated
unimplemented-API stop that happens to land nearby — not yet identified
which API.

**New scheduler debug visibility added** (`tew/kernel/scheduler.py`,
`tew/api/kernel32_io.py`): thread creation now logs its assigned scheduler
`idx` (previously only `tid` was logged, `idx` was untraceable); every
actual context switch is now logged (`switch: idx=X (tid=Y) -> idx=Z
(tid=W)`); and every block transition (`block_current_on_cs`,
`block_current_on_handles`, `sleep_current`) now logs why/what a thread is
waiting on — previously only wake-ups were logged, never the initiating
block. This is what made the `tid=1012` diagnosis possible; without it the
scheduler-fairness hypothesis (initially suspected) would have been very
hard to rule out. Enable via `LOG_LEVEL=debug LOG_CATEGORIES=scheduler,thread`.

---

### Background (2026-07-19 night session): the DAO `DllGetClassObject`/`*ppv`
investigation below is still accurate as static analysis, but is currently
UNREACHABLE at runtime (see blocker above) until `tid=1012`'s premature
death is fixed. Kept for reference — do not re-derive this from scratch.

The `abortmessage`/DAO database-init abort documented earlier today is now
understood far more precisely, via a real architectural shift: rather than
faking DAO's COM objects in Python, `CoGetClassObject`/`CoCreateInstance`
(`tew/api/oleaut32_handlers.py`) are now **registry-driven** (`hkcr\clsid\
{...}\inprocserver32` in `registry.json`, exactly like real Windows) and, for
CLSIDs registered to a server this emulator actually has
(`_KNOWN_COM_SERVERS`), **load and execute the real DLL** the same way
`authlogin.dll`/`NPSAnlyz.dll`/`dx8z.dll` already are — genuine COM activation
against real compiled code, not a Python stand-in. See changelog.md for the
full sequence of fixes that got this working tonight.

**The DAO CLSID version mismatch, confirmed**: the game's real CLSID
(`{00000010-0000-0010-8000-00AA006D2EA4}`) is DAO **3.5**'s, not 3.6's —
`dao360.dll` (generic period-correct binaries collection) does NOT contain
this CLSID anywhere in its binary; `dao350.dll` (extracted from the game's
OWN real installer, `~/.emu32/DBInst/DAO/data1.cab`) DOES. Real `dao350.dll`
now loads and runs from `~/.emu32/WINDOWS/System32/dao350.dll`.

**`dao350.dll` opened in Ghidra and its real `DllGetClassObject`/
`QueryInterface` read directly** (previously only `MCity_d.exe` had been
analyzed). Confirmed by decompile + raw disassembly (Ghidra's decompiled C
was actively misleading here — rendered real 16-byte `REP CMPSB` GUID
comparisons as fake 1-2 byte string literals):
- `DllGetClassObject` does a real, correct full 16-byte CLSID comparison
  against several DAO-family candidates and correctly matches our CLSID —
  live-confirmed via a logpoint at the match branch, fires every time.
- It builds a small helper object via a completely ordinary
  Borland/Delphi-style multi-level constructor chain (looked like a bug at
  first — the same memory field gets written 4 times in a row — but each
  write is a different base class's vtable in a multi-inheritance chain;
  only the last write matters, which is normal compiled-code shape, not
  a bug).
- That helper's `QueryInterface` (found the real vtable slot address, not
  the wrong one my first static-analysis guess landed on) correctly
  recognizes `IUnknown`/`IClassFactory`/`IClassFactory2`, calls `AddRef`,
  and writes itself into `*ppv` — this code, read statically, looks
  completely correct for what the game requests.

**Corrected understanding (later the same night, via live `seh`-category
logging + direct disassembly cross-referencing, prompted by "nothing I'm
seeing would explain a 496-byte object")**: this was never generic stack
corruption. It's a genuine NULL-pointer vtable-dispatch crash:

- `dao350.dll`'s real `DllGetClassObject` returns `S_OK` for the game's
  first `CoGetClassObject(rclsid, 1, NULL, IID_IClassFactory, &local_2c)`
  call but never actually populates `*ppv` — confirmed live by logging
  `*ppv`'s value alongside `hr` at the call site.
- The game's **own** fallback code doesn't NULL-check `local_2c` before
  dispatching through its vtable, so it wild-jumps to `EIP=0xfefc8d8f`
  (garbage read through a NULL vtable pointer) — a real x86
  `0xc0000005` ACCESS_VIOLATION, not an emulator artifact.
- SEH (`tew/kernel/seh.py`) walks the real `FS:[0]` chain (15 frames),
  finds no handler, and hits the "unhandled fault, halting as before"
  path. This recovery is **not a clean unwind** — it leaves stale return
  addresses sitting on the stack. That's what `__chkesp` was actually
  detecting: not a mystery corruption, but the aftermath of this crash.
  Confirmed two ways: (a) the ~496-byte delta is consistent with stale
  frames left behind by the 15-frame walk, not a fixed-size overwrite;
  (b) `__chkesp`'s own reported return address is wrong — it prints
  `0x008f55a0` when the real return address at that call site is
  `0x008f5351` (verified via `dump_bytes`), which only makes sense if the
  recovery path left old data in place rather than restoring the true
  frame.
- Separately, the `_chkesp` diagnostic (`patch_internals.py`,
  `0x009F1BC0`) itself has a **real, independent bug**: the ZF check it
  performs is correct and sign-agnostic, but its delta-computation
  message hardcodes `EBP` as "the" pre-call ESP snapshot register. At the
  specific call site `0x008f4f04`/`06` the compiler actually cached the
  snapshot in `ESI` (`3B F4` = `CMP ESI,ESP`, confirmed via raw byte
  decoding, not `CMP EBP,ESP` as the message claims) — the snapshot
  register is a compiler register-allocation choice, not always EBP, so
  the diagnostic can print a nonsense delta whenever it isn't. Not yet
  fixed in code.

Net effect: the actual open bug is **"why does `dao350.dll`'s real
`DllGetClassObject` return `S_OK` without writing `*ppv`?"**, plus **"why
does our SEH unhandled-fault path not unwind cleanly, and should it?"** —
not a stack-corruption hunt. This happens across every run so far,
regardless of whether `DllMain` returned 0 or nonzero.

**Ruled out tonight**: scheduler thread-switching mid-nested-call. Found and
fixed a real, separate bug this session (`_invoke_emulated_proc` now
detects when the scheduler swaps to a different thread mid-call and no
longer misreads that thread's halt as "our call returned" — see
changelog.md) — genuinely necessary and confirmed live (virtual time
jumped 1.4s and five unrelated threads ran inside one nested `DllMain`
call before the fix). But the exact same `__chkesp` failure (same address,
same ~496-byte delta) still occurred after this fix, so thread-switching
was not the (sole) cause of this specific corruption.

**RESOLVED 2026-07-21**: `DllMain`'s non-deterministic return value — see
the "Current status" section at the top of this file for the full
diagnosis and fix. It was never a symptom of this corruption; it was
`_invoke_emulated_proc` misattributing unrelated register state, on top of
a real thread (`tid=1012`) dying mid-call. The `tid=1012` death itself is
now the active blocker for reaching this `QueryInterface`/`*ppv` bug again.

**Next step, if this is picked up again** (blocked on the `tid=1012`
blocker above until DAO's `DllMain` can complete far enough to reach
`CoGetClassObject` again):
1. Root-cause why `dao350.dll`'s real `DllGetClassObject` leaves `*ppv`
   NULL despite returning `S_OK` — its `QueryInterface`/vtable-write logic
   read statically correct (see above), so the gap is likely in something
   this emulator provides it (an OLE/COM environment call it depends on
   that's stubbed wrong, or a memory-layout assumption it makes that we
   don't satisfy) rather than in the DLL's own code. Worth a memory-write
   trace across the `QueryInterface` call specifically (the ClickHouse
   execution-history tooling from `~/pe-walker/history-poc`, see
   [[tew_fake_kernel_gaps]] section 10, is likely the right tool) to see
   whether the write to `*ppv` happens and gets clobbered, or never
   happens at all.
2. Decide whether `tew/kernel/seh.py`'s unhandled-fault path should do a
   real stack unwind instead of "halt in place" — real Windows would
   terminate the process cleanly here; our current behavior's stale-stack
   side effect is actively misleading downstream diagnostics (see the
   `__chkesp` wrong-return-address symptom above).
3. Fix `_chkesp`'s diagnostic message (`patch_internals.py`) to report the
   actual snapshot register's value instead of hardcoding EBP — either by
   determining the register from the instruction bytes at the call site,
   or by not attempting to name a specific register at all if that's not
   reliably derivable.
4. **New, higher priority**: root-cause why `tid=1012` (DAO's `DllMain`-
   calling worker thread) dies mid-call instead of returning normally —
   see "Current status" at the top. Use the new `LOG_LEVEL=debug
   LOG_CATEGORIES=scheduler,thread` visibility to watch its exact
   instruction path between becoming `current_idx` and hitting
   `THREAD_SENTINEL`; likely worth a breakpoint at `0x04479f74` (real
   `DllMain`/`entry()`) paired with one at whatever address precedes the
   unwind, to see exactly which instruction causes the jump.

**Fixed 2026-07-19 night, real bugs found along the way** (see changelog.md
for full detail): `_invoke_emulated_proc` (`user32_handlers.py`) was
single-stepping one instruction at a time via Python — now runs at native
Zig speed via `cpu.run()` in bounded chunks, checking after each chunk
whether the scheduler swapped to a different thread (see "ruled out" above
— a real, separately-necessary fix, just not the cause of the corruption being
chased). The INT3 debug-breakpoint halt (`win32_handlers.py`) was missing
`fatal_halt`, so it was being silently un-halted by the next scheduler
switch instead of actually stopping — now fixed, not yet independently
live-verified in isolation (always seen together with the DAO work above).
`GetLastError`/`SetLastError` (`scheduler.py`) were sharing one
memory-backed value across ALL threads instead of being per-thread — now
saved/restored per `ThreadState` on every context switch, same shape as
the existing TLS-slot handling. `FormatMessageA` (`kernel32_io.py`) was an
unconditional halt — now implemented (`FORMAT_MESSAGE_FROM_SYSTEM`/
`FROM_STRING`, small table of the HRESULTs this emulator's own COM
handlers produce). `Channel_DebugPrint` (`patch_internals.py`, channel.c)
now surfaces at WARN instead of being silently dropped (its real routing
target — the game's own debug console — never reaches tew's log
regardless). Several very-high-frequency, zero-signal log lines
(`_CrtDbgReport`'s routine leak dump, `timeSetEvent`, `free`/`operator
delete`, `GetFullPathNameA`, `SNDMEMI_validate`'s per-entry detail) moved
from `debug`/`info` down to `trace`.

**Still open, not touched since 2026-07-19:**
- **~85 of ~90 `cpu.halted = True` call sites still lack the `cpu.fatal_halt`
  marker** — see [[tew_fake_kernel_gaps]] section 17's closing paragraph.
- **SDL window is 1536x1248**, not the 1024x768 the D3D8/GDI `GetDeviceCaps`
  fix standardized on.
- Git: not pushed to `origin/main`, per this project's "never push without
  being asked" norm.

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
add **`scheduler,thread`** (new 2026-07-21) with `LOG_LEVEL=debug` for
thread-lifecycle/scheduling investigation (idx assignment, every context
switch, every block reason) — this is what diagnosed the `tid=1012` bug and
is the right starting point for the still-open "why does it die" question.

The simpler run command (`timeout -k 5 90`, `LOG_LEVEL=info`, no extra
categories) is still correct for a general boot-health check that doesn't
need to reach all the way through the DAO handshake.

## Queued issues (priority order)
- **New top priority**: implement `CoGetMalloc` (`ole32.dll`) — root cause of
  the `*ppv` NULL mystery, see "Current status." Real OLE API returning the
  process's `IMalloc`; tew likely has enough COM/vtable infrastructure
  already to build one. Once implemented, `dao350.dll`'s `DllGetClassObject`
  should reach its real `QueryInterface` call and populate `*ppv` for real —
  and now that `fatal_halt` genuinely stops everything, this and any future
  unimplemented dependency will surface as a clean, accurate Halt Diagnostic
  immediately, not a misleading later halt.
- Correct `cpu/src/two_byte.zig`'s `CPUID` signature to real Pentium II
  (`0x00000630`/`0x00000650`) and fix this file's "source of truth" reference
  — see "Current status," blocked on locating the exact spec manual.
- Decide how `_invoke_emulated_proc`'s "didn't complete" sentinel should
  work for `HRESULT`-returning nested calls — its current bare `0` fallback
  collides with `S_OK`, making any aborted call (not just `CoGetMalloc`'s)
  look like a clean success to `_call_dll_get_class_object` and any future
  `HRESULT` nested-call site. See "Current status."
- Identify the `EIP=0x00200c00` final halt's real cause — confirmed
  unrelated to DAO/`DllMain` timing, still unidentified which API it is.
- Decide whether `mmtimer_callback`'s own nested-call halt (lands back at
  its own entry instead of its sentinel) is a real re-entrancy bug or
  another instance of the same "thread died mid-call" class just fixed.
- Decide/implement a real unwind for `seh.py`'s unhandled-fault path
  instead of "halt in place with stale stack data"
- Fix `_chkesp`'s diagnostic (`patch_internals.py`) hardcoding EBP as the
  snapshot register when it's a compiler register-allocation choice
  (confirmed ESI at one real call site)
- Dedicated pass on the ~85 unmarked `cpu.halted` sites (priority order not
  yet established)
- SDL window resolution (1536x1248) vs. `GetDeviceCaps` (1024x768) mismatch
- DrawPrimitive / DrawIndexedPrimitive coverage beyond what's needed to
  reach the DAO abort — not yet assessed how much is implemented
- `[alive]` heartbeat silent during `GetMessageA` host-sleep — low priority

## Architecture
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
  just another instance of the same pattern, not a DAO-specific mechanism;
  its premature-death bug is resolved, see "Current status."
- **COM activation** (new tonight): registry-driven (`hkcr\clsid\{...}\
  inprocserver32`), real DLLs loaded and executed for CLSIDs in
  `_KNOWN_COM_SERVERS` (`oleaut32_handlers.py`) — currently just DAO 3.5
  (`dao350.dll`, real file at `~/.emu32/WINDOWS/System32/`, kept out of the
  repo since it's a Microsoft-copyrighted redistributable). Unregistered or
  unimplemented CLSIDs fail honestly with `REGDB_E_CLASSNOTREG`, matching a
  real unmodified install missing that component. This pattern (search a
  directory of real DLLs, fall back to Python stub) is worth reusing for
  *other* pure user-mode COM/utility libraries the game touches — NOT for
  anything DirectX/hardware-driver-dependent (`d3d8.dll`, `ddraw.dll`,
  `dsound.dll` etc. all need a real kernel-mode HAL/driver stack this
  emulator doesn't have; tew's existing hand-built D3D8-over-Vulkan is
  already the correct solution to that problem, not something to replace).

## Test suite
589 tests (all passing, reconfirmed 2026-07-23).
