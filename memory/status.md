# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Holds only the single most-recent `## Current status` entry — do not let `## Previous status` entries accumulate here again; rotate them into `status_archive.md` instead (see below) once a new "Current status" replaces them. Completed work goes in changelog.md — do not add "what's fixed" sections here.*

*Full investigation history lives in two places, both newest-first — do not re-derive any of it from scratch, grep instead: `changelog.md` (durable, organized by fix) and `status_archive.md` (rotated-out `## Previous status` entries, 2026-08-02 through 2026-08-28, some session-in-progress detail not duplicated in changelog.md).*
---

## Known false leads (permanent — do not remove on rotation)

- **`dbcode.c(3376) "The class has not been licensed"`**: prints every run, every time DAO/Jet does COM work, well before any actual failure. Molly confirmed (2026-08-16) this is expected/ignorable — NOT the cause of `CreateQueryDef`/DAO-3075 failures. Got mistakenly re-flagged as a "new lead" once already the same night (see `status_archive.md`, "Previous status (2026-08-16, cont'd x4)", for the correction) — check here before treating it as new again.

## Current status (2026-09-05, full session) — Texture-sampling pipeline built end-to-end; real textured content CONFIRMED visible on screen. Next: investigate a real guest-code crash around t≈41s (unrelated to D3D8), and wire real mouse/keyboard input.

Eight real, independently-verified bugs found and fixed across one long session (full methodology in `changelog.md`'s 2026-09-05 entry and `TODO.md`'s matching RESOLVED item): CreateTexture/SetTexture/texture-stage-state converted from lying no-ops to real implementations; real GPU texture upload wired into `IDirect3DSurface8::LockRect`/`UnlockRect` (confirmed via call tracing to be the real game's actual upload path, not the texture's own Lock/Unlock); D3DFORMAT-aware pitch + BGRA8 conversion (real `D3DFMT_R5G6B5` textures were being corrupted by a hardcoded `width*4` pitch); a window-transparency bug (alpha=0 draws were making the game window see-through to the desktop via the Wayland compositor); a descriptor-set race (Vulkan reads descriptor contents at command-buffer *execution* time, not record time -- a single shared descriptor set meant every draw in an unpresented frame sampled whichever texture was bound last); the same class of bug for vertex data (every draw overwrote vertex-buffer offset 0); a Y-axis flip bug (Vulkan NDC is Y-down like D3D8 screen space, not Y-up like OpenGL -- the code used the OpenGL-style flip, rendering everything the wrong place); and a swapchain image-layout bug (`BeginScene`'s re-acquire barrier used `oldLayout=UNDEFINED` on every frame, a real content-discard hint, instead of only the first time each image is used).

**The Y-flip bug was found only because Molly refused to accept a provably-correct GPU pixel readback as proof of a working screen** ("I'd love to see something on the screen. I'm still not agreeing that a blank screen is working.") -- correctly so; the readback proved the *data* was right, not that anything was on screen. Verification chain, strongest to weakest: (a) a real textured quad directly visible in a live screenshot; (b) full-screen solid-red `Clear()` confirmed reaching the actual composited window; (c) direct GPU pixel readback via `vkCmdCopyImageToBuffer`; (d) full test suite green (1249 passed) throughout.

**Two real gaps found along the way, not yet fixed**:
1. A real guest-code crash around t≈41s in live runs (`EIP=0x00688c68`, crash details in `/tmp/emu_crash.json`), unrelated to D3D8/texture work -- reproduces both with and without this session's debug instrumentation. Not yet investigated. **This is the current task.**
2. The D3D8 game window receives no real mouse/keyboard input: `window_manager.py` never posts `WM_MOUSEMOVE` (constant defined, never used), never handles button-up or right/middle buttons, and has no focus-change handling at all for the render window; `dinput_handlers.py`'s `GetDeviceState` is a hardcoded zero-fill stub. Even with a fully visible persona-select screen, nothing can currently be clicked.

Still open from the texture work (see `TODO.md` for full detail): DXT/S3TC decompression (not needed for the textures traced this session, all uncompressed `D3DFMT_R5G6B5`, but would render garbage if hit); multitexturing (stage > 0 tracked but not rendered, not yet observed to matter).

Repro: `cd /data/Code/tew && LOG_LEVEL=debug LOG_CATEGORIES=d3d8 timeout 90 .venv/bin/python run_exe.py` on branch `debug/mmx-usage-counter`.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
- **CORRECTION (2026-08-30), supersedes `status_archive.md`'s x12/2026-08-25 and 2026-07-24 entries**: "restart the compositor in place" (`kwin_wayland --replace ...`, or `systemctl --user restart plasma-kwin_wayland.service`) is **no longer a valid stuck-SDL troubleshooting step** -- confirmed 2026-08-30 that a compositor restart crashes the *entire user session*, not just the wedged tew client, twice in a row. Whatever made this a safe in-place recovery in 2026-07-24/2026-08-25 no longer holds (environment/KWin-version drift, most likely -- not investigated further). Do not attempt a compositor restart as an automated recovery step; if a run hangs at SDL2/window init, check for and clean up orphaned `run_exe.py` processes first (`SIGTERM`, not `-9`, to let `window_manager.shutdown()` run), and otherwise stop and ask Molly rather than touching the compositor.
