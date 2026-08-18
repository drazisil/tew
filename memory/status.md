# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Holds only the single most-recent `## Current status` entry — do not let `## Previous status` entries accumulate here again; rotate them into `status_archive.md` instead (see below) once a new "Current status" replaces them. Completed work goes in changelog.md — do not add "what's fixed" sections here.*

*Full investigation history lives in two places, both newest-first — do not re-derive any of it from scratch, grep instead: `changelog.md` (durable, organized by fix) and `status_archive.md` (rotated-out `## Previous status` entries, 2026-08-02 through 2026-08-16, some session-in-progress detail not duplicated in changelog.md).*
---

## Known false leads (permanent — do not remove on rotation)

- **`dbcode.c(3376) "The class has not been licensed"`**: prints every run, every time DAO/Jet does COM work, well before any actual failure. Molly confirmed (2026-08-16) this is expected/ignorable — NOT the cause of `CreateQueryDef`/DAO-3075 failures. Got mistakenly re-flagged as a "new lead" once already the same night (see `status_archive.md`, "Previous status (2026-08-16, cont'd x4)", for the correction) — check here before treating it as new again.

## Current status (2026-08-17, cont'd x4)

**Scheduler-to-Zig port is DONE (all 7 stages, 0-6).** `tew/kernel/scheduler.py` (the original pure-Python scheduler) and its test suite are deleted; `tew/hardware/scheduler_zig.py` (`ZigScheduler`, backed by `cpu/src/scheduler.zig`) is the only scheduler now. Full design record: `~/.claude/plans/vast-drifting-pike.md` (artifact: https://claude.ai/code/artifact/b3751eed-4723-4010-8724-011c27f456e1). Full per-stage history: `changelog.md`, "2026-08-17 (cont'd)" through "(cont'd x7)"; a fuller wrap-up summary is archived at `status_archive.md`, "Previous status (2026-08-17, cont'd x3)".

**Outcome, confirmed and measured**: the motivating problem (160,433 reentrancy-guard refusals / 3.7s starvation during a heavy nested `expsrv.dll` DllMain call, caused by ~44 FFI hops per context switch under the old scheduler) is fixed -- a final live run spawning 14 real threads (including 3 created from inside that same nested DllMain call) shows **0 reentrancy violations**. `zig build test`: 154/154. `pytest -q`: 1112/1112. One real bug found and fixed along the way (not anticipated by the plan): `ZigCPU._py_halted`, a Python-side cache that went stale once the scheduler's halt-clearing moved into Zig -- see the archived summary for detail.

**Current blocker**: none -- the port is complete, no follow-up work pending on it. The DAO-3075/aggregate-function thread is still paused (not abandoned), unaffected by this port -- see `status_archive.md`, "Previous status (2026-08-17)", for where it left off if picked back up next.

