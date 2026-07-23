# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here. Full investigation history (the DAO `*ppv` NULL saga, `tid=1012`'s death, the `fatal_halt` fix, etc.) lives in changelog.md, newest-first — do not re-derive any of it from scratch, grep changelog.md instead.*
---

## Current status (2026-07-23)

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

**Current blocker**: `[UNIMPLEMENTED] oleaut32.dll!Ordinal #4 — halting`,
hit immediately after the three fixes above while live-verifying them —
almost certainly the same "named export exists, ordinal alias missing"
shape as #15/#21 (ordinal 4 = `SysAllocStringLen`, whose named export
already exists in `oleaut32_handlers.py`). See Queued issues, top item.

593/593 tests passing (reconfirmed 2026-07-23).

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
- **New top priority**: implement `oleaut32.dll` ordinal #4
  (`SysAllocStringLen`) — new blocker found live-verifying the `CoGetMalloc`
  fix, see "Current status." Almost certainly the same "named export
  exists, ordinal alias missing" shape as ordinals 15/21 fixed 2026-07-23 —
  the named `SysAllocStringLen` handler already exists in
  `oleaut32_handlers.py`, just needs `_ole_ord(4, ...)` wired to it, pending
  confirmation it doesn't need different stack-argument handling than the
  ordinal-2 (`SysAllocString`) sibling already there.
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
- Dedicated pass on the ~85 of ~90 `cpu.halted = True` call sites that
  still lack the `cpu.fatal_halt` marker (priority order not yet
  established) — see [[tew_fake_kernel_gaps]] section 17's closing
  paragraph.
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

## Test suite
593 tests (all passing, reconfirmed 2026-07-23).
