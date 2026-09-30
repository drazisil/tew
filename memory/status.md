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


## Current status (2026-09-29) — a plain `run_exe.py` plays itself from launch into the lobby and stays there; perf pass tasks 1-4 done (1.44x to 600M guest steps)

**Where the game is**: unattended, a run clicks Continue on the login
dialog, answers No to full screen, clicks START on persona-select 30s after
`MCity_Log.txt` reports `Done Getting Personas` (lands ~92s), then CONTINUE
on the Mayor's welcome letter 30s after `stdout.txt` reports `New mail IDs
detected!` (lands ~136s), leaving the lobby home screen up; it keeps sending
lobby heartbeats and 240s runs end cleanly there (~4.2B steps).
`TEW_NO_AUTO=1` turns all of that off; `TEW_MAX_STEPS` is unlimited unless
set. (CONTINUE click: branch `feat/lobby-population`, not merged yet.)

**Lobby home placeholders** (TODO.md has the detail; on that branch):
racing rows showing `kTxtChannel*Racing` keys are an exe/data mismatch --
real XP shows the same; "No Active Car"/empty MY CAR is correct for the
server data (persona 21 owns no car; mco-rust grants one only via the
new-persona starter screen); the left-frame `PlayerName` header is open --
its `LFrame_ProfileName` hook never fires because UPDATEUI only reaches the
view being Begun, not the side frame; unknown yet whether tew changes the
frame-vs-persona ordering.

**Fixed 2026-09-29** (changelog has the detail):
- Intermittent crash loading the lobby (1 run in 6): `DispatchMessageA`'s
  nested WndProc call gave up after 5M steps and rewound the CPU across
  thread switches. The WndProc now runs as guest code via a stack
  trampoline (PR #31). Other nested-call sites still have the pattern --
  TODO.md.
- Static initializers returned to `THREAD_SENTINEL`, marking the main
  thread DEAD during OLEAUT32's DllMain at every startup (PR #30).
- DLLs mapped over the heap (DAO350 at 0x04470000), the root of the
  INVALID BLOCK abort on front-end exit (PR #28, merged; the exit path
  itself not re-tested).
- Critical sections now work the way XP's ntdll does (guest struct is the
  state, Enter/Leave run as guest code, contention hands off through
  LockSemaphore), plus TEB+0x24 tracks the running thread (PR #29 +
  tew-cpu 0.3.0).

**Expected, not a bug**: the SEH fault at `EIP=0x004d980f` ~2s in is the
game's `_CLayer_DetectDebugger` self-test (null-page guard + its own SEH).

**Next candidates**: `PlayerName` (log MLFrame creation/Begin vs persona
select); perf (D3D8
`_convert_to_bgra8` ~30% once textures load, `simple_alloc`); the remaining
nested `_invoke_emulated_proc` callers.

## Known false leads (permanent — do not remove on rotation)

- **`dbcode.c(3376) "The class has not been licensed"`**: prints every run, every time DAO/Jet does COM work, well before any actual failure. Molly confirmed (2026-08-16) this is expected/ignorable — NOT the cause of `CreateQueryDef`/DAO-3075 failures. Got mistakenly re-flagged as a "new lead" once already the same night (see `status_archive.md`, "Previous status (2026-08-16, cont'd x4)", for the correction) — check here before treating it as new again.
- **"DB-thread scheduler starvation" is NOT a tew scheduler bug** — re-flagged this exact way on 2026-09-05 before being corrected the same session; already root-caused once before, on 2026-09-04 (see `status_archive.md`'s "Previous status (2026-09-04, evening)" entry), as genuine, correctly-emulated slow guest work (the real Jet/DAO database engine + the game's own polling loop), not a tew bug. Re-confirmed 2026-09-05: during the stall, `tid=1000` (the render thread) is NOT blocked/starved — `LOG_CATEGORIES=scheduler` shows it actively executing a real `SetEvent` polling loop right up to the end of the run. `cpu/src/scheduler.zig`'s `preemptSlice` round-robins to any other `.ready` thread every batch and only keeps running the current one when nothing else is ready — it is not unfairly favoring the DB thread. The real constraint is that this specific guest workload is genuinely slow under x86 emulation; no scheduler change fixes that. Check here before re-investigating this as a scheduler fairness bug again.
- **A "blocky mosaic" on the game window is NOT rendering corruption** — re-misdiagnosed this exact way on 2026-09-16 (this session) via screenshot before checking here; already root-caused as a false lead once before, 2026-09-05 (see `status_archive.md`'s "First non-black frame ever produced" entry): "it was real, correctly-loading icon content caught mid-load, not corruption." A frozen-looking mosaic across multiple screenshots seconds apart is consistent with the documented DB-thread slow window (see the scheduler-starvation entry above) -- the render thread simply hasn't been handed a finished frame yet, not a broken one. Check here before re-flagging a mosaic/garbled screen as a new bug; let the run reach virtual t≈327-330s (see current status entry for the exact `TEW_MAX_STEPS`/`timeout` needed) before judging what's on screen.
- **`MSJET35.DLL` ordinal #325 (the `ValidationRule`/`Required`/`AllowZeroLength` field-property-access gate) does NOT fire during login/persona-select** (2026-09-15) — confirmed via a live call-counter logpoint, zero calls across a full run to persona-select. The plausible-looking "per-record validation query" narrative built from its Ghidra decompile (it constructs a literal `PARAMETERS vt tableid; SELECT * FROM vt WHERE Not(...)` query) describes a real code path, just one that's dead for this flow — its only caller in `MSJET35.DLL` is reached exclusively through this same ordinal #325 gate. The real explanation for the observed `MSJET35.DLL!Ordinal #158` activity during login is `dbparts.c::DBParts_GetBrandedPartDefInfo` (154 calls, one per branded car part) via `dblog.txt`'s real trace, and the schema-action dispatcher `FUN_86e5b2` (real `CREATE TABLE`/schema-application work), not record validation. Check here before re-chasing ordinal #325 or the validation-rule theory as the DB-cost explanation again.

