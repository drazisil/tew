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

## Current status (2026-08-28, cont'd x39) — ROOT CAUSE FOUND AND FIXED: `StockAssembly_SelectAPT` (and every other date-literal-containing query) was failing because `GetLocaleInfoW` was a bare "always fail, zero logging" stub; real fix in place, one new (unrelated, trivial) gap found immediately after: `msvcrt.dll!_wtoi` unimplemented.

**The chain, traced live end-to-end from the DAO-3075-style HRESULT all the way down into real oleaut32.dll internals** (continuing past where cont'd x38 left off, `FUN_7a862215`'s `FUN_7a85e7e1`):

```
FUN_7a85e7e1 (msjet35.dll's real SQL execution-plan compiler tail) does NOT
produce -3100 itself (its own hardcoded fallback codes are all in the 3xxx
range, e.g. 0xbd8=3032) -- confirmed a dead end, live probes on it and its
neighbors (FUN_7a8c18ed/FUN_7a8c190b) never fired at all for the failing call.

Real producer found via a Ghidra byte-search across msjet35.dll for the raw
0xfffff3e4 (-3100) immediate (`B8 E4 F3 FF FF`), live-confirmed against 8
candidate sites: FUN_7a869ced (call site 0x7a8c6d67) is the one that fires --
its entry captured the REAL WHERE-clause token live:
  '(((BrandedPart.MfgDate)<>#1/1/2010#) AND ((PartType.AbstractPartTypeID)=[apt]))'
(confirmed byte-for-byte against mdbtools' `mdb-queries` dump of the real
stored SQL -- not corrupted, not misread).

FUN_7a869ced -> FUN_7a86756b (msjet35.dll's real expression tokenizer/parser,
a genuine hand-written recursive-descent state machine) -> tokenizes "1/"
correctly (locale date-separator match against the REAL locale-info struct
succeeds) -> FUN_7716c53f (real OLEAUT32.DLL, not msjet35 -- the whole
compiled expression eventually reaches real VarDateFromStr, Ordinal #94,
called from msjet35.dll's own VariantChangeType-style dispatcher,
FUN_7a8a27dc case 7/VT_DATE) resolves month=1/day=1 correctly, but the
THIRD field ("this", the locale struct's month/day/year accumulator slot
that should hold the year candidate) was NEVER populated by our own
tokenizer for this call -- confirmed live it's genuinely stale/uninitialized
stack memory left over from an unrelated EARLIER call ~17s prior in the
same run, not corrupted by anything in this call chain.

That stale value (0x01010101) gets fed into FUN_77165b74 (a
GetLocalTime-based "default missing year to current year" fallback) via
FUN_7716c47d (a real century-windowing/calendar-conversion helper) -- BOTH
confirmed live to be computing/passing through their inputs completely
correctly (FUN_77165b74's own GetLocalTime call correctly returns 2026).
The REAL culprit is upstream of all of this: the locale-info struct
VarDateFromStr consults for EVERYTHING (date separator, calendar type,
first-day-of-week, month names, era tables -- a ~3484-byte OLEAUT32-internal
cache, NOT the small UDATE result struct) was itself never properly built.

Traced via a static global pointer (`DAT_771a10c8`, the head of oleaut32's
process-wide locale-struct cache list) watched from run start: it's NULL
for the ENTIRE run until exactly one write, live-correlated to our failing
query's own VarDateFromStr call. That write happens in FUN_77145563
(oleaut32's locale-struct cache manager) -- confirmed via RAW ASSEMBLY (not
just decompile, per Molly's explicit request) that it calls the real
struct-builder (FUN_77145656) and then UNCONDITIONALLY caches + reports
success regardless of that builder's own return value -- there is
genuinely no CMP/TEST on it anywhere in the instruction stream between the
CALL and the cache-list write.

FUN_77145656 (confirmed live: called with the CORRECT lcid=0x409,
flags=0x80000000 -- no bad-argument story here) fails on its very FIRST
real locale query (LOCALE_IDATE via FUN_771454bc -> FUN_7713cee1, which
unconditionally calls real GetLocaleInfoW on the Wide code path -- live-
confirmed DAT_771a1030==0 selects that path). `kernel32.dll!GetLocaleInfoW`
was a bare stub: `EAX=0; cleanup_stdcall(...)` -- no logic, no logging at
all, which is exactly why "zero log lines" earlier in this same
investigation was wrongly read as "never called" (FUN_7713cee1 IS called,
twice, confirmed via a direct entry probe -- its own downstream real API
call was just silent). GetLocaleInfoW's honest failure return propagates
correctly through FUN_771454bc's retry-then-E_FAIL logic, and
FUN_77145656 bails at its very first query -- meaning the LCID field
itself, the calendar-type clamp, the date separator, and everything else
in the struct past that first call point never get set, staying at their
zero-initialized allocation defaults. FUN_77145563 (see above) then caches
this broken struct globally, permanently, for the rest of the process's
life -- serving it to every subsequent locale-info request from any
thread, regardless of what locale they actually asked for.
```

**Fixed**: `GetLocaleInfoW` (`kernel32.dll`) had never been properly implemented -- moved out of `kernel32_io.py`'s bare stub into `kernel32_locale.py` alongside `_get_locale_info_a`, sharing its `_LOCALE_STRINGS`/`_LOCALE_NUMBERS` tables (writes UTF-16 instead of ANSI bytes, `cch_data` counted in WCHARs per real TCHAR convention). Two more real bugs found and fixed while wiring it up: (1) the LCTYPE mask only stripped `LOCALE_RETURN_NUMBER` (`0x20000000`), not the separate `LOCALE_NOUSEROVERRIDE` (`0x80000000`) modifier flag that real callers OR in — `0x80000021` was failing table lookup for what should just be `0x21`; (2) several LCTYPEs (e.g. `LOCALE_IDATE`) are queried by real code *without* `LOCALE_RETURN_NUMBER` and expect the plain decimal-digit **string** form even though tew only had them in the numeric table — added a `_locale_string_for()` fallback that returns `str(value)` from `_LOCALE_NUMBERS` when `_LOCALE_STRINGS` has no entry, instead of duplicating every numeric constant into both tables.

**Confirmed live, post-fix**: all 4 real `GetLocaleInfoW` calls this chain makes now succeed with the *correct* real values (`LOCALE_IDATE='0'`, `LOCALE_IFIRSTDAYOFWEEK='6'`, `LOCALE_IFIRSTWEEKOFYEAR='0'`, `LOCALE_ICALENDARTYPE='1'`) — execution proceeds further into `FUN_77145656` than ever before and hits a **new, unrelated, trivial** gap: `[UNIMPLEMENTED] msvcrt.dll!_wtoi — halting` (real oleaut32.dll code converting the calendar-type digit string to an int). Not yet implemented — straightforward wide-string-to-int, next session should start here.

**Separately confirmed real, independently fixed, not the root cause**: `TlsGetValue` (`kernel32_sync.py`) had no per-thread isolation at all — `_tls_set_value` wrote to both a shared `TEB_BASE`-relative memory slot AND a per-thread `state.tls_thread_store(tid)` dict, but `_tls_get_value` only ever read the shared slot, meaning any two threads sharing a TLS index would clobber each other's values. Fixed to read from the per-thread store. Confirmed via rerun this doesn't change the outcome for this specific investigation (no cross-thread TLS contention on this call path) but is a real, general correctness bug worth keeping fixed.

**Open, unconfirmed suspicion flagged by Molly, not yet investigated**: `FUN_77121505` (oleaut32.dll's hand-crafted `__chkesp`-style stack-check/cleanup helper — confirmed live it reliably passes through whatever's in `EAX` when there's no error, which is what makes `FUN_7713cee1`'s Ghidra-misidentified `void` return type actually work correctly for the success path) may have a real bug on its OWN error path, possibly related to an earlier (pre-compaction, not found in this session's own logs) observation of a thread "going splat" -- swallowing/discarding a real exception rather than propagating it. Needs re-running with the `thread` log category enabled (not included in this investigation's `LOG_CATEGORIES=cpu,startup,loader,com,handlers`) to find the original incident before deciding whether/how to fix.

**Also confirmed NOT the bug, via the earlier-session (2026-08-25/26-ish) `DumpErrors`/`Error.Description` investigation**: the `CreateErrorInfo`/`SetErrorInfo`/`GetErrorInfo` OLE rich-error-info plumbing (`oleaut32.dll` ordinals 201/202) was already implemented in a prior session and confirmed working -- but `Error.Description` for this error class comes back as a real, validly-allocated, genuinely zero-length BSTR (not a plumbing bug, that's what real Jet actually produces for DAO-3075). `DBParamQuery`'s own `get_Count` failure branch doesn't even call `GetErrorInfo` anyway -- it aborts with a hardcoded format string directly.

Repro: `cd /data/Code/tew && TEW_FIXED_HEARTBEAT_MS=100 TEW_MAX_STEPS=5000000000 LOG_LEVEL=debug LOG_CATEGORIES=cpu,startup,loader,com,handlers timeout 300 .venv/bin/python run_exe.py`. `run_exe.py`'s investigation-probe infrastructure from this session is left in place, commented out with dated explanatory notes at each resolved checkpoint (grep for `2026-08-28`) -- re-enable selectively rather than re-deriving addresses from scratch. The 8-logpoint-slot cap (`cpu_add_logpoint`, still silently drops past capacity, see `TODO.md`) was hit and worked around repeatedly this session by pruning to confirmed-resolved probes before adding new ones.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- `CreateThread`'s log line (`tew/api/kernel32_io.py`) downgraded from `info` to `debug` -- was disproportionately noisy for a routine per-spawn event at `info` level.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
