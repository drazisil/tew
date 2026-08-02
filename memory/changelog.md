# Emulator Changelog (Python port)

Entries are newest-first.

---

## 2026-08-02 (later session, cont'd again) — root cause of the `EIP=0x15035655`
MSJET35.DLL fault found and fixed: `opMovR32Imm` never honored 0x66

Used the emulator's own debugger facility (`cpu.add_logpoint` -- inline
callback, no halt, see `run_exe.py`'s "Debugger: breakpoints and logpoints"
section) instead of building new instrumentation: registered temporary
logpoints at every instruction boundary in the local dispatch chain leading
up to the fault (per the Ghidra static analysis in the prior entry) and did
one live run. The trace fired cleanly through the first 5 addresses (entry
through `0x1503564b`) and then went straight to the fault -- the 3 addresses
after `0x1503564b` never fired, pinpointing the divergence to exactly one
instruction: `0x1503564b` is `66 B8 01 00` (`MOV AX, 1`).

Root cause: `opMovR32Imm` (`cpu/src/engine.zig`, opcodes `0xB8`-`0xBF`,
`MOV r32, imm32`) never checked `s.op_size_ovr` -- unconditionally called
`fetch32(s)` regardless of the `0x66` prefix. For the real 4-byte
instruction `66 B8 01 00`, this reads 2 bytes too many as a bogus 32-bit
immediate and writes the full `EAX` instead of just `AX`, desyncing `EIP`
by 2 bytes from the real instruction stream. From there, MSJET35.DLL's
repetitive `OR imm8 / TEST / Jcc rel8`-shaped dispatch-chain bytes kept
"parsing" as plausible-but-wrong instructions for a few more (garbage)
steps until finally landing mid-instruction at `0x15035655` and hitting a
genuine access violation -- confirming the diagnosis from the prior entry
(EIP landing one byte into a real instruction, no legitimate control-flow
edge reaching it) with a concrete, live-traced root cause instead of just
static-analysis inference. Same *bug class* as the already-fixed
accumulator-immediate flags-width issue (op_size_ovr silently ignored) but
far more severe: this one corrupts `EIP` itself, not just a flag.

Fixed: `opMovR32Imm` now branches on `s.op_size_ovr` -- `fetch16` + write
only the low 16 bits (preserving the upper 16, matching real 16-bit MOV
semantics) when set, `fetch32` unchanged otherwise. Rebuilt `libcpu.so`.

3 new regression tests in `tests/unit/emulator/test_opcodes_mov.py`
(`TestMovR16Imm16`): AX and CX forms honor the prefix and preserve upper
bits, plus a direct reproduction of the live bug shape (`MOV AX,1` followed
immediately by a second real instruction, asserting `EIP` lands exactly on
it, not 2 bytes past). Verified real: stashed just this fix (which also
un-stashed the earlier flags-width fix, both live in the same file --
confirmed the 3 new tests specifically fail with `EIP=6` instead of `4`),
restored, rebuilt. 612/612 tests passing (609 + 3 new) with both fixes in
place.

Confirmed live end-to-end: re-ran the same scenario that previously
produced the `EIP=0x15035655` unhandled-SEH halt. MSJET35.DLL now loads and
runs cleanly with zero `[seh]` fault lines; execution progresses noticeably
further (60.045s vs. the previous run's 58.55s) before halting at the
already-known, separately-tracked `EIP=0x00688c69` (see status.md's queued
seh.py real-unwind item) -- real forward progress, not a new regression.
The temporary logpoint instrumentation added to `run_exe.py` for this
investigation was removed once the root cause was confirmed.

## 2026-08-02 (later session, cont'd) — accumulator-immediate opcodes'
16-bit flags-width bug fixed; 8-bit + 0x66-prefix regression tests added

While investigating the `EIP=0x15035655` MSJET35.DLL fault (see prior entry;
that fault is an 8-bit `OR AL, 0x04`, unaffected by this bug), checked
whether tew's 8-bit opcode handlers could suffer the same class of bug as
the old TypeScript emulator's 2026-03-30 `0x66`-prefix fix
(`/data/Code/exe/memory/changelog.md`, "0x66 prefix fixes"). They can't --
8-bit forms are separate opcode numbers, never gated by `op_size_ovr`, and
`engine.zig`'s handlers (`op04`/`op0C`/`op1C`/`op24`/`op2C`/`op3C`/`op80`/
`opA8`/`op84`/`op88`/`op8A`/`opF6`) all correctly use `.w8` throughout.

But their *16-bit* siblings -- the 9 accumulator-immediate opcodes `op05`
(ADD), `op15` (ADC), `op1D` (SBB), `op2D` (SUB), `op3D` (CMP), `op0D` (OR),
`op25` (AND), `op35` (XOR), `opA9` (TEST), exactly the same list named in
the old TypeScript fix -- had a live instance of the *same bug class* in
`engine.zig`: all 9 correctly branch on `s.op_size_ovr` for the register
read/write and immediate fetch (via `readEaxv`/`writeEaxv`/`fetchImm`), but
every one hardcoded `.w32` for the flags-width argument regardless of the
prefix. Found by contrast with `op85` (`TEST rmv, rv`), a sibling opcode
whose own comment already documents this exact mistake and fix ("was
hardcoded-32-bit read AND a hardcoded-32-bit flag width; fixing only the
read would still always report SF=0 for a correctly-16-bit-masked value")
-- that fix was never propagated to the 9 accumulator-immediate opcodes.
Effect: any `66 <op>` 16-bit form of these 9 gets SF wrong whenever the
true 16-bit result's sign bit (bit 15) is set, since `.w32` checks bit 31
instead, which is always 0 for a 16-bit-masked `u32`. Any `JS`/`JNS` (or
other SF-consuming logic) right after one of these was a live wrong-branch
risk.

Fixed all 9 in `cpu/src/engine.zig`, same one-line pattern as `op85`:
compute `const width: Width = if (s.op_size_ovr) .w16 else .w32;` and pass
`width` instead of the hardcoded `.w32` to `updateFlagsArithW`/
`updateFlagsLogicW`. Rebuilt `libcpu.so`.

Also closed a real test-coverage gap surfaced by this investigation:
`tests/unit/emulator/test_opcodes_arithmetic.py` had zero tests for true
8-bit AL-register opcodes (existing `imm8` tests were all the Group 1
sign-extended-imm8-into-32-bit-destination forms, `0x83`, not real 8-bit
ops) and zero tests for the `0x66`-prefixed 16-bit forms. Added two new
test classes: `TestByteWidthAccumulatorOps` (7 tests covering
`op04`/`op0C`/`op1C`/`op24`/`op2C`/`op3C`/`opA8`, asserting correct
low-byte result, upper-EAX-bits preserved, and SF read at bit 7 not bit 31
-- coverage, since these were already correct) and
`TestAccumImmediate16BitFlags` (9 tests, one per fixed opcode, each
producing `AX=0x8000` -- bit 15 set, bit 31 clear -- so SF only reads
correctly once flags are computed at the right width). Verified the 9
regression tests are real, not tautological: stashed just the
`engine.zig` fix, rebuilt `libcpu.so`, confirmed all 9 fail against the
pre-fix build with `assert False is True` on `SF_BIT`, then restored the
fix and rebuilt again. 609/609 tests passing (593 + 16 new) with the fix
in place.

## 2026-08-02 (later session) — repeated NVIDIA driver crash on exit fixed;
turned out to be a second, unrelated bug from the `EIP=0x15035655` MSJET35.DLL
SEH fault

What looked like "Python segfaulting while investigating an SEH error in
MSJET35.DLL" was actually two independent problems, only adjacent in the log:

1. The `EIP=0x15035655` unhandled SEH fault inside `MSJET35.DLL` (see
   "2026-08-02" entry above) — this is real and still **unresolved**, needs
   Ghidra decompilation. tew's own SEH dispatch (`dispatch_exception()` in
   `run_exe.py`) handled it exactly as designed: found no handler in the
   guest's SEH chain, printed the full halt diagnostic, and halted the CPU
   cleanly. No Python-level crash here — this is tew correctly reporting that
   the *guest* program would have crashed, entirely inside the emulator's own
   bounds-checked `ZigMemory`, never touching real host memory.

2. A **second, unrelated** crash immediately after, during process exit:
   `sys.exit()` -> `Py_Exit` -> libc `exit()` -> `__run_exit_handlers` ran an
   NVIDIA proprietary driver atexit callback against X11/GLX state that
   `window_manager.shutdown()`'s `SDL_Quit()` had already torn down two lines
   earlier. `coredumpctl` on the resulting core (`SIGABRT`, PID 258180,
   2026-08-02 16:36:25) showed the real chain: `libGLX_nvidia.so` ->
   `libnvidia-glcore.so` -> `xcb_present_select_input_checked` ->
   `xcb_send_request` -> `get_lazyreply` -> glibc's `_FORTIFY_SOURCE`
   `__chk_fail` -> `abort()`. Zero `libcpu.so` frames anywhere in the crashed
   thread's stack, confirming this fires well after emulation has fully
   stopped — unrelated in content to the MSJET35.DLL fault, just close to it
   in time. Same bug *class* as the earlier `libnvidia-rtcore.so` crash
   (2026-07-xx, fixed by adding `window_manager.shutdown()`) but a different
   NVIDIA subsystem (GLX, not Vulkan RT) hitting the same "driver atexit
   handler runs against an already-closed X11 connection" hazard.

Rather than keep chasing individual NVIDIA-subsystem atexit bugs one at a
time (rtcore, now GLX, possibly more later), replaced `sys.exit()` with
`os._exit()` at the very end of `run_exe.py` (after
`crt_state.window_manager.shutdown()`), with an explicit `sys.stdout.flush()`
first. `os._exit()` skips `exit()`/`__run_exit_handlers` entirely, so no C
library atexit handler -- NVIDIA's or anyone else's -- runs at all.
`logger.py`'s `_emit()` already does `print(line, flush=True)` on every line,
so no log output is at risk from skipping normal Python finalization.
593/593 tests still passing. Confirmed live: re-running the same scenario
that previously produced the `SIGABRT`/coredump now exits with code 0 and
`coredumpctl list` shows no new entry.

## 2026-08-02 — `oleaut32.dll` ordinal 201 (`SetErrorInfo`) implemented; new
native-crash halt found immediately after

Implemented `SetErrorInfo(dwReserved, perrinfo) -> HRESULT` (ordinal 201,
`oleaut32.dll`) in `oleaut32_handlers.py`: stores `perrinfo` as the calling
thread's current COM error object in a new per-thread
`CRTState.error_info_store` dict (keyed by `state.tls_current_thread_id()`,
matching the existing TLS pattern in the same file), releasing the previous
entry and AddRef'ing the new one via the same `_errinfo_release_core`/
`_errinfo_addref_core` helpers `IErrorInfo::Release`/`AddRef` already use —
this emulator has exactly one `IErrorInfo` implementation (`CreateErrorInfo`'s,
same file), so direct field manipulation is equivalent to a real vtable call,
matching this file's own established convention (`_errinfo_qi_core` already
does the same). Always returns S_OK per spec (MSDN: "This function always
returns S_OK"). 593/593 tests still passing.

Confirmed live: `SetErrorInfo(perrinfo=0x06fd2624) -> S_OK (tid=1012)` clears
the blocker, and execution continues into `MCity_d.exe`'s own code for the
first time past this point — a 6-frame-deep call chain purely in `exe`
addresses (frames at 0x0068adf2, 0x00a301a1, 0x00684de7, 0x006848ee,
0x004d8c71, 0x0068a7d5, 0x009fcaa6).

**New blocker found immediately after, same run**: at `EIP=0x00688c69` (in
`MCity_d.exe`), the run prints a "Halt Diagnostic" (`EAX=0xCCCCCCCC` —
MSVC debug-heap uninitialized-fill pattern; `ESP+0x0c` through `ESP+0x3c`
on the stack are also all `0xCCCCCCCC`) and then the underlying process
itself crashes — `timeout` reports "the monitored command dumped core"
rather than a clean Python exit. This means the fault handler that prints
"Halt Diagnostic" is not actually stopping the CPU cleanly; something after
it (native/Zig side, or Python touching freed state during shutdown) causes
a real SIGSEGV. Not yet diagnosed — no `[UNIMPLEMENTED]` log line appears
anywhere in this run, so this isn't a missing-handler halt; it's either a
genuine fault in game code operating on uninitialized memory, or an
emulator-side bug in whatever path leads to "Halt Diagnostic". Full log:
`/tmp/emu.log` (this session, categories `com,dll,loader,exception`,
`LOG_LEVEL=info`). Likely related to the already-queued "Decide/implement a
real unwind for `seh.py`'s unhandled-fault path instead of 'halt in place
with stale stack data'" item — worth checking that path first next session.

## 2026-08-02 (cont'd) — native-crash halt diagnosed: not a CPU/memory bug,
missing `WindowManager.shutdown()` call let the NVIDIA driver's own atexit
cleanup fault against a live SDL/Vulkan context

Investigated the `EIP=0x00688c69` core-dump crash from the entry above.
Checked SELinux first (per Molly's hunch it might be interfering): enforcing
mode confirmed via `sestatus`, but `journalctl` search for AVC denials across
the crash window came back empty — ruled out.

`coredumpctl info <pid>` on the crashed process (found via `coredumpctl
list`, a `SIGSEGV` in `/usr/bin/python3.14` matching the run's timestamp)
told the real story: the segfaulting thread's backtrace is
`Py_BytesMain → Py_RunMain → ... → handle_system_exit → Py_Exit → exit() →
__run_exit_handlers → SEGV in libnvidia-rtcore.so`. That's `sys.exit(1 if
cpu.faulted else 0)` at `run_exe.py:568` firing normally (a deliberate,
already-logged halt, not a crash) — the actual fault happens *afterward*,
inside libc's `__run_exit_handlers` calling into an `atexit`-registered
cleanup hook that NVIDIA's proprietary driver installs itself.
`libcpu.so` (the Zig CPU/memory core) appears in the loaded-modules list but
not in any crash thread's actual stack trace — confirms the CPU/memory
backend was completely uninvolved.

Root cause: `WindowManager.shutdown()` (`tew/api/window_manager.py`) already
existed and correctly destroys SDL2 textures/renderers/windows before
calling `SDL_Quit()` — but nothing in `run_exe.py` ever called it. Separately
(grepped the whole `tew/api/d3d8/` package), the Vulkan side has *no*
teardown path at all: `_state.py` tracks instance/device/swapchain/pipeline/
semaphores/etc., but the only `vkDestroy*` call anywhere in the codebase is
`vkDestroySwapchainKHR` (used for swapchain recreation, not shutdown) —
`vkDestroyInstance`/`vkDestroyDevice` don't exist anywhere. So `sys.exit()`
ran with both SDL2 and Vulkan still fully live, and the NVIDIA driver's own
"someone forgot to clean up" atexit safety-net handler ran against that live
GL/Vulkan context and segfaulted inside its own ray-tracing-core module. This
is the first session this project has ever reached far enough into real game
code to create a live SDL/Vulkan context and then exit while it's still
up — explains why this class of crash has never appeared before.

Fix: added one call, `crt_state.window_manager.shutdown()`, right before
`sys.exit()` in `run_exe.py`. Confirmed live (re-ran with `LOG_CATEGORIES`
including `window`): `[WindowManager] SDL2 shut down` now logs and the
process exits cleanly; `coredumpctl list --since="10 minutes ago"` shows no
new core dump. Vulkan itself still has no explicit teardown — SDL2's own
`SDL_Quit()` was apparently sufficient to stop the driver's atexit handler
from faulting this time, but that's relying on driver behavior, not a real
fix for the Vulkan side; queued as a follow-up (see status.md).

The original `EIP=0x00688c69` halt (`EAX=0xCCCCCCCC`, MSVC debug-heap
uninitialized-fill pattern, in `MCity_d.exe`'s own code) is still
undiagnosed on its own merits — the crash was just masking it from being
investigated. That's next session's actual current blocker.

## 2026-08-02 (cont'd, later) — logger was silently dropping the halt
reason itself; fix relocates the real blocker from 0x00688c69 to 0x15035655

Molly noticed the `EIP=0x00688c69` halt diagnostic gave register/stack
detail but never said *why* the CPU halted, and asked for more log detail.
Root cause, in `tew/logger.py`'s `_emit()`: `ERROR`-level messages were
still subject to `LOG_CATEGORIES` filtering, with only the `"exception"`
category specially exempted. But this project's own mandatory "halt loudly"
convention (CLAUDE.md) requires every halt/fault to log an `ERROR` right
before setting `cpu.halted`/`cpu.faulted` — so whichever category actually
carried the real reason (`seh`, `cpu`, `handlers`, etc.) got silently
dropped whenever a run's `LOG_CATEGORIES` didn't happen to include it, which
is exactly what happened in both of today's earlier runs
(`com,dll,loader,exception,window`). Fixed by exempting `ERROR` level from
category filtering the same way `"exception"` already was — one condition
added to `_emit()`. Checked for any `is_active()`-gated call sites that
might skip constructing an error message before it reaches `_emit()`
(would have defeated the fix) — none exist in the codebase. 593/593 tests
still pass.

Confirmed live: re-ran the *exact same* narrow `LOG_CATEGORIES` used in the
earlier "unexplained" run. Two previously-invisible lines now appear:
```
[ERROR] [seh] fault at 0x15035655 unhandled by SEH chain -- halting as before
[ERROR] [cpu] Fatal halt: fatal halt at EIP=0x00688c69
```
This **relocates the real blocker**. `EIP=0x00688c69` was never the fault
site — `run_exe.py`'s step loop (`fault_eip = cpu.eip` at the moment
`cpu.faulted` becomes true) shows the actual access violation happened at
`EIP=0x15035655`, inside `MSJET35.DLL` (`0x15000000-0x15ffffff`, offset
`0x35655`). That got dispatched through `dispatch_exception()` looking for
a handler in the game's own SEH chain, found none, and fell into the
already-queued "halt in place with stale stack data" gap in `seh.py` —
`0x00688c69` and its `EAX=0xCCCCCCCC` are what that stale state looked like
afterward, a real but downstream symptom, not the cause.

Also noted, not fixed this session: the Zig CPU core's fault path
(`cpu_zig.py` `run()`/`step()`, `_RUN_FAULTED` branch) only synthesizes
`"CPU fault at EIP=0x...opcode=0x..."` — it never surfaces the actual
faulting *memory address*, only EIP. Real Windows access violations carry
the accessed address; not having it here is a real gap, queued for a
Zig-side follow-up, but EIP alone (`0x15035655`) is sufficient to start the
next investigation in Ghidra.

---

## 2026-07-24 (cont'd) — bump-allocator ported to Zig; Zig/Python FFI
boundary consolidated into a kernel module; merged to main and pushed

Continuing the same day's incremental Zig-porting work: `CRTState.simple_alloc`'s
guest-heap bump-pointer arithmetic (`(current + size + 15) & ~15`, backing
`malloc`/`HeapAlloc`/`operator new` etc. across ~30 call sites) moved to a new
`cpu/src/alloc.zig`, exported as `bump_alloc_next(current, size) -> u32` and
called from a new `tew/hardware/alloc_zig.py` wrapper. Same split as the memory
port: the cursor itself and the `heap_alloc_sizes`/`heap_alloc_owner` bookkeeping
dicts (used by `realloc`/`HeapSize`/`HeapFree`) stay Python-owned; only the pure
pointer math moved. Still a bump allocator, no free list — scope kept minimal on
purpose.

While tracing how the CPU's own memory access works (investigating a "how do
Python and Zig actually talk to each other" question), found that `core.zig`'s
CpuState-bound `memRead8`/`memWrite8` and `memory.zig`'s Python-facing
`mem_read8`/`mem_write8` independently reimplemented the same bounds-check
arithmetic — two implementations of the same logic with no shared source of
truth. Went beyond a point-fix: designed and built a proper kernel-module
architecture for the whole Zig/Python FFI boundary (CPU + memory + alloc, not
just memory), in three separately-verified, separately-merged stages:

- **Stage 1** — new `cpu/src/primitives.zig`: `inBounds1`/`inBoundsWidth`/
  `readByte`/`writeByte`, the single shared implementation of "is this address
  in bounds, read/write the byte(s)." `core.zig`'s `memRead8`/`memWrite8` and
  `memory.zig`'s `mem_read8`/`mem_write8` both now delegate to it — zero
  file-layout change otherwise. `inBounds1` (single-byte, core.zig's original
  formula) and `inBoundsWidth` (combined-width, memory.zig's original formula)
  deliberately kept as two distinct functions, not unified into one: a 16-bit
  access straddling the end of memory behaves observably differently under
  each (per-byte-composed access keeps the first byte's real value before
  faulting on the second; a combined-width pre-check would reject both bytes
  up front), so collapsing them would have been a real behavior change, not a
  pure refactor.
- **Stage 2** — physically split `cpu.zig` (2006 lines, was doing double duty
  as both the internal execution engine and the entire Python-facing C ABI)
  into `cpu/src/kernel.zig` (new build root — every one of the project's 63
  `export fn`s, and only those, absorbing `memory.zig`'s 11 and `alloc.zig`'s
  1) and `cpu/src/engine.zig` (dispatch table, `cpuStep`, all opcode handlers
  — internal only, never exported, called from `kernel.zig`'s `cpu_run`).
  Because every export is now a top-level declaration directly in the build
  root, the `comptime { _ = &...; }` force-reference block (needed back when
  `memory.zig`/`alloc.zig`'s exports weren't naturally referenced from the old
  root and Zig's lazy per-declaration analysis silently dropped them from the
  compiled `.so`) is gone — the underlying problem it worked around no longer
  exists structurally, not just patched over. Fixed two live (not just
  commented-out) `@import("../cpu.zig")` sites in `history/capture.zig`'s test
  bodies that an earlier audit pass had missed. (Correction to the earlier
  audit: the true export count was always 63, not 53 — cpu.zig alone had 51
  exports, not 41; verified against `nm -D` throughout, so this was a
  documentation-only miscount, not a functional gap.)
- **Stage 3** — new `tew/hardware/_kernel_lib.py`: one shared `ctypes.CDLL`
  handle instead of three independent `dlopen` calls (`cpu_zig.py`,
  `memory_zig.py`, `alloc_zig.py` each used to call `ctypes.CDLL()` on the same
  path separately). No Zig changes; purely how the existing symbols get bound.

Each stage done on its own worktree/branch, verified independently (`zig build
test`, `nm -D` symbol-set diff against the pre-stage baseline, 593/593 pytest,
a live `run_exe.py` run diffed against the saved baseline on `[com]`/
`[exception]`/`[dll]`), then merged to `main` before the next stage started.

Also chased down an unrelated false alarm mid-session: a live run started
crashing immediately after `SDL2 initialized` with an `X_GLXCreateContext
BadValue` error, right after the memory/cpu.py work had been merged, which
briefly looked like a regression from that merge. Bisected the last 5 commits
on `main` (checking out each individually, rebuilding, running) — every single
one crashed identically, including a commit that predates the memory port
entirely, ruling out a code cause. Confirmed independently via plain `glxinfo`
(unrelated to this repo) failing the exact same way, and via `journalctl`
showing `kwin_wayland_wrapper` repeatedly logging `XCB error: BadWindow` for
the same stale resource ID at every crashed run's timestamp — a wedged
Xwayland/kwin compositor resource, most likely caused by the crashed runs
themselves never cleaning up their SDL/GL window on the abrupt X IO-error
exit. Logging out and back in cleared it; re-verified full clean runs on both
`main` and the in-progress worktree afterward with zero code changes.

All three stages merged to `main` and pushed. Final state re-verified end to
end on `main` itself (not just in the stage worktrees): `zig build test`
clean, 593/593 pytest, and three separate live `run_exe.py` runs (including
one requested purely "for peace of mind" after everything else was done) all
landing on the identical documented halt — same registers, same stack dump,
same `Final EIP: 0x00209402` — with byte-identical `[com]`/`[exception]`/
`[dll]` output versus the original saved baseline throughout.

---

## 2026-07-24 — hardware/memory.py ported to Zig; dead pure-Python CPU
class and entire emulator/opcodes package retired; merged to main and
pushed

`tew/hardware/memory.py` (flat bytearray-backed address space:
read8/16/32, write8/16/32, bounds checks) ported to a Zig-backed
`ZigMemory`, following the exact FFI pattern the earlier `cpu.zig`/
`ZigCPU` port established. New `cpu/src/memory.zig`: bounds-checked
`mem_read8/16/32`, `mem_write8/16/32`, `mem_load`,
`mem_is_valid_address/range` C-ABI functions operating on a
caller-owned `(ptr, size)` pair (no allocator, no ownership change —
Python's `bytearray` still owns the buffer, exactly as `ZigCPU` already
borrows it via `ctypes.from_buffer`). `tew/hardware/memory.py` is now a
12-line re-export shim (`Memory = ZigMemory`), mirroring how
`run_exe.py` already aliases `ZigCPU as CPU`. Along the way, closed two
latent gaps the old pure-Python `Memory` had and that `test_memory.py`
never actually covered: `read16`/`write16` had no explicit bounds
check (relied on `struct.error` leaking through instead of
`ValueError`), and `write8`/`write16` silently accepted negative
addresses via Python's list/struct wraparound semantics instead of
rejecting them.

Separately, investigated whether `tew/hardware/cpu.py` (the original
pure-Python CPU class, kept around post-Zig-port only for its
register/flag constants) was safe to delete. Found it was worse than
just unused: `run_exe.py` calls `register_all_opcodes(cpu)` on every
startup, which imports all 10 files under `tew/emulator/opcodes/`
(`data_movement.py`, `arithmetic.py`, `logic.py`, etc. — the entire
pure-Python x86 instruction-decode implementation) and registers every
opcode handler onto the CPU — but `ZigCPU.register()` (`cpu_zig.py`)
is a literal no-op (`def register(self, opcode, handler): pass`,
comment: "Zig handles all opcodes"), so every one of those
registrations was silently discarded on every run. Deleted
`tew/hardware/cpu.py` and the entire `tew/emulator/` tree (12 files,
~2,700 lines) outright. 38 files' register/flag-constant imports
(`EAX`, `ESP`, `CF_BIT`, etc.) redirected from `tew.hardware.cpu` to
`tew.hardware.cpu_zig`, which already defined byte-identical constants
(confirmed by diff before redirecting); the handful of
`TYPE_CHECKING`-only `CPU` type-hint imports now alias `ZigCPU`
instead — confirmed via full-repo sweep that every `CPU`-class import
was type-hint-only, zero live uses.

Verification for both changes: gitnexus's `IMPORTS`-edge query against
`tew.hardware.cpu` found the same 51 importing files as a manual grep
sweep — nothing missed. 593/593 tests pass throughout. Two full
DAO-reaching `run_exe.py` sessions (`LOG_CATEGORIES=com,dll,loader,
exception`, ~57s to the halt) — one after the memory port, one after
the cpu.py/opcodes retirement — produced **byte-identical** `[com]`,
`[exception]`, and `[dll]` log output versus the pre-change baseline:
same COM/CreateErrorInfo sequence, same halt at `EIP: 0x00209402`
(`SetErrorInfo`, still unimplemented — unchanged, expected), same
registers, same stack dump. Both changes done on isolated
branches/worktrees, merged into `main`, and pushed to `origin/main`
(`0fd35bd..90bc231`). Stale branches cleaned up afterward: 2 local
(fully merged) and 2 remote (`origin/combined`, `origin/nt-syscall-
layer` — both fully subsumed into `main`, zero unique commits)
deleted; `origin/renovate/configure` left alone (has 1 unmerged
commit, a real open item, not dead weight).

---

## 2026-07-23 (post-midnight session, cont'd 3) — LoadStringA, lstrcatA,
and CreateErrorInfo (oleaut32 ordinal 202) added; real RT_STRING resource
parsing added; per-DLL resource resolution wired through user32; one
CLAUDE.md handler-declaration violation found on retroactive audit and
fixed; new SetErrorInfo blocker

Continued straight on from the `patch_dll_iats` fix below.
`[UNIMPLEMENTED] user32.dll!LoadStringA` was next. Real `RT_STRING`
resource lookup added to `pe_resources.py`
(`PEResources.find_string(string_id)`, standard Win32 packing: block
`(id>>4)+1` holds 16 `[WORD length][length WCHARs]` entries, index
`id&0xF`). `register_user32_gdi32_handlers` (`user32_handlers.py`)
previously had no `dll_loader` access at all, so it could only ever
resolve resources for the main EXE's own hInstance — added the param
(threaded from `crt_handlers.py`, matching the pattern
`register_kernel32_handlers`/`register_oleaut32_ole32_handlers` already
used), plus a per-DLL-name `PEResources` cache
(`_resources_for_module`) so a real loaded DLL calling back into user32
for its own resources (MSJET35.DLL/MSJINT35.DLL here) gets its own
`.rsrc`, not the game's. `DLLLoader._find_dll_file` renamed to the
public `find_dll_file` (was already only used internally, one call
site) so this new code could re-locate a loaded DLL's file on disk.
Confirmed live: cleared the halt.

`[UNIMPLEMENTED] kernel32.dll!lstrcatA` was next — added next to
`lstrcpyA`/`lstrcpynA` in `kernel32_io.py`, real (unbounded) `strcat`
semantics. Confirmed live: cleared the halt.

`[UNIMPLEMENTED] oleaut32.dll!Ordinal #202` was next. Looked up against
the real `oleaut32.dll`'s export table
(`/data/Downloads/i386-binaries/oleaut32.dll`, same `EXEFile`/
`ExportTable` technique as the earlier `msjint35.dll` investigation):
ordinal 202 = `CreateErrorInfo(ICreateErrorInfo **pperrinfo)`. Implemented
in `oleaut32_handlers.py` as a real dual-interface COM object: one
allocated 0x2C-byte object with two vtables at a +4 offset
(`ICreateErrorInfo` at the object's own address, `IErrorInfo` at +4 —
the standard C++ multiple-inheritance "this-adjustor" split), a shared
refcount, `QueryInterface` on either face correctly switching between
them (and AddRef'ing on success, per COM contract), and real Set*/Get*
method bodies that actually read/write the object's fields — no fake
success anywhere.

**Process note, found via retroactive self-audit prompted by direct
user pushback ("CreateErrorInfo doesn't feel right" / "is S_OK
truthful?" / "empty was not on DAO's bingo card")**: this entire
session skipped `CLAUDE.md`'s mandatory HANDLER DECLARATION step
(state Function/Signature/Spec-says/We-deliver/Truthful-YES-NO in chat
*before* writing any handler) — every handler from `RegEnumKeyA`
onward. Ran the required grep audit
(`TODO|FIXME|FAKE|stub|not implemented|pass$|return None|return 0.*#|
return False.*#|return True.*#`) across every file touched this
session via `git diff`: no concealed fakery found (the `return None`
hits are all legitimate "not found" returns matching the existing
`find_dialog`/`find_bitmap` convention). One real violation found on
manual re-check against spec: `LoadStringA`'s `cchBufferMax == 0` path
(pointer-swap mode — caller wants `*(LPSTR*)lpBuffer` set to point at
the resource's own storage, no copy) was silently returning a
plausible-looking character count with `lpBuffer` left untouched
instead of honestly halting on a capability we can't back (no stable
address for a resource string that only exists as a materialized
Python `str`). Fixed: that path now halts loudly
(`[UNIMPLEMENTED] LoadStringA(cchBufferMax=0)`) instead of guessing.
Confirmed live this session that real callers never actually hit
`cchBufferMax==0` in practice, so the halt is inert, not a live-blocking
gap — but the *previous* silent-wrong-answer behavior was a genuine
spec violation regardless of whether it was ever exercised.

Added full logging (`logger.info("com", ...)`) to every method on the
new error-info object specifically to settle the "is this actually used
correctly" question with evidence rather than argument. Live-verified
the dual-interface design was load-bearing, not speculative: DAO's real
code calls `QueryInterface(IID_IErrorInfo)` on the object immediately
after creation (succeeds via the +4 face — the this-adjustor math is
correct), then fills it via the original `ICreateErrorInfo` pointer with
real content (`SetSource("DAO.DbEngine")`, a real help context ID, a
help-file pointer) before the very next call. Had the `IErrorInfo` face
not been implemented (i.e. `QueryInterface` honestly returning
`E_NOINTERFACE` for it, which was considered as the "simpler, more
defensible" option before this evidence), this exact real call sequence
would have diverged from genuine DAO behavior.

New halt, right after the error-info object is fully populated:
`[UNIMPLEMENTED] oleaut32.dll!Ordinal #201` (`SetErrorInfo`, same
export-table lookup technique). Not yet implemented, next up — a
HANDLER DECLARATION should be stated first this time, and what pointer
it actually receives (the `ICreateErrorInfo` face vs. the already-QI'd
`IErrorInfo` face) should be checked rather than assumed.

593/593 tests passing after every fix in this entry.

---

## 2026-07-23 (post-midnight session, cont'd 2) — GetFileInformationByHandle
and lstrcpynA added; real DLLLoader.patch_dll_iats bug found and fixed
(was clobbering real DLL-to-DLL calls with unimplemented-stubs); new
LoadStringA blocker

Continued straight on. `[UNIMPLEMENTED] kernel32.dll!GetFileInformationByHandle`
was next — added in `kernel32_io.py`: looks up the handle in
`state.file_handle_map`, `os.fstat`s the real fd (`os.stat`s `entry.path`
for read-only entries with no fd), fills a real `BY_HANDLE_FILE_INFORMATION`
(attributes, real `ctime`/`atime`/`mtime` → `FILETIME`, real size, link
count, inode as file index). Confirmed live.

`[UNIMPLEMENTED] kernel32.dll!lstrcpynA` was next — added next to the
existing `lstrcpyA`/`lstrlenA` in `kernel32_io.py` (bounded copy, always
null-terminates within `iMaxLength`). Confirmed live.

`[UNIMPLEMENTED] msjint35.dll!Ordinal #2` was next, and looked like
another missing-handler case, but direct offline inspection of the real
`msjint35.dll`'s export table (`tew`'s own `EXEFile`/`ExportTable`
parser, no emulator run needed) showed ordinal #2 (`CchLszOfId2`)
genuinely exists, and `DLLLoader.load_dll` had already resolved and
written its correct real address into the IAT. The actual bug: back in
`tew/loader/dll_loader.py`, `DLLLoader.patch_dll_iats` runs *after*
`load_dll` and unconditionally re-patches every secondary-DLL IAT entry
via `patch_iat_entry` — but never passed the already-known real address
through as `real_addr`, so any entry with no matching Python handler
fell straight to the unimplemented auto-stub fallback, silently
clobbering correct real-DLL-to-real-DLL calls (`msjet35.dll` calling
into `msjint35.dll`) with a fatal halt. This wasn't specific to this one
ordinal — it affected every secondary-DLL IAT entry across the whole
run. Fixed by having `patch_dll_iats` look up
`self._loaded_dlls[...].exports` and pass that through as `real_addr`;
added a `real_count` bucket to the "Patched X/Y ... (N auto-stubs)"
summary log so this class of bug shows up in the log instead of
silently inflating the auto-stub count. Confirmed live: MSJET35.DLL's
own final IAT patch pass went from 23 auto-stubs/0 real (every prior
session) to 1 auto-stub/7 real. 593/593 tests passing after all three
fixes.

New halt, past the entire `msjint35.dll` call: `[UNIMPLEMENTED]
user32.dll!LoadStringA` — a genuine missing Win32 API this time, not a
loader bug. Not yet implemented, next up.

---

## 2026-07-23 (post-midnight session, cont'd) — GetTempPathA and
GetTempFileNameA added; execution now three levels deep inside
MSJET35.DLL, new GetFileInformationByHandle blocker

Continued straight on from the `RegEnumKeyA` fix below. Next halt:
`[UNIMPLEMENTED] kernel32.dll!GetTempPathA`, right after
`RegOpenKeyA("...ISAM Formats")`. Added in `kernel32_io.py`, modeled on
`GetCurrentDirectoryA`'s buffer-sizing contract (returns required size
including null terminator when the caller's buffer is too small).
Returns `C:\WINDOWS\TEMP\`; created the backing host directory
(`~/.emu32/WINDOWS/TEMP/`) so real file creation against that path
actually lands somewhere on disk, consistent with `~/.emu32/WINDOWS/`
already being a live `DLLLoader`/path-translation root.

Very next halt: `[UNIMPLEMENTED] kernel32.dll!GetTempFileNameA` — Jet
turning that temp path into an actual scratch filename. Added in
`kernel32_io.py`: builds `<path><3-char prefix><4 hex digits>.TMP`
(hex digits from `uUnique` if nonzero, else an internal incrementing
counter), and when `uUnique == 0` — the common "reserve me a unique
name" case — actually creates the 0-byte file on the host filesystem,
reusing the same `os.open(path, O_WRONLY|O_CREAT|O_TRUNC)` +
`os.makedirs` pattern `CreateFileA`'s writable-open branch already uses
(via `state.open_file_handle`), just called directly since
`GetTempFileNameA` returns a UINT, not a handle, and closes the fd
immediately rather than leaving an untracked handle open. This matters
because it's genuine real-file-creation, not just a string return: any
later real `CreateFileA`/`ReadFile` Jet does against this exact
filename will find a real file.

Both fixes confirmed live in the same run. 593/593 tests passing after
each. New halt, one level further than any previous session:
`[UNIMPLEMENTED] kernel32.dll!GetFileInformationByHandle`, called
immediately after the temp file is created — not yet implemented, next
up.

---

## 2026-07-23 (post-midnight session) — RegEnumKeyA added; execution now
two levels deep inside MSJET35.DLL, new GetTempPathA blocker

Picked up from the late-night session's fresh halt:
`[UNIMPLEMENTED] advapi32.dll!RegEnumKeyA`. Root cause: only `RegEnumKeyExA`
had ever been implemented in `advapi32_handlers.py` — the older, simpler
`RegEnumKeyA` (4 args, `cchName` passed by value rather than by pointer to
a DWORD, no class/last-write-time output) was never added. Added it,
sharing a new `_reg_list_subkeys()` helper factored out of
`RegEnumKeyExA`'s subkey-derivation logic (was duplicated inline before).

Live-verified: execution now runs cleanly through the entire
`HKLM\Software\Microsoft\Jet\3.5\Engines` subkey enumeration and all of
`Engines\ODBC`'s config-value reads — all honest `NOT FOUND`s (no
`registry.json` seeding needed; Jet tolerates the empty tree same as it
tolerated the earlier `IsTNT`/`GetProcessAffinityMask` misses). 593/593
tests still passing.

New blocker, one level further than any previous session: right after
opening `HKLM\Software\Microsoft\Jet\3.5\ISAM Formats`,
`[UNIMPLEMENTED] kernel32.dll!GetTempPathA` halts cleanly. Not yet
implemented — next up.

---

## 2026-07-23 (late-night session) — LoadLibraryA fake-success bug and
GetProcAddress ordinal-format bug fixed; real Jet 3.5 engine DLLs sourced
and now genuinely executing

Continued from the night session's DAO COM completion. The next blocker —
`GetProcAddress("msjter35.dll", "ordinal#2")` looping 7,005 times with zero
progress, burning the full step budget — turned out to be two independent,
real bugs, not one:

1. **`LoadLibraryA` fake-success bug** (`kernel32_handlers.py`,
   `_load_dll_by_name`/`_load_dll_by_path`). The fallback for any DLL name
   not found on disk unconditionally fabricated a stable, non-NULL, hash-
   based fake handle (`_fake_dll_handle`) — even for DLLs with zero handler
   coverage of any kind. `GetModuleHandleA`'s equivalent resolution path
   (`_resolve_module_handle`) already did this correctly: fake-succeed only
   if `stubs.get_stub_dll_handle()` finds real handler coverage, otherwise
   return NULL. `LoadLibraryA` now matches that same rule. This alone
   stopped the busy-loop: DAO saw an honest `LoadLibraryA` failure for
   `msjet35.dll`/`msjter35.dll` and moved on instead of spinning. Dead
   `_fake_dll_handle()` helper removed (no longer referenced anywhere).

2. **`GetProcAddress` ordinal-format bug** (`kernel32_handlers.py`,
   `_get_proc_address`). Ordinal-only lookups built the key as
   `f"ordinal#{name_ptr}"` (lowercase, no space) — but every other place in
   the codebase that registers or parses an ordinal export uses
   `f"Ordinal #{n}"` (capital O, space): `dll_loader.py`'s real PE
   export-table parsing, `oleaut32_handlers.py`'s `_ole_ord`,
   `wsock32_handlers.py`, `dsound_handlers.py`. This is a separate,
   independently-real bug from #1 — it means runtime `GetProcAddress`
   ordinal lookups against a genuinely-loaded real DLL's export table (or
   against a Python-registered ordinal-alias handler) could never succeed,
   full stop, regardless of whether the DLL/ordinal actually exists. Fixed
   to build `f"Ordinal #{name_ptr}"`, matching everywhere else. (The
   earlier oleaut32.dll ordinal #4/#15/#21 fixes this session-chain
   depended on a *different* code path — static IAT resolution at DLL-load
   time, not this runtime `GetProcAddress` handler — so they were unaffected
   by this bug and didn't reveal it.)

With both fixed, `msjter35.dll`'s (and `msjet35.dll`'s) real files were
still needed — Prompted by Molly asking "we will be using an access jet3
database... so um....." after the busy-loop diagnosis, which reframed
"honestly fail, DLL not installed" as insufficient for the actual use case
(the game needs working Jet reads/writes, not just DAO politely giving up).
Found the real Microsoft Jet 3.5 Database Engine redistributable already on
this host: `~/.emu32/DBInst/DAO/data1.cab` (an InstallShield cabinet,
extracted with `unshield`) — the *same* install package `dao350.dll` was
originally sourced from (confirmed: extracted `dao350.dll` sha256-matches
the one already deployed byte-for-byte). The cabinet also contains
`MSJET35.DLL` (core Jet engine), `msjter35.dll` (Jet error-message
resource), `MSJINT35.DLL` (Jet international/collation), `VBAJET32.DLL`,
`msrd2x35.dll` (Jet Red ISAM driver), `EXPSRV.DLL` (Jet expression
service), and `MSVCRT40.DLL` (a bonus fix — this was DAO350.DLL's own
previously-unresolved static import). All extracted and placed at
`~/.emu32/WINDOWS/System32/`, which was already a registered DLL search
path (added by `oleaut32_handlers.py` for the `dao350.dll` COM-server
lookup, but that search path was never gated to COM servers only — it's
generic, so the new Jet DLLs are discoverable with no code changes beyond
the two bug fixes above).

Live-verified end to end: `MSJET35.DLL` now loads for real (166 exports,
base `0x15000000`), `GetProcAddress` resolves its real exports correctly,
and execution genuinely runs *inside* `MSJET35.DLL`'s own x86 code for the
first time (EBP chain shows `MSJET35.DLL+0x36db2`) — a full level deeper
than DAO's own code, which is as far as every previous session got. Two
small non-blocking gaps surfaced along the way (`kernel32.dll!IsTNT`,
`kernel32.dll!GetProcessAffinityMask` — both return NULL harmlessly, Jet
handles the miss fine and keeps going) before hitting the new, clean,
single-shot current blocker: `[UNIMPLEMENTED] advapi32.dll!RegEnumKeyA`,
almost certainly Jet 3.5 enumerating its own ISAM/engine registration under
`HKLM\Software\Microsoft\Jet\3.5\Engines` during initialization.

593/593 tests passing throughout (reconfirmed after each fix).

## 2026-07-23 (night session) — oleaut32.dll ordinal #4, lstrcmpW, GlobalLock/
GlobalUnlock: DAO's entire COM activation chain now completes and returns
to the game's own code

Continued straight from the evening session's fatal_halt exception work,
live-verifying against the real `dao350.dll` after each fix:

1. **`oleaut32.dll` ordinal #4** (`SysAllocStringLen`) — same missing-
   ordinal-alias shape as #15/#21 fixed earlier today; aliased to the
   existing named-export handler, no new logic
   (`tew/api/oleaut32_handlers.py`).

With that fixed, DAO's `CoGetClassObject` ×2 and `CoCreateInstance` all
complete with real, non-fake results (`hr=0x80040112`, matching every
prior run — no regression), and for the first time execution genuinely
**returns to `MCity_d.exe`'s own code** (EBP chain frames show `← exe`
instead of `← DAO350.DLL`) — the "ole32 block" that motivated this whole
multi-day investigation is fully cleared.

Two more gaps found immediately after, both in the game's own code path
rather than DAO internals:

2. **`kernel32.dll!lstrcmpW`** — real UTF-16 word-by-word comparison,
   standard strcmp-style sign semantics (`tew/api/kernel32_io.py`,
   alongside the existing `lstrlenA`/`lstrcpyA`).
3. **`kernel32.dll!GlobalLock`/`GlobalUnlock`** — implemented as
   pass-through no-ops. Correct per real Windows' own documented behavior
   for fixed (non-moveable) memory, which is all this emulator's
   `GlobalAlloc` has ever handed out (a direct pointer via
   `state.simple_alloc`, no true `HGLOBAL` indirection) — `GlobalUnlock`
   added proactively alongside `GlobalLock` since real callers always pair
   them and it's equally trivial.

593/593 tests passing throughout (no new tests this session — all three
fixes are simple enough that live verification against real `dao350.dll`
was judged sufficient, consistent with how ordinals #15/#21/#4 were
handled).

**New blocker found, a genuinely different shape than every fix above**:
right after `CoCreateInstance` succeeds, DAO starts probing for the Jet
database engine DLL and locks onto
`GetProcAddress("msjter35.dll", "ordinal#2") -> NULL` — repeated 7,005
times verbatim with zero progress, burning the full 500,000,000-step
budget without ever halting. Not a missing-API halt; a genuine busy-loop.
`msjter35.dll` was flagged in status.md months ago as "referenced by
`dao350.dll`, never analyzed, never reached" — now actually reached, and
it hangs rather than failing cleanly. Not yet investigated. See status.md
"Current status"/queued issues for the specific open questions.

---

## 2026-07-23 (evening session) — `_invoke_emulated_proc`'s "didn't complete"
`0`-return sentinel replaced with a raised exception

Implemented the design agreed earlier the same day (see the previous
entry's "sentinel collision" discussion, and the full design that was
saved to status.md pending a prerequisite task): `cpu.fatal_halt` newly
becoming true during a call now makes `CPU.run()`/`CPU.step()`
(`tew/hardware/cpu_zig.py`) raise a new `FatalHaltError` instead of just
returning, so a fatally-halted nested call can no longer be silently
misread as a real return value downstream.

`_lib.cpu_run` (the actual FFI call into Zig) has exactly two callers in
the whole codebase, both inside `CPU.run()`/`CPU.step()` — every other
caller (`_invoke_emulated_proc`, `seh.py`'s `_invoke_handler`, the
top-level loop in `run_exe.py`) goes through those, never touches Zig
directly, so that's the one chokepoint the check needed adding to instead
of the 94 scattered `cpu.halted = True` call sites. The exact condition:
capture `was_fatal = self.fatal_halt` before calling `_lib.cpu_run`, raise
only if it's newly `True` afterward. This distinction matters and is
directly tested: an *already*-fatally-halted CPU at entry must stay a
silent no-op (the existing `test_cpu_zig_fatal_halt.py` tests from the
morning session already assert exactly this and would have caught a naive
"raise whenever fatal_halt is true" implementation). Deliberately **not**
raised for plain `cpu.faulted` — that stays a polled, SEH-recoverable
flag; `run_exe.py`'s main loop still gives it to `seh.py`'s
`dispatch_exception` for a real recovery attempt before giving up.

`_invoke_emulated_proc` (`user32_handlers.py`) needed no new logic, only
deletions: its `if cpu.fatal_halt: break` loop-exit and the matching
`elif cpu.fatal_halt and ...` branch in the "not genuinely_completed"
reporting are now unreachable (the exception already leaves the function
before either could run) and were removed, along with the now-always-true
`if not cpu.fatal_halt:` guard on the final `cpu.halted = False` cleanup.
`seh.py` and `_c_int_dispatch` needed zero changes -- reasoned through
ahead of time and confirmed live: Win32 handlers run as native ctypes
callbacks, which must catch any Python exception before it crosses back
into C, so when the exception escapes a handler mid-callback it's caught
there and discarded same as always; that's harmless because `fatal_halt`
is already permanently set by then, and the *next* `cpu.run()`/`cpu.step()`
call anywhere further up the (non-callback) Python stack sees it and
raises again, re-escaping at each ctypes boundary until it reaches
ordinary Python. `run_exe.py`'s top-level loop is the single real `except
FatalHaltError` for the whole program -- catches it, logs a one-line note,
and falls through into the existing (unchanged) watchpoint/faulted/halted
post-run reporting, since `cpu.halted` is guaranteed true by then and that
code already does the right thing.

Added 5 new tests: `CPU.run()`/`CPU.step()` raise when fatal_halt newly
fires mid-call (via a real `INT 0xFE` dispatch, not a mock), an
already-halted CPU stays a no-op, and a new
`test_invoke_emulated_proc_fatal_halt.py` exercising the full propagation
path end-to-end (a nested call hitting a fake unimplemented handler now
raises out of `_invoke_emulated_proc` instead of returning `0`). 593/593
tests passing.

Live-verified against the real `oleaut32.dll!Ordinal #4` blocker
(`SysAllocStringLen`, still unfixed, next up): the halt now surfaces as
`[UNIMPLEMENTED] oleaut32.dll!Ordinal #4 — halting` → `Fatal halt: fatal
halt at EIP=0x00209022` → one clean `Halt Diagnostic`, no duplication, run
exits immediately. Also reconfirmed the `CoGetMalloc`/ordinal-15/21 fixes
from earlier today still produce genuine non-NULL `*ppv` and a real
`hr=0x80040112` all the way up to this blocker -- no regression introduced
by this refactor.

---

## 2026-07-23 (later session) — CoGetMalloc/IMalloc implemented; DAO `*ppv`
NULL mystery resolved as a side effect

Picked up the top-priority queued item from earlier today: `ole32.dll!
CoGetMalloc` had no handler at all, and `dao350.dll`'s `DllGetClassObject`
helper-object init calls it as its very first real dependency. Implemented
in `tew/api/oleaut32_handlers.py` as a lazily-allocated singleton `IMalloc`
COM object (real COM vtable dispatch via the same `register_handler`/
`get_handler_address` trampoline mechanism D3D8's fake COM objects use, not
a Python-only shortcut) — `AddRef`'d on every `CoGetMalloc` call like real
Windows, with `Alloc`/`Realloc`/`Free`/`GetSize`/`DidAlloc` sharing
`state.simple_alloc` + `heap_alloc_sizes` bookkeeping with `HeapAlloc`/
`HeapFree`/`HeapSize` (`kernel32_memory.py`) so answers are consistent
across both allocators rather than a second, disagreeing bump heap.

Live-verifying this (per the emu32 skill's tee-once-grep-many workflow, one
short focused run per fix) surfaced two more, smaller gaps in the same
`DllGetClassObject` call path, both the identical shape — a named
`oleaut32.dll` export already implemented, but no ordinal alias registered
for it, so DAO's ordinal-only imports hit the auto-generated
`[UNIMPLEMENTED] ... — halting` stub instead:
- **Ordinal #15** (`SafeArrayCreate`) — aliased to the existing
  `_SafeArrayCreate` handler, no new logic.
- **Ordinal #21** (`SafeArrayLock`) — implemented as a no-op returning
  `S_OK`, following the precedent `SafeArrayUnaccessData` already set two
  lines above it (this emulator tracks no lock count anywhere).

With all three fixes in place, live-verified: `DllGetClassObject` now
genuinely completes its real `QueryInterface` call and writes `*ppv` for
real (`MOV [ECX],EAX (*ppv=this)` at `0x0447d458`, non-NULL), across three
separate `CoGetClassObject`/`CoCreateInstance` calls for different riids —
**resolving the `*ppv`-stays-NULL mystery** that the 2026-07-19/21
investigation (see status.md "Background") had statically diagnosed but
left blocked on `tid=1012`'s premature death (fixed 2026-07-21) and then on
`CoGetMalloc` itself. Root cause confirmed exactly as the 2026-07-22 entry
below predicted: `dao350.dll`'s helper-object init chain was aborting on
missing dependencies before ever reaching its own (statically-verified-
correct) `QueryInterface` code, and `_invoke_emulated_proc`'s bare-`0`-on-
abort sentinel was indistinguishable from a genuine `S_OK`, disguising the
abort as a clean "success with NULL `*ppv`" — not a bug in DAO's own code
at all, as suspected.

Next real blocker, immediately after: `oleaut32.dll!Ordinal #4`
(`SysAllocStringLen`) — almost certainly the same missing-ordinal-alias
shape as #15/#21 above; not yet fixed. Also noticed but not fixed: two
leftover debug logpoint callbacks from the earlier investigation
(`_log_dao_tlssetvalue_call`, `_log_dao_init_shortcut_check` in
`run_exe.py`) now throw on every call (`AttributeError: 'LP_c_ubyte' object
has no attribute 'read32'`, silently swallowed by ctypes) — harmless but
noisy, low priority. See status.md "Current status" for full detail.

589/589 tests still passing throughout (no test changes this session — all
three fixes verified live against the real `dao350.dll` binary rather than
via new unit tests, consistent with this investigation's existing
verification style).

Follow-up cleanup: removed all five now-resolved TEMP diagnostic logpoints
from `run_exe.py` (`_log_dgco_entry`/`_log_dgco_call_queryinterface`/
`_log_dgco_call_release`/`_log_qi_ppv_write` for the `*ppv` investigation
just closed above; `_log_dao_init_shortcut_check`/`_log_dao_tlssetvalue_call`
for the superseded `TlsSetValue`-skip hypothesis; `_log_isdbcs_call` for the
`tid=1012` investigation resolved 2026-07-21) — each was explicitly marked
"TEMP -- discard once root-caused/confirmed" in its own comment, and the two
`_log_dao_*` ones had gone stale, throwing `AttributeError: 'LP_c_ubyte'
object has no attribute 'read32'` on every call. Also dropped the
now-unused `EAX`/`ECX`/`ESI` register-constant imports these left behind.
589/589 tests still passing.

---

## 2026-07-23 — cpu.fatal_halt is now a real, unclearable native CPU lockup

Picked up directly from the previous session's discovery that execution
visibly continued past the `CoGetMalloc` fatal halt to a later, unrelated
halt seconds afterward, instead of stopping immediately. Root-caused:
`ZigCPU.faulted`'s setter (`tew/hardware/cpu_zig.py`) called the native
`cpu_clear_halted` (`cpu/src/cpu.zig`) unconditionally whenever `cpu.faulted`
was cleared back to `False` -- the emulator's SEH-resume path does exactly
this after deciding a CPU fault was "handled." `cpu_clear_halted` cleared
native `s.halted` regardless of `fatal_halt`, desyncing it from the
Python-side sticky flags every other check in the codebase reads (every
*other* halt-clearing call site was already guarded with
`if not cpu.fatal_halt:` -- confirmed via grep this was the one unguarded
site). `cpu_run`'s own per-instruction loop obeys native `s.halted`, not the
Python property, so later `cpu.run()` calls (notably
`_invoke_emulated_proc`'s polling loop, which calls `cpu.run()` before
re-checking halted state) genuinely executed more real instructions.

Fixed at the CPU/Zig layer rather than with another Python-level guard --
discussed at length and a deliberate choice, not the default one. `fatal_halt`
has no real x86 analog (there's no hardware concept of "an unimplemented
Win32 API"), but this emulator models exactly one physical core, so once it
fires, nothing should be able to hand the core to a different thread as if
it were merely idling -- scattered Python-level `if not cpu.fatal_halt:`
checks before every halt-clearing call site is exactly the pattern that let
this bug through in the first place, and a Plan agent's alternative
recommendation (a pure-Python guard on the `faulted` setter, matching that
same idiom) was explicitly overridden for the same reason.

Added a new native `fatal_halted` field on `CpuState` (`cpu/src/core.zig`),
a dedicated `cpu_set_fatal_halt`/`cpu_is_fatal_halted` pair with deliberately
no clear path, made `cpu_clear_halted` and `cpu_run`'s loop condition respect
it, and made every register/eflags/FPU setter (`cpu_set_reg`, `cpu_set_eip`,
`cpu_set_eflags`, `cpu_fpu_set*`) refuse to write once set -- reads stay
completely open, so diagnostics can still inspect the frozen state. This
also neutralizes a second bug found along the way: `_invoke_emulated_proc`'s
cleanup path unconditionally calls `cpu.restore_state(saved)` even when
fatally halted, which would silently overwrite the real failure-point EIP/
registers with their pre-nested-call values before any diagnostic saw them;
that write is now just a no-op. `tew/kernel/scheduler.py`'s `preempt_slice`
and the four other CPU-state-mutating entry points
(`block_current_on_cs`/`block_current_on_handles`/`sleep_current`/
`mark_current_dead`) now also refuse to act once fatally halted, for
defense-in-depth consistency, though none were concretely reachable
post-fatal-halt once the native fix landed.

Live-verified against the exact `CoGetMalloc` scenario from the previous
session: the run now stops dead at
`[UNIMPLEMENTED] ole32.dll!CoGetMalloc — halting` with zero further
scheduler activity (`grep`-confirmed no `switch:`/`[alive]` lines follow),
and the Halt Diagnostic shows the full, accurate 7-frame call chain at the
true failure point (`DllGetClassObject` → `_invoke_emulated_proc`'s own
sentinel → `Dbcode_InitDao` → `DBThreadCpp` → thread wrapper →
`THREAD_SENTINEL`) instead of the old shallow, unrelated, later trace.

Added `tests/unit/hardware/test_cpu_zig_fatal_halt.py` (real `ZigCPU`, no
mocks -- the original bug was a native-state desync a mocked CPU can't
reproduce) and a `TestPreemptSlice` class in `test_scheduler.py`. Verified
both fail against a rebuilt pre-fix `libcpu.so` and pass against the fix
(stashed the Zig/Python changes, rebuilt, confirmed 4 failures; restored,
rebuilt, confirmed all pass). 589/589 tests passing.

Also documented (not yet fixed, deferred, unrelated to the above): while
figuring out what "real CPU" this should match, traced `cpu/src/two_byte.zig`'s
`CPUID` handler against actual Intel datasheets (Pentium Pro's feature table,
Pentium II OverDrive's CPUID table) and found it doesn't self-consistently
identify as any real chip. Since MMX is a hard requirement and Pentium Pro's
own documented feature set has no MMX bit at all, the correct target is real
Pentium II (Family 6, Model 3 "Klamath" or Model 5 "Deschutes") -- reconciles
with `memory/status.md`'s existing "Pentium II instruction set" header, which
was already right; only the actual `CPUID` value (currently `0x00000600`,
should be `0x00000630`/`0x00000650`) and the "source of truth" reference
(currently the unrelated 80386 manual) need correcting. Blocked on locating
the exact Pentium II spec manual to confirm Model/Stepping.

## 2026-07-22 (later session) — Root-caused the *ppv NULL mystery:
CoGetMalloc is unimplemented; found a related hr/S_OK sentinel collision
along the way

Continued straight from the same session's IsDBCSLeadByte fix, which
unblocked `DllGetClassObject`. Of the four existing TEMP logpoints on
`DllGetClassObject`'s internals, only the entry one fired -- meaning the
real code takes an early-exit path before ever reaching the
`QueryInterface`/helper-construction code the previous session's static
analysis had reviewed (and found calling-convention-correct). That analysis
was accurate; it just wasn't the code actually being hit.

Traced further into `dao350.dll`'s real functions (`debug_clean` Ghidra
project): `DllGetClassObject`'s helper-object allocator (`FUN_0447d31e`)
calls `FUN_044947fc` for per-thread init, which needs a per-thread
"task memory" arena stored via `TlsSetValue`. Two rounds of manually
computed instruction offsets for logpoints (guessing which stack slot held
which decompiled variable) both missed -- Ghidra's decompile of this DLL
has already been shown misleading twice before in this investigation, and
manual offset math compounded it a third time. Switched to logging simple
function-entry points instead of guessed mid-function offsets, which
immediately showed the real problem: `FUN_044947fc` calls `CoGetMalloc`
(`ole32.dll`) as its very first real dependency, and tew has no handler for
it at all -- `[UNIMPLEMENTED] ole32.dll!CoGetMalloc — halting` fires
immediately, long before `TlsSetValue` or the "already initialized"
shortcut check (the branch two rounds of guessed logpoints were aimed at)
are ever reached. The whole arena-init chain aborts at this first
dependency; `FUN_0447d31e` returns NULL; `DllGetClassObject` takes its
`local_c == NULL` branch, skipping `QueryInterface`/`*ppv` writes entirely.

Found a second, structurally separate bug while confirming this:
`hr=0x00000000` in the `CoGetClassObject(...) -> hr=0x00000000
*ppv=0x00000000` log line isn't `DllGetClassObject` genuinely returning
`S_OK` -- `_invoke_emulated_proc` returns a bare `0` whenever a nested call
doesn't complete (fatal halt, dead thread, `max_steps` exhausted), which is
a safe, conservative sentinel for `DllMain`-style callers (`0` = FALSE) but
collides with `S_OK` for any `HRESULT`-returning nested call
(`_call_dll_get_class_object`, `oleaut32_handlers.py`, is the one hit here,
but the same collision would affect any future `HRESULT` nested-call site).
Not fixed yet -- flagged in status.md as its own queued item.

Also surfaced, not yet root-caused: `cpu.fatal_halt` is documented
everywhere as "the whole emulator must stop" and is never cleared anywhere
in the codebase, yet the run visibly continued past `CoGetMalloc`'s fatal
halt to a later, unrelated halt rather than stopping immediately. Queued as
the new top priority.

## 2026-07-22 — Implemented IsDBCSLeadByte properly (codepage-derived);
DllMain now completes for real; DllGetClassObject/*ppv NULL mystery
reproducing again with new evidence

Picked up right where the previous session left off: `IsDBCSLeadByte` was
the concrete cause of `tid=1012`'s death, confirmed by the fatal-halt fix
landing exactly there (`[UNIMPLEMENTED] kernel32.dll!IsDBCSLeadByte —
halting`). Rather than a bare always-FALSE stub, checked whether that
answer is actually *true* for this game rather than merely convenient: tew
already hardcodes `GetACP() -> 1252` (Western/Latin) everywhere, and
`GetCPInfo`'s `LeadByte[]` table was already written as all-zero — so
"no lead bytes" isn't a new assumption, it's what tew's own environment
already asserts. Also confirmed MCity_d.exe itself never imports
`IsDBCSLeadByte` at all (string search of the whole exe found zero
matches) — only `dao350.dll` does. Separately confirmed `dao350.dll`
references `MSJET35.DLL` (the real Jet 3.5 engine) as a DLL it loads
dynamically by name -- Jet's own code hasn't been analyzed or reached by
the emulator at all, so no DAO-specific guarantee is made about it;
instead, implemented `IsDBCSLeadByte` (`tew/api/kernel32_locale.py`) so
`GetACP`/`GetCPInfo`/`IsDBCSLeadByte` all derive from one `ANSI_CODEPAGE`
constant and a shared `_DBCS_LEAD_BYTE_RANGES` table (covering
932/936/949/950 too, not just 1252) -- correct by construction for
whichever of these three APIs any future caller (Jet included) happens to
use, rather than three independently-stubbed constants that could drift
out of sync.

Live-verified: all 256 `IsDBCSLeadByte` loop iterations now complete (was
1 of 256 before the fix), `DllMain(DLL_PROCESS_ATTACH) -> 1` (correct,
matching the real decompiled `entry()` function), and the run reaches
`DllGetClassObject` for real -- `CoGetClassObject(...) -> hr=0x00000000
*ppv=0x00000000`, reproducing the `*ppv` NULL mystery documented in the
previous session, now unblocked.

New evidence while there: of the four existing TEMP logpoints
(`_log_dgco_entry`, `_log_dgco_call_queryinterface`, `_log_dgco_call_release`,
`_log_qi_ppv_write`, all in `run_exe.py`), only the entry logpoint fired.
Neither the `QueryInterface` CALL site nor the `*ppv`-write site fired at
all -- meaning `DllGetClassObject`'s real code takes an early-exit path
*before* reaching the helper-object construction/`QueryInterface` call
the earlier session's static analysis reviewed (and found calling-
convention-correct). That earlier analysis may be entirely accurate for
code that's simply never reached. 583/583 tests passing.

## 2026-07-21 (later session) — Root-caused and fixed tid=1012's premature
death: an unmatched secondary-DLL import (IsDBCSLeadByte) silently executed
garbage instead of halting

Picked up where the previous session's `DllMain` fix left off: `tid=1012`
still died mid-call on every run, blocking DAO activation. Added a
thread-end stack dump (`diagnose_thread_end`, `tew/kernel/
exception_diagnostics.py`, fired from `_make_thread_return_handler` in
`crt_handlers.py`) gated to `tid=1012` to see what the stack actually looked
like at the moment of "death." It showed the nested-call sentinel
`_invoke_emulated_proc` had pushed for the `DllMain` call, and every real
frame above it (`Dbcode_InitDao` → `DBThreadCpp` → `DBThread` →
`lpStartAddress_009fc3a0`, confirmed via Ghidra decompilation of MCity_d.exe),
all still sitting untouched in memory -- proving something jumped straight
past all of them without ever executing a matching `RET`, and ruling out an
SEH/C++ `terminate()` explanation (no `[seh]` log activity anywhere in the
run, and none of `Dbcode_InitDao`'s own error-print branches ever fired --
its real code never got control back at all).

Traced into `dao350.dll` itself (project `debug_clean` in Ghidra, not
`mcity`): `DllMain`'s real code (`0x04479f74`) is trivial and safe --
`InitializeCriticalSection` x2, `TlsAlloc`, then a 256-iteration loop
calling `IsDBCSLeadByte` through a cached register (`FUN_044c63fc`, raw
bytes confirmed `MOV ESI,[0x04471074]` once before the loop, `CALL ESI`
each iteration). tew has no handler registered for `IsDBCSLeadByte`
anywhere. A one-shot logpoint at the `CALL ESI` site (`0x044c6410`)
confirmed it: only 1 of the expected 256 calls fired, with
`ESI=0x000735ba` -- not a valid code address in any known range.

Root cause: `patch_dll_iats` (`tew/loader/dll_loader.py`), the IAT-patching
path used for secondary DLLs loaded at runtime (unlike `write_iat_handlers`
for the main EXE), had no fallback for an unmatched import -- it silently
left the IAT slot holding whatever raw, unrelocated bytes were in the DLL
file on disk. Since tew's memory (`tew/hardware/memory.py`) is an
unprotected flat `bytearray`, `CALL ESI` into that garbage doesn't fault the
way it would on real Windows; it just silently executes whatever's there
(almost certainly zero bytes), with no halt, no SEH activity, and no log
line, until it wanders far enough to land on `THREAD_SENTINEL` by
coincidence -- exactly matching every symptom observed.

**Fix**: unified IAT-patching into one shared function, `patch_iat_entry`
(`tew/loader/dll_loader.py`), used by both `write_iat_handlers`
(`import_resolver.py`) and `patch_dll_iats`. Any unmatched import -- main
EXE or secondary DLL -- now gets the same auto-generated `[UNIMPLEMENTED]`
fatal-halt stub. Live-verified: `IsDBCSLeadByte` now produces
`[UNIMPLEMENTED] kernel32.dll!IsDBCSLeadByte — halting` instead of silent
garbage execution, and `DllMain`/`CoGetClassObject` report an honest,
traceable failure. Also consolidated the three diagnostic dump functions
(`diagnose_fault`/`diagnose_halt`/`diagnose_thread_end`,
`tew/kernel/exception_diagnostics.py`) onto one shared `_dump_cpu_state`
helper -- the original `diagnose_thread_end` didn't dump full GPRs (only
ESP/EBP), a gap caught mid-investigation since ESI was exactly the register
that mattered here.

Added `tests/unit/api/test_invoke_emulated_proc_thread_death.py`, a unit
test for the "calling thread died mid-call" detection fixed in the previous
session -- previously untested at the unit level. Verified it fails without
the fix (asserts on garbage EAX) and passes with it. 583/583 tests passing.

## 2026-07-21 — DllMain's "non-deterministic" return value fully diagnosed
and fixed: it was register misattribution on top of a real thread
(`tid=1012`) dying mid-call, not corruption

Started as "explain the DllMain non-determinism" (status.md's
2026-07-19-night open item: `0`, `105`, `70959764`, `70959264`,
`70961940`, `70957766` observed across separate runs). Ended up finding two
independent, real bugs in `_invoke_emulated_proc` (`tew/api/user32_handlers.py`),
plus new scheduler debug visibility that made the second one findable at all.

**Bug 1 — result misattribution.** `_invoke_emulated_proc`'s polling loop
(added in the 2026-07-19 thread-switch fix) unconditionally read
`cpu.regs[EAX]` as "the call's return value" after breaking out of its
chunked `cpu.run()` loop, regardless of *why* it broke: a fatal halt on an
unrelated thread, or exhausting `max_steps` while some other cooperative
thread was live. Both cases leave whatever register state that *other*
code left behind, misattributed as "this call returned `<it>`". Confirmed
via raw disassembly of the real `entry()`/`DllMain` wrapper
(`0x04479f74`, `dao350.dll`) that it can only ever return exactly `0` or
`1` — so every other observed value was leftover EAX from unrelated code,
not anything `DllMain` computed. Fix: track whether the call "genuinely
completed" (halted on the *originating* thread, at the sentinel) and
return `0` with a clear diagnostic for every other exit path, instead of
reading a register that doesn't hold what's claimed.

**Bug 2 — the real root cause.** Even with budget raised 10x (5,000,000 →
50,000,000 steps), `DllMain`'s call still never completed. New scheduler
debug logging (see below) traced it exactly: the real calling thread is
`tid=1012` (idx=12), a short-lived worker spawned via the same generic CRT
thread wrapper (`0x9fc3a0`) as the timer thread and others — spawned ~57s
in, and **dead 244ms later**, having hit its own `THREAD_SENTINEL` (i.e.
its stack unwound straight past the sentinel `_invoke_emulated_proc`
pushed for `DllMain`'s return). Since dead threads are permanently excluded
from `_pick_next_ready`, the loop's "wait for `scheduler.current_idx` to
become `started_thread_idx` again" condition was mathematically
unsatisfiable from that point on — no `max_steps` budget, however large,
was ever going to complete it. Fix: detect
`threads[started_thread_idx].status == DEAD` inside the polling loop and
bail immediately instead of exhausting the budget uselessly.

**Live-verified**: the run now reaches the identical final halt
(`EIP=0x00200c00`, same registers/stack) at **57.4s instead of 71.4s**
(~14s faster), with an honest `DllMain(DLL_PROCESS_ATTACH) -> 0` log
instead of a misleading garbage number or a multi-second stall. *Why*
`tid=1012` dies prematurely is still open — see status.md's new top
priority.

**New scheduler debug visibility** (`tew/kernel/scheduler.py`,
`tew/api/kernel32_io.py`) — this is what made the `tid=1012` diagnosis
possible at all; without it, the initial "scheduler fairness/starvation"
hypothesis would have been very hard to rule out:
- `create_thread`'s caller (`_create_thread`, `kernel32_io.py`) now logs
  the assigned scheduler `idx` alongside `tid` — previously `idx` was
  completely untraceable from logs, only `tid` was ever printed.
- Every actual context switch is now logged in `_load_next`: `switch:
  idx=X (tid=Y) -> idx=Z (tid=W)`.
- Every block transition now logs *why* (`block_current_on_cs`,
  `block_current_on_handles`, `sleep_current`) — previously only the
  *wake-up* side (`unblock_cs`, `unblock_handle`, `tick` sleep-wake) was
  logged, never what a thread actually blocked on or when.
- Enable via `LOG_LEVEL=debug LOG_CATEGORIES=scheduler,thread`.

**Ruled out this session** (worth recording so it isn't re-investigated):
a `tid=1007`/`tid=1009` wait/signal ping-pong on handle `0x701a` dominates
the scheduler log for the entire stall window and initially looked like a
starvation livelock — but the same wait/signal/wait-again shape is present
in `tid=1006` (the timer thread) from as early as 4s into the run, which is
its normal, designed per-tick behavior. Not a bug on its own; the real
cause was the dead-thread issue above, unrelated to this pattern.
Separately, `mmtimer_callback`'s own nested call was suspected of calling
`abortmessage` on failure — decompiled directly (`0x00a30a40`) and
confirmed it does NOT; its `timeSetEvent`-failure path is just a silent
`_DEBUG_trace` + graceful shutdown (`_TIMERhz = 0`, `timeEndPeriod`).
`_TIMER_init` (`0x00a30be0`) does have real `abortmessage` calls, but none
of its guard conditions are triggered by our stubs, and its one
retry-exhaustion path takes several real seconds via a `_THREAD_yield`
spin-loop — timing doesn't match the observed ~microsecond-scale event.
The final `EIP=0x00200c00` halt is confirmed unrelated to DAO/`DllMain`
(happens at the same relative point regardless of how `DllMain` resolves)
but its real cause (which unimplemented API) is still unidentified.

**Status**: 582/582 tests pass. Live-verified against the real
`MCity_d.exe` + `dao350.dll`. **Uncommitted** in the working tree
(`run_exe.py`, `tew/api/kernel32_io.py`, `tew/api/oleaut32_handlers.py`,
`tew/api/user32_handlers.py`, `tew/kernel/scheduler.py`) — not committed
per "only commit when explicitly asked."

---

## 2026-07-19 (night, continued further) — the "~496-byte stack corruption"
was actually a NULL-vtable dispatch crash; real root cause identified for
the crash chain, none of it fixed yet

Continuation of the same night's DAO investigation (previous entry below),
picked back up via a run of clarifying COM questions (IID vs IClassFactory,
what `ppv` points to, whether the `__chkesp` signed-comparison handling was
correct) that led to actually re-examining the evidence rather than trusting
the earlier "stack corruption" framing.

**The corruption framing was wrong.** Re-running with the `seh` log
category (prompted by "nothing I'm seeing would explain a 496-byte
object") showed the real sequence: `dao350.dll`'s real `DllGetClassObject`
returns `S_OK` for the game's first `CoGetClassObject` call but never
populates `*ppv` (confirmed by logging `*ppv` alongside `hr` at the call
site — it stays NULL). The game's own fallback code doesn't NULL-check
`local_2c` before dispatching through its vtable, wild-jumps to
`EIP=0xfefc8d8f`, and takes a real `0xc0000005` ACCESS_VIOLATION. SEH
(`tew/kernel/seh.py`) walks 15 real `FS:[0]` frames, finds no handler, and
takes the "unhandled fault, halting as before" path — which is not a clean
unwind, and leaves stale return addresses on the stack. `__chkesp` was
detecting *that* leftover stale data, not a fixed-size corruption: the
~496-byte delta is consistent with stale frames from the 15-frame walk,
and — the clinching piece — `__chkesp`'s own reported return address
(`0x008f55a0`) doesn't match the real one at that call site (`0x008f5351`,
confirmed via `dump_bytes`), which only makes sense if recovery left old
data in place instead of restoring the true frame.

**Also found, while chasing this**: `_chkesp`'s diagnostic
(`patch_internals.py`, `0x009F1BC0`) hardcodes `EBP` as "the" pre-call ESP
snapshot register in its delta-computation message. The ZF check it
performs to decide pass/fail is correct and sign-agnostic (`ZF_BIT` only),
but the snapshot register is actually a compiler register-allocation
choice, not always EBP — at the specific call site `0x008f4f04`/`06` the
real instruction is `CMP ESI,ESP` (`3B F4`, confirmed via raw byte
decoding), not `CMP EBP,ESP` as the message claims. Not yet fixed.

**Net result**: the open bug is no longer "find what's corrupting the
stack." It's now three concrete, well-scoped items — see status.md's
"Next step" section: (1) why `DllGetClassObject` leaves `*ppv` NULL
despite `S_OK` (its own code reads correct, so likely something this
emulator provides it that's wrong), (2) whether `seh.py`'s unhandled-fault
path should do a real unwind instead of halting with stale stack state,
and (3) fixing `_chkesp`'s hardcoded-EBP diagnostic message. Nothing code-
level was changed this entry — this was pure re-diagnosis of existing
behavior via more targeted logging and disassembly cross-referencing.

---

## 2026-07-19 (night, continued) — _invoke_emulated_proc made thread-aware
(real, separate bug, found and fixed); DAO's real DllGetClassObject read
directly in Ghidra and confirmed correct; the actual bug is now precisely
localized to a deterministic stack corruption, not yet root-caused

Continuation of the same night's DAO investigation (previous entry below).

**`dao350.dll` opened in Ghidra as its own program** (`import_and_analyze`,
`~/.emu32/WINDOWS/System32/dao350.dll` — previously only `MCity_d.exe` had
ever been analyzed) and its real `DllGetClassObject`/`QueryInterface` read
directly, at the user's request, after live logging showed both
`CoGetClassObject` calls returning `hr=S_OK` with `*ppv` staying NULL.
Ghidra's decompiled C was actively misleading for the CLSID-matching logic
— it rendered real 16-byte `REP CMPSB` GUID comparisons (confirmed from the
*raw disassembly*, not the decompile) as fake 1-2 byte C string literals,
making it look like only the first byte of the CLSID was being checked.
The real logic does a full, correct 16-byte compare against several
DAO-family candidate CLSIDs and correctly matches ours (live-confirmed via
a Zig-level logpoint at the match branch — fires every time, regardless of
outcome). The same investigation found what looked like a bug (the same
8-byte-allocated object's vtable-pointer field gets overwritten 4 times in
a row by `FUN_0447d398`) but is actually a completely ordinary
Borland/Delphi-style multi-level virtual-inheritance constructor chain —
each write is a different base class setting its own vtable, only the
last one (the most-derived class, set last) matters. `QueryInterface`
itself (found via `dump_bytes` on the real vtable address, after an
initial logpoint at a *wrong* guessed address never fired) correctly
recognizes `IUnknown`/`IClassFactory`/`IClassFactory2`, calls `AddRef`,
and writes itself into `*ppv` — this code, read statically, is completely
correct for what the game requests. None of tonight's earlier suspicion
that DAO's own code was somehow buggy held up under direct inspection.

**Root cause re-localized, live, via logpoints and Ghidra raw disassembly
cross-referencing**: a deterministic ~496-byte stack corruption, always
detected by the game's own `__chkesp` at the exact instruction
(`0x008f4f0b` in `FUN_008f4e70`, confirmed via raw disassembly down to the
individual `PUSH`/`CALL COMPUTED_CALL`/`CMP EBP,ESP`/`CALL __chkesp`
instructions) immediately following the game's first
`CoGetClassObject(rclsid, 1, NULL, IID_IClassFactory, &local_2c)` call —
not, as first suspected, something specific to `CoCreateInstance` (an
earlier run's corruption happened to surface there instead, but that was
this same underlying bug manifesting at a different point, not a second
distinct bug — confirmed by the address being identical to the
`CoGetClassObject`-triggered case in other runs).

**Found and fixed a real, separate bug while chasing this**:
`_invoke_emulated_proc` (`user32_handlers.py`) ran `cpu.run(max_steps)` in
one single 5,000,000-step call with no awareness of logical threads. If
the calling thread hits anything that makes the cooperative scheduler
switch away (`Sleep`, `WaitForSingleObject`, a contested critical section
— all implemented by directly swapping CPU state via
`scheduler._save_current`/`_load_next`), the nested call would keep
executing whatever thread was live next, for up to the full step budget,
with zero awareness it was no longer running the code it actually called.
A halt hit on that other thread (its own unrelated sentinel, or worse, an
unrelated fatal halt) would then be misread as "our nested call returned".
**Live-verified this genuinely happens**: calling `dao350.dll`'s real
`DllMain`, virtual time jumped 1.4 seconds and five unrelated threads
(1000, 1004, 1005, 1007, 1009) ran to completion inside what was supposed
to be one narrow nested call. Fixed: run in bounded 200,000-step chunks;
after each chunk, if `scheduler.current_idx` no longer matches the thread
that started the call, clear any non-fatal halt (it belongs to the other
thread) and keep going — the calling thread eventually gets rescheduled.
A genuine `fatal_halt` still stops everything immediately regardless of
which thread caused it. New optional `scheduler` parameter, backward
compatible for the existing short dialog-proc/timer-callback call sites
that don't pass it (their calls are short enough this was never observed
to matter); wired into the three DAO call sites where it was found.

**This fix did NOT resolve the stack corruption** — live-verified after
the fix, the exact same `__chkesp` failure (same address `0x008f4f0b`,
same ~496-byte delta) still occurred. So scheduler thread-switching, while
a real and worth-fixing bug in its own right, was not the (sole) cause of
this specific corruption. Since `_invoke_emulated_proc` forcibly restores
`ESP` as a *register* via `cpu.save_state()`/`restore_state()` regardless
of how the real DAO code cleans up internally, the corruption has to be
in stack *memory content*, not the ESP register itself — meaning
register-level instrumentation (which is all that was used tonight) can't
see it directly. `DllMain`'s return value also remains non-deterministic
across runs (`0`, `70959764`, `105`, `70959264`, `70961940`, `70957766`
observed) — not distinguished yet whether this is the same underlying
issue or separate.

**Diagnostic instrumentation used tonight, all discarded (`git checkout --`)
once it had served its purpose, not committed**: `run_exe.py` temporarily
grew three Zig-level logpoints (`cpu.add_logpoint`, not breakpoints —
breakpoints halt execution and aren't dispatched from inside a nested
`_invoke_emulated_proc` call, so they'd be silently misinterpreted as "the
call returned") tracing the outer CLSID-match branch and the real vtable
call site in `dao350.dll`'s `DllGetClassObject`. `oleaut32_handlers.py`
temporarily grew an ESP/EBP bracket around the nested calls in
`_CoGetClassObject`/`_CoCreateInstance`. Both served their purpose (found
the thread-switching bug, precisely localized the corruption's trigger
point) and were removed once done, per this project's established
"TEMP diagnostics get discarded, not shipped" convention.

**Next step, if this is picked up again**: a memory-write-level trace is
needed, not more register-level logpoints. The ClickHouse execution-history
capture tooling from `~/pe-walker/history-poc` (already proven for exactly
this "what wrote to this address" question in earlier sessions — see
[[tew_fake_kernel_gaps]] section 10 via the wiki/memory system) is likely
the right tool, scoped narrowly to the ~496-byte window between the game's
ESP and EBP during the first `CoGetClassObject` call specifically.

**Commits** (`main`, not pushed): `5e49168` `_invoke_emulated_proc`
thread-awareness (the only code change that survived this half of the
session — the corruption investigation itself produced no shippable fix
yet).

---

## 2026-07-19 (night) — Real COM activation: CoGetClassObject/CoCreateInstance
now load and execute real DLLs (DAO 3.5); several real emulator bugs found
and fixed along the way; final DAO object handoff still broken (open)

**Architecture change**: `CoGetClassObject`/`CoCreateInstance`
(`tew/api/oleaut32_handlers.py`) previously always returned
`REGDB_E_CLASSNOTREG` — no COM was implemented at all. They're now
registry-driven, the same way real Windows COM activation works: look the
requested CLSID up under `hkcr\clsid\{...}\inprocserver32` in
`registry.json` (new entries use this key shape; `registry.json`'s own
`_comment` already documented `hkcr` as a supported hive short name). A
CLSID nobody registered fails honestly with `REGDB_E_CLASSNOTREG`, exactly
like a real, unmodified install missing that component — no hardcoded
per-CLSID branching.

For CLSIDs registered to a server in the new `_KNOWN_COM_SERVERS` set, the
real DLL is loaded and executed via the existing `DLLLoader` machinery
(same mechanism already proven for `authlogin.dll`/`NPSAnlyz.dll`/
`dx8z.dll` — real PE load, real section mapping, real code execution; this
is architecturally the *opposite* of the Python-handler-interception
approach used for OS-level DLLs like kernel32/user32, and was a deliberate
choice discussed with the user rather than building fake Python vtable
objects). `DllMain(DLL_PROCESS_ATTACH)` is invoked for real, then the real
exported `DllGetClassObject` is called; for `CoCreateInstance`, the
resulting `IClassFactory`'s real `CreateInstance`/`Release` vtable methods
are dispatched through too. A `FALSE` return from `DllMain` is now treated
as a real load failure (matches real `LoadLibrary` semantics) rather than
proceeding to call into a DLL that just reported its own init failed.

**The DAO CLSID version mismatch** (this session's whole reason for
investigating): the game's compiled-in CLSID `{00000010-0000-0010-8000-
00AA006D2EA4}` is DAO **3.5**'s, confirmed via `dump_bytes` byte-scanning —
`dao360.dll` (from the generic period-correct-binaries collection at
`/data/Downloads/i386-binaries/`) does NOT contain this CLSID anywhere in
its binary; `dao350.dll`, extracted from the game's own real installer
(`~/.emu32/DBInst/DAO/data1.cab`, `DAO registered\dao350.dll`, via
`unshield x`), DOES. DAO 3.5 and 3.6 are different, non-interchangeable
installed components, not just a version bump — a newer DLL doesn't
substitute for an older CLSID a game statically compiled against. Placed
at `~/.emu32/WINDOWS/System32/dao350.dll` (i.e. exactly where the real
installer would have put it) — kept out of the tew repo itself since these
are Microsoft-copyrighted redistributable binaries, not project source.

**Real bugs found and fixed while getting this far** (each independently
live-verified and test-suite-clean, 582/582 throughout):

1. **`_invoke_emulated_proc` (`tew/api/user32_handlers.py`) was
   single-stepping one instruction at a time** via a Python `while` loop
   (`cpu.step()` is literally `cpu.run(1)` under the hood — same native FFI
   call, `max_steps=1`), paying a full Python/Zig crossing per instruction.
   Fine for the short dialog-proc/timer-callback calls this helper was
   originally built for, ruinously slow for a nested call running real
   third-party DLL code end to end. Fixed: call `cpu.run(max_steps)` once —
   the sentinel this waits for is a real `HLT` byte in memory
   (`_get_dialog_sentinel`), so the native Zig hot loop already halts on
   its own the instant the call returns, exactly like any other halt
   condition. Had to account for `HLT` advancing EIP past its own 1-byte
   opcode (lands at `sentinel+1`, not `sentinel`) when checking where the
   call actually stopped. Measured effect live: DAO's `DllMain` call
   dropped from ~7s to ~1.3s wall-clock; the first `CoGetClassObject` from
   ~17s to ~9.2s (still slow in absolute terms — DAO's real code makes many
   Win32/CRT calls, each of which still needs a real Python handler
   round-trip regardless of how the between-calls instructions are driven).

2. **`patch_dll_iats` was being called unconditionally on every
   `CoGetClassObject`/`CoCreateInstance`**, re-scanning the *entire*
   accumulated IAT-entry list for every DLL ever loaded (`MCity_d.exe` +
   `authlogin.dll` + `NPSAnlyz.dll` + `dx8z.dll` + `dao350.dll`, hundreds of
   entries) even when nothing new had been loaded since the previous call.
   Fixed: only call it right after a genuinely new `load_dll` (`not
   was_loaded`). Directly observed contributing to each successive DAO call
   being measurably slower than the last.

3. **`dao350.dll` imports from the legacy `MSVCRT40.dll`** (VC4-era CRT
   naming), not `MSVCRT.dll` like `dao360.dll` and everything else tew has
   loaded so far — so its IAT entries for that DLL were silently left
   unpatched (`win32_handlers.get_handler_address("msvcrt40.dll", ...)`
   never matches tew's `"msvcrt.dll"`-registered handlers). Fixed: a small
   `_LEGACY_DLL_ALIASES` table in `dll_loader.patch_dll_iats`
   (`msvcrt40.dll`/`msvcrt20.dll`/`crtdll.dll` → `msvcrt.dll`), tried as a
   last resort after the normal and `.dll`-suffixed lookups.

4. **The INT3 debug-breakpoint halt (`tew/api/win32_handlers.py`) was
   missing `cpu.fatal_halt`** — set `cpu.halted = True` only. Exactly the
   same class of gap the section-15 scheduler fix closed for unimplemented
   Win32 API halts: a plain `halted` without `fatal_halt` gets silently
   cleared by the very next scheduler thread-switch or nested
   `_invoke_emulated_proc` call, so this "halt" was never actually stopping
   anything — execution continued right past `INT3 breakpoint at
   EIP=0x00688c69 — halting` instead of genuinely halting there. This
   explains an observation from earlier tonight that looked like
   nondeterministic "bouncing" between reaching a second `CoGetClassObject`
   call versus hitting this INT3: it isn't random — when DAO's real
   `DllMain` fails fast, very little wall-clock is spent inside it, so the
   rest of boot has time to reach the (unrelated, later) INT3 milestone
   within a fixed timeout; when DAO succeeds, the real `CoGetClassObject`
   calls burn most of the wall-clock budget, so the run simply hasn't
   reached that later point yet when the timeout fires. Not yet
   independently re-verified in isolation (a full run since this fix always
   also has the DAO work active).

5. **`GetLastError`/`SetLastError` (`tew/kernel/scheduler.py`) shared one
   fixed memory-backed value (`TEB_BASE+0x34`) across every thread** —
   real Windows gives each thread its own TEB/last-error slot; here, one
   thread's `SetLastError` could silently clobber every other thread's
   `GetLastError` result across a context switch. Fixed: added
   `ThreadState.last_error`, saved/restored on every context switch
   (`_save_current`/`_load_thread`/`_init_thread_stack`), exactly mirroring
   the existing TLS-slot save/restore (`_save_tls`/`_load_tls`) shape.
   Found while reasoning about whether real third-party DLL code (far more
   likely than tew's own handlers to actually race multiple threads through
   Win32 calls) could be exposing this kind of latent gap.

6. **`FormatMessageA` (`tew/api/kernel32_io.py`) was an unconditional
   halt.** The game's own COM error-handling path (`dbcode.c`, called when
   `CoGetClassObject`/`CoCreateInstance` fail) calls this to turn an
   HRESULT into readable text before logging/displaying it, so the halt was
   masking the actual diagnostic message rather than just being an
   unrelated missing API. Implemented: `FORMAT_MESSAGE_FROM_SYSTEM` (a
   small table of the HRESULTs this emulator's own COM handlers actually
   produce, e.g. `0x80040111` → "Class not registered for this server --
   the object is not available") and `FORMAT_MESSAGE_FROM_STRING`;
   `ALLOCATE_BUFFER` supported; `%1`/`%2`-style insert-argument
   substitution is not, since every caller seen so far uses
   `FORMAT_MESSAGE_IGNORE_INSERTS`.

7. **`Channel_DebugPrint` (`channel.c`, `FUN_004cc5b0`) was entirely
   unpatched real game code** whose real routing target (a per-`(user,
   channel)` listener table gated on a runtime debug-console-enabled flag)
   is the game's own unrendered debug console — meaning nothing it logs
   ever reached tew's log regardless of that gate. Patched the whole
   function like `_CrtDbgReport`: format the `%s`/`%d` varargs ourselves
   (multi-arg, in appearance order — the existing `_CrtDbgReport` pattern
   only substituted one), always surface the result at `WARN`, skip
   replicating the real listener-routing plumbing.

**Logging noise cleanup** (several call sites moved from `debug`/`info`
down to `trace`, the quietest level, below `debug` — found while trying to
actually read a `LOG_LEVEL=debug` capture of the DAO work above and
discovering it was 90%+ drowned out): `_CrtDbgReport`'s routine
`_CRT_WARN`-type end-of-process memory-leak dump (fires dozens of times
per run, zero signal — the fatal `_CRT_ERROR`/`_CRT_ASSERT` path, which
halts, is untouched); `timeSetEvent` (fires roughly every 1ms via the
game's self-rescheduling multimedia timer — also quieted a second, dead/
overridden duplicate registration in `advapi32_handlers.py` for
consistency); `free`/`operator delete` (fire on every one of the bump
allocator's no-op releases); `GetFullPathNameA` (fires per file-path
lookup); `SNDMEMI_validate`'s per-pool-entry OK/not-OK detail (its
corruption-summary line, which fires only when something's actually wrong,
stays at `warn`).

**Also fixed, smaller correctness/observability items**:
- Two "activation failed" log messages in the new `CoGetClassObject`/
  `CoCreateInstance` code unconditionally claimed the DLL "failed to load
  from disk", which was actively misleading whenever the real, already-
  logged reason was a `DllMain` failure instead — now a generic, accurate
  "activation failed (see above)".
- The `IID_IClassFactory` scratch GUID buffer (needed for
  `CoCreateInstance`'s internal `DllGetClassObject(IID_IClassFactory)`
  call) was being allocated unconditionally at handler-registration time —
  broke small-memory test harnesses that construct `CRTState` without ever
  touching COM. Now allocated lazily on first use, mirroring the existing
  `user32_handlers._get_dialog_sentinel` pattern.
- Success log lines for `CoGetClassObject`/`CoCreateInstance` now include
  the resulting `*ppv` object address, not just the HRESULT — this is what
  actually surfaced the open bug below; before this, the two-line log
  looked identical apart from the requested IID and gave no way to tell
  whether two calls returned distinct objects or nothing at all.

**Open, not resolved tonight**: with all of the above fixed and `dao350.dll`
loading and running for real, both `CoGetClassObject` calls the game makes
(`IID_IClassFactory`, then `IID_IClassFactory2`) return genuine `hr=S_OK`
from real compiled DAO 3.5 code — but `*ppv` is `0x00000000` (NULL) on both,
a real COM contract violation (success must guarantee a valid object
pointer). No `_invoke_emulated_proc` timeout or unexpected-halt warning
fires during these calls, so this isn't a scheduler/timing artifact — the
nested call genuinely runs to completion and hits the real sentinel
normally; `dao350.dll`'s own code is doing this. `CoCreateInstance`'s
internal `DllGetClassObject(IID_IClassFactory)` call hits the identical
bug, and the existing NULL-check fallback correctly reports `E_FAIL` rather
than crashing on a null vtable dispatch, but the DAO handshake still can't
complete. Separately, `DllMain`'s own return value is non-deterministic
across runs (`0`, `70959764`, `105`, `70905676` observed in separate runs)
— currently handled defensively, not root-caused. Next step if this is
picked up again: `dao350.dll` itself has never been opened as its own
Ghidra program (only `MCity_d.exe` has been analyzed) — import it and read
its real `DllGetClassObject` implementation directly.

**Commits** (`main`, not pushed): `2d5f69d` `_CrtDbgReport` → debug,
`adff060` `Channel_DebugPrint`, `b163faa` `FormatMessageA`, `836ff95`
per-thread last-error, `30a1cc6` more trace-level noise, `166f0d6`
`_invoke_emulated_proc` native speed, `ed1f685` INT3 `fatal_halt`,
`bc6ac2d` real `dao350.dll` COM activation, `f59d4ef` remaining trace-level
noise.

---

## 2026-07-19 — MessageBoxA/W log severity now matches dialog icon; fatal dialogs
tracked so a voluntary ExitProcess after one can't be logged as a clean exit

**`tew/api/user32_handlers.py`**: `_show_messagebox` now maps the same `u_type &
0x70` icon bits already used to pick the SDL dialog's appearance to a log level —
`MB_ICONERROR`/`MB_ICONSTOP`/`MB_ICONHAND` (`0x10`/`0x20`) → `logger.error`,
`MB_ICONWARNING`/`MB_ICONEXCLAMATION` (`0x30`) → `logger.warn`, else `logger.info`
(unchanged default). Applies whether the dialog was auto-answered via a hook or
shown for real. Previously every `MessageBoxA`/`MessageBoxW` call logged at a flat
`INFO`, so a fatal stop-icon abort and a routine yes/no confirmation were
indistinguishable in the log — found via a live run where the game's own
`abortmessage` dialog (`depthconv.c:1137`, "Failed to initialize database...",
`type=0x11011`) logged identically to the harmless "run full screen?" prompt
(`type=0x4`).

**`tew/api/_state.py`**: new `CRTState.fatal_dialogs: list[tuple[str, str]]` —
every error-severity dialog's `(caption, text)` is appended here regardless of how
it was answered.

**`run_exe.py`**: the final run summary now checks `crt_state.fatal_dialogs` —
if any fired, logs `=== Emulation Complete (NOT a clean exit) ===` at `ERROR` plus
a listing of each fatal dialog, instead of the previous unconditional
`=== Emulation Complete ===`. This directly fixes a real misleading-log risk: a
game-side fatal abort followed by a voluntary `ExitProcess(0)` (exactly what
happens today, see status.md) used to produce a summary indistinguishable from an
actually successful run.

**Live-verified**: full run reproduces the fix end-to-end — the `abortmessage`
dialog logs at `ERROR`, the full-screen prompt still logs at `INFO` (no
regression), and the summary correctly reads `NOT a clean exit` with the abort's
caption/text listed. 582/582 tests pass (no test changes needed — this is a pure
logging-severity change with no behavior affecting emulation itself).

---

## 2026-06-06 — IDirect3DTexture8 COM interface + vtable layout fix

**New file: `tew/api/d3d8/idirect3d8texture.py`** — full 18-slot `IDirect3DTexture8`
vtable at `D3DTEX_VTABLE = 0x00220290`.

- IUnknown [0-2], IDirect3DResource8 [3-10], IDirect3DBaseTexture8 [11-13],
  IDirect3DTexture8 [14-17] (GetLevelDesc / GetSurfaceLevel / LockRect / UnlockRect).
- `GetSurfaceLevel(Level, ppSurface)` returns the pre-allocated `IDirect3DSurface8*`
  stored at `texture_obj + 28 + Level*4`.

**`tew/api/d3d8/_layout.py`** — added `D3DTEX_VTABLE = 0x00220290`.

**`tew/api/d3d8/_helpers.py`** — added `_alloc_texture_obj(w, h, fmt, levels, mem)`;
texture object holds `levels` surface ptrs starting at `obj+28`.

**`tew/api/d3d8/idirect3d8device.py`** — `CreateTexture`/`CreateVolumeTexture`/
`CreateCubeTexture` now call `_alloc_texture_obj` instead of `_alloc_surface_obj`;
`levels` argument is now read and forwarded.

**`tew/api/dinput_handlers.py`** — relocated `DI_VTABLE → 0x002202E0`,
`DI_OBJ → 0x00220310`, `DI_DEV_VTABLE → 0x00220320` (D3DTEX_VTABLE now occupies
the old DI range `0x00220290`–`0x002202D7`).

**`tew/api/dsound_handlers.py`** — relocated `DS_VTABLE → 0x00220370`,
`DS_OBJ → 0x002203A0`, `DS_BUF_VTABLE → 0x002203B0` (old DS addresses were inside
the shifted DI range).

**`tew/api/patch_internals.py`** — removed SNDMEMI pool watchpoint (was on `blist+7`,
MSB of pool block-entry size field); game now runs past ~189M steps.

**`tew/api/kernel32_io.py`** — added per-file ReadFile logging for `ealogo.mad`.

**Result:** game runs ~189M steps through many BeginScene/EndScene/Present cycles,
then crashes in `_MAD_decodemacroblock` (showmad playing ealogo.mad). SNDMEMI
watchpoint removed (new blocker: MAD decoder crash, see status.md).

---

## 2026-05-31 — ifc22.dll (ImmVersion FFB) stubs + dialog window fix

**New file: `tew/api/ifc22_handlers.py`** — stubs for all 11 ifc22.dll imports:
- Default constructors (CImmMouse, CImmProject, CImmPeriodic): thiscall no-ops
- `CImmMouse::Initialize` → returns 0 (no FFB hardware); the game's entire FFB
  code path is conditional on this return value, so it is fully skipped
- Destructors: no-op (nothing was allocated)
- FFB device methods (UsesWin32MouseServices, OpenFile, Start, ChangeParameters):
  registered as loud halts — should never be called when Initialize returns 0
- All names confirmed via `pefile` on MCity_d.exe; call graph verified in Ghidra

**`tew/api/crt_handlers.py`** — added `register_ifc22_handlers` call.

**`tew/api/window_manager.py`** — fixed regression from 54189a1: dialog windows
had `SDL_WINDOW_VULKAN` flag which prevents SDL renderer creation. Restored
`SDL_WINDOW_SHOWN` + SDL renderer (with soft fallback) for all dialog windows.
The Vulkan surface belongs to the D3D8 game window, not dialog windows.

**Note:** The login dialog (`DialogBoxParamA`) requires a user click on OK to
proceed — there is no auto-submit. Interactive runs proceed past login; automated
test runs remain at the dialog. This is expected behavior.

**Result:** No new step count measurable in automated runs (login requires
interaction). When the user clicks OK, the game will proceed past ifc22 and
continue. Next automated blocker will be visible in the next interactive run.

---

## 2026-05-31 — Implement CharUpperA

**`tew/api/user32_handlers.py`** — added `_CharUpperA` handler.
Both spec branches: HIWORD==0 → single-char uppercase return; HIWORD!=0 → string
pointer, uppercase ASCII ('a'-'z') in-place, return pointer. Non-ASCII unchanged.

**Result:** Game proceeds past step 133,014,085 (4,546 steps past CharUpperA).
New blocker: `ifc22.dll!??0CImmMouse@@QAE@XZ` (CImmMouse constructor).

---

## 2026-05-31 — Implement GetFullPathNameA

**`tew/api/kernel32_io.py`** — replaced `_halt("GetFullPathNameA")` with real handler.
Resolves relative paths against `C:\MCity`; normalises `..` and `.` segments; writes
to lpBuffer and sets *lpFilePart. Returns char count on success, required size if buffer
too small.

**Result:** Game proceeds past step 133,014,079. New blocker: `user32.dll!CharUpperA`.

---

## 2026-05-31 — Implement Dev::DrawPrimitive (Vulkan geometry rendering)

**New file:** `tew/api/d3d8/_pipeline.py`
- SPIR-V passthrough vertex/fragment shaders encoded as Python word lists (no external compiler)
- `create_image_views`: VkImageView per swapchain image
- `create_render_pass`: single color attachment, loadOp=LOAD (preserves cleared background)
- `create_framebuffers`: one VkFramebuffer per swapchain image
- `create_pipeline`: graphics pipeline — XYZRHW+DIFFUSE vertex layout (stride=32), dynamic viewport/scissor, no culling
- `create_vertex_buffer`: 4MB host-visible/host-coherent Vulkan buffer, permanently mapped
- `init_pipeline`: orchestrates all of the above

**`tew/api/d3d8/_state.py`** additions:
- `_vk_image_views`, `_vk_render_pass`, `_vk_framebuffers`, `_vk_pipeline`, `_vk_pipeline_layout`, `_vk_vertex_buffer`, `_vk_vertex_memory`, `_vk_vertex_mapped_ptr`
- `_vk_in_render_pass` flag
- `_draw_stream_ptr`, `_draw_stream_stride`, `_draw_vertex_fvf`

**`tew/api/d3d8/idirect3d8.py`** — CreateDevice calls `init_pipeline` after swapchain + sync primitives are ready.

**`tew/api/d3d8/idirect3d8device.py`** changes:
- `SetStreamSource`: now stores vertex buffer data pointer and stride in `_state`
- `SetVertexShader`: stores FVF handle in `_state._draw_vertex_fvf`
- `BeginScene`: adds `TRANSFER_DST → COLOR_ATTACHMENT` barrier + `vkCmdBeginRenderPass`
- `EndScene`: calls `vkCmdEndRenderPass` (render pass finalLayout handles `PRESENT_SRC_KHR` transition)
- `Clear`: uses `vkCmdClearAttachments` when inside render pass, `vkCmdClearColorImage` otherwise
- `DrawPrimitive(slot 70)`: transforms XYZRHW→NDC in Python, uploads to Vulkan buffer, issues `vkCmdDraw`

**Result:** Game now renders geometry. DrawPrimitive fires (prim_count=1 triangles).
New blocker: `GetFullPathNameA` (kernel32, file I/O) — game has advanced past rendering.

---

## 2026-05-31 — Fix D3DRES_VTABLE buffer slot ordering

**Root cause diagnosed:** `D3DRES_VTABLE` (used by vertex/index buffer objects) had Surface methods
at slots 11–14. dx8z calls Lock (slot 11) and Unlock (slot 12) on vertex buffers per the D3D8 spec,
but our vtable had Surface::GetContainer (arg_bytes=8) and Surface::GetDesc (arg_bytes=4) there.
Each Lock call (which pushes 4 args = 16 bytes) hit GetContainer's 8-byte cleanup, leaving 8 bytes
uncleaned. After 100K steps this corrupted EBP → 0x020d9228 (.data) and the LEAVE+RET sequence
popped a .data value (0x2196) as the return address → RUNAWAY.

**Files changed:**
- `tew/api/d3d8/idirect3d8resource.py`: Reduced from 18 to 14 slots. Removed dead Surface
  methods (slots 11–14 — surfaces use D3DSURF_VTABLE, not D3DRES_VTABLE). Buffer methods now at
  correct spec positions: Lock(11, arg_bytes=16), Unlock(12, arg_bytes=0), GetDesc(13, arg_bytes=4).
  Also fixed `_get_device` which read ESP+4 (= `this`) instead of ESP+8 (`ppDevice`).
  Implemented `_buffer_get_desc` to fill D3DVERTEXBUFFER_DESC with size from object field.
- `tew/api/d3d8/_layout.py`: Updated D3DRES_VTABLE comment (14 × 4 = 56 bytes).

**Result:** RUNAWAY at 132.7M eliminated. Game now halts loudly at `Dev::DrawPrimitive`.

---

## 2026-05-31 — DirectInput stub + cursor/keyboard handlers

**New file:** `tew/api/dinput_handlers.py`
- `DirectInputCreateA` (dinput.dll + dinput8.dll) — returns DI_OK, writes singleton IDirectInput2 COM object to `*lplpDirectInput`
- IDirectInput2A vtable (9 slots @ DI_VTABLE=0x00220290, singleton @ DI_OBJ=0x002202C0): QI, AddRef, Release, CreateDevice (heap-allocates device obj), EnumDevices (S_OK, no callbacks), GetDeviceStatus (DI_NOTATTACHED), RunControlPanel (E_NOTIMPL), Initialize (S_OK), FindDevice (DIERR_DEVICENOTREG)
- IDirectInputDevice2A vtable (18 slots @ DI_DEV_VTABLE=0x002202D0): full set including GetDeviceState (zeroes output buffer), GetDeviceData (pdwInOut=0), SetDataFormat/SetCooperativeLevel/Acquire/Unacquire (all S_OK)

**user32_handlers.py additions:**
- `GetCursorPos` → writes (0,0), returns TRUE
- `ScreenToClient`, `ClientToScreen` → no-op, returns TRUE (window at screen origin)
- `SetCursorPos` → no-op, TRUE
- `SetCapture` → 0, `ReleaseCapture` → TRUE, `GetCapture` → 0
- `ClipCursor` → TRUE, `MapWindowPoints` → 0
- `GetAsyncKeyState` → 0 (key up)
- `GetKeyboardState` → zero-fills 256-byte table, TRUE
- `GetKeyboardType` → 4/0/12 for type/subtype/fkeys (Enhanced 101-key)
- `MapVirtualKeyA` → 0

**Result:** Game now reaches the render loop. RUNAWAY at step 132.7M (new blocker).

---

## 2026-05-31 — IDirect3DSurface8 vtable fix + BeginScene deadlock fix

**Root cause 1:** D3DRES_VTABLE had IDirect3DResource8 slot ordering (slot 9 = PreLoad),
but dx8z expected IDirect3DSurface8 ordering (slot 9 = LockRect). `_THRASH_lockwindow`
crashed on INT 0xcd because it called LockRect through the wrong vtable.

**Fix:** New `D3DSURF_VTABLE` @ 0x00220260 with correct 11-slot IDirect3DSurface8 layout.
`_alloc_surface_obj(w, h, fmt, memory)` allocates 24-byte COM objects with vtable,
data ptr, size, width, height, and D3DFORMAT. GetDesc reads these fields; LockRect
fills D3DLOCKED_RECT with correct pitch (w×4) and data pointer.
All surface-returning device methods (`GetBackBuffer`, `CreateTexture`,
`CreateRenderTarget`, `CreateDepthStencilSurface`, `CreateImageSurface`,
`GetRenderTarget`, `GetDepthStencilSurface`, `CreateVolumeTexture`, `CreateCubeTexture`)
switched to `_alloc_surface_obj`.

**Root cause 2:** BeginScene deadlock on frame 2+. `vkWaitForFences` was called directly
on the main thread; Mesa/Wayland WSI can call `wl_display_roundtrip` internally, which
needs the main thread to pump events — deadlock.

Also: dx8z calls BeginScene/EndScene twice during THRASH init without any Present
between them. The second BeginScene called `vkWaitForFences` on an unsignaled fence
(reset in frame 1, never re-signaled because QueueSubmit was never called).

**Fix:** Moved `vkWaitForFences` + `vkResetFences` into `vk_pump` background thread.
Added `_vk_frame_submitted` flag (set by Present after QueueSubmit, cleared by
BeginScene) to gate the wait. Added `_vk_image_acquired` flag to skip re-acquiring
when BeginScene is called multiple times without Present.

**Result:** Game now reaches `dinput.dll!DirectInputCreateA` (new blocker).

---

## 2026-04-25 — Beta binary CD check investigation

Investigated why mcity_beta_1.exe shows "Game CD not found" despite instLev=2 in registry.

**Root cause:** The beta binary's CD check function (~0x4d27c0) reads instLev and sets
bMaxInstall at 0x975f78, but has no conditional skip of the CD loop — it unconditionally
calls SetErrorMode(0x8001), loops GetDriveTypeA over A:–Z:, then returns 2 (failure) if no
game CDROM found. The debug binary has Platform_IsMaxInstall (0x52dc30) which reads from
a different global (0xa10f50) and skips the loop; the beta binary lacks this path entirely.

**Key addresses (beta binary):**
- CD check function entry: ~0x4d27c0 (complex SEH prologue, thiscall)
- instLev check + bMaxInstall set: 0x4d2a11–0x4d2a31
- SetErrorMode call (start of CD loop): 0x4d2a7b, return to 0x4d2a81
- Inner drive check (calls GetDriveTypeA): 0x4d56b0
- Loop back: 0x4d2ae0 (JL 0x4d2a86, 26 drives)
- SetErrorMode restore + CD-found check: 0x4d2ae6, CMP ESI,EDI at 0x4d2aec
- Failure path (return 2): 0x4d2afc; success path (return 0): 0x4d2bb8

**Planned fix (deferred):** return DRIVE_CDROM for the install drive in GetDriveTypeA
handler — binary-agnostic, no address-specific patches needed.

**Diagnostics removed:** SetErrorMode return-address logging, Platform_IsMaxInstall patch.
GetDriveTypeA first-call trace kept.

---

## 2026-04-25 — TDD sweep: fix silent failures, implement missing handlers, port tests to ZigCPU

**Silent failure fixes:**
- `msvcrt._realloc`: was returning a new pointer but discarding old data; now copies
  `min(old_size, new_size)` bytes using `heap_alloc_sizes` to find old allocation size.
- `msvcrt._write`: was ignoring the fd entirely; now routes to host fd via `os.write`,
  with fallback for raw stdout/stderr (fd 1/2) when no file handle entry exists.

**Missing handler implementations:**
- `advapi32.RegEnumKeyExA` / `RegEnumValueA`: fully implemented direct-child enumeration
  from the flat `registry_values` dict using backslash-prefix filtering.
- `kernel32.TryEnterCriticalSection`: three-case implementation — recursive by owner
  (TRUE), free CS (acquire + TRUE), contested by other thread (FALSE, no blocking).
- `user32.CallNextHookEx`: full LIFO chain propagation via `_winhook_chains`; each
  handle knows its position in the chain and invokes the next via `_invoke_emulated_proc`.
- `user32._dispatch_winhooks`: fixed to call only the chain head (not all hooks).
- `oleaut32`: added `logger.warn` to previously silent stubs (VarCyFromStr, LoadTypeLibEx,
  RegisterTypeLib, CoCreateInstance).

**Test suite (543 passing):**
- New API unit tests: msvcrt realloc + write, registry enum happy + invalid paths,
  CS (Init/Enter/Leave/TryEnter) and mutex (Create/Wait/Release/Close) paths,
  hook chain happy + sad paths.
- Hypothesis chaos tests: `@given` invariants for CS/mutex, garbage-memory fuzzing,
  `RuleBasedStateMachine` for arbitrary Enter/TryEnter/Leave sequences.
- Opcode tests ported from Python CPU to ZigCPU black-box execution (all 5 files).
- Deleted `test_cpu.py` — tested Python CPU internals, not the production ZigCPU path.

**Run result:** With server running, game logs in, initializes D3D8, and hangs at
`IDirect3D8::CreateDevice` (Wayland deadlock — unchanged blocker).

---

## 2026-04-24 — GetMessageA cooperative yield + Wayland roundtrip attempt

**`GetMessageA` cooperative yield (`user32_handlers.py`):**
- Fixed broken cooperative-yield path. Old code set `cpu.halted = True` +
  `state.thread_yield_requested = True`; `thread_yield_requested` was never read
  anywhere, so it terminated the run loop instead of yielding.
- New code: `state.scheduler.sleep_current(cpu, memory, retry_eip, 0, 1)` — saves
  thread state at stub entry EIP, marks thread SLEEPING, switches to next READY thread.

**`IDirect3D8::CreateDevice` — Wayland roundtrip (`d3d8/idirect3d8.py`):**
- Added `wl_display_roundtrip(wl_display)` via ctypes after `vkCreateWaylandSurfaceKHR`,
  before any surface-property queries (`vkGetPhysicalDeviceSurfaceSupportKHR` etc.).
- Rationale: Wayland compositor needs to process events before it can respond to
  surface capability queries; without this, those calls deadlock.
- Status: hang persists — exact call still unknown. Next: fine-grained step logging
  inside the surface creation block to isolate which call deadlocks.

**Tests:** 450 (all passing).

---

## 2026-04-24 — BeginPaint/EndPaint + full Vulkan swapchain

**`BeginPaint` / `EndPaint` (`user32_handlers.py`):**
- Implemented `BeginPaint(HWND, LPPAINTSTRUCT) → HDC` using the existing
  `_alloc_hdc` infrastructure. Fills the 64-byte PAINTSTRUCT: hdc, fErase=0,
  rcPaint from window entry cx/cy, all reserved bytes zero.
- Implemented `EndPaint(HWND, LPPAINTSTRUCT)`: reads hdc from PAINTSTRUCT,
  removes it from `_live_hdcs` / `_dc_selected`, returns TRUE.
- Both registered as user32.dll stubs with stdcall 8-byte cleanup (2 args).

**`IDirect3D8::CreateDevice` (`d3d8/idirect3d8.py`):**
- Now builds the complete Vulkan rendering backend:
  - Creates `VkSurfaceKHR` from the SDL window's SDL_GetWindowWMInfo X11/Wayland
    display and window handles via `vkCreateXlibSurfaceKHR` /
    `vkCreateWaylandSurfaceKHR` (selected by WAYLAND_DISPLAY).
  - Destroys the SDL renderer on the game window (Vulkan takes over).
  - Finds a queue family with graphics + present support.
  - Creates `VkDevice` with `VK_KHR_swapchain`.
  - Creates `VkSwapchainKHR` (format B8G8R8A8_UNORM preferred, FIFO present mode,
    extent from D3DPRESENT_PARAMETERS or surface capabilities).
  - Allocates command pool + single `VkCommandBuffer`.
  - Creates 2 semaphores (`image_available`, `render_done`) + 1 fence
    (`in_flight`, pre-signalled).
  - All failures halt with explicit error log.
- `make_vtable` now accepts `window_manager` parameter; threaded through from
  `register_d3d8_handlers(stubs, memory, state)` in crt_handlers.py.

**`BeginScene` / `EndScene` / `Clear` / `Present` (`d3d8/idirect3d8device.py`):**
- `BeginScene`: waits for in-flight fence, acquires swapchain image, records
  UNDEFINED → TRANSFER_DST_OPTIMAL barrier, begins command buffer.
- `Clear`: records `vkCmdClearColorImage` (D3DCOLOR ARGB → float RGBA; only when
  D3DCLEAR_TARGET flag set).
- `EndScene`: records TRANSFER_DST_OPTIMAL → PRESENT_SRC_KHR barrier, ends
  command buffer.
- `Present`: `vkQueueSubmit` with image_available wait + render_done signal +
  in_flight fence; `vkQueuePresentKHR`.
- All four were previously `_halt`; now real Vulkan.

**State (`d3d8/_state.py`):**
- Added: `_vk_device`, `_vk_*_queue_family`, `_vk_*_queue`, `_vk_surface`,
  `_vk_swapchain`, `_vk_swapchain_*`, `_vk_command_pool`, `_vk_cmd_buf`,
  `_vk_image_available`, `_vk_render_done`, `_vk_in_flight`,
  `_vk_current_image_idx`, `_vk_fn_*` extension function slots.

**Tests:** 450 (all passing).

---

## 2026-04-24 — Round-robin preemption

**Round-robin preemption (`scheduler.py`, `run_exe.py`):**
- Added `Scheduler.preempt_slice(cpu, memory)`: after each `cpu.run(batch)`, if the
  current thread is READY and another READY thread exists, rotate to it.
- Called in the main run loop immediately after `cpu.run(batch)`.
- Root cause: `mmtimer_callback` (0x00a30a40) signals its own wait event (0x7012) via
  `_SIGNAL_set` inside the `_tmrsub[]` dispatch loop, so `WaitForMultipleObjectsEx`
  always found the event signaled and never yielded. The timer thread consumed 100% of
  emulated CPU, starving all other threads.
- Result: at 132M steps the main game window (`Motor City Online` HWND 0x1034) is
  created and the game progresses further than before.

**Tests:** 450 (up from 388).

---

## 2026-04-23 — CreateDialogParamA fix, timer dispatch, advapi32 time source

**`proc=0` / 122-second stall (`user32_handlers.py`):**
- `CreateDialogParamA(#106)` fixed with null-guard.

**`PendingTimer.fu_event` — `timeSetEvent` dispatch modes (`kernel32_io.py`):**
- `TIME_CALLBACK_FUNCTION` (0x00): invoke emulated proc
- `TIME_CALLBACK_EVENT_SET` (0x10): SetEvent on handle directly

**advapi32 `timeSetEvent` time source (`advapi32_handlers.py`):**
- Fixed to use `state.virtual_ticks_ms + u_delay`.

---

## 2026-04-21 — Cooperative CS blocking, mutex owner tracking

**Cooperative CriticalSection blocking (`kernel32_sync.py`, `kernel32_system.py`, `kernel32_io.py`, `_state.py`):**
- `_enter_cs`: contested background thread now suspends cleanly — undoes LockCount
  increment, sets `thread.waiting_on_cs = ptr`, sets `thread_yield_requested = True`,
  halts without `cleanup_stdcall` so EIP stays at stub for retry on resume.
  `_leave_cs`: full release resets LockCount = -1 and OwningThread = 0 (replaces old
  decrement + halt-if-waiters). Removed noisy per-release debug log.
- Scheduler (`_cooperative_sleep` + `_run_background_slice`): added `waiting_on_cs`
  check — if `OwningThread == 0`, clears `waiting_on_cs` and allows thread to retry.
- `PendingThreadInfo`: added `waiting_on_cs: Optional[int] = None`.
- Result: NPS networking threads (tid=0x3ec, 0x3ed) survive CS contention; game
  reaches and sustains the rendering loop (BeginScene/SetRenderState cycling) at 129M+ steps.

**Mutex owner tracking (`kernel32_io.py`, `_state.py`):**
- `MutexHandle`: added `owner_tid: Optional[int]` and `recursion_count: int`.
- `WaitForSingleObject`: mutex now checks `owner_tid is None` before acquiring.
  Contested mutex blocks via `waiting_on_handles` (same path as unsignaled event).
  Recursive acquisition by owning thread increments `recursion_count` and returns 0.
- `WaitForMultipleObjectsEx`: mutex ready only when `owner_tid is None`.
- `ReleaseMutex`: decrements `recursion_count`; clears `owner_tid`/`locked` on zero.
- `CreateMutexA`: sets `owner_tid` + `recursion_count = 1` when `bInitialOwner != 0`.
- Scheduler checks updated to use `owner_tid is None` instead of `not obj.locked`.

**Tests:** 388 (unchanged).

---

## 2026-04-20 — TEB/PEB truthfulness, CriticalSection fix, kernel32 split

**TEB/PEB truthfulness (`kernel32_system.py`, `kernel32_sync.py`, `kernel32_io.py`,
`kernel_structures.py`, `_state.py`):**
- `SetLastError`/`GetLastError` now read/write TEB memory at `TEB_BASE + 0x34`.
  Previously used Python `state.last_error` field — binary code doing `MOV EAX, FS:[0x34]`
  directly would get stale zero. `state.last_error` removed entirely.
- `TlsSetValue`/`TlsGetValue` now read/write TEB memory at `TEB_BASE + 0xE0 + slot*4`.
  `TlsFree` zeros the TEB slot. `_cooperative_sleep` saves/restores TLS in TEB memory
  around background thread slices so FS:[0xE0+] is always correct for the active thread.
- `PEB+0x18` (ProcessHeap) now populated: `initialize_kernel_structures` takes a
  `process_heap` argument and writes it into PEB memory.
- `TEB_BASE = 0x00320000` and `PEB_BASE = 0x00300000` added as module constants to `_state.py`.

**CriticalSection fix (`kernel32_sync.py`):**
- `EnterCriticalSection`: correctly increments LockCount (+0x04), sets RecursionCount (+0x08)
  and OwningThread (+0x0C) on first acquisition; increments RecursionCount on recursive entry
  by the same thread. Halts loudly if contested (blocked thread — not implemented).
- `LeaveCriticalSection`: decrements RecursionCount; on full release clears OwningThread
  and decrements LockCount. Halts loudly if waiters exist (LockSemaphore signal — not implemented).

**kernel32_handlers.py split:**
- Monolithic `kernel32_handlers.py` (~1295 lines) split into orchestrator (~329 lines)
  + 5 focused sub-modules: `kernel32_memory.py`, `kernel32_sync.py`, `kernel32_locale.py`,
  `kernel32_system.py`, `kernel32_io.py`. All 388 tests pass.

**Tests:** 388 (up from 386; 2 new: PEB ProcessHeap layout, LastError TEB address).

---

## 2026-04-17 — Heap fix, VirtualAlloc accuracy, user32 handlers, hook dispatch

**Progress:**
Game now enters the main message loop (GetMessageA/PeekMessageA). Blocked by
sporadic SDL_QUIT of unknown origin — not user-triggered. All previously queued
issues (GetKeyState, GetSystemMetrics cap, SetActiveWindow, SystemParametersInfoA)
resolved.

**`__free_dbg` patch (`patch_internals.py`):**
- 0x009F6E20 patched to no-op: MSVC debug CRT internal free validates block headers
  that our bump allocator never writes. `__freeptd` (called by `__endthread`) was
  asserting on every thread exit. Consistent with existing `free()` IAT no-op.

**VirtualAlloc accuracy (`kernel32_handlers.py`):**
- `MEM_RESERVE` with non-zero `lp_addr` now honors the requested address instead of
  ignoring it and using the bump allocator. Bump pointer advanced past the reserved
  region to prevent future overlap.
- `MEM_COMMIT` only: range check against all reserved regions instead of exact-base
  lookup. Game's custom allocator commits sub-pages of a block reserved as a whole.

**New user32 handlers (`user32_handlers.py`):**
- `GetKeyState` → 0 (all keys up)
- `GetSystemMetrics` → SM_CXSCREEN/SM_CYSCREEN capped at 1024×768
- `SetActiveWindow` → NULL
- `SystemParametersInfoA` → SPI_GETSCREENSAVEACTIVE (FALSE), SPI_GETWORKAREA
  (0,0,1024,768), TRUE for others
- `SetWindowsHookExA` → real registration in `_winhooks` dict
- `UnhookWindowsHookEx` → removes from `_winhooks`
- `CallNextHookEx` → 0 (no chain)

**Hook dispatch (`user32_handlers.py`):**
- `_dispatch_winhooks`: called from `PeekMessageA` and `GetMessageA` after writing
  MSG struct. Fires WH_GETMESSAGE hooks with (HC_ACTION, PM_REMOVE, lp_msg) and
  WH_KEYBOARD hooks with (HC_ACTION, vk, 0) for WM_KEYDOWN/WM_KEYUP messages.

**Keyboard input pipeline (`window_manager.py`):**
- WM_KEYDOWN/WM_KEYUP/WM_CHAR/WM_MOUSEMOVE/WM_LBUTTONDOWN/WM_LBUTTONUP constants added
- `_sdl_sym_to_vk`: maps SDL keysyms to Win32 VK codes
- SDL_KEYDOWN/SDL_KEYUP now post WM_KEYDOWN/WM_KEYUP to message queue in addition to
  existing dialog-specific handling

**Progress heartbeat (`run_exe.py`):**
- `[alive]` INFO log every 5M `cpu.step()` calls: step count, EIP, virtual time
- Does NOT fire during `GetMessageA` host-sleep (see status.md queued issues)

---

## 2026-04-17 — GDI object table + step-loop performance

**Progress:**
Game now opens a real SDL window ('Motor City Online') and runs further into startup.
Halts at `GetKeyState(VK_CAPITAL)` on tid=1007 — next blocker.

**GDI object table (user32_handlers.py):**
- `_GdiObj` dataclass: kind/color/style/is_stock
- Stock objects pre-populated at registration (WHITE_BRUSH=0 through DC_PEN=19),
  handles 0x2001+fnObject — stable and traceable
- `GetStockObject`: O(1) lookup into `_stock_handles` dict, returns real handle
- `SelectObject`: real per-DC selection tracking (`_dc_selected`), returns previous handle
- `CreateSolidBrush`: allocates dynamic `_GdiObj` entry from counter 0x3001+
- `DeleteObject`: removes dynamic objects; stock objects survive
- DC state initialized in `_alloc_hdc`, cleaned up in `ReleaseDC`/`DeleteDC`

**Performance:**
- `is_valid_eip`: O(N) linear scan → O(1) dict keyed on 4KB page number (~29K entries,
  built once at startup); major win at 123M+ calls per run
- `cpu.step()`: merged `_skip_prefix` — fetch opcode first, handle prefix inline;
  eliminates one wasted memory read per non-prefix instruction (~99% of steps);
  `_clear_prefixes` only called when a prefix was actually set
- Main loop: modulo → countdown counters; removed dead `prev_eip` assignment

**Next blockers (status.md updated):**
1. `GetKeyState` on tid=1007 — return 0 (key not pressed), 2 lines
2. `GetSystemMetrics` returns real display resolution → window too large (cap at 1024×768)

---

## 2026-04-17 — Timer unblock + handler correctness audit

**Progress:**
Game now runs through full startup: login dialog → HTTP auth (200 OK) → authlogin.dll →
options.ini defaults → dx8z.dll/D3D8 init → timer thread created. Halts at
`GetStockObject(BLACK_BRUSH)` in gdi32. Next milestone: GDI object table.

**Timer / scheduler fixes (unblocked _TIMER_waitticks spin):**
- `_run_background_slice` extracted as module-level function in `kernel32_io.py`
- `WaitForSingleObject(INFINITE)` on main thread now drives background threads
  (process-zero pattern) instead of halting emulation
- Timer heartbeat in `run_exe.py` fires every 100K steps: advances `virtual_ticks_ms`,
  invokes due timer callbacks, runs one background slice — unblocks tid=1006 ticks

**Handler correctness audit:**
- `_lclose`: was silently returning 0; now reads handle, closes host fd, returns
  correct value (handle on success, HFILE_ERROR on unknown)
- `advapi32_handlers.py`: five stack reads missing `& 0xFFFFFFFF` mask — fixed
- `SetForegroundWindow`: misleading "pretend it worked" comment replaced with
  explanation (SDL2 owns the window; Win32 focus mechanics don't apply)
- `GetStockObject`: a fake-handle implementation was written and reverted — kept
  as `_halt` until a real GDI object table exists
- Unused imports and dead variable cleaned (ruff)

---

## 2026-04-13 (third pass) — Bitmap rendering for STATIC controls

**What was broken:**
The "Motor City Online" connecting dialog has one STATIC child control whose
`title = "#109"` (parsed from the Win32 resource 0xFFFF ordinal encoding as a
bitmap reference).  `_render_static` was rendering this as `"[#109]"` placeholder
text, which appeared as a visible window on screen.

**Fix:**
- New `tew/api/bitmap_loader.py`:
  - `BitmapInfo` frozen dataclass (width, height, BGR24 pixels)
  - `parse_dib(raw)` — parses BITMAPINFOHEADER, supports 1/4/8/24/32 bpp,
    flips bottom-up rows to top-down
  - `create_sdl_texture(renderer, info)` — converts BGR24 to SDL2 texture
  - `load_bitmap_texture(renderer, bitmap_id, pe_resources)` — high-level loader
- `WindowEntry.bitmap_texture` field — holds SDL_Texture for STATIC bitmap controls
- `WindowManager.set_pe_resources(pe_resources)` — wires PE resources into wm
- `WindowManager.create_dialog` — preloads bitmap textures for STATIC "#N" controls
- `destroy_window` / `shutdown` — call `SDL_DestroyTexture` on bitmap_texture
- `dialog_renderer._render_static` — renders actual texture via `SDL_RenderCopy`;
  falls back to plain grey rectangle (no text) if texture unavailable
- `run_exe.py` — wires `set_pe_resources` after PE load

**Tests:** 18 new tests in `tests/unit/kernel/test_bitmap_loader.py` (all passing).
Total: 386 tests.

**Still open:**
- `bpp:-1` in dx8z OutputDebugString (D3D display mode query returns garbage)
- TIMER_init failure (`__beginthreadex` not handled → timer thread never starts)
- Threads 1004/1005 crash at stub 68 (InterlockedCompareExchange)

---

## 2026-04-13 (second pass) — Bug-fix session: thread crashes + scheduler stability

**Commit**: e4eb34a

**What broke and why:**

Five background threads (Chat Filter, two INet, two more) were all dying every run.
Root causes found via Ghidra + log analysis:

- Threads 1004–1005 hit `TlsSetValue` with an invalid slot index → halt stub fired.
  Fixed: return `FALSE` (Win32 behaviour) instead of halting.
- Threads 1001–1003 hit `EnterCriticalSection` and triggered `cpu.halted` via an
  internal Python exception (`ValueError` from out-of-bounds `memory.read32`).
  Root cause: corrupted ESP. Now the crash message includes `cpu.last_error` so
  the actual fault address will be visible on next run.
- After all threads died, the main thread entered a Sleep() loop. Each Sleep() tried
  to schedule another thread, which called Sleep(), which recursed into another
  `_run_thread_slice` — Python stack overflow ("maximum recursion depth exceeded").
  Fixed: `_cooperative_sleep` / `_cooperative_sleep_ex` guard on `state.is_running_thread`.

**Other fixes:**

- `MessageBoxA` returned `IDOK=1` for `MB_YESNO` — the fullscreen-prompt branch in
  `Platform_SysStartUp` checks `if (result == 7)` (IDNO) for windowed mode. `1` is
  neither yes nor no, so the game's mode flag was indeterminate. Now returns `IDYES=6`.
- `GetModuleHandleA("KERNEL32")` returned NULL — stub-only DLLs have no `LoadedDLL`
  entry. Added `Win32Handlers.get_stub_dll_handle()` returning the first handler's
  trampoline address as a stable non-NULL handle. 11 unit tests added.
- `GetDeviceCaps` had no logging — calls were invisible in `LOG_CATEGORIES=handlers`.
  Added hdc + capability-name debug line. Confirmed BITSPIXEL returns 32 correctly.
- `CreateWindowExA` with `MAKEINTATOM(109)` did a name-table lookup for `"#109"` which
  was never inserted. Fixed to look up by atom value in `_classes` directly.

**Ghidra analysis this session:**

- `Platform_SysStartUp` (0x006b13b0): full decompile — fullscreen prompt, dx8z load,
  `_THRASH_setvideomode`, window init sequence, thread creation order
- `func_0x0040490d` → JMP → `GameSetup_LoadOptions` (0x0055d280): NOT window creation
- `_THRASH_createwindow` (dx8z.dll 0x60001760): internal dx8z allocation, no Win32
- `FUN_60003920` (dx8z.dll): D3D device enumeration, BITSPIXEL check, COM vtable calls

## 2026-04-12 — v0.9.0 — CRT init session: 10K → 6.77M steps

**Step count**: 10,765 → 6,772,724 (657× increase in one session).

Game is now printing OutputDebugString messages from authlogin/INET startup code:
`Filter thread started` and `Creating INET Message Object`.

**New modules:**
- `tew/api/char_type.py` — CT_CTYPE1 lookup table, WideMemory Protocol, GetStringTypeArgs DTO
- `tew/api/lc_map.py` — LCMapFlags IntFlag enum, LCMapStringArgs DTO, case conversion
- `tew/api/win32_errors.py` — Win32Error IntEnum (winerror.h constants)
- `tew/api/ini_file.py` — full INI parser + reader + writer (parse_ini, read/write_profile_string/section)
- `tew/api/version_handlers.py` — GetFileVersionInfoSizeA family (returns 0: no version resource)
- `tew/api/wininet_handlers.py` — full WinINet HTTP stack via http.client; forwarding to localhost

**Blockers cleared (in order):**
1. `GetStringTypeW` — CT_CTYPE1 lookup table for Unicode classification
2. `LCMapStringW` — LCMAP_LOWERCASE/UPPERCASE via codepoint mapping
3. `GetModuleFileNameA` — CRTState.exe_path + reverse_translate_path()
4. `HeapValidate` — checks heap_handles set, returns TRUE
5. `GetLastError` / `SetLastError` — last_error field on CRTState
6. `GetPrivateProfileStringA/IntA` — real INI parsing; file read via find_file_ci
7. `WritePrivateProfileStringA/SectionA` — real INI file write
8. `GetFileVersionInfoSizeA/A`, `VerQueryValueA` — version_handlers.py
9. Full WinINet stack — InternetOpen/Connect/HttpSend/Read/QueryInfo/Close
10. `_initterm` / `_initterm_e` — **calls back into guest CPU**; real static initializer dispatch via THREAD_SENTINEL
11. `VirtualProtect` — flat memory model; returns PAGE_EXECUTE_READWRITE, TRUE
12. `GlobalMemoryStatus` — plausible 256 MB values
13. `SetEnvironmentVariableA/W`, `GetEnvironmentVariableA/W` — module-level _env_vars dict

**Infrastructure improvements:**
- `flush=True` on all logger print() calls — fixes INFO/ERROR ordering in piped output
- `diagnose_halt()` in exception_diagnostics.py — prints registers + 16-slot stack walk on any halt
- `diagnose_halt` wired into run_exe.py post-loop alongside existing diagnose_fault

**Tests:**
- 302 tests, all passing
- New test files: test_char_type.py (46), test_lc_map.py (34), test_module_filename.py (14),
  test_heap.py (6), test_last_error.py (15), test_ini_file.py (47)

**Next blocker:** `DuplicateHandle`

---

## 2026-04-12 — v0.8.0 — GetStringTypeW implemented

**Blocker cleared** — `GetStringTypeW` now has a real implementation.

**New files:**
- `tew/api/char_type.py` — CT_CTYPE1 lookup table for ASCII (U+0000–U+007F),
  `Ctype1` IntFlag enum, `GetStringTypeArgs` frozen dataclass (DTO),
  `WideMemory` Protocol, `classify_ctype1()`, `classify_wide_string()`
- `tests/unit/kernel/test_char_type.py` — 46 tests; all pass

**Changed:**
- `kernel32_handlers.py` — `GetStringTypeW` handler replaced; halts loudly
  if called with CT_CTYPE2 or CT_CTYPE3 (not needed by MCO, implement on demand)

**Design notes:**
- CT_CTYPE2 and CT_CTYPE3 are NOT implemented — handler halts if called.
  This is intentional: fail loudly rather than return silent garbage.
- `WideMemory` Protocol makes `classify_wide_string` testable without the
  full emulator setup — `Memory` satisfies it structurally.

---

## 2026-04-11 — v0.7.0 — Code hygiene session

**No step count change** — 10,765 steps, same GetStringTypeW halt.

**Structural fixes:**
- `SavedCPUState` moved from `tew/api/_state.py` to `tew/hardware/cpu.py`
  (it's a CPU type; hardware layer shouldn't depend on api layer)
- `save_state()` / `restore_state()` added as methods on `CPU`
  (they touch CPU internals directly; belong on the class)
- Duplicate `_save_cpu_state` / `_restore_cpu_state` functions removed from
  `kernel32_handlers.py` and `kernel32_io.py` — was an exact copy-paste

**Linter / tooling:**
- `ruff` installed and configured in `pyproject.toml`
- `requires-python` bumped to `>=3.12` (venv runs 3.13; fixes f-string
  backslash escape syntax errors that were latent on 3.11)
- 48 unused imports removed (auto-fix)
- `_vt` in `oleaut32_handlers.py` prefixed with `_` to signal intentional discard
- E701/E702 (compact one-liners in opcode tables) suppressed globally —
  intentional style for condition code dispatch and paired flag sets

**Removed:**
- `main.py` — predated the port, `run_exe.py` is the entry point

**Deferred (documented in memory):**
- Split `_state.py` into DTOs vs runtime state
- Split large handler files by concept (heap, file, thread, sync)
  rather than by DLL name

---

## 2026-04-01 — v0.6.0 — Initial Python port committed

Full Python port of the TypeScript emulator. Runs 10,765 steps through
CRT initialization, halts at `GetStringTypeW`. Includes:
- CPU core, full opcode implementations, x87 FPU
- PE/DLL loader with base relocations and IAT patching
- Win32/CRT/D3D8/User32/OleAut32/Advapi32 handler stubs
- Cooperative thread scheduler
- Unit test suite (140 tests)
