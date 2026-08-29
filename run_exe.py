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
import ctypes
import json
import os
import signal
import sys
import time
from os.path import dirname

from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import ZigCPU as CPU, EAX, ECX, EDX, EBX, ESP, EBP, ESI, EDI, REG_NAMES, FatalHaltError
from tew.kernel.kernel_structures import KernelStructures
from tew.kernel.exception_diagnostics import diagnose_fault, diagnose_halt, _dump_cpu_state, _annotate_address
from tew.pe.exe_file import EXEFile
from tew.api.win32_handlers import Win32Handlers
from tew.api.crt_handlers import register_crt_handlers, patch_crt_internals
from tew.api.kernel32_handlers import _invoke_dependency_dllmain
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
    logger.error("startup", f"Failed to load emulator.json from {_repo_dir} -- using defaults")
    raise SystemExit(f"emulator.json not found in {_repo_dir} -- run from the repo root or pass --install-dir")

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

# DLL search paths: application directory first (mirrors Windows loader behavior).
# 2026-08-26: dropped the dgVoodoo/rayman_d3d8 search path (and deleted its
# d3d8.dll) -- tew's own d3d8_handlers.py registers Python handlers for
# every function the game actually calls (Direct3DCreate8, DebugSetMute,
# Validate*Shader), and register_handler always wins over a real DLL's own
# export (dll_loader.py's patch_iat_entry precedence). Loading dgVoodoo's
# real, Vista+-targeting d3d8.dll and running its real DllMain provided
# nothing the game uses, while costing an entire extra wave of Vista+-only
# missing-handler chasing (LoadLibraryExW, InitializeSListHead, CreateEventW,
# InitializeCriticalSectionEx, ...). Confirmed live: removing it entirely
# changes nothing about the run past this point. General rule going forward:
# DLL search paths should point into ~/.emu32 (period-correct, purpose-built
# for this project) -- copy a file in there if something new is ever
# genuinely needed, don't add an arbitrary ~/Downloads directory.
exe.import_resolver.add_dll_search_path(dirname(exe_path))
# Real, period-correct COM servers (oleaut32.dll, dao350.dll, ...) live here
# -- must be registered before build_iat_map, not just later inside
# register_ole32_handlers (ole32_handlers.py's own
# _KNOWN_COM_SERVER_DIR add_search_path call): build_iat_map does its own
# eager load_dll() for every DLL MCity_d.exe directly imports, and that
# result is cached into _iat_map permanently -- if the search path isn't
# registered yet, oleaut32.dll's real, direct-import IAT slots (e.g. an
# early ordinal-based BSTR alloc call, well before DAO/Jet loads it again
# later via a different path) silently resolve to nothing and fall through
# to an auto-generated fatal-halt stub instead of the genuinely correct,
# already-loaded-later real DLL. Found 2026-08-26 after removing the
# old oleaut32.dll Python handlers exposed the gap they'd been silently
# covering for.
exe.import_resolver.add_dll_search_path("/home/drazisil/.emu32/WINDOWS/System32")

# 2026-08-26: MCity_d.exe's own directly-imported real DLLs (d3d8.dll,
# oleaut32.dll, rpcrt4.dll, secur32.dll -- and their own real-DLL
# dependencies, e.g. ole32.dll, loaded recursively by build_iat_map's own
# load_dll calls) never ran their DllMain at all -- should_invoke_dependency_dllmain's
# own docstring documents this as a deliberate scoping choice when the
# on_dependency_loaded mechanism was first built (it only ever got wired up
# at runtime LoadLibraryA/CoGetClassObject call sites). Real, confirmed bug
# this caused: oleaut32.dll's own TlsAlloc() (called from its DllMain, via
# FUN_77121f36) never ran, so its module-global TLS slot stayed at
# TLS_OUT_OF_INDEXES, which made every real SysAllocString call fail (its
# lazy per-thread init bails out on the first TlsSetValue). Collecting the
# DLLs here instead of invoking DllMain inline: at this point in startup,
# these DLLs' own kernel32/user32/etc. IAT slots aren't patched with
# working Python handler stubs yet (that's write_iat_handlers/
# patch_crt_internals, below) -- calling DllMain this early would call
# through unresolved/zeroed IAT entries. _pending_dllmain_dlls ends up in
# correct dependency-before-dependent order for free, since it's populated
# via the same should_invoke_dependency_dllmain check used by the existing,
# already-proven-correct runtime dependency-DllMain path (a dependency's
# own load_dll call, and thus its own DllMain, always completes before its
# dependent's does).
_pending_dllmain_dlls: list = []
exe.import_resolver.build_iat_map(
    exe.import_table, exe.optional_header.image_base, _pending_dllmain_dlls.append
)

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

# Now that the main thread has a real stack (ESP/EBP above) and kernel
# structures (TEB/PEB) are initialized, it's safe to run the
# statically-imported real DLLs' DllMain(DLL_PROCESS_ATTACH) --
# _invoke_emulated_proc builds its nested call's stack frame "on top of the
# current stack" (see its own docstring), which needs a real ESP to build
# on top of; calling this any earlier (e.g. right after write_iat_handlers/
# patch_crt_internals above, before ESP is set) crashes with a write32
# bounds error from ESP still being 0. Still strictly before the guest
# entry point itself runs (cpu.eip was set above, but the step loop hasn't
# started yet), matching real Windows load-then-attach-then-run ordering.
# See the long comment at build_iat_map's call site for why this whole
# invocation is deferred this far rather than running inline there.
_failed_static_dllmains = [
    _pending_dll.name for _pending_dll in _pending_dllmain_dlls
    if not _invoke_dependency_dllmain(cpu, mem, crt_state, _pending_dll, _dll_loader_ref, win32_handlers)
]
if _failed_static_dllmains:
    logger.warn(
        "startup",
        f"DllMain(DLL_PROCESS_ATTACH) returned FALSE for: {', '.join(_failed_static_dllmains)} "
        "-- real LoadLibrary would treat this as a failed load; continuing anyway "
        "since these are direct EXE imports we can't simply not-load, but their exports "
        "may be unreliable from here on.",
    )

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
# register_breakpoint(0x008fbde4, _fields_probe)

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
# register_breakpoint(0x008fbf90, _fields_count_probe)

# 2026-08-25 (cont'd x16): tracing the expsrv.dll ESI=0xFFFFFFFF halt (see
# status.md's current entry). Static analysis (file-based E8 scan + manual
# vtable walk in expsrv.dll's .rdata, no live run needed for that part)
# confirmed MSJET35.DLL's CALL DWORD PTR [EAX+0x24] at static 0x7a8a1d84
# (runtime 0x18061d84 this session) targets expsrv.dll's FUN_0f9dd3d9
# (vtable base 0x0FA041C0, slot 9/offset 0x24 -- its stored pointer,
# 0xf9dd3d9, matches FUN_0f9dd3d9 exactly). What's NOT confirmable from the
# file alone: the live value of ECX at that call, which by push order becomes
# FUN_0f9dd3d9's arg3 ([EBP+0x10]), which FUN_0f9dd3d9 forwards unchanged as
# FUN_0f9dd9a7's param_2 -- the pointer that ends up 0xFFFFFFFF in ESI at the
# actual crash (static 0x0F9DD9E9, runtime 0x1a01d9e9). One-shot: confirms
# the vtable-slot hypothesis live (EAX/[EAX+0x24] should equal the runtime
# FUN_0f9dd3d9 address, 0x1a01d3d9) and captures ECX's real value.
def _expsrv_vtable_call_probe(cpu, mem):
    from tew.hardware.cpu_zig import EAX, ECX, EDX, EBX
    eax = cpu.regs[EAX]
    vtable_slot_target = mem.read32((eax + 0x24) & 0xFFFFFFFF) if eax else 0
    logger.error(
        "cpu",
        f"[expsrv-vtable-call-probe] EAX(vtable)=0x{eax:x} "
        f"[EAX+0x24]=0x{vtable_slot_target:x} (expect 0x1a01d3d9 == FUN_0f9dd3d9) "
        f"ECX(->arg3->param_2)=0x{cpu.regs[ECX]:x} EDX=0x{cpu.regs[EDX]:x} EBX=0x{cpu.regs[EBX]:x}",
    )
    return True  # log every hit -- the crash may not be the first call through this vtable slot,
    # only the one right before the halt matters; correlate by log order against the exception dump
# register_breakpoint(0x18061d84, _expsrv_vtable_call_probe)

# 2026-08-25 (cont'd x17): one instruction earlier than the vtable-call probe
# above -- static 0x7a8a1d5b (runtime 0x18061d5b), `MOV ECX,[EDX+ECX*4]`.
# EDX here is the collation-array base (msjet35.dll's per-session
# DAT_7a9362c0[slot].field_0x6d8, a pointer to a separately heap-allocated,
# 0x1c-stride array -- confirmed via mcity project's decompile of
# FUN_7a8a1c78/FUN_7a926327), and ECX has already been scaled to (index*7) by
# 2026-08-25 (cont'd x18): TRUST CHECK -- bisected with one logpoint per
# instruction to find that FUN_7a8a1c78's array-lookup branch never fires
# (confirming a wrong branch assumption, not a tew bug -- see status.md/
# changelog.md). Question answered, removed per usual practice.

# 2026-08-25 (cont'd x19): live-stalking the locale-info object itself,
# rather than continuing to decompile-guess who writes it. Breakpoint at
# msjet35.dll's FUN_7a926327, right at its call into FUN_7a8a1c78 (static
# 0x7a926414, runtime 0x180e6414 this session) -- by this point local_14
# (the astruct being passed) is fully built from `iVar2 + 0x8f0..0x8fc`.
# Recomputes iVar2 independently (session table base, read live from
# PTR_7a9362c0's own runtime address, static 0x7a9362c0 -> runtime
# 0x180f62c0 -- +param_1*0x708 +0x6f0) rather than trying to catch it in a
# register, and dumps a wide window around it to see whether this is a
# fully-populated object with one bad field, or an empty/unallocated one.
# 2026-08-25 (cont'd x20): both the address above and the FUN_7a926327
# assumption were wrong -- caller-hunt (byte-scanned all 12 direct E8 call
# sites targeting FUN_7a8a1c78, one logpoint per distinct caller function)
# found the real one live: FUN_7a9267a1, specifically its FIRST of six
# sequential calls (static 0x7a926824, offset block 0x8c8-0x8d4 -- NOT
# FUN_7a926327's 0x8f0-0x8fc block, a different function entirely).
# FUN_7a9267a1's decompile: iVar1 = *(session+0x6f0) (the same locale-info
# object pointer), then six field-block/FUN_7a8a1c78 call pairs at
# iVar1+0x8c8, +0x7b0, +0x738, +0x760, +0x788, +0x800 -- our crash is the
# FIRST of these (field3_0xc = *(iVar1+0x8d4)). Dumping iVar1 itself plus
# all six blocks to see whether this is one bad field or the whole object.
def _locale_info_object_probe(cpu, mem):
    from tew.hardware.cpu_zig import ESP
    esp = cpu.regs[ESP]
    param_1 = mem.read32(esp & 0xFFFFFFFF)
    local_14_ptr = mem.read32((esp + 0xC) & 0xFFFFFFFF)
    table_base = mem.read32(0x180f62c0)
    iVar1_addr = (table_base + param_1 * 0x708 + 0x6f0) & 0xFFFFFFFF
    iVar1 = mem.read32(iVar1_addr)
    logger.error(
        "cpu",
        f"[locale-info-object-probe] param_1(session)=0x{param_1:x} table_base=0x{table_base:x} "
        f"iVar1_addr=0x{iVar1_addr:x} iVar1(locale obj ptr)=0x{iVar1:x}",
    )
    local_14_bytes = bytes(mem.read8((local_14_ptr + i) & 0xFFFFFFFF) for i in range(16))
    logger.error("cpu", f"[locale-info-object-probe] local_14 (astruct passed to FUN_7a8a1c78, block 0x8c8): {local_14_bytes.hex()}")
    if iVar1 != 0:
        for block_off, block_label in [(0x8c8, "block_0x8c8_THIS_CALL"), (0x7b0, "block_0x7b0"),
                                        (0x738, "block_0x738"), (0x760, "block_0x760"),
                                        (0x788, "block_0x788"), (0x800, "block_0x800")]:
            addr = (iVar1 + block_off) & 0xFFFFFFFF
            block = bytes(mem.read8((addr + i) & 0xFFFFFFFF) for i in range(16))
            logger.error("cpu", f"[locale-info-object-probe] {block_label} @0x{addr:x}: {block.hex()}")
        # Testing whether record 0 (and a spread of others) also come back
        # -1 (a systemic lookup failure) or are valid (record 56 is just one
        # of 74 optional entries that legitimately isn't installed) -- each
        # record is 40 (0x28) bytes, its result-block (local_1c/uStack_18/
        # 14/10) starts at record_offset+8.
        for record_idx in [0, 1, 2, 5, 10, 20, 40, 56, 73]:
            addr = (iVar1 + record_idx * 0x28 + 8) & 0xFFFFFFFF
            block = bytes(mem.read8((addr + i) & 0xFFFFFFFF) for i in range(16))
            local_1c = int.from_bytes(block[0:4], "little", signed=True)
            logger.error("cpu", f"[locale-info-object-probe] record[{record_idx}] result-block @0x{addr:x}: {block.hex()} (local_1c={local_1c})")
    else:
        logger.error("cpu", "[locale-info-object-probe] iVar1 is NULL -- locale-info object was never allocated")
    return True
# register_breakpoint(0x180e6824, _locale_info_object_probe)

# 2026-08-25 (cont'd x21): pinning down WHY uStack_18/14/10 stay garbage
# while local_1c reliably reads -1 for failing records. Manual disassembly
# of FUN_7a8a4975 (static 0x7a8a4975) hit real ambiguity: Ghidra's decompile
# merges two different control-flow paths (the "iVar3<0 immediately" path
# and the "iVar3==-2 after FUN_7a8a2b80" path) into one `if(iVar3==-1){...}`
# shape, and manual byte-level tracing of the JMP targets didn't converge
# cleanly -- exactly the kind of decompile-shape trap this investigation
# already got burned by twice. Going live instead: breakpoint at the actual
# copy site `LEA ESI,[local_1c]; LEA EDI,[puVar2+8]; MOVSD x4` (static
# 0x7a8a49db, runtime 0x180649db this session -- delta 0x62840000, same as
# the other msjet35.dll addresses on this page), which unconditionally
# performs `puVar2[2..5] = local_1c/uStack_18/14/10` right before the copy
# executes. Dumps EBP and a wide raw window so the actual byte layout is
# visible regardless of whether local_1c's real offset is EBP-0x1c (source
# names are a guess until confirmed live).
# Answered 2026-08-25 (cont'd x22): three garbage siblings confirmed constant
# across all 74 records -- never written by the failing path, just leftover
# stack content. Question answered (see status_archive.md), no longer needs
# re-verifying live each run.
# register_breakpoint(0x180649db, _locale_result_copy_probe)

# 2026-08-25 (cont'd x22): the copy-site probe above found the real offsets
# (Ghidra's uStack_XX names are shifted 4 bytes off the true EBP-relative
# layout) and confirmed the three garbage siblings are CONSTANT across all
# 74 records (0x1, 0x082be46f, 0xffffffff at EBP-0x14/-0x10/-0xc this run) --
# meaning they're never written by the failing path at all, just whatever
# was already sitting in that stack slot before the loop started. Arming a
# watchpoint on EBP-0x10's concrete runtime address (0x082be240-0x10 =
# 0x082be230 this run, confirmed stable across all 74 hits above) from the
# very start of execution, then reading it at FUN_7a8a4975's own entry
# (runtime 0x18064975) -- before this function's prologue writes anything --
# to find the actual last writer of that slot.
# Answered 2026-08-25 (cont'd x22/x23): confirmed the three garbage siblings
# are stale leftover stack content, never written by the failing path -- see
# status_archive.md. Question answered; the watchpoint was left armed
# unconditionally afterward and was silently misfiring as a false "crash" on
# unrelated threads whose stacks happened to reuse this exact address
# (discovered 2026-08-25 cont'd x29 -- tid=1011's apparent EIP=0x18001300
# fault was actually this watchpoint's own halt, not a real SEH-dispatched
# exception; wasted two full run cycles before the WATCHPOINT HIT log line
# was noticed). Removed rather than re-gated -- its question is answered.
# cpu.set_watchpoint(0x082be230)
# register_breakpoint(0x18064975, _locale_result_stack_watch_probe)

# 2026-08-25 (cont'd x25): with ITypeComp::Bind now honestly returning
# DESCKIND_NONE/S_OK instead of the whole chain failing on LoadTypeLibEx's
# E_NOTIMPL, the crash is gone (run completes past the old ~76-89s failure
# point with no fault at all) -- but confirming WHY: is FUN_7a8a16b7 now
# returning -2 (msjet35.dll's own "clean not found" internal code, which
# routes into the FUN_7a8a2b80 success path) rather than -1 (the harder
# failure code), or did the crash just happen not to reproduce by luck?
# FUN_7a8a16b7's decompile kept timing out in Ghidra -- checking its actual
# EAX at its own RET (static 0x7a8a1994, the confirmed terminator from its
# raw instruction listing) live instead.
# 2026-08-25 (cont'd x26): __beginthread (0x9f5820) stashes the REAL user
# thread function/arg into two fields of the per-thread _tiddata block before
# calling CreateThread with a shared generic trampoline (lpStartAddress_9f58f0)
# -- confirmed all 5 worker threads (tid=1001..1005) share that same generic
# entry, so the CreateThread log line alone can't tell them apart. Ghidra's
# decompile names those fields _cvtbuf/_con_ch_buf (offsets 72/76 in its
# 952-byte modern _tiddata type), but the actual __calloc_dbg call here only
# allocates 0x74=116 bytes -- this binary's real (older/smaller) _tiddata
# layout, where those two offsets happen to be the dedicated start-addr/arg
# slots. Reading raw bytes live rather than trusting the mismatched type.
def _beginthread_call_probe(cpu, mem):
    # Breakpoint at the CALL itself (0x9f5880, a 6-byte COMPUTED_CALL --
    # confirmed via get_function_instructions -- returning to 0x9f5886), so
    # args are on the stack pre-call: [ESP+0]=lpThreadAttributes,
    # +4=dwStackSize, +8=lpStartAddress, +12=lpParameter, +16=dwCreationFlags,
    # +20=lpThreadId. First attempt used the post-call return-site address
    # with the post-call (+4-shifted) offsets, which read stale stack garbage
    # left over from an unrelated earlier call -- wrong breakpoint site, not
    # a wrong struct offset.
    from tew.hardware.cpu_zig import ESP
    esp = cpu.regs[ESP]
    ptd = mem.read32((esp + 12) & 0xFFFFFFFF)  # lpParameter arg to CreateThread
    real_start = mem.read32((ptd + 72) & 0xFFFFFFFF) if ptd else 0
    real_arg = mem.read32((ptd + 76) & 0xFFFFFFFF) if ptd else 0
    logger.error(
        "cpu",
        f"[beginthread-call-probe] _Ptd=0x{ptd:x} real_start=0x{real_start:x} real_arg=0x{real_arg:x}",
    )
    return True  # need this for every __beginthread call, not just the first
# register_breakpoint(0x9f5880, _beginthread_call_probe)

# 2026-08-25 (cont'd x27): tracing why NPSThreadSender (tid=1005, the "Sender"
# NPS thread -- confirmed via the real _tiddata start-addr field AND
# independently via NPS_ThreadCreateWithPriority's literal "Sender" name
# string) faults at EIP=0x00000002 ~99s in, the first time it processes a
# real (non-broadcast) outbound message. The crash path does 3 virtual calls
# through a CryptoPP BufferedTransformation-derived filter object hanging off
# a cServerData's +0x1e8 (or +0x1f8) field, via a vtordisp adjustor-thunk
# pattern: base_obj = *(local_48+0x1e8); real_vtable = base_obj + 4 +
# *(*(base_obj+4)+4). Molly found the real (healthy) vtable for this filter
# class live at 0x011fed28 -- slot+4=0x9f4e30, slot+0x10=0xb814a0,
# slot+0x2c=0xb818a0 are what a correctly-linked object should resolve to.
# Ghidra's local_48 EBP-offset labels are not to be trusted at face value
# (this binary has a known history of Ghidra's stack-offset labels being
# shifted from the true EBP-relative address -- see the fields-probe note
# above) so this breaks at the shared _memcpy call site (both branches always
# reach it) and scans a window of candidate stack slots for the cServerData*,
# resolving the vtordisp chain for each candidate rather than trusting one
# guessed offset.
# 2026-08-25 (cont'd x28): the +0x1e8 branch's memcpy (0x00acb1e0) got ZERO
# hits in a full 99s+ run -- that branch never executes at all, another
# decompile-shape trap (assumed top-to-bottom branch order matched execution
# order). Moving to the mirrored +0x1f8 branch's memcpy (0x00acb3ba), and
# checking BOTH offsets per candidate since which branch is real is now
# exactly the open question, not an assumption.
def _crypto_object_probe(cpu, mem):
    from tew.hardware.cpu_zig import EBP
    ebp = cpu.regs[EBP]
    for off in range(0x30, 0x60, 4):
        cand = mem.read32((ebp - off) & 0xFFFFFFFF)
        if cand == 0 or (cand & 0xFFFF0000) == 0xcccc0000:
            continue
        for field_off, field_label in ((0x1e8, "+0x1e8"), (0x1f8, "+0x1f8")):
            try:
                base_obj = mem.read32((cand + field_off) & 0xFFFFFFFF)
                if base_obj == 0:
                    continue
                vt2 = mem.read32((base_obj + 4) & 0xFFFFFFFF)
                disp = mem.read32((vt2 + 4) & 0xFFFFFFFF)
                real_vtable = (base_obj + 4 + disp) & 0xFFFFFFFF
                logger.error(
                    "cpu",
                    f"[crypto-object-probe] candidate EBP-0x{off:x}=0x{cand:x} {field_label}: "
                    f"base_obj=0x{base_obj:x} vt2=0x{vt2:x} disp=0x{disp:x} "
                    f"real_vtable=0x{real_vtable:x} (expect 0x11fed28)",
                )
            except Exception as e:
                logger.error("cpu", f"[crypto-object-probe] candidate EBP-0x{off:x}=0x{cand:x} {field_label} failed: {e!r}")
    return True  # need every hit, not just the first -- want the one right before the fault
# register_breakpoint(0x00acb3ba, _crypto_object_probe)

def _plain_sendtosocket_logpoint(eip, regs, memory, memory_size):
    logger.error("cpu", "[plain-sendtosocket] LAB_00acb5ab path fired -- no crypto branch taken this message")
# cpu.add_logpoint(0x00acb643, _plain_sendtosocket_logpoint)

# 2026-08-25 (cont'd x30): identifying tid=1010 (owner of thread-stack slot
# 10, 0x08280000-0x082c0000 -- the range 0x082be46f, the "garbage" sibling
# field in msjet35.dll's locale-info object, falls into) and tid=1011 (the
# thread that crashes reading it), both spawned via the SAME generic
# _beginthreadex trampoline (0x9fc3a0, decompiled: real start/arg stashed at
# lpThreadParameter[0x12]/[0x13] = offsets 0x48/0x4c -- same 72/76 numeric
# offsets as __beginthread's _tiddata, just DWORD-indexed here).
# lpThreadParameter IS the CreateThread `param` value already in the thread
# log (no extra indirection needed) -- breaking at the trampoline's own
# entry to resolve real_start/real_arg for whichever thread hits it.
def _beginthreadex_entry_probe(cpu, mem):
    from tew.hardware.cpu_zig import ESP
    esp = cpu.regs[ESP]
    lp_thread_param = mem.read32((esp + 4) & 0xFFFFFFFF)  # cdecl arg at entry
    real_start = mem.read32((lp_thread_param + 0x48) & 0xFFFFFFFF) if lp_thread_param else 0
    real_arg = mem.read32((lp_thread_param + 0x4c) & 0xFFFFFFFF) if lp_thread_param else 0
    logger.error(
        "cpu",
        f"[beginthreadex-entry-probe] lpThreadParameter=0x{lp_thread_param:x} "
        f"real_start=0x{real_start:x} real_arg=0x{real_arg:x}",
    )
    return True  # need this for every thread using this trampoline
# register_breakpoint(0x009fc3a0, _beginthreadex_entry_probe)

def _mem_init_logpoint(eip, regs, memory, memory_size):
    logger.error("cpu", "[mem-init-probe] _MEM_init (00a719e0) reached")
# cpu.add_logpoint(0x00a719e0, _mem_init_logpoint)

# 2026-08-26: re-opening the "who actually writes field2_0x8" question an
# earlier pre-compaction pass in this same investigation started but never
# finished (its watchpoint got removed as an apparently-answered leftover --
# it wasn't). Copy site confirmed live: FUN_7a8a4975's `LEA ESI,[local_1c];
# ...; MOVSD x4` (static 0x7a8a49db-0x7a8a49e4), real EBP-relative offsets
# EBP-0x14/-0x10/-0xc (Ghidra's uStack_18/14/10 names are shifted 4 bytes
# off the true layout -- established earlier this investigation). Pass 1:
# find this run's concrete address for EBP-0x10 (the field2_0x8 slot) before
# arming a watchpoint on it.
# Pass 1/2 answered: field2_0x8's live address is stable at 0x082be230 for
# this run (matches the pre-compaction investigation's original target
# exactly), current_val=137094255=0x082be46f. The watchpoint showed this
# address is a shallow, frequently-reused stack depth written by MULTIPLE
# unrelated pieces of code throughout execution (0x180e859a, 0x180e866b,
# ...), not owned by any single writer -- there is no "the" last writer to
# find, it's whichever unrelated call happened to run last before
# FUN_7a8a4975 reads it. Removed (was also causing an early false-positive
# halt on the first unrelated write, same failure mode as the original
# leftover watchpoint this reopened).
# cpu.set_watchpoint(0x082be230)

def _typelib_lookup_return_logpoint(eip, regs, memory, memory_size):
    try:
        eax = regs[EAX]
        signed = eax - 0x100000000 if eax >= 0x80000000 else eax
        logger.error("cpu", f"[typelib-lookup-return] FUN_7a8a16b7 returned EAX=0x{eax:x} ({signed})")
    except Exception as e:
        logger.error("cpu", f"[typelib-lookup-return] EXCEPTION: {e!r}")
# cpu.add_logpoint(0x18061994, _typelib_lookup_return_logpoint)

# 2026-08-26: with real oleaut32.dll now genuinely running, dbcode.c's
# Dbcode_InitDao (MCity_d.exe) fails at IClassFactory2::CreateInstanceLic --
# a real dao350.dll vtable call -- for BOTH its license-key attempts (a
# dbVariant-wrapped ANSI string, then a plain SysAllocString'd BSTR), where
# it used to succeed earlier this session with the old, Python-faked BSTR
# layout. Real vtable COMPUTED_CALL sites found live via
# get_function_instructions: static 0x008f580c (dbVariant attempt) and
# 0x008f59b3 (SysAllocString attempt) -- both in MCity_d.exe (loads at its
# preferred base, no delta needed). Pre-call stdcall stack layout (COM
# vtable call, args pushed right-to-left): [ESP+0]=this, +4=pUnkOuter,
# +8=pUnkReserved, +12=&riid, +16=bstrKey, +20=&ppvObj. Dumping the BSTR's
# length-prefix and content at the call site, plus EAX (HRESULT) right after
# the call's paired __chkesp completes.
def _read32_raw(memory, memory_size, addr: int) -> int | None:
    # Raw LP_c_ubyte ctypes pointer -- unlike Memory.read32, indexing past
    # memory_size doesn't raise, it segfaults the whole process (confirmed
    # live: ctypes Pointer_item_lock_held, no bounds check at all). Every
    # byte access here MUST be range-checked first.
    addr &= 0xFFFFFFFF
    if addr + 3 >= memory_size:
        return None
    return memory[addr] | (memory[addr + 1] << 8) | (memory[addr + 2] << 16) | (memory[addr + 3] << 24)

def _walk_ebp_chain_raw(memory, memory_size, ebp_val: int, max_frames: int = 25) -> list[str]:
    """Same logic as exception_diagnostics._walk_ebp_chain, reimplemented
    against the raw ctypes memory a logpoint callback actually receives
    (that function needs a full cpu.memory object, which logpoints don't
    get) -- reuses _annotate_address (imported from that same module) for
    module+offset resolution, so results match the existing halt-diagnostic
    "EBP chain (call frames)" dumps exactly."""
    lines = []
    frame_ebp = ebp_val
    seen = set()
    depth = 0
    while depth < max_frames and frame_ebp and frame_ebp not in seen and frame_ebp + 7 < memory_size:
        seen.add(frame_ebp)
        saved_ebp = _read32_raw(memory, memory_size, frame_ebp)
        ret_addr = _read32_raw(memory, memory_size, frame_ebp + 4)
        if saved_ebp is None or ret_addr is None:
            lines.append(f"  frame[{depth}] EBP=0x{frame_ebp:08x} (read error)")
            break
        lines.append(
            f"  frame[{depth}] EBP=0x{frame_ebp:08x} ret=0x{ret_addr:08x}"
            f"{_annotate_address(ret_addr, exe.import_resolver)}"
        )
        frame_ebp = saved_ebp
        depth += 1
    return lines

def _scan_memory_for(memory, memory_size, needle: bytes, start: int, end: int):
    """Bulk-search a guest memory range for a byte string, via a single
    ctypes.string_at() read (fast, one real memcpy) instead of a
    per-byte Python loop. Returns a list of matching guest addresses."""
    end = min(end, memory_size)
    if start >= end:
        return []
    base_addr = ctypes.cast(memory, ctypes.c_void_p).value
    raw = ctypes.string_at(base_addr + start, end - start)
    hits = []
    idx = 0
    while True:
        idx = raw.find(needle, idx)
        if idx == -1:
            break
        hits.append(start + idx)
        idx += 1
    return hits

def _make_createinstancelic_probe(label: str):
    def _probe(eip, regs, memory, memory_size):
        esp = regs[ESP]
        ebp = regs[EBP]
        this_ptr = _read32_raw(memory, memory_size, esp + 0)
        p_unk_outer = _read32_raw(memory, memory_size, esp + 4)
        p_unk_reserved = _read32_raw(memory, memory_size, esp + 8)
        riid_ptr = _read32_raw(memory, memory_size, esp + 12)
        bstr_ptr = _read32_raw(memory, memory_size, esp + 16)
        ppv_obj_ptr = _read32_raw(memory, memory_size, esp + 20)
        byte_len, text = 0, ""
        if bstr_ptr:
            byte_len = _read32_raw(memory, memory_size, bstr_ptr - 4)
            if byte_len is None or byte_len > 1000 or bstr_ptr + byte_len >= memory_size:
                byte_len, text = -1, "<out of bounds or implausible length>"
            else:
                raw = bytes(memory[bstr_ptr + i] for i in range(byte_len))
                text = raw.decode("utf-16-le", errors="replace")
        logger.error(
            "com",
            f"[createinstancelic-{label}] EBP=0x{ebp:x} this=0x{this_ptr or 0:x} "
            f"pUnkOuter=0x{p_unk_outer or 0:x} pUnkReserved=0x{p_unk_reserved or 0:x} "
            f"riid_ptr=0x{riid_ptr or 0:x} bstr_ptr=0x{bstr_ptr or 0:x} "
            f"byte_len={byte_len} content={text!r} ppvObj_ptr=0x{ppv_obj_ptr or 0:x}",
        )
    return _probe
# cpu.add_logpoint(0x008f580c, _make_createinstancelic_probe("dbvariant-call"))
# 2026-08-28: disabled -- resolved BSTR milestone bug, and this specific
# probe's own comments below already document it as self-inflicted-
# garbage-prone (double-dispatch artifact). Was burning a logpoint slot.
# cpu.add_logpoint(0x008f59b3, _make_createinstancelic_probe("sysallocstring-call"))

# 2026-08-26 (cont'd): local_44's real address confirmed live == EBP-0x44 ==
# 0x082bfa60 this run (matched bstr_ptr exactly). Watching that concrete
# address from program start to find who's actually supposed to write
# Ordinal_2's (SysAllocString's) return value there and isn't -- if this
# comes back with zero hits, it's never written at all (same class of bug as
# the earlier field2_0x8 investigation); if it fires from unrelated code
# reusing this stack depth before Dbcode_InitDao runs, that's the false-
# positive-prone "shared shallow stack slot" pattern seen before with
# 0x082be230 -- watch for that before trusting a hit here.
# cpu.set_watchpoint(0x082bfa60)

# Answered: watchpoint_hit=True, last_write_eip=0x8f4ea3 (the function-entry
# 0xCCCCCCCC stack-fill loop) -- local_44 is never written after that.
# Removed this breakpoint: it shared 0x008f59b3 with the logpoint just above,
# and the createinstancelic-sysallocstring-call logpoint was firing TWICE,
# identically, per hit -- suspect a breakpoint+logpoint-at-the-same-address
# double-dispatch/re-execution artifact, not a real double CALL. Testing
# with the breakpoint removed (logpoint alone) to see if the duplicate and
# the garbage bstr_ptr reading were self-inflicted measurement artifacts.
# def _local_44_watch_probe(cpu, mem):
#     logger.error(
#         "cpu",
#         f"[local-44-watch] watchpoint_hit={cpu.watchpoint_hit} "
#         f"last_write_eip=0x{cpu.watchpoint_eip:x} last_write_val=0x{cpu.watchpoint_val:x}",
#     )
#     return True
# register_breakpoint(0x008f59b3, _local_44_watch_probe)

# 2026-08-26 (cont'd): does real oleaut32.dll's SysAllocString (ordinal 2,
# real runtime address computed live from the actual loaded file:
# 0x11000000 base + 0x4ba2 RVA = 0x11004ba2) even get called during
# Dbcode_InitDao's window at all? If this never fires, the game's own
# control flow skips the whole "call Ordinal_2, retry CreateInstanceLic"
# branch entirely (another wrong-branch-assumption case); if it fires but
# EAX comes back wrong, the bug is in the call/return path itself.
def _sysallocstring_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    psz = _read32_raw(memory, memory_size, esp + 4)
    text = "<null>"
    if psz:
        # wide-string read, 2 bytes at a time, bounds-checked
        out = []
        for i in range(60):
            addr = psz + i * 2
            if addr + 1 >= memory_size:
                break
            lo = memory[addr]
            hi = memory[addr + 1]
            ch = lo | (hi << 8)
            if ch == 0:
                break
            out.append(chr(ch))
        text = "".join(out)
    logger.error("com", f"[sysallocstring-entry] called, psz=0x{psz or 0:x} text={text!r}")
# cpu.add_logpoint(0x11004ba2, _sysallocstring_entry_probe)

def _make_createinstancelic_return_probe(label: str):
    def _probe(eip, regs, memory, memory_size):
        eax = regs[EAX]
        signed = eax - 0x100000000 if eax >= 0x80000000 else eax
        logger.error("com", f"[createinstancelic-{label}-return] HRESULT=0x{eax:x} ({signed})")
    return _probe
# cpu.add_logpoint(0x008f5816, _make_createinstancelic_return_probe("dbvariant"))
# 2026-08-28: disabled, same reason as its -call sibling above.
# cpu.add_logpoint(0x008f59c0, _make_createinstancelic_return_probe("sysallocstring"))

# 2026-08-28: DBParamQuery::DBParamQuery's DAOParameters::get_Count call
# (vtable slot 0x1c on the interface obtained from get_Parameters) is where
# "could not get param count; does table really exist?" comes from --
# confirmed by full decompile of the constructor at 0x00995970 (three
# COMPUTED_CALLs: 0x00995b1c=get_Parameters, 0x00995c7b=get_Count,
# 0x00995ed7=per-param DBParam ctor loop). Probing the two calls right
# before this one to identify which real DLL's vtable is actually being
# called (get_Count's implementation lives wherever local_28's object came
# from -- likely expsrv.dll or dao350.dll, not yet confirmed) and the
# HRESULT it returns.
def _dbparamquery_getcount_pre_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    this_ptr = _read32_raw(memory, memory_size, esp + 0)
    vtable_ptr = _read32_raw(memory, memory_size, this_ptr) if this_ptr else None
    func_ptr = _read32_raw(memory, memory_size, vtable_ptr + 0x1c) if vtable_ptr else None
    # dao350.dll's get_Count is a thin thunk (FUN_0447dfe2, confirmed via
    # Ghidra decompile): (**(code**)(**(int**)(this+8) + 0x24))(param_2) --
    # forwards to the *real* inner object at [this+8], vtable slot 0x24.
    # Same "tear-off wrapper delegates to real implementer" shape as the
    # earlier Fields.Count investigation's FUN_0447dc1c -- follow it one
    # level deeper to find the real, non-thunk implementer.
    inner_this = _read32_raw(memory, memory_size, this_ptr + 8) if this_ptr else None
    inner_vtable = _read32_raw(memory, memory_size, inner_this) if inner_this else None
    inner_func = _read32_raw(memory, memory_size, inner_vtable + 0x24) if inner_vtable else None
    logger.error(
        "com",
        f"[dbparamquery-getcount-call] this=0x{this_ptr or 0:x} "
        f"vtable=0x{vtable_ptr or 0:x} func=0x{func_ptr or 0:x} "
        f"inner_this=0x{inner_this or 0:x} inner_vtable=0x{inner_vtable or 0:x} "
        f"inner_func=0x{inner_func or 0:x}",
    )
cpu.add_logpoint(0x00995c7b, _dbparamquery_getcount_pre_probe)

def _dbparamquery_getcount_return_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    signed = eax - 0x100000000 if eax >= 0x80000000 else eax
    logger.error("com", f"[dbparamquery-getcount-return] HRESULT=0x{eax:x} ({signed})")
cpu.add_logpoint(0x00995c7e, _dbparamquery_getcount_return_probe)

# 2026-08-28: dao350.dll FUN_0447dc1c (the real, non-thunk get_Count
# implementer -- same function the earlier Fields.Count investigation
# found) calls FUN_044d26ce(iVar1) as its "refresh gate" -- iVar1 =
# *(int*)(inner_this+8), i.e. a third layer of indirection past the
# outer DAOParameters thunk. FUN_044d26ce dispatches through a type-
# indexed function table (DAT_044770b0[*(int*)(iVar1+0x10)]) to the
# real per-type "populate the count" handler, only when not already
# cached. Its return value becomes our observed HRESULT's low 16 bits
# (0x800a0000 | ret == 0x800a0c03 == DAO error 3075). Capturing the
# type index and target handler address to find which one is failing.
def _refresh_gate_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    param_1 = _read32_raw(memory, memory_size, esp + 4)
    type_idx = _read32_raw(memory, memory_size, param_1 + 0x10) if param_1 else None
    handler = (
        _read32_raw(memory, memory_size, 0x044770b0 + type_idx * 4)
        if type_idx is not None else None
    )
    count_field = _read32_raw(memory, memory_size, param_1 + 0x2c) if param_1 else None
    logger.error(
        "com",
        f"[refresh-gate-entry] param_1=0x{param_1 or 0:x} type_idx={type_idx} "
        f"handler=0x{handler or 0:x} count_field_raw=0x{count_field or 0:x}",
    )
# 2026-08-28: confirmed (type_idx=25=Parameters, handler=0x44c69bc for our
# call) and no longer needed -- this is a hot shared path fired for every
# DAO collection refresh in the whole run, not just ours; disabled, was
# pure log noise past this point in the investigation.
# cpu.add_logpoint(0x044d26ce, _refresh_gate_entry_probe)

def _read_cstr_raw(memory, memory_size, addr, max_len=200):
    if not addr or addr + max_len >= memory_size:
        return "<invalid>"
    out = []
    for i in range(max_len):
        c = memory[addr + i]
        if c == 0:
            break
        out.append(chr(c) if 32 <= c < 127 else f"\\x{c:02x}")
    return "".join(out)

# 2026-08-28: FUN_044c69bc (the real per-type "populate Parameters.Count"
# handler, dispatched via FUN_044d26ce's type table for type_idx=25) has
# two candidate failure points: a generic "no compiled statement" error
# (FUN_044d44c2 @ call site 0x044c6a40, fixed sentinel arg -- same shape
# regardless of query content) vs FUN_044d525b (call site 0x044c6ac2),
# which takes the query's own text as an LPCSTR (4th arg) -- much more
# likely to be the content-specific failure. Probing both entries to see
# which one actually fires for StockAssembly_SelectAPT, and reading the
# LPCSTR live to see exactly what text is being parsed at failure time.
def _generic_error_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    sentinel = _read32_raw(memory, memory_size, esp + 4)
    logger.error("com", f"[fun-044d44c2-entry] sentinel=0x{sentinel & 0xffffffff:x}")
# 2026-08-28: confirmed this path never fires for StockAssembly_SelectAPT
# (ruled out) -- disabled to free a logpoint slot.
# cpu.add_logpoint(0x044d44c2, _generic_error_probe)

def _param_lookup_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    p1 = _read32_raw(memory, memory_size, esp + 4)
    p2 = _read32_raw(memory, memory_size, esp + 8)
    p3 = _read32_raw(memory, memory_size, esp + 12)
    lpcstr = _read32_raw(memory, memory_size, esp + 16)
    p5 = _read32_raw(memory, memory_size, esp + 20)
    p6 = _read32_raw(memory, memory_size, esp + 24)
    text = _read_cstr_raw(memory, memory_size, lpcstr) if lpcstr else "<null>"
    logger.error(
        "com",
        f"[fun-044d525b-entry] param_1=0x{p1 or 0:x} param_2=0x{p2 or 0:x} "
        f"param_3=0x{p3 or 0:x} param_4(lpcstr)=0x{lpcstr or 0:x} text={text!r} "
        f"param_5=0x{p5 or 0:x} param_6=0x{p6 or 0:x}",
    )
# cpu.add_logpoint(0x044d525b, _param_lookup_probe)  # 2026-08-28: confirmed clean/identical args across all 4 queries

# 2026-08-28: the query is a compiled/stored function in MSysQueries, not
# plain text -- searching live memory for the literal date TEXT was never
# going to find anything (confirmed: 0 hits). Redirected to tracing the
# real deserialize/compile call chain instead: FUN_7a862215 (msjet35.dll's
# SQL execution-plan compiler) -> FUN_7a862942 -> FUN_7a862cd4, which
# checks the catalog object-type of our query name via FUN_7a858c87 into
# a local `short` -- 0 triggers an explicit abort/raise-error path, 5 is
# Jet's real "Query" object type (one branch), anything else non-zero
# takes a different branch. Probing that return value live for our
# specific failing call to see which path it actually takes.
def _catalog_type_check_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    signed = eax - 0x10000 if eax & 0xFFFF >= 0x8000 else eax & 0xFFFF
    logger.error("com", f"[catalog-type-check] EAX=0x{eax & 0xffffffff:x} (as int16: {signed})")
# 2026-08-28: confirmed EAX=5 (Jet's real "Query" object type) -- ruled
# out, no longer needed.
# cpu.add_logpoint(0x17022d55, _catalog_type_check_probe)

# 2026-08-28: per the earlier DAO-3075 investigation's own hard-won lesson
# ("Ghidra's positional parameter names do NOT reliably track the same
# real value across different functions... wide-scan live registers + a
# broad stack range at each hop and match by actual content, never trust
# name continuity alone") -- rather than keep trusting decompiled param_N
# labels, wide-dumping FUN_7a8635de's real args plus a raw hex/ASCII
# window around its node pointer (param_2) at every invocation. This is
# the real recursive statement/node compiler -- fires once per node,
# recursively, for every query compiled all run, so this will be noisy;
# correlate by timestamp against the known dbparamquery-getcount-call/
# return window like every other probe this session.
_last_node_ptr = [None]  # shared with _table_lookup_return_probe below

def _node_compiler_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    param_1 = _read32_raw(memory, memory_size, esp + 4)
    param_2 = _read32_raw(memory, memory_size, esp + 8)
    param_3 = _read32_raw(memory, memory_size, esp + 12)
    _last_node_ptr[0] = param_2
    logger.error(
        "com",
        f"[node-compiler-entry] param_1=0x{param_1 or 0:x} param_2(node)=0x{param_2 or 0:x} "
        f"param_3=0x{param_3 or 0:x}",
    )
    if param_2:
        base_addr = ctypes.cast(memory, ctypes.c_void_p).value
        window_end = min(memory_size, param_2 + 0xE0)
        if param_2 < window_end:
            raw = ctypes.string_at(base_addr + param_2, window_end - param_2)
            printable = "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
            hexdump = raw.hex()
            logger.error("com", f"[node-compiler-entry]   node bytes: {hexdump}")
            logger.error("com", f"[node-compiler-entry]   node ascii: {printable!r}")
# cpu.add_logpoint(0x170235de, _node_compiler_entry_probe)  # 2026-08-28: disabled to free a slot -- relationship to FUN_7a893ba6's real path still unconfirmed

# The two branches inside FUN_044d525b (param_5==-1 selects which dynamically-
# bound msjet35.dll function pointer gets called) converge on the same
# "if (-1 < iVar2) success else translate-and-return-error" check. Probing
# EAX right after each call returns to get the RAW Jet-native error code
# before FUN_044d418f translates it into our observed OLE HRESULT.
def _jet_lookup_returnA_probe(eip, regs, memory, memory_size):
    logger.error("com", f"[jet-lookup-branchA-return] EAX=0x{regs[EAX] & 0xffffffff:x}")
# 2026-08-28: confirmed neither branch fires (allocator fails before this
# point) -- disabled to free a logpoint slot.
# 2026-08-28: confirmed branch A never fires (param_5 != -1 for this
# query) -- disabled to free a logpoint slot.
# cpu.add_logpoint(0x044d529f, _jet_lookup_returnA_probe)

def _jet_lookup_returnB_probe(eip, regs, memory, memory_size):
    logger.error("com", f"[jet-lookup-branchB-return] EAX=0x{regs[EAX] & 0xffffffff:x}")
    for line in _walk_ebp_chain_raw(memory, memory_size, regs[EBP] & 0xFFFFFFFF):
        logger.error("com", f"[jet-lookup-branchB-return] {line}")
cpu.add_logpoint(0x044d52be, _jet_lookup_returnB_probe)

# 2026-08-28: does FUN_044d525b propagate iVar2 (-3100) raw, or the
# formatted return from FUN_044d418f(iVar2,...)? Per decompile it's the
# latter -- probing the real RET site (0x044d5309, right after the
# FUN_044d418f call at 0x044d52fd) to see what it actually returns.
def _fun_044d525b_final_return_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    signed = eax - 0x100000000 if eax >= 0x80000000 else eax
    logger.error("com", f"[fun-044d525b-final-return] EAX=0x{eax & 0xffffffff:x} ({signed})")
# cpu.add_logpoint(0x044d5309, _fun_044d525b_final_return_probe)  # 2026-08-28: confirmed 3075, no longer needed

# 2026-08-28: FUN_7a85e7e1 (call site 0x7a8624fc inside FUN_7a862215) is
# what FUN_7a862215 directly `return`s -- confirmed via decompile it's the
# tail: `local_44 = FUN_7a85e7e1(...); ...; return local_44;`. Everything
# in the node-compile chain before this point (FUN_7a8635de's self-lookup
# + binding-resolve) is now confirmed clean for all 4 distinct queries
# this run, including ours -- so this is where -3100 most likely actually
# originates. Reading its REAL args at entry (not assuming param_2 ==
# our node pointer just because a decompiled variable name suggests it).
# 2026-08-28 (Molly's correction): FUN_7a85e7e1's own hardcoded early-exit
# codes (0xbd8 = 3032) are in the 3xxx range, not -3100 -- so its own
# fallback returns can't be the source of our observed -3100. The real
# error value has to come from one of the two inner calls on its main
# path: FUN_7a885bea(param_1,param_2) at call site 0x7a85e876, returning
# to 0x7a85e87b (TEST EAX,EAX immediately after) -- probe that return
# point directly for FUN_7a885bea's REAL return value, instead of
# inferring it from the outer function's final result.
# 2026-08-28: confirmed dead end -- neither of these ever fires for the
# failing StockAssembly_SelectAPT call (FUN_7a862215/FUN_7a85e7e1 is never
# entered at all; the real -3100 return site is 0x044d52be, several layers
# below this, reached via the DAT_044e52a8 indirect call, not this chain).
# Disabled to free logpoint slots.
def _inner_call1_return_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    signed = eax - 0x100000000 if eax >= 0x80000000 else eax
    logger.error("com", f"[inner-call1-return FUN_7a885bea] EAX=0x{eax & 0xffffffff:x} ({signed})")
# cpu.add_logpoint(0x1701e87b, _inner_call1_return_probe)

def _final_stage_return_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    signed = eax - 0x100000000 if eax >= 0x80000000 else eax
    logger.error("com", f"[final-stage-return] EAX=0x{eax & 0xffffffff:x} ({signed})")
# cpu.add_logpoint(0x17022501, _final_stage_return_probe)

# 2026-08-28: 0x044d52be (EAX=0xfffff3e4=-3100, live-confirmed) is the
# return of the indirect CALL at 0x044d52b8 -- CALL DAT_044e52a8. Reading
# full register state + the 6 pushed stack args + DAT_044e52a8's own raw
# value (the actual resolved call target) right before that CALL executes,
# for the failing call specifically.
def _dat_044e52a8_call_site_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    target = _read32_raw(memory, memory_size, 0x044e52a8)
    a0 = _read32_raw(memory, memory_size, esp + 0)
    a1 = _read32_raw(memory, memory_size, esp + 4)
    a2 = _read32_raw(memory, memory_size, esp + 8)
    a3 = _read32_raw(memory, memory_size, esp + 12)
    a4 = _read32_raw(memory, memory_size, esp + 16)
    a5 = _read32_raw(memory, memory_size, esp + 20)
    logger.error(
        "com",
        f"[dat-044e52a8-call-site] target=0x{target or 0:x} ESP=0x{esp:x} "
        f"EAX=0x{regs[EAX]:x} EBX=0x{regs[EBX]:x} ECX=0x{regs[ECX]:x} "
        f"EDX=0x{regs[EDX]:x} ESI=0x{regs[ESI]:x} EDI=0x{regs[EDI]:x} "
        f"EBP=0x{regs[EBP]:x}",
    )
    logger.error(
        "com",
        f"[dat-044e52a8-call-site] args(local_4)=0x{a0 or 0:x} (0x44)=0x{a1 or 0:x} "
        f"(puVar1)=0x{a2 or 0:x} (param_4)=0x{a3 or 0:x} (param_6)=0x{a4 or 0:x} "
        f"(param_2)=0x{a5 or 0:x}",
    )
# cpu.add_logpoint(0x044d52b8, _dat_044e52a8_call_site_probe)  # 2026-08-28: confirmed stable target 0x17053ba6 + args across all 4 queries

# 2026-08-28: FUN_7a893ba6 (confirmed live DAT_044e52a8 target) calls
# FUN_7a89fd45 at call site 0x7a893bfa. Inside it: `if (param_3 == 0)
# FUN_7a862215(...) else FUN_7a8a0d65(...)`. FUN_7a862215's return-site
# never fired in any run, so the live path is likely the FUN_7a8a0d65
# branch (param_3 != 0) -- verifying param_3 live instead of assuming.
def _fun_7a89fd45_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    p1 = _read32_raw(memory, memory_size, esp + 4)
    p2 = _read32_raw(memory, memory_size, esp + 8)
    p3 = _read32_raw(memory, memory_size, esp + 12)
    p4 = _read32_raw(memory, memory_size, esp + 16)
    logger.error(
        "com",
        f"[fun-7a89fd45-entry] param_1=0x{p1 or 0:x} param_2=0x{p2 or 0:x} "
        f"param_3=0x{p3 or 0:x} param_4=0x{p4 or 0:x}",
    )
# cpu.add_logpoint(0x1759fd45, _fun_7a89fd45_entry_probe)  # 2026-08-28: confirmed 0 hits across all 4 queries

# 2026-08-28: Ghidra's static call graph for FUN_7a893ba6 isn't trustworthy
# (its own body includes disconnected far tail chunks at 0x7a8d6960+, same
# pattern as FUN_7a85e7e1/FUN_7a89fd45) -- and its listed callee
# FUN_7a89fd45 is confirmed never entered live. Live-stepping the real
# function instead: entry, then the 3 TEST/JZ decision points right after
# its first 3 calls (each TEST doubles as reading that call's real return
# value), then a sanity probe right after the CALL at 0x7a893bfa (listed
# callee FUN_7a89fd45) to independently confirm whether that call site
# itself is ever reached at all (rules out a translation bug on our end
# for the entry probe that just came back empty).
# cpu.add_logpoint(0x17053ba6, _fun_7a893ba6_entry_probe)  # 2026-08-28: superseded -- went straight to the literal producer instead
def _fun_7a893ba6_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    p1 = _read32_raw(memory, memory_size, esp + 4)
    p2 = _read32_raw(memory, memory_size, esp + 8)
    logger.error("com", f"[fun-7a893ba6-entry] param_1=0x{p1 or 0:x} param_2=0x{p2 or 0:x}")

# cpu.add_logpoint(0x17053bbf, _fun_7a893ba6_decision1_probe)
def _fun_7a893ba6_decision1_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    logger.error("com", f"[fun-7a893ba6-decision1 (post FUN_7a848e20)] EAX=0x{eax & 0xffffffff:x} taken={eax == 0}")

# cpu.add_logpoint(0x17053bd1, _fun_7a893ba6_decision2_probe)
def _fun_7a893ba6_decision2_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    logger.error("com", f"[fun-7a893ba6-decision2 (post FUN_7a8536a6)] EAX=0x{eax & 0xffffffff:x} taken={eax == 0}")

# cpu.add_logpoint(0x17053be0, _fun_7a893ba6_decision3_probe)
def _fun_7a893ba6_decision3_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    logger.error("com", f"[fun-7a893ba6-decision3 (post FUN_7a85375e)] EAX=0x{eax & 0xffffffff:x} taken={eax == 0}")

# cpu.add_logpoint(0x17053bff, _fun_7a893ba6_post_7a89fd45call_probe)
def _fun_7a893ba6_post_7a89fd45call_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    signed = eax - 0x100000000 if eax >= 0x80000000 else eax
    logger.error("com", f"[fun-7a893ba6-post-fun7a89fd45-call] EAX=0x{eax & 0xffffffff:x} ({signed})")

# 2026-08-28: found the literal producer via raw byte scan (Ghidra's
# decompile/instruction-listing for FUN_7a8beaad was itself fragmented
# with address gaps, same disconnected-tail issue). Exact bytes at
# 0x7a8beb6e: B8 E4 F3 FF FF = MOV EAX,0xfffff3e4, right after
# CALL FUN_7a854cd0 (args verified against the decompile's error-report
# call: esi, [ebp-0x158], 0, 0, 0xffffe0bb, ...). This function shares its
# caller's frame directly (unaff_EBP everywhere, no own prologue) so
# EBP-relative reads here are real live values, not garbage. Reading the
# token pointer at [EBP-0x158] (the string FUN_7a854cd0 reports as the
# offending token) plus the surrounding offset fields.
def _literal_producer_probe(eip, regs, memory, memory_size):
    ebp = regs[EBP]
    token_ptr = _read32_raw(memory, memory_size, ebp - 0x158)
    token_text = _read_cstr_raw(memory, memory_size, token_ptr) if token_ptr else "<null>"
    off14 = _read32_raw(memory, memory_size, ebp - 0x14)
    off2c = _read32_raw(memory, memory_size, ebp - 0x2c)
    off28 = _read32_raw(memory, memory_size, ebp - 0x28)
    off30 = _read32_raw(memory, memory_size, ebp - 0x30)
    off8 = _read32_raw(memory, memory_size, ebp - 8)
    logger.error(
        "com",
        f"[literal-producer FUN_7a8beaad] EBP=0x{ebp:x} token_ptr=0x{token_ptr or 0:x} "
        f"token_text={token_text!r} [ebp-0x14]=0x{off14 or 0:x} [ebp-0x2c]=0x{off2c or 0:x} "
        f"[ebp-0x28]=0x{off28 or 0:x} [ebp-0x30](errflag)=0x{off30 or 0:x} [ebp-8]=0x{off8 or 0:x}",
    )
# cpu.add_logpoint(0x174beb6e, _literal_producer_probe)  # 2026-08-28: confirmed 0 hits -- not the live producer

# 2026-08-28: Molly found 8 total sites in msjet35.dll that load the
# 0xfffff3e4 (-3100) literal. 7a8beb6e (above) is confirmed dead. Testing
# the next batch of candidates -- just need to know which EIP is actually
# reached for the failing call, not decode each instruction's operands.
def _make_literal_candidate_probe(label):
    def _probe(eip, regs, memory, memory_size):
        logger.error("com", f"[literal-candidate-hit] {label} EBP=0x{regs[EBP]:x} EIP=0x{eip:x}")
    return _probe
# cpu.add_logpoint(0x17026ed0, ...)  # 2026-08-28: ruled out -- FUN_7a869ced@7a8c6d6c is the live hit
# cpu.add_logpoint(0x17066695, ...)  # ruled out
# cpu.add_logpoint(0x17086d6c, _make_literal_candidate_probe(...))  # 2026-08-28: confirmed hit, superseded below

# 2026-08-28: right before this (0x7a8c6d67), FUN_7a869ced calls
# FUN_7a854cd0(param_1,param_2,(char*)param_3,(char*)0x0,0xffffe0bc,
# local_c,local_8,local_10) -- local_10 is the real detailed Jet error
# subcode from FUN_7a86756b's out-param (the specific *DAT_7a93aafc=0x27xx
# site that fired inside that giant parser). Reading all 8 pushed cdecl
# args at the call site itself (before CALL executes, so ESP is stable):
# args pushed right-to-left means [esp+0]=param_1 ... [esp+0x1c]=local_10.
def _fun_7a854cd0_callsite_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    vals = [_read32_raw(memory, memory_size, esp + i * 4) for i in range(8)]
    names = ["param_1", "param_2", "param_3(token)", "0", "resid(0xffffe0bc)", "local_c", "local_8", "local_10(errcode)"]
    parts = " ".join(f"{n}=0x{(v or 0):x}" for n, v in zip(names, vals))
    logger.error("com", f"[fun-7a854cd0-callsite] {parts}")
# cpu.add_logpoint(0x17086d67, _fun_7a854cd0_callsite_probe)  # 2026-08-28: confirmed errcode=0x2711
# cpu.add_logpoint(0x1709d8c2, ...)  # ruled out
# cpu.add_logpoint(0x170cc7a0, ...)  # ruled out

# 2026-08-28: FUN_7a869ced is real, clean code (proper params, no unaff_
# register garbage) -- calls the actual converter
# FUN_7a86756b(iVar3,param_5,local_34,param_3,param_4,&local_10) and on
# failure reports param_3 (the raw token bytes) via FUN_7a854cd0 before
# returning -0xc1c. Reading param_3/param_4 (the real offending token +
# length) live at entry, for the failing call specifically.
def _fun_7a869ced_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    p2 = _read32_raw(memory, memory_size, esp + 8)
    p3 = _read32_raw(memory, memory_size, esp + 12)
    p4 = _read32_raw(memory, memory_size, esp + 16)
    p2_text = _read_cstr_raw(memory, memory_size, p2) if p2 else "<null>"
    p3_text = _read_cstr_raw(memory, memory_size, p3) if p3 else "<null>"
    logger.error(
        "com",
        f"[fun-7a869ced-entry] param_2(ctx)=0x{p2 or 0:x} {p2_text!r} "
        f"param_3(token)=0x{p3 or 0:x} {p3_text!r} param_4(len)=0x{p4 or 0:x}",
    )
# cpu.add_logpoint(0x17029ced, _fun_7a869ced_entry_probe)  # 2026-08-28: confirmed token text, no longer needed

# 2026-08-28: FUN_7a8a7db3(param_1,param_2) is the actual date-literal
# closing-'#'-scanner (case 0x23 in the tokenizer FUN_7a8685de) -- trivial
# loop: scan forward from *param_1 for a '#' byte, fail with errcode=0x2711
# if it reaches param_2 (end-of-buffer) first. The real stored SQL
# (confirmed via mdbtools) clearly has a closing '#' right after "2010",
# so either the scan starts from the wrong position or param_2 (the
# boundary) is wrong. Reading both live, plus the text in between.
def _fun_7a8a7db3_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    p1_ptr = _read32_raw(memory, memory_size, esp + 4)
    scan_start = _read32_raw(memory, memory_size, p1_ptr) if p1_ptr else None
    p2 = _read32_raw(memory, memory_size, esp + 8)
    remaining = (p2 - scan_start) if (scan_start and p2) else None
    span_text = None
    if scan_start and p2 and 0 < remaining < 200 and scan_start + remaining < memory_size:
        n = min(remaining, 100)
        span_text = "".join(
            chr(b) if 32 <= b < 127 else f"\\x{b:02x}"
            for b in (memory[scan_start + i] for i in range(n))
        )
    logger.error(
        "com",
        f"[fun-7a8a7db3-entry] scan_start=0x{scan_start or 0:x} param_2(end)=0x{p2 or 0:x} "
        f"remaining_bytes={remaining} span={span_text!r}",
    )
# cpu.add_logpoint(0x17067db3, _fun_7a8a7db3_entry_probe)  # 2026-08-28: confirmed clean (found closing '#' fine)

# 2026-08-28: Molly identified the real culprit -- real oleaut32.dll's own
# VarDateFromStr (Ordinal #94, called from msjet35.dll @ 7a8a28a7) has a
# conditional DBCS-reprocessing branch gated on *(int*)(local_10+0x8f0)!=0
# (local_10 = a locale-info struct from FUN_77144da9). For real en-US
# (lcid=0x409) this should be false/0 -- if it's somehow true, real
# VarDateFromStr calls MultiByteToWideChar, a real kernel32 API tew
# emulates via INT 0xFE (the "wide string functions we just added").
# image_base=0x77120000 runtime_base=0x10000000 (confirmed via PE header).
# Entry: read real lcid/dwFlags args. Decision point (0x7716dadb, right
# after `MOV EAX,[EBP-0xC]` loads local_10, before `CMP [EAX+0x8F0],EBX`):
# read EAX (local_10 ptr) and dereference [EAX+0x8F0] directly.
# 2026-08-28: dbcs_branch_taken confirmed False, and zero GetLocaleInfoA/W
# (or any kernel32 NLS) calls occur anywhere in this whole run -- so
# VarDateFromStr's own state machine runs entirely on real code with no
# tew intervention. Ruled that theory out. Next: verify the actual wide
# BSTR content live -- if tew's earlier ANSI->wide BSTR construction for
# this literal put anything wrong in memory (extra chars, bad length,
# missing/misplaced null), real VarDateFromStr would legitimately fail on
# genuinely bad input even though its own code is untouched.
def _vardatefromstr_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    strin = _read32_raw(memory, memory_size, esp + 4)
    lcid = _read32_raw(memory, memory_size, esp + 8)
    dwflags = _read32_raw(memory, memory_size, esp + 12)
    chars = []
    if strin:
        addr = strin
        for _ in range(40):
            b0 = memory[addr] if addr < memory_size else None
            b1 = memory[addr + 1] if addr + 1 < memory_size else None
            if b0 is None or b1 is None:
                break
            code = b0 | (b1 << 8)
            if code == 0:
                break
            chars.append(chr(code) if 32 <= code < 127 else f"\\u{code:04x}")
            addr += 2
    wide_text = "".join(chars)
    # BSTR length prefix lives at strin-4 (real OLE BSTR layout)
    bstr_len = _read32_raw(memory, memory_size, strin - 4) if strin else None
    logger.error(
        "com",
        f"[VarDateFromStr-entry] strIn=0x{strin or 0:x} lcid=0x{lcid or 0:x} dwFlags=0x{dwflags or 0:x} "
        f"bstr_byte_len={bstr_len} wide_text={wide_text!r}",
    )
# cpu.add_logpoint(0x1004da97, _vardatefromstr_entry_probe)  # 2026-08-28: confirmed clean input, no longer needed

# 2026-08-28: input is confirmed clean (bstr_byte_len=16, wide_text=
# '1/1/2010', correct lcid/dwFlags). Need to know: does VarDateFromStr
# itself actually return success or failure for this call? Its real
# internal code uses FLD/FSTP (x87 FPU) for date-serial math near its
# single RET (0x7716dfd7) -- if tew's FPU emulation has a subtle bug
# (same shape as the DAO-3075 root cause: a CPU-instruction-emulation
# bug, not a Jet logic bug), that's exactly where it'd surface.
def _vardatefromstr_return_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    signed = eax - 0x100000000 if eax >= 0x80000000 else eax
    logger.error("com", f"[VarDateFromStr-return] EAX=0x{eax & 0xffffffff:x} ({signed})")
cpu.add_logpoint(0x1004dfd7, _vardatefromstr_return_probe)

# 2026-08-28: VarDateFromStr confirmed to genuinely return
# DISP_E_TYPEMISMATCH (0x80020005) on clean input with zero tew
# involvement. Tracing its internal table-driven state machine to see
# exactly where it rejects "1/1/2010" -- local_8 (state) at [EBP-4],
# local_54 (token type from FUN_7716d562) at [EBP-0x50], both confirmed
# via raw byte decode. Probing the computed jump at 0x7716dc6e (dispatches
# on local_54, gated by local_8<=0xc) to trace each iteration.
def _vardatefromstr_state_probe(eip, regs, memory, memory_size):
    ebp = regs[EBP]
    state = _read32_raw(memory, memory_size, ebp - 4)
    token_type = _read32_raw(memory, memory_size, ebp - 0x50)
    logger.error(
        "com",
        f"[VarDateFromStr-state] state(local_8)=0x{(state or 0) & 0xff:x} "
        f"token_type(local_54)=0x{(token_type or 0) & 0xff:x}",
    )
# cpu.add_logpoint(0x1004dc6e, _vardatefromstr_state_probe)  # 2026-08-28: confirmed token_type=4 is correct, no longer needed

# 2026-08-28: token_type=4 turned out to be CORRECT (number+separator
# classification for "1/", per FUN_7716bde4's real logic) -- not a bug,
# and the single probe hit doesn't mean immediate failure; later states
# likely route through the specialized field-parser states (0xe-0x11,
# calling FUN_7716c53f/c6ca/c85f/c8bf) which bypass this exact jump.
# FUN_771357da is VarDateFromStr's own final validator, called only on a
# clean finish (state==0, token_type==0, i.e. end-of-string reached
# cleanly). If it never fires, the loop errored out mid-parse in one of
# those field-parser states instead. Probing its entry for the real
# accumulated UDATE fields (year/month/day/hour/min/sec) it receives.
def _final_date_validator_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    udate_ptr = _read32_raw(memory, memory_size, esp + 4)
    fields = None
    if udate_ptr:
        raw = [_read32_raw(memory, memory_size, udate_ptr + i * 4) for i in range(3)]
        fields = raw
    logger.error(
        "com",
        f"[FUN_771357da-entry (final date validator)] udate_ptr=0x{udate_ptr or 0:x} raw_dwords={fields}",
    )
# cpu.add_logpoint(0x100157da, _final_date_validator_entry_probe)  # 2026-08-28: confirmed never reached

# 2026-08-28: parse dies mid-way after the first token (number+separator
# "1/"), never reaching the final validator. The specialized field-parser
# states (0xe-0x11 in VarDateFromStr's outer switch) call one of these 4
# functions via a function pointer: FUN_7716c53f, FUN_7716c6ca,
# FUN_7716c85f, FUN_7716c8bf -- each takes (&local_80,&local_3c,local_10,
# dwFlags) and a 0 return means failure (goto switchD_7716dd8a_caseD_d).
# Shared hit-probe: just need to know which one fires and its return.
# 2026-08-28: FUN_7716c53f's own decompile shows `iVar3 =
# *(int*)((int)param_3+0x10);` -- a 3-way switch (0/1/2) on the locale
# struct's +0x10 field, matching real LOCALE_IDATE semantics (0=M/D/Y,
# 1=D/M/Y, 2=Y/M/D). param_3 is local_10 (the locale struct), passed as
# the 3rd call arg -- reading it directly + dereferencing +0x10 live.
def _make_field_parser_probe(label, entry_addr):
    def _entry(eip, regs, memory, memory_size):
        esp = regs[ESP]
        param1 = _read32_raw(memory, memory_size, esp + 4)  # &local_80 in VarDateFromStr
        local_10 = _read32_raw(memory, memory_size, esp + 12)
        date_order = _read32_raw(memory, memory_size, local_10 + 0x10) if local_10 else None
        this_00_val = _read32_raw(memory, memory_size, param1 + 4) if param1 else None
        this_val = _read32_raw(memory, memory_size, param1 + 8) if param1 else None
        # 2026-08-28: Molly's request -- re-verify caltype(+0xd7e) and the
        # date-separator string(+0x44) from the SAME local_10 instance, at
        # the SAME instant, to settle whether the earlier "caltype=0 but
        # separator works" observation was a real contradiction or just
        # two facts pulled from different runs/instances.
        caltype = _read16_raw(memory, memory_size, local_10 + 0xd7e) if local_10 else None
        # 2026-08-28: Molly asked whether we're seeing a genuinely-tagged
        # struct that failed to populate, or one that was never tagged at
        # all -- reading the struct's own internal LCID field (+0x8, set
        # via `*(LCID*)(this+8)=param_1;` in FUN_77145656) to settle it.
        struct_lcid = _read32_raw(memory, memory_size, local_10 + 8) if local_10 else None
        sep_chars = []
        if local_10:
            addr = local_10 + 0x44
            for _ in range(8):
                c = _read16_raw(memory, memory_size, addr)
                if not c:
                    break
                sep_chars.append(chr(c) if 32 <= c < 127 else f"\\u{c:04x}")
                addr += 2
        sep_text = "".join(sep_chars)
        logger.error(
            "com",
            f"[field-parser-entry] {label} EIP=0x{eip:x} local_10=0x{local_10 or 0:x} "
            f"date_order(+0x10)={date_order} param3_slot_addr=0x{esp + 12:x} "
            f"param1(&local_80)=0x{param1 or 0:x} this_00_addr=0x{(param1 or 0)+4:x} "
            f"this_00_val=0x{this_00_val if this_00_val is not None else -1:x} "
            f"this_addr=0x{(param1 or 0)+8:x} this_val=0x{this_val if this_val is not None else -1:x} "
            f"caltype(+0xd7e)={caltype} date_sep(+0x44)={sep_text!r} "
            f"struct_lcid(+0x8)=0x{struct_lcid if struct_lcid is not None else -1:x}",
        )
    return _entry
# cpu.add_logpoint(0x1004c53f, _make_field_parser_probe("FUN_7716c53f", 0x1004c53f))  # 2026-08-28: root cause found upstream, freeing slot
# cpu.add_logpoint(0x1004c6ca, ...)  # 2026-08-28: confirmed FUN_7716c53f is the one that fires, freeing these 3
# cpu.add_logpoint(0x1004c85f, ...)
# cpu.add_logpoint(0x1004c8bf, ...)

# 2026-08-28: FUN_7716c53f fires, VarDateFromStr returns failure 1ms
# later, nothing else in between -- but FUN_7716c53f's own decompile only
# shows ONE visible RET (0x7716c600, the `return 0` failure path right
# after XOR EAX,EAX); the success path is a disconnected tail elsewhere.
# Simpler from the caller's side: VarDateFromStr reads the real return
# value right after its indirect call (0x7716de91 CALL -> 0x7716de93
# TEST EAX,EAX). Probing there directly settles success/failure cleanly.
def _field_parser_callsite_return_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    logger.error("com", f"[field-parser-callsite-return] EAX=0x{eax & 0xffffffff:x} (0=failure)")
# cpu.add_logpoint(0x1004de93, _field_parser_callsite_return_probe)  # 2026-08-28: confirmed succeeds both times, freeing slot

# 2026-08-28: Molly wants 0x7716dd61 logged -- part of the `case 2: case
# 4:` handling in the outer switch(local_8) (`if (local_54==2 ||
# local_54==5) {...}`), a code path our one observed state transition
# (state=0,token=4) doesn't obviously lead through. Checking live rather
# than assuming it's unreached.
def _addr_7716dd61_probe(eip, regs, memory, memory_size):
    ebp = regs[EBP]
    state = _read32_raw(memory, memory_size, ebp - 4)
    token_type = _read32_raw(memory, memory_size, ebp - 0x50)
    logger.error(
        "com",
        f"[addr-7716dd61] EIP=0x{eip:x} state(local_8)=0x{(state or 0) & 0xff:x} "
        f"token_type(local_54)=0x{(token_type or 0) & 0xff:x} EAX=0x{regs[EAX]:x}",
    )
# cpu.add_logpoint(0x1004dd61, _addr_7716dd61_probe)  # 2026-08-28: confirmed unrelated to local_54, freeing slot

def _read16_raw(memory, memory_size, addr):
    addr &= 0xFFFFFFFF
    if addr + 1 >= memory_size:
        return None
    return memory[addr] | (memory[addr + 1] << 8)

def _addr_7716dd7d_probe(eip, regs, memory, memory_size):
    ebp = regs[EBP]
    state = _read32_raw(memory, memory_size, ebp - 4)
    token_type = _read32_raw(memory, memory_size, ebp - 0x50)
    # local_3c (UDATE.st) fields, offsets confirmed via raw byte decode of
    # the init block at 0x7716dbf0-dc07 (matches real SYSTEMTIME layout,
    # wDayOfWeek at -0x34 unwritten/skipped).
    wyear = _read16_raw(memory, memory_size, ebp - 0x38)
    wmonth = _read16_raw(memory, memory_size, ebp - 0x36)
    wday = _read16_raw(memory, memory_size, ebp - 0x32)
    whour = _read16_raw(memory, memory_size, ebp - 0x30)
    wmin = _read16_raw(memory, memory_size, ebp - 0x2E)
    wsec = _read16_raw(memory, memory_size, ebp - 0x2C)
    logger.error(
        "com",
        f"[addr-7716dd7d] EIP=0x{eip:x} state(local_8)=0x{(state or 0) & 0xff:x} "
        f"token_type(local_54)=0x{(token_type or 0) & 0xff:x} EAX=0x{regs[EAX]:x} "
        f"udate: Y=0x{wyear if wyear is not None else -1:x} M=0x{wmonth if wmonth is not None else -1:x} "
        f"D=0x{wday if wday is not None else -1:x} h=0x{whour if whour is not None else -1:x} "
        f"m=0x{wmin if wmin is not None else -1:x} s=0x{wsec if wsec is not None else -1:x}",
    )
# cpu.add_logpoint(0x1004dd7d, _addr_7716dd7d_probe)  # 2026-08-28: state sequence well established, freeing slot

# 2026-08-28: 0x7716dd7d is `CMP [EBP-4],0x16` (local_8 vs 0x16), followed
# by JA at 0x7716dd81 -> target 0x7716dfc3 (rel32 0x23c, computed:
# 0x7716dd87+0x23c). Fallthrough-on-not-taken lands at 0x7716dd87 (loads
# EAX=local_8 for the real jump table at 0x7716dd8a). Probing both to
# settle live whether JA is ever actually taken for our 4 observed states
# (0,3,0xe,0xd -- all <=0x16, so it shouldn't be, but verifying not assuming).
def _addr_7716dd87_probe(eip, regs, memory, memory_size):
    state = _read32_raw(memory, memory_size, regs[EBP] - 4)
    logger.error("com", f"[addr-7716dd87 (JA-not-taken)] state(local_8)=0x{(state or 0) & 0xff:x}")
# cpu.add_logpoint(0x1004dd87, _addr_7716dd87_probe)  # 2026-08-28: confirmed pattern, freeing slot to stay under cap

def _addr_7716dfc3_probe(eip, regs, memory, memory_size):
    state = _read32_raw(memory, memory_size, regs[EBP] - 4)
    logger.error("com", f"[addr-7716dfc3 (JA-taken, state>0x16)] state(local_8)=0x{(state or 0) & 0xff:x}")
# cpu.add_logpoint(0x1004dfc3, _addr_7716dfc3_probe)  # 2026-08-28: confirmed shared default/fail handler, freeing slot

def _addr_7716dc8e_probe(eip, regs, memory, memory_size):
    state = _read32_raw(memory, memory_size, regs[EBP] - 4)
    logger.error(
        "com",
        f"[addr-7716dc8e] state(local_8)=0x{(state or 0) & 0xff:x} "
        f"EAX=0x{regs[EAX]:x} ECX=0x{regs[ECX]:x} EDX=0x{regs[EDX]:x}",
    )
# cpu.add_logpoint(0x1004dc8e, _addr_7716dc8e_probe)  # 2026-08-28: confirmed 0 hits, freeing slot

# 2026-08-28: tracing where the "year"=0x101 value in local_3c actually
# comes from -- FUN_77165b74 (called from FUN_7716c53f as `uVar4 =
# FUN_77165b74(param_4)`) calls real GetLocalTime() unconditionally at
# its top, and on the simple path just returns `local_14.wYear` raw.
# tew's own GetLocalTime handler (_get_local_time in kernel32_io.py) is
# completely silent (no logger call at all) so grepping the log for it
# proves nothing -- probing FUN_77165b74's real entry+return directly.
def _fun_77165b74_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    p1 = _read32_raw(memory, memory_size, esp + 4)
    logger.error("com", f"[FUN_77165b74-entry] param_1(flags byte)=0x{(p1 or 0) & 0xff:x}")
# cpu.add_logpoint(0x10045b74, _fun_77165b74_entry_probe)  # 2026-08-28: confirmed param_1=0, freeing slot

def _fun_77165b74_return_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    logger.error("com", f"[FUN_77165b74-return] EAX(year)=0x{eax & 0xffffffff:x} ({eax & 0xffff})")
# cpu.add_logpoint(0x10045c05, _fun_77165b74_return_probe)  # 2026-08-28: confirmed correct (2026), freeing slot

# 2026-08-28: FUN_77165b74 confirmed returns the CORRECT year (0x7ea=2026)
# but the value written into local_3c via FUN_7716c455 is 0x101. The
# corruption happens somewhere between those two points, inside
# FUN_7716c53f. Reading the real pushed args directly at the CALL site
# (0x7716c6b8) settles it -- cdecl right-to-left push means at the CALL
# instruction: [esp+0]=local_3c ptr, [esp+4]=uVar1(year), [esp+8]=
# this_00(month), [esp+12]=uVar6(day).
def _fun_7716c455_callsite_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    dst = _read32_raw(memory, memory_size, esp + 0)
    year = _read32_raw(memory, memory_size, esp + 4)
    month = _read32_raw(memory, memory_size, esp + 8)
    day = _read32_raw(memory, memory_size, esp + 12)
    logger.error(
        "com",
        f"[fun-7716c455-callsite] dst=0x{dst or 0:x} year=0x{(year or 0) & 0xffff:x} "
        f"month=0x{(month or 0) & 0xffff:x} day=0x{(day or 0) & 0xffff:x}",
    )
# cpu.add_logpoint(0x1004c6b8, _fun_7716c455_callsite_probe)  # 2026-08-28: root cause found upstream, freeing slot

# 2026-08-28: 0x7713ceec is `CMP dword[0x771a1030],0` inside FUN_7713cee1
# -- falling through (JNZ not taken) goes straight to real GetLocaleInfoW
# (CALL [0x77121160]); taken jumps to the GetLocaleInfoA path instead.
# Reading DAT_771a1030's live value directly to confirm which branch,
# rather than infer it.
def _addr_7713ceec_probe(eip, regs, memory, memory_size):
    val = _read32_raw(memory, memory_size, 0x10081030)
    logger.error("com", f"[addr-7713ceec] DAT_771a1030=0x{val if val is not None else -1:x} (0=WideChar path, nonzero=Ansi path)")
cpu.add_logpoint(0x1001ceec, _addr_7713ceec_probe)

# 2026-08-28: two more reads of [EBP+0x10] (the uVar4/year spill slot)
# found in the raw bytes between the date_order==0 branch (0x7716c644)
# and the FUN_7716c455 call: `MOV EBX,[EBP+0x10]` at 0x7716c675, and
# `PUSH [EBP+0x10]` at 0x7716c687. Watchpoint confirmed no WRITE happens
# to this address between the correct spill and the corrupted use, so if
# corruption exists it must show up in one of these READS. Probing both
# live rather than trust the (possibly incomplete, same pattern as
# FUN_7716c3a5's real signature elsewhere) decompile.
def _addr_7716c675_probe(eip, regs, memory, memory_size):
    ebx = regs[EBX]
    logger.error("com", f"[addr-7716c675 (MOV EBX,[EBP+0x10])] EBX(after)=0x{ebx:x}")
# cpu.add_logpoint(0x1004c678, _addr_7716c675_probe)  # 2026-08-28: confirmed 0 hits, path not taken -- freeing slot

def _addr_7716c687_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    top_of_stack = _read32_raw(memory, memory_size, esp)
    logger.error("com", f"[addr-7716c687 (PUSH [EBP+0x10])] value_pushed=0x{top_of_stack or 0:x}")
# cpu.add_logpoint(0x1004c68a, _addr_7716c687_probe)  # 2026-08-28: confirmed 0 hits, path not taken -- freeing slot

# 2026-08-28: neither prior guess about the branch path panned out --
# instrumenting the real decision points directly instead of tracing
# static bytes further. Two conditional jumps, each preceded by
# TEST EAX,EAX (reading the just-called FUN_7716c3a5-style helper's
# result): 0x7716c65f (JNZ -> 0x7716c5b9, after the FIRST helper call
# using "this"/EDI) and 0x7716c681 (JZ -> 0x7716c68c, after the SECOND
# helper call). Probing both to see live which way each actually goes.
def _addr_7716c65f_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    logger.error("com", f"[addr-7716c65f (JNZ after 1st helper)] EAX=0x{eax:x} taken={eax != 0}")
# cpu.add_logpoint(0x1004c65f, _addr_7716c65f_probe)  # 2026-08-28: confirmed not taken, freeing slot

def _addr_7716c681_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    logger.error("com", f"[addr-7716c681 (JZ after 2nd helper)] EAX=0x{eax:x} taken={eax == 0}")
# cpu.add_logpoint(0x1004c681, _addr_7716c681_probe)  # 2026-08-28: confirmed 0 hits, freeing slot

# 2026-08-28: found the real shortcut -- 0x7716c697-c69d:
# `PUSH 1; PUSH EDI; PUSH [EBP-4]; JMP 0x7716c6b5` lands right at the
# FUN_7716c455 arg-push site. [EBP-4] is set way earlier at 0x7716c57d
# (`MOV [EBP-4],EAX`, right after the SECOND FUN_7716c47d call) --
# matching a DIFFERENT `uVar1=(undefined2)iVar3` assignment in the
# decompile than the GetLocalTime-derived one. FUN_77165b74's correct
# 2026 may just be dead/unused on this branch. Confirming live.
def _addr_7716c580_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    logger.error("com", f"[addr-7716c580 (MOV [EBP-4],EAX after 2nd FUN_7716c47d)] EAX=0x{eax:x}")
# cpu.add_logpoint(0x1004c580, _addr_7716c580_probe)  # 2026-08-28: confirmed matches c575's input, freeing slot

# 2026-08-28: FUN_7716c47d is __thiscall(this=ECX) with an IMPLICIT
# in_EAX input (Ghidra's "in_EAX" convention -- the real value comes from
# whatever's in EAX at entry, set by the caller's `MOV EAX,EBX` at
# 0x7716c571 right before this call, `MOV ECX,ESI` at 0x7716c573). this=
# the locale struct. Checking Molly's locale hypothesis directly: read
# EAX (the real input) and ECX (locale struct ptr), then dereference its
# +0xd7e (calendar-type short) and +0xd78 (cached "current year" short)
# fields -- these drive the century-windowing/calendar-conversion logic.
def _addr_7716c575_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    ecx = regs[ECX]
    caltype = _read16_raw(memory, memory_size, ecx + 0xd7e) if ecx else None
    cached_year = _read16_raw(memory, memory_size, ecx + 0xd78) if ecx else None
    logger.error(
        "com",
        f"[addr-7716c575 (call FUN_7716c47d)] in_EAX=0x{eax:x} this(locale)=0x{ecx:x} "
        f"caltype(+0xd7e)={caltype} cached_year(+0xd78)={cached_year}",
    )
# cpu.add_logpoint(0x1004c575, _addr_7716c575_probe)  # 2026-08-28: confirmed input already 0x1010101, freeing slot

# 2026-08-28: EBX gets loaded at 0x7716c54e (`MOV EBX,[ESI+8]` = "this",
# decompile's *(param_1+8)) BEFORE the first FUN_7716c47d call at
# 0x7716c560 -- but it's read again at 0x7716c571 (`MOV EAX,EBX`) AFTER
# that call. If FUN_7716c47d doesn't preserve EBX properly, that's where
# 1 could become 0x01010101. Checking the value right at the load, before
# any call has a chance to touch it.
def _addr_7716c551_probe(eip, regs, memory, memory_size):
    logger.error("com", f"[addr-7716c551 (MOV EBX,[ESI+8]='this', pre-call)] EBX=0x{regs[EBX]:x}")
# cpu.add_logpoint(0x1004c551, _addr_7716c551_probe)  # 2026-08-28: confirmed already-corrupted pre-call, freeing slot

# 2026-08-28: FUN_7716cb38 is the real digit-string-to-integer converter
# (`*param_3=iVar4; return 0;` at its single RET, 0x7716ce5a). Its own
# logic looks completely correct for a simple "1" input. Checking live
# whether IT already produces 0x01010101 (meaning the bug is inside this
# function or its own execution) or a clean 1 (meaning the bug is in the
# copy-into-accumulator step in FUN_7716d562 right after this returns).
_cb38_last_param3 = [None]
def _fun_7716cb38_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    p2 = _read32_raw(memory, memory_size, esp + 8)
    p3 = _read32_raw(memory, memory_size, esp + 12)
    _cb38_last_param3[0] = p3
    text = _read_cstr_raw(memory, memory_size, p2) if p2 else "<null>"
    logger.error(
        "com",
        f"[FUN_7716cb38-entry] param_2(digits)=0x{p2 or 0:x} text={text!r} param_3(out)=0x{p3 or 0:x}",
    )
# cpu.add_logpoint(0x1004cb38, _fun_7716cb38_entry_probe)  # 2026-08-28: confirmed correct, freeing slot

def _fun_7716cb38_return_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    p3 = _cb38_last_param3[0]
    out_val = _read32_raw(memory, memory_size, p3) if p3 else None
    logger.error(
        "com",
        f"[FUN_7716cb38-return] EAX(hresult)=0x{eax:x} *param_3(computed int)=0x{out_val if out_val is not None else -1:x}",
    )
# cpu.add_logpoint(0x1004ce5a, _fun_7716cb38_return_probe)  # 2026-08-28: confirmed correct, freeing slot

# 2026-08-28: Molly wants FUN_77145656's real live params -- it's
# __thiscall, so `this` arrives in ECX at entry; param_1/2/3 (lcid,
# flags, passthrough) are pushed on the stack normally (esp+4/+8/+12 at
# entry, before this function's own prologue runs).
def _fun_77145656_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    this_ptr = regs[ECX]
    p1_lcid = _read32_raw(memory, memory_size, esp + 4)
    p2_flags = _read32_raw(memory, memory_size, esp + 8)
    p3 = _read32_raw(memory, memory_size, esp + 12)
    logger.error(
        "com",
        f"[FUN_77145656-entry] this=0x{this_ptr:x} lcid=0x{p1_lcid or 0:x} "
        f"flags=0x{p2_flags or 0:x} param_3=0x{p3 or 0:x}",
    )
cpu.add_logpoint(0x10025656, _fun_77145656_entry_probe)

# 2026-08-28: Molly wants absolute proof, not inference -- probing
# FUN_7713cee1's own entry directly (the function that unconditionally
# calls real GetLocaleInfoW/GetLocaleInfoA at its very top, no early-out
# before that call). If this NEVER fires, it's definitive: nothing in
# FUN_77145656 ever reaches even the first real locale-info query.
def _fun_7713cee1_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    lcid = _read32_raw(memory, memory_size, esp + 4)
    lctype = _read32_raw(memory, memory_size, esp + 8)
    logger.error("com", f"[FUN_7713cee1-entry] lcid=0x{lcid or 0:x} lctype=0x{lctype or 0:x}")
cpu.add_logpoint(0x1001cee1, _fun_7713cee1_entry_probe)

# 2026-08-28: FUN_7a852ef4 (call site 0x7a86388f inside FUN_7a8635de) is
# the real "Tables" catalog lookup by name for THIS invocation's node's
# table-name pointer (param_2[0x1e], offset 0x78 in the node dump above).
# First pass hardcoded this run's specific pointer value, which only
# happened to be right for the failing call and read stale/wrong memory
# for the other three -- reading it fresh from _last_node_ptr (set by the
# paired entry probe for THIS SAME invocation) instead, per the DAO-3075
# lesson: match by actual content at each hop, don't assume a value stays
# constant across different invocations.
def _table_lookup_return_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    signed = eax - 0x100000000 if eax >= 0x80000000 else eax
    node_ptr = _last_node_ptr[0]
    name_ptr = _read32_raw(memory, memory_size, node_ptr + 0x78) if node_ptr else None
    ansi = _read_cstr_raw(memory, memory_size, name_ptr, max_len=64) if name_ptr else "<no node>"
    wide = "<no node>"
    if name_ptr:
        base_addr = ctypes.cast(memory, ctypes.c_void_p).value
        wide_raw = ctypes.string_at(base_addr + name_ptr, 64)
        wide = wide_raw.decode("utf-16-le", errors="replace").split("\x00")[0]
    logger.error(
        "com",
        f"[table-lookup-return] EAX=0x{eax & 0xffffffff:x} ({signed}) node=0x{node_ptr or 0:x} "
        f"name_ptr=0x{name_ptr or 0:x} name_as_ansi={ansi!r} name_as_wide={wide!r}",
    )
# 2026-08-28: confirmed clean/universal for all queries -- disabled.
# cpu.add_logpoint(0x17023894, _table_lookup_return_probe)

# 2026-08-28: the table-name self-lookup just above is confirmed normal/
# universal (identical for all 4 distinct queries this run, all succeed).
# Next real step in FUN_7a8635de: FUN_7a85308c (call site 0x7a8638c8),
# whose own return code (iVar5) triggers an immediate abort if negative,
# and whose out-param (local_28) feeds the "(local_28 & 0xd0) == 0" error
# check right after. Capturing iVar5 first -- a negative value here alone
# would already explain everything, no need to chase local_28 yet.
def _resolve_binding_return_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    signed = eax - 0x100000000 if eax >= 0x80000000 else eax
    node_ptr = _last_node_ptr[0]
    logger.error(
        "com",
        f"[resolve-binding-return] node=0x{node_ptr or 0:x} EAX=0x{eax & 0xffffffff:x} ({signed})",
    )
# 2026-08-28: confirmed clean/universal for all queries -- disabled.
# cpu.add_logpoint(0x170238cd, _resolve_binding_return_probe)

# 2026-08-28: FUN_044e2b5c is dao350.dll's own custom free-list
# sub-allocator (param_1 = pool object, param_2 = requested size). Neither
# of FUN_044d525b's two dynamically-bound-lookup branches fired for the
# failing StockAssembly_SelectAPT call, meaning this allocator itself
# returned NULL for the request (0x44 bytes) -- the two ways that happens:
# (a) param_2 > param_1[8] (pool's configured max size) -- an immediate
# early-exit before any real allocation work, or (b) the free-list has no
# suitable block and the pool's outer chunk source (FUN_044e2b10 or an
# indirect vtable call through *param_1) itself returns NULL. Reading the
# pool object's own fields at entry to see which.
def _pool_allocator_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    param_1 = _read32_raw(memory, memory_size, esp + 4)
    param_2 = _read32_raw(memory, memory_size, esp + 8)
    if not param_1:
        logger.error("com", f"[pool-alloc-entry] param_1=NULL param_2={param_2}")
        return
    p3 = _read32_raw(memory, memory_size, param_1 + 0xc)
    p4 = _read32_raw(memory, memory_size, param_1 + 0x10)
    p6 = _read32_raw(memory, memory_size, param_1 + 0x18)
    p7 = _read32_raw(memory, memory_size, param_1 + 0x1c)
    p8 = _read32_raw(memory, memory_size, param_1 + 0x20)
    p9 = _read32_raw(memory, memory_size, param_1 + 0x24)
    logger.error(
        "com",
        f"[pool-alloc-entry] pool=0x{param_1:x} size_req={param_2} "
        f"[3]=0x{p3 or 0:x} [4](freelist)=0x{p4 or 0:x} [6]=0x{p6 or 0:x} "
        f"[7]=0x{p7 or 0:x} [8](maxsize)=0x{p8 or 0:x} [9]=0x{p9 or 0:x}",
    )
# 2026-08-28: confirmed the pool allocator succeeds (EAX=0x7309c4c, a real
# pointer) -- disabled entry+return probes to free logpoint slots.
# cpu.add_logpoint(0x044e2b5c, _pool_allocator_entry_probe)

# Entry alone doesn't say success/failure -- the pool keeps serving many
# more allocations right after ours in the log, which cuts against "the
# allocator itself is broken." Reading its real return value directly at
# the instruction right after its CALL site inside FUN_044d525b (0x044d526c
# + 5-byte E8 rel32 = 0x044d5271) to settle it.
def _pool_allocator_return_probe(eip, regs, memory, memory_size):
    logger.error("com", f"[pool-alloc-return] EAX=0x{regs[EAX] & 0xffffffff:x}")
# cpu.add_logpoint(0x044d5271, _pool_allocator_return_probe)  # 2026-08-28: confirmed non-NULL (0x7309c4c) for the failing call too

# 2026-08-28: msjter35.dll's real JetErrFormattedMessage (Ordinal #5,
# static 0x04221088, runtime 0x1a001088 -- confirmed via DAT_044e51f8's
# resolved GetProcAddress value) computes the final translated error
# number TWICE: first from our raw -3100 (FUN_042211a7(param_1, ...)),
# then -- only if a flag bit from the first call isn't set -- a SECOND
# time from a different input (param_2[1]), OVERWRITING the first
# result before it's ever used. Molly asked whether this is a date-
# format/epoch issue; checking live whether the first computation
# yields Jet error 2421 ("Syntax error in date") specifically, and
# whether it then gets silently overridden by the second call to the
# more generic 3075 -- rather than assuming either way.
def _jeterr_first_calc_probe(eip, regs, memory, memory_size):
    logger.error("com", f"[jeterr-first-calc] uVar1(EAX)=0x{regs[EAX] & 0xffffffff:x}")
# 2026-08-28: confirmed/no longer needed, disabled to free logpoint slots.
# cpu.add_logpoint(0x1a0010c8, _jeterr_first_calc_probe)

def _jeterr_second_calc_probe(eip, regs, memory, memory_size):
    logger.error("com", f"[jeterr-second-calc] uVar1(EAX)=0x{regs[EAX] & 0xffffffff:x}")
# 2026-08-28: confirmed/no longer needed, disabled to free logpoint slots.
# cpu.add_logpoint(0x1a0010e4, _jeterr_second_calc_probe)

# 2026-08-28: neither FUN_04222652 nor FUN_042224f3 (the real message-
# template-substitution engine) call CchLszOfId2 with id=2421 anywhere --
# only once, with uVar1=3075. So the LoadStringA(id=2421) seen in the log
# must come from a caller one level above what's already logged
# ("caller=0x1b00145b" is CchLszOfId2's OWN address, confirmed by
# decompiling it -- a thin one-line wrapper). Hooking CchLszOfId2's entry
# directly to read its real caller's return address off the stack.
def _cchlszofid2_entry_probe(eip, regs, memory, memory_size):
    esp = regs[ESP]
    ret_addr = _read32_raw(memory, memory_size, esp + 0)
    string_id = _read32_raw(memory, memory_size, esp + 4)
    logger.error(
        "com",
        f"[cchlszofid2-entry] id={string_id} real_caller=0x{ret_addr or 0:x}",
    )
# 2026-08-28: confirmed/no longer needed, disabled to free logpoint slots.
# cpu.add_logpoint(0x1b00145b, _cchlszofid2_entry_probe)

def _dbparamquery_getcount_local18_probe(eip, regs, memory, memory_size):
    eax = regs[EAX]
    signed = eax - 0x100000000 if eax >= 0x80000000 else eax
    logger.error("com", f"[dbparamquery-getcount-local18@995c88] EAX=0x{eax:x} ({signed})")
# 2026-08-28: redundant -- always matches getcount-return exactly
# (confirmed live, __RTC_CheckEsp doesn't touch EAX). Disabled.
# cpu.add_logpoint(0x00995c88, _dbparamquery_getcount_local18_probe)

# 2026-08-28: removed the resolved 2026-08-26 CoGetMalloc/TlsSetValue/
# CoSetState/TlsAlloc probe block (oleaut32.dll lazy-COM-state-init
# investigation, fixed and merged in the DllMain milestone work) -- was
# eating 6 of the 8 available logpoint slots (see below), silently
# starving this session's own probes with no error.

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
# invalid locale -- fixed in tew/api/kernel32_io.py's _locale_is_valid.
# Removed now that it's fixed and confirmed live; full chain preserved in
# memory/changelog.md. (_prefclass_assert_probe, the breakpoint that
# confirmed the fix live, has itself since been removed the same way.)

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
