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
from tew.logger import logger, set_thread_id_provider


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
# DISABLED for now (2026-08-07): confirmed live that this is NOT
# lightweight in practice -- it hooks every single memory write and every
# register/EIP/EFLAGS change for the whole run, and the periodic HTTP
# flush to ClickHouse can't keep up with that volume (a run stalled at
# 83s of virtual time after 2+ minutes of real wall-clock time, RSS
# climbing past 2.3GB as the unflushed buffer piled up in memory). Also:
# the FILE_allocateop/Dbcode_AtExit blocker this was wired up for turned
# out to be downstream of a separate, earlier bug (IDirect3D8::Release
# always reporting refcount 0 -- see idirect3d8.py -- which never even
# let a run reach dbcode.c's failure point). Flip _HISTORY_CAPTURE_ENABLED
# back on only if that investigation resumes and is worth the overhead.
_HISTORY_CAPTURE_ENABLED = False
if _HISTORY_CAPTURE_ENABLED:
    cpu.enable_history_capture_clickhouse("http://localhost:8123", "default", "poc")
    logger.info("startup", "[history] ClickHouse capture enabled, run_id will be queryable after the run")


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
            logger.warn("seh", f"CPU fault at EIP=0x{fault_eip:08x} -- attempting SEH dispatch")
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
            detected_runaway = True
            logger.error("cpu", f"RUNAWAY DETECTED at step {step_count}")
            logger.error("cpu", f"  Current EIP: 0x{cpu.eip & 0xFFFFFFFF:08x} (INVALID)")
            logger.error(
                "cpu",
                f"  Last valid step: {last_valid_step},"
                f" EIP: 0x{last_valid_eip & 0xFFFFFFFF:08x} in {last_valid_region}",
            )
            try:
                raw = [f"{mem.read8(cpu.eip + i):02x}" for i in range(16)]
                logger.error("cpu", f"  Bytes at EIP: {' '.join(raw)}")
            except Exception:
                logger.error("cpu", "  Bytes at EIP: (out of bounds)")
            logger.error("cpu", "  Registers at crash:")
            for i in range(8):
                val = cpu.regs[i] & 0xFFFFFFFF
                logger.error("cpu", f"    {REG_NAMES[i]}: 0x{val:08x}")
            esp_val = cpu.regs[ESP] & 0xFFFFFFFF
            logger.error("cpu", "  Stack at crash (top 32):")
            for i in range(32):
                try:
                    slot = mem.read32(esp_val + i * 4) & 0xFFFFFFFF
                    logger.error("cpu", f"    [ESP+{i*4:02x}] 0x{slot:08x}")
                except Exception:
                    break
            logger.error("cpu", "  Last 30 Win32 handler calls:")
            for call in win32_handlers.get_call_log()[-30:]:
                logger.error("cpu", f"    {call}")
            # Run a few more steps to capture the pattern
            for _ in range(20):
                if cpu.halted:
                    break
                cpu.step()
                step_count += 1
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
