# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

## Current state (2026-04-22)

Game reaches the rendering loop and cycles through it actively:
- Login, auth, THRASH/Vulkan, SDL window, timer threads, NPS networking threads — all alive
- NPS threads (tid=0x3ec, tid=0x3ed) now survive CS contention and keep running
- 129M+ steps reached in 30s timeout run
- Rendering loop active: BeginScene/SetRenderState/SetVertexShader/SetStreamSource cycling
- Thread 1007 (hook thread) spins at GetMessageA — alive but consuming scheduler time

ZigCPU is wired in and working. Main loop uses cpu.run(100K) batches.

## Current blocker

Scheduler refactor in progress (Steps 2/12 complete). `cpu.halted` was being used
for thread suspension as well as real CPU halts — refactoring to separate these
cleanly. Scheduler class (`tew/kernel/scheduler.py`) is fully implemented and
tested; wired into `CRTState` but not yet used for actual thread switching.

Next step: Step 3 — migrate `PendingThreadInfo` → `ThreadState`, add property
delegation on `CRTState` so existing handler code keeps working.

After refactor: `Dev::BeginScene` remains the next real blocker (requires Vulkan
device). Design `D3D8DeviceState` before touching BeginScene/EndScene/Present.

## Queued issues (priority order)
- **Scheduler refactor** — Steps 3–12 remaining. See plan in session notes.
  - **Step 9 note (heartbeat round-robin):** The heartbeat must call
    `scheduler.maybe_switch(cpu, memory)` after each quantum so threads that
    never voluntarily yield (no Sleep/Wait/CS calls) still get preempted at
    batch boundaries. The old design used a hard 10K-step slice per background
    thread; the new design achieves the same via heartbeat-driven switching.
    Without this, a non-yielding thread monopolizes the CPU indefinitely.
    Clock advancement is NOT the issue (tick() handles that unconditionally);
    this is purely about CPU time fairness between threads.
- **`D3D8DeviceState` class** — design before implementing CreateDevice/BeginScene/Present.
- `BeginScene` / `EndScene` / `Clear` / `Present` — require real Vulkan device; NOT stubs.
- `[alive]` heartbeat still silent during `GetMessageA` host-sleep — heartbeat
  only fires between batches, not inside the host sleep loop. Low priority.
- SDL window is 1536×1248 despite SM_CXSCREEN/SM_CYSCREEN capped at 1024×768 —
  game has additional sizing logic. Not critical.
- `diagnose_fault` assert fixed (BeginScene halt now propagates correctly).
- BeginScene fires 4x instead of 1x — caused by scheduler clearing cpu.halted
  when resuming threads. Will be fully resolved by the scheduler refactor.

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
