# TODO

Durable follow-up work, checked/updated as picked up. Distinct from status.md
(current session's active blocker) and changelog.md (completed work) --
items here are queued but not yet started, or started and paused.

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

## NEW (2026-09-05): real guest-code crash around t≈41s, unrelated to D3D8 -- needs investigation

Observed repeatedly during this session's live runs (`EIP=0x00688c68`,
crash details written to `/tmp/emu_crash.json`). Not yet investigated --
happens independently of the D3D8/texture work above (seen both with and
without the debug instrumentation from that session). Next step: read
`/tmp/emu_crash.json` and correlate `0x00688c68` against `MCity_d.exe` in
Ghidra to identify what function is crashing and why.

## NEW (2026-09-05): D3D8 game window receives no real mouse/keyboard input at all

Confirmed via direct code inspection this session, while investigating why
a persona couldn't be selected even with a visible screen:
- `tew/api/window_manager.py`'s `_handle_sdl_event` only translates
  keyboard events and left-mouse-button-*down* into Win32 messages, and
  only for tew's own emulated Win32 dialog-box widget system
  (`_focused_hwnd`/`_cycle_focus`) -- not the D3D8 render window itself.
  Mouse *movement* (`SDL_MOUSEMOTION`) is never handled at all -- a
  `WM_MOUSEMOVE` constant is defined but never referenced anywhere.
  Left-button-*up*, and the right/middle buttons, are also never handled.
  There is no window focus-change handling either (no SDL
  `SDL_WINDOWEVENT_FOCUS_GAINED`/`_LOST` handling, no `WM_ACTIVATE`/
  `WM_SETFOCUS`/`WM_KILLFOCUS` ever posted).
- `tew/api/dinput_handlers.py`'s `Dev::GetDeviceState` -- the real
  DirectInput polling API a game like this almost certainly uses every
  frame for keyboard/mouse -- is a hardcoded zero-fill stub (the comment
  literally says "zero-fill output"). Even if the game polls correctly, it
  always reads "nothing pressed, no movement."

Net effect: even once the persona-select screen is fully visible, there is
currently no way to click a persona or otherwise interact with the game.
Real scope: wire `SDL_MOUSEMOTION`/button-up/right-and-middle-button events
into the D3D8 window's real input path (separate from the dialog-widget
system), add focus-change message posting, and replace `GetDeviceState`'s
zero-fill with real `SDL_GetMouseState`/`SDL_GetKeyboardState` polling.

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
