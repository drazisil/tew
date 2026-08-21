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

## Current status (2026-08-21) — 3 straightforward halts cleared past DAO-3075 (VariantChangeType VT_INT, VirtualQuery, GetModuleFileNameW); new 4th halt looks like a real, harder bug -- indirect jump to invalid near-null address deep in an expsrv.dll call chain

**Context: DAO-3075 is resolved** (see `changelog.md`/`status_archive.md` for full detail — real cause was a `0x66`-prefix flag bug in `opIncR32`/`opDecR32`, cpu/src/engine.zig, fixed and live-verified). This entry covers what happened immediately after, re-running the same scenario further.

**Three halts cleared in sequence, each the same shape**: a genuinely missing/incomplete Win32 handler logging `[UNIMPLEMENTED] ... — halting` (a deliberate, self-documenting stop, not a mystery crash), fixed with tests-first (red confirmed, then green), full suite green, then a live re-run confirming forward progress before moving to the next one:

1. **`VariantChangeType` unhandled source `vt=22` (`VT_INT`)** — MSDN documents `VT_INT` as storage-identical to `VT_I4` (same 4-byte signed int at `+8`). `tew/api/oleaut32_handlers.py`: added `_VT_INT = 22`, treated as `_VT_I4`'s equivalent on both the source-read and target-write side. 5 new tests in `tests/unit/api/test_oleaut32_variant_change_type.py` (`TestVtIntIsI4Equivalent`).
2. **`VirtualQuery` had no handler at all** (unlike every other `Virtual*`/`Heap*` API in `kernel32_memory.py`) — MSJET35.DLL's own memory manager called it on a page it got from `VirtualAlloc`. Implemented against `state.virtual_reserved`/`virtual_committed` (already tracked); added `state.virtual_protect: dict[int,int]` (new, wired into `VirtualAlloc`) since `MEMORY_BASIC_INFORMATION.Protect`/`AllocationProtect` need the real protection flags, which weren't tracked before. Halts loudly (not guessed) on an address outside any tracked region or an undersized output buffer — real free-region-size reporting was never observed live, so not implemented rather than guessed. 7 new tests in `test_kernel32_memory.py` (`TestVirtualQuery`).
3. **`GetModuleFileNameW` was a deliberate `_halt()` placeholder** (`GetModuleFileNameA` next to it was fully implemented) — expsrv.dll (VBA runtime) called it. Mirrors the `A` version exactly except `nSize` is a WCHAR count (not bytes) and output is null-terminated UTF-16LE. New `tests/unit/api/test_kernel32_get_module_file_name.py`, 9 tests.

Each live re-run: `pytest -q` green (1124 → 1131 → 1140 as tests were added), then `LOG_LEVEL=error LOG_CATEGORIES=cpu,handlers timeout 90 .venv/bin/python run_exe.py`, confirming the specific halt was gone and a new one (or none) appeared further along.

**4th halt, current blocker — looks like a real bug, not a missing handler.** After fix #3, the run no longer hits any `[UNIMPLEMENTED]`/halting handler at all — instead `run_exe.py`'s own runaway-detector fires: `RUNAWAY DETECTED at step 175900000`, `Current EIP: 0x0003049c (INVALID)` (a tiny address, `Bytes at EIP: 00 00 00 00...` — entirely unwritten memory), `Last valid step: 175800000, EIP: 0x15027571` (inside MSJET35.DLL's runtime range). The stack at the crash shows real expsrv.dll return addresses (`[ESP+00] 0x17009d1b`, EBP-chain frames `ret=0x17009d08`/`ret=0x1701cbd7`, both `expsrv.dll+...`) — this is MSJET35.DLL calling into expsrv.dll (Jet's real mechanism for evaluating calculated/expression fields in a query) and something in that chain does an indirect call/jump through a bad (near-null, all-zero-bytes-at-destination) pointer.

**Checked against memory, ruled out as a match**: `tew_fake_kernel_gaps.md` section 18 (2026-07-19 night, 32 days stale) documents a *different*, previously-open bug — `dao350.dll`'s `DllGetClassObject` returning `S_OK` without writing `*ppv`, causing a wild jump to `EIP=0xfefc8d8f` from the game's own `CoGetClassObject` fallback code. Different address, different call path (DAO's COM activation, not MSJET35→expsrv expression evaluation), never fixed per that memory. Worth keeping in mind as a *related family* of bug (uninitialized/NULL out-pointer or vtable slot causing a wild jump) but not assumed to be the same root cause without live evidence.

**Not yet investigated**: what exactly deep in the expsrv.dll call chain computes/returns the bad pointer that gets jumped through. Next step would be the same single-step-trace technique that resolved DAO-3075 (ground-truth Ghidra listing + live `cpu.step()` trace from a known-good anchor, e.g. `0x17009d1b`'s call site or `0x1701cbd7`), rather than hand-disassembly.

**Improvement made (2026-08-21, same day): the runaway detector now routes through the real SEH pipeline instead of its own ad-hoc dump.** Molly's suggestion: since real Windows would raise `STATUS_ACCESS_VIOLATION` the instant code fetches from a page it never mapped executable, and `run_exe.py` already has exactly that pipeline wired up for genuine `cpu.faulted` events (`dispatch_exception(cpu, mem, STATUS_ACCESS_VIOLATION, eip)`, real `fs:[0]` SEH-chain walk), the runaway-detected branch (`is_valid_eip` returns False) now calls the same function instead of printing its own shallower diagnostic. If a handler resolves it, the main loop just resumes normally; if not, `cpu.halted = True` and the post-run block's existing `diagnose_halt()` (32-frame EBP-chain walker, strictly more capable than the old inline 32-slot flat dump) fires automatically. **Live-verified**: for the current expsrv.dll halt, the SEH chain is walked and genuinely has nothing to offer (`runaway at 0x0003049c unhandled by SEH chain -- halting`) — a real negative result, not a bug in the new dispatch path, and confirms via a second, independent code path that the EBP-chain only extends one real frame (`expsrv.dll+0x1cbd7`) before dead-ending at a non-frame `ret=0x00000001`, same as the flat stack scan already showed. No new lead surfaced yet, but the tooling itself is a durable improvement — every future runaway now gets a real shot at the game's own recovery path first. `pytest -q`: 1140/1140 unaffected (run_exe.py has no direct unit tests; this is a live-run-verified change).

**Diagnostic instrumentation in `run_exe.py`**: unchanged from the DAO-3075 investigation (`_lookahead_call_probe`'s single-step tracer and the other 7 breakpoint slots) — none of it is relevant to this new expsrv.dll halt, all 8 slots are free to repurpose next session.


