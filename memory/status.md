# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

## Current state (2026-04-17)

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

## Current blocker
Sporadic `SDL_QUIT` received during the message loop — not triggered by user input.
Source unknown: possibly Wayland/X11 compositor sending close event to idle window,
or a bug in our SDL event handling. Investigate `pump_sdl_events` and SDL window
lifetime when no SDL events are expected.

## Queued issues
- **`[alive]` heartbeat dead during GetMessageA**: `_progress_countdown` only ticks
  on `cpu.step()`, but `GetMessageA` blocks in host `_time.sleep(0.001)` between
  SDL polls. Add alive logging inside `GetMessageA`'s sleep loop, or use a
  Python-level periodic log.
- `GetSystemMetrics` cap at 1024×768 applied, but window is still 1536×1248 —
  game has additional sizing logic. Not critical.

## Fixed this session (2026-04-17)

### Memory / heap (earlier)
- **`__free_dbg` patched** (`patch_internals.py`, 0x009F6E20): internal MSVC debug
  CRT free validates debug block headers our bump allocator never writes → assertion
  `_BLOCK_TYPE_IS_VALID` in `__freeptd` → `_CrtDbgReport` halt. Patch to no-op,
  consistent with existing `free()` IAT handler.

### VirtualAlloc accuracy
- **Honored `lp_addr` for MEM_RESERVE** (`kernel32_handlers.py`): previously always
  used bump allocator, ignoring requested address. Now uses `lp_addr` when non-zero
  and advances bump pointer past the reserved region to prevent future overlap.
- **MEM_COMMIT range check** (`kernel32_handlers.py`): was exact-base lookup only.
  Now checks if `lp_addr` falls within any reserved region (game's allocator commits
  sub-pages of a previously reserved block).

### New user32 handlers
- **`GetKeyState`**: return 0 (all keys up/untoggled) — was halting tid=1007 at 223 steps
- **`GetSystemMetrics`**: cap SM_CXSCREEN/SM_CYSCREEN at 1024×768 (was returning real
  display resolution 5160×2340)
- **`SetActiveWindow`**: return NULL (no previously active window)
- **`SystemParametersInfoA`**: handle SPI_GETSCREENSAVEACTIVE (write FALSE) and
  SPI_GETWORKAREA (write RECT 0,0,1024,768); return TRUE for others
- **`SetWindowsHookExA`**: real registration — stores (idHook, lpfn) in `_winhooks` dict
- **`UnhookWindowsHookEx`**: removes from `_winhooks`
- **`CallNextHookEx`**: returns 0 (no chain)

### Hook dispatch
- `_dispatch_winhooks` called from `PeekMessageA` and `GetMessageA` after writing MSG
  struct: fires WH_GETMESSAGE hooks with (HC_ACTION, PM_REMOVE, lp_msg) and
  WH_KEYBOARD hooks with (HC_ACTION, vk, 0) for WM_KEYDOWN/WM_KEYUP

### Keyboard input pipeline
- **WM_KEYDOWN/WM_KEYUP** added to `window_manager.py` constants
- **`_sdl_sym_to_vk`** added: maps SDL keysyms to Win32 VK codes (letters, digits,
  arrows, F-keys, modifiers)
- **SDL_KEYDOWN/SDL_KEYUP** → WM_KEYDOWN/WM_KEYUP now posted to message queue for
  every key event (in addition to existing dialog-specific direct handling)

### Progress heartbeat
- **`[alive]` log** added to main step loop (`run_exe.py`): logs every 5M steps at
  INFO level. NOTE: does not fire during `GetMessageA` host-sleep — see queued issues.

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
