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

## Current status (2026-08-25) — `mcity.c(588)` / `Fields.Count==1` bug FIXED (see changelog.md and status_archive.md's matching "Previous status" entry for the full mechanism). New blocker further downstream: a Zig-level CPU-core integer-overflow panic, not yet chased.

**What changed**: fixed `_locale_is_valid` in `tew/api/kernel32_io.py` to resolve `LOCALE_USER_DEFAULT` (`0x0400`)/`LOCALE_SYSTEM_DEFAULT` (`0x0800`) to `0x0409` instead of rejecting them -- root cause was tew's `CompareStringA` rejecting this completely standard Windows locale sentinel, which dao350.dll's field-name dedup check depends on for every column comparison while building a recordset's Fields collection; the rejection made every comparison silently report "equal", so real columns after the first were misidentified as duplicates and dropped. Confirmed live: the `prefClass` assert this session's whole investigation chased no longer fires (137 probe hits, all `real_answer:ok`), 3 diagnostic probes cleaned up (`_column_loop_probe`/`_column_loop_return_probe`/`_dedup_lookup_probe`/`_pre_add_struct_probe` -- their question is answered, removed rather than left disabled per usual practice).

**New blocker, not yet investigated**: run now progresses ~16s further than ever before (63s -> 79s, into COM/OLE automation, repeated `LoadTypeLibEx` calls) before dying with a real Zig panic:
```
thread panic: integer overflow
/data/Code/tew/cpu/src/core.zig:163:69: in readRmFixed32
    return @as(u32, memRead8(s, addr)) | (@as(u32, memRead8(s, addr + 1)) << 8) | ...
/data/Code/tew/cpu/src/engine.zig:878: in op8B (MOV r32,r/m32)
```
`addr + 1` overflowing `u32` (i.e. `addr == 0xFFFFFFFF`) is the immediate suspect, but not yet confirmed -- could be a wraparound-address r/m operand computed somewhere upstream, or a genuinely out-of-range `mod`/`rm` decode. Repro: `cd /data/Code/tew && TEW_FIXED_HEARTBEAT_MS=100 TEW_MAX_STEPS=5000000000 .venv/bin/python run_exe.py`. Not yet started: find the actual EIP/instruction that computed the overflowing `addr`, and whether it's a real x86 edge case (e.g. `[EDI]` with `EDI=0xFFFFFFFF` from a decrement-past-zero loop) or a CPU-core decode bug producing a bogus address.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- `run_exe.py`'s breakpoint slots: cleaned up again 2026-08-25 after the `Fields.Count`/`CompareStringA` fix landed -- the whole `_column_loop_probe`/`_column_loop_return_probe`/`_dedup_lookup_probe`/`_pre_add_struct_probe` diagnostic chain (and the `_hexdump` helper it used) removed, question answered. 3 of 8 slots in use now (`_fields_probe`, `_fields_count_probe`, `_prefclass_assert_probe`, all cheap permanent landmarks). 5 slots free.
