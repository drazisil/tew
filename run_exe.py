"""run_exe.py — Boot and run a Win32 PE executable in the x86-32 emulator.

Usage:
    python run_exe.py [path/to/game.exe]
    python run_exe.py --install-dir /path/to/game [--exe MCity_d.exe]

If no path is given, reads 'exePath' from emulator.json in the current directory.

Environment variables:
    LOG_LEVEL=trace|debug|info|warn|error   (default: info)
    LOG_CATEGORIES=startup,cpu,handlers,...  (default: all)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from os.path import dirname

from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import ZigCPU as CPU, EAX, ECX, EDX, EBX, ESP, EBP, ESI, EDI, REG_NAMES, FatalHaltError
from tew.kernel.kernel_structures import KernelStructures
from tew.kernel.exception_diagnostics import diagnose_fault, diagnose_halt, _dump_cpu_state
from tew.pe.exe_file import EXEFile
from tew.api.win32_handlers import Win32Handlers
from tew.api.crt_handlers import register_crt_handlers, patch_crt_internals
from tew.api.pe_resources import PEResources
from tew.api._state import EmulatorConfig
from tew.api.nt_handlers import register_nt_handlers
from tew.kernel.seh import dispatch_exception, STATUS_ACCESS_VIOLATION
from tew.logger import logger, set_thread_id_provider, WARN


# ── Parse arguments ───────────────────────────────────────────────────────────

_parser = argparse.ArgumentParser(
    description="Run a Win32 PE executable in the x86-32 emulator",
    add_help=True,
)
_parser.add_argument(
    "positional_exe", nargs="?", metavar="exe",
    help="Path to the .exe to run (overrides emulator.json exePath)",
)
_parser.add_argument(
    "--install-dir", metavar="DIR",
    help="Game install directory. Sets C:\\ path mapping and working directory. "
         "Replaces the need for pathMappings in emulator.json.",
)
_parser.add_argument(
    "--exe", metavar="EXE", dest="exe_flag",
    help="Exe filename or relative path within --install-dir (e.g. MCity_d.exe or "
         "3dSetup/3DSetup.exe). Ignored if --install-dir is not given.",
)
_args = _parser.parse_args()

# ── Resolve install dir, config, and exe path — all before any chdir ─────────

_repo_dir = os.getcwd()

# Load emulator.json from the repo dir for baseline config + fallback exe path.
_cfg: dict = {}
try:
    with open(os.path.join(_repo_dir, "emulator.json"), "r", encoding="utf-8") as _f:
        _cfg = json.load(_f)
except Exception:
    pass

install_dir: str | None = None
if _args.install_dir:
    install_dir = os.path.abspath(_args.install_dir)
    if not os.path.isdir(install_dir):
        raise SystemExit(f"--install-dir does not exist: {install_dir}")

# Build EmulatorConfig: --install-dir overrides C:\\ mapping; otherwise use emulator.json.
if install_dir:
    _path_mappings: dict[str, str] = {"c:/": install_dir.rstrip("/") + "/"}
else:
    _raw = _cfg.get("pathMappings", {})
    _path_mappings = {
        k.replace("\\", "/").lower(): v
        for k, v in _raw.items()
        if not k.startswith("_")
    }
_emulator_config = EmulatorConfig(
    path_mappings=_path_mappings,
    interactive_on_missing_file=_cfg.get("interactiveOnMissingFile") is True,
)

# Resolve exe_path.
exe_path: str = ""
if _args.positional_exe:
    exe_path = _args.positional_exe
elif install_dir:
    if _args.exe_flag:
        exe_path = os.path.join(install_dir, _args.exe_flag)
    else:
        # Fall back to the basename from emulator.json exePath, looked up in install_dir.
        _json_exe = _cfg.get("exePath", "")
        if _json_exe:
            exe_path = os.path.join(install_dir, os.path.basename(_json_exe))
        else:
            _exes = [f for f in os.listdir(install_dir) if f.lower().endswith(".exe")]
            if len(_exes) == 1:
                exe_path = os.path.join(install_dir, _exes[0])
            elif _exes:
                raise SystemExit(
                    f"Multiple .exe files in {install_dir}: {_exes}\n"
                    f"Use --exe to specify one."
                )
            else:
                raise SystemExit(f"No .exe found in {install_dir}")
else:
    exe_path = _cfg.get("exePath", "")

if not exe_path:
    raise SystemExit(
        "No exe path specified. Pass as a positional argument, use --install-dir/--exe, "
        "or set 'exePath' in emulator.json."
    )

# chdir to install_dir so relative game file writes land there, not in the repo.
if install_dir:
    os.chdir(install_dir)

# ── Load PE ───────────────────────────────────────────────────────────────────

logger.info("startup", f"=== Loading PE File: {exe_path} ===")
exe = EXEFile(exe_path, [])

logger.debug("startup", f"Entry point RVA: 0x{exe.optional_header.address_of_entry_point:x}")
logger.debug("startup", f"Image base: 0x{exe.optional_header.image_base:x}")
logger.debug("startup", f"Sections: {len(exe.section_headers)}")

entry_rva = exe.optional_header.address_of_entry_point
entry_section = next(
    (s for s in exe.section_headers
     if s.virtual_address <= entry_rva < s.virtual_address + s.virtual_size),
    None,
)
logger.debug("startup", f"Entry point in section: {entry_section.name if entry_section else 'NOT FOUND'}")

# ── Create emulator ───────────────────────────────────────────────────────────
# 2 GB flat address space (Linux lazily commits pages; physical RAM usage is
# proportional to what the game actually writes, not the reservation size).

mem = Memory(2 * 1024 * 1024 * 1024)
cpu = CPU(mem)
# Real Windows never maps the first 64KB of address space ("the null page")
# in any 32-bit process. Some guest code (MCity_d.exe's own anti-debug
# self-test, _CLayer_DetectDebugger) depends on a genuine access violation
# at a low address to detect whether a debugger is attached -- without this,
# that self-test can never fault, so its "no debugger" correction path
# never runs, leaving _Nfs_DebuggerIsPresent stuck at its wrong default.
# Off by default at the Zig-core level (bare CpuState tests rely on
# address-0-based tiny buffers); the real emulator always wants it on.
cpu.enable_null_page_guard()

kernel_structures = KernelStructures(mem)
cpu.kernel_structures = kernel_structures

exe.import_resolver.set_memory(mem)

# DLL search paths: application directory first (mirrors Windows loader behavior),
# then any additional directories (e.g. dgVoodoo d3d8 shim).
exe.import_resolver.add_dll_search_path(dirname(exe_path))
exe.import_resolver.add_dll_search_path("/data/Downloads/rayman_d3d8")

exe.import_resolver.build_iat_map(exe.import_table, exe.optional_header.image_base)

# ── Register Win32 stubs ──────────────────────────────────────────────────────

win32_handlers = Win32Handlers(mem)
_dll_loader_ref = exe.import_resolver.get_dll_loader()
crt_state = register_crt_handlers(
    win32_handlers, mem, _dll_loader_ref,
    config=_emulator_config, registry_dir=_repo_dir,
)
crt_state.exe_path = exe_path   # used by GetModuleFileNameA
set_thread_id_provider(crt_state.tls_current_thread_id)

# Attach PE resources so dialog templates and bitmap controls can be loaded
with open(exe_path, "rb") as _f:
    _pe_resources = PEResources(_f.read())
crt_state.pe_resources = _pe_resources
crt_state.window_manager.set_pe_resources(_pe_resources)

# ── Unattended boot: auto-answer the two interactive prompts MCity_d.exe's
# ── startup flow shows before real gameplay begins, so a run doesn't sit
# ── blocked on real mouse/keyboard input. (A third window appears in this
# ── same stretch of boot -- dialog resource 106, an untitled splash bitmap
# ── with no buttons -- but it dismisses itself and needs no hook.)

_LOGIN_CONTINUE_ID = 0x0001
_IDNO = 7

def _auto_click_login_continue(wm, dlg_hwnd):
    """Dialog 114 ("Motor City Online Login"): username/password are
    already sourced from registry.json by the game itself, so only the
    Continue click is needed."""
    entry = wm.get_window(dlg_hwnd)
    if entry is None or entry.title != "Motor City Online Login":
        wm.set_dialog_step_hook(_auto_click_login_continue)
        return
    wm.click_control(dlg_hwnd, _LOGIN_CONTINUE_ID)

def _auto_decline_fullscreen_prompt(caption, text, u_type):
    """MB_YESNO "Do you want to run Motor City Online full screen?"
    (FUN_006b13b0) -- default to windowed."""
    if "full screen" in text:
        return _IDNO
    return None

crt_state.window_manager.set_dialog_step_hook(_auto_click_login_continue)
crt_state.window_manager.set_messagebox_hook(_auto_decline_fullscreen_prompt)

# `timeout N ... run_exe.py` (the standard way this project's debugging
# sessions bound a run) sends SIGTERM on expiry -- Python's default handler
# for that just kills the process immediately, skipping every line below
# including window_manager.shutdown()'s SDL_Quit(). Every run that ever hit
# its time limit this way left an orphaned SDL window/GL context behind
# (confirmed 2026-08-24: no signal handler existed anywhere in this file).
# Mirrors the normal post-run path's own os._exit() (not sys.exit()) for the
# same NVIDIA-driver-atexit-crash reason documented there.
def _handle_termination_signal(signum, frame):
    logger.error("startup", f"Received signal {signum} -- shutting down SDL2 before exit")
    try:
        crt_state.window_manager.shutdown()
    except Exception as e:
        logger.error("startup", f"SDL2 shutdown during signal handling failed: {e}")
    sys.stdout.flush()
    os._exit(1)

signal.signal(signal.SIGTERM, _handle_termination_signal)
signal.signal(signal.SIGINT, _handle_termination_signal)

win32_handlers.install(cpu)
register_nt_handlers(win32_handlers.nt_dispatcher)

# ── Load sections ─────────────────────────────────────────────────────────────

logger.info("startup", "=== Loading Sections ===")
total_loaded = 0
for section in exe.section_headers:
    vaddr = exe.optional_header.image_base + section.virtual_address
    logger.info(
        "startup",
        f"  {section.name:<8} @ 0x{vaddr:08x}"
        f" (raw:{len(section.data)} virt:{section.virtual_size})",
    )
    if section.data:
        mem.load(vaddr, section.data)
        total_loaded += len(section.data)
    if section.virtual_size > len(section.data):
        uninit = section.virtual_size - len(section.data)
        logger.debug("startup", f"    Note: {uninit} bytes uninitialized (auto-zeroed)")

logger.debug("startup", f"Total loaded: {total_loaded} bytes")

# ── Write IAT entries and patch CRT internals ─────────────────────────────────

exe.import_resolver.write_iat_handlers(
    mem, exe.optional_header.image_base, exe.import_table, win32_handlers
)
patch_crt_internals(win32_handlers, mem, crt_state)

# ── Set up initial CPU state ──────────────────────────────────────────────────

if not entry_section:
    raise SystemExit(
        f"Entry point RVA 0x{entry_rva:x} not in any section!"
    )

eip = (exe.optional_header.image_base + entry_rva) & 0xFFFFFFFF
logger.debug(
    "startup",
    f"Setting EIP = imageBase(0x{exe.optional_header.image_base:x})"
    f" + entryRVA(0x{entry_rva:x}) = 0x{eip:08x}",
)
cpu.eip = eip

# Sentinel HLT so mainCRTStartup return hits a clean halt
SENTINEL_ADDR = 0x001FF000
mem.write8(SENTINEL_ADDR, 0xF4)  # HLT

mem_size = mem.size
stack_base = mem_size - 16
stack_limit = mem_size - (128 * 1024)
cpu.regs[ESP] = stack_base & 0xFFFFFFFF
cpu.regs[EBP] = stack_base & 0xFFFFFFFF

cpu.regs[ESP] -= 4
mem.write32(cpu.regs[ESP], SENTINEL_ADDR)

kernel_structures.initialize_kernel_structures(stack_base, stack_limit, crt_state.process_heap)

# ── Build valid EIP range table ───────────────────────────────────────────────

valid_ranges: list[tuple[int, int, str]] = []

for section in exe.section_headers:
    start = exe.optional_header.image_base + section.virtual_address
    end = start + section.virtual_size
    valid_ranges.append((start, end, f"exe:{section.name}"))

logger.debug("startup", "=== DLL Address Mappings ===")
for mapping in exe.import_resolver.get_address_mappings():
    valid_ranges.append((mapping["base_address"], mapping["end_address"], f"dll:{mapping['dll_name']}"))
    logger.debug(
        "startup",
        f"  0x{mapping['base_address']:08x}-0x{mapping['end_address']:08x} {mapping['dll_name']}",
    )

# MAX_HANDLERS (4096) × HANDLER_SIZE (32) = 0x20000 bytes from HANDLER_BASE
valid_ranges.append((0x00200000, 0x00220000, "stubs"))
valid_ranges.append((SENTINEL_ADDR, SENTINEL_ADDR + 1, "sentinel-hlt"))
valid_ranges.append((0x001FE000, 0x001FE004, "thread-sentinel"))
valid_ranges.append((0x08000000, 0x09000000, "thread-stacks"))


# O(1) EIP validity check: map 4KB page numbers to region names.
# Built once from valid_ranges; dynamically-loaded DLLs fall through to
# is_in_dll_range which handles them via the import resolver.
_eip_page_to_name: dict[int, str] = {}
for _vr_start, _vr_end, _vr_name in valid_ranges:
    for _page in range(_vr_start >> 12, (_vr_end + 0xFFF) >> 12):
        _eip_page_to_name[_page] = _vr_name


def is_valid_eip(eip: int) -> str | None:
    name = _eip_page_to_name.get(eip >> 12)
    if name:
        return name
    if exe.import_resolver.is_in_dll_range(eip):
        return "dll:dynamic"
    return None


# ── Debugger: breakpoints and logpoints ──────────────────────────────────────
#
# Breakpoints halt execution before the target instruction and call a Python
# handler(cpu, mem).  Resume is automatic.
#
# CAUTION -- cpu.add_breakpoint (cpu/src/kernel.zig) backs this with a FIXED
# 8-slot table (bp_table: [8]u32) and silently no-ops once full -- no error,
# no log, the breakpoint just never fires. Live-verified 2026-08-16: 3 new
# probes added past the 8th registration never fired at all, no diagnostic
# anywhere. Keep total register_breakpoint() calls in this file at <= 8, or
# unregister_breakpoint() something first -- there is currently no code-level
# guard against silently exceeding this.
#
# Logpoints fire a C callback inline from the Zig hot loop (no halt, near-zero
# overhead).  The callback signature is:
#   fn(eip: u32, regs: ptr[u32 x8], memory: ptr[u8], memory_size: usize)
# Use mem.read32() / cpu.regs[] from the *Python* handler for readable access;
# use the raw pointers only when you need speed.

_bp_handlers: dict = {}   # eip -> callable(cpu, mem)

def register_breakpoint(eip: int, handler) -> None:
    _bp_handlers[eip] = handler
    cpu.add_breakpoint(eip)

def unregister_breakpoint(eip: int) -> None:
    _bp_handlers.pop(eip, None)
    cpu.remove_breakpoint(eip)

def _dispatch_breakpoint() -> None:
    if not cpu.breakpoint_hit:
        return
    hit_eip = cpu.breakpoint_hit_eip
    cpu.clear_breakpoint_hit()            # unhalt + clear flag
    h = _bp_handlers.get(hit_eip)
    keep = True
    if h:
        result = h(cpu, mem)
        if result is False:               # handler returns False → one-shot, remove
            keep = False
    # Execute the halted instruction once without re-triggering the breakpoint.
    cpu.remove_breakpoint(hit_eip)
    cpu.run(1)
    if keep and hit_eip in _bp_handlers:
        cpu.add_breakpoint(hit_eip)


# ── TEMPORARY: execution-history capture (2026-08-07, cont'd again x5) ─────────
# The DAO/Jet OpenDatabase chain (GetFileType, LockFile, GetComputerNameA)
# is fully resolved -- see changelog.md. New blocker: a fatal_halt on
# tid=1000 (main thread) via nfile.c's FILE_allocateop "FILE SYSTEM NOT
# INITIALIZED" assertion (real check: `if (DAT_020e2ccc == 0)
# abortmessage(...)`, confirmed a real fatal path). Molly's read: this is
# likely a downstream symptom of dbcode.c's own Dbcode_AtExit() (fires
# right before it, posting an exit request via DBRequestQ_Put), not an
# independent nfile.c bug -- and Molly correctly caught that a naive
# Python logpoint on the hot-path FILE_allocateop was adding enough real
# wall-clock overhead to measurably slow virtual-time progress (the
# scheduler's timer heartbeat is wall-clock-driven), which cost the run
# 500M steps' worth of budget before ever reaching the real failure.
#
# Using the real execution-history capture layer instead (native Zig
# hooks, see cpu/src/history/capture.zig and cpu_zig.py's
# enable_history_capture_clickhouse) -- exactly the right tool for "what
# wrote DAT_020e2ccc last, and from where" instead of more hand-rolled
# logpoints, IF this investigation resumes. ClickHouse already running
# (history-poc's docker-compose, port 8123, user default/password poc,
# schema from history-poc/schema.sql).
#
# DISABLED-FROM-START for now (2026-08-07): confirmed live that enabling
# this from step 0 is NOT lightweight in practice -- it hooks every single
# memory write and every register/EIP/EFLAGS change for the whole run, and
# the periodic HTTP flush to ClickHouse can't keep up with that volume (a
# run stalled at 83s of virtual time after 2+ minutes of real wall-clock
# time, RSS climbing past 2.3GB as the unflushed buffer piled up in
# memory).
#
# 2026-08-21: re-enabled, but gated to a narrow window instead of the
# whole run -- see _HISTORY_CAPTURE_START_STEP/_HISTORY_CAPTURE_STOP_STEP
# below, checked inside the step loop. This investigation (msjet35.dll's
# per-session collation-cache field, DAT_7a9362c0[session]+0x2c0, never
# observed being written by static analysis or by capturing the last
# ~900k steps before the crash) needs exactly "what wrote address X last,
# and from where". fileio logging confirms Online.mdb's header is read
# very early (~step 1-2M, well before the first 5M-step heartbeat) --
# capturing just that early window (database-open) instead of the whole
# run avoids the overhead blowup hit last time.
#
# DISABLED-FROM-START again (2026-08-24): that investigation is done and
# the 0-9M window was left on, capturing nearly the whole run to
# ClickHouse over HTTP -- real added wall-clock drag on every run since,
# stacked on top of this session's separate SDL2-under-Xwayland slowness.
# Not needed for the current RtlUnwind investigation (a live breakpoint
# probe is enough, same as prior sessions used) -- _HISTORY_CAPTURE_DONE
# starting True skips the trigger check below for the whole run via the
# same completion flag the step-window logic already sets when it's done;
# flip back to False (and pick a fresh narrow window) if a future
# investigation genuinely needs the instruction-level trace.
_HISTORY_CAPTURE_ENABLED = False
_HISTORY_CAPTURE_DONE = True
_HISTORY_CAPTURE_START_STEP = 0
_HISTORY_CAPTURE_STOP_STEP = 9_000_000


# B-tree page-metadata/next-page-resolution/tail-page investigation
# (TEMPORARY 2026-08-09/2026-08-15) removed 2026-08-16: fully RESOLVED, see
# status_archive.md "2026-08-16" -- the page-34/field=1903 symptom was
# tew's ReadFile ignoring OVERLAPPED.Offset (fixed, see kernel32_io.py),
# not a real B-tree/page-resolution bug. FUN_7a8870a2/FUN_7a879d3b/
# FUN_7a848399 were all real, accurately-decompiled, but not the actual
# cause -- their probes (_fun_7a8870a2_entry, _fun_7a879d3b_entry,
# _fun_7a848399_entry, and the 3 breakpoint slots they occupied) are gone;
# re-derive from status_archive.md if this area needs revisiting.


# DAO-3075/CreateQueryDef investigation (expsrv.dll / VBAGetExprSrv /
# query-rewrite probes) removed 2026-08-17: concluded for now, see
# status.md/changelog.md "2026-08-17" for the full trace. Not resumed
# tonight -- pivoting to the scheduler-to-Zig port (see
# ~/.claude/plans/vast-drifting-pike.md) instead, motivated by the
# starvation incident found while tracing this. All 8 breakpoint slots
# freed back to baseline.

# Scheduler-to-Zig port Stage 0 baseline-capture probes (2026-08-17) removed
# 2026-08-17: the port completed all 6 stages, each verified against this
# baseline's checkpoints (see changelog.md's "2026-08-17 (cont'd)"-through-
# "(cont'd x7)" entries); the port is done and `tew/kernel/scheduler.py` has
# been deleted. All breakpoint slots freed back to baseline.

# DAO-3075 investigation (msjet35.dll SELECT-list lookahead scanner, the
# whole block that lived here through 2026-08-20) removed 2026-08-25: fully
# RESOLVED and verified (see changelog.md "2026-08-20", zig build test +
# pytest all green, live re-run confirmed the SELECT-list column boundary
# computes correctly and the run progresses into real game-data loading).
# All 8 breakpoint slots freed; re-derive from status_archive.md if this
# area needs revisiting.


# DBParamQuery::DoQuery's real OpenRecordset vtable call (0x0099778e, CALL
# DWORD PTR [ECX+0x8C]) -- confirmed once, live: EAX(HRESULT)=0x0/S_OK, see
# status.md. Recordset creation itself is not the bug; probe removed.

# 2026-08-25: a deep hand-disassembly trace through msjet35.dll's internal
# column-enumeration machinery (sentinel dispatcher -> inner dispatcher ->
# FUN_7a879476 cursor-walk -> FUN_7a879561 scan -> FUN_7a8436ac append ->
# FUN_7a842fd1 per-table-source state machine -> its vtable-dispatched
# caller) pinned the literal write site of a count field that gates the
# scan (live-confirmed via a watchpoint: write of byte 0x2 at runtime
# 0x180036d7), and definitively ruled out a buffer-capacity/allocation-size
# bug as the cause (the buffer is a correctly page-sized, properly-tracked
# VirtualAlloc region with 1900/4096 bytes still free -- tew's HeapAlloc and
# VirtualAlloc both honor real requested sizes, confirmed by code and live
# evidence). But the specific field being traced (this+8+0x38, "table_col_
# count") turned out to never exceed 2 for ANY query in a full run, strongly
# suggesting it isn't actually "SELECT-list column count" at all -- probably
# a different, small-cardinality piece of per-source metadata (e.g. index or
# relationship count). That thread was abandoned as likely-wrong rather than
# pushed further; all its probes (colcount-field, inner-vtable,
# tablesource-advance, dispatch-selector, colappend-caller, advance-loop)
# were removed rather than left disabled. Full detail in status.md's
# "cont'd x13" entry. Redirected upstream instead: find the actual
# SELECT-list compile/bind step for this stored QueryDef and trace its
# function-boundary inputs and outputs (params in, return value out) rather
# than continuing to hand-disassemble unrecognized internal jump-table code.

# Next hop past the openrecordset probe above: GetValue (0x0040da3f exe thunk
# -> real body 0x008fb8e0) is what actually reads Fields.Count -- but GetValue
# is a shared __cdecl function called for every column of every DAO query in
# the whole run, not just ours. First pass tried filtering on the target
# recordset pointer captured from DoQuery's own frame (local_1c) -- abandoned:
# Ghidra's local_XX names don't map 1:1 to real EBP-XX offsets in this
# SEH-instrumented function (hand-decoded proof: local_14, the `this` pointer,
# is really at [EBP-0x10], not [EBP-0x14] as its name implies), so the
# captured pointer read as 0xcccccccc (RTC stack poison) every run. Rather
# than keep hand-decoding stack offsets (the exact failure mode
# status_archive.md already flags as unreliable for this codebase), filter
# instead on GetValue's own __cdecl args, which are ABI-fixed regardless of
# Ghidra's naming: param_1 (recordset) [EBP+8], param_2 (col) [EBP+0xC],
# param_3 (row) [EBP+0x10]. Gate logging on Count<=2 -- the known bad value
# is 1, and any legitimate query should have well more than 2 fields, so this
# isolates our query without flooding the log with every column of every
# other DAO query in the run.
# Real vtable call addresses AND out-param offsets confirmed by hand-decoding
# raw bytes (not decompiler names -- Ghidra's piStack_24/asStack_30 are off by
# a consistent -4 from their real EBP offsets in this function, same shift as
# local_14 above):
#   get_Fields  (Recordset vtable+0xB4): CALL @ 0x008fbdd7, out-param
#     (Fields collection ptr) really at [EBP-0x20], HRESULT in EAX at
#     the real post-call address 0x008fbde4.
#   get_Count   (Fields collection vtable+0x1C): CALL @ 0x008fbf86, out-param
#     (short) really at [EBP-0x2C], HRESULT in EAX at 0x008fbf90.
_fields_by_ebp = {}  # ebp (this GetValue call's frame) -> fields collection ptr

def _fields_probe(cpu, mem):
    from tew.hardware.cpu_zig import EAX, EBP
    ebp = cpu.regs[EBP]
    fields_ptr = mem.read32((ebp - 0x20) & 0xFFFFFFFF)  # real offset, hand-decoded -- see note above
    _fields_by_ebp[ebp] = (fields_ptr, cpu.regs[EAX])
    return True  # never disable -- need this for every GetValue call
register_breakpoint(0x008fbde4, _fields_probe)

def _fields_count_probe(cpu, mem):
    # Once per distinct recordset's first fetch (col==0/row==0): sanity-check
    # rec_base's raw Count storage. Cheap landmark, kept for regression.
    from tew.hardware.cpu_zig import EBP
    ebp = cpu.regs[EBP]
    count = mem.read16((ebp - 0x2c) & 0xFFFFFFFF)  # real offset, hand-decoded -- see note above
    if count <= 2:
        col = mem.read32((ebp + 0xC) & 0xFFFFFFFF)
        row = mem.read32((ebp + 0x10) & 0xFFFFFFFF)
        if col == 0 and row == 0:
            fields_ptr, _ = _fields_by_ebp.get(ebp, (None, None))
            inner_ptr = mem.read32((fields_ptr + 8) & 0xFFFFFFFF) if fields_ptr else 0
            rec_base = mem.read32((inner_ptr + 8) & 0xFFFFFFFF) if inner_ptr else 0
            raw_count = mem.read16((rec_base + 0x2C) & 0xFFFFFFFF) if rec_base else None
            logger.error("cpu", f"[fields-count-probe] rec_base=0x{rec_base:x} raw_count={raw_count}")
    return True  # never disable -- want every low-Count occurrence, not just the first
register_breakpoint(0x008fbf90, _fields_count_probe)

# 2026-08-25: ROOT CAUSE FOUND AND FIXED. Fields.Count reading 1 instead of
# 10 for 3-table-join QueryDefs (e.g. StockVehicleAttributes_SelectAll2) was
# never a marshaling/memory-corruption bug -- it was tew's CompareStringA/W
# handler (tew/api/kernel32_io.py, _locale_is_valid) rejecting
# LOCALE_USER_DEFAULT (0x0400), a completely standard Windows locale
# sentinel that real CompareStringA resolves and succeeds on. dao350.dll's
# internal field-name dedup check (FUN_044da868 -> FUN_044d1d53 ->
# FUN_044c6126 -> FUN_044c6284) calls CompareStringA(LOCALE_USER_DEFAULT,
# ...) for every column-name comparison during collection building (371
# calls in one run, ALL rejected). dao350's own switch on the result maps
# failure (EAX=0, "invalid locale") to the same code path as CSTR_EQUAL --
# harmless on real Windows (which essentially never fails this call), but
# under tew's rejection it turned every single name comparison into an
# unconditional "equal", so every column after the first got misidentified
# as a duplicate and silently skipped. Fixed by resolving LOCALE_USER_DEFAULT
# (0x0400) and LOCALE_SYSTEM_DEFAULT (0x0800) to 0x0409 before validation.
# Confirmed live: the prefClass assert below no longer fires, and the run
# progresses ~16s further than it ever had before hitting an unrelated new
# blocker (a Zig-level integer-overflow panic in cpu/src/core.zig's
# readRmFixed32, op8B/MOV r32,r/m32 -- a different bug, not yet chased).
#
# Independently re-derives the correct branch decision in Python for the
# captured local_1c value, to also verify tew's own CMP/JGE signed-comparison
# flag computation isn't itself a bug (same general class as the already-
# fixed 0x66-prefix INC/DEC issue) rather than the value being wrong.
def _prefclass_assert_probe(cpu, mem):
    from tew.hardware.cpu_zig import EBP
    ebp = cpu.regs[EBP]
    raw = mem.read32((ebp - 0x18) & 0xFFFFFFFF)
    signed = raw - 0x100000000 if raw >= 0x80000000 else raw
    should_assert = signed < 0 or signed >= 8
    logger.error("cpu", f"[prefclass-assert-probe] local_1c=0x{raw:x} ({signed}) real_answer:{'ASSERT' if should_assert else 'ok'}")
    return True  # log every occurrence -- fires once per row
# Originally broken at 0x005baedb (the second CMP, local_1c>=8) -- moved to
# 0x005baed5 (the FIRST cmp, local_1c<0) after live-confirming the probe
# never fired there: when local_1c is negative, the first CMP's JL already
# jumps straight to the assert block, so the second CMP never executes at
# all. That's itself informative (consistent with a negative value), but
# means this address only fires unconditionally.
register_breakpoint(0x005baed5, _prefclass_assert_probe)

# 2026-08-25 (cont'd x14): clean function-boundary trace, redirected upstream
# per Molly's request -- print params in / return out for the actual COM
# OpenRecordset call, rather than more scattered internal-state probes.
# DBParamQuery::DoQuery's real dispatch is CALL DWORD PTR [ECX+0x8C] at
# 0x0099778e (confirmed via raw bytes) -- a SINGLE dereference, not a
# classic [obj]->vtable->[vtable+off] double hop. First pass wrongly read
# this as double indirection (real_target came out as an address inside no
# known DLL range, 4-byte-misaligned -- both red flags) -- re-checked the
# raw bytes and the setup chain right before the call (MOV EDX,[EBP-0x10];
# MOV EAX,[EDX+4]; MOV ECX,[EBP-0x10]; MOV EDX,[ECX+4]; MOV ECX,[EDX]; PUSH
# EAX), which shows ECX is ALREADY the resolved flat vtable-array pointer by
# the time it reaches the CALL (dao350.dll uses C-style flat dispatch
# tables, not obj+vtable-pointer C++ style) -- so [ECX+0x8C] is a direct
# index into that array. Fixed: single dereference.
def _openrecordset_call_boundary_probe(cpu, mem):
    from tew.hardware.cpu_zig import ECX, ESP
    vtable = cpu.regs[ECX]
    real_target = mem.read32((vtable + 0x8C) & 0xFFFFFFFF) if vtable else 0
    esp = cpu.regs[ESP]
    stack_args = [mem.read32((esp + i * 4) & 0xFFFFFFFF) for i in range(6)]
    # real_target (FUN_0449c844, decompiled) is a forwarding thunk:
    # (**(code**)(**(int**)(param_1+8)+0x7c))(*(int**)(param_1+8), ...) --
    # resolved here too, no second breakpoint needed since param_1 is
    # already known (stack_args[0], what becomes the callee's first stack
    # arg once the CALL pushes its own return address).
    param_1 = stack_args[0] if stack_args else 0
    inner = mem.read32((param_1 + 8) & 0xFFFFFFFF) if param_1 else 0
    inner_vtable = mem.read32(inner & 0xFFFFFFFF) if inner else 0
    real_target2 = mem.read32((inner_vtable + 0x7c) & 0xFFFFFFFF) if inner_vtable else 0
    logger.error("cpu", f"[openrecordset-call-boundary] vtable(ECX)=0x{vtable:x} real_target=0x{real_target:x} param_1=0x{param_1:x} inner=0x{inner:x} inner_vtable=0x{inner_vtable:x} real_target2=0x{real_target2:x} stack[ESP..ESP+0x14]={[hex(v) for v in stack_args]}")
    return True  # DoQuery's OpenRecordset branch can fire more than once per run
# Answered 2026-08-25 (cont'd x14): real_target2 = 0x449833e, dao350.dll's
# actual OpenRecordset (confirmed via its own debug string literal,
# "s_OpenRecordset_044e480c") -- no longer registered, its address is fixed
# knowledge now, not something that needs re-verifying live each run.
# register_breakpoint(0x0099778e, _openrecordset_call_boundary_probe)

# FUN_0449833e (dao350.dll's real OpenRecordset, decompiled) makes 3 real
# vtable calls in sequence: +0x80 (no args, error-checked), +0x84 (this,
# &out_param -- the actual bind/cursor-creation step), then +0x188 on a
# nested object. dao350.dll is never relocated (static == runtime), so all
# these addresses are usable directly. Answered 2026-08-25 (cont'd x15) via
# paired entry/return probes at the +0x84 call site (0x044983d0/0x044983d6):
# this=0x7067adc, return=0 (success), out_param_val is a real live
# recordset/cursor address -- clean at this boundary, no longer registered
# (see status.md's "cont'd x15" entry for the full chain).

# Its real target, FUN_04498a79 (decompiled), constructs a fresh cursor
# object then calls FUN_0447c475(new_obj, 0, "x", &out, *(this+0x28),
# local_8) -- "x" is a constant literal from the caller, ruled out as
# query-specific by inspection. local_8 comes from FUN_044c9ecd(&local_8,
# *(this+8)), called BEFORE any object construction -- the genuinely
# query-specific input. FUN_044c9ecd (decompiled) reads two strings, at
# *(param_2+0x84) and *(param_2+0x8c) off its own param_2 (= *(outer
# this+8)), and resolves them into a bound object -- shaped like a
# name-resolution step, plausibly where the query name gets looked up.
# Molly, 2026-08-25: print the real params in and the real return/out value
# out, and read the actual string content, before treating this as a
# confirmed boundary -- not just assuming the shape implies the behavior.
# Real call site hand-found via get_function_instructions (cdecl, 2 pushes
# right before it, right-to-left -- param_2 pushed first/further, param_1
# (&local_8) pushed last/closest to the call): CALL @ 0x04498aa6, return
# point @ 0x04498aab.
# FUN_044c9ecd (name-resolution shape) fully investigated and ruled out as
# our bug site, 2026-08-25 (cont'd x15/x16): its own real output (local_8)
# differs correctly per query (0x70729c2 for ours, 0x707265f for an
# unrelated Brand query) regardless of what's in the shared string-staging
# buffer at param_2+0x84/+0x8c -- that buffer is read by both calls via a
# real REPNE SCASB (the compiled form of strlen) but its content, whatever
# it's for, doesn't feed local_8's per-query distinctness. An apparent
# contradiction (entry probe read the buffer as null while a later probe at
# the real MOV EBP,[EBX+0x84] instruction, 0x044c9f4d, showed a valid
# pointer) turned out to be a probe-timing artifact, not a null-page-guard/
# fault-suppression bug: the field genuinely gets populated by earlier logic
# inside this same function, between entry and that later point. No further
# lead here -- see status.md for the full trace if this needs revisiting.

# Molly's catch: Dbcode_Fetch's binding table (DAT_020be5d0, gBinding[] in
# real source) is accessed via a FIXED ABSOLUTE address, not through param_1
# (the recordset) -- confirming it's a single GLOBAL 64-slot table shared
# across the whole process, not scoped per-recordset. Dbcode_Fetch's do-loop
# walks all 64 slots and calls GetValue(OUR_recordset, gBinding[i].m_col, row)
# for every active one -- if a stale/concurrent binding from a DIFFERENT
# query is sitting in an earlier slot, this could fail (and Dbcode_Fetch
# bails with "success") for a column that has nothing to do with our actual
# 10-column query. Real GetValue call site hand-found at 0x008f9c82; cdecl
# right-to-left push order means at the CALL instruction (before it executes)
# [ESP+4] is the column index argument (row is [ESP+0], recordset is [ESP+8]).
# Confirmed 2026-08-25 this probe (dbcode-fetch-col-probe, was here) only
# ever fires for the 4 OTHER, unrelated recordsets -- our identity-confirmed
# target's recordset never appears in Dbcode_Fetch's own calls across an
# entire run. That function serves a different code path (see status.md's
# "cont'd x11" entry: our target actually goes through DBRecordset::Fetch /
# GetVariant / DBBinding::Set, not this one). Removed to free the slot.

# 2026-08-25: the column-loop instrumentation that lived here (breakpoints
# on FUN_044dac2b's per-column loop @ 0x044dacc0/0x044dacc5, the dedup-
# lookup TEST @ 0x044da8a8, and the pre-add struct dump @ 0x044dad7b) traced
# the bug to its root: FUN_044da868's field-name dedup check calls into
# CompareStringA(LOCALE_USER_DEFAULT, ...), which tew was rejecting as an
# invalid locale -- see the note above _prefclass_assert_probe, and the
# fix in tew/api/kernel32_io.py's _locale_is_valid. Removed now that it's
# fixed and confirmed live; full chain preserved in memory/status.md.

# The msjet35.dll FUN_7a84269c EBP-chain probe that was here (2026-08-25)
# answered its question -- correlated by exact timestamp against
# _column_loop_probe, our target's 3 FUN_044d5200 calls all resolve to
# session_idx=2038 -> session_obj=0x15010d40 -> real_target 0x7a847105,
# confirmed same-run (not a stale cross-run reuse). Removed once that
# answer was in hand -- it fires on every call system-wide (hundreds per
# run) and was pure noise once its one useful data point was extracted.
# Static target for the next session: 0x7a847105 (raw byte decode in
# progress, not yet a Ghidra-recognized function -- see status.md).

# The collation-locale probe that was here (2026-08-22) answered its
# question -- confirmed MCity_d.exe's CreateDatabase call for Tmp.MDB
# passes an empty Locale connect-string -- and has been removed. Root
# cause fixed in tew/api/kernel32_io.py (CompareStringA/W now validate the
# locale id instead of always succeeding); see memory/status.md.

# The EBP-restoration probe that was here (2026-08-23) answered its
# question -- confirmed the RtlUnwind EBP-restoration fix (tew/kernel/
# seh.py) DOES take effect correctly, but the crash isn't actually caused
# by stale EBP: the second __except_handler3 invocation's own return
# address (0x011f3b90) is inside a data/string-table region, not real
# code -- meaning by that point the CPU is already executing from a wrong
# EIP entirely. Traced to the thread's own outermost stack frame having an
# invalid/garbage "return address" for when its own entry-point function
# eventually returns -- a different, deeper gap than EBP restoration. See
# memory/status.md.


# ── Run loop ──────────────────────────────────────────────────────────────────

logger.info("startup", "=== Starting Emulation ===")

# TEMPORARY (2026-08-07): diagnostic-only cProfile hook, gated behind
# TEW_PROFILE so it's a no-op unless explicitly requested. Molly asked to
# find out why the run is dragging (DSOUND serve thread missing its 10ms
# deadline by 30-100x, every single tick). `-m cProfile -o file` doesn't
# work here: this script deliberately calls os._exit() at the very end
# (see that line's own comment -- avoids a real NVIDIA-driver atexit
# segfault), which is a hard C-level exit that skips normal Python
# shutdown/atexit entirely, so cProfile never gets a chance to dump stats.
# Dumping explicitly, synchronously, right before that os._exit() call
# sidesteps the problem. Remove this block once the investigation is done.
import cProfile as _cProfile
_TEW_PROFILE = os.environ.get("TEW_PROFILE")
_profiler = _cProfile.Profile() if _TEW_PROFILE else None
if _profiler is not None:
    _profiler.enable()

MAX_STEPS = int(os.environ.get("TEW_MAX_STEPS", "500000000"))
# Debug-only: watch a specific address for writes (cpu/src/core.zig's single
# hardware-style watchpoint -- fires on every matching write, unconditionally
# overwriting watchpoint_eip/watchpoint_val each time, so what's reported at
# halt is whichever write happened most recently to that address). Existing
# postmortem report is further down (search "WATCHPOINT HIT"). Only useful
# with TEW_FIXED_HEARTBEAT_MS also set, since the heap address to watch has
# to be known in advance from a prior run and heap layout isn't stable
# without it.
_TEW_WATCH_ADDR = os.environ.get("TEW_WATCH_ADDR")
_tew_watch_addr_int = None
if _TEW_WATCH_ADDR is not None:
    _tew_watch_addr_int = int(_TEW_WATCH_ADDR, 16)
    cpu.set_watchpoint(_tew_watch_addr_int)
# Steps per batch (also the virtual-clock tick interval).
# _TIMER_waitticks spins without Sleep/SleepEx so multimedia timers never fire
# from the normal SleepEx path.  Advancing the clock here lets due callbacks fire.
_TIMER_HEARTBEAT_INTERVAL = 100_000
# Upper bound on wall-clock ms creditable to the virtual clock in a single
# heartbeat. Measured real throughput is ~220k-250k instr/sec, i.e. ~400-450ms
# per 100k-instruction batch, so this is >10x normal headroom. Anything beyond
# it (debugger pause, OS suspend/resume, a slow future breakpoint handler) is
# capped rather than credited in full, so one heartbeat can't inject a huge
# virtual-time jump that fires a backlog of periodic timers/timeouts at once.
_TIMER_HEARTBEAT_MAX_MS = 5_000
# Debug-only determinism knob (2026-08-25): normally elapsed_ms below is real
# host wall-clock time (time.monotonic()), which feeds the Zig scheduler's
# tick() -- cpu/src/scheduler.zig's tick() wakes sleeping/timed-out threads
# once virtual_ticks_ms crosses their deadline, and preemptSlice() then
# round-robins among whichever threads are .ready *at that instant*. Real
# host timing jitter (other processes, disk I/O, even Ghidra/JVM competing
# for CPU as observed repeatedly this session) shifts exactly which
# instruction-count quantum a sleeping thread first becomes ready in,
# reordering cross-thread LoadLibraryA calls (hence DLL load base -- see
# dll_loader.py's deterministic-but-history-dependent _find_available_base)
# and, suspected but not yet confirmed, heap allocation order feeding
# msjet35.dll's own per-query cursor state -- see status.md's "cont'd x12"
# entry (run-to-run non-determinism in Recordset.Fields.Count's root cause).
# Since tew only ever runs one guest process, forcing every run to schedule
# identically has no downside here. Set to pin elapsed_ms to a fixed value
# instead of real elapsed wall-clock time, for reproducible debugging runs.
_TEW_FIXED_HEARTBEAT_MS = os.environ.get("TEW_FIXED_HEARTBEAT_MS")
if _TEW_FIXED_HEARTBEAT_MS is not None:
    _TEW_FIXED_HEARTBEAT_MS = int(_TEW_FIXED_HEARTBEAT_MS)

step_count = 0
last_valid_step = 0
last_valid_eip = 0
last_valid_region = ""
detected_runaway = False

# Resolved once on first heartbeat call.
_pending_timers = None
_invoke_emulated_proc_fn = None
_get_dialog_sentinel_fn = None
_time_callback_event_set = None
_event_handle_cls = None
_heartbeat_count = 0


def _run_timer_heartbeat() -> None:
    global _heartbeat_count
    global _pending_timers, _invoke_emulated_proc_fn, _get_dialog_sentinel_fn
    global _time_callback_event_set, _event_handle_cls
    global _last_heartbeat_wall_time
    _heartbeat_count += 1
    if _pending_timers is None:
        from tew.api.win32_handlers import pending_timers as _pt, _TIME_CALLBACK_EVENT_SET as _tces
        from tew.api.user32_handlers import _invoke_emulated_proc as _iep, _get_dialog_sentinel as _gds
        from tew.api._state import EventHandle as _eh
        _pending_timers = _pt
        _invoke_emulated_proc_fn = _iep
        _get_dialog_sentinel_fn = _gds
        _time_callback_event_set = _tces
        _event_handle_cls = _eh
    if _TEW_FIXED_HEARTBEAT_MS is not None:
        elapsed_ms = _TEW_FIXED_HEARTBEAT_MS
    else:
        now = time.monotonic()
        elapsed_ms = int((now - _last_heartbeat_wall_time) * 1000)
        elapsed_ms = max(1, min(elapsed_ms, _TIMER_HEARTBEAT_MAX_MS))
        _last_heartbeat_wall_time = now
    crt_state.scheduler.tick(elapsed_ms, mem)
    if not _pending_timers:
        return
    due = [t for t in list(_pending_timers.values()) if t.due_at <= crt_state.virtual_ticks_ms]
    if not due:
        return
    sentinel = _get_dialog_sentinel_fn(crt_state, mem)
    for timer in due:
        if timer.fu_event & _time_callback_event_set:
            obj = crt_state.kernel_handle_map.get(timer.cb_addr)
            if isinstance(obj, _event_handle_cls):
                obj.signaled = True
                crt_state.scheduler.unblock_handle(timer.cb_addr)
        elif timer.cb_addr != 0:
            _invoke_emulated_proc_fn(cpu, mem, timer.cb_addr, [timer.id, 0, timer.dw_user, 0, 0], sentinel)
        if timer.period_ms > 0:
            timer.due_at += timer.period_ms
        else:
            _pending_timers.pop(timer.id, None)


_heartbeat_countdown = _TIMER_HEARTBEAT_INTERVAL
# Captured here (not earlier) so DLL loading / breakpoint setup above isn't
# credited as elapsed emulation time for the first heartbeat.
_last_heartbeat_wall_time = time.monotonic()
_sample_countdown = 1_000_000
_progress_countdown = 5_000_000

try:
    while not cpu.halted and step_count < MAX_STEPS and not detected_runaway:
        if not _HISTORY_CAPTURE_ENABLED and not _HISTORY_CAPTURE_DONE and step_count >= _HISTORY_CAPTURE_START_STEP:
            cpu.enable_history_capture_clickhouse("http://localhost:8123", "default", "poc")
            _HISTORY_CAPTURE_ENABLED = True
            logger.always(WARN, "startup", f"[history] ClickHouse capture enabled at step {step_count:,}, run_id={cpu.run_id}")
        elif _HISTORY_CAPTURE_ENABLED and step_count >= _HISTORY_CAPTURE_STOP_STEP:
            cpu.flush_history_capture()
            cpu.disable_history_capture()
            _HISTORY_CAPTURE_ENABLED = False
            _HISTORY_CAPTURE_DONE = True
            logger.always(WARN, "startup", f"[history] ClickHouse capture disabled at step {step_count:,} (window closed)")
        eip_before = cpu.eip
        batch = min(_TIMER_HEARTBEAT_INTERVAL, MAX_STEPS - step_count)
        cpu.run(batch)
        step_count += batch

        if cpu.faulted:
            # Give the game's own SEH chain a chance to handle this before
            # giving up -- see tew/kernel/seh.py. Real Windows would report
            # this as an access violation; that's the only fault shape this
            # CPU core currently produces (see core.zig's memRead8/memWrite8),
            # so it's the honest default rather than a guess.
            fault_eip = cpu.eip & 0xFFFFFFFF
            logger.always(WARN, "seh", f"CPU fault at EIP=0x{fault_eip:08x} -- attempting SEH dispatch")
            handled = dispatch_exception(cpu, mem, STATUS_ACCESS_VIOLATION, fault_eip)
            if handled:
                logger.info("seh", f"fault at 0x{fault_eip:08x} handled by game's own SEH chain -- resuming")
                cpu.faulted = False
            else:
                logger.error("seh", f"fault at 0x{fault_eip:08x} unhandled by SEH chain -- halting")
                try:
                    raw = [f"{mem.read8(fault_eip + i):02x}" for i in range(16)]
                    logger.error("seh", f"  Bytes at fault EIP: {' '.join(raw)}")
                except Exception:
                    logger.error("seh", "  Bytes at fault EIP: (out of bounds)")
                # Previously this branch only logged and let the loop keep
                # running -- cpu.faulted isn't part of the while condition,
                # so an unhandled fault silently kept executing from
                # whatever (corrupted) state the Zig core left EIP/regs in,
                # and cpu_is_faulted() clears itself on the next successful
                # cpu_run() call, so the *next* iteration's `if cpu.faulted`
                # check would already read False. The eventual diagnostic
                # (if any) reflected wherever execution wandered to much
                # later, not this fault -- confirmed live: this exact bug
                # made a real fault at 0x15035655 (inside MSJET35.DLL)
                # report as an unrelated halt at 0x00688c69 (back in
                # MCity_d.exe, ~94ms and two more DLL loads later).
                #
                # cpu.halted = True alone is NOT enough either -- confirmed
                # live: it only gates the *next* iteration's while-condition
                # check, but the rest of *this* iteration's body still runs
                # first, including crt_state.scheduler.preempt_slice() a few
                # lines below, which cooperatively context-switches to and
                # executes *other* threads. That ran two more DLL loads and
                # several more COM calls after the "halting" log line, on a
                # CPU already left in an unrecovered fault state, which is
                # what produced a second, worse native crash (a glibc
                # buffer-overflow abort/core dump) this same session. Must
                # break out of the loop immediately, not just flag it.
                cpu.halted = True
                break

        if _bp_handlers:
            _dispatch_breakpoint()
        # 2026-08-25: the existing watchpoint report (search "WATCHPOINT
        # HIT" below) only checks once, postmortem -- core.zig's memWrite8
        # unconditionally overwrites watchpoint_eip/watchpoint_val on every
        # matching write, so a run with multiple writes to the watched
        # address only ever surfaces the LAST one. Confirmed live: a single
        # postmortem check on rec_base+0x2C (Fields.Count's real storage)
        # only showed a late decrement (1->0, dao350.dll cleanup/teardown,
        # AFTER Fields.Count was already read as 1) -- the earlier write
        # that actually SET it to 1 in the first place was overwritten in
        # the single-slot state before the run ended. Polling actively here
        # instead: log every hit, then re-arm (set_watchpoint on the same
        # address also clears watchpoint_hit) so later writes aren't missed.
        if _tew_watch_addr_int is not None and cpu.watchpoint_hit:
            logger.error("cpu", f"[watchpoint-hit-live] EIP=0x{cpu.watchpoint_eip:08x} written=0x{cpu.watchpoint_val:02x} step={step_count}")
            cpu.set_watchpoint(_tew_watch_addr_int)
            cpu.halted = False
        crt_state.scheduler.preempt_slice(cpu, mem)

        _heartbeat_countdown -= batch
        if _heartbeat_countdown <= 0:
            _heartbeat_countdown = _TIMER_HEARTBEAT_INTERVAL
            _run_timer_heartbeat()

        _sample_countdown -= batch
        if _sample_countdown <= 0:
            _sample_countdown = 1_000_000
            logger.debug(
                "watch",
                f"[EIP sample @ {step_count}] EIP=0x{cpu.eip & 0xFFFFFFFF:08x}"
                f" ESP=0x{cpu.regs[ESP] & 0xFFFFFFFF:08x}",
            )

        _progress_countdown -= batch
        if _progress_countdown <= 0:
            _progress_countdown = 5_000_000
            eip_now = cpu.eip & 0xFFFFFFFF
            stub_note = ""
            if 0x00200000 <= eip_now < 0x00220000:
                recent = win32_handlers._call_log[-8:]
                stub_note = f" calls={recent}"
            logger.debug(
                "startup",
                f"[alive] step={step_count:,} EIP=0x{eip_now:08x}{stub_note}"
                f" vtime={crt_state.virtual_ticks_ms}ms",
            )

        region = is_valid_eip(cpu.eip)
        if region:
            last_valid_step = step_count
            last_valid_eip = eip_before
            last_valid_region = region
        elif not detected_runaway and step_count > 100:
            # EIP left every region tew recognizes as valid code -- real
            # Windows would raise STATUS_ACCESS_VIOLATION the moment the CPU
            # tried to fetch from a page it never mapped executable (same
            # honest-default reasoning as the cpu.faulted branch above; tew's
            # flat memory model has no page protection to fault on this
            # itself, so this heuristic is standing in for that fetch-fault).
            # Route it through the exact same real-SEH-dispatch pipeline
            # instead of a bespoke diagnostic dump: give the game's own
            # fs:[0] handler chain a real chance to recover (some of these
            # may be genuine, handled game-code exceptions we'd otherwise
            # never see resolve), and on failure fall through to the same
            # richer diagnose_halt() EBP-chain-walked report every other
            # unhandled halt already gets via the post-run block below,
            # rather than this block's own shallower ad-hoc dump.
            runaway_eip = cpu.eip & 0xFFFFFFFF
            logger.always(
                WARN,
                "seh",
                f"RUNAWAY at step {step_count}, EIP=0x{runaway_eip:08x} (last valid "
                f"step {last_valid_step}, EIP=0x{last_valid_eip & 0xFFFFFFFF:08x} in "
                f"{last_valid_region}) -- attempting SEH dispatch",
            )
            handled = dispatch_exception(cpu, mem, STATUS_ACCESS_VIOLATION, runaway_eip)
            if handled:
                logger.info("seh", f"runaway at 0x{runaway_eip:08x} handled by game's own SEH chain -- resuming")
            else:
                logger.error("seh", f"runaway at 0x{runaway_eip:08x} unhandled by SEH chain -- halting")
                try:
                    raw = [f"{mem.read8(runaway_eip + i):02x}" for i in range(16)]
                    logger.error("seh", f"  Bytes at EIP: {' '.join(raw)}")
                except Exception:
                    logger.error("seh", "  Bytes at EIP: (out of bounds)")
                detected_runaway = True
                cpu.halted = True
                break
except FatalHaltError as e:
    # cpu.run()/cpu.step() (tew/hardware/cpu_zig.py) raise this the moment
    # cpu.fatal_halt newly becomes true anywhere in the call chain, however
    # deeply nested -- the single real "except" for the whole run, per the
    # design in memory/status.md. cpu.halted is guaranteed true by the time
    # this is caught, so the existing watchpoint/faulted/halted reporting
    # below (unchanged) already does the right thing once control falls
    # through -- no separate diagnostic call needed here.
    logger.error("cpu", f"Fatal halt: {e}")

if step_count >= MAX_STEPS:
    logger.warn("cpu", f"Execution limit reached ({MAX_STEPS} steps)")

# ── Post-run reporting ────────────────────────────────────────────────────────

if crt_state.fatal_dialogs:
    logger.error("startup", "=== Emulation Complete (NOT a clean exit) ===")
    logger.error(
        "startup",
        f"{len(crt_state.fatal_dialogs)} fatal (stop/hand-icon) dialog(s) fired"
        " during this run -- a voluntary ExitProcess afterward is the game"
        " aborting, not a successful run:",
    )
    for caption, text in crt_state.fatal_dialogs:
        logger.error("startup", f'  "{caption}": {text.splitlines()[0] if text else ""}')
else:
    logger.info("startup", "=== Emulation Complete (clean exit) ===")
logger.info("startup", f"Steps executed: {cpu._step_count}")

logger.debug("handlers", "--- Win32 Stub Call Log (last 50) ---")
for call in win32_handlers.get_call_log()[-50:]:
    logger.debug("handlers", f"  {call}")

if cpu.watchpoint_hit:
    logger.error("exception",
        f"WATCHPOINT HIT at EIP=0x{cpu.watchpoint_eip:08x}"
        f"  written=0x{cpu.watchpoint_val:02x}"
        f"  (first byte of write to watchpoint address)")
    diagnose_halt(cpu, exe.import_resolver)
elif cpu.faulted:
    diagnose_fault(cpu, exe.import_resolver)
elif cpu.halted:
    diagnose_halt(cpu, exe.import_resolver)

logger.info("startup", f"Final EIP: 0x{cpu.eip & 0xFFFFFFFF:08x}")

# Flush the execution-history capture buffer to ClickHouse before shutdown,
# and log the run_id so it's queryable -- see the capture-enable comment
# near cpu = CPU(mem) above. Without this flush, any buffered-but-not-yet-
# flushed events from the tail of the run would be lost on process exit.
# disable_history_capture() during cpu cleanup is a no-op if never enabled,
# but skip the flush/run_id logging entirely when capture is off so this
# doesn't print a misleading "flushed to ClickHouse" line for a run that
# never captured anything.
if _HISTORY_CAPTURE_ENABLED:
    history_run_id = cpu.run_id
    cpu.flush_history_capture()
    logger.info("startup", f"[history] run_id={history_run_id} flushed to ClickHouse (history_events table)")

# Tear down SDL2 (and any windows/renderers it owns) before exiting -- an
# implicit process exit with SDL2 still live left the NVIDIA driver's own
# atexit cleanup to run against a live GL/Vulkan-backed context, which
# segfaulted inside the driver itself (libnvidia-rtcore.so), not our code.
crt_state.window_manager.shutdown()

# os._exit() instead of sys.exit(): the NVIDIA driver registers its own
# atexit handlers (GLX, Vulkan RT) regardless of whether this process ever
# used them, and those handlers crash when run after SDL_Quit() has already
# closed the X11 connection they expect (libnvidia-rtcore.so segfault seen
# earlier; libGLX_nvidia.so -> libxcb FORTIFY abort seen 2026-08-02). Skipping
# exit()/__run_exit_handlers entirely avoids the whole class of driver-atexit
# bugs. logger.py flushes every line as it's printed, so no output is lost.
sys.stdout.flush()

if _profiler is not None:
    _profiler.disable()
    _profiler.dump_stats(_TEW_PROFILE)
    logger.info("startup", f"[profile] cProfile stats dumped to {_TEW_PROFILE}")

os._exit(1 if cpu.faulted else 0)
