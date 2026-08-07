# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here. Full investigation history (the DAO `*ppv` NULL saga, `tid=1012`'s death, the `fatal_halt` fix, etc.) lives in changelog.md, newest-first — do not re-derive any of it from scratch, grep changelog.md instead.*
---

## Current status (2026-08-07, cont'd again)

**The `Workspace::OpenDatabase`/error-3343 blocker below is RESOLVED, and it
was two real tew bugs in the Win32 file-I/O layer, not a Jet/DAO problem at
all** -- confirmed by Molly's own instinct going in ("zero chance this is a
Jet bug... if anything, it's that we are running windows files under linux
encoding"), which was exactly right.

Traced live via Ghidra decompile of the real DAO350.DLL/MSJET35.DLL chain
(project `debug_clean`) plus `cpu.add_logpoint`s, including working out
MSJET35.DLL's real runtime-vs-Ghidra-static address delta
(`runtime = static - 0x65840000`, verified against the known-good
`opMovR32Imm` landmark instruction). The full real call chain: DAO350.DLL's
`FUN_0448c745` (`Workspace::OpenDatabase` wrapper) &rarr; a vtable delegation
chain (`FUN_044c5ee9` &rarr; `FUN_044c2d8a`) &rarr; `FUN_044e20c8` &rarr;
`FUN_044d896d` &rarr; a dynamically-bound ISAM function-pointer table
(`DAT_044e52e8` etc., resolved at runtime into MSJET35.DLL) &rarr;
`FUN_7a8701ed` &rarr; `FUN_7a85a900` &rarr; `FUN_7a86fac5` &rarr;
`FUN_7a86fbed` &rarr; `FUN_7a8709b6` &rarr; `FUN_7a870879` &rarr;
`FUN_7a8708a1` (the real `CreateFileA`/`GetFullPathNameA`/`FindFirstFileA`
path-resolve-and-open) &rarr; `FUN_7a8706e9` (the actual `CreateFileA` call,
confirmed requesting `GENERIC_READ|GENERIC_WRITE`) &rarr; `FUN_7a870b40`,
which calls real `GetFileType()` via `FUN_7a8709a5` before ever reading a
byte, and fails immediately if the result isn't exactly `FILE_TYPE_DISK`.

Two real bugs found, both in `tew/api/*.py`, both fixed:

1. **`kernel32_io.py`'s `_create_file_a`/`_create_file_w` collapsed
   `dwDesiredAccess` into a single `writable` boolean**, discarding whether
   `GENERIC_READ` was *also* requested alongside `GENERIC_WRITE`.
   `open_file_handle` (`_state.py`) always opened the real fd with
   `os.O_WRONLY` for any writable open, never `O_RDWR` -- and
   `kernel32_io.py`'s `ReadFile`/`msvcrt_handlers.py`'s `fread`/`_read`
   unconditionally rejected *any* handle flagged writable, regardless of
   what the fd could actually do. A real Win32 handle opened
   `GENERIC_READ|GENERIC_WRITE` (exactly what Jet requests for a live
   database file) supports both `ReadFile` and `WriteFile`; ours could only
   ever write. Fixed: `FileHandleEntry` gained a `readable` field,
   `open_file_handle` gained an `also_readable` parameter (opens `O_RDWR`
   when set), `_create_file_a`/`_create_file_w` now check `GENERIC_READ`
   too, `fopen`'s mode-string parsing now checks for `"+"`, and
   `ReadFile`/`fread`/`_read` now do a real `os.pread()` for handles that
   are both writable and readable. **Confirmed via live logpoint this was
   real and engaged correctly** (`CreateFile(...) -> 0x5041 [write+read]`)
   **but was NOT the actual root cause of this specific blocker** -- `ReadFile`
   is never even called before the real failure point, confirmed by tracing
   further.
2. **`kernel32_system.py`'s `GetFileType` had its own logic backwards**:
   `cpu.regs[EAX] = 2 if entry.fd is not None else 1` -- exactly inverted
   from its own comment ("FILE_TYPE_CHAR(2) for std handles... FILE_TYPE_DISK(1)
   for files"). Every real disk file (read-write *or* write-only) also keeps
   a live fd open, so this reported `FILE_TYPE_CHAR` for every real file and
   `FILE_TYPE_DISK` only for the read-only-with-cached-data case (`entry.fd
   is None` there). Real `Workspace::OpenDatabase` calls `GetFileType()`
   immediately after `CreateFileA` and aborts with error `-0x404` if the
   result isn't exactly `FILE_TYPE_DISK` -- **before ever calling `ReadFile`
   or checking the "Standard Jet DB" signature**, fully explaining why
   `FUN_7a870cf8` (the signature-check function, found earlier via a string
   search) never actually fired despite genuinely being in the call graph:
   the `GetFileType` gate rejects the open before ever reaching it. Fixed to
   key off `entry.path` instead (`'<...>'` sentinel paths and `/dev/null`
   are the only real `FILE_TYPE_CHAR` cases; everything else with a real
   path is `FILE_TYPE_DISK`), matching how std handles/NUL are actually
   modeled. **This was the real root cause** -- confirmed live: the
   `0x800a0d0f`/"unrecognized database format" failure is completely gone,
   `Workspace::OpenDatabase` now proceeds cleanly past the entire chain
   above it ever reached before.

Along the way, also confirmed (and this is worth keeping in mind for
future investigations, not something to redo): `Online.MDB`'s file
integrity was never in question -- byte-identical to a second, independently
obtained copy (`/data/Downloads/Motor City Online/Data/DB/Online.mdb`,
different size/date but identical header including the byte at offset
`0x42` that earlier looked like a password flag -- that theory is now known
wrong, see below), size is an exact whole number of real Jet-3.x 2048-byte
pages, and `mdb-tools` (an independent, non-Microsoft Jet parser)
successfully extracted its full schema/data back in 2025. The earlier
"password-protected, `FUN_7a870cf8`'s byte-0x42 gate never satisfied"
theory from the previous entry below is **superseded and was based on an
incomplete trace** -- `FUN_7a870cf8` genuinely is in the real call graph
(called from `FUN_7a870b40`), just never reached in practice because
`GetFileType`'s bug rejected the open one step earlier every time.

1061/1061 tests pass. New regression tests: `test_read_write_file_handle.py`
(the full `CreateFileA`&rarr;`WriteFile`&rarr;`ReadFile` round trip on a
`GENERIC_READ|GENERIC_WRITE` handle, plus a write-only-still-rejects guard),
and `TestGetFileType` additions in `test_kernel32_system_info.py` (real disk
file &rarr; `FILE_TYPE_DISK`, NUL device still &rarr; `FILE_TYPE_CHAR`).

**New blocker surfaced by this fix**: `[UNIMPLEMENTED] kernel32.dll!LockFile
-- halting`, hit shortly after `Workspace::OpenDatabase` succeeds (real Jet
trying to lock the database file, real address `EIP=0x002092c2`, inside
`MSJET35.DLL`+0x5532 per the halt diagnostic's own stack annotations). A
clean, honest, well-understood gap -- not a mystery -- `LockFile`/`UnlockFile`
simply aren't implemented yet.

**Current blocker**: implement `kernel32.dll!LockFile` (and its
`UnlockFile` counterpart, not yet checked for a matching gap). Not yet
started.

## Previous status (2026-08-07, cont'd)

**The `Tmp.MDB`-never-gets-created mystery below is RESOLVED, and the root
cause was tew's own code, not a missing dependency or a guest-code
mystery.** Traced via Ghidra (real `MCity_d.exe` decompile, `debug_clean`
project) plus live logpoints (`cpu.add_logpoint`, capped at 8 slots --
`cpu/src/core.zig:113`/`kernel.zig:203-206`, exceeding it silently drops
registrations with no error, a real gap worth fixing separately, not done
here): `Tmp.MDB` is created exactly once, early in `WinMain`, by
`Dbcode_CopyDataBaseToSaveData` (real address `0x008ED560`) via
`FeTools_CopyFile(dest="...DB\Tmp.MDB", source="...Online.MDB")` -- a real
`fopen`/`fread`/`fwrite` copy loop, nothing to do with `DB_StartUpDatabase`'s
later `OPEN_EXISTING` open at all (the "should retry with CREATE_ALWAYS"
theory in the previous entry below was wrong, see `_state.py`'s corrected
`open_file_handle` docstring). The actual bug: `tew/api/patch_internals.py`
had a patch, `_winmain_check3` (dating to when this function was still
"unnamed" in Ghidra), that unconditionally forced `EAX=1` (success) at
`Dbcode_CopyDataBaseToSaveData`'s entry -- skipping its real body
entirely, including the copy, every run, forever. Molly confirmed the real
source template, `Online.MDB`, has always genuinely existed on disk
(`~/.emu32/Data/DB/Online.mdb`, 5,883,904 bytes) -- there was never a
reason for this patch to exist. Removed it. Confirmed live: the real copy
now runs (`CreateFile("C:\Data\DB\Online.MDB") -> [read, 5883904 bytes]`,
streamed via real `ReadFile`/`WriteFile`), and `~/.emu32/SaveData/DB/Tmp.MDB`
now exists on disk, byte-identical in size to `Online.MDB`. 1045/1045 tests
pass (one test, `TestWinmainCheck3`, removed along with the patch it tested).

Also this session: added the `-CaptureStdout` command-line flag (confirmed
via Ghidra against the real `NFSArgs_ProcessArgs` switch table, row 13) --
`WinMain` was redirecting the game's own `stdout` to the NUL device;
`stdout.txt` (gitignored, not committed) now captures real `puts()`/
`printf()` output, confirmed live with `_CLayer_DetectDebugger`'s
`"Causing exception to test for debugger...\nFound Debugger!"` lines
landing in it exactly as predicted from the decompile.

**New blocker surfaced by this fix**: `DB_StartUpDatabase` still hits the
identical `INT3`/`cpu.fatal_halt at EIP=0x001fe012` on `tid=1012` (same EBP
chain) even with `Tmp.MDB` now present and openable
(`CreateFile("C:\SaveData\DB\Tmp.MDB") -> [write]`,
`GetFileInformationByHandle` confirms the real 5.6MB size). So the raw
file-level open now succeeds, but DAO's `Workspace::OpenDatabase` COM call
(vtable `+0x58` on the Workspace object) still returns a NULL database.
`except.txt`/`dblog.txt` show only the same generic `ERROR: open database
'C:\SaveData\DB\Tmp.MDB' failed.` / `dbcode.c(1709) ERROR: open database
failed.` text -- no more specific reason at this log level. Not yet
investigated: real Jet-format validation, a missing dependency specific to
opening a *populated* database (vs. the empty/auto-created `system.mdb`
case already solved 2026-08-04), or something else entirely.

**Current blocker**: diagnose why `Workspace::OpenDatabase` fails on a
real, present, correctly-sized `Tmp.MDB`. Not yet started.

## Previous status (2026-08-07)

**Correction to the "no blocker identified" claim below (2026-08-06, cont'd
again): that was wrong, or at best described a run that never actually hit
this path.** A fresh run today (post the `CreateFile` logging-clarity fix,
see changelog.md "2026-08-07") halts at ~59s, well before any message-pump
steady state, on a reproducible `cpu.fatal_halt at EIP=0x001fe012` --
`INT3 breakpoint at EIP=0x00688c68 unhandled by SEH chain` on `tid=1012`,
same EBP-chain signature (`0x0068adf2`/`0x00a301a1`/...) as the
`Nfs_REALabortcallback`/`DebugBreak()` assertion pattern diagnosed
2026-08-03. `tid=1012` still has no `_CLayer_CatchSEH` coverage (confirmed
main-thread-only back then), so the halt itself is correctly-behaving, not a
tew bug in the halt mechanism.

**New evidence this session, added to the standing debug toolkit (see
emu32 skill v1.9, "Post-Run Checks")**: two guest-written files the
emulator's own `/tmp/emu.log` never captures, both confirming the exact
failure independently of register/EBP-chain inference:
- `/data/Code/tew/except.txt` (the game's own exception dump):
  `ERROR: open database 'C:\SaveData\DB\Tmp.MDB' failed.`
- `/home/drazisil/.emu32/dblog.txt` (MSJET35.DLL's `-dbEnableLog` trace, real
  `dbcode.c` source lines from the shipped Jet 3.5 engine):
  ```
  dbcode.c(1691) DB_StartUpDatabase
  dbcode.c(1698) C:\SaveData\DB\Tmp.MDB
  dbcode.c(1709) ERROR: open database failed.
  ```

**Root cause, confirmed reproducible (not flaky)**: `~/.emu32/SaveData/DB/`
is genuinely empty on disk (verified via `ls`) -- both `C:\system.mdb` and
`C:\SaveData\DB\Tmp.MDB` are missing, so DAO/Jet's `DB_StartUpDatabase`
(`FUN_0448c745` in `dao350.dll`, per last session's live logpoints) correctly
gets an honest `OPEN_EXISTING` failure from `CreateFile` for both -- this is
tew behaving correctly per the 2026-08-04 `open_file_handle` disposition fix,
not a regression. What's unconfirmed: `open_file_handle`'s own docstring
claims real DAO/Jet is supposed to retry `Tmp.MDB` with
`CREATE_ALWAYS`/`CREATE_NEW` after this miss (it's meant to be created
fresh, not pre-provisioned) -- but no such retry is observed; `dbcode.c`
goes straight to `ERROR: open database failed.` and the game asserts.
Either (a) that retry claim was wrong/inapplicable to this specific
`DB_StartUpDatabase` call path, or (b) something upstream that would enable
Jet's real retry logic is still missing from tew.

**Current blocker**: determine which of (a)/(b) above is true. Needs Ghidra
on `MCity_d.exe`/`0x00688c68`/`0x0068adf2` (the assertion call site) and/or
`dao350.dll`'s `FUN_0448c745`/`DB_StartUpDatabase` real disassembly to see
whether a `CREATE_ALWAYS` retry path exists in the real binary and why it
isn't being taken. Not yet started.

## Previous status (2026-08-06, cont'd again)

**The `~85 of ~90 cpu.halted = True sites lack fatal_halt` item -- deliberately
deferred since 2026-08-04 -- is now RESOLVED.** Prompted by tracing why
`VariantChangeType (Ordinal 12)`'s "halting" log didn't actually stop
execution (led to a `RUNAWAY` ~100k steps later): confirmed that plain
`cpu.halted = True` gets silently cleared by any of ~9 scheduler/nested-
call-bookkeeping sites, while `cpu.fatal_halt = True` is the only thing the
native layer (`cpu_clear_halted`) actually refuses to undo -- and most
individual handler halts across the codebase were never using it.

Surveyed every `<var>.halted = True` write site (86 total, via a script
checking the following line for `fatal_halt` rather than a same-line grep,
which undercounts -- e.g. `win32_handlers.py`'s INT3 handler already had it
on the next line). 85 were genuine unrecoverable-error halts (the
established `logger.error(...) + "halting"/"UNIMPLEMENTED"/"failed"`
shape, plus three clean process-exit calls -- `ExitProcess`,
`TerminateProcess`, `NtTerminateProcess` -- correctly permanent even though
logged at INFO not ERROR) and got `fatal_halt = True` added. One,
`seh.py`'s `_sentinel_handler`, is a legitimate resumable step-loop
completion signal (fires when a nested SEH handler call returns
*normally*) and was correctly left alone.

**One genuine false positive, caught by the existing test suite and
reverted**: `scheduler.py`'s `mark_current_dead` "no runnable threads
remain" halt was assumed to always mean a clean process exit and initially
marked fatal -- broke
`test_invoke_emulated_proc_thread_death.py::test_invoke_emulated_proc_returns_zero_when_calling_thread_dies_mid_call`,
which explicitly asserts `cpu.fatal_halt is False` for the case of a
*single* thread dying mid-nested-call (e.g. `ExitThread` from inside
`_invoke_emulated_proc`) -- a real, designed-for, recoverable scenario, not
a whole-process exit. Reverted that one site, left the other 84 in place.
1029/1029 tests pass.

**Confirmed the native boundary needed no changes at all.** Traced
`cpu.halted`'s Python property (`cpu_zig.py`) end to end: the getter is
`self._py_halted or cpu_is_halted(state)` and the setter's clear path calls
native `cpu_clear_halted`, which already refuses to actually flip
`s.halted` back to false once `s.fatal_halted` is set -- so even the two
call sites in `user32_handlers.py` (177, 220) that clear `cpu.halted`
*without* an explicit `if not cpu.fatal_halt` guard can't actually resume
a genuinely fatal halt: native `s.halted` stays true regardless (the guard
lives at the native layer, not the call site), and `cpu_run`'s own
execution loop reads that native flag directly, never the Python shadow.
`cpu.fatal_halt` itself is untouched by the `halted` setter entirely. The
architecture already fully enforced "Python has zero ability to restart a
fatal halt" -- the actual gap was 85 individual handlers never opting into
the existing mechanism, not a hole in the mechanism itself.

## Previous status (2026-08-06, cont'd)

**The `n=0xfffffffc` mystery queued earlier today is RESOLVED, and it was a
real Zig CPU-core bug, not a DAO/Jet or tew-handler issue.** Traced the
exact call site to `DAO350.DLL`'s `FUN_044d1f27` (a sorted name-index
insert routine) and its `FUN_044d1d98` binary-search helper, live, via
targeted `cpu.add_logpoint`s reading real register/memory state (not
guessing from the decompile alone). Root cause: `doGroup1`
(`cpu/src/engine.zig`, the shared handler for opcodes `0x80`/`0x81`/`0x83`
— ADD/OR/ADC/SBB/AND/SUB/XOR/CMP against an immediate) hardcoded `.w32`
for its flags computation in every case, never checking `s.op_size_ovr` —
the exact same `0x66`-prefix flags-width bug class already fixed elsewhere
in this project (accumulator-immediate opcodes, `doGroup2`), but never
audited here, matching the open "broader audit" item this queued-issues
list already flagged. The real guest instruction was `66 83 7C 24 0C 00`
= `CMP WORD PTR [ESP+0xC], 0`, comparing `local_4=-1` (`0xFFFF`) — wrongly
read as `SF=False` (positive, as a 32-bit quantity) instead of `SF=True`
(negative, as the correct 16-bit quantity), so a `JLE` that should have
skipped an `INC` didn't, incrementing an insertion index one past a valid
(empty) collection's bounds and producing `memmove`'s `n=-4`. Fixed:
`doGroup1` now takes a real `width` parameter, computed from
`op_size_ovr` by both callers (`op81`/`op83`), matching the pattern already
used by `op39`/`op3B`/`op3D`/`opA9`. New regression tests (written and
confirmed failing *before* the fix, per Molly's request):
`TestGroup1_16BitFlags` in `tests/unit/emulator/test_opcodes_arithmetic.py`
(4 tests, including a byte-for-byte repro of the real guest instruction
against a memory operand). 1029/1029 tests pass. Confirmed live:
`FUN_044d1f27` now computes `n=0x00000000` (correct, empty-collection
first-insert case) — the emulator sails straight through the entire
DAO/Jet init sequence that blocked every prior session and reaches the
game's real message loop (`GetMessageA`/`WaitForMultipleObjectsEx` steady
state), running stably until killed by timeout rather than crashing. Full
diagnosis: changelog.md, "2026-08-06 (cont'd)".

**Current blocker**: none identified yet — this is the furthest the
emulator has ever reached (past all of DAO/Jet, into the game's own
message-pump steady state). Next session should pick up from here: what
happens if it's allowed to run past the message-pump idle state (does
real UI/gameplay progress happen, or is there a *different*, not-yet-hit
blocker further in), not yet investigated.

## Previous status (2026-08-06)

**The `FUN_0448a033` "hang" queued 2026-08-04 is RESOLVED, and it was never a
hang at all** — a real bug in `_memcpy`/`_memmove`/`_memset`/`_memcmp`
(`tew/api/msvcrt_handlers.py`) made a *slow but genuinely still-running*
process look identical to a stuck one. All four looped `for idx in
range(n)` on a size read straight from guest memory with zero validation;
a garbage/underflowed `n` turned into hundreds of millions of real
`read8`/`write8` FFI calls (minutes of true wall-clock work, not a freeze)
that eventually swept the unbounded `dst`/`src` pointers through tew's own
`0x00200000+` Win32 trampoline region, corrupting the `memmove` trampoline
itself before finally faulting on a genuinely out-of-range address. Every
prior "hung" run was actually just killed by too-short a timeout (`300s`)
before it reached its own honest halt at `354s`. Fixed: all four now call
`memory.is_valid_range()` before touching anything and halt loudly
(`logger.error` + `cpu.fatal_halt`) instead of looping. New regression
tests: `tests/unit/api/test_msvcrt_memfuncs.py` (13 tests, first coverage
these four functions have ever had). 1025/1025 tests pass. Full diagnosis
(gdb-attach live-process investigation, the ClickHouse execution-history
tooling, ~/pe-walker/history-poc setup): changelog.md, "2026-08-06".

**RESOLVED (2026-08-06, cont'd)**: confirmed live, the actual faulting
call was `memmove(dst=0x06f9e014, src=0x06f9e010, n=0xfffffffc)` —
`n` is `-4` as a signed value, `dst`/`src` are real adjacent heap addresses
only 4 bytes apart. This looked like a signed-length computation
(`end - start`-shaped) underflowing somewhere upstream in the DAO/Jet call
chain, not raw garbage — confirmed correct guess: it was a tew emulation
gap, not a guest bug. See "Current status" above.

## Previous status (2026-08-04, cont'd again x3)

**Real progress on the `GetWindow`-adjacent hang, still not fully resolved.**
Two genuine bugs found and fixed while chasing it (both confirmed correct
by decompiling `FUN_0448d1f5`/`FUN_0448a801` in real `DAO350.DLL`):

1. `GetCurrentProcessId()` (`kernel32_system.py`) returned a hardcoded
   `1234`, while `GetWindowThreadProcessId` (`user32_handlers.py`) always
   writes a hardcoded fake PID of `1` -- these never agreed, so any code
   asking "does this window belong to my process" could never succeed.
   Fixed `GetCurrentProcessId` to return `1`, matching the established
   "our fake PID" convention.
2. `GetWindowLongA`/`SetWindowLongA` compared the `nIndex` argument
   (always unsigned via `memory.read32`) directly against negative Python
   int constants (`GWL_STYLE=-16` etc.) -- `0xFFFFFFF0 == -16` is always
   `False` in Python, so every `GWL_*` case silently fell through to the
   generic default, for the entire life of both handlers. Fixed with a
   proper unsigned-to-signed conversion (matching the existing idiom
   already used in `msvcrt_handlers.py`). Also added a real `GWL_HWNDPARENT`
   case (previously relied on the same broken fallback, which happened to
   produce the right answer by accident) and human-readable `GWL_*` name
   logging for both handlers.
3. Also fixed live-discovered gaps in the same investigation:
   `_ShowWindow` never updated its tracked `WS_VISIBLE` style bit (only
   toggled the real SDL window), so `IsWindowVisible` -- newly implemented
   this session -- would have gone stale after any real show/hide.
   `WindowManager` gained a public `all_windows()` accessor (previously
   only single-hwnd lookup existed) needed for `GetWindow`'s Z-order/
   sibling-walk logic.

**Confirmed via live logpoints** (`cpu.add_logpoint`, temporarily wired
into `run_exe.py` -- addresses are real, unrelocated `DAO350.DLL`
addresses since it loads at its preferred base) that neither bug was
actually the root hang cause: `FUN_0448d1f5` (the "find my own top-level
window" idiom) now returns correctly and fast; its caller `FUN_0448a801`
makes one indirect call (`CALL [0x44e5350]`, target `0x150332db` inside
`MSJET35.DLL`) which also returns cleanly (`EAX=0`); and `FUN_0448a033`
(`FUN_0448a801`'s own caller) successfully gets control back at `0x448a16a`.
**But `FUN_0448a033` never reaches either of its own two `RET` sites**
(`0x448a241`, `0x448a281`) -- the actual hang is somewhere in the stretch
between `0x448a16a` and those returns, which contains several more direct
and indirect calls (`0x448a184`, `0x448a19b`, `0x448a1b7`, `0x448a1da`,
`0x448a20b`, `0x448a21c`, `0x448a234`) not yet individually logpointed.

**Current blocker**: same as above -- narrow down which specific call in
`FUN_0448a033` (between `0x448a16a` and its returns) is where execution
actually gets stuck. The temporary diagnostic logpoints are still wired
into `run_exe.py` (clearly marked "TEMPORARY diagnostic logpoints") for
the next session to extend rather than starting over.

## Previous status (2026-08-04, cont'd again x2)

**Major root-cause find.** The `System.mdb`/error-3049/`DebugBreak` saga
from the last few entries is **fully resolved, and it was never a missing
asset or a Jet bug** -- it was a real correctness bug in tew's own
`CreateFile` handling. `open_file_handle`'s writable branch
(`tew/api/_state.py`) unconditionally did `O_CREAT | O_TRUNC` whenever a
write-capable handle was requested, completely ignoring the actual
`dwCreationDisposition` value -- so a normal `OPEN_EXISTING` request
(which must fail honestly if the file is missing, and must never truncate
an existing one) instead silently fabricated an empty 0-byte file every
time. Jet saw that empty file, correctly concluded "not a database I
recognize" (error 3049), and the game's own debug-build assertion fired
in response -- all downstream of the one bug. Fixed: `open_file_handle`
now takes the real `disposition` value and switches on real Win32
semantics (`CREATE_NEW`/`CREATE_ALWAYS`/`OPEN_EXISTING`/`OPEN_ALWAYS`/
`TRUNCATE_EXISTING`, matching the actual OS API), threaded through from
`_create_file_a`/`_create_file_w` (`kernel32_io.py`). Confirmed live:
`CreateFile("C:\system.mdb")` now honestly fails
(`disposition=3`=`OPEN_EXISTING`, file genuinely missing) instead of
faking success -- and Jet's own real fallback (auto-creating a default
workgroup database when none exists) just works, no external asset ever
needed. Neither `DebugBreak` nor error 3049 reproduce at all anymore.
615/615 tests pass.

Also confirmed as a side effect of the investigation: this same bug meant
ANY `OPEN_EXISTING`+`GENERIC_WRITE` open of a real existing file would
have silently truncated its contents to 0 bytes (`O_TRUNC` fired
unconditionally) -- a real, if not yet observed live, data-loss bug fixed
by the same change, not just the `system.mdb` symptom.

**Current blocker**: clean, honest `[UNIMPLEMENTED] user32.dll!GetWindow`
-- not yet implemented. Reached deep inside real `DAO350.DLL` code, well
past the old blocker.

Not yet re-investigated this session (superseded by the above): the
`LoadStringA(hInst=0x0, ...)` systemic empty-description bug -- may
still be worth fixing independently for readability of *future* error
reports, but is no longer on the critical path since the error it was
garbling no longer occurs.

## Previous status (2026-08-04, cont'd)

`GetUserDefaultLangID`, `GetSystemDefaultLangID` (both `kernel32_io.py`,
same fixed en-US value as the existing `GetUserDefaultLCID`), and
`GetShortPathNameA` (real `find_file_ci` existence check; returns the long
path unchanged since this emulator doesn't implement true NTFS 8.3
short-name generation -- matches real Windows behavior with 8dot3name
generation disabled, not a fabrication) are all fixed and confirmed live.
Execution now reaches significantly further: `MSJTER35.DLL`/
`MSJINT35.DLL` load, `CreateErrorInfo`/`SetErrorInfo` succeed for a real
Jet error.

**Current blocker**: that Jet error is **3049**, confirmed from
`msjint35.dll`'s real resource table: *"Can't open database '|'. It may
not be a database that your application recognizes, or the file may be
corrupt."* Right after `tid=1012` (the DAO/Jet worker thread) reports
this via `SetErrorInfo`, `tid=1000` (the **main** thread this time, not
1012) independently hits the same `Nfs_REALabortcallback`/`DebugBreak`
assertion chain seen and fixed earlier today (same EBP frames:
`0x0068adf2`/`0x00a301a1`/`0x00684de7`/`0x006848ee`/`0x004d8c71`/
`0x0068a7d5`/`0x009fcaa6`) -- this is expected, correctly-behaving
`cpu.fatal_halt` (INT3 routed through the real SEH chain, all handlers
decline, same as always), not a bug in tew. Not yet investigated: whether
the actual `.mdb` database file DAO/Jet expects even exists at the path
being used, or whether this is a real bug further up the Jet
open-database call chain.

## Previous status (2026-08-04)

The `EIP=0x001fe012` `Nfs_REALabortcallback`/`DebugBreak` halt from earlier
today is **resolved, and doesn't reproduce at all anymore**. Root cause:
`expsrv.dll`'s own init probes `oleaut32.dll!DispCallFunc`, `ole32.dll!
CoCreateInstanceEx`/`CLSIDFromProgIDEx`/`CLSIDFromProgID` via
`GetProcAddress`, all previously NULL since tew's real (non-stub)
ole32/oleaut32 COM layer never had them. Implemented `CLSIDFromProgID`,
`CLSIDFromProgIDEx`, `CoCreateInstanceEx` for real in
`oleaut32_handlers.py` (registry-driven, same honest-failure pattern as
the rest of that file). `DispCallFunc` intentionally still not
implemented -- generic x86 calling-convention/VARIANT marshaling, a
separate, larger piece of work. Execution now gets substantially further:
genuinely deep into `expsrv.dll -> vbajet32.dll -> DAO350.DLL -> exe`.
`kernel32.dll!GetUserDefaultLangID` (hit right after) is also fixed
(`kernel32_io.py`, same en-US LCID as the existing `GetUserDefaultLCID`).
Full detail: changelog.md, "2026-08-04 (cont'd)".

**Current blocker**: `[UNIMPLEMENTED] kernel32.dll!GetSystemDefaultLangID`
-- same shape as the `GetUserDefaultLangID` fix just made (this emulator
has no real multi-locale concept, so `GetSystemDefaultLangID` and
`GetUserDefaultLangID` should return the same fixed en-US value, `0x0409`)
-- not yet implemented.

Also this session: per-thread `[tid=N]` log tagging added everywhere
(`logger.py`/`run_exe.py`), and two DLL-loader efficiency fixes
(`dll_loader.py`) -- negative DLL-lookup caching, and `patch_dll_iats`
made incremental instead of O(N^2) full-rescans across a run's DLL loads.
Both confirmed live: no repeated "Could not find X" lines anywhere in a
full run, "Patched X/Y new DLL IAT entries" now reports small per-call
batches. Full detail: changelog.md, "2026-08-04 (cont'd)".

## Previous status (2026-08-04)

The `RUNAWAY` at ~195.8M steps queued from 2026-08-03 is **resolved, and
was never a real blocker** -- it was corruption fallout from a genuine
`VirtualAlloc` bug: `_virtual_alloc` (`tew/api/kernel32_memory.py`)
rejected `flProtect 0x1` (`PAGE_NOACCESS`, a legitimate reserve-then-commit
pattern MSJET35.DLL uses) as unimplemented. Fixed by adding it to
`_KNOWN_PROTECT_FLAGS`; 615/615 tests pass. Confirmed live: neither the
`VirtualAlloc` halts nor the `RUNAWAY` reproduce, and MSJET35.DLL now
loads and resolves all ordinal imports cleanly. Full detail: changelog.md,
"2026-08-04".

**RESOLVED (2026-08-04, cont'd)**: the `cpu.fatal_halt at EIP=0x001fe012`
blocker described below traced to the four missing ole32/oleaut32 COM
functions -- see "Current status" above for the fix and the new frontier
(`GetSystemDefaultLangID`). Original diagnosis kept for the EBP-chain
reference:

A new, genuine `cpu.fatal_halt at EIP=0x001fe012`, reached right after
MSJET35.DLL's import resolution. The `EBP` chain matches the
`Nfs_REALabortcallback`/`DebugBreak()` assertion path fully diagnosed
2026-08-03 -- this is the correctly-behaving, properly-marked fatal halt
case (not the soft-halt-that-doesn't-halt bug class, which remains
deliberately deferred -- see queued issues).

## Previous status (2026-08-02)

The DAO `*ppv`-stays-NULL mystery that this project chased across several
sessions (2026-07-19 through 2026-07-23) is **resolved**. Root cause: three
missing dependencies in `dao350.dll`'s real `DllGetClassObject` call chain
(`ole32.dll!CoGetMalloc` entirely unimplemented, plus two `oleaut32.dll`
ordinal-only import aliases missing — ordinals #15 `SafeArrayCreate` and #21
`SafeArrayLock`) were causing the call to abort before reaching its own
(correct) `QueryInterface` code; `_invoke_emulated_proc`'s bare-`0`-on-abort
fallback then made the abort look like a genuine `S_OK` success with a NULL
`*ppv`. All three fixed in `tew/api/oleaut32_handlers.py`. Full diagnosis
and fix sequence: changelog.md, "2026-07-23 (later session)" and the three
entries before it.

Separately, `cpu.fatal_halt` is now a real, unclearable native CPU lockup —
previously a Python-side desync let execution continue past a fatal halt to
a later, unrelated one instead of stopping dead. Fixed at the Zig/CPU layer.
Full detail: changelog.md, "2026-07-23 — cpu.fatal_halt is now a real,
unclearable native CPU lockup."

And the sentinel-collision bug that fix exposed is now fixed too:
`CPU.run()`/`CPU.step()` raise a new `FatalHaltError` the instant
`cpu.fatal_halt` newly becomes true during a call, instead of returning
normally and leaving `_invoke_emulated_proc` to fall back to a bare `0`
that any `HRESULT`-returning caller could misread as `S_OK`. Full detail:
changelog.md, "2026-07-23 (evening session) — `_invoke_emulated_proc`'s
'didn't complete' `0`-return sentinel replaced with a raised exception."

With `oleaut32.dll` ordinal #4 (`SysAllocStringLen`) also fixed the same
way as #15/#21, DAO's entire COM activation chain now completes cleanly
end-to-end: `CoGetClassObject` ×2 and `CoCreateInstance` all return real,
non-fake results, and execution genuinely **returns to the game's own
code** (`MCity_d.exe`) for the first time — the "ole32 block" that
motivated this whole multi-day investigation is fully cleared. Two more
small gaps found and fixed live-verifying that: `kernel32.dll!lstrcmpW`
(real UTF-16 comparison, `kernel32_io.py`) and `kernel32.dll!GlobalLock`/
`GlobalUnlock` (pass-through no-ops, correct for the fixed/non-moveable
memory this emulator's `GlobalAlloc` always hands out).

The `msjter35.dll`/`msjet35.dll` busy-loop described above (7,005×
`GetProcAddress` repeats, zero progress) is **resolved** — two independent
bugs, both in `kernel32_handlers.py`: (1) `LoadLibraryA`'s fallback for
DLLs not found on disk unconditionally fabricated a fake-success handle
even with zero handler coverage, unlike `GetModuleHandleA`'s equivalent
path — DAO saw a fake "loaded" DLL and kept retrying instead of getting an
honest failure; (2) `GetProcAddress`'s ordinal-lookup key format
(`"ordinal#N"`) never matched how ordinals are actually registered/parsed
everywhere else in the codebase (`"Ordinal #N"`), so ordinal lookups could
never succeed regardless of whether the export existed. Full diagnosis and
fix: changelog.md, "2026-07-23 (late-night session)".

Separately, since the actual goal is a working Access Jet 3 database (not
just DAO's COM activation succeeding), the real Microsoft Jet 3.5 Database
Engine redistributable was sourced from `~/.emu32/DBInst/DAO/data1.cab`
(same InstallShield package `dao350.dll` came from — confirmed via sha256)
and deployed to `~/.emu32/WINDOWS/System32/`: `msjet35.dll`, `msjter35.dll`,
`msjint35.dll`, `vbajet32.dll`, `msrd2x35.dll`, `expsrv.dll`, and
`msvcrt40.dll` (the last one incidentally fixing a previously-unresolved
static import of DAO350.DLL's own). See Architecture section.

`advapi32.dll!RegEnumKeyA` — the older, non-Ex sibling of
`RegEnumKeyExA` (4 args, `cchName` passed by value not by pointer, no
class/last-write-time output) — was simply never implemented, only
`RegEnumKeyExA` existed. Added it in `advapi32_handlers.py`, sharing a new
`_reg_list_subkeys()` helper factored out of `RegEnumKeyExA`'s subkey-
derivation logic. Confirmed live: execution now sails through the entire
`HKLM\Software\Microsoft\Jet\3.5\Engines` enumeration and `Engines\ODBC`
config reads (all honest `NOT FOUND`s, gracefully tolerated) — no seeding
of `registry.json` was needed for this. 593/593 tests still passing.

`kernel32.dll!GetTempPathA` was next: added in `kernel32_io.py`, returns
`C:\WINDOWS\TEMP\` (backed by a real, newly-created
`~/.emu32/WINDOWS/TEMP/` host directory so later real file I/O against
that path works). Confirmed live: cleared the halt.

`kernel32.dll!GetTempFileNameA` was the halt right after that — Jet
generating its scratch filename. Added in `kernel32_io.py`: builds
`<path><3-char prefix><4 hex digits>.TMP`, and when `uUnique == 0` (the
common case) actually creates the 0-byte file on the host filesystem via
the same `os.open(..., O_CREAT|O_TRUNC)` pattern `CreateFileA`'s writable
branch uses — so a later real `CreateFileA`/`ReadFile` against that exact
name (Jet's own scratch-file use) sees a real file, not just a name.
Confirmed live: cleared the halt. 593/593 tests passing after both.

`kernel32.dll!GetFileInformationByHandle` was next — Jet querying the
new temp file's attributes/timestamps. Added in `kernel32_io.py`: looks
up the handle in `state.file_handle_map`, `os.fstat`s the real fd (or
`os.stat`s `entry.path` for read-only entries with no fd), and fills a
real `BY_HANDLE_FILE_INFORMATION` struct (attributes via `stat.S_ISDIR`,
real `ctime`/`atime`/`mtime` converted to `FILETIME`, real size, `1` for
link count, real inode as file index). Confirmed live: cleared the halt.

`kernel32.dll!lstrcpynA` was next — added in `kernel32_io.py` next to
the existing `lstrcpyA`/`lstrlenA` (bounded copy, always null-terminates
within `iMaxLength`). Confirmed live: cleared the halt.

**Real bug found and fixed, not just a missing handler**: the very next
halt, `[UNIMPLEMENTED] msjint35.dll!Ordinal #2`, looked like another
missing-handler case but wasn't — direct inspection of the real
`msjint35.dll`'s export table (via `tew`'s own `EXEFile`/`ExportTable`
parser, offline, no emulator run needed) confirmed ordinal #2
(`CchLszOfId2`) genuinely exists and `DLLLoader.load_dll` already
resolves and writes its real address into the IAT correctly. The actual
bug: `DLLLoader.patch_dll_iats` (`tew/loader/dll_loader.py`) runs
*after* `load_dll` and unconditionally re-patches every secondary-DLL
IAT entry via `patch_iat_entry` — but never passed the already-known
real address as `real_addr`, so any entry without a matching Python
handler fell straight through to the unimplemented auto-stub fallback,
silently clobbering correct real-DLL-to-real-DLL calls (e.g. `msjet35
.dll` calling into `msjint35.dll`) with a fatal halt. Fixed by having
`patch_dll_iats` look up `self._loaded_dlls[...].exports` and pass that
through as `real_addr`; also added a `real_count` outcome bucket to the
existing "Patched X/Y ... (N auto-stubs)" summary log so this class of
bug is visible going forward instead of silently inflating the
auto-stub count. Confirmed live: MSJET35.DLL's own IAT patch pass went
from 23 auto-stubs/0 real to 1 auto-stub/7 real. 593/593 tests passing
after all three fixes above.

`user32.dll!LoadStringA` was next — added in `user32_handlers.py`. Real
`RT_STRING` resource lookup was added to `pe_resources.py`
(`PEResources.find_string`, block=(id>>4)+1 / index=id&0xF packing) and
threaded per-module: `dll_loader` is now passed into
`register_user32_gdi32_handlers` (previously it wasn't) so a real loaded
DLL's own hInstance (not just the main EXE's) resolves to that DLL's own
`.rsrc`, cached per-DLL-name. `cchBufferMax == 0` (pointer-swap mode, no
copy) is explicitly **not** implemented and halts loudly instead of
silently returning a plausible-but-wrong result — confirmed live this
session that real callers never actually hit that path, so the halt is
inert in practice, not a live gap. Confirmed live: cleared the halt.

`kernel32.dll!lstrcatA` was next — added next to `lstrcpyA`/`lstrcpynA`
in `kernel32_io.py`, matching real (unbounded, like real `strcat`)
semantics. Confirmed live: cleared the halt.

`oleaut32.dll!Ordinal #202` (`CreateErrorInfo`, confirmed via the real
`oleaut32.dll`'s export table at `/data/Downloads/i386-binaries/`) was
next. Implemented as a real dual-interface COM object in
`oleaut32_handlers.py`: one allocated object with two vtables at a
+4 offset (`ICreateErrorInfo` at the object's own address, `IErrorInfo`
at +4 — a C++-style "this-adjustor" split), `QueryInterface` switching
between them, shared refcount, and real Set*/Get* method bodies that
actually read/write the object's fields (no fake success). **Live-
verified this design was necessary, not speculative over-engineering**:
DAO's real code calls `QueryInterface(IID_IErrorInfo)` on the returned
pointer immediately after creation (succeeds via the +4 face), then
fills the object via the original `ICreateErrorInfo` pointer with real
content — `SetSource("DAO.DbEngine")`, a help context ID, a help file
pointer — before the next call. Session process note: this session
skipped `CLAUDE.md`'s mandatory HANDLER DECLARATION step (state
Function/Signature/Spec/Truthful-YES-NO in chat before writing any
handler) for every handler above; a retroactive audit found one real
violation — `LoadStringA`'s `cchBufferMax==0` path was silently
returning a plausible-but-spec-incomplete result instead of halting —
now fixed as described above. No other violations found (grep audit for
TODO/FAKE/stub/silent-pass patterns across every file touched this
session came back clean).

`oleaut32.dll!Ordinal #201` (`SetErrorInfo`) is now **implemented and
confirmed live**: stores `perrinfo` as the calling thread's current COM
error object in a new per-thread `CRTState.error_info_store` dict (keyed
by `state.tls_current_thread_id()`), releasing the previous entry and
AddRef'ing the new one via the existing `_errinfo_release_core`/
`_errinfo_addref_core` helpers (this emulator has exactly one
`IErrorInfo` implementation — `CreateErrorInfo`'s, same file — so direct
field manipulation is equivalent to a real vtable call). Always returns
S_OK per spec. Confirmed live: `SetErrorInfo(perrinfo=0x06fd2624) ->
S_OK (tid=1012)` clears the halt, and execution continues into
`MCity_d.exe`'s **own code** for the first time past this point — a
6-frame-deep call chain purely in `exe` addresses. 593/593 tests passing
after the fix. Full detail: changelog.md, "2026-08-02".

The `EIP=0x00688c69` core-dump crash found right after the above is now
**resolved, and it was never a CPU/memory bug**. `coredumpctl info` on the
crashed PID showed the segfault happening in `libnvidia-rtcore.so`
(NVIDIA's proprietary driver), reached via
`Py_Exit → exit() → __run_exit_handlers` — i.e. *after* our own code had
already cleanly logged the halt diagnostic and hit `sys.exit()` in
`run_exe.py`. `libcpu.so` (the Zig CPU) does not appear anywhere in any
crash-thread stack. Root cause: `WindowManager.shutdown()`
(`tew/api/window_manager.py`) — which properly destroys SDL2
textures/renderers/windows and calls `SDL_Quit()` — existed but was never
called anywhere in `run_exe.py`; separately, the entire Vulkan side
(`tew/api/d3d8/_state.py` instance/device/swapchain/pipeline/semaphores)
has no teardown path at all (only one `vkDestroy*` call exists in the
whole codebase, for swapchain recreation, not shutdown). So `sys.exit()`
fired with SDL2 and Vulkan still fully live, and the NVIDIA driver's own
`atexit`-registered "someone forgot to clean up" safety-net handler ran
against that live context and crashed inside its own code. This is the
first time this project reached far enough into real game code to create
a live SDL/Vulkan context and then exit while it was still up — which is
why this was never seen before. SELinux was checked and ruled out (no AVC
denials logged for the crash window). Fix: added a call to
`crt_state.window_manager.shutdown()` right before `sys.exit()` in
`run_exe.py`. Confirmed live: `[WindowManager] SDL2 shut down` now logs
and the process exits cleanly — `coredumpctl` shows no new core dump.
Vulkan itself still has no explicit teardown (only SDL2 does now) — see
queued issues below.

**Logger bug fixed, and it changed the diagnosis entirely.** `tew/logger.py`'s
`_emit()` silently dropped any `ERROR`-level message whose category wasn't in
`LOG_CATEGORIES` -- except `"exception"`, which already had a special
exemption. But the project's own mandatory "halt loudly" convention
(CLAUDE.md) requires every halt to log an `ERROR` right before setting
`cpu.halted`/`cpu.faulted` -- so any run whose `LOG_CATEGORIES` didn't happen
to include the category that logged the real reason (e.g. `seh`, `handlers`,
`cpu`) got a halt diagnostic with no cause attached, ever, regardless of how
much register/stack detail was in it. Fixed by exempting `ERROR`-level
messages from category filtering the same way `"exception"` already was.
593/593 tests still pass.

Confirmed live: re-running the *same* narrow `LOG_CATEGORIES` (`com,dll,
loader,exception,window`, still no `seh`/`cpu`/`handlers`) that previously
produced the "unexplained" `EIP=0x00688c69` halt now also surfaces:
```
[ERROR] [seh] fault at 0x15035655 unhandled by SEH chain -- halting as before
[ERROR] [cpu] Fatal halt: fatal halt at EIP=0x00688c69
```
**This completely relocates the real blocker.** `0x00688c69` was never the
fault site -- it's where the CPU ended up *after* an unhandled SEH dispatch
left things in the known "halt in place with stale stack data" state (the
already-queued `seh.py` gap). The actual fault is a genuine
`STATUS_ACCESS_VIOLATION` at `EIP=0x15035655`, **inside `MSJET35.DLL`**
(loaded at `0x15000000-0x15ffffff`), dispatched via `dispatch_exception()`
in `run_exe.py`, found no handler in the game's own SEH chain, and fell
through to the "halting as before" path. The `EAX=0xCCCCCCCC` /
`0x00688c69` diagnostic previously investigated was real but downstream --
a symptom of the unhandled-fault fallback, not the cause.

**Current blocker**: diagnose the real fault at `EIP=0x15035655` inside
`MSJET35.DLL` -- needs Ghidra decompilation of the real DLL at that offset
(`0x35655` into the module) to determine what it's doing and why the access
violates. Separately noted: the Zig CPU core's fault-reporting
(`tew/hardware/cpu_zig.py` `run()`/`step()`, `_RUN_FAULTED` path) only
surfaces `EIP` and the opcode byte, never the actual faulting *memory
address* -- real Windows access violations carry that (read/write, target
address), and not having it here made this diagnosis slower than it needed
to be. Worth a Zig-side follow-up (new `cpu_get_last_fault_addr()`-style
export) later; not blocking the current investigation since EIP alone
(0x15035655) is enough to start in Ghidra.

Two small non-blocking gaps surfaced earlier, before the `RegEnumKeyA`
halt (`kernel32.dll!IsTNT`, `kernel32.dll!GetProcessAffinityMask` — both
harmlessly return NULL, Jet handles the miss and keeps going).

**Unrelated bug found and fixed the same day (2026-08-02, later session)**:
what initially looked like "Python crashing while investigating the
`0x15035655` SEH fault" was actually two separate things. The SEH fault
itself was never the crash — tew's own dispatch handled it exactly as
designed (halted cleanly, full diagnostic printed, no host-level fault,
since guest memory faults never touch real host memory). The actual crash
was a second, unrelated bug: an NVIDIA driver atexit handler
(`libGLX_nvidia.so` this time, not the earlier `libnvidia-rtcore.so`)
aborting during process exit because `SDL_Quit()` had already closed the
X11 connection it expected to use. Fixed by replacing `sys.exit()` with
`os._exit()` in `run_exe.py`, which skips the whole atexit chain. Full
diagnosis: changelog.md, "2026-08-02 (later session)".

**Separately fixed while investigating the above (2026-08-02, later session,
cont'd)**: the 9 accumulator-immediate opcodes (`op05`/`15`/`1D`/`2D`/`3D`/
`0D`/`25`/`35`/`A9` in `cpu/src/engine.zig`) had a live instance of the same
`0x66`-prefix flags-width bug the old TypeScript emulator fixed back in
2026-03-30 -- correct register read/write, but hardcoded `.w32` for the
flags width regardless of the prefix, so SF was wrong for 16-bit results
with bit 15 set. Fixed to match the already-correct `op85` pattern; 16 new
tests added (7 covering previously-untested 8-bit AL forms, which were
already correct; 9 regression tests for the fix, each verified to fail
against the pre-fix build). `libcpu.so` rebuilt, 609/609 tests passing.
Full detail: changelog.md, "2026-08-02 (later session, cont'd)".

**The `EIP=0x15035655` MSJET35.DLL fault -- the actual current blocker --
is now RESOLVED (2026-08-02, later session, cont'd again).** Root cause:
`opMovR32Imm` (`cpu/src/engine.zig`, opcodes `0xB8`-`0xBF`) never checked
`s.op_size_ovr`, so the 0x66-prefixed 16-bit form (`MOV AX/CX/etc, imm16`)
always read a bogus 4-byte immediate instead of 2 and wrote the full
32-bit register instead of just the low 16 bits -- desyncing `EIP` by 2
bytes from the real instruction stream. Found live using the emulator's
own logpoint debugger facility (`cpu.add_logpoint`), not static analysis
alone: traced the exact instruction boundaries in MSJET35.DLL's dispatch
chain and watched execution diverge at `0x1503564b` (`66 B8 01 00`, `MOV
AX, 1`). Fixed to branch on `op_size_ovr` like the rest of the file; 3 new
regression tests in `test_opcodes_mov.py` (`TestMovR16Imm16`), verified to
fail pre-fix. `libcpu.so` rebuilt, 612/612 tests passing. Confirmed live
end-to-end: MSJET35.DLL now loads and runs with zero `[seh]` fault lines,
and execution progresses further (60.045s vs. the previous 58.55s) before
halting at the separately-tracked `EIP=0x00688c69` (see "New top priority"
below) -- real forward progress. Full detail: changelog.md, "2026-08-02
(later session, cont'd again)".

**`EIP=0x00688c69` is now fully diagnosed (2026-08-03) -- and the earlier
note about it above (`seh.py`'s "halt in place with stale stack data"
path) was wrong, superseded by fresh investigation.** It's a
`cpu.fatal_halt`, not the SEH-stale-stack path at all. Root cause: the
game's own DAO/Jet database-init-failure handler (`Nfs_REALabortcallback`
-- it's what wrote `except.txt`, noticed earlier this session) checks a
global, `_Nfs_DebuggerIsPresent`, hardcoded to `1` unconditionally in
`WinMain` (not an `IsDebuggerPresent()` check -- deliberate debug-build
behavior, used at 1,780 call sites across 922 functions, the primary
assertion mechanism for the whole binary), and calls `_Nfs_DebugBreak()`
-- a real `INT3`. tew's INT3 dispatch was unconditionally fatal, skipping
any chance for the game's own SEH handling (it installs a real frame,
`_CLayer_CatchSEH`, specifically for `STATUS_BREAKPOINT`) to run. Fixed to
route INT3 through the same `dispatch_exception()` machinery already used
for access violations. 3 new tests, 615/615 passing. Confirmed live:
`tid=1012`'s real, compiled 11-frame SEH chain now genuinely gets walked
(handlers at `0x009f5eb8`/`0x00c771b0`/`0x00c93b54`/`0x00c93cc9`, all for
`code=0x80000003`) -- every one declines, so it's still genuinely
unhandled and still halts at the same point, but now as a *proven* result
instead of a skip. This also confirmed `tid=1012` does **not** share
`_CLayer_CatchSEH`'s coverage (main-thread-only). Full detail:
changelog.md, "2026-08-03 -- INT3 now routes through the real SEH chain".

**Major forward progress (2026-08-03, cont'd): the run now gets past DAO/Jet
entirely and reaches ~195.8 million steps** (previous best: a few million).
Chasing "why does `Nfs_REALabortcallback` fire" led to `msjet35.dll`'s
own `dbcode.c`-sourced debug log (enabled via a new real `-dbEnableLog`
command-line flag), which showed DAO's COM activation fully succeeding
and pinpointed the real failure as `DBEngine::get_Workspaces()`
(`dao350.dll`, real vtable call) returning null. Traced deep into
`msjet35.dll` (`FUN_7a876127`/ordinal 154 -> ... -> `FUN_7a876e2b`,
returning `-1022`) and found two real bugs in `kernel32_io.py`:
`DeleteFileA`/`DeleteFileW` never called `SetLastError` on failure, and
`GetFileSize` unconditionally failed on any writable-mode handle (a real
Windows API restriction that doesn't exist -- confirmed live,
`GetFileInformationByHandle` already proved the exact same handle valid
moments earlier in the same log). Fixed both. Also seeded two previously-
missing Jet 3.5 registry values, `SystemDB` and `TryJetAuth`. None of
these individually fixed the exact `-1022` (still traces to a third,
unidentified call site), but the combined effect took the run from halting
after a few million steps to running clean for ~195.8M steps, well past
the entire DAO/Jet sequence into real gameplay (`dsound.dll`, `winmm.dll`,
the `GetMessageA` message pump). Full detail: changelog.md, "2026-08-03
(cont'd)".

**New blocker at the new frontier**: a `RUNAWAY` (`EIP` in invalid/
unmapped memory) at step ~195.8M, immediately preceded by two
`[UNIMPLEMENTED]` `VirtualAlloc` halts (`unsupported flProtect 0x1`, then
`MEM_COMMIT on unreserved 0x4000000`) that don't actually stop execution
(~195M more steps ran afterward) -- see "New top priority" below.

## Run command
```bash
cd /data/Code/tew
timeout -k 5 300 env LOG_LEVEL=info LOG_CATEGORIES=com,dll,loader,exception /data/Code/tew/.venv/bin/python -u /data/Code/tew/run_exe.py 2>&1 | tee /tmp/emu.log | tail -60
```
Real `dao350.dll` execution takes anywhere from ~1s to ~30s per individual
`CoGetClassObject`/`CoCreateInstance` call, so a run reaching the DAO
section needs far more than a short timeout. Since 2026-07-21's fix, runs
now reach their final halt in ~57s instead of stalling to ~71s+ — a 300s
budget is generous headroom, not an observed requirement. Add
`registry`/`handlers` to `LOG_CATEGORIES` for deeper COM/IAT investigation;
add **`scheduler,thread`** with `LOG_LEVEL=debug` for
thread-lifecycle/scheduling investigation (idx assignment, every context
switch, every block reason).

The simpler run command (`timeout -k 5 90`, `LOG_LEVEL=info`, no extra
categories) is still correct for a general boot-health check that doesn't
need to reach all the way through the DAO handshake.

## Queued issues (priority order)
- Worth a broader audit for the same "op_size_ovr-aware read, hardcoded
  flags width" bug pattern beyond the 9 accumulator-immediate opcodes just
  fixed (see "Current status") -- that fix was scoped to every call site of
  `readEaxv`/`writeEaxv` specifically (grepped exhaustively), but other
  opcode families using `op_size_ovr` directly (e.g. `op21`/`op23`/`op31`/
  `op33`/`op85`, already correct, found by inspection not a systematic
  sweep) weren't exhaustively re-verified. Not blocking anything today.
- **RESOLVED (2026-08-03, cont'd)**: `EIP=0x00688c69`'s "real next step" --
  *why does `Nfs_REALabortcallback` fire at all* -- is answered. Root cause
  was real bugs in tew's own Win32 emulation (`DeleteFileA`/`DeleteFileW`
  missing `SetLastError`, `GetFileSize` wrongly failing on writable handles)
  plus two missing Jet 3.5 registry values (`SystemDB`, `TryJetAuth`). Fixed
  all four; the game no longer hits this DAO/Jet init-failure path at all
  and execution now runs clean to ~195.8M steps. Full detail: "Current
  status" above and changelog.md "2026-08-03 (cont'd)". The general
  `_Nfs_DebuggerIsPresent`/`DebugBreak()` pattern (1,780 other call sites)
  remains true as a fact about the binary but is no longer an active
  blocker -- no further action needed unless a *different* one of those
  1,780 sites is actually hit by a future run.
- **RESOLVED (2026-08-04)**: `RUNAWAY` at ~195.8M steps. Root cause was a
  real `VirtualAlloc` gap (`PAGE_NOACCESS` wrongly rejected) plus its
  corruption fallout, not a genuinely-missing halt-propagation fix -- see
  "Current status" above and changelog.md "2026-08-04". **New current
  blocker**: `cpu.fatal_halt at EIP=0x001fe012` (see "Current status").
- **Deliberately deferred (per Molly, 2026-08-04)**: the general
  "`cpu.halted = True` doesn't actually stop the CPU" bug class (the
  ~85-of-~90-sites gap further down this list) is a known, real issue --
  it's what let the `VirtualAlloc` halts above silently corrupt the stack
  and produce the `RUNAWAY` instead of stopping cleanly. Not fixed this
  session by explicit request; still worth its own dedicated pass.
- Historical note, kept for context, now incorrect -- do not act on this:
  `0x00688c69` was previously guessed to be the same open item as the
  "Decide/implement a real unwind for seh.py's unhandled-fault path" bullet
  further down this list. Fresh investigation (see "Current status")
  disproved that: it's a `cpu.fatal_halt` from INT3 (now properly routed
  through `dispatch_exception`, see above), not the SEH-stale-stack-data
  path at all. The `EBP` chain frames captured at this halt (`0x0068adf2`,
  `0x00a301a1`, `0x00684de7`, `0x006848ee`, `0x004d8c71`, `0x0068a7d5`,
  `0x009fcaa6`, all in `MCity_d.exe`) are still accurate as the real call
  chain leading into `Nfs_REALabortcallback` -- useful starting context if
  chasing the "why does DAO/Jet init fail" question above.
- Add real faulting-address reporting to the Zig CPU core's fault path
  (`cpu_run`'s `_RUN_FAULTED` result currently only carries EIP + opcode,
  not the memory address that was actually being accessed) — would have
  made the (now-resolved) `0x15035655` diagnosis faster, and would help
  the `0x00688c69` one too. Not blocking, EIP is enough to start.
- Vulkan resource teardown is still entirely missing (`tew/api/d3d8/_state.py`
  tracks instance/device/swapchain/pipeline/semaphores/etc. with zero
  `vkDestroyInstance`/`vkDestroyDevice` calls anywhere in the codebase —
  only `vkDestroySwapchainKHR`, used for recreation, not shutdown). Lower
  urgency as of 2026-08-02 (later session): `run_exe.py` now calls
  `os._exit()` instead of `sys.exit()` after `window_manager.shutdown()`,
  which skips `exit()`/`__run_exit_handlers` entirely — so no NVIDIA driver
  atexit handler runs at all regardless of what graphics state (Vulkan, GLX,
  or otherwise) is still live. This was in direct response to a *second*
  NVIDIA-atexit crash (`libGLX_nvidia.so`/`xcb`, distinct from the earlier
  `libnvidia-rtcore.so` one) that the driver's "undocumented atexit fallback"
  mentioned below turned out not to reliably cover. See changelog.md,
  "2026-08-02 (later session)". A proper `vk_shutdown()` (destroy pipeline →
  framebuffers → image views → render pass → command pool →
  semaphores/fence → swapchain → device → surface → instance, in that
  order) is still worth doing for hygiene/correctness, but is no longer
  covering for a live crash.
- Worth a dedicated pass later: now that `patch_dll_iats`'s real-address
  bug is fixed, re-check whether any of the *other* previously-"fixed"
  halts in this session were actually this same class of bug
  (real-DLL-to-real-DLL call wrongly auto-stubbed) rather than a truly
  missing Win32 API — unlikely for the kernel32/advapi32 fixes already
  made (those were genuinely-unimplemented Python-handler gaps, confirmed
  by checking the handler registry directly each time), but worth keeping
  in mind for future `[UNIMPLEMENTED] <dll>.dll!Ordinal #N` or
  `<dll>.dll!<name>` halts where `<dll>` is one of the real Jet-family
  DLLs (`msjet35.dll`, `msjint35.dll`, `vbajet32.dll`, `msrd2x35.dll`,
  `expsrv.dll`) rather than a standard Win32 system DLL.
- Low priority, not currently blocking: `kernel32.dll!IsTNT` and
  `kernel32.dll!GetProcessAffinityMask` are unimplemented (`GetProcAddress`
  returns NULL for both) — `MSJET35.DLL` tolerates the miss and continues,
  but a real caller elsewhere might not.
- Revisit `SafeArrayLock`/`SafeArrayUnaccessData` (`oleaut32.dll` ordinals
  21/24, `oleaut32_handlers.py`) at some point — both are hardcoded no-ops
  returning `S_OK` with no real lock-count tracking, harmless only because
  nothing in this emulator currently moves or frees a `SAFEARRAY`'s
  `pvData` out from under a caller. If that assumption ever changes (real
  `SafeArrayRedim`/compaction, or any future GC-like behavior), these two
  need actual `cLocks` bookkeeping (the `SAFEARRAY` header already has a
  `cLocks` field at `psa+8`, currently always `0` — see `_SafeArrayCreate`).
  Not blocking anything today.
- Correct `cpu/src/two_byte.zig`'s `CPUID` signature to real Pentium II
  (`0x00000630`/`0x00000650`) and fix this file's "source of truth" reference
  — blocked on locating the exact Pentium II spec manual to confirm
  Model/Stepping before committing to a value.
- Not in scope when the fatal-halt sentinel was replaced with an exception
  (2026-07-23 evening), noted as a related but separate gap: a genuine
  fault occurring deep inside a *nested* `_invoke_emulated_proc` call
  currently never gets an SEH-recovery attempt at all (only the top-level
  loop calls `dispatch_exception`) — it's silently swallowed into the
  bare-`0` fallback that still exists for non-fatal incompletions
  (max_steps exhausted, thread died, unexpected non-fatal halt). Worth its
  own decision later.
- Identify the `EIP=0x00200c00` final halt's real cause — confirmed
  unrelated to DAO/`DllMain` timing, still unidentified which API it is.
- Decide whether `mmtimer_callback`'s own nested-call halt (lands back at
  its own entry instead of its sentinel) is a real re-entrancy bug or
  another instance of the same "thread died mid-call" class already fixed
  for `tid=1012`.
- Decide/implement a real unwind for `seh.py`'s unhandled-fault path
  instead of "halt in place with stale stack data"
- Fix `_chkesp`'s diagnostic (`patch_internals.py`) hardcoding EBP as the
  snapshot register when it's a compiler register-allocation choice
  (confirmed ESI at one real call site)
- **RESOLVED (2026-08-06, cont'd again)**: the `cpu.halted = True` sites
  missing `cpu.fatal_halt` — see "Current status" above.
- SDL window resolution (1536x1248) vs. `GetDeviceCaps` (1024x768) mismatch
- DrawPrimitive / DrawIndexedPrimitive coverage beyond what's needed to
  reach the DAO abort — not yet assessed how much is implemented
- `[alive]` heartbeat silent during `GetMessageA` host-sleep — low priority
- Low priority, structural only, no runtime risk: `tew/loader/dll_loader.py`
  → `tew/pe/exe_file.py` → `tew/loader/import_resolver.py` → back to
  `dll_loader.py` form a genuine three-file import cycle (found via
  `gitnexus check --cycles` 2026-07-23, confirmed by reading the actual
  imports — not a false positive like the two other cycles gitnexus also
  flagged that turn out to be `TYPE_CHECKING`-only). Currently held together
  by two deliberate deferred (function-body, not module-level) imports:
  `DLLLoader.load_dll()` imports `EXEFile` lazily, and `EXEFile.__init__`
  imports `ImportResolver` lazily; `import_resolver.py`'s import of
  `DLLLoader` is the only real top-level one. Works today, no crash risk,
  but reflects a genuine mutual dependency between all three files — a
  cleaner layering (e.g. `EXEFile` not needing to know about
  `ImportResolver` at all) would let all three imports be plain top-level
  ones instead of relying on load-order timing. Worth a look if this area
  is touched again for other reasons; not worth a dedicated pass on its own.

## Architecture
- **CPU + memory backend**: fully Zig now, no pure-Python fallback path
  remains. `tew/hardware/cpu.py` (the original pure-Python CPU class) and
  the entire `tew/emulator/opcodes/` package (pure-Python x86 instruction
  decode) were deleted 2026-07-24 — confirmed dead (`ZigCPU.register()`
  was a no-op; opcodes were built and registered every run but never
  executed). `tew/hardware/memory.py` is likewise now a re-export shim
  over `ZigMemory` (`tew/hardware/memory_zig.py`), and the guest heap's
  bump-allocator cursor math (`CRTState.simple_alloc`) now delegates to
  `tew/hardware/alloc_zig.py`. Register/flag constants (`EAX`, `CF_BIT`,
  etc.) now come from `tew.hardware.cpu_zig`, not the deleted `cpu.py`.
  See changelog.md, "2026-07-24."
- **Zig/Python FFI boundary — kernel module**: as of 2026-07-24 (cont'd),
  the whole Zig side of `libcpu.so` is organized as a real kernel-style
  split. `cpu/src/kernel.zig` is the build root and the *only* file with
  `export fn`s anywhere in the project (63 total: CPU control, memory
  access, guest-heap allocator) — the Python-facing C ABI, full stop.
  `cpu/src/engine.zig` holds the internal execution engine (dispatch
  table, `cpuStep`, all opcode handlers), never exported, driven only by
  `kernel.zig`'s `cpu_run`. `cpu/src/primitives.zig` holds the one shared
  bounds-check/byte-access implementation both `core.zig`'s CpuState-bound
  memory helpers and `kernel.zig`'s `mem_*` C ABI delegate to (previously
  two independent reimplementations of the same logic). On the Python
  side, `tew/hardware/_kernel_lib.py` is now the single `ctypes.CDLL`
  loader shared by `cpu_zig.py`/`memory_zig.py`/`alloc_zig.py` (previously
  three independent `dlopen` calls to the same `.so`). `cpu/src/memory.zig`
  and `cpu/src/alloc.zig` no longer exist — absorbed into `kernel.zig`.
  See changelog.md, "2026-07-24 (cont'd)."
- Game does NOT call D3D8 directly.
- Rendering path: Game → THRASH API (dx8z.dll) → D3D8 (fake COM, Vulkan backend)
- WinINet connects to localhost:443 (HTTPS)
- authlogin.dll reads AuthLoginServer from registry (localhost)
- Login dialog (SDL2): admin/admin from registry, auto-filled
- Timer thread: FUN_00a30ea0, runs as tid=1006 via CRT wrapper at 0x9fc3a0
  `mmtimer_callback` (0x00a30a40) is the multimedia timer proc AND a `_tmrsub[]` subscriber.
  It calls `_SIGNAL_set(event)` + re-registers via `timeSetEvent` each tick.
  Event handle at runtime is 0x7012 (may vary).
- `0x9fc3a0` is a **generic CRT thread-spawn wrapper**, not specific to the
  timer thread — the real work function is passed as `_THREAD_create`'s
  parameter. Several threads use it (`tid=1006`-`1011`), and DAO's own
  `DllMain`-calling worker (`tid=1012`, spawned ~57s in, short-lived) is
  just another instance of the same pattern, not a DAO-specific mechanism.
- **COM activation**: registry-driven (`hkcr\clsid\{...}\inprocserver32`),
  real DLLs loaded and executed for CLSIDs in `_KNOWN_COM_SERVERS`
  (`oleaut32_handlers.py`) — currently just DAO 3.5 (`dao350.dll`, real
  file at `~/.emu32/WINDOWS/System32/`, kept out of the repo since it's a
  Microsoft-copyrighted redistributable). Unregistered or unimplemented
  CLSIDs fail honestly with `REGDB_E_CLASSNOTREG`, matching a real
  unmodified install missing that component. This pattern (search a
  directory of real DLLs, fall back to Python stub) is worth reusing for
  *other* pure user-mode COM/utility libraries the game touches — NOT for
  anything DirectX/hardware-driver-dependent (`d3d8.dll`, `ddraw.dll`,
  `dsound.dll` etc. all need a real kernel-mode HAL/driver stack this
  emulator doesn't have; tew's existing hand-built D3D8-over-Vulkan is
  already the correct solution to that problem, not something to replace).
- **Jet 3.5 database engine**: real files, same pattern as `dao350.dll`,
  also at `~/.emu32/WINDOWS/System32/` (kept out of the repo, Microsoft-
  copyrighted): `msjet35.dll` (core engine), `msjter35.dll` (error-message
  resource), `msjint35.dll` (international/collation), `vbajet32.dll`,
  `msrd2x35.dll` (Jet Red ISAM driver), `expsrv.dll` (expression service),
  `msvcrt40.dll` (DAO350.DLL's own CRT dependency). All sourced from
  `~/.emu32/DBInst/DAO/data1.cab` (InstallShield cabinet, extract with
  `unshield -d <dir> x data1.cab`) — the same install package `dao350.dll`
  itself came from, confirmed via sha256 match. Unlike `dao350.dll`, these
  are *not* gated through `_KNOWN_COM_SERVERS` (they're not COM-activated —
  DAO loads them directly via `LoadLibraryA`/`GetProcAddress` by name); they
  work because `~/.emu32/WINDOWS/System32/` was already a generic
  `DLLLoader` search path, not one scoped to COM servers only. This is the
  first case where a real DLL genuinely needs to *execute meaningfully*
  (actual Jet database reads/writes for an Access `.mdb` file), not just
  activate and hand back to caller code — expect deeper Win32/advapi32
  registry surface area to be needed than DAO alone required.

## Test suite
593 tests (all passing, reconfirmed 2026-07-24 after the memory.py Zig
port, cpu.py/opcodes retirement, the bump-allocator port, and the
kernel.zig/engine.zig/primitives.zig FFI-boundary refactor, on `main`).
