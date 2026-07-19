# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here.*
---

### Current status (2026-07-19 night session): real DAO COM activation works,
but the final object handoff is broken — `*ppv` stays NULL despite `S_OK`

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

**Live-verified tonight**: the game's real DAO CLSID (`{00000010-0000-0010-
8000-00AA006D2EA4}`) is DAO **3.5**'s, not 3.6's — confirmed by `dump_bytes`
scanning both `dao360.dll` (generic period-correct binaries collection, does
NOT contain this CLSID) and `dao350.dll` (extracted from the game's OWN real
installer, `~/.emu32/DBInst/DAO/data1.cab`, DOES contain it). With the real
`dao350.dll` loaded (`~/.emu32/WINDOWS/System32/dao350.dll`):
- `DllMain(DLL_PROCESS_ATTACH)` returns nonzero (TRUE) most runs — see "open"
  below, this value is non-deterministic across runs.
- Both `CoGetClassObject` calls the game makes (`IID_IClassFactory`, then
  `IID_IClassFactory2`) return genuine `hr=S_OK` from real dao350.dll code.
- **But `*ppv` is `0x00000000` (NULL) on both, despite `S_OK`** — a real COM
  contract violation (success must guarantee a valid object pointer).
  `CoCreateInstance`'s internal `DllGetClassObject(IID_IClassFactory)` call
  hits the same bug, and the existing NULL-check fallback correctly reports
  `E_FAIL` rather than crashing on a null vtable dispatch — but the DAO
  handshake still can't complete.
- No `_invoke_emulated_proc` timeout/unexpected-halt warning fires during
  these calls — the nested call genuinely runs to completion and hits the
  real sentinel normally. This is not a timing/scheduler artifact; dao350's
  own code is doing this.

**Not yet root-caused**: why real `DllGetClassObject` returns success without
writing `*ppv`. Next step, if this is picked up again: open `dao350.dll`
itself in Ghidra (not yet analyzed as its own program — only `MCity_d.exe`
has been) and read its real `DllGetClassObject` implementation to find what
its success-and-write-`*ppv` branch actually requires.

**Also not yet root-caused**: `DllMain(DLL_PROCESS_ATTACH)`'s return value is
non-deterministic across separate runs (`0`, `70959764`, `105`, `70905676`
observed) — currently handled defensively (a `0`/FALSE return is treated as
a real load failure, matching real `LoadLibrary` semantics), but a real DLL's
`DllMain` returning different garbage-looking values across otherwise-
identical runs suggests something in our environment it depends on isn't
being initialized consistently.

**Fixed tonight, real bugs found along the way** (see changelog.md for full
detail): `_invoke_emulated_proc` (user32_handlers.py) was single-stepping
one instruction at a time via Python — now runs at native Zig speed via
`cpu.run()`, since the sentinel it waits for is a real `HLT` byte and halts
the native loop on its own. The INT3 debug-breakpoint halt
(`win32_handlers.py`) was missing `fatal_halt`, so it was being silently
un-halted by the next scheduler switch instead of actually stopping —
now fixed, not yet independently live-verified in isolation (always seen
together with the DAO work above). `GetLastError`/`SetLastError`
(`scheduler.py`) were sharing one memory-backed value across ALL threads
instead of being per-thread — now saved/restored per `ThreadState` on every
context switch, same shape as the existing TLS-slot handling.
`FormatMessageA` (`kernel32_io.py`) was an unconditional halt — now
implemented (`FORMAT_MESSAGE_FROM_SYSTEM`/`FROM_STRING`, small table of the
HRESULTs this emulator's own COM handlers produce). `Channel_DebugPrint`
(`patch_internals.py`, channel.c) now surfaces at WARN instead of being
silently dropped (its real routing target — the game's own debug console —
never reaches tew's log regardless). Several very-high-frequency,
zero-signal log lines (`_CrtDbgReport`'s routine leak dump, `timeSetEvent`,
`free`/`operator delete`, `GetFullPathNameA`, `SNDMEMI_validate`'s per-entry
detail) moved from `debug`/`info` down to `trace`.

**Still open, not touched tonight:**
- **~85 of ~90 `cpu.halted = True` call sites still lack the `cpu.fatal_halt`
  marker** (unchanged from this morning — see [[tew_fake_kernel_gaps]]
  section 17's closing paragraph). The INT3 site got fixed tonight as one
  specific instance of this same class of gap; the rest are untouched.
- **SDL window is 1536x1248**, not the 1024x768 the D3D8/GDI `GetDeviceCaps`
  fix standardized on (unchanged from this morning, not re-checked tonight).
- Git: `main` is **9 commits ahead** of `origin/main` (as of `f59d4ef`), not
  pushed (per this project's "never push without being asked" norm).

## Run command
```bash
cd /data/Code/tew
timeout -k 5 600 env LOG_LEVEL=debug LOG_CATEGORIES=com,dll,loader,handlers,startup,registry,exception /data/Code/tew/.venv/bin/python -u /data/Code/tew/run_exe.py 2>&1 | tee /tmp/emu.log | tail -40
```
**Updated tonight**: a run that reaches and completes the real DAO handshake
needs far more than the `timeout 90` from this morning — real `dao350.dll`
execution alone can take 60-90s+ of wall-clock across its several nested
calls (each individual `CoGetClassObject`/`CoCreateInstance` call has been
observed taking anywhere from ~1s to ~30s). A 600s budget was sufficient
tonight; the process actually finished in ~118s once it ran to completion.
Add `com,dll,loader,registry` to `LOG_CATEGORIES` when investigating DAO
specifically — `loader` carries the IAT-patch confirmation
(`Patched N/M DLL IAT entries`), `registry` would show any registry calls
dao350.dll makes internally (none observed so far).

The morning's simpler run command (`timeout -k 5 90`, `LOG_LEVEL=info`, no
extra categories) is still correct for a general boot-health check that
doesn't need to reach all the way through the DAO handshake.

## Queued issues (priority order)
- Root-cause `dao350.dll`'s `DllGetClassObject` returning `S_OK` with NULL
  `*ppv` — open `dao350.dll` itself in Ghidra (new program, not yet
  analyzed) and read its real implementation
- Root-cause `DllMain`'s non-deterministic return value across runs
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
