# TODO

Durable follow-up work, checked/updated as picked up. Distinct from status.md
(current session's active blocker) and changelog.md (completed work) --
items here are queued but not yet started, or started and paused.

---

## OBSOLETE (2026-08-26): real `.tlb` type-library parsing for `LoadTypeLibEx`

**Superseded, do not pick this up** -- the entire premise was wrong. `LoadTypeLibEx`
never actually needed a hand-built parser: `oleaut32.dll` genuinely loads as
real code in this emulator, but was being unconditionally shadowed by
`oleaut32_handlers.py`'s own registered Python handlers (`dll_loader.py`'s
`patch_iat_entry` tries a registered handler before ever checking a real
DLL's export). Fixed 2026-08-26 by dropping every `"oleaut32.dll"`
registration that file makes -- real `oleaut32.dll` now parses `expsrv.dll`'s
real, embedded `TYPELIB` PE resource itself and answers `Bind`/`GetDllEntry`/
`GetFuncDesc` correctly and automatically. See changelog.md 2026-08-26.

**Follow-up cleanup, not urgent**: `oleaut32_handlers.py` still contains the
now-dead `_EXPR_FUNCTIONS` table, hand-crafted `FUNCDESC`/`ITypeInfo`/
`ITypeComp`/`ITypeLib` trap-object code, and `GetDllEntry`/`Bind` handlers
from the superseded investigation -- none of it executes anymore (the
wrapper silently drops its registration), but it's dead weight in the file.
Should be deleted, along with re-auditing whether any of the ~35
pre-existing (pre-dating this investigation) `oleaut32.dll` handlers in that
file were ALSO unnecessarily shadowing real code the whole time, not just
the `LoadTypeLibEx`-related ones added during this investigation.

## Database initialization failure (NEW, 2026-08-26, cont'd x32 -- current blocker)

With real `oleaut32.dll` now genuinely running, the emulator reaches a new,
legitimate `INT3` assertion inside `MCity_d.exe` itself at ~40.6s (`tid=1000`).
Real, human-readable reason from the game's own `~/.emu32/MCity/stdout.txt`:
`Nfs.c(677) Database initialization failed!` / `nfspc.c(1164) NFS_abortmsg
callback 'Failed to initialize database. Please be sure you have setup the
DCOM and DAO drivers provided on your installation disk...'`. Not yet
investigated at all -- next session should start here. Unclear whether this
is a new manifestation of something related to the original `expsrv.dll`
crash chain, or a completely separate DAO/DCOM setup issue that was simply
never reached before (since the old trap-object code was intercepting calls
before real `oleaut32.dll`/DAO initialization could run this far for real).
