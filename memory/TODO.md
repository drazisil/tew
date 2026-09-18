# TODO

Durable follow-up work, checked/updated as picked up. Distinct from status.md
(current session's active blocker) and changelog.md (completed work) --
items here are queued but not yet started, or started and paused.

---

## NEW (2026-09-18): ~28 more real x86 opcodes still missing from `dispatch_table`, silently falling through to `opFault`

Found via a full enumeration of `cpu/src/engine.zig`'s `dispatch_table`
(excluding loop-populated ranges `0x40-0x5F`/`0x70-0x7F`/`0x90-0x97`/
`0xB0-0xBF` and real prefix bytes) while root-causing the `DBRES_Login`
crash -- see status.md's current entry. `0xD0` (SHR AL,1 etc.) and `0x34`
(XOR AL,imm8) were the two real gaps fixed that session; these are not:
`0x27`/`0x2F`/`0x37`/`0x3F` (DAA/DAS/AAA/AAS, BCD adjust -- rare in modern
codegen), `0x8E` (MOV Sreg,rm -- segment register load), `0xD4`/`0xD5`/
`0xD6` (AAM/AAD/SALC; **`0xD7` XLAT was fixed 2026-09-18** -- it turned out to be in the CRT's `__trandisp2`, behind `fmod`/`atan2`),
`0x9A`/`0xEA` (far CALL/JMP -- plausible if any anti-debug trick uses
segment switching), `0x62`/`0x63`/`0x82`/`0xCA`/`0xCB`/`0xCE`/`0xCF`/
`0xE4`-`0xE7`/`0xEC`-`0xEF`/`0xFA`/`0xFB` (BOUND/ARPL/far RET/INTO/IRET/
port I/O/CLI/STI -- mostly rare or privileged in usermode compiled code).

Not urgent to fix preemptively -- the 2026-09-18 `unknown_opcode`/
`last_instr_eip` diagnostic (see status.md) means any of these that
actually fires will now report cleanly as `Unknown opcode: 0xXX at
EIP=...` instead of masquerading as a wild-pointer crash, the way `0xD0`
did for a full session. Worth a dedicated pass if any of them ever
actually shows up in a real run's log.

---

## NEW (2026-09-18): the game asserts `screen.c(475) width>=0&&height>=0` right after the lobby loads -- `FeDC::DoClip` hands `Screen_SetClip` an inverted rect

Reached only now that clicks, `DBRES_Login`, `strpbrk` and the FPU panic are fixed. ~110s after the port-43300 connect (lobby up: `LFrame.cpp Refreshing Buddy List`, `INet_Mail Calling Mail Poll`) the game prints `ASSERT: screen.c(475) width>=0&&height>=0` (in `~/.emu32/MCity/stdout.txt`; no `except.txt`, inline `_Nfs_DebugBreak`) and tew halts on the unhandled `INT3` at `0x00688c68` -- correct per the standing no-auto-continue rule. Chain (crash JSON): `Screen_SetClip(x,y,w,h)` `0x0073d4b5` <- `FeDC::DoClip` `0x0053e089` (intersects the widget rect with `GUI_ViewRect` via `GRect::operator&`, `ViewToScreen`, and if it differs from the cached rect at `this+0x7cc` calls `Screen_SetClip`) <- `0x543a77` <- `0xb43285` <- `0xaed019` (`GUI::OnEvent`). Meaning: a widget's rect does not overlap the view rect, so the intersection is inverted.

**Next steps (not started)**: log `GUI_ViewRect` and the widget rect at `FeDC::DoClip` (entry `0x0053e0..`; args are `this` in ECX) to see the real numbers, and work out which input comes from tew (window/client size, `GetTextMetrics`/font metrics, D3D8 viewport, `GetSystemMetrics`). Real game code is assumed correct, so the divergence is expected to be tew's. Also still unverified live: whether the `XLAT`/`FISTP`/`FSCALE` fixes carry the animation (`fmod`) path, which the run would have reached ~25s after this halt.

---

## NEW (2026-09-18): the game's own `dprintf` debug output is almost entirely swallowed by three gates -- only an entry logpoint exists so far

Found while tracing the click chain in Ghidra. `FUN_00780d80` (the
`WM_*BUTTON*` handler, wParam `MK_*` bits -> `button_mask` -> `seteacmouse`)
calls `dprintf(&_winmsgdebugflag, 2, "lib_mbutton")` first, and that message
never appears anywhere. Full path (all confirmed by decompile):

    dprintf(int *flag, int level, fmt, ...)          00a34c40
      if (flag == NULL || level <= *flag)             gate 1: _winmsgdebugflag @ 016f3658 (needs >= 2 here)
        vsprintf(buf) -> _DEBUG_trace(buf)            game's own static CRT vsprintf @ 009f4d30 (guest code, not tew's msvcrt handler)
          __vsnprintf -> _PRINT_string(2, text)       re-formats the already-formatted text (a stray '%' would garble it)
            if (byte @ 01282a1c + channel*2) & 1      gate 2: channel 2 reads 0x24 @ 01282a20 -> bit 0 clear, looks DISABLED (single-byte read, not double-checked)
              vsprintf again; call each enabled sink   gate 3: table @ 01282ebc, 12-byte entries {callback, flags, ?} -- not examined

**Done 2026-09-18, then DELETED the same day (click investigation resolved; re-add from this description if needed)**: `_dprintf_entry_probe` (`run_exe.py`, logpoint at
`0x00a34c40`, uses 1 of the 8 logpoint slots) logs level, `*flag`, and the
RAW format string at entry, before any gate; bounded to the first 3 hits
per distinct fmt string. Args are not expanded (`%d` stays `%d`).

**Not done (todo)**:
- Formatted-text tap: a logpoint on `_DEBUG_trace` entry sees the already-
  formatted string, but only for calls that pass gate 1. Or open the gates
  from tew: poke `_winmsgdebugflag` (016f3658) >= 2, set bit 0 of the channel-2
  byte (01282a20), and identify/enable a sink in the 01282ebc table.
- Read the sink table (gate 3) -- what the callbacks actually do (debugger
  output? file? on-screen console?) decides where a re-enabled message would land.
- (ANSWERED 2026-09-18: `lib_mbutton` fired for BOTH real and synthetic clicks, so the handler was always reached; the difference was wParam's `MK_LBUTTON` bit -- see the resolved synthetic-clicks entry. Kept for reference.) The decisive click test using this: does `"lib_mbutton"` show up in
  `[dprintf-probe]` for a REAL click and NOT for a synthetic one? If so,
  synthetic clicks never reach `FUN_00780d80` at all (bug is earlier than
  wParam). If it shows for both, the handler runs and wParam's `MK_LBUTTON`
  bit is the suspect (see the synthetic-clicks entry below). A direct wParam
  logpoint at `0x00780d80` (`[ESP+0x10]`=wParam, `[ESP+8]`=hwnd, `[ESP+0x14]`=
  lParam) settles that independently; also not added yet.
- `FUN_00780d80` also gates on `DAT_016f3628 == param_2` (presumably the
  registered mouse hwnd) -- not verified.

Side note from the same session: the `BTS` crash (fault EIP `0x009f3ffd`,
real instruction at `0x009f3ffb`) is inside the game's own STATIC CRT
(`vsprintf` is `009f4d30`, same 0x009fxxxx neighbourhood) -- the bytes
`8a 06 0a c0 74 0a 46 0f a3` look like a `strspn`/`strcspn`/`strpbrk`-style
char-set bitmap loop, i.e. plain CRT string code, not game logic.

---

## RESOLVED (2026-09-18): synthetic clicks never registered -- `wParam` lacked `MK_LBUTTON` because `SDL_GetMouseState()` ignores `SDL_PushEvent`-injected events (original write-up kept below)

**RESOLVED 2026-09-18.** The hypothesis below was correct and is now measured: a guest logpoint on `FUN_00780d80` showed real `WM_LBUTTONDOWN` `wParam=0x1` vs synthetic `wParam=0x0`, every other field identical (hwnd `0x1034`, lParam (387,491), message `0x201`, the `DAT_016f3628` gate). Fixed in `tew/api/window_manager.py` by tracking pressed buttons from the events themselves (`_mouse_buttons_down`), never from `SDL_GetMouseState()`; 8 tests in `tests/unit/api/test_window_manager_mouse_wparam.py`. Live-confirmed: a synthetic START click now logs in (port 43300, lobby user list). Also fixes a latent race for REAL clicks (`SDL_GetMouseState()` is poll-time state; under emulator lag a quick click could already be released). Corrections to details below: `MouseSetButton`'s first arg is a real button index (0..3 over `rgbButtons[4]`), not a `GInput` type (those are 2/3/4); the guest's mask bits are `1/2/4` (left/right/middle) remapped from `MK_LBUTTON 1/MK_RBUTTON 2/MK_MBUTTON 0x10`. The click-investigation probes were deleted after this.

Original write-up:


**The actual root cause of "clicks don't work," found via live logpoints +
a side-by-side real-vs-synthetic click on the same running process.** Full
chain traced in Ghidra: `MMouseInput::AppPollMouse` (0075fae0) calls
`_MOUSE_getstate(6)` (00a72d20), confirmed live via logpoint to run in mode
`DAT_0128af04==4` (the `getmousepos()`/00a73a60 absolute-position path, not
DirectInput's buffered `_INPUT_getdevicedata`). `getmousepos()` just reads
3 globals (`DAT_020e398c`=buttons, `DAT_020e3990`/`3994`=x/y) written by
exactly one function, `seteacmouse` (00a73b90) -- confirmed via Ghidra
XREFs, no other writer exists. `seteacmouse` gates its entire body behind
`_mouseflag != 0 && _winmsgmutex != NULL`; if either is unset it silently
no-ops on every call.

**Live-confirmed** (logpoint on `GMouseInput::MouseSetButton`, 0x00b1b2c0,
the only writer downstream of `AppPollMouse`): a real manual click (Molly,
same running process) produced `state=112` sustained across 10+
consecutive polls for the whole real hold duration. Every synthetic click
attempted the same session (392 logged polls total, multiple attempts,
various hold durations/wiggle patterns) produced `state=0`, always. This
rules out `_mouseflag`/`_winmsgmutex` init as the problem (real clicks
clearly work on the same live process) -- the divergence is specifically
synthetic vs. real input, upstream of `seteacmouse` itself.

**Most likely mechanism (not yet fixed or further verified)**:
`window_manager.py`'s `_handle_sdl_event` computes
`wparam = _sdl_buttons_to_wparam(SDL_GetMouseState(None, None))` for
`WM_LBUTTONDOWN`/`WM_LBUTTONUP`. `SDL_GetMouseState()` reads SDL's own
internally-tracked mouse-button state, updated via `SDL_SendMouseButton` --
the same internal call real hardware-driven events go through. Our
synthetic clicks build a raw `SDL_Event` and call `SDL_PushEvent()`
directly, which enqueues the event but does **not** call
`SDL_SendMouseButton` -- so SDL's own tracked button state never updates
for a synthetic press, and `SDL_GetMouseState()` keeps reporting "nothing
down" even during a synthetic button-down. That would make the computed
`wparam` always miss `MK_LBUTTON` for synthetic events specifically -- a
clean explanation for the real-vs-synthetic split just observed, though not
yet directly confirmed by reading the live wParam value itself (cheap next
step: one more logpoint/log line at the wparam computation site, real vs.
synthetic).

**Fix direction (not yet implemented)**: stop deriving wParam's button bits
from `SDL_GetMouseState()`; track pressed-button state directly inside
`window_manager.py` from the events `_handle_sdl_event` itself already
processes (real and synthetic both flow through this one function), and
compute `wparam` from that self-tracked state instead. This would very
likely also explain every prior click-delivery investigation this project
has done (the 2026-09-14 "OnMouseUp never entered" dead end, the whole
multi-session click-coordinate saga) -- none of those synthetic clicks
would have carried correct wParam either, regardless of how correct their
coordinates were.

See `status.md`'s 2026-09-17/18 entry and `status_archive.md` for the full
session narrative (manual click-trigger API, `GetCursorPos` fix, the
research-agent history dig, and this live logpoint chain).

**Ruled out 2026-09-18**: confirmed unrelated to the `0xD0`/`0x34` CPU
opcode gaps fixed the same session -- a synthetic DOWN still shows
`state=0` throughout the hold with the fixes in, and the new
`unknown_opcode` diagnostic never fires during a click attempt. The
`SDL_GetMouseState()`/`SDL_PushEvent` mechanism above remains the real,
still-unfixed lead.

---

## RESOLVED (2026-09-18, this entry's 2026-09-17 conclusion was WRONG -- corrected below): real crash right after a live `MC_LOGIN_COMPLETE` -- a genuine tew CPU-core bug (missing 0xD0 opcode), not a server payload issue

**Correction (2026-09-18): everything below the original write-up is wrong.**
This entry originally concluded "not a tew bug, fix belongs in mco-server's
`LoginCompletePayload`" and recommended adding shard/server-list fields
server-side. **Do not act on that.** The crash was re-verified live with
the heap-fill fix in place (see status.md's 2026-09-17 entry) and
reproduced again at the exact same fault site, with no server change --
ruling out the payload-size theory entirely. Real root cause, found the
same session Molly decoded the actual bytes at the fault EIP in Ghidra:
`0x0099ed78` is `D0 E8` (`SHR AL,1`), and `cpu/src/engine.zig`'s
`dispatch_table` never had `0xD0` wired in (`0xD1`/`0xD2`/`0xD3` were, `0xD0`
was skipped) -- a genuine missing-opcode gap in tew's own CPU core, not a
wild pointer and not a server bug. Fixed by adding `opD0`. **Live-confirmed
2026-09-18**: a fresh run sailed straight through `MC_LOGIN_COMPLETE` ->
`DBRES_Login` into persona-physical + `MC_GET_OWNED_PARTS` with zero faults.
See status.md's current entry for the full writeup, including why `opFault`
never reporting which opcode triggered it is what let this go unnoticed,
and the resulting ~29-opcode coverage audit.

Original (now-superseded) write-up kept below for the false-lead detail --
the `0x4d980f`-vs-real-fault-EIP confusion it describes is still real and
worth knowing, just not the actual root cause.

On the click-repro run that live-confirmed the `_recv`/`_select` fixes,
right after `dblog.txt` shows a genuine `DBServiceResultQ msg #213
MC_LOGIN_COMPLETE Seq:3`, `tid=1011` faulted inside `DBRES_Login`
(`DBResultQ.C`, reached via `DBServiceResultQ`'s indirect message-dispatch
table -- invisible to static Ghidra XREF, which is why naively chasing the
logged fault address's "only caller" led to the wrong function entirely).
Crash JSON's real `eip`/`ebp_chain` (7 clean frames to `THREAD_SENTINEL`)
put EIP genuinely inside `DBRES_Login`; `memory_access.attempted_address`
was `0x4d980f` -- the unrelated `_CLayer_DetectDebugger`'s entry point -- a
wild/corrupted pointer read, not a deliberate jump.

**Root cause, confirmed against mco-server's own log**
(`/data/Code/server/data/application-2026-09-17-12.log`, port 43300,
connectionId `43fcb52f`): the server's `LoginCompletePayload` response body
is only 72 bytes, but `DBRES_Login` unconditionally reads fixed struct
fields out to offset `0xa9+4 = 173` bytes into that buffer -- no length or
bounds check, matching the real original client's expected (longer)
message format. The server's own decoded log shows `serverList: ` empty --
it never fills in the shard/server-list entries `DBRES_Login` expects
(it later loops over what should be 4 server-list entries using fields read
from those far-past-buffer-end offsets). **Not a tew emulation bug** --
nothing to fix here; the fix belongs in mco-server's `LoginCompletePayload`
generation (add the missing shard/server-list fields to match the real
client's expected fixed-size struct). See `status_archive.md`'s
"2026-09-17, later still" entry for the byte-for-byte comparison.

**False-lead trap hit once while root-causing this**: the log's own
`[exception] CPU fault at EIP=0x004d980f` line is NOT the fault EIP --
that's the `memory_access.attempted_address` field from the crash JSON; the
real fault EIP (`0x0099ed78`) is a separate field. Don't re-chase
`0x4d980f`/`_CLayer_DetectDebugger` (WinMain's one-time debugger self-test,
normally SEH-caught, unrelated) as a crash site again -- check `eip` in
`/tmp/emu_crash.json` directly, not the log line.

---

## RESOLVED (2026-09-16, fixed later same day): guest `recv()` blocking with no data ready freezes the ENTIRE emulator, not just the calling guest thread

`tew/api/wsock32_handlers.py`'s `_recv` (~line 459) calls the real host
socket's blocking `entry.py_sock.recv(length)` directly. Guest "threads"
have no real host OS thread each -- `CreateThread` doesn't spawn one;
they're cooperatively scheduled via `crt_state.scheduler.preempt_slice(cpu,
mem)` on tew's single shared CPU-stepping host thread (same thread
`run_exe.py`'s one `[alive]` log site lives in). So when any guest thread's
`recv()` has no data ready, it blocks that single shared thread -- freezing
every other guest thread and the heartbeat log too, not just the caller.
Confirmed live 2026-09-16: after a successful DB-attach handshake on a new
MCOTS connection (port 43300), the whole run went completely silent (no log
growth anywhere) for 1000+s until killed by our own timeout -- root-caused
via `/proc/<pid>/task/*/syscall` showing the main host thread parked in a
real blocking `recvfrom`, confirmed via `ss -tin` that this was NOT stuck
unread data (`Recv-Q:0`) but a genuine wait for a message that never came.
See `status.md`'s 2026-09-16 entry for the full trace. Fix direction: make
the host socket non-blocking (or poll with a short timeout cooperating with
`preempt_slice`) so one guest thread's network wait yields instead of
wedging the whole machine.

Also noticed while in `_recv`, lower priority: it still writes received
bytes into guest memory one byte at a time (`for i, b in enumerate(data):
memory.write8(...)`, ~lines 476-477) -- the same per-byte anti-pattern
already fixed elsewhere (`_heap_alloc`, `sendto`'s read, 2026-09-14) but
missed here. Bulk-write while fixing the blocking issue above.

**Both fixed and LIVE-CONFIRMED (2026-09-17)**: `_recv` now does a
zero-timeout `select()` check and, for a guest-blocking socket with nothing
ready, yields via `state.scheduler.sleep_current(cpu, memory, retry_eip, 0,
5)` (same retry-via-rewound-EIP pattern as `WaitForSingleObject`) instead
of calling the real blocking `recv()`; the per-byte write loop is now
`memory.load(...)`. A sibling bug in `_select` (same file) had the
identical disease -- fixed the same way, tracked per-thread since one
`select()` call spans multiple sockets. Reproduced the exact original
scenario live (real MCOTS connect to port 43300, `recv()`/`select()` both
finding nothing ready) and the process stayed fully alive with every other
thread continuing normally -- no more `poll_schedule_timeout`/`recvfrom`
host freeze. See `status.md`'s current entry and `status_archive.md`'s
2026-09-16/17 entries for the full trace.

---

## NEW (2026-09-13): persona-select list's selection-highlight bar overdraws the PERSONA/SERVER-vs-POP. column divider

On the persona-select screen, every unselected row shows a visible
vertical divider between the Persona/Server list and the POP. list. The
selected row's black highlight bar paints straight over that divider --
looks like the highlight quad is wider than the Persona/Server list
control's actual bounds, not a missing-alpha/blending issue (RGB
blending is already on, see the 2026-09-05 session). Deliberately not
investigated yet -- deferred by Molly 2026-09-13 ("if it's broken, it
won't be the only instance"), and not yet known whether POP. is even
supposed to be part of the same highlighted selection. Don't touch the
swapchain's alpha colorWriteMask to "fix" this -- that's deliberately
excluded to prevent the window itself going transparent to the desktop
(see `_pipeline.py`'s `blend_attach` comment). If picked up: trace the
actual `DrawPrimitive` call for this quad (vertex rect + diffuse color)
before changing any blend state.

---

## NEW (2026-09-13): `SetWindowPos`/`MoveWindow` are lying no-ops for the real SDL window -- resize/move requests after `CreateDevice` are silently dropped

`user32_handlers.py`'s `_SetWindowPos` and `_MoveWindow` only update the
internal `WindowEntry.x/y/cx/cy` bookkeeping and unconditionally return
`TRUE` -- neither ever calls `SDL_SetWindowSize`/`SDL_SetWindowPosition`
on `entry.sdl_window`. The only place the real SDL window is actually
resized today is `idirect3d8.py`'s `CreateDevice` (the swapchain
resolution fix from the 2026-09-05 persona-select session, see
`window_manager.py:350`'s comment) -- a one-time resize at device
creation, not an ongoing sync. Any resize/move the game requests
afterward via these two APIs (or `SetWindowPlacement`, currently
entirely unregistered and would fatal-halt if the game called it) claims
success but has no visible effect on the real window. Not yet known
whether MCity_d.exe actually calls either post-`CreateDevice` in a way
that matters (not observed causing a visible bug yet) -- flagged as a
known gap, not a confirmed active blocker.

---

## RESOLVED (2026-09-13): `IDirect3DDevice8::Reset` was a complete lying no-op, clipping the persona-select screen

Found while attempting the mouse/keyboard interaction item below: the
persona dialog rendered larger than the actual window, clipping "PLEASE
SELECT FROM THE LIST BELOW" and the persona list past the right/bottom
edge. Root cause was `Dev::Reset` never reading its
`D3DPRESENT_PARAMETERS*`, never resizing the window, and never
recreating the swapchain -- the game's `setvideomode` takes the `Reset`
path (not `CreateDevice`) for every mode change after the first, so the
window/swapchain stayed frozen at the login screen's size. Fixed for
real: swapchain/image-views/framebuffers now destroyed and recreated at
the new size on every `Reset`, window resized to match (same
`WINDOW_SCALE` as `CreateDevice`). Confirmed live via screenshot -- full
writeup in changelog.md's 2026-09-13 entry. Does not fix the separate,
still-open `SetWindowPos`/`MoveWindow` lying-no-op gap above -- Reset was
the actual mechanism the game uses for mode changes; that item stays
open as an unconfirmed, unrelated gap.

---

## NEW (2026-09-13, corrected): real mouse/keyboard interaction with the persona-select screen -- three real bugs fixed, but click delivery is still NOT confirmed working

**Correction, same session**: this was briefly marked RESOLVED after
clicking "Dr Brown" appeared to advance the game to "Connecting to
localhost:8226 try 1" -- that screen turned out to be the LOGIN server
reconnecting (`LoginServerPort=8226`), not the lobby (`LobbyServerPort=
7003`), and further testing showed it's a periodic automatic refresh
unrelated to any click (same payload repeats ~44s later regardless of
what's clicked). Do not treat that screen transition as evidence the
click worked.

Three real, independently-confirmed bugs were fixed getting here -- see
changelog.md's 2026-09-13 entries for the full chain: (1)
`IDirect3DDevice8::Reset` was a lying no-op clipping the window
(separate RESOLVED entry above), (2) `SDL_MOUSEBUTTONDOWN` never posted
a real `WM_LBUTTONDOWN` to any non-dialog top-level window (only
MOUSEBUTTONUP/MOTION did), and (3) DirectInput's `SetEventNotification`
accepted event-handle registration and then never signaled it. All three
are real and necessary fixes, confirmed via extensive live testing
(SDL reliably delivers real clicks; `pump_sdl_events` runs continuously)
-- but NOT sufficient: most real clicks during the persona-select screen
still produce zero downstream reaction, while clicks during the earlier
login-dialog stage reliably work.

**Real, confirmed next step**: one dropped click was caught directly in
the log (`SDL event type=0x401` with no follow-up) and traced to
`_handle_sdl_event`'s `SDL_MOUSEBUTTONDOWN`/`UP` handlers
(`window_manager.py`) having **silent** early-return paths (non-left
button, or `windowID` not found in `_sdl_window_id_to_hwnd`) -- now
instrumented with logging (both branches, both handlers, plus the actual
`sdl_win_id` added to window-creation log lines) but not yet re-tested
against a real dropped click. Next session: reproduce one and read which
branch actually fired.

Real mouse/keyboard input being "wired into DirectInput" (an even
earlier session's note) meant the vtable slots existed and returned
plausible values, not that any of them were actually being called or fed
by real events -- worth remembering next time a similar "should just
work" input claim comes up.

---

## RESOLVED (2026-09-05, cont'd again): "mistiled/blocky background image" root-caused as `GetRenderTarget`/`GetDepthStencilSurface` fabricating a fresh surface object every call — a real premature-free bug, not a missing D3DFORMAT case

What first looked like a texture-format bug (a 1536x1248 surface,
`this=0x09750000`, flipping from `fmt=0x16` to `fmt=0x4f`/`D3DFMT_D24X4S4`
between `UnlockRect` calls) turned out to be a genuine object-lifetime
bug, confirmed via temporary alloc/free diagnostics correlated
chronologically against `Surface::UnlockRect` calls: `Dev::GetRenderTarget`
and `Dev::GetDepthStencilSurface` allocated a brand-new, independently
ref-counted surface object on *every* call instead of returning a stable,
AddRef'd, cached one. Real D3D8 AddRef's and returns the same underlying
surface every time -- the caller's matching `Release()` only drops their
own reference, since the device keeps its own. Fabricating a fresh object
per call meant the game's single, correct `Release()` immediately freed
tew's only copy of it; the freed heap address was then handed to an
unrelated later allocation, whose write into the object header's format
field corrupted what the still-in-use original surface reported. Proven
directly: a `Surface::UnlockRect` call on that address succeeded 23.8
seconds after tew's own bookkeeping had already freed it, reading stale
leftover memory.

**Fixed**: both accessors now cache one canonical surface object per
device (`_state._vk_backbuffer_surface_obj`/`_vk_depth_stencil_surface_obj`)
and AddRef on repeat calls. Regression test:
`tests/unit/api/test_d3d8_render_target_cache.py`.

The visible "mosaic" itself was a separate misdiagnosis, corrected by
Molly watching the actual game window live: it was real, correctly
loading 32x32 icon content (1201 icon uploads in one run) caught
mid-population, not corrupted output -- it resolved into real content
once loading finished.

Also fixed along the way (real, correct, but confirmed NOT the cause of
this particular bug): `IDirect3DSurface8::LockRect` ignored the `pRect`
sub-rectangle parameter, always returning a pointer to the surface's
absolute origin regardless of which sub-rectangle was requested -- would
cause exactly this kind of tile-patchwork corruption for any surface
genuinely streamed/decoded via repeated `Lock(pRect)`/`Unlock` cycles on
different sub-rects. Regression test:
`tests/unit/api/test_d3d8_lock_rect_prect.py`.

---

## RESOLVED (2026-09-05, full session): D3D8 texture-sampling pipeline built end-to-end, real content confirmed ON SCREEN

Full methodology in changelog.md (2026-09-05 entry) and the rotated
status_archive.md entry. Six real, independently-verified bugs found and
fixed across a single long session, ending with a real textured quad
confirmed visible in a live screenshot (not just via GPU pixel readback):

1. `CreateTexture`/`SetTexture`/`GetTextureStageState`/`SetTextureStageState`
   converted from lying no-ops to real state-tracking implementations.
2. Real GPU texture upload wired into `IDirect3DSurface8::LockRect`/
   `UnlockRect` (confirmed via call tracing to be the actual path the real
   game uses -- the texture's OWN `LockRect`/`UnlockRect` are never called).
3. D3DFORMAT-aware pitch + BGRA8 conversion (`_format_bytes_per_pixel`/
   `_convert_to_bgra8` in `_helpers.py`) -- `LockRect`'s `Pitch` was
   hardcoded to `width*4` regardless of real format, corrupting every
   non-32bpp texture (confirmed live with real `D3DFMT_R5G6B5` textures).
   Handles R5G6B5/X1R5G5B5/A1R5G5B5/A4R4G4B4/A8; DXT/S3TC still unhandled
   (see item below).
4. Window-transparency bug: alpha=0 draws (blend disabled) were writing
   real transparency into the swapchain's alpha channel, letting the
   Wayland compositor show the desktop through the game window. Fixed by
   excluding alpha from the pipeline's `colorWriteMask`.
5. Descriptor-set race: a single shared descriptor set mutated per
   `SetTexture()` call doesn't work, because Vulkan reads descriptor
   contents at command-buffer *execution* time (Present's `vkQueueSubmit`),
   not at record time -- every draw in an unpresented frame sampled
   whichever texture was bound *last* in that frame. Fixed with one
   persistent descriptor set per texture (`_pipeline.py`'s
   `_MAX_TEXTURE_DESCRIPTOR_SETS` pool), resolved and bound per-draw at
   record time in `_draw_primitive`.
6. Same class of bug, for vertex data: `_draw_primitive` always wrote to
   vertex-buffer offset 0, so every draw accumulated in a frame overwrote
   the previous one's vertex data before the GPU ever read any of it. Fixed
   with a per-frame cursor (`_state._vk_vertex_cursor`) giving each draw
   its own buffer region, reset once per new frame acquire.
7. **The actual remaining bug, found only after Molly refused to accept "a
   provably-correct GPU readback" as proof of a working screen**: Vulkan's
   NDC Y-axis points DOWN by default (opposite of OpenGL), but
   `_draw_primitive`'s screen-to-NDC Y math used the OpenGL-style flip
   formula (`1 - y/h*2`) inherited from a GL mental model, rendering
   everything upside-down/off-screen relative to where anyone would look
   for it. Fixed: `yn = (y/vp_h)*2 - 1` (no flip; D3D8 screen-space Y-down
   already matches Vulkan NDC Y-down).
8. A separate, real Vulkan correctness bug found while chasing the above:
   `BeginScene`'s per-frame swapchain-image re-acquire barrier used
   `oldLayout=UNDEFINED` unconditionally -- a real "discard prior content"
   hint some drivers honor literally, wrong for every re-acquire after the
   first (D3D8's `Clear()`, not every frame boundary, is supposed to be
   what erases backbuffer content). Fixed by tracking which swapchain image
   indices have completed at least one frame
   (`_state._vk_swapchain_images_used`) and using `oldLayout=PRESENT_SRC_KHR`
   for every re-acquire after the first.

**Verification chain, strongest to weakest**: (a) a real textured quad
directly confirmed in a live screenshot -- the actual bar Molly held this
session to, not accepted until met; (b) full-screen solid-red `Clear()`
confirmed reaching the actual composited window (proved the presentation
pipeline itself, independent of any draw-content bug); (c) direct GPU pixel
readback (`vkCmdCopyImageToBuffer` to a host-visible staging buffer) showing
real non-clear-color texture data at the expected screen location; (d) full
test suite green (1249 passed) after every change.

**Still open, carried forward**:
- DXT/S3TC/BC1-3 texture decompression -- `IDirect3D8::CheckDeviceFormat`
  still unconditionally returns `S_OK` for DXT1/DXT3 FourCCs
  (`tew/api/d3d8/idirect3d8.py:495`) with no decompression path; the real
  textures traced this session were uncompressed (`D3DFMT_R5G6B5`), so this
  wasn't blocking, but a DXT-compressed texture would currently render
  garbage via the "unrecognized format, pass through raw" fallback in
  `_convert_to_bgra8`.
- Only stage-0 textures are wired to the GPU-visible descriptor set --
  multitexturing (stage > 0) is tracked in `_state._bound_textures` but not
  rendered. Not yet observed to matter in practice.
- `C:\Data\GUI\dlg.options` still occasionally reported missing -- low
  priority, doesn't block rendering.
- DirectSound looks like a real, working implementation -- "no music"
  probably isn't the same class of bug as the texture pipeline; not
  investigated this session.

## NEW (2026-09-05): real guest-code crash around t≈41s, IDENTIFIED but not yet root-caused -- genuinely rare, only reproduced once

`EIP=0x00688c68` decompiles (via Ghidra, `MCity_d.exe`) to `_Nfs_DebugBreak`
-- the game's own deliberate `INT3` assert mechanism, not a random fault.
Its caller (`ebp_chain` depth 0, ret `0x0068b0b2`) is `Nfs_exitCallback`
(`nfspc.c`), which asserts `hMutexNfsRunning != NULL` during the game's own
exit sequence -- `Channel_SystemPrint("ASSERT: %s(%d) %s\n", ...,
"hMutexNfsRunning")` fires immediately before the breakpoint, confirming
this is exactly that assert failing, not an unrelated fault landing at the
same address. `_hMutexNfsRunning` was `0`/NULL when this ran.

Two live possibilities, not yet distinguished: (1) real original-game logic
-- something else triggered the game's exit sequence, and by the time this
callback ran its own tracked mutex handle was already cleared/never set,
a genuine (if rare) bug in the shipped game; (2) a tew bug in the real
Win32 `CreateMutex`/`CloseHandle` handlers that back `_hMutexNfsRunning`.

**Could not reproduce for further live tracing** -- captured once, then 5+
subsequent fresh runs (varying `LOG_LEVEL`/`LOG_CATEGORIES`) all completed
their full timeout cleanly with no crash. Whatever triggers it is timing-
sensitive (coincides with heavy DAO/Jet DB worker-thread activity --
`Tmp.MDB` reads, `DBThread is alive` -- in the one run it fired), and
tew's own thread-scheduler interleaving isn't identical run to run.

Next step (needs a fresh repro, or static-only tracing without one): find
where `_hMutexNfsRunning` is really set (its `CreateMutex` call site) and
what actually calls into the exit sequence at t≈41s, then check tew's
`CreateMutex`/`CloseHandle` handlers (`tew/api/kernel32_*.py`) for a real
bug vs. confirming this is genuine original-game behavior.

## RESOLVED (2026-09-05): D3D8 game window now receives real mouse/keyboard input

Was: no real mouse/keyboard input reached the game at all (see prior
description in `changelog.md`'s 2026-09-05 entry for the full original
finding). Fixed both real gaps:
- `tew/api/dinput_handlers.py`'s `Dev::GetDeviceState` now really polls SDL:
  `cbData==256` is treated as the keyboard (real `SDL_GetKeyboardState` +
  a fixed SDL-scancode -> real `DIK_*` table covering letters, digits,
  punctuation, function keys, arrows, and modifiers); any other `cbData`
  (16/20) is treated as the mouse (`SDL_GetMouseState`, reported as
  DirectInput's default *relative* lX/lY deltas since the last poll, plus
  left/right/middle button bytes). The single generic device object
  (`CreateDevice` doesn't distinguish keyboard vs. mouse by REFGUID) is
  disambiguated this way since `cbData` is the one thing every caller
  always states.
- `tew/api/window_manager.py`'s `_handle_sdl_event` now handles
  `SDL_MOUSEMOTION` (posts `WM_MOUSEMOVE`), `SDL_MOUSEBUTTONUP` (posts
  `WM_LBUTTONUP`), and `SDL_WINDOWEVENT_FOCUS_GAINED`/`_LOST` (posts
  `WM_ACTIVATE`+`WM_SETFOCUS` / `WM_ACTIVATE`+`WM_KILLFOCUS`) to the real
  top-level window, not just tew's own dialog-widget system.

Not yet done: right/middle mouse buttons in `window_manager.py`'s own
message-based dialog path (DirectInput's mouse polling above does report
them); mouse wheel (`lZ`) is not tracked at all. Full suite green (1249
passed) throughout; sanity-checked with a live run (no exceptions from the
new SDL event handling).

## NEW (2026-09-04, evening): possible native fast-path for the highest-volume trivial Win32 calls

## NEW (2026-09-04, evening): possible native fast-path for the highest-volume trivial Win32 calls

Full investigation and root-cause in status.md (rotated to status_archive.md
once superseded). Summary: the ~200s GUI-init delay is genuine, correctly-
emulated work (real Jet/DAO database engine + the game's own polling loop
in `wait_task_executing`/`_SYNCTASK_run`), not a bug -- confirmed via direct
measurement (cpu.step_count ground truth, per-function dispatch timing,
Ghidra decompilation of the actual hot address). Not fixed this session,
by design -- it would need either a faster (JIT-style) CPU core, or:

**Scoped option, not attempted**: `EnterCriticalSection`/`LeaveCriticalSection`
alone account for ~830,000 calls / ~17.4s of dispatch time in one ~217s
window (measured, see status.md step 6) -- by far the highest call volume
of any Win32 API in this profile. Both have simple, well-defined semantics
(a few memory reads/writes against a CRITICAL_SECTION struct, no guest-
visible side effects beyond that struct) and no logging/dispatch-log
requirement that couldn't be dropped for this pair specifically. A native
(Zig) fast-path that resolves these two calls entirely inside `cpu_run`'s
INT-dispatch check -- without ever crossing into Python -- could plausibly
eliminate most of that 17.4s, and would likely also reduce, though not
eliminate (per-call overhead isn't the same as native-execution time),
some of the game's own polling-loop cost since `_SYNCTASK_run`'s callbacks
may use CS internally too (not confirmed -- would need re-measuring after
the fact). Real engineering effort: needs the semantics reimplemented in
Zig and kept in sync with `tew/api/kernel32_sync.py`'s Python version, and
a plan for what happens on the (currently only Python-side) contested/
blocking path.

## NEW (2026-09-04): tew runs killed abruptly (harness OOM-guard, not real memory pressure) can wedge the compositor / stall SDL2 at startup

A run killed abruptly by Claude Code's own low-memory task guard (confirmed
by Molly to be the harness, not genuine system memory pressure -- `free -h`
showed plenty available both times) left the run's own process alive
outside the harness's job tracking, but a *subsequent* run got stuck at
SDL2 window init (main thread parked in `poll()`, virtual time frozen at
1.65s) -- the same compositor-wedge signature documented in emu32's "Stuck
SDL Window" pattern, previously only seen after a `SIGKILL`. A plain
`SIGTERM` sent to the wedged run did not land for several minutes; sending
one real input event via `/dev/uinput` (scratch script, KEY_A press+release)
was followed shortly after by the process finally noticing the signal and
running its own clean SDL2 shutdown path -- correlation only, not confirmed
causation. Needs a real investigation: does Claude Code's background-task
killer send SIGKILL (not SIGTERM) to processes under its job tracking even
when nohup+disown'd, and is there a more reliable way to launch a
long-running tew session that survives the harness's own memory guard
without wedging the compositor.

**UPDATE (2026-09-04 15:29 EDT), likely real root cause found for the
SDL2-stuck-at-startup half of this (not the abrupt-kill-with-no-error half,
which is still unexplained -- see above)**: `~/.config/powerdevilrc` had
`[AC][Display] DimDisplayIdleTimeoutSec=900` (15 min). Two separate stalls
(one at 10:24, one at ~15:11) each froze at SDL2 window init for a duration
matching that 900s timeout, and each was immediately followed by real
progress (not just an exit) right after sending one real input event via
`/dev/uinput` -- confirmed on the second stall specifically (main thread
went from `poll()`-parked to `running`, new Vulkan/SDL worker threads
appeared, vtime jumped from 1.59s to 786.6s within 9s of CPU time, all
*without* sending any signal to the process). Leading theory: `tew`'s SDL2/
Vulkan window-surface creation does a `wl_display_roundtrip()` (see the
`vk_pump` comment in `tew/api/d3d8/_helpers.py`), and KWin stops servicing
that round-trip for a *new* client while the display is dimmed/idle --
existing, already-rendering clients seem unaffected, only first-time window
creation blocks. This would also retroactively explain the older
`status_archive.md` "SIGKILL wedges the compositor" incidents
(2026-07-24, 2026-08-25) as likely the same idle-dim mechanism, misattributed
to the kill itself, since those were also long unattended/AFK runs.
No `journalctl` entries exist for the dim transition either time (KWin
doesn't log it to the system journal) -- this is a strong timing/behavior
correlation, not a confirmed mechanism.

**Test result so far (2026-09-04, evening)**: set `DimDisplayIdleTimeoutSec=0`
via `kwriteconfig6 --file powerdevilrc --group AC --group Display --key
DimDisplayIdleTimeoutSec 0` and restarted `plasma-powerdevil.service` to
apply it live (2026-09-04 15:29 EDT). Since then, roughly a dozen more tew
launches this session (each stopped gracefully via `kill -TERM` and
relaunched fresh, for an unrelated GUI-init-delay investigation -- see
status.md) -- **none** stalled at SDL2 window init, all progressed normally
from launch. This is real supporting evidence but not a full confirmation:
every one of those relaunches followed a *graceful* stop, not the original
abrupt-harness-kill scenario that triggered the stall in the first place --
that specific repro hasn't recurred to retest. If a future abrupt kill also
fails to wedge the next launch, the emu32 skill's "Stuck SDL Window" section
needs a rewrite: the fix is disabling AC display dimming, not avoiding
SIGKILL, and the "do not restart the compositor" guidance may have been
solving the wrong problem all along.

## NEW (2026-08-29): a scheduler mock/test helper is worth building at some point

Noted by Molly: real async coverage (queues, packet handling) is coming up,
and testing that against the full `ZigScheduler` (as `test_invoke_emulated_proc_thread_death.py`
and friends do today) is heavier than most tests need. A lightweight mock
scheduler double, usable wherever a test only needs to control
`current_idx`/thread status without a real cooperative scheduler, would
make that work easier to test in isolation. Not started -- no immediate
blocker yet, just flagged before the queue/packet work begins.

---

## RESOLVED (2026-08-29, cont'd x40): DAO/Jet query-parameter gap -- `StockAssembly_SelectAPT` "could not get param count" -- FULLY FIXED end to end

Full root cause and fix in status.md "cont'd x40" and changelog.md's
matching entry. x39 fixed `GetLocaleInfoW` and a cascade of exposed
locale/calendar table gaps (`_wtoi`, `_itoa`, a mislabeled
`LOCALE_SMONTHNAME1..13` entry, `GetCalendarInfoW`, `NlsGetCacheUpdateCount`,
a silent-stub logging gap in `MultiByteToWideChar`/`WideCharToMultiByte`).
With those closed, `VarDateFromStr` still failed -- traced to
`classify_wide_string` (`char_type.py`) leaving its output buffer
unwritten when asked to classify a null-terminated "string" whose first
character IS the terminator (a zero-length classification), which let a
caller read back stale leftover data (a `DIGIT` flag from the character
tested just before) and made a date-string tokenizer wrongly treat two
null bytes as still-a-digit, overshooting the true end of a number by 2
WCHARs. Fixed by always writing a real classification for the terminator
in that case. Confirmed live end-to-end: `StockAssembly_SelectAPT`'s error
no longer appears anywhere in `stdout.txt`; the game runs straight past
the whole query.

**New, completely unrelated blocker opened immediately downstream**: an
unhandled SEH fault at `EIP=0x1901d9eb` (0x19xxxxxx range -- a different
DLL entirely). Not yet investigated at all -- pick this up first next
session.

## RESOLVED (2026-08-29, cont'd x40): `FUN_77121505` "thread splat" suspicion -- ruled out, real cause was the already-documented `THREAD_SENTINEL` collision

Reran with `thread` added to `LOG_CATEGORIES` per the plan from x39;
`FUN_77121505` never fires at all in the relevant window. What DOES fire
early in every run is the already-documented (see the `THREAD_SENTINEL`
entry below, "NEW (2026-08-26)") spurious "thread died" event from
`OLEAUT32.dll`'s real `DllMain` static initializer sharing `THREAD_SENTINEL`
with real thread completion -- non-fatal, `_invoke_emulated_proc` catches
it and continues normally. This is almost certainly what Molly's original
"thread went splat" recollection actually was. `FUN_77121505` itself is
confirmed, separately and conclusively, to be a correct, real stack-cookie
check (`__chkesp`-style) with clean success/`TerminateProcess`-on-mismatch
semantics -- not a bug, not involved.

## NEW (2026-08-28, cont'd x38): `cpu_add_logpoint` silently drops registrations past its 8-slot cap -- violates this project's own fail-loudly standard

`cpu/src/core.zig`: `lp_eip: [8]u32`/`lp_cb: [8]?LogpointFn`, fixed-size
arrays in the FFI-shared `CpuState` struct (same pattern as the breakpoint
table, `bp_table: [8]u32` -- not derived from any real hardware limit,
just a round number picked when this was built). `kernel.zig`'s
`cpu_add_logpoint` loops the 8 slots looking for an empty one and just
`return`s with no signal at all if none is free -- the new registration is
silently discarded, and the caller has no way to know. Bit this
investigation live 2026-08-28: 9-10 active logpoints (several stale, from
already-resolved earlier investigations) meant two newly-added probes
never fired, producing a real false lead (see the DAO/Jet entry above)
before the cap was noticed and probes were pruned. Not yet fixed --
`cpu_add_logpoint` should return a bool (or otherwise signal) on failure,
and the Python `add_logpoint` wrapper (`cpu_zig.py`) should raise/log
loudly when registration fails, matching this project's own "fail loudly
or not at all" standard. Deferred by Molly ("stay on the trace, come back
to it after") -- pick this up next.

## NEW (2026-08-28): `WaitForMultipleObjects(Ex)`'s `bAlertable` param is a no-op -- fine today, must be wired in if APCs ever get modeled

`_wait_for_multiple_common` (`kernel32_io.py`) reads `WaitForMultipleObjectsEx`'s
trailing `bAlertable` arg but never uses it -- a wait can never be interrupted
early by a pending APC. Verified this is currently harmless, not just
unimplemented: `QueueUserAPC`, `ReadFileEx`, and `WriteFileEx` (the only three
real Win32 APIs that can ever queue an APC to a thread) are not implemented
anywhere in `tew/api/*.py` -- with no APC source, there is no pending-APC
state `bAlertable` could ever act on. If any of those three are implemented
later, `bAlertable` (and the plain, non-Ex `SleepEx`'s alertable semantics --
same gap, same cause) need to be wired in at the same time, or an alertable
wait/sleep will silently never wake early for a queued APC.

## NEW (2026-08-26): `THREAD_SENTINEL` collision between `_call_guest_void` (static initializers) and real thread completion -- currently harmless, likely to bite later

`_call_guest_void` (`msvcrt_handlers.py:272`, used by `_initterm` to invoke a
DLL's C++ static initializers) pushes `THREAD_SENTINEL` (`0x001FE000`) as its
own inner-call return address and steps until it returns there. But
`THREAD_SENTINEL` has a real `INT 0xFE` trampoline permanently wired to
`_make_thread_return_handler` (`crt_handlers.py`) -- the same handler used
for a real spawned thread's natural completion, which calls
`scheduler.mark_current_dead()` unconditionally, with no way to tell "an
initializer just returned" apart from "this thread just died".

`_call_guest_void`'s own docstring documents the precondition this
violates: "this helper is only called from main-thread context ... so
hitting the sentinel does not corrupt cooperative-thread bookkeeping." That
was true until 2026-08-26's DllMain-for-static-imports fix (see changelog
x34/x35): `oleaut32.dll`'s real `DllMain` has a genuine `_initterm` static
initializer, and calling it now happens *nested inside* an in-flight
`_invoke_emulated_proc` call (tracking `OLEAUT32.dll`'s own `DllMain` as
`started_thread_idx`). The initializer returning to `THREAD_SENTINEL`
spuriously marks that thread dead mid-call
(`/tmp/emu.log`: "Thread 1000 returned normally" immediately followed by
"[_invoke_emulated_proc] thread idx=0 ... has died"). This run happened to
still produce the correct result (`_invoke_emulated_proc`'s
`genuinely_completed` check apparently still passed despite the spurious
death flag) -- not verified whether that holds in general, or just got
lucky this once.

Not yet fixed -- flagging so it doesn't get rediscovered from scratch next
time a DLL's `DllMain` (real or via `_ensure_dll_ready`) has static
initializers AND is invoked through `_invoke_emulated_proc`'s nested-call
path. Fix would need `_call_guest_void` to use its own dedicated
sentinel/return address (like `_invoke_emulated_proc`'s own
`_get_dialog_sentinel`-allocated one) instead of sharing `THREAD_SENTINEL`
with real thread completion.

## RESOLVED (2026-08-26): 101 `test_oleaut32_*.py` unit tests and dead `oleaut32_handlers.py` cleaned up

Deleted the 7 obsolete `tests/unit/api/test_oleaut32_*.py` unit test files that tested Python stubs now handled by real `oleaut32.dll`. Removed all dead Python `oleaut32.dll` stubs and trap objects (~1,100 lines), removed the temporary `_NoOleaut32Stubs` shim, migrated active `ole32.dll` COM handlers to `tew/api/ole32_handlers.py` (`register_ole32_handlers`), and updated callers in `crt_handlers.py` and `test_ole32_com.py`.

## RESOLVED (2026-08-26): statically-imported DLLs' `DllMain` now runs; original DAO license-key BSTR bug confirmed fixed

Fixed `build_iat_map`/`run_exe.py` so `d3d8.dll`/`oleaut32.dll`/`rpcrt4.dll`/
`secur32.dll` (MCity_d.exe's own direct imports) actually run their real
`DllMain(DLL_PROCESS_ATTACH)` now, matching real Windows loader ordering.
Working through the resulting wave of newly-exercised missing handlers
(GetSystemTimeAsFileTime, LoadLibraryExW, InitializeSListHead, CreateEventW,
several ntdll.dll Rtl* primitives, wsprintfA, RegisterClipboardFormatA,
GetSystemDirectoryA, CoSetState) confirmed the original bug fixed
end-to-end: `SysAllocString` now returns a real BSTR, and the game runs
real single-race gameplay DB traffic instead of halting on
`Database initialization failed!`. Full writeup: status.md "cont'd x35",
status_archive.md "cont'd x34" for the root-cause trace.

## RESOLVED (2026-08-26): `kernel32.dll!SearchPathA` and `SearchPathW` implemented

Implemented standard Win32 file search sequence and custom path search for `SearchPathA` and `SearchPathW` (`kernel32_io.py`). Live run confirmed `SearchPathW("expsrv.dll")` resolves cleanly to `C:\WINDOWS\SYSTEM32\expsrv.dll`.

New blocker opened immediately downstream: `msvcrt.dll!wcsncpy`, called by `OLEAUT32.dll` at ~61.3s to copy the found typelib/DLL path.

## OBSOLETE (2026-08-26): real `.tlb` type-library parsing for `LoadTypeLibEx`

**Superseded, do not pick this up** -- the entire premise was wrong. `LoadTypeLibEx`
never actually needed a hand-built parser: `oleaut32.dll` genuinely loads as
real code in this emulator, but was being unconditionally shadowed by
`oleaut32_handlers.py`'s own registered Python handlers (`dll_loader.py`'s
`patch_iat_entry` tries a registered handler before ever checking a real
DLL's export). Fixed 2026-08-26 by dropping every `"oleaut32.dll"`
registration that file makes -- real `oleaut32.dll` now parses `expsrv.dll`'s
real, embedded `TYPELIB` PE resource itself and answers `Bind`/`GetDllEntry`/
`GetFuncDesc` correctly and automatically. See changelog.md 2026-08-26.

**Follow-up cleanup (RESOLVED 2026-08-26)**: dead `oleaut32_handlers.py` code removed and active `ole32.dll` handlers moved to `ole32_handlers.py`.

## RESOLVED (2026-08-27/28, cont'd x36/x37): "Database initialization failed!" cleared -- DB init now runs for real

Was caused by the chain of missing handlers fixed across x36/x37 (`_llseek`/`_lread`,
`LoadLibraryA` stub-DLL fallback, `RegNotifyChangeKeyValue`, `WaitForMultipleObjects`,
`GetStringTypeExW`, `wcsncmp`, etc.) -- DB init itself now completes and the game reaches
real query execution. New, deeper blocker opened immediately downstream: see the
`StockAssembly_SelectAPT` DAO/Jet query-parameter entry above.
