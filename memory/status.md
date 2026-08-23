# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Holds only the single most-recent `## Current status` entry — do not let `## Previous status` entries accumulate here again; rotate them into `status_archive.md` instead (see below) once a new "Current status" replaces them. Completed work goes in changelog.md — do not add "what's fixed" sections here.*

*Full investigation history lives in two places, both newest-first — do not re-derive any of it from scratch, grep instead: `changelog.md` (durable, organized by fix) and `status_archive.md` (rotated-out `## Previous status` entries, 2026-08-02 through 2026-08-22, some session-in-progress detail not duplicated in changelog.md).*
---

## Known false leads (permanent — do not remove on rotation)

- **`dbcode.c(3376) "The class has not been licensed"`**: prints every run, every time DAO/Jet does COM work, well before any actual failure. Molly confirmed (2026-08-16) this is expected/ignorable — NOT the cause of `CreateQueryDef`/DAO-3075 failures. Got mistakenly re-flagged as a "new lead" once already the same night (see `status_archive.md`, "Previous status (2026-08-16, cont'd x4)", for the correction) — check here before treating it as new again.

## Current status (2026-08-23) — RtlUnwind EBP-restoration fix shipped (correct, tested, kept) but does NOT resolve the anti-debug-self-test crash; real root cause is a garbage return address on the thread's own outermost stack frame, not yet investigated

**Context**: prior session chain (all fixed/verified, full detail in `status_archive.md`'s "Previous status (2026-08-22, cont'd...)" entries and `changelog.md`'s matching dated entries): (1) `msjet35.dll` collation-cache crash (`CompareStringA`/`CompareStringW` locale validation), (2) opt-in null-page memory guard so the game's anti-debug self-test can genuinely fault, (3) `dispatch_exception` no longer conflates a handler crashing with a clean `RtlUnwind` escape, (4) traced the self-test's own crash to `RtlUnwind` never restoring EBP after redirecting execution -- planned and implemented this session, see `status_archive.md`'s newest "Previous status (2026-08-23)" entry for the full story.

**This session's outcome, in one line**: the EBP-restoration fix is real, correct, tested (2 new tests, 1236/1236 passing), and live-confirmed to work exactly as designed -- but it does not fix the crash, because EBP was never actually the cause. Empirically ruled out: the same `EstablisherFrame=0x7ffffff0` garbage value recurs identically whether EBP is the old stale value or the newly-correctly-restored one.

**Real root cause, found via one more probe**: the second `__except_handler3` invocation's own *return address* is `0x011f3b90` -- an address already established this session to be inside a data/string-table region, not real code. That's also the exact same value that appeared as the outermost stack frame's "return address" in every crash dump all night (previously misread as just "where the EBP-chain diagnostic walk gives up," not as an actual return path the CPU executes). The real story: by the time this happens, `_CLayer_DetectDebugger`'s own function (and whatever calls it) has already returned normally all the way up the call stack to the thread's own outermost function -- which then tries to `RET` into *its own* stored return address, and that value is garbage instead of valid thread-exit/kernel32 code. Execution wanders from there into whatever that garbage decodes as, eventually hitting a `CALL` into `0x009f5eb8` with nonsense arguments.

**Not yet investigated**: how tew sets up a thread's initial stack frame -- specifically, what "return address" gets placed there for when the thread's own entry-point function eventually returns. Likely candidates to check first: `cpu/src/scheduler.zig`'s `initThreadStack`, and wherever the *main* thread's own initial frame gets set up (this crash is on `tid=1000`, the main thread, not a worker thread created via `initThreadStack` -- so the real answer may be in `run_exe.py`'s or `kernel_structures.py`'s main-thread setup instead). This needs fresh investigation, not more SEH-dispatch work -- the SEH dispatcher itself is now behaving correctly and honestly throughout.

**Housekeeping, still live from earlier this session**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- `run_exe.py`'s breakpoint slots: all 8 are effectively free for the next investigation.
- Note for future memory-history investigations: `dispatch_exception`/`_invoke_handler`'s own Python-level `memory.read32()`/`write32()` calls go through a different code path than the guest CPU's own instruction execution, so ClickHouse's write-hook capture (hooked into the guest-instruction path only) won't see Python-side SEH-dispatch writes -- use a live breakpoint probe for those, not write-history reconstruction.
