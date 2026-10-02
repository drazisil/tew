# TODO

Open items only, a few lines each. Done work goes in changelog.md (one line);
detail lives in git history.

## Perf (remaining after tasks 1-4)
- D3D8 `UnlockRect` -> `_convert_to_bgra8`: ~30% inclusive once textures load.
- `simple_alloc` (`tew/api/_state.py`): free list is never coalesced or
  trimmed, so every alloc walks a growing list.
- sprintf / `_write_cstring` / write8 write byte by byte (use bulk
  write_bytes); `eip`/`eflags`/`get_flag` crossings ~3% each.

## Nested `_invoke_emulated_proc` callers still rewind on step exhaustion
`DispatchMessageA` moved to a stack trampoline (PR #31). Still nested with a
step budget + `restore_state` rewind: timer callbacks (timeSetEvent), message
hooks, CreateDialogParamA's WM_INITDIALOG, CreateWindowEx's creation
messages, `patch_internals.py`, `exception_diagnostics.py`, DllMain calls.
Move any that can run long or block to the trampoline pattern.

## `cpu_add_logpoint` / `cpu_add_breakpoint` silently drop past 8 slots
Fixed `[8]` tables in `cpu/src/core.zig`; a 9th registration is discarded
with no signal. Return a failure and have `cpu_zig.py` raise.

## Thread stacks have no upper bound
`cpu/src/scheduler.zig` bumps each new stack by 256 KB from 0x08000000 and
never reuses them; past ~512 threads they hit the DLL slots at 0x10000000.

## D3D8 render state: only blending is applied
PR #48 tracks SetRenderState/GetRenderState and builds a pipeline per
(ALPHABLENDENABLE, SRCBLEND, DESTBLEND). Still not applied: depth buffer +
ZENABLE/ZWRITE/ZFUNC (319 of 600 trade-in draws are the 3D car with Z on;
likely cause of parts drawn in the wrong order, e.g. steering wheel outside the
car), cull mode, alpha test. `DrawPrimitive` still skips PrimType 6 (fan,
~870/run) and 2 (line list, ~5300/run) but returns S_OK (missing car parts);
`DrawIndexedPrimitive`/`UP` just halt. A partial `Clear` outside BeginScene is
logged and skipped (no render pass to clear a sub-rect in).

## Rendering gaps
- In-game 3D is badly warped: the track and pit-lane ground are sheared wedges
  with black voids, the grandstand roof is faceted, the hood/car body has
  misplaced polygons. Changes with camera angle, so suspect projection/clipping
  or missing depth test, not the skipped `PrimType` 2/6 draws (those leave
  holes). Trace one large ground triangle's vertices through DrawPrimitive.
- In-game Options (Controls > Assign Functions) panel has no background: the
  car and track show through, so the controller-mapping list text is unreadable.
  Same family as the FEUI dialog backgrounds below.
- SDL window steals input focus while drawing (can't click elsewhere). Only
  creation calls `SDL_RaiseWindow`; suspect repeated `ShowWindow` ->
  `SDL_ShowWindow` (user32_handlers.py). Log its calls first.
- Persona-select highlight bar overdraws the list's column divider. Trace
  the quad's DrawPrimitive before touching blend state; never enable the
  swapchain alpha write mask (the window goes transparent).
- Logo cursor may not animate (low priority; may resolve as a side effect).
- HOME avatar seen side-on: a few facets shade wrong (gray patch on the white
  shirt below the shoulder, mismatched upper-arm facet); the rest of the model
  is fine, so suspect per-triangle normals/UVs, not global state. Needs an XP
  side-on screenshot of the same persona to confirm it's tew.

## `SetWindowPos` / `MoveWindow` don't touch the SDL window
They update `WindowEntry` bookkeeping and return TRUE; only CreateDevice
resizes the real window. Not seen causing a bug yet.

## Rare `hMutexNfsRunning` assert at ~41s (seen once)
`Nfs_exitCallback` asserts with `_hMutexNfsRunning == NULL`. Unreproduced.
If it recurs: find the CreateMutex site and what starts the exit sequence;
check tew's CreateMutex/CloseHandle.

## Missing opcodes (fix when one shows up)
DAA/DAS/AAA/AAS, MOV Sreg (0x8E), AAM/AAD/SALC, far CALL/JMP/RET, BOUND,
ARPL, INTO, IRET, port I/O, CLI/STI. Any that runs reports
`Unknown opcode: 0xXX at EIP=...`. x87: FLDENV, FNSTENV, FRSTOR, FNSAVE, FBLD,
FBSTP now fault loudly (tew-cpu 0.3.2, PR #4) instead of silently doing
nothing; implement the one that trips.

## Game debug output (`dprintf` 0x00a34c40) is gated off
Gates: `_winmsgdebugflag` (0x016f3658) >= level; channel byte at
0x01282a1c + channel*2 bit 0; sink table at 0x01282ebc (12-byte entries,
unexamined). Poke them from tew to see the game's own debug text.

## Latent: `bAlertable` ignored in WaitForMultipleObjectsEx / SleepEx
Harmless while no APC source exists (QueueUserAPC, ReadFileEx, WriteFileEx
are unimplemented). Wire it in if any of those are added.

## Auto-clicks after the Mayor's letter use fixed 30s delays
CONTINUE, Screen Tips X and its OK, then the Racing menu, its Test Drive item
and the TEST DRIVE button each fire 30s after the previous click. Trigger each
on a log/stdout line or a `gui_begin` event instead, like the first two clicks.
The menu item position (446,141) and TEST DRIVE (274,396) were read off
screenshots; the menu is built in code, so there is no GUI-file source.

## Automation events: gaps before they can drive navigation
`gui_begin`/`gui_exit` (tew/automation) give screen names, the widget tree
(class, name, bounds) and exit codes. Still missing:
- **Absolute position.** `mBounds` is parent-relative (`jumpCarsales` is
  `[9, 2] 56, 12`). Add `parent` (GUI+0x38; `GUI::GetParent` returns it) to the
  events and a subscriber that tracks the live tree (begin adds, exit removes)
  and sums ancestor x/y. Unknown whether scroll offsets also apply.
- **String-table labels.** A GUIStr with a null buffer holds a text id at +8
  (`jumpCarAuc` = 0x0d37; the length matches the label), resolved only once
  drawn. Print `#0xd37` instead of the `?(...)` fallback; map ids via text.eng.
- **Safe-to-click signal.** Nothing says a screen accepts input (why clicks wait
  a fixed 30s). Candidate: no modal `Generic` GMsgBox open (they bracket network
  connects) and the target FEInterface has begun; maybe the first idle event.
- **Names aren't unique.** Ten .gui files define an `<EDIT>`; look widgets up by
  parent/screen plus name, not name alone.
- **Visibility / enabled / z-order** (GUI.mStyle +0x3c, mZOrder +0x40,
  mTabOrder +0x44) aren't captured.
- **Box wording.** `GText` is in LOG_SKIP_CLASSES, so message-box text isn't
  visible in the log; emit it for GMsgBox children if it's needed.
- **First consumer.** From Home click `jumpCarsales` and wait for the Dealer
  FEInterface to begin; then replace the fixed click delays with events.
- `gui_begin` hooks GUI::OnBegin (0x00aec5e0); an override that never chains to
  it would be missed. Watch for an exit with no begin.

## DAO duplicate-key INSERT into Vehicle (file for later)
`dblog.txt`: `DAOERROR (3022) ... would create duplicate values in the index, primary
key` on `INSERT INTO Vehicle ( VehicleID, SkinID, Flags, Class, InfoSetting )
VALUES ( 1, 158, 0, 0, 0 )` (`Dbcode_TmpActionQuery` fails), then
`DBPart_ModelData_PUTCACHE: veh: 1 EMPTY`. Unknown whether the game expects the
row to exist already (stale ~/.emu32 db, or the DB persisting between runs) or
tew's Jet/DAO emulation reports 3022 wrongly. Not yet investigated.

## Test helper: lightweight scheduler mock
For queue/packet tests that only need `current_idx`/thread status.

## Server-side (Molly's), not tew
Lobby headline ticker shows a raw 301 body; "Avg. Player Level: 83,886,080".
