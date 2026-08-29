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

## Known false leads (permanent — do not remove on rotation)

- **`dbcode.c(3376) "The class has not been licensed"`**: prints every run, every time DAO/Jet does COM work, well before any actual failure. Molly confirmed (2026-08-16) this is expected/ignorable — NOT the cause of `CreateQueryDef`/DAO-3075 failures. Got mistakenly re-flagged as a "new lead" once already the same night (see `status_archive.md`, "Previous status (2026-08-16, cont'd x4)", for the correction) — check here before treating it as new again.

## Current status (2026-08-29, cont'd x40) — RESOLVED: `StockAssembly_SelectAPT` (and every date-literal-containing query) now succeeds end to end. Root cause was a `classify_wide_string`/`GetStringTypeExW` edge case that made the date-string tokenizer overshoot the end of a numeric token by 2 WCHARs. New, unrelated blocker opened immediately downstream: an unhandled SEH fault at `EIP=0x1901d9eb`.

**Picking up from x39's `_wtoi` gap**, fixed a chain of small, real gaps one at a time, each confirmed live before moving to the next: `msvcrt.dll!_wtoi`/`_itoa` (both genuinely unimplemented, now real cdecl implementations in `msvcrt_handlers.py`), a mislabeled locale-string-table entry (`0x0038` was wrongly recorded as `LOCALE_SYEARMONTH` — it's actually the start of the `LOCALE_SMONTHNAME1..13` range, `0x0038`-`0x0044`; corrected and filled in the whole range in `kernel32_locale.py`), `kernel32.dll!GetCalendarInfoW` (a distinct, GetProcAddress-resolved API entirely missing before — real CAL_GREGORIAN CALTYPE table added), and `kernel32.dll!NlsGetCacheUpdateCount` (undocumented but real 0-param export; returns `0` since tew's locale data never gets invalidated). Also closed a `GetLocaleInfoW`-style silent-stub gap in `_multi_byte_to_wide`/`_wide_to_multi_byte` (`kernel32_locale.py`) — neither had ever logged on any path, so "no log line" could never be trusted as "never called"; both now log.

**With every locale/calendar gap closed, `VarDateFromStr("1/1/2010")` still failed** (`EAX=0x80020005`, `DISP_E_TYPEMISMATCH`). Traced live via a chain of entry/return probes (working around Ghidra's repeated inability to identify real `EAX` passthrough on stack-frame-omitted VC6 functions — `FUN_77121505`, `FUN_7716d2ff`, `FUN_7716bde4`, `FUN_7713cf60` are all declared `void` by Ghidra despite genuinely returning a value through untouched `EAX`, confirmed live each time rather than trusted from decompile):

```
VarDateFromStr's own state machine (switch(local_8) at 0x7716dd7d) went
0 -> 3 -> 5 -> 0xd, where 0xd has no case and falls to the function's
terminal-failure `return local_14` (still DISP_E_TYPEMISMATCH, nothing
along this path ever set it to 0).

Traced why token 3 (the 4-digit year "2010", the LAST token in
"1/1/2010") never got classified: the tokenizer's digit-collection loop
(inside FUN_7716d562) determines "still a digit" via FUN_7713cf60 ->
FUN_7713cfd6 -> real GetStringTypeExW (confirmed live DAT_771a1030==0
selects the Wide path) -> tew's classify_wide_string (char_type.py).

FUN_7713cf60 tests ONE character at a time via a 2-WCHAR buffer
[char, 0] with cchSrc=-1 (null-terminated convention). When the character
BEING TESTED is itself the string's own null terminator, that buffer's
own effective length is 0 -- classify_wide_string's loop ran 0
iterations and left the output flags WORD completely unwritten. The
caller then read back whatever was ALREADY in that stack slot: the
leftover DIGIT flag from testing the real digit '0' immediately before
it. Confirmed live via raw memory dumps (run_exe.py's
_fun_7716bde4_entry_probe, extended to dump raw WCHARs around the
tokenizer's position pointer): for "1/1/2010", the real null terminator
sits at a known, correctly-predicted address (cross-checked against
where tokens 1 and 2 correctly landed on "/") -- but the digit loop
consumed it AND a second stray null right after it (heap padding) before
finally hitting real garbage 2 WCHARs later, which correctly stopped it.
That left the tokenizer's position pointer 2 WCHARs past the true end of
string, so the NEXT classification step (FUN_7716bde4, checking what
follows the number) saw garbage instead of end-of-string, never returned
its "end of string" code (1), and the token stayed at its unset default
sentinel -- routing the outer FSM straight into the terminal failure
state.
```

**Fixed**: `classify_wide_string` (`tew/api/char_type.py`) now special-cases a null-terminated (`cch_src=-1`) request whose computed length is 0 — it writes a real classification word for the terminator character itself (`classify_ctype1(0)`, correctly `CNTRL` only, never `DIGIT`) instead of leaving the output buffer untouched. Confirmed live: the third token's tokenizer position now lands exactly on the real null terminator (`text_ptr=0x76ae854`, matching the address predicted from tokens 1/2's known-good positions), `FUN_7716bde4` correctly returns `1` (end of string), and `VarDateFromStr` returns `EAX=0x0` (success) for `"1/1/2010"`.

**Confirmed end to end**: `stdout.txt`'s `"could not get param count"` / `StockAssembly_SelectAPT` DBQuery error no longer appears anywhere in a fresh run. The game now runs straight past the entire query into new territory and hits a genuinely different, unrelated halt: an unhandled SEH fault at `EIP=0x1901d9eb` (0x19xxxxxx range — a different DLL entirely). Not yet investigated — next session should start here.

**Resolved (was open in x39)**: Molly's `FUN_77121505` "thread splat" suspicion — reran with `thread` added to `LOG_CATEGORIES` and it never fires at all. What DOES fire early in every run is the already-documented `THREAD_SENTINEL` collision (`TODO.md`, "NEW (2026-08-26)"): `OLEAUT32.dll`'s real `DllMain` runs a static initializer via `_call_guest_void`, which shares `THREAD_SENTINEL` with real thread completion, spuriously marking the calling thread dead. Non-fatal (`_invoke_emulated_proc` catches it, returns 0, execution proceeds normally) — almost certainly what Molly actually recalled, not `FUN_77121505` (confirmed, separately, to be a real, correct stack-cookie check with clean success/failure semantics).

**Also fixed, unrelated cleanup found along the way**: `dll_loader.py`'s `patch_dll_iats` summary log line undercounted -- its "Patched X/N ... with stubs" numerator only counted `"handler"` outcomes, excluding `"auto"` (auto-generated fatal-halt) outcomes despite those also being real stubs; harmless when both counts happened to be 0 but wrong in general. Now sums both, and logs per-DLL (previously one aggregate line could blur which of several DLLs loaded in one batch had the gap).

Repro: `cd /data/Code/tew && TEW_FIXED_HEARTBEAT_MS=100 TEW_MAX_STEPS=5000000000 LOG_LEVEL=debug LOG_CATEGORIES=cpu,startup,loader,com,handlers timeout 300 .venv/bin/python run_exe.py`. `run_exe.py`'s investigation-probe infrastructure is left in place, commented out with dated explanatory notes at each resolved checkpoint (grep for `2026-08-29`) -- re-enable selectively rather than re-deriving addresses from scratch. The 8-logpoint-slot cap (`cpu_add_logpoint`, still silently drops past capacity, see `TODO.md`) was hit and worked around repeatedly this session the same way as x38/x39.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- `CreateThread`'s log line (`tew/api/kernel32_io.py`) downgraded from `info` to `debug` -- was disproportionately noisy for a routine per-spawn event at `info` level.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
