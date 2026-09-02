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

## Current status (2026-09-02, cont'd x50) — RESOLVED: `WSAStartup` always reported back Winsock version 2.2 regardless of what was requested, silently failing `TCPMgr::Initialize`'s version check and skipping all three `MessagePool::Initialize` calls every run — root cause of the `MessagePool::Get(NULL)` crash chased since x49. Also fixed two missing wsock32 ordinal aliases (`gethostbyname`/`gethostbyname`) and made `OutputDebugString` bypass `LOG_LEVEL`/`LOG_CATEGORIES` like other crash-diagnostic lines. Confirmed live: the game now completes both `TCPMgr::Initialize` and `SocketMgr::Initialize` fully (both worker threads spawn) and reaches real connection-attempt code for the first time. New, unrelated blocker: a DirectSound `DS::DuplicateSoundBuffer` "invalid this" halt.

**`0x004d980f` at x49 (the fault inside the nested leak-dump call) turned out to be a dead end, not a mechanism worth chasing** — it's just `_CLayer_DetectDebugger`'s already-understood anti-debug self-test (see `changelog.md`'s 2026-08-22 through 2026-08-24 entries), re-triggered from a different thread context; confirmed unrelated to everything below.

**Root cause, fully traced via Ghidra + a live EBP-chain walk of the real `MessagePool::Get(NULL)` crash**: `CommMgr::GetFreeMsg` (`0x00a80163`) picks one of three `MessagePool*` member fields (small/medium/large, at `this+0x34/0x38/0x3c`) based on requested size, read straight off the object — no null check. Traced upstream via the crash's real EBP chain (`DBHandlers.c` → `DBResultQ_AllocMsg` → `SocketMgr::Initialize` → `TCPMgr::Initialize`, `0x00a7a4fe`): `TCPMgr::Initialize` calls `Ordinal_115` (`WSAStartup`, `wsock32.dll`) requesting `MAKEWORD(1,1)=0x0101` on `this+0x76` (set in the constructor), then does an **exact byte-for-byte match** of the returned `wVersion` against what it asked for before proceeding — real Winsock always echoes back the requested version; `tew/api/wsock32_handlers.py`'s `_wsa_startup` hardcoded `wVersion=0x0202` unconditionally. The check always failed, `TCPMgr::Initialize` jumped straight to its error path (`Ordinal_116`/`WSACleanup`, return `0x11`) **without ever calling any of the three `MessagePool::Initialize`s** — but `DAT_020d6250` (the global `CommMgr*`) is set *before* `Initialize()` runs, so `DBResultQ_Startup`'s own null-check thinks it's "already done" forever after and never retries.

**Fix** (`tew/api/wsock32_handlers.py`): `_wsa_startup` now reads the real `wVersionRequested` stack arg and echoes it back as `wVersion` (matching real WSAStartup semantics); `wHighVersion` stays the DLL's real max (`0x0202`, unrelated to the request). New `tests/unit/api/test_wsock32_wsastartup.py` (5 tests).

**Also found while verifying live**: `wsock32.dll!Ordinal #57` (`gethostname`) unimplemented halt, right after the version fix cleared the MessagePool crash — both `gethostname` and `gethostbyname` were already fully implemented and registered *by name*, just missing from `ordinal_map` (same "wrong `GetProcAddress` key" bug class as the 2026-08-21 `LoadTypeLibEx` fix). Confirmed via `objdump -p` on the real `wsock32.dll` and MCity_d.exe's own import table (ordinal-only, no names) that the game imports exactly ordinals 52 and 57 among the previously-missing ones; added the full real contiguous ordinal block (51/`gethostbyaddr`, 52/`gethostbyname`, 53/`getprotobyname`, 54/`getprotobynumber`, 55/`getservbyname`, 56/`getservbyport`, 57/`gethostname`) for completeness. New `tests/unit/api/test_wsock32_ordinals.py` (5 tests, including an every-real-ordinal aliasing regression guard).

**Also fixed**: `OutputDebugString` (`tew/api/kernel32_io.py`) used `logger.info`, silently droppable by `LOG_LEVEL`/`LOG_CATEGORIES` — real debuggers show it unconditionally. Switched to `logger.always()`, matching the precedent for other load-bearing diagnostic lines (2026-08-21 x5). Confirmed live: previously-invisible startup lines (Chat Filter thread, INet threads, AnalyzeAPI Init) now show up regardless of category filter.

**Confirmed live, full chain**: with all of the above, a run now shows `TcpMgr::Initialize(keepAliveThread) thread created` and `SockMgr::Initialize(outgoingThread) thread created` — both `Initialize()` calls complete fully for the first time. Reaches `Missing resource file: scn.login` (non-fatal) then halts at 77.5s on `DS::DuplicateSoundBuffer: invalid this=0x067c0080 (expected 0x002203a0)` — a DirectSound bug, unrelated to any of the above, not yet investigated.

Repro: `cd /data/Code/tew/.claude/worktrees/investigate-004d980f && TEW_MAX_STEPS=5000000000 LOG_LEVEL=info LOG_CATEGORIES=exception,seh,startup timeout 300 .venv/bin/python run_exe.py`. Branch: `worktree-investigate-004d980f` (stacked on `worktree-getdoubleclicktime`, PR #13). Full suite passing (1208 tests).

**Next**: `DS::DuplicateSoundBuffer` invalid-`this` halt — a new, unrelated DirectSound investigation.

**Housekeeping, still live from earlier sessions**:
- ClickHouse execution-history capture (`~/pe-walker/history-poc` docker-compose) does **not** survive a reboot/power-cut -- needs `docker compose up -d` again (schema/data persist on the bind-mounted volume, just the container needs restarting). Same for `ghidra-mcp.service`'s project state -- survives service restart via systemd, but needs a fresh MCP handshake (new session ID) and re-opening the project/program.
- Ghidra's full auto-analysis crashes on `expsrv.dll` but works fine on `msjet35.dll` -- both are in the `mcity` project (separate from this project's own default, `debug_clean`; remember to switch back and forth as needed, and to switch back to `debug_clean` when done so `mcity` isn't left locked).
- **CORRECTION (2026-08-30), supersedes `status_archive.md`'s x12/2026-08-25 and 2026-07-24 entries**: "restart the compositor in place" (`kwin_wayland --replace ...`, or `systemctl --user restart plasma-kwin_wayland.service`) is **no longer a valid stuck-SDL troubleshooting step** -- confirmed tonight (2026-08-30) that a compositor restart crashes the *entire user session*, not just the wedged tew client, twice in a row. Whatever made this a safe in-place recovery in 2026-07-24/2026-08-25 no longer holds (environment/KWin-version drift, most likely -- not investigated further). Do not attempt a compositor restart as an automated recovery step; if a run hangs at SDL2/window init, check for and clean up orphaned `run_exe.py` processes first (`SIGTERM`, not `-9`, to let `window_manager.shutdown()` run), and otherwise stop and ask Molly rather than touching the compositor.
