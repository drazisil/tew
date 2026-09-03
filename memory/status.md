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

## Current status (2026-09-02, cont'd x51) — RESOLVED: `EIP=0x00000000` null-jump crash, root-caused to two uncoordinated, unbounded bump allocators (`state.simple_alloc` / D3D8's `_heap_alloc`) sharing overlapping guest address space. `_heap_alloc` now has its own real, bounded region. A `DI_DEV_VTABLE` gap (real but insufficient fix, found along the way) also landed. New, unrelated blocker one layer deeper: `DS::DuplicateSoundBuffer` now halts on a *legitimately different* `this` — the invalid-this check doesn't support more than one DirectSound buffer object.

Picked up chasing the `DS::DuplicateSoundBuffer` halt left open at x50 — turned out to be downstream of missing guest asset files (`scn.*` GUI resources), resolved once Molly supplied the real files. Past that, hit a new, genuine `EIP=0x00000000` crash.

**Root cause, confirmed live via watchpoint + allocator-cursor diagnostic**: `tew/api/d3d8/_helpers.py`'s `_heap_alloc()` (backs D3D8/DirectSound/DirectInput COM object allocation) was a bare bump allocator starting at `0x04800000` with **no upper bound at all**, despite a comment claiming separation from the CRT heap. That start address is *already inside* `state.simple_alloc`'s own valid CRT-heap range (`0x04000000`-`THREAD_STACK_BASE`=`0x08000000`, `tew/api/_state.py`) — the two allocators' regions overlapped from the very first `_heap_alloc` call, not just after long runs. Confirmed the D3D8 cursor reached `0x09d91780` (~89.6MB) by crash time, well past even `THREAD_STACK_BASE`. A real texture-format-converter function (`_bpp16to15`, part of a legitimate SHAPE/TEXTURE conversion dispatch table at `0x01284cb8`-`0x01284db4`) writing genuine pixel data clobbered a `_heap_alloc`'d DirectInput device object's vtable pointer with garbage (`0xbdecc1cc`), which the CPU then jumped through.

**Dead end investigated first, kept anyway**: extended `DI_DEV_VTABLE` (`tew/api/dinput_handlers.py`) from 18 to 26 slots (`CreateEffect`, `EnumEffects`, `GetEffectInfo`, `GetForceFeedbackState`, `SendForceFeedbackCommand`, `EnumCreatedEffectObjects`, `Escape`, `Poll`) to match the real `IDirectInputDevice2A` spec — legitimate, independently-correct fix, but live-verification showed the identical crash recurring after it landed. The real crashing object's vtable pointer was garbage unrelated to any tew-defined vtable, which is what led to the allocator-overlap investigation below.

**Also checked, dead end**: whether MSVC CRT globals track a real heap boundary we could reuse (`___sbh_threshold`, `__heap_alloc_base`, `__CrtCheckMemory`, `__heapchk`). All real, all traced live in Ghidra — `___sbh_threshold` is the small-block-heap size-class threshold (≤1016 bytes, unrelated to address bounds) and MCity never even calls its setter; `__CrtCheckMemory`/`__heapchk` are pure corruption validators with no limit-setting or -querying capability, ultimately delegating to `HeapValidate(__crtheap, ...)` — a real Win32 call, meaning any bound enforcement is tew's own emulation to own, not something inherited from the guest. Confirmed `__heap_init` (`0x00a06100`, called from `entry()` at `0x009fc9fe`, before `WinMain`) itself calls `HeapCreate(flags, 0x1000, 0)` — `dwMaximumSize=0`, i.e. the real binary also declines to state a real limit, relying on the OS. No CRT lever existed for this; had to be a tew-side fix.

**Fix** (`tew/api/d3d8/_helpers.py`): `_heap_alloc` now starts at `D3D8_HEAP_BASE=0x09000000` and raises `RuntimeError` past `D3D8_HEAP_LIMIT=0x10000000` — the gap between `THREAD_STACK_BASE`'s region (`0x08000000`-`0x08FFFFFF`) and the DLL range (`0x10000000+`, `tew/loader/dll_loader.py`), mirroring `simple_alloc`'s own loud `THREAD_STACK_BASE` bounds check. New `tests/unit/api/test_d3d8_heap_alloc.py` (5 tests). Bumped `MEM_SIZE` in the four D3D8/DirectSound/DirectInput test files that hardcoded `128MB` (no longer covers the new region) to `272MB`.

**Confirmed live**: reran with a raised `TEW_MAX_STEPS`, execution now sails straight through the step count (~921M) where the null-jump used to fire — reaches step 923M / 93s wall-clock with no allocator overlap. New halt one layer deeper: `DS::DuplicateSoundBuffer: invalid this=0x0afc0080 (expected 0x002203a0)` — `0x0afc0080` is a legitimately-allocated object inside the *new* D3D8 heap region, not garbage. The `_com_stub`'s `expected_this` check is a hardcoded singleton-object assumption that doesn't support the game creating more than one DirectSound buffer.

Repro: `cd /data/Code/tew/.claude/worktrees/directsound-dupbuffer && TEW_MAX_STEPS=1500000000 timeout 200 .venv/bin/python run_exe.py`. Branch: `fix/d3d8-heap-bounded-region`. Full suite passing (1223 tests).

**Next**: `DS::DuplicateSoundBuffer` invalid-`this` halt — needs the DirectSound object model to support more than one buffer, not just a single hardcoded expected `this`.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
- **CORRECTION (2026-08-30), supersedes `status_archive.md`'s x12/2026-08-25 and 2026-07-24 entries**: "restart the compositor in place" (`kwin_wayland --replace ...`, or `systemctl --user restart plasma-kwin_wayland.service`) is **no longer a valid stuck-SDL troubleshooting step** -- confirmed tonight (2026-08-30) that a compositor restart crashes the *entire user session*, not just the wedged tew client, twice in a row. Whatever made this a safe in-place recovery in 2026-07-24/2026-08-25 no longer holds (environment/KWin-version drift, most likely -- not investigated further). Do not attempt a compositor restart as an automated recovery step; if a run hangs at SDL2/window init, check for and clean up orphaned `run_exe.py` processes first (`SIGTERM`, not `-9`, to let `window_manager.shutdown()` run), and otherwise stop and ask Molly rather than touching the compositor.
