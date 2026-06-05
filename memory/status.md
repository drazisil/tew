# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here.*
---

### Current status: WATCHPOINT HIT at 0x00a544f3 + missing font file

**Previous blocker resolved**: "Hardware acceleration unable to initialize" MessageBoxA.
- Root cause: registry.json "d3d device" was 1 (adapter index 1), but we only expose
  adapter 0. `FUN_006b3890` in main exe checks the requested adapter index against
  `IDirect3D8::GetAdapterCount`. With index 1 and count 1, it fails before even calling
  dx8z. Fix: set "d3d device" to 0.
- `IDirect3D8::CreateDevice` now succeeds (Vulkan swapchain created).

**New blockers** (from most recent run):

1. **Font file missing**: `FILE_load - unable to open file C:\Data\Fonts\Macaro14.ffn`
   — game calls `abortmessage` (OutputDebugString). This may or may not halt execution;
   the watchpoint fires at roughly the same time.

2. **WATCHPOINT HIT at EIP=0x00a544f3** — pre-existing watchpoint on SNDMEMI pool
   size field MSB (from plan: `blist+7`). The write is `0x00` (one byte). The plan
   says to change watch address to `blist+7` (MSB of size field) to catch corruption.
   Final EIP after halt: `0x00a30142`.

**Next investigative step**: Determine if the font file failure is the real abort
path or if it's cosmetic and the watchpoint is the actual halt. Then address whichever
is the true blocker.

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
