"""run_exe.py — Boot and run a Win32 PE executable in the x86-32 emulator.

Usage:
    python run_exe.py [path/to/game.exe]

If no path is given, reads 'exePath' from emulator.json in the current directory.

Environment variables:
    LOG_LEVEL=trace|debug|info|warn|error   (default: info)
    LOG_CATEGORIES=startup,cpu,handlers,...  (default: all)
"""

from __future__ import annotations

import json
import os
import sys
from os.path import dirname

from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import ZigCPU as CPU, ESP, EBP, REG_NAMES
from tew.kernel.kernel_structures import KernelStructures
from tew.kernel.exception_diagnostics import diagnose_fault, diagnose_halt
from tew.emulator.opcodes import register_all_opcodes
from tew.pe.exe_file import EXEFile
from tew.api.win32_handlers import Win32Handlers
from tew.api.crt_handlers import register_crt_handlers, patch_crt_internals
from tew.api.pe_resources import PEResources
from tew.logger import logger


# ── Resolve exe path ──────────────────────────────────────────────────────────

exe_path: str = sys.argv[1] if len(sys.argv) > 1 else ""
if not exe_path:
    try:
        cfg = json.loads(open(os.path.join(os.getcwd(), "emulator.json")).read())
        exe_path = cfg.get("exePath", "")
    except Exception:
        pass
if not exe_path:
    raise SystemExit(
        "No exe path specified. Pass as CLI argument or set 'exePath' in emulator.json"
    )

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

# ── Register opcodes and Win32 stubs ──────────────────────────────────────────

register_all_opcodes(cpu)

win32_handlers = Win32Handlers(mem)
crt_state = register_crt_handlers(win32_handlers, mem, exe.import_resolver.get_dll_loader())
crt_state.exe_path = exe_path   # used by GetModuleFileNameA

# Attach PE resources so dialog templates and bitmap controls can be loaded
with open(exe_path, "rb") as _f:
    _pe_resources = PEResources(_f.read())
crt_state.pe_resources = _pe_resources
crt_state.window_manager.set_pe_resources(_pe_resources)

win32_handlers.install(cpu)

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

kernel_structures.initialize_kernel_structures(stack_base, stack_limit)

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


# ── Run loop ──────────────────────────────────────────────────────────────────

logger.info("startup", "=== Starting Emulation ===")

MAX_STEPS = 500_000_000
# Steps per batch (also the virtual-clock tick interval).
# _TIMER_waitticks spins without Sleep/SleepEx so multimedia timers never fire
# from the normal SleepEx path.  Advancing the clock here lets due callbacks fire.
_TIMER_HEARTBEAT_INTERVAL = 100_000

step_count = 0
last_valid_step = 0
last_valid_eip = 0
last_valid_region = ""
detected_runaway = False

# Resolved once on first heartbeat call.
_pending_timers = None
_invoke_emulated_proc_fn = None
_get_dialog_sentinel_fn = None
_run_background_slice_fn = None
_heartbeat_count = 0


def _run_timer_heartbeat() -> None:
    global _heartbeat_count
    global _pending_timers, _invoke_emulated_proc_fn, _get_dialog_sentinel_fn, _run_background_slice_fn
    _heartbeat_count += 1
    if crt_state.is_running_thread:
        return
    if _pending_timers is None:
        from tew.api.win32_handlers import pending_timers as _pt
        from tew.api.user32_handlers import _invoke_emulated_proc as _iep, _get_dialog_sentinel as _gds
        from tew.api.kernel32_io import _run_background_slice as _rbs
        _pending_timers = _pt
        _invoke_emulated_proc_fn = _iep
        _get_dialog_sentinel_fn = _gds
        _run_background_slice_fn = _rbs
    crt_state.virtual_ticks_ms = (crt_state.virtual_ticks_ms + 1) & 0xFFFFFFFF
    if not _pending_timers:
        return
    due = [t for t in list(_pending_timers.values()) if t.due_at <= crt_state.virtual_ticks_ms]
    if not due:
        return
    sentinel = _get_dialog_sentinel_fn(crt_state, mem)
    for timer in due:
        _invoke_emulated_proc_fn(cpu, mem, timer.cb_addr, [timer.id, 0, timer.dw_user, 0, 0], sentinel)
        if timer.period_ms > 0:
            timer.due_at += timer.period_ms
        else:
            _pending_timers.pop(timer.id, None)
    _run_background_slice_fn(cpu, mem, crt_state)


_heartbeat_countdown = _TIMER_HEARTBEAT_INTERVAL
_sample_countdown = 1_000_000
_progress_countdown = 5_000_000

while not cpu.halted and step_count < MAX_STEPS and not detected_runaway:
    eip_before = cpu.eip
    batch = min(_TIMER_HEARTBEAT_INTERVAL, MAX_STEPS - step_count)
    cpu.run(batch)
    step_count += batch

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
        logger.info(
            "startup",
            f"[alive] step={step_count:,} EIP=0x{cpu.eip & 0xFFFFFFFF:08x}"
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

if step_count >= MAX_STEPS:
    logger.warn("cpu", f"Execution limit reached ({MAX_STEPS} steps)")

# ── Post-run reporting ────────────────────────────────────────────────────────

logger.info("startup", "=== Emulation Complete ===")
logger.info("startup", f"Steps executed: {cpu._step_count}")

logger.debug("handlers", "--- Win32 Stub Call Log (last 50) ---")
for call in win32_handlers.get_call_log()[-50:]:
    logger.debug("handlers", f"  {call}")

if cpu.faulted:
    diagnose_fault(cpu, exe.import_resolver)
elif cpu.halted:
    diagnose_halt(cpu, exe.import_resolver)

logger.info("startup", f"Final EIP: 0x{cpu.eip & 0xFFFFFFFF:08x}")

sys.exit(1 if cpu.faulted else 0)
