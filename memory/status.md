# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here.*
---

### Current status (2026-07-19 night session, continued): root-caused down to
a deterministic stack-corruption bug, precisely localized but not yet fixed

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

**The real, now precisely localized bug**: a **deterministic stack
corruption**, always detected by the game's own `__chkesp` at the exact
instruction right after the game's **first** `CoGetClassObject(rclsid, 1,
NULL, IID_IClassFactory, &local_2c)` call (confirmed via raw disassembly —
`0x008f4f0b` in `FUN_008f4e70`, immediately after that call's own
`CMP EBP,ESP; CALL __chkesp`), with a consistent ~496-byte ESP/EBP
imbalance. This happens across every run so far, regardless of whether
`DllMain` returned 0 or nonzero. Since `_invoke_emulated_proc` forcibly
restores `ESP` as a register via `cpu.save_state()`/`restore_state()`
regardless of how the real DAO code cleans up internally, this is not a
stdcall/cdecl argument-count mismatch on our side — the corruption is in
stack **memory content**, not the register.

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

**Next step, if this is picked up again**: the corruption is in stack
memory, not registers, so static analysis and the register-only
instrumentation used tonight can't see it directly. Options: (a) a
finer-grained live memory-write trace bracketing the first
`CoGetClassObject` nested call specifically (watch for writes landing in
the ~496-byte window between the game's current ESP and EBP during the
call), or (b) the ClickHouse execution-history capture tooling from
`~/pe-walker/history-poc` (already proven for exactly this kind of
"what wrote to this address" question in earlier sessions — see
[[tew_fake_kernel_gaps]] section 10) — likely a more systematic fit than
more one-off logpoint guessing at this point.

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
- Root-cause the deterministic ~496-byte stack corruption detected right
  after the game's first `CoGetClassObject` call — corruption is in stack
  memory content, not registers, so needs a memory-write-level trace (or
  ClickHouse execution history) rather than more register-only logpoints
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
