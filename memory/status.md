# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here.*
---

### Current status: WATCHPOINT HIT at 0x00a544f3 (SNDMEMI pool corruption)

**Previous blockers resolved**:

1. **Font file `C:\Data\Fonts\Macaro14.ffn` failing to open** — root cause was
   `_MEM_copyfpi` using `FILD m64` / `FISTP m64` (FPU integer 64-bit load/store) to
   copy the filename buffer. The Zig CPU's FPU stack was `[8]f64` (53-bit mantissa),
   which loses precision for i64 values > 2^53, zeroing the destination buffer.
   Fix: changed `fpu_stack` to `[8]f80` (64-bit mantissa — exact round-trip for all
   i64 values). Also added CPUID MMX bit (EDX bit 23) + implemented MOVQ, MOVD,
   PUNPCKLDQ, EMMS so the game uses the integer MMX copy path instead.

2. **cpu.zig split**: 1904-line monolith split into `core.zig` (CpuState + shared
   helpers), `fpu.zig` (FPU ops with f80 fix), `mmx.zig` (MMX instructions),
   `two_byte.zig` (0x0F dispatch with MMX CPUID), `cpu.zig` (one-byte ops + C API).

**Current blocker**: WATCHPOINT HIT at EIP=0x00a544f3 — SNDMEMI pool size field
corruption. Plan file exists: change watchpoint to `blist+7` (MSB of size field)
to catch the actual corruption write, not the innocent `0x00` write that follows it.

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
