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

## Current status (2026-09-14, very late) — persona-select click bug RESOLVED, root cause and fix confirmed live end-to-end

**Root cause**: real `WM_MOUSEMOVE`/`WM_LBUTTONDOWN`/`WM_LBUTTONUP` always carry the live `MK_LBUTTON`/`MK_RBUTTON`/`MK_MBUTTON` bits in `wParam`. `window_manager.py`'s `_handle_sdl_event` hardcoded `wParam=0` at all three post sites. The guest's mouse tracking turned out to be on a **legacy path** (`seteacmouse`, reached via `_MESSAGE_handler`→`FUN_00780d80`→`seteacmouse`) rather than DirectInput -- this game's DirectInput `GetDeviceState` mouse branch (`DAT_0128af04 != 4` gate in `_MOUSE_getstate`) never actually fires; only the keyboard's 256-byte polls do. `FUN_00780d80` reads button state exclusively from `wParam`'s `MK_*` bits, so with `wParam` always 0 the guest's own click-tracking global (`DAT_020e398c`) was always set to "not pressed" regardless of the real button state -- explaining every earlier "correctly delivered, correctly timed, still zero reaction" result this session.

**Fix** (`tew/api/window_manager.py`): added `_sdl_buttons_to_wparam()`, computing real `MK_*` bits from `SDL_GetMouseState`/`event.motion.state`, applied at all three call sites. Three lines of actual logic change.

**Verified live, end to end**, via CPU logpoints on `GMouseInput::Do`, `GButton::OnMouseUp`, `GDialog::OnNotify`, and the raw `GetDeviceState` buffer: `Do()`'s `raw_down0` now reads `1` on a real press (was always `0`, confirmed across 168+ sampled polls spanning multiple real, multi-second click holds before the fix); `OnNotify` sees `event=42` (button pressed) with `notifier_handle` exactly matching `GDialog+0x114`; `Login.log` progresses past persona-select (`Persona Selected: 21` → `PLS use selected` → `PLS connect`); screenshot-confirmed a real "Connecting to Motor City Server" dialog on screen.

**Full investigation history** (every false lead ruled out along the way -- coordinate scaling, click-hold/poll-gap timing, enable/visible flags, OK/CANCEL handle wiring, `ScreenToView` coefficient staleness, and a long detour into DirectInput internals before discovering the legacy branch was active all along) is archived in `status_archive.md`'s matching 2026-09-14 entry -- read it before re-investigating any input-dispatch question in this codebase, several of these are easy to re-hypothesize from scratch.

**Two real, separate environment bugs found and worked around along the way** (not tew bugs, but cost real time before being isolated):
- Under native Wayland, real OS mouse button events (down/up) never reached SDL at all for tew's window, while motion events did -- fixed for manual testing by launching with `SDL_VIDEODRIVER=x11` (forces XWayland). `xdotool`/`_NET_CLIENT_LIST` still cannot see or programmatically click the window even under this mode -- no synthetic OS-level click is currently possible from an unattended agent turn; use `TEW_CLICK_AT`/`TEW_CLICK_AFTER_SEC` (tew's own `SDL_PushEvent` injection) instead when a human isn't available to click, and say so explicitly.
- Even under `SDL_VIDEODRIVER=x11`, KWin appears to swallow the first click after any focus change as a pure focus-grab (no button event reaches the app). Click the window twice in quick succession (first refocuses, second registers) as a reliable workaround -- this bit repeatedly when Bash tool calls between checks kept stealing focus back to the terminal.

**Debugging-process notes worth keeping**:
- `TEW_MAX_STEPS` (not just wall-clock `timeout`) can silently end a run early -- default `500000000` only covers ~180s real time, well short of persona-select (~250-350s). Use a generous multi-billion budget for any run expected to reach that screen.
- `LOG_LEVEL=info` silently hides `SDL event type=...`/`[dinput] real mouse button...`/`DispatchMessageA`, all DEBUG-level -- confirmed this cost real time mid-session (briefly looked like "no clicks happening" when it was actually just the wrong log level). Use `LOG_LEVEL=debug` whenever click/input delivery itself is in question, not just `info`.
- Guest-written files (`~/.emu32/MCity/stdout.txt`, `~/.emu32/Login.log`) are NOT subject to tew's own `LOG_CATEGORIES` filtering and are often the more reliable source for "did anything actually happen" checks than tew's own log.
- Live CPU logpoints (`cpu.add_logpoint`, 8-slot cap) reading real guest memory were what actually closed this out -- static Ghidra decompilation got close on every sub-question but never definitively settled one on its own. `run_exe.py` keeps this session's probes as commented-out reference for the next input-dispatch investigation.

Not yet tried: DXT/S3TC decompression, multitexturing (stage > 0). tew is currently hardcoded to decline any "run in fullscreen?" prompt the game shows -- known, deliberate, not a bug. Not yet done: `vkQueueWaitIdle` stall after every texture upload (`_pipeline.py:523`) is still the likely dominant remaining graphics-speed bottleneck, scoped but untouched this session.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
- **CORRECTION (2026-08-30), supersedes `status_archive.md`'s x12/2026-08-25 and 2026-07-24 entries**: "restart the compositor in place" (`kwin_wayland --replace ...`, or `systemctl --user restart plasma-kwin_wayland.service`) is **no longer a valid stuck-SDL troubleshooting step** -- confirmed 2026-08-30 that a compositor restart crashes the *entire user session*, not just the wedged tew client, twice in a row. Whatever made this a safe in-place recovery in 2026-07-24/2026-08-25 no longer holds (environment/KWin-version drift, most likely -- not investigated further). Do not attempt a compositor restart as an automated recovery step; if a run hangs at SDL2/window init, check for and clean up orphaned `run_exe.py` processes first (`SIGTERM`, not `-9`, to let `window_manager.shutdown()` run), and otherwise stop and ask Molly rather than touching the compositor.
