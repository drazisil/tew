# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

---
*This file: current blocker, queued issues, run command, architecture. Completed work goes in changelog.md — do not add "what's fixed" sections here. Full investigation history (the DAO `*ppv` NULL saga, `tid=1012`'s death, the `fatal_halt` fix, etc.) lives in changelog.md, newest-first — do not re-derive any of it from scratch, grep changelog.md instead.*
---

## Current status (2026-07-23)

The DAO `*ppv`-stays-NULL mystery that this project chased across several
sessions (2026-07-19 through 2026-07-23) is **resolved**. Root cause: three
missing dependencies in `dao350.dll`'s real `DllGetClassObject` call chain
(`ole32.dll!CoGetMalloc` entirely unimplemented, plus two `oleaut32.dll`
ordinal-only import aliases missing — ordinals #15 `SafeArrayCreate` and #21
`SafeArrayLock`) were causing the call to abort before reaching its own
(correct) `QueryInterface` code; `_invoke_emulated_proc`'s bare-`0`-on-abort
fallback then made the abort look like a genuine `S_OK` success with a NULL
`*ppv`. All three fixed in `tew/api/oleaut32_handlers.py`. Full diagnosis
and fix sequence: changelog.md, "2026-07-23 (later session)" and the three
entries before it.

Separately, `cpu.fatal_halt` is now a real, unclearable native CPU lockup —
previously a Python-side desync let execution continue past a fatal halt to
a later, unrelated one instead of stopping dead. Fixed at the Zig/CPU layer.
Full detail: changelog.md, "2026-07-23 — cpu.fatal_halt is now a real,
unclearable native CPU lockup."

And the sentinel-collision bug that fix exposed is now fixed too:
`CPU.run()`/`CPU.step()` raise a new `FatalHaltError` the instant
`cpu.fatal_halt` newly becomes true during a call, instead of returning
normally and leaving `_invoke_emulated_proc` to fall back to a bare `0`
that any `HRESULT`-returning caller could misread as `S_OK`. Full detail:
changelog.md, "2026-07-23 (evening session) — `_invoke_emulated_proc`'s
'didn't complete' `0`-return sentinel replaced with a raised exception."

With `oleaut32.dll` ordinal #4 (`SysAllocStringLen`) also fixed the same
way as #15/#21, DAO's entire COM activation chain now completes cleanly
end-to-end: `CoGetClassObject` ×2 and `CoCreateInstance` all return real,
non-fake results, and execution genuinely **returns to the game's own
code** (`MCity_d.exe`) for the first time — the "ole32 block" that
motivated this whole multi-day investigation is fully cleared. Two more
small gaps found and fixed live-verifying that: `kernel32.dll!lstrcmpW`
(real UTF-16 comparison, `kernel32_io.py`) and `kernel32.dll!GlobalLock`/
`GlobalUnlock` (pass-through no-ops, correct for the fixed/non-moveable
memory this emulator's `GlobalAlloc` always hands out).

The `msjter35.dll`/`msjet35.dll` busy-loop described above (7,005×
`GetProcAddress` repeats, zero progress) is **resolved** — two independent
bugs, both in `kernel32_handlers.py`: (1) `LoadLibraryA`'s fallback for
DLLs not found on disk unconditionally fabricated a fake-success handle
even with zero handler coverage, unlike `GetModuleHandleA`'s equivalent
path — DAO saw a fake "loaded" DLL and kept retrying instead of getting an
honest failure; (2) `GetProcAddress`'s ordinal-lookup key format
(`"ordinal#N"`) never matched how ordinals are actually registered/parsed
everywhere else in the codebase (`"Ordinal #N"`), so ordinal lookups could
never succeed regardless of whether the export existed. Full diagnosis and
fix: changelog.md, "2026-07-23 (late-night session)".

Separately, since the actual goal is a working Access Jet 3 database (not
just DAO's COM activation succeeding), the real Microsoft Jet 3.5 Database
Engine redistributable was sourced from `~/.emu32/DBInst/DAO/data1.cab`
(same InstallShield package `dao350.dll` came from — confirmed via sha256)
and deployed to `~/.emu32/WINDOWS/System32/`: `msjet35.dll`, `msjter35.dll`,
`msjint35.dll`, `vbajet32.dll`, `msrd2x35.dll`, `expsrv.dll`, and
`msvcrt40.dll` (the last one incidentally fixing a previously-unresolved
static import of DAO350.DLL's own). See Architecture section.

`advapi32.dll!RegEnumKeyA` — the older, non-Ex sibling of
`RegEnumKeyExA` (4 args, `cchName` passed by value not by pointer, no
class/last-write-time output) — was simply never implemented, only
`RegEnumKeyExA` existed. Added it in `advapi32_handlers.py`, sharing a new
`_reg_list_subkeys()` helper factored out of `RegEnumKeyExA`'s subkey-
derivation logic. Confirmed live: execution now sails through the entire
`HKLM\Software\Microsoft\Jet\3.5\Engines` enumeration and `Engines\ODBC`
config reads (all honest `NOT FOUND`s, gracefully tolerated) — no seeding
of `registry.json` was needed for this. 593/593 tests still passing.

`kernel32.dll!GetTempPathA` was next: added in `kernel32_io.py`, returns
`C:\WINDOWS\TEMP\` (backed by a real, newly-created
`~/.emu32/WINDOWS/TEMP/` host directory so later real file I/O against
that path works). Confirmed live: cleared the halt.

`kernel32.dll!GetTempFileNameA` was the halt right after that — Jet
generating its scratch filename. Added in `kernel32_io.py`: builds
`<path><3-char prefix><4 hex digits>.TMP`, and when `uUnique == 0` (the
common case) actually creates the 0-byte file on the host filesystem via
the same `os.open(..., O_CREAT|O_TRUNC)` pattern `CreateFileA`'s writable
branch uses — so a later real `CreateFileA`/`ReadFile` against that exact
name (Jet's own scratch-file use) sees a real file, not just a name.
Confirmed live: cleared the halt. 593/593 tests passing after both.

`kernel32.dll!GetFileInformationByHandle` was next — Jet querying the
new temp file's attributes/timestamps. Added in `kernel32_io.py`: looks
up the handle in `state.file_handle_map`, `os.fstat`s the real fd (or
`os.stat`s `entry.path` for read-only entries with no fd), and fills a
real `BY_HANDLE_FILE_INFORMATION` struct (attributes via `stat.S_ISDIR`,
real `ctime`/`atime`/`mtime` converted to `FILETIME`, real size, `1` for
link count, real inode as file index). Confirmed live: cleared the halt.

`kernel32.dll!lstrcpynA` was next — added in `kernel32_io.py` next to
the existing `lstrcpyA`/`lstrlenA` (bounded copy, always null-terminates
within `iMaxLength`). Confirmed live: cleared the halt.

**Real bug found and fixed, not just a missing handler**: the very next
halt, `[UNIMPLEMENTED] msjint35.dll!Ordinal #2`, looked like another
missing-handler case but wasn't — direct inspection of the real
`msjint35.dll`'s export table (via `tew`'s own `EXEFile`/`ExportTable`
parser, offline, no emulator run needed) confirmed ordinal #2
(`CchLszOfId2`) genuinely exists and `DLLLoader.load_dll` already
resolves and writes its real address into the IAT correctly. The actual
bug: `DLLLoader.patch_dll_iats` (`tew/loader/dll_loader.py`) runs
*after* `load_dll` and unconditionally re-patches every secondary-DLL
IAT entry via `patch_iat_entry` — but never passed the already-known
real address as `real_addr`, so any entry without a matching Python
handler fell straight through to the unimplemented auto-stub fallback,
silently clobbering correct real-DLL-to-real-DLL calls (e.g. `msjet35
.dll` calling into `msjint35.dll`) with a fatal halt. Fixed by having
`patch_dll_iats` look up `self._loaded_dlls[...].exports` and pass that
through as `real_addr`; also added a `real_count` outcome bucket to the
existing "Patched X/Y ... (N auto-stubs)" summary log so this class of
bug is visible going forward instead of silently inflating the
auto-stub count. Confirmed live: MSJET35.DLL's own IAT patch pass went
from 23 auto-stubs/0 real to 1 auto-stub/7 real. 593/593 tests passing
after all three fixes above.

`user32.dll!LoadStringA` was next — added in `user32_handlers.py`. Real
`RT_STRING` resource lookup was added to `pe_resources.py`
(`PEResources.find_string`, block=(id>>4)+1 / index=id&0xF packing) and
threaded per-module: `dll_loader` is now passed into
`register_user32_gdi32_handlers` (previously it wasn't) so a real loaded
DLL's own hInstance (not just the main EXE's) resolves to that DLL's own
`.rsrc`, cached per-DLL-name. `cchBufferMax == 0` (pointer-swap mode, no
copy) is explicitly **not** implemented and halts loudly instead of
silently returning a plausible-but-wrong result — confirmed live this
session that real callers never actually hit that path, so the halt is
inert in practice, not a live gap. Confirmed live: cleared the halt.

`kernel32.dll!lstrcatA` was next — added next to `lstrcpyA`/`lstrcpynA`
in `kernel32_io.py`, matching real (unbounded, like real `strcat`)
semantics. Confirmed live: cleared the halt.

`oleaut32.dll!Ordinal #202` (`CreateErrorInfo`, confirmed via the real
`oleaut32.dll`'s export table at `/data/Downloads/i386-binaries/`) was
next. Implemented as a real dual-interface COM object in
`oleaut32_handlers.py`: one allocated object with two vtables at a
+4 offset (`ICreateErrorInfo` at the object's own address, `IErrorInfo`
at +4 — a C++-style "this-adjustor" split), `QueryInterface` switching
between them, shared refcount, and real Set*/Get* method bodies that
actually read/write the object's fields (no fake success). **Live-
verified this design was necessary, not speculative over-engineering**:
DAO's real code calls `QueryInterface(IID_IErrorInfo)` on the returned
pointer immediately after creation (succeeds via the +4 face), then
fills the object via the original `ICreateErrorInfo` pointer with real
content — `SetSource("DAO.DbEngine")`, a help context ID, a help file
pointer — before the next call. Session process note: this session
skipped `CLAUDE.md`'s mandatory HANDLER DECLARATION step (state
Function/Signature/Spec/Truthful-YES-NO in chat before writing any
handler) for every handler above; a retroactive audit found one real
violation — `LoadStringA`'s `cchBufferMax==0` path was silently
returning a plausible-but-spec-incomplete result instead of halting —
now fixed as described above. No other violations found (grep audit for
TODO/FAKE/stub/silent-pass patterns across every file touched this
session came back clean).

**Current blocker**: `oleaut32.dll!Ordinal #201` (`SetErrorInfo`,
confirmed via the same export-table lookup), called immediately after
`CreateErrorInfo`'s object is fully populated. Not yet implemented —
next up. This is furthest any session has reached: past all of
`MSJET35.DLL`'s and `MSJINT35.DLL`'s init-time gaps, into DAO's own
error-reporting plumbing.

Two small non-blocking gaps surfaced earlier, before the `RegEnumKeyA`
halt (`kernel32.dll!IsTNT`, `kernel32.dll!GetProcessAffinityMask` — both
harmlessly return NULL, Jet handles the miss and keeps going).

## Run command
```bash
cd /data/Code/tew
timeout -k 5 300 env LOG_LEVEL=info LOG_CATEGORIES=com,dll,loader,exception /data/Code/tew/.venv/bin/python -u /data/Code/tew/run_exe.py 2>&1 | tee /tmp/emu.log | tail -60
```
Real `dao350.dll` execution takes anywhere from ~1s to ~30s per individual
`CoGetClassObject`/`CoCreateInstance` call, so a run reaching the DAO
section needs far more than a short timeout. Since 2026-07-21's fix, runs
now reach their final halt in ~57s instead of stalling to ~71s+ — a 300s
budget is generous headroom, not an observed requirement. Add
`registry`/`handlers` to `LOG_CATEGORIES` for deeper COM/IAT investigation;
add **`scheduler,thread`** with `LOG_LEVEL=debug` for
thread-lifecycle/scheduling investigation (idx assignment, every context
switch, every block reason).

The simpler run command (`timeout -k 5 90`, `LOG_LEVEL=info`, no extra
categories) is still correct for a general boot-health check that doesn't
need to reach all the way through the DAO handshake.

## Queued issues (priority order)
- **New top priority**: implement `oleaut32.dll!Ordinal #201`
  (`SetErrorInfo`) — see "Current status." Blocks right after DAO fully
  populates its error-info object. State a HANDLER DECLARATION first
  per `CLAUDE.md` before writing it — check what pointer it actually
  receives (the `ICreateErrorInfo` face or the `IErrorInfo` face
  obtained via the QI already observed) rather than assuming.
- Worth a dedicated pass later: now that `patch_dll_iats`'s real-address
  bug is fixed, re-check whether any of the *other* previously-"fixed"
  halts in this session were actually this same class of bug
  (real-DLL-to-real-DLL call wrongly auto-stubbed) rather than a truly
  missing Win32 API — unlikely for the kernel32/advapi32 fixes already
  made (those were genuinely-unimplemented Python-handler gaps, confirmed
  by checking the handler registry directly each time), but worth keeping
  in mind for future `[UNIMPLEMENTED] <dll>.dll!Ordinal #N` or
  `<dll>.dll!<name>` halts where `<dll>` is one of the real Jet-family
  DLLs (`msjet35.dll`, `msjint35.dll`, `vbajet32.dll`, `msrd2x35.dll`,
  `expsrv.dll`) rather than a standard Win32 system DLL.
- Low priority, not currently blocking: `kernel32.dll!IsTNT` and
  `kernel32.dll!GetProcessAffinityMask` are unimplemented (`GetProcAddress`
  returns NULL for both) — `MSJET35.DLL` tolerates the miss and continues,
  but a real caller elsewhere might not.
- Revisit `SafeArrayLock`/`SafeArrayUnaccessData` (`oleaut32.dll` ordinals
  21/24, `oleaut32_handlers.py`) at some point — both are hardcoded no-ops
  returning `S_OK` with no real lock-count tracking, harmless only because
  nothing in this emulator currently moves or frees a `SAFEARRAY`'s
  `pvData` out from under a caller. If that assumption ever changes (real
  `SafeArrayRedim`/compaction, or any future GC-like behavior), these two
  need actual `cLocks` bookkeeping (the `SAFEARRAY` header already has a
  `cLocks` field at `psa+8`, currently always `0` — see `_SafeArrayCreate`).
  Not blocking anything today.
- Correct `cpu/src/two_byte.zig`'s `CPUID` signature to real Pentium II
  (`0x00000630`/`0x00000650`) and fix this file's "source of truth" reference
  — blocked on locating the exact Pentium II spec manual to confirm
  Model/Stepping before committing to a value.
- Not in scope when the fatal-halt sentinel was replaced with an exception
  (2026-07-23 evening), noted as a related but separate gap: a genuine
  fault occurring deep inside a *nested* `_invoke_emulated_proc` call
  currently never gets an SEH-recovery attempt at all (only the top-level
  loop calls `dispatch_exception`) — it's silently swallowed into the
  bare-`0` fallback that still exists for non-fatal incompletions
  (max_steps exhausted, thread died, unexpected non-fatal halt). Worth its
  own decision later.
- Identify the `EIP=0x00200c00` final halt's real cause — confirmed
  unrelated to DAO/`DllMain` timing, still unidentified which API it is.
- Decide whether `mmtimer_callback`'s own nested-call halt (lands back at
  its own entry instead of its sentinel) is a real re-entrancy bug or
  another instance of the same "thread died mid-call" class already fixed
  for `tid=1012`.
- Decide/implement a real unwind for `seh.py`'s unhandled-fault path
  instead of "halt in place with stale stack data"
- Fix `_chkesp`'s diagnostic (`patch_internals.py`) hardcoding EBP as the
  snapshot register when it's a compiler register-allocation choice
  (confirmed ESI at one real call site)
- Dedicated pass on the ~85 of ~90 `cpu.halted = True` call sites that
  still lack the `cpu.fatal_halt` marker (priority order not yet
  established) — see [[tew_fake_kernel_gaps]] section 17's closing
  paragraph.
- SDL window resolution (1536x1248) vs. `GetDeviceCaps` (1024x768) mismatch
- DrawPrimitive / DrawIndexedPrimitive coverage beyond what's needed to
  reach the DAO abort — not yet assessed how much is implemented
- `[alive]` heartbeat silent during `GetMessageA` host-sleep — low priority
- Low priority, structural only, no runtime risk: `tew/loader/dll_loader.py`
  → `tew/pe/exe_file.py` → `tew/loader/import_resolver.py` → back to
  `dll_loader.py` form a genuine three-file import cycle (found via
  `gitnexus check --cycles` 2026-07-23, confirmed by reading the actual
  imports — not a false positive like the two other cycles gitnexus also
  flagged that turn out to be `TYPE_CHECKING`-only). Currently held together
  by two deliberate deferred (function-body, not module-level) imports:
  `DLLLoader.load_dll()` imports `EXEFile` lazily, and `EXEFile.__init__`
  imports `ImportResolver` lazily; `import_resolver.py`'s import of
  `DLLLoader` is the only real top-level one. Works today, no crash risk,
  but reflects a genuine mutual dependency between all three files — a
  cleaner layering (e.g. `EXEFile` not needing to know about
  `ImportResolver` at all) would let all three imports be plain top-level
  ones instead of relying on load-order timing. Worth a look if this area
  is touched again for other reasons; not worth a dedicated pass on its own.

## Architecture
- **CPU + memory backend**: fully Zig now, no pure-Python fallback path
  remains. `tew/hardware/cpu.py` (the original pure-Python CPU class) and
  the entire `tew/emulator/opcodes/` package (pure-Python x86 instruction
  decode) were deleted 2026-07-24 — confirmed dead (`ZigCPU.register()`
  was a no-op; opcodes were built and registered every run but never
  executed). `tew/hardware/memory.py` is likewise now a re-export shim
  over `ZigMemory` (`tew/hardware/memory_zig.py`), and the guest heap's
  bump-allocator cursor math (`CRTState.simple_alloc`) now delegates to
  `tew/hardware/alloc_zig.py`. Register/flag constants (`EAX`, `CF_BIT`,
  etc.) now come from `tew.hardware.cpu_zig`, not the deleted `cpu.py`.
  See changelog.md, "2026-07-24."
- **Zig/Python FFI boundary — kernel module**: as of 2026-07-24 (cont'd),
  the whole Zig side of `libcpu.so` is organized as a real kernel-style
  split. `cpu/src/kernel.zig` is the build root and the *only* file with
  `export fn`s anywhere in the project (63 total: CPU control, memory
  access, guest-heap allocator) — the Python-facing C ABI, full stop.
  `cpu/src/engine.zig` holds the internal execution engine (dispatch
  table, `cpuStep`, all opcode handlers), never exported, driven only by
  `kernel.zig`'s `cpu_run`. `cpu/src/primitives.zig` holds the one shared
  bounds-check/byte-access implementation both `core.zig`'s CpuState-bound
  memory helpers and `kernel.zig`'s `mem_*` C ABI delegate to (previously
  two independent reimplementations of the same logic). On the Python
  side, `tew/hardware/_kernel_lib.py` is now the single `ctypes.CDLL`
  loader shared by `cpu_zig.py`/`memory_zig.py`/`alloc_zig.py` (previously
  three independent `dlopen` calls to the same `.so`). `cpu/src/memory.zig`
  and `cpu/src/alloc.zig` no longer exist — absorbed into `kernel.zig`.
  See changelog.md, "2026-07-24 (cont'd)."
- Game does NOT call D3D8 directly.
- Rendering path: Game → THRASH API (dx8z.dll) → D3D8 (fake COM, Vulkan backend)
- WinINet connects to localhost:443 (HTTPS)
- authlogin.dll reads AuthLoginServer from registry (localhost)
- Login dialog (SDL2): admin/admin from registry, auto-filled
- Timer thread: FUN_00a30ea0, runs as tid=1006 via CRT wrapper at 0x9fc3a0
  `mmtimer_callback` (0x00a30a40) is the multimedia timer proc AND a `_tmrsub[]` subscriber.
  It calls `_SIGNAL_set(event)` + re-registers via `timeSetEvent` each tick.
  Event handle at runtime is 0x7012 (may vary).
- `0x9fc3a0` is a **generic CRT thread-spawn wrapper**, not specific to the
  timer thread — the real work function is passed as `_THREAD_create`'s
  parameter. Several threads use it (`tid=1006`-`1011`), and DAO's own
  `DllMain`-calling worker (`tid=1012`, spawned ~57s in, short-lived) is
  just another instance of the same pattern, not a DAO-specific mechanism.
- **COM activation**: registry-driven (`hkcr\clsid\{...}\inprocserver32`),
  real DLLs loaded and executed for CLSIDs in `_KNOWN_COM_SERVERS`
  (`oleaut32_handlers.py`) — currently just DAO 3.5 (`dao350.dll`, real
  file at `~/.emu32/WINDOWS/System32/`, kept out of the repo since it's a
  Microsoft-copyrighted redistributable). Unregistered or unimplemented
  CLSIDs fail honestly with `REGDB_E_CLASSNOTREG`, matching a real
  unmodified install missing that component. This pattern (search a
  directory of real DLLs, fall back to Python stub) is worth reusing for
  *other* pure user-mode COM/utility libraries the game touches — NOT for
  anything DirectX/hardware-driver-dependent (`d3d8.dll`, `ddraw.dll`,
  `dsound.dll` etc. all need a real kernel-mode HAL/driver stack this
  emulator doesn't have; tew's existing hand-built D3D8-over-Vulkan is
  already the correct solution to that problem, not something to replace).
- **Jet 3.5 database engine**: real files, same pattern as `dao350.dll`,
  also at `~/.emu32/WINDOWS/System32/` (kept out of the repo, Microsoft-
  copyrighted): `msjet35.dll` (core engine), `msjter35.dll` (error-message
  resource), `msjint35.dll` (international/collation), `vbajet32.dll`,
  `msrd2x35.dll` (Jet Red ISAM driver), `expsrv.dll` (expression service),
  `msvcrt40.dll` (DAO350.DLL's own CRT dependency). All sourced from
  `~/.emu32/DBInst/DAO/data1.cab` (InstallShield cabinet, extract with
  `unshield -d <dir> x data1.cab`) — the same install package `dao350.dll`
  itself came from, confirmed via sha256 match. Unlike `dao350.dll`, these
  are *not* gated through `_KNOWN_COM_SERVERS` (they're not COM-activated —
  DAO loads them directly via `LoadLibraryA`/`GetProcAddress` by name); they
  work because `~/.emu32/WINDOWS/System32/` was already a generic
  `DLLLoader` search path, not one scoped to COM servers only. This is the
  first case where a real DLL genuinely needs to *execute meaningfully*
  (actual Jet database reads/writes for an Access `.mdb` file), not just
  activate and hand back to caller code — expect deeper Win32/advapi32
  registry surface area to be needed than DAO alone required.

## Test suite
593 tests (all passing, reconfirmed 2026-07-24 after the memory.py Zig
port, cpu.py/opcodes retirement, the bump-allocator port, and the
kernel.zig/engine.zig/primitives.zig FFI-boundary refactor, on `main`).
