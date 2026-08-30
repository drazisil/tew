"""Crash analysis and diagnostic reporting for the emulator."""

from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from tew.hardware.cpu_zig import REG_NAMES, ESP, EBP
from tew.logger import logger

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory
    from tew.api._state import CRTState
    from tew.loader.import_resolver import ImportResolver

# Real address of the debug CRT's _CrtDumpMemoryLeaks in MCity_d.exe (confirmed
# via Ghidra decompile 2026-08-29 -- statically linked debug CRT, present and
# real, not a stub). Calling it walks the debug heap's block list and reports
# every still-live allocation via _CrtDbgReport, which patch_internals.py's
# _crt_dbg_report handler already logs/forwards for the _CRT_WARN report type
# -- that's the same mechanism that normally only fires on a clean process
# exit (the CRT's own atexit-driven leak check). Invoking it here gets a real,
# per-block leak report (with source file+line, since every operator new call
# seen in this binary already uses the debug-instrumented 4-arg overload) at
# the moment of an unhandled fault instead, where no clean exit ever happens.
_CRT_DUMP_MEMORY_LEAKS_ADDR = 0x009F81B0

LogFn = Callable[[str, str], None]

# Structured crash dump written by diagnose_fault/diagnose_halt, consumed by
# tools/crashlog_reader.py instead of re-parsing /tmp/emu.log for DLL table /
# register / stack / EBP-chain data. Overwritten every run, same path always
# (matches /tmp/emu.log convention).
CRASH_LOG_PATH = Path("/tmp/emu_crash.json")

# Static address ranges outside any loaded DLL. Order matters -- checked
# top to bottom, first match wins (ranges don't overlap today, but keep the
# order stable in case that ever changes). end=None means "no upper bound".
_MEMORY_REGIONS: list[tuple[int, int | None, str]] = [
    (0x00400000, 0x02200000, "exe"),         # full PE image range
    (0x00200000, 0x00220000, "stub"),
    (0x7FFF0000, None, "main stack"),
    (0x08000000, 0x09000000, "thread stack"),
    (0x04000000, 0x08000000, "heap"),
    (0x40000000, None, "VirtualAlloc"),
]


def _classify_static_region(value: int) -> str | None:
    for start, end, label in _MEMORY_REGIONS:
        if value >= start and (end is None or value < end):
            return label
    return None


def _annotate_address(value: int, import_resolver: "ImportResolver | None") -> str:
    if import_resolver:
        dll = import_resolver.find_dll_for_address(value)
        if dll:
            return f"  ← {dll['name']}+0x{value - dll['base_address']:x}"
    region = _classify_static_region(value)
    return f"  ← {region}" if region else ""


def _walk_ebp_chain(
    cpu: "CPU",
    import_resolver: "ImportResolver | None",
    ebp_val: int,
    log_fn: LogFn,
    category: str,
    max_frames: int = 32,
) -> None:
    """Reconstructs the call stack from saved frame pointers (fs:[EBP]/[EBP+4] chain)."""
    log_fn(category, "EBP chain (call frames):")
    frame_ebp = ebp_val
    frame_depth = 0
    seen = set()
    while frame_depth < max_frames and frame_ebp and cpu.memory.is_valid_address(frame_ebp):
        if frame_ebp in seen:
            log_fn(category, f"  frame[{frame_depth}] EBP=0x{frame_ebp:08x} (cycle — stopping)")
            break
        seen.add(frame_ebp)
        try:
            saved_ebp = cpu.memory.read32(frame_ebp) & 0xFFFFFFFF
            ret_addr = cpu.memory.read32(frame_ebp + 4) & 0xFFFFFFFF
        except Exception:
            log_fn(category, f"  frame[{frame_depth}] EBP=0x{frame_ebp:08x} (read error)")
            break
        log_fn(
            category,
            f"  frame[{frame_depth}] EBP=0x{frame_ebp:08x}  ret=0x{ret_addr:08x}{_annotate_address(ret_addr, import_resolver)}",
        )
        frame_ebp = saved_ebp
        frame_depth += 1


def _dump_cpu_state(
    cpu: "CPU",
    import_resolver: "ImportResolver | None",
    log_fn: LogFn,
    category: str,
    stack_slots: int,
    annotate_validity: bool = False,
) -> None:
    """Shared register + stack + EBP-chain dump used by every diagnostic entry point.

    The single place that reads cpu.regs[i] for all 8 GPRs -- previously
    diagnose_fault/diagnose_halt/diagnose_thread_end each carried their own
    copy of this loop, so a register (e.g. ESI) added to one didn't reach
    the others. All 8 REG_NAMES are always included; annotate_validity adds
    the fault-only [ok]/[!!] address-validity prefix.
    """
    log_fn(category, "General Purpose Registers:")
    for i in range(8):
        val = cpu.regs[i] & 0xFFFFFFFF
        if annotate_validity:
            status = "ok" if cpu.memory.is_valid_address(val) else "!!"
            log_fn(category, f"  [{status}] {REG_NAMES[i]}: 0x{val:08x}")
        else:
            log_fn(category, f"  {REG_NAMES[i]}: 0x{val:08x}")

    esp = cpu.regs[ESP] & 0xFFFFFFFF
    ebp = cpu.regs[EBP] & 0xFFFFFFFF
    log_fn(category, f"Stack: ESP=0x{esp:08x}  EBP=0x{ebp:08x}")
    log_fn(category, f"Stack dump ({stack_slots} slots):")
    for i in range(stack_slots):
        slot_addr = esp + i * 4
        try:
            value = cpu.memory.read32(slot_addr) & 0xFFFFFFFF
        except Exception:
            log_fn(category, f"  [ESP+{i*4:03x}] (read error)")
            break
        log_fn(category, f"  [ESP+{i*4:03x}] 0x{value:08x}{_annotate_address(value, import_resolver)}")

    _walk_ebp_chain(cpu, import_resolver, ebp, log_fn, category)


def _collect_register_dump(cpu: "CPU", annotate_validity: bool = False) -> dict:
    regs = {}
    for i in range(8):
        val = cpu.regs[i] & 0xFFFFFFFF
        entry = {"value": val}
        if annotate_validity:
            entry["valid"] = cpu.memory.is_valid_address(val)
        regs[REG_NAMES[i]] = entry
    return regs


def _collect_stack_dump(cpu: "CPU", import_resolver: "ImportResolver | None", stack_slots: int) -> list[dict]:
    esp = cpu.regs[ESP] & 0xFFFFFFFF
    slots = []
    for i in range(stack_slots):
        slot_addr = esp + i * 4
        try:
            value = cpu.memory.read32(slot_addr) & 0xFFFFFFFF
        except Exception:
            slots.append({"offset": i * 4, "error": "read error"})
            break
        slots.append({
            "offset": i * 4,
            "value": value,
            "annotation": _annotate_address(value, import_resolver).strip(" ←") or None,
        })
    return slots


def _collect_ebp_chain(cpu: "CPU", import_resolver: "ImportResolver | None", ebp_val: int, max_frames: int = 32) -> list[dict]:
    frames = []
    frame_ebp = ebp_val
    depth = 0
    seen = set()
    while depth < max_frames and frame_ebp and cpu.memory.is_valid_address(frame_ebp):
        if frame_ebp in seen:
            frames.append({"depth": depth, "ebp": frame_ebp, "cycle": True})
            break
        seen.add(frame_ebp)
        try:
            saved_ebp = cpu.memory.read32(frame_ebp) & 0xFFFFFFFF
            ret_addr = cpu.memory.read32(frame_ebp + 4) & 0xFFFFFFFF
        except Exception:
            frames.append({"depth": depth, "ebp": frame_ebp, "error": "read error"})
            break
        frames.append({
            "depth": depth,
            "ebp": frame_ebp,
            "ret": ret_addr,
            "annotation": _annotate_address(ret_addr, import_resolver).strip(" ←") or None,
        })
        frame_ebp = saved_ebp
        depth += 1
    return frames


def _collect_dll_table(import_resolver: "ImportResolver | None") -> list[dict]:
    if not import_resolver:
        return []
    return [
        {"name": m["dll_name"], "base": m["base_address"], "end": m["end_address"]}
        for m in import_resolver.get_address_mappings()
    ]


def _write_crash_log(kind: str, cpu: "CPU", import_resolver: "ImportResolver | None", extra: dict | None = None) -> Path:
    """Writes the structured crash dump consumed by tools/crashlog_reader.py.

    Always overwrites CRASH_LOG_PATH -- one crash file per run, matching the
    /tmp/emu.log convention of a single fixed path across a session.
    """
    esp = cpu.regs[ESP] & 0xFFFFFFFF
    ebp = cpu.regs[EBP] & 0xFFFFFFFF
    stack_slots = 64 if kind == "fault" else 16
    data = {
        "kind": kind,
        "eip": cpu.eip & 0xFFFFFFFF,
        "eip_annotation": _annotate_address(cpu.eip & 0xFFFFFFFF, import_resolver).strip(" ←") or None,
        "esp": esp,
        "ebp": ebp,
        "registers": _collect_register_dump(cpu, annotate_validity=(kind == "fault")),
        "stack_dump": _collect_stack_dump(cpu, import_resolver, stack_slots),
        "ebp_chain": _collect_ebp_chain(cpu, import_resolver, ebp),
        "dll_table": _collect_dll_table(import_resolver),
        "static_memory_map": [
            {"start": start, "end": end, "label": label}
            for start, end, label in _MEMORY_REGIONS
        ],
    }
    if extra:
        data.update(extra)
    CRASH_LOG_PATH.write_text(json.dumps(data, indent=2))
    return CRASH_LOG_PATH


def _dump_crt_memory_leaks(cpu: "CPU", memory: "Memory", state: "CRTState") -> None:
    """Calls the guest's own _CrtDumpMemoryLeaks (see _CRT_DUMP_MEMORY_LEAKS_ADDR
    above) via a nested emulated call, right before a fault is finalized.

    Real per-block leak lines land in the main log at DEBUG under
    [exception] -- patch_internals.py's _crt_dbg_report handler already logs
    every block _CrtMemDumpAllObjectsSince reports, unconditionally, before
    it even considers forwarding to the game's own registered report hook.

    That hook forward is deliberately skipped here (see the save/zero/restore
    of _CRT_REPORT_HOOK_PTR below): the hook is `crtReportHookCallback`
    (0x006881a0 in MCity_d.exe), and live-verified 2026-08-29, its own
    fallback path ends in a bare `INT 3` whenever it isn't actively mid a
    single leak-report burst (its own static bIsLeakReport flag) -- real
    MSVC debug-CRT-hook behavior, not a bug in the guest, but exactly the
    kind of nested real-guest-code side effect this diagnostic call has no
    business triggering. tew's own log line doesn't need the hook to fire at
    all, so the clean fix is to just not let this call reach it, rather than
    chase the exact nested-call/report_type interaction that trips it.
    """
    from tew.api.patch_internals import _CRT_REPORT_HOOK_PTR
    from tew.api.user32_handlers import _invoke_emulated_proc, _get_dialog_sentinel
    saved_hook_addr = memory.read32(_CRT_REPORT_HOOK_PTR)
    try:
        memory.write32(_CRT_REPORT_HOOK_PTR, 0)
        logger.info("exception",
            "Invoking guest _CrtDumpMemoryLeaks before finalizing this fault -- "
            "real per-block leak lines (if any) follow at DEBUG under [exception].")
        # Default max_steps=5_000_000 is sized for per-frame callbacks that
        # must never hang the emulator; this call is a one-shot diagnostic
        # action after the run loop has already exited (nothing else is
        # waiting on it), and live-verified 2026-08-29 that a real dump with
        # a few hundred leaked blocks already exhausts the default budget
        # partway through -- same reasoning as the increased budgets used
        # elsewhere for calls into real, substantial guest code rather than
        # a short callback.
        _invoke_emulated_proc(
            cpu, memory, _CRT_DUMP_MEMORY_LEAKS_ADDR, [],
            _get_dialog_sentinel(state, memory),
            max_steps=500_000_000,
            scheduler=state.scheduler,
        )
    except Exception as e:
        logger.warn("exception", f"_CrtDumpMemoryLeaks call for crash diagnostics failed: {e}")
    finally:
        memory.write32(_CRT_REPORT_HOOK_PTR, saved_hook_addr)


def diagnose_fault(
    cpu: "CPU",
    import_resolver: "ImportResolver | None",
    memory: "Memory | None" = None,
    state: "CRTState | None" = None,
) -> None:
    """
    Called after the run loop detects cpu.faulted == True.

    Logs a short human-readable summary; the full memory-access diagnostics,
    register dump, stack dump, EBP chain, and DLL table go to CRASH_LOG_PATH
    (see tools/crashlog_reader.py) instead of the main log.

    When memory/state are provided, also triggers a real guest-side
    _CrtDumpMemoryLeaks call first (see _dump_crt_memory_leaks) -- both are
    optional and skipped together so existing callers that don't have them
    handy keep working unchanged.
    """
    if memory is not None and state is not None:
        _dump_crt_memory_leaks(cpu, memory, state)

    error = cpu.last_error
    if error is not None:
        logger.error("exception", str(error))
    else:
        logger.error("exception", f"CPU faulted (no Python error — likely unhandled opcode or bad memory access at EIP=0x{cpu.eip:08x} opcode=0x{cpu.memory.read8(cpu.eip):02x})")

    extra: dict = {}

    match = re.search(r"0x([0-9a-fA-F]+)", str(error))
    if match:
        addr = int(match.group(1), 16)
        bounds = cpu.memory.get_bounds()
        memory_access: dict = {
            "attempted_address": addr,
            "valid_memory_range": {"start": bounds["start"], "end": bounds["end"], "size": bounds["size"]},
        }
        if import_resolver:
            dll = import_resolver.find_dll_for_address(addr)
            if dll:
                memory_access["in_dll"] = {
                    "name": dll["name"],
                    "base": dll["base_address"],
                    "end": dll["base_address"] + dll["size"] - 1,
                    "offset": addr - dll["base_address"],
                }
            else:
                memory_access["in_dll"] = None
                memory_access["looks_like_unresolved_import"] = addr < 0x00100000
        extra["memory_access"] = memory_access

    if import_resolver:
        current_dll = import_resolver.find_dll_for_address(cpu.eip)
        extra["eip_location"] = current_dll["name"] if current_dll else "Main executable"
        extra["eip_likely_unresolved_import"] = (not current_dll) and cpu.eip < 0x00100000

    path = _write_crash_log("fault", cpu, import_resolver, extra=extra)
    logger.error("exception", f"Crash details written to {path}")
    logger.error("exception", "Execution stopped.")


def diagnose_halt(cpu: "CPU", import_resolver: "ImportResolver | None") -> None:
    """
    Called after the run loop detects cpu.halted == True without a CPU fault.

    Logs EIP + location so the halt is visible at a glance in the main log;
    the register/stack/EBP-chain dump used to trace the calling game code
    goes to CRASH_LOG_PATH instead (see tools/crashlog_reader.py).
    """
    logger.error("exception", "--- Halt Diagnostic ---")
    logger.error("exception", f"EIP: 0x{cpu.eip & 0xFFFFFFFF:08x}")

    extra: dict = {}
    if import_resolver:
        dll = import_resolver.find_dll_for_address(cpu.eip)
        if dll:
            logger.error("exception", f"Location: {dll['name']}+0x{cpu.eip - dll['base_address']:x}")

    path = _write_crash_log("halt", cpu, import_resolver, extra=extra)
    logger.error("exception", f"Crash details written to {path}")


def diagnose_thread_end(
    cpu: "CPU",
    import_resolver: "ImportResolver | None",
    thread_id: int,
    stack_slots: int = 48,
) -> None:
    """Stack dump fired when a thread hits THREAD_SENTINEL (returns normally).

    Not a fault/halt diagnostic -- logged under "thread" so it can be
    filtered independently of real crash reports. Exists to answer "was a
    pushed nested-call sentinel skipped over, or overwritten?": a skipped
    sentinel still appears verbatim somewhere in the raw stack dump below
    ESP; an overwritten one is simply gone.
    """
    logger.debug("thread", f"--- Thread End Stack Dump (tid={thread_id}) ---")
    _dump_cpu_state(cpu, import_resolver, logger.debug, "thread", stack_slots=stack_slots)
