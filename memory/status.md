# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here.*
---

### Current status: SNDMEMI pool corruption — watchpoint hit at EIP=0x00a544f3

**Previous blockers resolved**:

1. **Font file `C:\Data\Fonts\Macaro14.ffn` failing to open** — `_MEM_copyfpi` used
   `FILD m64`/`FISTP m64` with f64 FPU stack (53-bit mantissa, lossy for i64 > 2^53).
   Fix: `fpu_stack: [8]f80`. Also added CPUID MMX bit + MOVQ/MOVD/PUNPCKLDQ/EMMS.

2. **cpu.zig split**: 1904-line monolith → `core.zig`, `fpu.zig`, `mmx.zig`,
   `two_byte.zig`, `cpu.zig`.

3. **VirtualAlloc(NULL, size, MEM_COMMIT, prot)**: was halting. Fixed: NULL + MEM_COMMIT
   alone is spec-valid (implicit reserve+commit); allocate from `next_virtual_alloc`.
   Unblocked `_DTEX_settextureramsize`.

4. **mixerGetNumDevs**: was returning 0 with wrong log level. Fixed to return 1
   (we have a wave device). Verified safe: game calls `mixerGetLineInfoA` once,
   gets `MMSYSERR_NODRIVER`, skips CD volume set cleanly.

5. **IDirect3DTexture8 COM interface**: `CreateTexture`/`CreateVolumeTexture`/
   `CreateCubeTexture` were returning `IDirect3DSurface8` objects (11-slot vtable).
   Implemented `idirect3d8texture.py` — full 18-slot vtable at `D3DTEX_VTABLE=0x00220290`.
   Texture objects store per-mip `IDirect3DSurface8*` at `obj+28+i*4`.
   Also relocated `DI_VTABLE` → `0x002202E0`, `DS_VTABLE` → `0x00220370` to avoid collision.

**Current blocker**: CPU fault at `EIP=0x00a6bfcb`, `ECX=0xfe000088` during `showmad`.
`showmad` (`005b6bb0`) plays `C:\Data\Movies\ealogo.mad` (1,619,700 bytes) via a 1MB
circular stream buffer. The crash happens in `_MAD_decodemacroblock` (called from
`showmad`) when `_maddataptr` points to garbage data — ECX goes out of bounds in the
zigzag table lookup.

**Root cause analysis** (2026-06-06): Stream uses EA's async FILESYS layer (not direct
Win32 ReadFile). Buffer fills via `_FILESYS_read` → `FUN_00a64850` → per-device queue +
`_SIGNAL_set` (→ `SetEvent`) to wake I/O thread (`LAB_00a64c00`, spawned in
`FUN_00a64a60`). Refill chain: `_STREAM_release` → `FUN_00a661d0` → `_FILESYS_read`
only when stream state==2 (WAITING_FOR_SPACE, set when buffer has <8192 bytes free).

Log shows exactly 127 ReadFile calls (127×8192=1,040,384 bytes of 1,619,700), then no
more reads. After the initial fill, state becomes 2. Each `_STREAM_release` call should
trigger one more 8192-byte read via the I/O thread. Decode loop in `showmad` runs
without `_SYNCTASK_run` — relies on `preempt_slice` every 100K steps for I/O thread
scheduling. Crash happens before buffer exhaustion; likely the refill IS triggering but
decoded data is hitting bad VLC input for a different reason.

**Next step**: Determine whether `_STREAM_release` is triggering `_FILESYS_read` (and
thus more ReadFile calls) at all. Add ReadFile logging for ALL reads (not just ealogo.mad)
to see if the I/O thread is waking and doing work. Alternatively, check what's at
EIP=0x00a6bfcb and whether EBP=0x00000014 (smashed frame pointer) explains the fault.

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
