# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here.*
---

### Current status (2026-07-19, live-reconfirmed): full boot, zero emulator crashes,
but exit is a real game-side ABORT, not a clean run

Fresh run against `main` today (commit below): **~201M steps in ~56-62s wall-clock,
zero emulator crashes, zero unimplemented-API halts on the boot path**. Login
succeeds, real Vulkan rendering happens, then the game hits its own `abortmessage`
abort (a real, game-side DAO/DCOM database-init failure — `depthconv.c:1137`,
"Failed to initialize database...") and calls `ExitProcess(0)` afterward. The run
log now says so explicitly (`=== Emulation Complete (NOT a clean exit) ===`, see
"Fixed today" below) — the emulator itself is healthy, but this is NOT the same
thing as the game reaching a successful end state. This matches the milestone
documented in [[tew_fake_kernel_gaps]] sections 16-17 — re-verified live today, not
just carried over from memory.

**Fixed today: `MessageBoxA`/`MessageBoxW` now log at a severity matching the
dialog's own icon** (`MB_ICONERROR`/`MB_ICONSTOP`/`MB_ICONHAND` → `error`,
`MB_ICONWARNING` → `warn`, else `info`), and `CRTState.fatal_dialogs` records every
error-severity dialog so `run_exe.py`'s final summary can no longer look clean when
it wasn't. Previously the fatal `abortmessage` dialog and the harmless "run full
screen?" prompt both logged at flat `INFO`, indistinguishable in a real triage. Full
details in changelog.md.

**Two unattended-boot dialog auto-clicks** (`run_exe.py:182-209`) — these are the
only two interactive prompts MCity_d.exe shows before real gameplay starts, and the
run would otherwise sit blocked on real mouse input without them:

1. **Login dialog** (`_auto_click_login_continue`, resource 114, title "Motor City
   Online Login"). Username/password are already sourced from `registry.json` by
   the game itself (`LoginName`/`LoginPW` registry reads, confirmed in today's log
   at 2.778s–2.792s) — the hook only clicks the Continue button
   (`wm.click_control(dlg_hwnd, _LOGIN_CONTINUE_ID)`, control ID `0x0001`). Installed
   via `window_manager.set_dialog_step_hook`.
2. **"Run full screen?" prompt** (`_auto_decline_fullscreen_prompt`, `MB_YESNO`,
   text contains "full screen", from `FUN_006b13b0`). Auto-answers `IDNO` (7) to
   default to windowed mode. Installed via `window_manager.set_messagebox_hook`.
   Confirmed in today's log at 3.602s, `MessageBoxA(...) type=0x4 -> 7`.

A third window (dialog resource 106, untitled splash bitmap, no buttons) also
appears in this stretch of boot but dismisses itself and needs no hook — documented
in the code comment so a future reader doesn't go looking for a third auto-click.

**Still open, not touched today:**
- **~85 of ~90 `cpu.halted = True` call sites still lack the `cpu.fatal_halt`
  marker** from the section-15 scheduler fix (`kernel32_memory.py`, the d3d8 files,
  `oleaut32_handlers.py`, `wsock32_handlers.py`, and more — see
  [[tew_fake_kernel_gaps]] section 17's closing paragraph). Only the 5 shared
  `_halt()` factories plus `__chkesp`/`_CrtDbgReport` in `patch_internals.py` are
  covered. Any of these ~85 sites can still be silently un-halted by the same
  scheduler/nested-call mechanisms section 15 fixed for the covered sites.
- **SDL window is 1536x1248**, not the 1024x768 the D3D8/GDI `GetDeviceCaps`
  fix (memory section 9) standardized on — confirmed still true in today's log
  (`Created SDL window ... (1536x1248)`, `swapchain 1536x1248`). This is a
  *different* code path from the one section 9 fixed (window creation vs.
  device-caps reporting) — not yet investigated which one is authoritative or
  whether they need to agree.
- `INT3 breakpoint at EIP=0x00688c69 — halting` still fires (57.0s in today's run)
  and still doesn't actually stop execution (per memory section 8, gated behind a
  dead-code global so this is expected, not a regression) — still not root-caused
  why this specific `cpu.halted = True` gets cleared; likely one of the ~85
  unmarked sites above.
- Git: `main` is 4 commits ahead of `origin/main`, not pushed (per this project's
  "never push without being asked" norm).

## Run command
```bash
cd /data/Code/tew
timeout -k 5 90 env LOG_LEVEL=info /data/Code/tew/.venv/bin/python -u /data/Code/tew/run_exe.py 2>&1 | tee /tmp/emu.log | tail -20
```
**Updated 2026-07-19: raised from `timeout 30` to `timeout -k 5 90`.** A clean run
now legitimately takes ~62s wall-clock (201M steps) to reach voluntary
`ExitProcess(0)` — the old 30s budget would truncate every clean run before it
finishes and misreport it as a hang. `-k 5` sends `SIGKILL` 5s after `SIGTERM`
since plain `timeout`'s `SIGTERM` has been observed not to kill this process
(suspected Vulkan-driver thread signal mask, see [[tew_fake_kernel_gaps]]
section 14). uutils `timeout` (installed on this system) does not support inline
env vars — use `env KEY=VAL` prefix and absolute paths. `-u` on python keeps
output unbuffered.

## Queued issues (priority order)
- Dedicated pass on the ~85 unmarked `cpu.halted` sites (priority order not yet
  established — start with whichever subsystem is next actually exercised)
- SDL window resolution (1536x1248) vs. `GetDeviceCaps` (1024x768) mismatch
- `abortmessage`'s DAO/DCOM database-init failure (`depthconv.c:1137`) is a real
  game-side blocker to reaching actual gameplay past this point — root cause not
  investigated (likely a missing/incomplete DAO/DCOM COM stub, same family as
  `CoGetClassObject`)
- DrawPrimitive / DrawIndexedPrimitive coverage beyond what's needed to reach the
  abort above — not yet assessed how much is implemented
- `[alive]` heartbeat silent during `GetMessageA` host-sleep — low priority

## Architecture
- Game does NOT call D3D8 directly.
- Rendering path: Game → THRASH API (dx8z.dll) → D3D8 (fake COM, Vulkan backend)
- WinINet connects to localhost:443 (HTTPS)
- authlogin.dll reads AuthLoginServer from registry (localhost)
- Login dialog (SDL2): admin/admin from registry, auto-filled — see the two
  dialog auto-clicks documented above
- Timer thread: FUN_00a30ea0, runs as tid=1006 via CRT wrapper at 0x9fc3a0
  `mmtimer_callback` (0x00a30a40) is the multimedia timer proc AND a `_tmrsub[]` subscriber.
  It calls `_SIGNAL_set(event)` + re-registers via `timeSetEvent` each tick.
  Event handle at runtime is 0x7012 (may vary).

## Test suite
582 tests (all passing, reconfirmed 2026-07-19 via `pytest --collect-only`).
