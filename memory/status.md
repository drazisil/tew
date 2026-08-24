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

## Current status (2026-08-25) — New downstream blocker after the SEH fixes (see status_archive.md for those, DONE/committed): `ASSERT: mcity.c(588) prefClass>=0 && prefClass<DBCP_MaxRatings`. Root cause traced down to "a real DAO Recordset.Fields.Count reads as 1 instead of 10" -- confirmed via real game/DAO code, not yet confirmed WHY

**Context**: with the anti-debug-self-test crash and SEH-dispatch-nesting blocker both fixed (see `status_archive.md`'s "Previous status (2026-08-24, cont'd)" and matching `changelog.md` entries), the game now reaches its real main window and runs 40+ seconds before halting here -- furthest ever. This is a new, unrelated, genuine bug, not a recurrence of anything.

**Full mechanism traced, real game/DAO code (not msjet35.dll internals) confirmed via Ghidra decompiles + `mdbtools` + one focused breakpoint run** -- full detail (addresses, ruled-out theories) in `status_archive.md`'s matching entry, short version here:
- `carClassList::carClassList()` validates a `prefClass` field from `DB_GetGameConfigCarTableOffline`'s query, a real stored QueryDef `StockVehicleAttributes_SelectAll2` (`SELECT ...AIRestrictionClass...CarClass... FROM [StockVehicleAttributes],[BrandedPart],[Model]`, confirmed via `mdb-queries ~/.emu32/Data/DB/Online.mdb`). "prefClass" = column 1 = `AIRestrictionClass` (the variable name is misleading, not a bug).
- Ruled out via `mdbtools`: bad source data (fully populated, 0-7, real), bad file copy (`Tmp.MDB` byte-identical schema/query/relationships to `Online.mdb`), lock conflicts (zero failures in a full run). One unexplained oddity, not chased further: `Tmp.ldb` opens twice same-millisecond/same-thread, only the second handle ever used, no failures result.
- `Dbcode_Fetch` (game's own DAO wrapper, `0x8f9c10`) calls `GetValue(recordset,col,row)` (real DAO C++ wrapper, `0x40da3f`) per bound column; `GetValue` returns `NULL` exactly when `col >= Fields.Count` (a real COM property read on the real `dao350.dll` Recordset). `Dbcode_Fetch` treats `NULL` as "no data," prints the `dbcode.c(3687)` warning, and **returns immediately** -- explaining why `dblog.txt` only ever shows column 1 (it's the *first* failure per row, not literally the only one).
- Since column 0 never warns and column 1 always does, on every row, every run: `Fields.Count` is provably exactly `1` for this query's live recordset (logical deduction from existing `dblog.txt` evidence, no live check needed for this specific fact).
- `Fields.Count` comes from `DBParamQuery::DoQuery`'s real body (`0x00997450` -- `0x40758b` is just a `JMP` thunk to it) calling the real `_DAOQueryDef::OpenRecordset` COM method. `DBParamQuery`'s constructor only resolves the QueryDef by name (confirmed succeeds -- no abort) and binds unrelated `Parameters`, never touches `Fields`.

**Not yet root-caused past this point, and not yet fixed**: *why* the real `OpenRecordset()` call returns `Fields.Count==1` for this specific query in tew's environment. Confirmed live which of `DoQuery`'s 2 calls-per-run is ours (return address matches `DB_GetGameConfigCarTableOffline`'s call site exactly), but two attempts to catch the actual HRESULT/count by single-stepping past the real vtable call both missed -- 30 steps undershot (still mid-air), 300 overshot clean past the whole ~1200-byte function body. **Next step**: `get_function_instructions` on `0x00997450` was already fetched in full tonight (reuse, don't re-fetch) -- find the exact `CALL [reg+0x??]` instruction for the "recordset not yet open" branch and its precisely-following address from that *real* disassembly (not the decompiler's internal "return address" literals, which is what led to two wrong breakpoint addresses tonight -- `0x0099729f` and `0x009975c5` were both never reached), then breakpoint exactly there to read `EAX` (HRESULT) and the resulting `Fields.Count` directly.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- `run_exe.py`'s breakpoint slots: 7 of 8 are still occupied by stale msjet35.dll/DAO-3075 probes from an earlier, unrelated investigation (`_source_rewrite_probe`, `_parser_probe`, `_exit_probe`, `_dat_ab04_probe`, `_lookahead_call_probe`, `_lookahead_result_probe`, `_gated_scan_token_probe`) -- despite this file previously claiming "all 8 slots free," they're still textually registered; only 1 slot is actually free. Clean these up before the next investigation that needs more than 1 breakpoint.
