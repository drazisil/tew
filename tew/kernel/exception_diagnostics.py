"""Crash analysis and diagnostic reporting for the emulator."""

from __future__ import annotations
import re
from typing import Callable, TYPE_CHECKING

from tew.hardware.cpu_zig import REG_NAMES, ESP, EBP
from tew.logger import logger

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.loader.import_resolver import ImportResolver

LogFn = Callable[[str, str], None]


def _annotate_address(value: int, import_resolver: "ImportResolver | None") -> str:
    if import_resolver:
        dll = import_resolver.find_dll_for_address(value)
        if dll:
            return f"  ← {dll['name']}+0x{value - dll['base_address']:x}"
    if 0x00400000 <= value < 0x02200000:   # full PE image range
        return "  ← exe"
    if 0x00200000 <= value < 0x00220000:
        return "  ← stub"
    if 0x7FFF0000 <= value:
        return "  ← main stack"
    if 0x08000000 <= value < 0x09000000:
        return "  ← thread stack"
    if 0x04000000 <= value < 0x08000000:
        return "  ← heap"
    if 0x40000000 <= value:
        return "  ← VirtualAlloc"
    return ""


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


def diagnose_fault(cpu: "CPU", import_resolver: "ImportResolver | None") -> None:
    """
    Called after the run loop detects cpu.faulted == True.
    Produces a diagnostic report with memory access info, CPU state, and DLL ranges.
    """
    error = cpu.last_error
    if error is not None:
        logger.error("exception", str(error))
    else:
        logger.error("exception", f"CPU faulted (no Python error — likely unhandled opcode or bad memory access at EIP=0x{cpu.eip:08x} opcode=0x{cpu.memory.read8(cpu.eip):02x})")

    # Extract address from error message
    match = re.search(r"0x([0-9a-fA-F]+)", str(error))
    if match:
        addr = int(match.group(1), 16)
        logger.error("exception", "--- Memory Access Diagnostics ---")
        logger.error("exception", f"Attempted address: 0x{addr:08x}")

        bounds = cpu.memory.get_bounds()
        logger.error(
            "exception",
            f"Valid memory range: 0x{bounds['start']:08x}-0x{bounds['end']:08x} "
            f"({bounds['size'] // (1024 * 1024)}MB)",
        )

        if addr > 0x40000000:
            logger.error("exception", "Address is outside normal DLL range - likely segment-relative (e.g., FS:[offset])")
            fs_base = 0x7FFDD000
            potential_offset = addr - fs_base
            logger.error("exception", f"  If FS base is 0x{fs_base:08x}: offset would be 0x{potential_offset:08x}")
            logger.error("exception", "  Common TEB/PEB fields: ExceptionList=FS:[0x00], StackBase=FS:[0x04], StackLimit=FS:[0x08]")

        if import_resolver:
            dll = import_resolver.find_dll_for_address(addr)
            if dll:
                logger.error("exception", f"Address is in {dll['name']}")
                logger.error("exception", f"  Range: 0x{dll['base_address']:08x}-0x{dll['base_address'] + dll['size'] - 1:08x}")
                logger.error("exception", f"  Offset in DLL: 0x{addr - dll['base_address']:08x}")
            else:
                logger.error("exception", "Address is NOT in any loaded DLL")
                if addr < 0x00100000:
                    logger.error("exception", "Address looks like an UNRESOLVED IMPORT (value not filled in IAT / NULL pointer)")
                    logger.error("exception", "  Possible causes: missing DLL, missing export, or circular import")
                logger.error("exception", "Loaded DLL ranges:")
                for mapping in import_resolver.get_address_mappings():
                    logger.error(
                        "exception",
                        f"  0x{mapping['base_address']:08x}-0x{mapping['end_address']:08x} {mapping['dll_name']}",
                    )

    logger.error("exception", "--- CPU State ---")
    logger.error("exception", f"EIP: 0x{cpu.eip & 0xFFFFFFFF:08x}")

    if import_resolver:
        current_dll = import_resolver.find_dll_for_address(cpu.eip)
        if current_dll:
            logger.error("exception", f"Location: {current_dll['name']}")
        else:
            logger.error("exception", "Location: Main executable")
            if cpu.eip < 0x00100000:
                logger.error("exception", "LIKELY UNRESOLVED IMPORT: EIP < 1MB, indirect call through unfilled IAT entry")

    _dump_cpu_state(cpu, import_resolver, logger.error, "exception", stack_slots=64, annotate_validity=True)

    logger.error("exception", "Execution stopped.")


def diagnose_halt(cpu: "CPU", import_resolver: "ImportResolver | None") -> None:
    """
    Called after the run loop detects cpu.halted == True without a CPU fault.

    Prints the register state and a shallow stack walk so the cause of the
    halt (usually an unimplemented Win32 handler) can be traced back to the
    calling game code.
    """
    logger.error("exception", "--- Halt Diagnostic ---")
    logger.error("exception", f"EIP: 0x{cpu.eip & 0xFFFFFFFF:08x}")

    if import_resolver:
        dll = import_resolver.find_dll_for_address(cpu.eip)
        if dll:
            logger.error(
                "exception",
                f"Location: {dll['name']}+0x{cpu.eip - dll['base_address']:x}",
            )

    _dump_cpu_state(cpu, import_resolver, logger.error, "exception", stack_slots=16)


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
