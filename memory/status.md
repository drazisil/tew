# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here.*
---

### Current status (2026-07-19 night session, continued further): the
"stack corruption" was a NULL-vtable dispatch crash, root cause now
understood; the crash itself and the SEH recovery path are still unfixed

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

**Also still not root-caused**: `DllMain(DLL_PROCESS_ATTACH)`'s return value
is non-deterministic across separate runs (`0`, `70959764`, `105`,
`70959264`, `70961940`, `70957766` observed) — currently handled
defensively (a `0`/FALSE return is treated as a real load failure, matching
real `LoadLibrary` semantics). Possibly the same underlying corruption
affecting DllMain's own local state before it returns, or a separate issue
— not distinguished yet.

**Next step, if this is picked up again**: three distinct, now well-scoped
items (no longer "chase a mystery corruption"):
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

**Fixed tonight, real bugs found along the way** (see changelog.md for full
detail): `_invoke_emulated_proc` (`user32_handlers.py`) was single-stepping
one instruction at a time via Python — now runs at native Zig speed via
`cpu.run()` in bounded chunks, checking after each chunk whether the
scheduler swapped to a different thread (see "ruled out" above — a real,
separately-necessary fix, just not the cause of the corruption being
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

**Still open, not touched tonight:**
- **~85 of ~90 `cpu.halted = True` call sites still lack the `cpu.fatal_halt`
  marker** (unchanged from this morning — see [[tew_fake_kernel_gaps]]
  section 17's closing paragraph). The INT3 site got fixed tonight as one
  specific instance of this same class of gap; the rest are untouched.
- **SDL window is 1536x1248**, not the 1024x768 the D3D8/GDI `GetDeviceCaps`
  fix standardized on (unchanged from this morning, not re-checked tonight).
- Git: `main` is **11 commits ahead** of `origin/main` (as of `5e49168`),
  not pushed (per this project's "never push without being asked" norm).

## Run command
```bash
cd /data/Code/tew
timeout -k 5 300 env LOG_LEVEL=info LOG_CATEGORIES=com,dll,loader,exception /data/Code/tew/.venv/bin/python -u /data/Code/tew/run_exe.py 2>&1 | tee /tmp/emu.log | tail -60
```
**Updated tonight**: real `dao350.dll` execution takes anywhere from ~1s to
~30s per individual `CoGetClassObject`/`CoCreateInstance` call, so a run
reaching the DAO section needs far more than the `timeout 90` from this
morning. In practice, every run so far has stopped via the `__chkesp`
stack-corruption halt (see above) before ever reaching a full, clean
completion — a 300s budget is generous headroom, not an observed
requirement. Add `registry`/`handlers` to `LOG_CATEGORIES` for deeper
investigation; `loader` carries the IAT-patch confirmation (`Patched N/M
DLL IAT entries`).

The morning's simpler run command (`timeout -k 5 90`, `LOG_LEVEL=info`, no
extra categories) is still correct for a general boot-health check that
doesn't need to reach all the way through the DAO handshake.

## Queued issues (priority order)
- Root-cause why `dao350.dll`'s real `DllGetClassObject` returns `S_OK`
  without writing `*ppv` — the NULL-vtable crash and everything downstream
  of it (SEH walk, `__chkesp` failure) is a consequence of this, not a
  separate bug
- Decide/implement a real unwind for `seh.py`'s unhandled-fault path
  instead of "halt in place with stale stack data"
- Fix `_chkesp`'s diagnostic (`patch_internals.py`) hardcoding EBP as the
  snapshot register when it's a compiler register-allocation choice
  (confirmed ESI at one real call site)
- Root-cause `DllMain`'s non-deterministic return value across runs
  (possibly the same underlying issue, not distinguished yet)
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
582 tests (all passing, reconfirmed tonight).
