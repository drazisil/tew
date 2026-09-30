# TODO

Durable follow-up work, checked/updated as picked up. Distinct from status.md
(current session's active blocker) and changelog.md (completed work) --
items here are queued but not yet started, or started and paused.

---

## NEW (2026-09-26): perf pass, remaining items (tasks 1-4 done, see changelog)

Tasks 1-4 done (changelog 2026-09-26 and 2026-09-29): 1.44x to 600M guest
steps. `CompareStringA` (was 4.5%) is gone since PR #28 (DAO350 no longer
over the heap, so it takes its `_stricmp` path). Remaining, biggest first:
- New in the 2026-09-29 profile (texture-upload phase, reached ~12% sooner
  now): D3D8 `UnlockRect` -> `_convert_to_bgra8` ~30% inclusive, the biggest
  single item once that phase starts.
- `simple_alloc` (`tew/api/_state.py`): first-fit linear scan of a free list
  that is never coalesced or trimmed, so it grows and every alloc walks it.
- sprintf/`_write_cstring`/write8 write strings byte by byte (bulk
  write_bytes); `eip`/`eflags`/`get_flag` crossings ~3% each (Desktop's list).

- RESOLVED (2026-09-29): critical sections the XP way (perf task 4) → status_archive.md "Resolved TODOs"

## NEW (2026-09-29): remaining nested `_invoke_emulated_proc` calls still give up and rewind

`DispatchMessageA` no longer uses it (PR #31: the WndProc runs as guest
code via a stack trampoline -- the fix for the intermittent lobby-load
crash). The other callers still run guest code as a nested `cpu.run` with a
step budget, and on exhaustion `restore_state` rewinds the CPU to the moment
of the call, even if other threads ran in between: timer callbacks
(`kernel32_system.py`, timeSetEvent), message hooks (`user32_handlers.py`
CallNextHookEx / PeekMessage hook paths), CreateDialogParamA's
WM_INITDIALOG and CreateWindowEx's creation messages, `patch_internals.py`,
`exception_diagnostics.py`, and DllMain calls. All are short today. Any of
them that can run long or block (a timer callback waiting on another
thread) should move to the same trampoline pattern.

## NEW (2026-09-30): lobby home screen leftovers -- 301 news ticker, Avg. Player Level

The 2026-09-29 placeholder findings (raw `kTxtChannel*Racing` rows,
`PlayerName` header, 100000 / $123,456,789 values) were all the wrong loose
GUI set in `Data/GUI` -- resolved by replacing it, see changelog 2026-09-30.
The earlier "exe/data version mismatch", "`Lobby_GetRaceTypeName` has no
callers" (its thunk 0x004034d1 has 14) and "UPDATEUI never reaches the side
frame" conclusions were wrong; don't reuse them. Still open:
- **Headline ticker shows `<html><head><title>301 Moved Permanently...`.**
  The news fetch gets a 301 and the body is displayed. Real WinINet follows
  redirects unless `INTERNET_FLAG_NO_AUTO_REDIRECT` is set -- check tew's
  `InternetOpenUrlA` / `InternetReadFile` first.
- **"Avg. Player Level: 83,886,080"** (0x05000000). Not a view default;
  find what computes it.
- "No Active Car" / empty MY CAR is correct for the server data (persona 21
  owns no car; mco-rust grants one only via the new-persona starter screen).

## NEW (2026-09-26): thread stacks have no upper bound

The scheduler (`cpu/src/scheduler.zig`) bumps each new thread's stack upward
from `THREAD_STACK_BASE` (0x08000000) by 256 KB and never reuses or caps it.
PR #28 reserves 0x08000000-0x0FFFFFFF for them in the loader; past ~512
threads they would run into the DLL slots at 0x10000000 with no diagnostic.

## NEW (2026-09-26): FEUI dialog backgrounds don't render

The Exit confirmation dialog shows only its YES/NO button sprites over the
lobby screen; the dialog's own background panel is not drawn. Part of the
general FEUI rendering gaps (alongside the known font and 3D model issues).

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

- RESOLVED (2026-09-18): `screen.c(475) width>=0&&height>=0` → status_archive.md "Resolved TODOs"

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

- RESOLVED (2026-09-18): synthetic clicks never registered → status_archive.md "Resolved TODOs"

- RESOLVED (2026-09-18, this entry's 2026-09-17 conclusion was WRONG -- corrected below): real crash right after a live `MC_LOGIN_COMPLETE` → status_archive.md "Resolved TODOs"

- RESOLVED (2026-09-16, fixed later same day): guest `recv()` blocking with no data ready freezes the ENTIRE emulator, not just the calling guest thread → status_archive.md "Resolved TODOs"

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

- RESOLVED (2026-09-13): `IDirect3DDevice8::Reset` was a complete lying no-op, clipping the persona-select screen → status_archive.md "Resolved TODOs"

- RESOLVED (2026-09-18, opened 2026-09-13): real mouse/keyboard interaction with the persona-select screen → status_archive.md "Resolved TODOs"

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

- RESOLVED (2026-09-05): D3D8 game window now receives real mouse/keyboard input → status_archive.md "Resolved TODOs"

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

- RESOLVED (2026-08-29, cont'd x40): DAO/Jet query-parameter gap → status_archive.md "Resolved TODOs"

- RESOLVED (2026-08-29, cont'd x40): `FUN_77121505` "thread splat" suspicion → status_archive.md "Resolved TODOs"

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

- RESOLVED (2026-09-29, opened 2026-08-26): `THREAD_SENTINEL` collision between `_call_guest_void` (static initializers) and real thread completion → status_archive.md "Resolved TODOs"

- RESOLVED (2026-08-26): 101 `test_oleaut32_*.py` unit tests and dead `oleaut32_handlers.py` cleaned up → status_archive.md "Resolved TODOs"

- RESOLVED (2026-08-26): statically-imported DLLs' `DllMain` now runs; original DAO license-key BSTR bug confirmed fixed → status_archive.md "Resolved TODOs"

- RESOLVED (2026-08-26): `kernel32.dll!SearchPathA` and `SearchPathW` implemented → status_archive.md "Resolved TODOs"

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

- RESOLVED (2026-08-27/28, cont'd x36/x37): "Database initialization failed!" cleared → status_archive.md "Resolved TODOs"
