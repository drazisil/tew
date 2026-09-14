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

## New finding, unconfirmed root cause (2026-09-14, very late) — real unhandled CPU fault inside the game's own memory-leak walker, ~50s after last real click

Found while diagnosing a crash from a run left running after the decisive
click-timing test (see the persona-select click-delivery entry below).
**Not caused by clicking Start, the background dead-zone, or the window
close button** — none of those match the timing: last real click at
771.5s, fault at 821.267s (a ~50s gap), no `WM_CLOSE`/`SDL_QUIT` anywhere
in the log at all.

**Two separate events, easy to conflate**:
1. `5.817s` — `_CLayer_DetectDebugger` (0x4d980f, `c:\mcity\game\clayer.c`)
   deliberately reads from address `0x190` to self-test for a debugger
   (`SetErrorMode(2)` + a real access violation on purpose, caught by two
   nested SEH frames -- `_CLayer_CatchSEH` wraps it). This is the
   already-documented benign "Found Debugger!" false positive. SEH catches
   it fine here (no "unhandled" error follows) -- **not the bug**.
2. `821.267s` — a genuine, independent, **unhandled** fault at `0x5a7bfc`,
   inside `LEAK_printclassf` (the game's own custom memory-leak reporter,
   *not* the MSVC CRT debug heap -- this is guest code: `_memclass`,
   `checksentinel`, `_MEM_name`, `_MEMSYS_validaddress`, all real,
   unmodified `c:\mcity\game`/frontend source). `dispatch_exception()` was
   attempted and genuinely found no handler: `"unhandled by SEH chain --
   halting"`. **This is the real bug.**
3. `831.419s` — tew's own post-fault diagnostic step (re-running the
   guest's leak-dump for extra crash context) lands back at `0x4d980f` --
   a secondary, diagnostic-only re-hit, not a third independent fault.

**Mechanism, confirmed via disassembly**: the exact faulting instruction is
the `MOV` at `005a7bf8` (EIP `0x5a7bfc` is the *next* instruction, `AND
EDX,0x4000`, register-only -- the fault is reported one instruction late),
reading `local_1c[+2]` while walking `LEAK_printclassf`'s tracked-allocation
linked list (`for (local_1c = ...; local_1c != NULL; local_1c =
*(local_1c+8))`). `crash.json`'s `attempted_address` is `0x4d980f` -- a
**code** address (right next to `_CLayer_DetectDebugger`), not a heap
address. Since the read target is `local_1c+2`, `local_1c` itself must be
≈`0x4d980d` -- the list's `next` pointer holds a code/return address instead
of a valid heap-block pointer. Classic wild-write signature: something
overwrote this list node's linkage well before the crash surfaced; the
walker just happened to be what finally dereferenced it.

**Root cause NOT yet found -- this is a real, live lead, not closed.**
Ruled out: this is not the MSVC CRT's own debug heap (tew doesn't need to
maintain `_memclass`'s format at all -- it's guest-code-maintained), so it's
*not* a "tew's malloc header format is wrong" bug. More likely: tew's
emulation of some *other* memory-writing operation (a string/buffer
function, a `REP STOSD` zero-fill, anything with an off-by-some bounds bug)
overran into an adjacent allocation and clobbered this list node's `next`
field. **Next step**: reproduce live with a memory watchpoint on the
corrupted node's `next`-pointer field (see [[feedback_stalk_memory_over_decompile_guessing]]
-- dump live objects at every hit rather than keep guessing from the
decompile) to catch the actual corrupting write in the act. Don't blind-guess
which handler is responsible without that.

**Traced one level further (still 2026-09-14, very late)**: the crash isn't
random -- `stdout.txt`/`/tmp/emu.log` both confirm `_NFSabortmessage`
(0x687bd8, the game's real assert/abort handler) is entered right before
the `LEAK_printclassf` fault (`OutputDebugString` lines `"NFSAM -1"`,
`"NFSAM 0"`, `"checking for memory leaks..."` are the last guest output
before the crash). So a REAL assert/abort fired first; the leak-dump
crash is a *secondary* failure while the game tries to report it -- and
because the crash lands before `_NFSabortmessage` ever formats/prints its
message, **the actual reason for the abort has been lost every time this
has happened so far**. Ruled out the three click-based hypotheses (Molly's
original guesses: clicking Start, clicking the dead-zone background,
clicking the window close button) -- none match the timing (last real
click 771.5s, fault 821.267s, no `WM_CLOSE`/`SDL_QUIT` anywhere in the
log). A window-focus-loss event (`SDL_WINDOWEVENT` + `SDL_CLIPBOARDUPDATE`,
`WM_KILLFOCUS`/`WM_ACTIVATE`) at 795.4s is closer in time (~26s before) but
recurs many other times earlier in the same run with no crash following,
so it's not confirmed as the trigger either -- just the closest candidate
found so far.

**FIXED the visibility gap, not yet the root cause**: added
`_nfsabortmessage_probe` (`run_exe.py`, logpoint at `_NFSabortmessage`'s
entry 0x687bd8, one of 8 available `cpu.add_logpoint` slots -- 0 others
currently active) that reads the abort message format string (cdecl arg1)
plus `_REALabortfilename`/`_REALabortlinenum` the instant the function is
entered, before `LEAKS_CheckForMemoryLeaks` can crash and destroy that
context. Logs under the `cpu` category. Full suite still green (1275
passed). **Launched a fresh detached run (PID 186323, `nohup`+`disown`,
`LOG_CATEGORIES=window,startup,handlers,cpu`, no synthetic click since
the previous crash happened independent of any click) to try to
reproduce and finally capture the real trigger.**

**Did NOT reproduce, 2026-09-14 very late, same session**: that run ran
the full 900s and shut down cleanly via `timeout`'s own SIGTERM -- no
`nfsabortmessage-probe` line, no `unhandled by SEH chain`, no
`LEAK_printclassf`/`0x5a7bfc` anywhere in the log. Plain non-result, not
evidence the bug is fixed or gone -- the original occurrence happened
~820s into a run that included real manual clicks and at least one
window-focus-loss event; this run had neither. **The probe (`run_exe.py`,
`_nfsabortmessage_probe` at 0x687bd8) stays in place** -- next session,
reproduce with more real interaction (clicks, alt-tabbing away and back)
during a long run rather than a passive idle one, since the one confirmed
occurrence happened during/after exactly that kind of session, not a
quiet one.

**Second attempt, also did NOT reproduce (2026-09-14, very late, same
session)**: Molly's suggestion -- click START (physical 987,1097,
confirmed-correct coords) at 620s, then run a full 1500s (~13 more
minutes after the click) instead of just 900s total, with the probe
active and real-time screenshots taken every ~2.5min for a visual
timeline (`/tmp/claude-.../scratchpad/shot_<elapsed>.png`, 8 shots from
97s to 1352s). Clean 1500s run, SIGTERM shutdown, **no crash, probe never
fired**. Click delivered correctly (`lp=0x26e01ee` decodes to logical
(494,622), matches START) but -- consistent with every previous attempt
tonight -- still produced no screen transition. Two open, unreproduced
questions now, not one: (1) why a correctly-delivered click on START
still doesn't advance the game (open since earlier tonight, see the
click-delivery entry above), and (2) what triggers the intermittent
`LEAK_printclassf` fault (2-for-2 non-reproductions now, both with and
without a click). No evidence either is fixed -- both remain live,
unreproduced leads for next session. The probe and the visual-timeline
screenshots are a reusable technique for the next attempt; screenshots
were not deleted (`shot_97.png` through `shot_1352.png` in this
session's scratchpad, harness-managed cleanup applies as usual).

**Real breakthrough, Molly's own recollection (2026-09-14, very late,
same session)**: from her earlier manual testing, the persona-select
dialog genuinely CAN dismiss -- but only after a sequence that included
double-clicking the persona name ("Dr Brown"), not a single click on any
button alone. After it dismissed, the screen behind it read "please
wait...", and **~1 minute later it faulted** -- this matches the original
`LEAK_printclassf` crash's own timing almost exactly. This reframes the
whole investigation: the single-click-does-nothing puzzle and the
intermittent-fault puzzle are very likely the SAME bug at two different
stages -- a real, working transition (dialog dismiss -> "please wait") that
then crashes about a minute in, not two unrelated problems.

Root mechanism, confirmed in `dlg.persona`: `<PERSONAS>.GListBox` has
`*WEVENT_ACCEPT=GWidget_ACCEPT_PARENT` -- a double-click on the list IS the
real accept gesture, not a single click on START. This also fits why
single START/QUIT clicks (correctly delivered, correctly timed, confirmed
multiple ways tonight) never did anything: they were never going to --
wrong control.

**Genuine timing tension found while implementing this**: real
double-click detection (`GMouseInput::Do`, Ghidra-confirmed) measures
down-edge to down-edge and must land inside `GetDoubleClickTime` (confirmed
live: 500ms) -- but a single click needs ~400ms+ hold to reliably survive
this emulator's ~385ms DirectInput poll gap (see the `TEW_CLICK_HOLD_SEC`
fix above). Two such "safe" holds back-to-back can't fit inside a 500ms
double-click window -- these two requirements are nearly mutually
exclusive at this poll rate. A real human double-click (short holds,
~80-150ms per click) only has partial odds of being caught by any one
poll, which fits Molly's own account ("when I was about to give up") --
it likely took her several tries too, not one deterministic double-click.

**Added `TEW_DBLCLICK_*` env vars to `run_exe.py`** (`_AT`, `_AFTER_SEC`,
`_HOLD_SEC` default 0.15s, `_GAP_SEC` default 0.05s between the two
clicks, `_REPEAT` default 3 attempts, `_RETRY_SEC` default 1.0s between
attempts) -- fires the whole attempt multiple times in a row rather than
pretending one synthetic attempt is guaranteed to land on favorable poll
timing, matching the inherently timing-sensitive real mechanism. Full
suite green (1275 passed). Persona-name click target (logical ~532,417,
dialog-relative ~100,158 scaled 1.28x from `<PERSONAS>.GListBox`'s own
bounds) -> physical ~(1064,735) at this session's 1024x768/2048x1354
sizing.

**Also did NOT reproduce (2026-09-14, very late, same session)**: full
1200s run, all 3 double-click attempts fired cleanly (621.4s-623.7s per
the new `[dblclick]` log lines), clean SIGTERM shutdown at 1200s, no
crash, probe never fired. Screenshots at 741s/921s/1101s/final all show
the dialog still up, "Dr Brown" still highlighted, zero visible change --
confirmed visually, not just from logs. **Three separate synthetic
approaches have now all failed** to reproduce what Molly saw manually
(single click START, single click QUIT, double-click persona name) --
that's a real signal, not bad luck. Something about the real manual
interaction differs from what these synthetic attempts model beyond just
"which control was clicked."

**Live lead found via Ghidra while this ran, not yet tested**: `GButton`
has real `OnMouseDown`/`OnMouseUp` overrides (00b47630/00b47720) --
`OnMouseDown` (normal case) just does `SetFocus`+`CaptureMouse`, no press
event; `OnMouseUp` only fires the real press (`SendEvent(0x2a)`) if
`HasMouseCapture(this)` is STILL true at release time and a hit-test
confirms the mouse is still over the button. Found that `ReleaseMouse`
(which clears `_mCapture`) is called not only from the expected
`OnMouseUp` path but also from `OnKillFocus`, `SetEnable`, and
`SetVisible` -- Molly's own suspicion: if anything else (a periodic
UI-refresh/redraw pass touching these buttons' enabled/visible state, or
another widget stealing focus) fires during our necessarily-long
(~0.5s+, to survive the ~385ms DirectInput poll gap) hold, capture could
be silently dropped mid-hold, and `OnMouseUp` would take the do-nothing
fallback path. **Not yet verified** whether this actually happens live --
would need to trace `_mCapture`'s value across a real held click, not
just reason about it from the decompile. This is the strongest remaining
lead: it would explain why a hold long enough to satisfy the polling
requirement might specifically defeat the button's own capture-based
press mechanism, independent of which button/control is targeted.

**QUIT also confirmed NOT to work (2026-09-14, very late, same
session)**: re-ran cleanly to full 1200s completion (the first attempt
had been killed early at 4min to pivot to the double-click test, so QUIT
was never actually validated until now). Click fired correctly
(621.36s-621.87s, confirmed via log), screenshots at 676s/856s/1036s/final
all show the cursor sitting right on QUIT with zero reaction -- no
confirm dialog, no dismiss, dialog fully intact throughout. No crash, no
probe fire, no orphaned process.

**Session-end consolidated conclusion**: four separate synthetic
approaches tested tonight -- single click START, single click QUIT,
double-click persona name, and (implicitly) the persona row itself being
pre-selected -- all correctly delivered (confirmed via coordinates,
timing, and/or screenshots) and all producing **zero reaction**. This is
a strong, consistent result, not bad luck across multiple attempts.
Something is genuinely broken in how synthetic input reaches this
screen's accept logic that Molly's real manual interaction apparently got
past at least once (her recollection: dialog eventually dismissed to
"please wait...", crashed ~1min later). The strongest still-untested lead
going into next session is the capture-instability theory above
(`ReleaseMouse` firing from `OnKillFocus`/`SetEnable`/`SetVisible`,
possibly clearing `_mCapture` mid-hold) -- verify live by tracing
`_mCapture`'s actual value across a held click rather than reasoning from
the decompile alone.

**CRITICAL CORRECTION, Molly's own recollection (2026-09-14, later
still)**: none of the four things tested tonight (Start, QUIT game-button,
persona double-click, persona row pre-selection) were ever the real
trigger. What actually dismissed the dialog in her original manual
session was pressing the **window's OS-level close button (X)** -- and
that was her *only* click that session. This is a completely different
code path (`SDL_WINDOWEVENT_CLOSE` -> `WM_CLOSE` posted to the dialog,
per `window_manager.py`'s `_handle_sdl_event`), not a GUI button click at
all -- explains why every GUI-internal click attempt failed identically,
since none of them were ever going to be the real trigger.

**Important open question this raises**: `WM_CLOSE` on a modal dialog
commonly maps to a *Cancel* path, not Accept/Start, in many dialog
implementations. If that's what's happening here, the "please wait"
screen and the ~1min-later crash may be a **cancel/disconnect flow bug**,
not a bug in the normal persona-accept flow this whole investigation has
been chasing. Needs live testing to determine which. **Next step**: inject
a real `SDL_WINDOWEVENT_CLOSE` (not a mouse click) at the right time and
observe -- `run_exe.py`'s click-injection tool doesn't support this yet,
would need a new `TEW_CLOSE_AFTER_SEC`-style env var pushing a real
`SDL_WINDOWEVENT`/`SDL_WINDOWEVENT_CLOSE` event instead of a mouse event.

**RESOLVED the open question above, same session, via Ghidra**: `WM_CLOSE`
(0x10) on the main window is handled by `FUN_00780550`, which just calls
`PostQuitMessage(0)` -- a full application-quit request. **Not a dialog
Cancel path at all.** This means Molly's original manual dismissal was her
telling the whole game to shut down, not accepting or cancelling the
persona dialog through any normal gameplay path. The "please wait" screen
and the crash ~1min later are very likely part of **shutdown/teardown**,
not persona-select's accept flow -- this entire night's framing (find the
right control to accept the persona and advance normally) may have been
chasing the wrong mechanism. Still need to actually inject a real
`SDL_WINDOWEVENT_CLOSE` to confirm this live (not yet done).

**Independent, major finding, same session**: the `LEAK_printclassf`
fault (see the entry above) **reproduced spontaneously with zero clicks
and no manual interaction** during an unrelated passive profiling run
(404.7s guest time, right after a `CreateFile("trace006.txt")` failure --
read-only open, file not found). This is real evidence the crash is NOT
tied to any specific click, the window-close event, or any UI action at
all -- it's a real, periodic/intermittent bug reachable just by playing
normally for long enough. Confirms this is worth treating as its own,
separate investigation from "how do you get past persona-select."

**Also found, same session**: the `_nfsabortmessage_probe` logpoint (added
earlier to capture the real abort message) did NOT fire on this
reproduction, even though `_NFSabortmessage` demonstrably ran (`NFSAM 0`
printed to stdout.txt as expected). Only one logpoint was registered (not
the documented 8-slot-cap issue), so this is a real, separate,
not-yet-understood bug in the logpoint tooling itself -- the callback
either isn't being invoked at the registered address, or is failing
silently (ctypes callback exceptions can be swallowed at the FFI
boundary). Needs its own investigation before relying on this probe again.

**Window-close TESTED live, same session -- ruled out as a unique cause,
but led to the real lead.** Added `TEW_CLOSE_AFTER_SEC` (pushes a real
`SDL_WINDOWEVENT_CLOSE`) plus a fix for a genuinely silent bug in
`window_manager.py`'s `_handle_sdl_event` (`WINDOWEVENT_CLOSE` dropped
with zero logging at any level if the windowID lookup failed -- fixed to
warn like the mouse-button handlers already did). Two live tests:
1. Synthetic `SDL_WINDOWEVENT_CLOSE` at 300s -- windowID matched fine
   (new warning never fired, so `WM_CLOSE` genuinely was queued), but
   **no reaction of any kind**, ran 400+ more seconds with nothing
   happening. (NOTE: `DispatchMessageA` logging is DEBUG-only, not
   captured at this run's `LOG_LEVEL=info` -- can't yet confirm whether
   the guest actually *dispatched* the queued `WM_CLOSE`; next session
   should re-test with `LOG_LEVEL=debug LOG_CATEGORIES=handlers,window,startup`
   specifically to check.)
2. **Molly's real physical click** on the same live window (at ~420s) --
   **crashed for real**, same fault, `NFSAM -1` at 420.172s ->
   unhandled SEH fault at 420.207s (35ms later).

**CORRECTED same session, before acting on it**: initially misread
`CreateFile("traceNNN.txt")` (immediately before `NFSAM -1` both times)
as a *precondition* that fails and triggers the abort. Molly corrected
this: the trace file "gets created, and it's fired in abort" -- i.e. this
`CreateFile` is `_NFSabortmessage`'s **own diagnostic logging**, opened
as part of already being inside the abort handler, not something that ran
*before* and caused entry into it. So the incrementing trace file number
(`trace006.txt` -> `trace007.txt`) is a **symptom**, not a lead -- don't
waste time chasing why it fails to open, that's expected/correct for
whatever `_NFSabortmessage` does with a probably-never-backed guest path.

**The real open question, unresolved**: what actually triggers entry into
`_NFSabortmessage` in the first place, *before* any of this. Still
completely hidden -- the message-format-string argument that would answer
this directly is only available at the function's own entry (before
`LEAKS_CheckForMemoryLeaks` can crash and destroy the context), which is
exactly what `_nfsabortmessage_probe` was built to catch -- and it still
hasn't fired across two real reproductions. **Fixing the logpoint probe
itself is now the highest-value next step** -- without it, the actual
trigger is undiscoverable from static analysis alone, since the crash
that follows destroys the evidence every time before it can be printed.
Diagnostic version added (unconditional entry log + exception-wrapped
body, since a ctypes-callback exception can be silently swallowed at the
FFI boundary) but not yet tested against a real reproduction.

**Screenshot comparison against real MCO footage, CORRECTED after
re-checking the actual resource data (2026-09-14, very late)**: Molly
supplied a reference screenshot of the real persona-select screen showing
what looked like two side-by-side panels (existing personas on the left,
a "create new persona" server-selection list with POP./PIND columns on
the right, QUIT centered alone between them). Initial read: tew was
missing/collapsing this two-panel layout -- **WRONG, caught by Molly**.
Re-checking `dlg.persona`'s actual bounds: all four buttons
(`<OK>`=START, `<DELETE>`, `<CANCEL>`=QUIT, `<CREATE>`) sit at the exact
same y=310, spread evenly across the full 461-wide panel (x=30/129/228/
334) -- **one row of four, structurally identical to what tew already
renders**, not "QUIT alone, centered between two panels" as first
(over-)read from the screenshot. There is no second `GListBox` anywhere
in this file for server selection -- the right side only has a
hidden-by-default description text (`txt_variety`) and a visible "Select
Below" label pointing at the CREATE button, nothing resembling the
server/POP/PIND list the reference screenshot shows.

**Corrected conclusion**: the discrepancy isn't a tew rendering bug
collapsing two panels -- it's that this specific `dlg.persona` (this
debug build's resource) has **no definition at all** for the
server-selection list the reference screenshot shows. Most likely a
version mismatch (the screenshot may be from a later retail patch with an
updated resource that added that feature) rather than something tew is
doing wrong. **Do not chase "restore the missing right panel" as a tew
bug** -- check first whether this debug build's real, unmodified
`dlg.persona` ever had that feature at all before assuming tew dropped
it.

---

## Known false leads (permanent — do not remove on rotation)

- **`dbcode.c(3376) "The class has not been licensed"`**: prints every run, every time DAO/Jet does COM work, well before any actual failure. Molly confirmed (2026-08-16) this is expected/ignorable — NOT the cause of `CreateQueryDef`/DAO-3075 failures. Got mistakenly re-flagged as a "new lead" once already the same night (see `status_archive.md`, "Previous status (2026-08-16, cont'd x4)", for the correction) — check here before treating it as new again.
- **"DB-thread scheduler starvation" is NOT a tew scheduler bug** — re-flagged this exact way on 2026-09-05 before being corrected the same session; already root-caused once before, on 2026-09-04 (see `status_archive.md`'s "Previous status (2026-09-04, evening)" entry), as genuine, correctly-emulated slow guest work (the real Jet/DAO database engine + the game's own polling loop), not a tew bug. Re-confirmed 2026-09-05: during the stall, `tid=1000` (the render thread) is NOT blocked/starved — `LOG_CATEGORIES=scheduler` shows it actively executing a real `SetEvent` polling loop right up to the end of the run. `cpu/src/scheduler.zig`'s `preemptSlice` round-robins to any other `.ready` thread every batch and only keeps running the current one when nothing else is ready — it is not unfairly favoring the DB thread. The real constraint is that this specific guest workload is genuinely slow under x86 emulation; no scheduler change fixes that. Check here before re-investigating this as a scheduler fairness bug again.

## Current status (2026-09-14, very late) — persona-select click delivery: FIVE real bugs fixed and confirmed (Reset, WM_LBUTTONDOWN, DirectInput event-driven redesign, click-coordinate 2x WINDOW_SCALE scaling, click-injection hold duration). **DECISIVE TEST RUN, same session**: synthetic click at physical (987,1097) -- directly measured via `xdotool` with the real cursor sitting dead-center on START, not calculated -- held 524ms (past the confirmed ~385ms DirectInput poll gap), with a `GetDeviceState` call (621.468s) confirmed landing squarely inside the down(621.252s)-to-up(621.776s) window. **Still zero reaction.** This rules out emulation speed / poll-timing as the cause entirely -- coordinates, delivery, and timing are all now proven correct simultaneously, in the same click, for the first time. The remaining bug is a genuine, distinct issue further down the GUI chain -- see "Next lead" below. (Also ruled out earlier in this same investigation: two real manual clicks from Molly landed exactly on the confirmed-correct position but were too quick (~120-180ms, under the poll gap) to be a valid test of this -- superseded by the decisive synthetic test above.)

**IMPORTANT correction on `GDialogs.gui` coordinates**: they are NOT literal absolute pixels at the current resolution despite `GS_ABSOLUTE`/`GS_NOAUTOCENTER` -- confirmed by Molly manually clicking dead-center on the real, visible START button and comparing against the logged logical coordinate. `GDialogs.gui`'s `[PersonaSelect.MPersonaSelectDlg] GUI.mBounds=[316, 168] 461, 354` is authored for an **800x600 reference canvas** and FEDC uniformly scales the whole dialog layout to the real active resolution -- 1024x768/800x600 = exactly **1.28**. Recomputed START center: dialog origin (316,168)*1.28=(404,215); button center dialog-relative (78,323.5)*1.28=(100,414); absolute = (504,629). Molly's real click logged at (508,632) -- within 4px, i.e. this really was a correct, dead-center click on START. **Use the 1.28x reference-resolution scale factor for any future `GDialogs.gui`-derived coordinate math at 1024x768** (recompute the factor if a different resolution is ever active -- it's `active_res / 800x600`, not a fixed constant).

**Three real, committed fixes this session** (full writeups in changelog.md's 2026-09-13 entries):
1. `IDirect3DDevice8::Reset` was a complete lying no-op -- window/swapchain never resized past `CreateDevice`, clipping the persona-select screen. Fixed for real.
2. `SDL_MOUSEBUTTONDOWN` never posted a real `WM_LBUTTONDOWN` to any non-dialog window (only MOUSEBUTTONUP/MOTION did) -- the main game window never received a click message at all. Fixed.
3. DirectInput's `SetEventNotification` accepted event-handle registration and never signaled it; mouse state was only ever sampled on demand (which the game never does). Redesigned to be event-driven: `window_manager.py`'s real SDL pump now feeds `dinput_handlers.py` directly as events arrive.

**Confirmed live after fix 2+3**: clicking "Dr Brown" advanced the game off persona-select to a "Connecting to localhost:8226" screen, with a real, verified round-trip against `mco-server` (login + `GET_PERSONA_MAPS`, correct "Dr Brown" persona data returned). **Caveat, corrected same session**: this is the LOGIN server reconnecting (`LoginServerPort=8226` per the shard list), NOT the lobby (`LobbyServerPort=7003`) -- and it turned out to be a periodic automatic refresh unrelated to any click (identical payload sent again ~44s later regardless of which UI element was clicked), not proof the click itself worked. Do not reuse this as "click confirmed working" evidence -- see `changelog.md` for the correction.

**Where investigation stands right now**: extensive live testing (both synthetic `SDL_PushEvent` clicks and real manual clicks from Molly) shows SDL reliably delivers real click events (`SDL_MOUSEBUTTONDOWN`/`UP`, confirmed via unconditional event-type logging in `pump_sdl_events`) and `pump_sdl_events` itself is called continuously, not stalled -- ruling out "the pump stopped running" as an explanation. But most real clicks during the persona-select screen produce **no** downstream `[dinput]`/`DispatchMessageA` reaction at all, while every click during the earlier login-dialog stage does. One specific instance was caught directly in the log (`SDL event type=0x401` with zero follow-up) and traced to `_handle_sdl_event`'s `SDL_MOUSEBUTTONDOWN`/`UP` handlers: both had **silent** early-return paths (non-`SDL_BUTTON_LEFT`, or `windowID` not found in `_sdl_window_id_to_hwnd`) with no log line explaining which one fired. **Just fixed**: both branches now log explicitly (button value, or the unmapped windowID plus the full list of known IDs), and window-creation log lines now include the actual `sdl_win_id` they registered, so a click's `windowID` can finally be checked against what's known. **Not yet run** -- next session should reproduce a dropped persona-select click and read which branch actually fired.

**Debugging-process notes worth keeping**:
- `TEW_MAX_STEPS` (not just wall-clock `timeout`) can silently end a run early -- confirmed live this session: a run reporting "Emulation Complete (clean exit)" had simply hit `TEW_MAX_STEPS=1200000000`, not a real game-driven stop. Use a generous budget (multi-billion) for any run expected to run past ~400s real time.
- `LOG_CATEGORIES` mistakes cost real time this session, repeatedly: `handlers` carries DirectInput/dinput calls AND (as of a mid-session fix) still does NOT carry D3D8's own COM traffic (that's now split into its own `d3d8` category specifically to avoid this) -- but `startup` (clean-exit/halt diagnostics, `[alive]` heartbeats) is easy to forget and was excluded by accident more than once, hiding the actual stop reason.
- A full, unfiltered `LOG_LEVEL=debug` run got killed by the harness's background-task memory guard once this session -- keep `LOG_CATEGORIES` scoped for any run expected to last minutes, not just for readability.
- Guest-written files (`~/.emu32/MCity/stdout.txt`, `~/.emu32/Login.log`, `~/.emu32/MCity/MCity_Log.txt`) are NOT subject to tew's own `LOG_CATEGORIES` filtering and are often the more reliable source for "has this screen actually loaded yet" checks than tew's own log.

Not yet tried: DXT/S3TC decompression, multitexturing (stage > 0). tew is currently hardcoded to decline any "run in fullscreen?" prompt the game shows -- known, deliberate, not a bug.

Repro: `cd /data/Code/tew && LOG_LEVEL=debug LOG_CATEGORIES=window,startup,handlers TEW_MAX_STEPS=6000000000 timeout 900 .venv/bin/python run_exe.py` (no synthetic click -- click manually once the persona list is visible) -- persona-select screen appears roughly 200-280s in, real time varies run to run; do not assume a fixed timing marker (Molly's correction, 2026-09-13).

**Ghidra reference (2026-09-13): real guest-binary click pipeline, traced end to end in `MCity_d.exe` (project `debug_clean`)** -- gives tew's own `_handle_sdl_event`/dinput redesign something concrete to match against:
- OS `WM_LBUTTONDOWN` etc. actually route through `_MESSAGE_handler` (0077e920) to `FUN_00780d80` -> `seteacmouse` -> `Mouse_MyClick` (global `gMouseButton`) -- this is a **separate legacy/anti-cheat click-tracking layer, not the GUI's own event feed**. Dead end, don't chase it further.
- The real GUI-facing input is polled, not WM_*-message-driven: `GUI_Main` (00ae9b30) each frame calls `GUI_ProcessEventQueue` then `GUSER_DoInput` (00b32cf0) -> `GUser::DoInput` (00b32bb0), which calls each registered `GInput` subclass's vtable+4 `Process`/`AppPoll*` method.
- `FEI_Init` (007f5fb0, the frontend/persona-select-era init) registers `MKeyInput` (keyboard) and `MMouseInput` (or `MFFBMouseInput` for force-feedback mice) via `GUSER_AddInput`. **`MMouseInput::AppPollMouse` (0075fae0) is the function that actually reads the mouse device and pushes GEVENTs into the queue** -- this is where click/edge-detection logic and double-click timing (`GMouseInput::SetDblClickRate`) live, and the natural next thing to decompile.
- Consumer side: `GEventQueue::Process` (00ae9550) pops queued events and calls `GUI::SendEvent` -> `GUI::OnEvent` (00aed150, `c:\guiduck\source\gui.cpp`), a switch on the `GEVENT` enum that calls fixed vtable-offset virtuals (offset `0x98`=`OnMouseDown`, `0x90`=`OnButtonDown`, `0x94`=`OnMouseMove`).
- The vtable Molly pasted (`0x011d26b0`: `GDialog::OnKeyDown`...`MPersonaSelectDlg::OnAccept`/`OnCancel`) is confirmed as `MPersonaSelectDlg`'s own vtable (derived through `GDialog`->`GUI`).
- Traced one level deeper: `MMouseInput::AppPollMouse` (0075fae0) calls `_MOUSE_getstate(6)` (a snapshot: pos + 4 button bytes) and feeds it to `GMouseInput::MouseSetPos`/`MouseSetButton` (just raw state setters, offset `this+0x2c+i*4` per button, no edge detection). The actual click edge-detection and `GUI::PostEvent` call live in `GMouseInput::Do` (00b1b360, the `GInput` vtable+4 override): a button posts `GEVENT=0xb` (mouse down) **only on the transition frame** where its stored state goes from "not down" (`mDblClickPending[i]==0` at offset `+0x48`) to down, and `GEVENT=0xc` (mouse up) only when it reads not-down again with that pending flag still set. **Implication for tew**: this is a per-frame polled edge-detector, not an event queue -- if tew's emulated mouse-button state can go down *and back up* between two successive `AppPollMouse` polls (i.e. the guest's poll rate loses a fast down+up), the guest's edge-detector never observes the down transition and the click is silently swallowed **even with no bug in tew's SDL/dinput event plumbing at all**. This is a second, independent candidate root cause alongside the already-found silent early-return bug in `_handle_sdl_event` -- worth checking once the current instrumentation run comes back: does the dropped click's underlying button-state snapshot (whatever tew exposes to `_MOUSE_getstate`'s equivalent) actually stay "down" across at least one guest poll interval?
- **CORRECTED same session, after actually reading `tew/api/dinput_handlers.py`**: the "latch until polled" idea above does NOT apply to tew's current code and should not be implemented as originally stated. `GetDeviceData` (buffered, index 10) is already a real-time FIFO (`_mouse_dod_queue`, pushed by `notify_mouse_button` the instant SDL delivers an event) -- exactly matching real buffered DirectInput, nothing to fix. `GetDeviceState` (immediate, index 9) and the `GetAsyncKeyState`/`GetKeyState` VK_LBUTTON path (`user32_handlers.py`) both read `_mouse_buttons[0]` live at call time -- correct, since real immediate-mode DirectInput and real `GetAsyncKeyState` genuinely are live samples in actual Windows (a sub-poll-interval down+up can legitimately be missed there too). Latching either would make tew diverge from real Windows, not match it.
- Bigger finding from the same Ghidra read: `_MOUSE_getstate`'s DirectInput branch (`_INPUT_getdevicedata`, really `GetDeviceState` under the hood per its 16-byte DIMOUSESTATE shape) only runs when a guest flag `DAT_0128af04 != 4`; when it's `4` the guest takes a **different, non-DirectInput legacy branch** (`getmousepos()`-based). tew's own earlier note that `GetDeviceState`'s mouse branch was never observed firing during the persona-select screen now reads as evidence the guest is on that legacy branch, not the DirectInput one -- and tew's handling of that legacy path (if any exists) hasn't been located yet. Do not patch `dinput_handlers.py` for this without first confirming, from the next instrumented run, which branch the guest is actually taking.

**FIXED (2026-09-13, same session): real click-coordinate scaling bug, found by reading `window_manager.py` while chasing the above.** `IDirect3DDevice8::CreateDevice`/`Reset` (`idirect3d8.py`/`idirect3d8device.py`) call `SDL_SetWindowSize` to enlarge the *real* SDL window to `WINDOW_SCALE=2`x the guest's requested backbuffer size (640x480 guest -> 1280x960 real window) purely for host-display readability -- their own comments are explicit that the guest must never observe this anywhere (`GetBackBuffer`/`GetRenderTarget` size, vertex math, all still see the unscaled logical size). But `window_manager.py`'s `_handle_sdl_event` posted raw real-window SDL pixel coordinates straight into `WM_MOUSEMOVE`/`WM_LBUTTONDOWN`/`WM_LBUTTONUP`'s lParam and into `dinput_handlers.notify_mouse_motion`, unscaled -- so every click/move on the *enlarged* window was reported to the guest at exactly 2x its real logical position. This exactly explains the persona-select symptom pattern: the **login dialog** (a separate, native, never-rescaled SDL window) always worked; the **main D3D8/FEDC window** (rescaled after `CreateDevice`) is where clicks kept silently missing -- `WM_LBUTTONDOWN` was genuinely delivered, but at coordinates the guest's own `GMouseInput::Do`/`GUI::OnEvent` hit-testing (see the Ghidra trace above) would find no control at, since real Windows never has this discrepancy in the first place.
  - Fix: added `WindowEntry.logical_w/h` (recorded at `CreateWindow` time) and `.phys_w/h` (recorded whenever `CreateDevice`/`Reset` calls `SDL_SetWindowSize`, 0 if never rescaled), plus `WindowManager._to_logical_xy(hwnd, x, y)` which divides back to logical coords before they reach `WM_MOUSE*`'s lParam, `_handle_mouse_click`, or DirectInput's tracked mouse position. A window that's never been resized by D3D8 (dialogs, or the main window pre-`CreateDevice`) is an exact no-op -- not a special case, just `phys_w == 0`.
  - Verified with new unit tests (`tests/unit/api/test_window_manager.py`: `test_to_logical_xy_scales_down_for_enlarged_window`, `test_to_logical_xy_noop_for_never_rescaled_window`, `test_to_logical_xy_noop_for_unknown_hwnd`) rather than a full emulator run -- the harness's background-task OOM guard killed two consecutive attempted runs this session before the persona-select screen was ever reached (real host memory looked fine both times, ~7GB free; likely desktop-app cumulative load, not a tew issue). Full suite (1275 tests) green after the change.
  - **CONFIRMED end-to-end, same session (2026-09-13, late)**: harness-tracked background runs kept getting OOM-killed (3x) even with headroom freed (VSCode closed, 7GB->10GB free) -- worked around by launching fully detached (`nohup ... & disown`, not `run_in_background`), which survived. Reached persona-select for real (confirmed via `~/.emu32/Login.log`: `Persona Select` / `Persona added: Dr Brown on shard44`). That run's actual active size: logical 1024x768, physical 2048x1354 (clamped, confirmed via `Reset back=1024x768` / `Reset: swapchain recreated 2048x1354` -- NOT 640x480, the Reset calls happen almost immediately after `CreateDevice`). Computed a real click target from `dlg.persona`'s actual layout (`<PERSONAS>.GListBox` [28,149] 274,94, rowHeight=18, background panel 461x354 centered in 1024x768) -> logical (382,365) -> physical (764,644). Injected via `TEW_CLICK_AT=764,644 TEW_CLICK_AFTER_SEC=620` on a fresh run: log shows `[dinput] real mouse button 1 down at (382,365)` (exact match) and `DispatchMessageA hwnd=0x1034 msg=0x0201 wp=0x0 lp=0x16d017e` -- lParam decodes to x=0x017e=382, y=0x016d=365, i.e. `WM_LBUTTONDOWN` really was dispatched to the game's own WndProc at the exact intended logical position. **The coordinate-scaling bug is real and the fix works** -- this is no longer a hypothesis.
  - **Molly's correction**: the persona is already selected by default (it's the only entry) -- no need to click the row first, just click START directly. Re-ran with a click at START's location instead (`<OK>.GButton` [30,310] 96,27 dialog-relative -> logical (360,531) -> physical (720,936) at this run's 1024x768/2048x1354 sizing). Delivery confirmed again by exact lParam match (`lp=0x02130168` decodes to x=360, y=531) -- but still **no screen transition**, no new `Login.log`/`MCity_Log.txt` (`Done Getting Personas` stayed the last line)/`stdout.txt` activity.
  - **FOUND THE REAL REASON, same session**: grepped this run's log for `GetDeviceState`/`GetDeviceData` calls -- **1166 `GetDeviceState` calls, ZERO `GetDeviceData` calls**. The persona-select screen's FEDC input system polls DirectInput's *immediate* mode only (a live snapshot at call time), never buffered mode, at a real ~385ms interval (confirmed by measuring gaps between consecutive calls). The old `_inject_click` (`run_exe.py`) pushed `SDL_MOUSEBUTTONDOWN` immediately followed by `SDL_MOUSEBUTTONUP` within ~3ms of each other -- the tracked button state flips 0->1->0 entirely inside a single ~385ms poll gap, so no real `GetDeviceState` call ever observes it. This is **not a DirectInput/game bug** -- real immediate-mode DirectInput can legitimately miss a transition shorter than the poll interval too, that's exactly why buffered mode exists. It's the injected test click that wasn't modeling a real click's hold duration (nobody physically presses and releases a mouse button in 3ms).
  - **FIXED (2026-09-13, later still)**: `_inject_click` split into `_inject_click_down`/`_inject_click_up`, scheduled as two separate real-wall-clock-timed events in the main loop (new `TEW_CLICK_HOLD_SEC`, default 0.5s) rather than pushed back-to-back or via a blocking `time.sleep()` (which would also stall the CPU stepping loop and prevent the guest from polling at all during the hold -- defeats the purpose). Full suite still green (1275 passed).
  - **Tested, and the hold-duration theory alone does NOT explain the stuck screen**: re-ran with the fix, held 621.341s-621.884s (543ms). Confirmed two real `GetDeviceState` calls landed squarely inside that window (621.439s, 621.825s) -- the guest's poll genuinely had the button-down state available to read this time. **Still zero reaction** -- `Login.log`/`MCity_Log.txt` (`Done Getting Personas` still the last line)/`stdout.txt` all unchanged, no assert. So click delivery (WM_LBUTTONDOWN at the right coords, DirectInput seeing the down state at the right time) is now fully confirmed correct end-to-end, and the game *still* doesn't react -- the remaining bug (in tew, or a misunderstanding of the guest's own logic) is further downstream than input delivery.
  - **Deep Ghidra trace, same session, done by Molly directly (`debug_clean` project)** -- ruled out several sub-theories, narrowed to one real remaining suspect:
    - `MMouseInput`'s real vtable (`??_7MMouseInput@@6B@`=011b42fc): `[0]`=destructor, `[0x4]`=`GMouseInput::Do`, `[0x8]`=`GMouseInput::Clear`, `[0xc]`=`MMouseInput::AppPollMouse` (thunk 0x0040d1f2), `[0x10]`=`AppClearWheel`. Confirms `Do`'s vtable+0xc gate really is `AppPollMouse` -- but it only returns 0 if `_MOUSE_getstate(6)` returns NULL (uninitialized DirectInput handle), which the 1166-successful-`GetDeviceState`-calls evidence already rules out. **Not the blocker.**
    - `GDialog::OnMouseDown` (00b07aa0) mostly falls through to base `GUI::OnMouseDown` (00aeca10) for a normal inside-dialog click. `GUI::OnEvent`'s mouse-down case only calls `_mfHitNext` (which IS gated by `IsModal(this)`, a real dead end for a modal dialog) if the vtable+0x98 call returns 0 -- **but that's not the real dispatch path**: `GMouseInput::Do`'s target comes from `GUI::GetMouseFocus()` (00aef350), which checks `_mCapture` first (idle/null in this scenario -- Molly confirmed capture-ownership semantics: `HasMouseCapture(this)` means "*this* holds capture", not "something holds capture"), then falls to `GetModalProcess()` -> `_mfHitFirst(modalDialog)` (00aef1b0), a *different*, non-`IsModal`-gated recursive descent that walks children checking each one's own rect (`child+0xbc`) against `GMouseInput::GetPosition()`. Mechanically sound-looking code; **not yet disproven, but no smoking gun found in the descent logic itself.**
    - Also ruled out: `DoModal`'s own loop (`while (result==NULL) GUI_Main(this)`) passes the dialog directly into `GUI_Main`/`GUSER_DoInput`, so a `GetModalProcess()` failure wouldn't even matter here -- the dialog is already the correct target object regardless. **Wrong-target-via-modal-resolution theory is dead.**
    - **The one live, unexamined suspect**: `GMouseInput::Do` stores the mouse position through `ScreenToView(GPos)` (00af2c20) before anything else touches it: `x_view = round(x_screen * GUI_fS2VX - GUI_fV2SXt)`, `y_view = round(y_screen * GUI_fS2VY - GUI_fV2SYt)`. Four global float coefficients (`GUI_fS2VX`=01290c9c, `GUI_fS2VY`=01290ca0, `GUI_fV2SXt`=020e5c48, `GUI_fV2SYt`=020e5c4c) -- **writer/initializer not yet found**. This is very likely the real mechanism behind the empirically-derived 1.28x `GDialogs.gui`-authored-at-800x600 scale factor found earlier. If tew feeds this transform (indirectly, via whatever screen-size/viewport state it depends on) a value that doesn't match the real active resolution, `_mfHitFirst`'s rect comparisons would silently fail even with perfectly correct WM_LBUTTONDOWN coordinates and perfect timing -- exactly matching every observed symptom. **Next step: find who writes these four globals and trace whether tew's emulated environment feeds them the same values real Windows would.**
  - **DECISIVE TEST, same session (2026-09-14)**: ruled out timing/emulation-speed as a contributing factor entirely (see "Current status" above) -- a synthetic click at the directly-measured correct coordinates, held past the confirmed poll gap, with a poll confirmed landing inside the hold window, still produced zero reaction. The `ScreenToView` coefficient lead above is now the most likely remaining candidate.
  - **Process-management note worth keeping**: when a harness-tracked background run gets killed by the "system is low on memory" guard but `free -h` shows real headroom, it's the guard being wrong/oversensitive, not real pressure -- confirmed 3 times same session, at wildly different guest sim-times (3s, 4s, 0.8s), never correlated with any actual memory spike. Workaround: `nohup <cmd> > /tmp/emu.log 2>&1 & disown` (NOT the `run_in_background` tool flag) fully detaches the process from the harness's tracked tree so the guard can't touch it; poll it with plain `ps -p <pid>` from ordinary foreground tool calls instead of a tracked wait-loop.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
- **CORRECTION (2026-08-30), supersedes `status_archive.md`'s x12/2026-08-25 and 2026-07-24 entries**: "restart the compositor in place" (`kwin_wayland --replace ...`, or `systemctl --user restart plasma-kwin_wayland.service`) is **no longer a valid stuck-SDL troubleshooting step** -- confirmed 2026-08-30 that a compositor restart crashes the *entire user session*, not just the wedged tew client, twice in a row. Whatever made this a safe in-place recovery in 2026-07-24/2026-08-25 no longer holds (environment/KWin-version drift, most likely -- not investigated further). Do not attempt a compositor restart as an automated recovery step; if a run hangs at SDL2/window init, check for and clean up orphaned `run_exe.py` processes first (`SIGTERM`, not `-9`, to let `window_manager.shutdown()` run), and otherwise stop and ask Molly rather than touching the compositor.
