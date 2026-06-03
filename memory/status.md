# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here.*
---

### Current status: ifc22.dll RESOLVED — login dialog requires interaction

As of 2026-05-31, all known programmatic blockers are fixed. The login dialog
(`DialogBoxParamA`) requires a user click on OK to proceed. Automated test runs
get stuck there. Interactive runs proceed past login.

ifc22.dll (ImmVersion FFB middleware): all 11 imports stubbed.
- CImmMouse::Initialize returns 0 → no FFB hardware → entire FFB path skipped.
- Constructors/destructors are no-ops.
- FFB device methods (UsesWin32MouseServices, OpenFile, etc.) are loud halts
  if ever reached (should not be with Initialize=0).

Next blocker after login: unknown — will be discovered in next interactive run.

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
