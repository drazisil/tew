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
from tew.api._state import EmulatorConfig, read_cstring
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
crt_state = register_crt_handlers(
    win32_handlers, mem, exe.import_resolver.get_dll_loader(),
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

# TEMPORARY (2026-08-18): DAO-3075 thread. Full real call chain confirmed
# live, dao350.dll's Dbcode_CreateTmpQuery -> ... -> msjet35.dll's real
# parser (FUN_7a86756b, static addr, receives the SQL text directly). See
# status.md/changelog.md "2026-08-18 (cont'd)" for the full traced chain --
# earlier probes for each intermediate hop (vtable resolution, name vs SQL
# text disambiguation, the dao350->msjet35 bridge) have served their
# purpose and are removed; keeping only what's still useful going forward.
#
# msjet35.dll -- CONFIRMED: runtime = static - 0x65840000 for THIS DLL
# specifically (dao350.dll and the EXE are static==runtime, unaffected).
# Any new msjet35.dll breakpoint must add this delta.

def _try_read_str(mem, addr):
    if addr == 0 or addr > 0xFFFFFFFF:
        return None
    try:
        s = read_cstring(addr, mem)
        if s and all(32 <= ord(c) < 127 for c in s):
            return s
    except Exception:
        pass
    return None

# CORRECTED REWRITE POINT (2026-08-18): the deep-parser rewrite above (at
# FUN_7a86756b's own entry, patching just param_4/param_5) was INVALID --
# control test proved it: rewriting to "PartID FROM Part;" (a query already
# confirmed to compile cleanly by the original investigation, with NO
# rewrite at all) ALSO hit 0x271e through this path. That means something
# upstream of FUN_7a86756b (dao350.dll's own processing, or state set up
# earlier in the call chain) already depends on the ORIGINAL query's exact
# content by the time we reach this deep -- patching only the substring
# here produces a mismatched, invalid state, not a fair test. Molly's
# objection was right: "no function call ever recognized" cannot be true of
# real Jet 3.5, and it wasn't -- it was a broken test harness.
#
# Fixed: patch the STRING LITERAL ITSELF, in MCity_d.exe's own data section
# (0x011e0de4, confirmed via search_strings; EXE is static==runtime, no
# delta needed), at Dbcode_CreateTmpQuery's own entry (0x008fe4a0) -- the
# earliest point, before dao350.dll ever sees the text at all. This is the
# same rewrite point the original (pre-scheduler-detour) investigation used
# successfully.
_QUERY_LITERAL_ADDR = 0x011e0de4
_REWRITE_QUERY = False  # disabled by default -- set True + edit _NEW_QUERY to re-enable
_NEW_QUERY = b"SELECT Len(PartID) FROM Part;\x00"

def _source_rewrite_probe(cpu, mem):
    if not _REWRITE_QUERY:
        return
    for i, b in enumerate(_NEW_QUERY):
        mem.write8((_QUERY_LITERAL_ADDR + i) & 0xFFFFFFFF, b)
    logger.error("cpu",
        f"[source-rewrite-probe] rewrote query literal at 0x{_QUERY_LITERAL_ADDR:08x} "
        f"to {_NEW_QUERY[:-1]!r}")
register_breakpoint(0x008fe4a0, _source_rewrite_probe)

# The real parser entry (static 0x7a86756b, runtime 0x1502756b) -- gets the
# SQL text directly, confirmed live. Also stashes param_6's real address
# (the out error-code param, slot[5]) for the shared-exit probe below.
_param_6_addr = [0]  # mutable cell, set on entry
def _parser_probe(cpu, mem):
    args = [mem.read32((cpu.regs[ESP] + 4 + i * 4) & 0xFFFFFFFF) for i in range(6)]
    _param_6_addr[0] = args[5]
    ret_addr = mem.read32(cpu.regs[ESP] & 0xFFFFFFFF)
    logger.error("cpu", f"[parser-probe] FUN_7a86756b entered from ret=0x{ret_addr:08x}, raw args={[hex(a) for a in args]}")
    for i, w in enumerate(args):
        s = _try_read_str(mem, w)
        if s:
            logger.error("cpu", f"[parser-probe]   slot[{i}]=0x{w:08x} -> string: {s!r}")
    # Molly's question: is the byte right after ')' *really* a plain space
    # (0x20), or something else that only LOOKS like one in an ASCII
    # preview? Dump the raw hex of every byte in the text+length param_4
    # gives, no ASCII rendering to hide behind.
    text_ptr = args[3]
    length   = args[4]
    raw = bytes(mem.read8((text_ptr + i) & 0xFFFFFFFF) for i in range(length))
    logger.error("cpu", f"[parser-probe] raw hex ({length} bytes) = {raw.hex()}")
register_breakpoint(0x1502756b, _parser_probe)

# The parser sets one of ~15 distinct specific error codes (0x271b-0x2731)
# into *param_6 before jumping/calling to a SHARED exit point at static
# 0x7a8683bb (runtime 0x150283bb) -- catching that instead of every
# individual branch tells us exactly which specific error code fired.
def _exit_probe(cpu, mem):
    addr = _param_6_addr[0]
    code = mem.read32(addr & 0xFFFFFFFF) if addr else 0
    logger.error("cpu", f"[exit-probe] shared exit 0x150283bb hit, *param_6(0x{addr:08x})=0x{code:04x}")
    # Sanity check: does live memory at runtime 0x15029880 (FUN_7a869880's
    # supposed entry) match Ghidra's static bytes (83 ec 04 53 56 57 55 8b
    # 6c 24 1c)? If not, the loaded msjet35.dll build differs from the one
    # Ghidra analyzed and the whole address-mapping approach is unsound.
    live_bytes = bytes(mem.read8((0x15029880 + i) & 0xFFFFFFFF) for i in range(11))
    logger.error("cpu", f"[sanity-check] live bytes at 0x15029880 = {live_bytes.hex()} (expect 83ec04535657558b6c241c)")
register_breakpoint(0x150283bb, _exit_probe)

# 2026-08-19: DAO-3075 continued -- DispCallFunc did not fix it (see
# status.md, "cont'd x5"). Tracing one level deeper: FUN_7a8699a2 (runtime
# 0x150299a2) is what the identifier-token handler in the main parser calls
# to resolve "Max" once it's peeked a following '(' -- it internally calls
# FUN_7a87a1e4 (runtime 0x1503a1e4), which looks like the real
# "is this a known function name" lookup (-1 = not found -> falls through
# to plain column/identifier resolution, which is consistent with the
# observed 0x271e: the parser would then treat "Max" as a bare identifier
# operand, immediately followed by the '(' token as a SEPARATE operand --
# exactly "two operands in a row"). This probe reads EAX right after that
# call returns (0x15029a96, the instruction after CALL FUN_7a87a1e4 in
# FUN_7a8699a2) to confirm empirically whether the lookup is failing.
# NOTE: "caseD_1" (Ghidra's synthetic name for a jump-table fragment) does
# NOT correspond to switch-case *value* 0x1 -- get_function_calls on it
# showed calls matching case 0x9's logic instead (FUN_887ac4/FUN_8a5934/
# FUN_8c59b4), not case 0x1's identifier-handling calls. Ghidra names these
# fragments by position in the jump table, not by the C-level case label --
# unreliable for targeting a specific case body. Breaking on the real,
# non-split helper functions the decompile shows case 0x1 calling instead:
# FUN_7a869880 (allocates an identifier node, called unconditionally for
# any identifier token) and FUN_7a8699a2 (resolves it, called only when the
# '(' peek succeeds, isFuncCall=1). If FUN_7a869880 never fires either, the
# tokenizer isn't even classifying "Max" as token type 0x1 to begin with.
# Neither FUN_7a869880 nor FUN_7a8699a2 fired even once for the whole run
# -- "Max" isn't reaching case 0x1's identifier handling at all. Going
# fully empirical instead of guessing more case addresses: log the actual
# token-type value the tokenizer (FUN_7a8685de) returns at each of its
# three call sites Ghidra couldn't attribute to a named function
# (get_references_to showed these as the "(none)" entries -- almost
# certainly the real main-loop/lookahead call sites, since the ones Ghidra
# DID attribute turned out to belong to specific case bodies like case
# 0x9's lookahead loop). Return address = call site + 5 (near CALL rel32).
# Confirmed via the entry probe above: every real main-loop call to
# FUN_7a8685de returns to the SAME address (0x1502777e, static 0x7a86777e)
# -- this is the actual `pbStack_17c = FUN_8685de(...)` call site right
# after LAB_7a86773e. Breaking there reads EAX = the real token type value
# the switch(pbStack_17c) dispatches on, for every token in the query.
# (token-type-probe fully mined earlier this session -- removed to free a
# slot for the paren-close-read verification below.)
# Hand-disasm of the paren-skip sub-loop's close predicted a specific call
# (0x7a866d05, return 0x7a866d0a) reads one more token ("AS", 0x105) right
# after ')' closes and jumps straight to LAB_7a866c87. Breakpointed 0x15026d0a
# to test directly: NEVER FIRED -- second consecutive wrong prediction from
# hand-disassembly this session (see status.md). Superseded below by a real
# single-instruction-step trace through the whole function instead of more
# hand-decoded predictions -- slot freed.

# Jump table at static 0x7a8684f0 (dumped live) confirms token type 1
# ("Max") really does dispatch to 0x7a8676fb (runtime 0x150276fb) -- same
# address as the case1-probe below, which already fired 3x for Max/PartID/
# AS with pcVar10={0,0} on the first hit (doesn't match the '\x03' bang
# check, so it should take the normal cVar3=='(' peek path). The character
# right after "Max" is genuinely '(' (confirmed via tokenizer-probe's
# preview). So FUN_7a869880 should fire -- re-testing fresh now that the
# address mapping confusion from caseD_1/case-0x9 is resolved.
# New hypothesis: ")" (case 0xa) sets DAT_7a93ab04=2 (not 0) at its own
# LAB_7a867b77 tail -- meaning the very next operand-group token (here,
# "AS", type 0x1) hits the SHARED "if(DAT_7a93ab04!=0) error 0x271e" check
# at the top of 0x7a8676fb BEFORE ever reaching case-1's own inner body
# (the peek/FUN_7a869880 path traced above) -- which would fully explain
# why those breakpoints never fire for the 3rd identifier ("AS") while
# still firing... except they ALSO never fired for "Max" itself (1st
# identifier, DAT_7a93ab04 confirmed 0 at entry). Reading DAT_7a93ab04
# directly (runtime 0x150fab04) at case1-probe's own entry point settles
# this for all 3 identifier hits in one shot.
def _dat_ab04_probe(cpu, mem):
    val = mem.read32(0x150fab04)
    # local_178 (the "if(local_178==0){...real resolution...} else
    # {piVar8=NULL;}" mode flag) lives at [ESP+0x20] at this exact point
    # in the prologue (confirmed via hand-disasm: "CMP [ESP+0x20],0" at
    # 0x7a867712, no push/pop between there and here) -- never actually
    # verified directly until now. If this is nonzero, "Max" takes the
    # TRIVIAL fall-through path (XOR EDI,EDI; push NULL operand) instead
    # of ever reaching real identifier resolution -- which would explain
    # every dead breakpoint downstream in one shot.
    local_178 = mem.read32((cpu.regs[ESP] + 0x20) & 0xFFFFFFFF)
    logger.error("cpu", f"[dat-ab04-probe] DAT_7a93ab04 = {val}, local_178 = {local_178} at case-0x1-group entry")
register_breakpoint(0x150276fb, _dat_ab04_probe)

# FUN_7a86756b's real caller for our query is FUN_7a855d02 (static
# 0x7a855d02, runtime 0x13015d02) -- looks like ODBC-style per-column
# metadata validation (types/precision/driver-info flags), calling the
# expression parser inside a `local_10 & 0x88`-gated block, seemingly to
# validate a column's default-value/validation-rule expression syntax --
# yet it's handed the ENTIRE remaining SELECT-list text in this run, not a
# per-column default expression. Two real callers of FUN_7a855d02 exist
# (FUN_7a855cc3 at 7a855cd1, FUN_7a90e276 at 7a90e2f7) -- reading the
# return address here identifies which one is actually live for this query.
# Neither FUN_7a858cef nor FUN_7a863500 fired, nor does FUN_7a8549b6 --
# that whole "SELECT keyword classifier" chain is confirmed NOT part of
# CreateQueryDef's real path (probably used by OpenRecordset/Execute
# instead, which genuinely need to disambiguate "saved query name vs
# literal SQL" -- CreateQueryDef always treats its argument as SQL, no
# disambiguation needed).
#
# Pivoting to a different, already-confirmed lead: dao350.dll's
# FUN_044d5e64 calls `(*DAT_044e5238)(param_2,param_3,param_4)` -- a
# function pointer stored in dao350.dll's own data section, called
# directly with the raw SQL text. This is very likely msjet35.dll's real
# "compile this SQL into a query" entry point. dao350.dll is confirmed
# static==runtime (no delta) -- read DAT_044e5238's live value (a raw
# memory read, doesn't need EIP to be there) to find exactly which
# msjet35.dll address it targets, then breakpoint the tokenizer/parser
# entry again to confirm reachability from there.
# Confirmed chain: DAT_044e5238 (ordinal 302, FUN_7a89de86) only threads
# the query NAME ("tmp") through -- catalog bookkeeping, not SQL. The real
# SQL-text path is dao350.dll's FUN_044d519b -> DAT_044e534c (ordinal 319,
# FUN_7a8ae64d, confirmed live with param_4 = 'SELECT Max(PartID) AS
# Expr1 FROM Part;') -> FUN_7a856c17, msjet35.dll's real top-level SQL
# statement compiler (flagged back in the pre-compaction investigation as
# "makes zero external calls"). FUN_7a856c17 tokenizes the FIRST token via
# FUN_7a85683d (a STATEMENT-level tokenizer, distinct from FUN_7a8685de,
# the EXPRESSION-level one this whole session has focused on) and
# dispatches to one of three named handlers by token code, or a generic
# fallback:
#   local_1c == 0x104 -> FUN_7a924264
#   local_1c == 0x10a -> FUN_7a86e301
#   local_1c == 0x10f -> FUN_7a92447e
#   else               -> FUN_7a866d2b (generic fallback)
# Check live which one actually fires for our real "SELECT ..." query --
# this is very likely THE function that's supposed to split the SELECT
# list into per-column expressions.
# None of the 3 named handlers fired -- and parser-probe still shows the
# SAME return address (0x15015f7d, FUN_7a855d02's call site) as every
# earlier run. Two live possibilities: (a) FUN_7a856c17 itself isn't
# actually reached (chain-tracing error, same pattern as before), or
# (b) it IS reached but "SELECT"'s token code isn't 0x104/0x10a/0x10f,
# meaning it goes through the generic fallback FUN_7a866d2b instead.
# Check both directly.
# DAT_7a86a940's table genuinely contains 0x105 (AS) as a real entry
# (confirmed via dump_bytes: 0x2c, 0x105, 0x118, 0x11c, 0x111, 0x132,
# 0x3b, 0x16, then 0-terminator) -- yet the lookahead scan overshoots to
# the terminator, meaning the live tokenizer isn't producing 0x105 when it
# reads "AS". FUN_7a866c6d's inner scan loop calls FUN_7a866b7c
# repeatedly (call site 0x7a866cec, inside the do-while) -- log every
# token this scan sees, with the read position, to find what "AS" really
# tokenizes to at the statement level.
# The captured token sequence (0x100, 0x29, 0x105, 0x100, 0x111, 0x100,
# 0x3b, 0x16) DOES include 0x105 (AS) and 0x111 (FROM) -- contradicting
# the "AS doesn't tokenize correctly at this level" hypothesis. But this
# breakpoint fires for every call through this one address regardless of
# which of possibly-multiple FUN_7a866c6d invocations it belongs to, so
# the sequence could be multiple calls interleaved. Check the ACTUAL
# RETURN VALUE FUN_7a866c6d hands back to its caller (FUN_7a86a5a7, call
# site 0x7a86a62a) for the "Max(PartID)" column specifically -- if it's
# 0x101 (success/rewound), its own matching logic worked and the bug is
# downstream in how the rewound position gets used.
# FUN_7a85683d (the real tokenizer) reads its cursor from param+0x10
# ("piVar1 = param_1+0x10; pbVar13 = *piVar1;"), NOT param+0x18. But
# FUN_7a866c6d's rewind only does "*(int*)(param_1+0x18) = iVar1" --
# resets the boundary-bookmark field the caller reads for param_2+0x80,
# but NOT the +0x10 cursor field the tokenizer actually reads from next.
# msjet35.dll uses stdcall (callee cleans up) -- by the return point the
# stack is already restored to pre-call state, so param_3 isn't at
# [ESP+0] there. Capture it at the CALL SITE instead (args still pushed,
# stashed for the return-point probe to use).
# 2026-08-20 (cont'd x3): get_function_instructions (real Ghidra listing,
# not hand-decoding) caught a whole loop my own byte-by-byte disassembly of
# FUN_7a866c6d had missed entirely (the JNZ at 0x7a866cc8 feeding back into
# the whitespace-trim call) -- and it still doesn't show jump targets, so it
# can't settle the real question either. Two hand-traced predictions had
# already been contradicted by live data this session. Molly asked directly
# whether the stack could be dropping/misaligning values across the scan's
# repeated FUN_7a866b7c calls -- test it for real instead of predicting:
# single-step cpu.step() one real instruction at a time from this CALL site
# to the confirmed return address (0x1502a62f), logging EIP/ESP/EAX/EBX/ECX
# every step. This is the actual control-flow graph as executed, and ESP's
# trajectory across every call/ret in the loop directly answers the stack
# question -- no prediction, no hand-decoding, no ambiguity about which
# branch was taken. LAB_7a866c87 (0x15026c87) sits inside the range being
# stepped through and is still a live breakpoint; Zig's bp_table halts
# BEFORE executing an instruction at an armed address, so leaving it armed
# would freeze the trace there without executing anything. Pull it for the
# duration and restore it after, regardless of how the loop exits.
_lookahead_param3 = [0]
_in_lookahead_scan = [False]
_lookahead_call_count = [0]
_LOOKAHEAD_RETURN_EIP = 0x1502a62f
_LOOKAHEAD_MATCHCHECK_EIP = 0x15026c87
def _lookahead_call_probe(cpu, mem):
    _lookahead_param3[0] = mem.read32(cpu.regs[ESP] & 0xFFFFFFFF)
    _in_lookahead_scan[0] = True
    call_idx = _lookahead_call_count[0]
    _lookahead_call_count[0] += 1

    entry_esp = cpu.regs[ESP] & 0xFFFFFFFF
    cpu.remove_breakpoint(_LOOKAHEAD_MATCHCHECK_EIP)
    # _dispatch_breakpoint only removes THIS bp (0x1502a62a) from Zig's
    # bp_table AFTER this handler returns -- while we're in here it's still
    # armed, so cpu.step() would just re-halt on it forever without
    # executing anything (caught live: EIP frozen at 0x1502a62a for the
    # entire 800-step budget on the first real run of this tracer). Pull it
    # too; _dispatch_breakpoint's own post-handler cleanup re-arms it since
    # `keep` stays True by default.
    cpu.remove_breakpoint(0x1502a62a)
    trace = []
    try:
        for i in range(100_000):
            cpu.step()
            eip = cpu.eip & 0xFFFFFFFF
            esp = cpu.regs[ESP] & 0xFFFFFFFF
            trace.append((i, eip, esp,
                          cpu.regs[EAX] & 0xFFFFFFFF,
                          cpu.regs[EBX] & 0xFFFFFFFF,
                          cpu.regs[ECX] & 0xFFFFFFFF))
            if eip == _LOOKAHEAD_RETURN_EIP:
                break
    finally:
        cpu.add_breakpoint(_LOOKAHEAD_MATCHCHECK_EIP)

    if not trace:
        logger.error("cpu", f"[singlestep-trace #{call_idx}] EMPTY -- cpu.step() produced no instructions")
        return
    reached_return = trace[-1][1] == _LOOKAHEAD_RETURN_EIP
    final_delta = trace[-1][2] - entry_esp
    logger.error("cpu", f"[singlestep-trace #{call_idx}] {len(trace)} instrs, entry_esp=0x{entry_esp:08x}, "
                         f"reached_return={reached_return}, final_eip=0x{trace[-1][1]:08x}, "
                         f"final_esp=0x{trace[-1][2]:08x} (delta_vs_entry={final_delta:+d})")
    # Full per-instruction body only for the first few calls and any call
    # whose stack didn't balance back to entry_esp exactly -- 1000+ calls at
    # up to 800 lines each would be unreadable otherwise, and an ESP
    # mismatch is exactly the signal this trace exists to catch.
    if call_idx < 5 or final_delta != 0:
        lines = [f"[singlestep-trace #{call_idx} detail]"]
        for i, eip, esp, eax, ebx, ecx in trace:
            lines.append(f"  #{i:3d} eip=0x{eip:08x} esp=0x{esp:08x} (d={esp - entry_esp:+d}) "
                         f"eax=0x{eax:08x} ebx=0x{ebx:08x} ecx=0x{ecx:08x}")
        logger.error("cpu", "\n".join(lines))
register_breakpoint(0x1502a62a, _lookahead_call_probe)  # static 0x7a86a62a (the CALL itself)

def _lookahead_result_probe(cpu, mem):
    from tew.hardware.cpu_zig import EAX
    _in_lookahead_scan[0] = False
    param3 = _lookahead_param3[0]
    cursor_10 = mem.read32((param3 + 0x10) & 0xFFFFFFFF)
    bookmark_18 = mem.read32((param3 + 0x18) & 0xFFFFFFFF)
    cursor_preview = bytes(mem.read8((cursor_10 + i) & 0xFFFFFFFF) for i in range(8))
    bookmark_preview = bytes(mem.read8((bookmark_18 + i) & 0xFFFFFFFF) for i in range(8))
    logger.error("cpu", f"[lookahead-result-probe] FUN_7a866c6d returned 0x{cpu.regs[EAX]:x}, param3=0x{param3:08x}, "
                         f"param+0x10(cursor)=0x{cursor_10:08x} {cursor_preview!r}, "
                         f"param+0x18(bookmark)=0x{bookmark_18:08x} {bookmark_preview!r}, "
                         f"diff={cursor_10 - bookmark_18}")
register_breakpoint(0x1502a62f, _lookahead_result_probe)  # static 0x7a86a62f (0x7a86a62a + 5)

# CORRECTED after hand-disassembly: 0x7a866cec+5 (the previous breakpoint)
# turned out to be inside the PAREN-SKIP section (reached via the
# `JZ 0x7a866ce7` branch at 0x7a866c85), NOT the main match-check loop's
# own token-read. The real "read next candidate token to compare against
# the table" call is at 0x7a866ca4 (CALL FUN_7a866b7c), return address
# 0x7a866ca9, immediately followed by `CMP EAX,0x16` -- THIS is what the
# match-check loop (LAB_7a866c87, 0x7a866c8d-0x7a866ca1: walks the table,
# CMP [ECX],EAX, JZ on match) actually compares against on each iteration.
# That confirmed the 0x7a866ca4 main-loop read only fires once (for the
# opening '('), meaning the rest of the sequence flows through the
# paren-skip sub-loop's shared fallthrough into LAB_7a866c87 instead.
# Rather than keep reconstructing control flow by hand, read the ACTUAL
# matched table entry directly: 0x7a866cae is the confirmed jump target
# for "real match found" (from the JNZ at 0x7a866ca1). ECX points at the
# matched table slot there -- *ECX tells us definitively which value
# (0x105/AS, or something else entirely) the match-check actually fired on.
# Confirmed: the match only ever fires on 0x16 (the terminator, table
# index 7), never on 0x105. Narrowing further -- trace EVERY entry into
# the shared match-check label LAB_7a866c87 (0x7a866c87 itself, reached
# either by direct fallthrough from the '(' check, or from the paren-skip
# sub-loop's fallthrough) with the real EAX value at that exact instant.
def _gated_scan_token_probe(cpu, mem):
    if not _in_lookahead_scan[0]:
        return
    from tew.hardware.cpu_zig import EAX
    logger.error("cpu", f"[matchcheck-entry-probe] LAB_7a866c87 entered, EAX=0x{cpu.regs[EAX]:x}")
register_breakpoint(0x15026c87, _gated_scan_token_probe)  # static 0x7a866c87 (LAB_7a866c87 itself)

# Three more hops of static-only reasoning (FUN_7a867064's comma-loop ->
# FUN_7a86713b per-column parse -> FUN_7a86727e -> FUN_7a856e4d) without
# any live confirmation -- stop and verify before going deeper, given the
# proven pattern this session of static assumptions turning out wrong.
# get_function_calls on FUN_7a866d2b precisely located the FUN_7a85683d
# call right after the rewind (call site 0x7a866e17, right after
# FUN_7a866f98 at 0x7a866dff) -- read EAX at the return address (+5) for
# the REAL token value, rather than continuing to guess at decompiled
# branch targets.
# get_function_calls on FUN_7a86a5a7 precisely located the FUN_7a85683d
# call right after the column boundary gets recorded (param_2+0x80/0x98 =
# param_3[6]/[8]) -- call site 0x7a86a684, checked against 0x105 in the
# decompile. Read the REAL token value here to see what actually comes
# after "Max(PartID)" at this parsing level, and whether it matches 0x105.
# (post-column-probe already confirmed: token here = 0x16, terminator --
# folded into the cursor/bookmark comparison above, probe removed to stay
# within the 8-slot budget.)

# Jump table at 0x7a8669ec, entry for token 0x167 (SELECT, confirmed the
# rewind re-reads SELECT itself) resolves to 0x7a86a5a7 -- read directly
# from live memory, not guessed. This decompiles as a real select-list
# comma-loop (handles "SELECT *", "table.*", records column boundaries at
# param_2+0x80/0x98, calls FUN_7a856e4d for one branch). Verify it's
# actually reached before trusting any more of its internal structure.
# (table-check already confirmed: live DAT_7a86a940 matches expected
# static bytes exactly -- relocation is NOT corrupting this table. Probe
# simplified back to entry-only to stay within the 8-slot budget.)
def _fun_86a5a7_probe(cpu, mem):
    logger.error("cpu", "[selectlist-probe] FUN_7a86a5a7 (real SELECT-list handler) entered")
# register_breakpoint(0x1502a5a7, _fun_86a5a7_probe)  # static 0x7a86a5a7 -- disabled 2026-08-22, slot free to repurpose (stale, per status.md: "all 8 slots are free to repurpose")

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
