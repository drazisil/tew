# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

## Current state (2026-04-21)

Game reaches the rendering loop and cycles through it actively:
- Login, auth, THRASH/Vulkan, SDL window, timer threads, NPS networking threads — all alive
- NPS threads (tid=0x3ec, tid=0x3ed) now survive CS contention and keep running
- 129M+ steps reached in 30s timeout run
- Rendering loop active: BeginScene/SetRenderState/SetVertexShader/SetStreamSource cycling
- Thread 1007 (hook thread) spins at GetMessageA — alive but consuming scheduler time

ZigCPU is wired in and working. Main loop uses cpu.run(100K) batches.

## Current blocker

`Dev::BeginScene` is unimplemented (stubs with `cpu.halted = True`). This is the
expected next blocker now that threading is correct. Requires a real Vulkan device.

Design `D3D8DeviceState` before touching BeginScene/EndScene/Present.

## Queued issues (priority order)
- **`D3D8DeviceState` class** — design before implementing CreateDevice/BeginScene/Present.
- `BeginScene` / `EndScene` / `Clear` / `Present` — require real Vulkan device; NOT stubs.
- `[alive]` heartbeat still silent during `GetMessageA` host-sleep — heartbeat
  only fires between batches, not inside the host sleep loop. Low priority.
- SDL window is 1536×1248 despite SM_CXSCREEN/SM_CYSCREEN capped at 1024×768 —
  game has additional sizing logic. Not critical.
- `diagnose_fault` in run_exe.py crashes with `assert error is not None` when
  `cpu.last_error` is None (happens when BeginScene halt has no error message set).

## Run command
```bash
cd /data/Code/tew
timeout 120 env LOG_LEVEL=info /data/Code/tew/.venv/bin/python /data/Code/tew/run_exe.py 2>&1 | tee /tmp/emu.log | tail -5
```
Note: uutils timeout (installed on this system) does not support inline env vars —
use `env KEY=VAL` prefix and absolute paths.

## Cooperative scheduler — key facts
- Sleep() from main thread → `_cooperative_sleep` → `_run_thread_slice`
- SleepEx() from main thread → `_cooperative_sleep_ex` → `_run_background_slice`
- Background threads that call Sleep/SleepEx during their slice must NOT recurse.
- `virtual_ticks_ms` advances: by `dw_ms` in Sleep handler + 1ms per background slice
  + 1ms per `_run_timer_heartbeat` tick.
- Finite-timeout wait: `waiting_on_handles` + `wait_deadline_ms` on `PendingThreadInfo`.
  Scheduler wakes on signal OR deadline; sets `wait_timed_out=True` on deadline.
- TLS context switch: `_cooperative_sleep` saves current thread TLS from TEB to
  `state.tls_store`, loads next thread TLS from store into TEB (FS:[0xE0+]).

## Architecture
- Game does NOT call D3D8 directly.
- Rendering path: Game → THRASH API (dx8z.dll) → D3D8 (fake COM, Vulkan backend)
- WinINet connects to localhost:443 (HTTPS)
- authlogin.dll reads AuthLoginServer from registry (localhost)
- Login dialog (SDL2): admin/admin from registry, auto-filled
- Timer thread: FUN_00a30ea0, runs as tid=1006 via CRT wrapper at 0x9fc3a0
  Increments `_ticks` and `_libticks` each time signal 0x7012 fires.

## Test suite
388 tests (all passing as of 2026-04-21).
