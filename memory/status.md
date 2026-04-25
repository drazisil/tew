# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here.*
---

### Current blocker: Wayland deadlock inside IDirect3D8::CreateDevice

`CreateDevice` is called successfully (log: `IDirect3D8::CreateDevice hwnd=0x1034 back=800x600`),
but then hangs before "VkSurfaceKHR created" is logged. The hang is somewhere in the
VkSurface creation block (lines 152–198 of `idirect3d8.py`): either `vkCreateWaylandSurfaceKHR`,
`wl_display_roundtrip`, or possibly `vkGetPhysicalDeviceSurfaceSupportKHR`.

A `wl_display_roundtrip` was added after `vkCreateWaylandSurfaceKHR` to flush Wayland events
before surface queries — but the hang appears before that log line, so either:
1. `vkCreateWaylandSurfaceKHR` itself is hanging (unlikely — it's a local wrap), or
2. `wl_display_roundtrip` is deadlocking because SDL2 holds the Wayland display lock
   and the call can't dispatch without SDL2 pumping events.

Next step: add fine-grained logging inside the surface creation block to pinpoint
which call hangs, then fix (likely: call SDL_PumpEvents before Vulkan surface queries,
or use wl_display_dispatch_pending instead of roundtrip).

## Run command
```bash
cd /data/Code/tew
timeout 30 env LOG_LEVEL=info /data/Code/tew/.venv/bin/python -u /data/Code/tew/run_exe.py 2>&1 | tee /tmp/emu.log | tail -20
```
Note: uutils timeout (installed on this system) does not support inline env vars —
use `env KEY=VAL` prefix and absolute paths. Add `-u` to python for unbuffered output.

## Queued issues (priority order)
- **Pinpoint Wayland deadlock** — add step logging inside surface creation block
- **Fix Wayland deadlock** — likely SDL_PumpEvents or wl_display_dispatch_pending
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
543 tests (all passing as of 2026-04-25).
