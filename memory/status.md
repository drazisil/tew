# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

## Current state (2026-04-17)

Game progresses through full startup sequence before halting:
- Login dialog shown, admin/admin credentials filled from registry
- HTTP GET localhost:443 → 200 OK (auth succeeds)
- authlogin.dll loaded and IAT patched
- options.ini read (SaveData dir missing, all values default — harmless)
- dx8z.dll (THRASH) loaded; Direct3DCreate8 called; Vulkan instance up, 2 devices
- Timer thread (tid=1006) created
- Halts at `GetStockObject(BLACK_BRUSH)` — gdi32, requires GDI object table

Threads 1001–1005 alive but blocked on WaitForSingleObject. Not on critical path.

## Current blocker
`GetStockObject` (gdi32.dll) — kept as `_halt` intentionally.
Implementing it requires a GDI object table so handles are real and traceable.
Returning a fake handle lets the game walk forward on lies — violates project rules.

## Fixed this session (2026-04-17)

### Timer / scheduler (earlier in session)
- **`_run_background_slice` extracted** (`kernel32_io.py`): module-level function that
  runs one background thread slice without touching the caller's stack frame or calling
  `cleanup_stdcall`. `_cooperative_sleep_ex` now delegates to it.

- **`WaitForSingleObject(INFINITE)` on main thread** (`kernel32_io.py`, `_wait_for_single`):
  previously did `cpu.halted = True` → killed emulation entirely. Now drives background
  threads in a loop (process-zero pattern) until the handle is signaled. Logs deadlock
  warning and returns WAIT_TIMEOUT if all threads are blocked.

- **Timer heartbeat** (`run_exe.py`, `_run_timer_heartbeat`): `_TIMER_waitticks` spins
  without Sleep/SleepEx so multimedia timers never fired from the normal SleepEx path.
  Heartbeat fires every 100K main-loop steps: advances `virtual_ticks_ms` by 1ms, invokes
  any due timer callbacks via `_invoke_emulated_proc`, then runs one background slice.
  This unblocks the `_ticks` increment inside the timer thread (FUN_00a30ea0, tid=1006).

### Handler correctness audit (later in session)
- **`_lclose`** (`kernel32_io.py`): was returning 0 unconditionally. Now reads handle,
  closes host fd if open, returns handle on success or HFILE_ERROR (0xFFFFFFFF) on unknown.
- **Stack read masks** (`advapi32_handlers.py`): five `memory.read32(ESP + N)` calls
  were missing `& 0xFFFFFFFF`. Fixed for correctness if ESP sits near 0xFFFFFFFF.
- **`SetForegroundWindow` comment** (`user32_handlers.py`): "pretend it worked" replaced
  with explanation of why TRUE is correct (SDL2 owns the window; Win32 focus N/A).
- **Unused imports and dead variable** cleaned up (ruff).

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

## Architecture
- Game does NOT call D3D8 directly.
- Rendering path: Game → THRASH API (dx8z.dll) → D3D8 (fake COM)
- WinINet connects to localhost:443 (HTTPS)
- authlogin.dll reads AuthLoginServer from registry (localhost)
- Login dialog (SDL2): admin/admin from registry, auto-filled
- Timer thread: FUN_00a30ea0, runs as tid=1006 via CRT wrapper at 0x9fc3a0
  Increments `_ticks` and `_libticks` each time signal 0x7012 fires.

## Test suite
386 tests (all passing as of 2026-04-13).
