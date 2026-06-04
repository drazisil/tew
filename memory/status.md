# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here.*
---

### Current status: _THRASH_setvideomode always returns false → hardware acceleration MessageBoxA

Post-login, the game calls `_THRASHDRIVER_init` which:
1. Calls `_THRASH_setstate(0x13, &_librarythrashinterface)` — sets 8 callback ptrs
   in dx8z.dll globals `DAT_600200e0..fc`. `DAT_600200e8 = 0x0040439f` (`setwinhandler`).
2. Calls `_THRASH_init()` → creates D3D8 object → enumerates adapters → registers
   window message handlers 0x464→`FUN_60003500` and 0x465→`FUN_60003430` via callback.
3. Calls `_THRASH_setvideomode(display, mode, bpp)`.

**Root cause of failure**: `_THRASH_setvideomode` (dx8z.dll:0x60003230) only sets
`bVar4=true` in the cross-thread path. It defaults false and the same-thread path
never changes it — so the return value is always false → game shows error MessageBoxA.

**Cross-thread path** (requires `DAT_600200e8 != 0`):
- Creates unsignaled event via `CreateEventA`
- `PostMessageA(hwnd, 0x464, display_idx, mode)` — queues to window message queue
- `WaitForSingleObject(event, 10000)` — blocks thrash thread
- Window thread (`FUN_0077ef80`) runs `GetMessageA`/`DispatchMessageA` loop
- DispatchMessageA → window proc → dispatches 0x464 → calls `FUN_60003500`
- `FUN_60003500` does the actual `IDirect3D8::CreateDevice` call
- `FUN_60003500` calls `SetEvent(DAT_6001de50)` → wakes thrash thread → bVar4=true

**Key question**: Does the game's window thread (`FUN_0077ef80`) run as a cooperative
thread in our scheduler? It's created by `_THREAD_create(FUN_0077ef80, ...)` inside
`openmainwindow`. If that thread isn't running, no one processes the PostMessageA
message, the event is never signaled, and WaitForSingleObject times out → WAIT_TIMEOUT
→ bVar4=false.

**Next investigative step**: Check whether `_THREAD_create`/`_THREAD_yield`/`_SYNCTASK_run`
are hooked, and whether the window thread exists in the cooperative scheduler at the
point `_THRASH_setvideomode` runs.

Also: `CoCreateInstance` fails (REGDB_E_CLASSNOTREG) — still happening, probably DirectSound.

Note: game also opens `CreateFile("")` (empty path) around this area — this returns
INVALID_HANDLE_VALUE and the game continues normally, so it's not blocking.

### Deferred: beta binary (mcity_beta_1.exe) — "Game CD not found"

The beta binary always runs the CD check regardless of instLev. The check function
(~0x4d27c0) reads instLev and sets bMaxInstall, but then unconditionally calls
SetErrorMode + loops GetDriveTypeA over all 26 drives. No CD found → MessageBoxA +
return 2 → ExitProcess.

**Planned fix (not yet implemented):** make `GetDriveTypeA` return `DRIVE_CDROM` for
the drive containing the install directory. Binary-agnostic Win32-layer fix that works
for any game using the standard GetDriveTypeA CD detection pattern.

## Run command
```bash
cd /data/Code/tew
timeout 30 env LOG_LEVEL=info /data/Code/tew/.venv/bin/python -u /data/Code/tew/run_exe.py 2>&1 | tee /tmp/emu.log | tail -20
```
Note: uutils timeout (installed on this system) does not support inline env vars —
use `env KEY=VAL` prefix and absolute paths. Add `-u` to python for unbuffered output.

## Queued issues (priority order)
- **RUNAWAY at 0x2196** — diagnose bad call from 0x9f8d11 (current blocker)
- SDL window is 1536×1248 despite SM_CXSCREEN/SM_CYSCREEN capped at 1024×768
- DrawPrimitive / DrawIndexedPrimitive — currently `_halt`; needed for actual geometry
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

## Test suite
543 tests (all passing as of 2026-05-08).
