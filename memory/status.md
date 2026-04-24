# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here.*
---

### Current blocker: BeginPaint unimplemented

At step ~132M, immediately after `CreateWindowExA` creates the `Motor City Online`
window, the game calls `BeginPaint` — the entry point to the full GDI/Direct3D
rendering system. This is the next major subsystem to build.

`BeginPaint(HWND hwnd, LPPAINTSTRUCT lpPaint)` — fills a 64-byte PAINTSTRUCT and
returns an HDC. Blocked on the broader GDI + D3D8 design.

## Uncommitted changes
- `window_manager.py`: add `WM_PAINT = 0x000F` constant
- `user32_handlers.py`: fix `_GetMessageA` background-thread check (`is_running_thread` → `scheduler.current_idx != 0`)

## Queued issues (priority order)
- **`BeginPaint` / GDI + D3D8 system** — full rendering pipeline; design before implementing
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
