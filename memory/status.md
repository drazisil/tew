# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

## Current state (2026-04-24)

### What's fixed this session

**Round-robin preemption (`scheduler.py`, `run_exe.py`):**
- Added `Scheduler.preempt_slice(cpu, memory)`: after each `cpu.run(batch)`, if the
  current thread is READY and another READY thread exists, rotate to it.
- Called in the main run loop immediately after `cpu.run(batch)`.
- Root cause: `mmtimer_callback` (0x00a30a40) signals its own wait event (0x7012) via
  `_SIGNAL_set` inside the `_tmrsub[]` dispatch loop, so `WaitForMultipleObjectsEx`
  always found the event signaled and never yielded. The timer thread consumed 100% of
  emulated CPU, starving all other threads.
- Fix confirmed: at 132M steps the main game window (`Motor City Online` HWND 0x1034)
  is created and the game progresses further than before.

### What was fixed in previous session (2026-04-23)

**`proc=0` / 122-second stall** — `CreateDialogParamA(#106)` fixed with null-guard.

**`PendingTimer.fu_event`** — `timeSetEvent` dispatch modes separated:
- `TIME_CALLBACK_FUNCTION` (0x00): invoke emulated proc
- `TIME_CALLBACK_EVENT_SET` (0x10): SetEvent on handle directly

**advapi32 `timeSetEvent` time source** — fixed to use `state.virtual_ticks_ms + u_delay`.

### Current blocker: __chkesp stack mismatch at 0x0077f8e5

At step ~132M (real ~12s), `__chkesp` fires at return to `0x0077f8e5`.
ESP=0x081bfe68, EBP=0x081bff08, delta=-160 (40 dwords under-popped).
Triggered after `CreateWindowExA` creates the main `Motor City Online` window.

**Stack at crash:**
- `[ESP+00] = 0x0077f8e5` — return address
- `[ESP+04] = 0x00000008` — 1 arg?
- `[ESP+08/0c] = 0x011a9ed4` — ptr (repeated)
- `[ESP+10] = 0x92000000` — main stack sentinel
- `[ESP+1c/20] = 0x00000400 / 0x00000300` — likely width/height (1024/768)

**What to investigate:**
- Decompile `0x0077f8e5` (the caller) — what Win32 call precedes the return?
- The 160-byte delta suggests ~10 args that weren't cleaned up, or a cdecl function
  that was wrapped as stdcall (caller didn't clean up).
- `CreateWindowExA` takes 12 args (48 bytes). Check our stub's `cleanup_stdcall` byte count.
  Alternatively, some other call in the game init path has the wrong convention.

## Uncommitted changes
All changes committed as of 2026-04-24 (450/450 tests pass).

## Queued issues (priority order)
- **`__chkesp` at 0x0077f8e5** — stack mismatch at main window creation
- **`D3D8DeviceState` class** — design before implementing CreateDevice/BeginScene/Present
- `BeginScene` / `EndScene` / `Clear` / `Present` — require real Vulkan device; NOT stubs
- SDL window is 1536×1248 despite SM_CXSCREEN/SM_CYSCREEN capped at 1024×768
- `[alive]` heartbeat silent during `GetMessageA` host-sleep — low priority

## Run command
```bash
cd /data/Code/tew
timeout 120 env LOG_LEVEL=info /data/Code/tew/.venv/bin/python /data/Code/tew/run_exe.py 2>&1 | tee /tmp/emu.log | tail -5
```
Note: uutils timeout (installed on this system) does not support inline env vars —
use `env KEY=VAL` prefix and absolute paths.

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
450 tests (all passing as of 2026-04-24).
