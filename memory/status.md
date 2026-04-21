# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

## Current state (2026-04-20)

Game progresses through full startup sequence, opens a game window, installs
Windows hooks, and enters its main message loop:
- Login dialog shown, admin/admin credentials filled from registry
- HTTP GET localhost:443 → 200 OK (auth succeeds)
- authlogin.dll loaded and IAT patched
- options.ini read (SaveData dir missing, all values default — harmless)
- dx8z.dll (THRASH) loaded; Direct3DCreate8 called; Vulkan instance up, 2 devices
- Timer threads (tid=1006, tid=1007) created
- SDL window 'Motor City Online' created (1536x1248)
- `SetWindowsHookExA` called from tid=1007 — hook registered, proc will be dispatched
- Game enters message loop (GetMessageA/PeekMessageA)
- Game reaches `BeginScene` (D3D8 vtable slot 34)

ZigCPU is wired in and working. Main loop uses cpu.run(100K) batches.

## Current blocker

`BeginScene` / `EndScene` / `Clear` / `Present` require a real Vulkan device.
`CreateDevice` must be implemented first (VkDevice, VkSurface, VkSwapchain, command pool,
sync primitives). Design `D3D8DeviceState` class before touching CreateDevice.

## Queued issues
- `GetSystemMetrics` cap at 1024×768 applied, but window is still 1536×1248 —
  game has additional sizing logic. Not critical.
- `[alive]` heartbeat still silent during `GetMessageA` host-sleep — heartbeat
  only fires between batches, not inside the host sleep loop. Low priority.

## Fixed this session (2026-04-20)

### TEB/PEB truthfulness
- `SetLastError`/`GetLastError` now read/write TEB memory at `TEB_BASE + 0x34`
  (was: Python `state.last_error` field, disconnected from FS:[0x34]).
  `state.last_error` removed entirely from `CRTState`.
- `TlsSetValue`/`TlsGetValue` now read/write TEB memory at `TEB_BASE + 0xE0 + slot*4`.
  `_cooperative_sleep` saves/restores TLS in TEB memory around background thread slices
  so FS:[0xE0+] is always correct for the active thread.
- `PEB+0x18` (ProcessHeap) now populated by
  `initialize_kernel_structures(stack_base, stack_limit, process_heap)`.
- `TEB_BASE = 0x00320000` and `PEB_BASE = 0x00300000` added as module constants to `_state.py`.

### CriticalSection fix
- `EnterCriticalSection` and `LeaveCriticalSection` now correctly track ownership
  (OwningThread at +0x0C) and recursion count (RecursionCount at +0x08).
- Contested lock or waiter-exists conditions halt loudly instead of silently no-oping.

### kernel32_handlers.py split
- Monolithic file (~1295 lines) split into orchestrator (~329 lines) + 5 sub-modules:
  `kernel32_memory.py`, `kernel32_sync.py`, `kernel32_locale.py`,
  `kernel32_system.py`, `kernel32_io.py`.

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
388 tests (all passing as of 2026-04-20).
