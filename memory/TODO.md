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

## D3D8 render states are never applied (found 2026-10-02)
`Dev::SetRenderState` is `_ok` (returns S_OK, applies nothing); the pipeline is
fixed SRC_ALPHA/INV_SRC_ALPHA, no depth test. Probe of one trade-in frame
(600 draws): 319 draw with ZENABLE/ZWRITE on (the 3D car), 125 additive
(SRCALPHA,ONE), 49 multiply (DESTCOLOR,ZERO). Probable cause of parts drawn
in the wrong order (steering wheel outside the car) and wrong glows/darkening.
Needs: track states, depth buffer cleared by `Clear`, ZFUNC, blend factors per
draw (pipeline variants). Until then SetRenderState should not claim success.
Also `DrawPrimitive` skips PrimType 6 (fan, ~870/run) and 2 (line list,
~5300/run) but returns S_OK; `DrawIndexedPrimitive`/`UP` just halt.

## DealerTradeIn dialog: black screen, buttons off the frame
Per-frame probe: dim layer (alpha 84, `GDialogs.gui` `mColor=[84,0,0,0]` is
ARGB) arrives intact and blends right, so it is not the black. Dialog frame
draws at ~(222,118) 356x363, not the `[409,122]` gui_begin logs (bounds there
are pre-layout). OK/Cancel images draw at y=481 = the frame's bottom edge:
fits `GUI::Layout` (0x00aebe20) doing `y = Bottom - height` with height 0, not
proven. Next: framebuffer screenshot of that frame; log bounds after layout.
Ghidra: `GUI` struct (452 B) lacks the x/y fields at +0x64/+0x68; derived-class
`this` is typed GUI, so check the owning class before trusting an offset.

## Rendering gaps
- SDL window steals input focus while drawing (can't click elsewhere). Only
  creation calls `SDL_RaiseWindow`; suspect repeated `ShowWindow` ->
  `SDL_ShowWindow` (user32_handlers.py). Log its calls first.
- FEUI dialog backgrounds don't draw (Exit dialog shows only its buttons).
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
`Unknown opcode: 0xXX at EIP=...`.

## Game debug output (`dprintf` 0x00a34c40) is gated off
Gates: `_winmsgdebugflag` (0x016f3658) >= level; channel byte at
0x01282a1c + channel*2 bit 0; sink table at 0x01282ebc (12-byte entries,
unexamined). Poke them from tew to see the game's own debug text.

## Latent: `bAlertable` ignored in WaitForMultipleObjectsEx / SleepEx
Harmless while no APC source exists (QueueUserAPC, ReadFileEx, WriteFileEx
are unimplemented). Wire it in if any of those are added.

## Auto-clicks after the Mayor's letter use fixed 30s delays
CONTINUE, Screen Tips X and its OK each fire 30s after the previous click
(~90s of padding to the lobby). Trigger each on a log/stdout line instead,
like the first two clicks.

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

## `PSimWag_GetWagInfo: unhandled exception` = RunEngSim writes nothing
The catch(...) in DBParts_FillVehicleInfo (0x0095d250) swallows a game assert
(INT3 in _Nfs_DebugBreak 0x00688c68, STATUS_BREAKPOINT): the torque curve at
car+0x80 is all zero. Hit #1 `iPeakT > 0` dyno2000.c:1146 (FUN_00526340,
caller 0x0052648d); dealer cars hit #2 `MaxTorque > 0.f` pSimPart.c
(PSimPart_CrossFlowPipe 0x006f2440). Curve = RunEngSim output copied in
Dyno2000_RunDyno (0x00522fa0). Probe: the DDYNO2000 struct (EBP-0x3EC, 0x3EC
bytes) is byte-identical before/after RunEngSim (0x00536e90, thunk 0x0040b203,
call 0x00523456) for the first car AND a clean dealer Buick, inputs sane (bore,
stroke, compression, cam, flow tables match the MDB Physics rows). FPU CW is
0x133F (normal). So RunEngSim takes an early exit / never writes in tew. Next:
find its early-exit condition and any unimplemented instruction under it.
Ruled out: part tree/attachments (Part rows match StockAssembly), m80 FSTP/FLD
(was a real bug, fixed on tew-cpu branch fix/x87-m80-store-load, uncommitted).
Dealer data ("He's got cars!" onward in dblog.txt) is the clean source; Part and
Vehicle rows are game-written and untrusted.

## Test helper: lightweight scheduler mock
For queue/packet tests that only need `current_idx`/thread status.

## Server-side (Molly's), not tew
Lobby headline ticker shows a raw 301 body; "Avg. Player Level: 83,886,080".
