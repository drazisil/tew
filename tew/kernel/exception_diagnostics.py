"""Crash analysis and diagnostic reporting for the emulator."""

from __future__ import annotations
import re
from typing import TYPE_CHECKING

from tew.hardware.cpu import REG_NAMES, ESP, EBP
from tew.logger import logger

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.loader.import_resolver import ImportResolver


def diagnose_fault(cpu: "CPU", import_resolver: "ImportResolver | None") -> None:
    """
    Called after the run loop detects cpu.faulted == True.
    Produces a diagnostic report with memory access info, CPU state, and DLL ranges.
    """
    error = cpu.last_error
    assert error is not None
    logger.error("exception", str(error))

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

    logger.error("exception", "General Purpose Registers:")
    for i in range(8):
        val = cpu.regs[i] & 0xFFFFFFFF
        is_valid = cpu.memory.is_valid_address(val)
        status = "ok" if is_valid else "!!"
        logger.error("exception", f"  [{status}] {REG_NAMES[i]}: 0x{val:08x}")

    esp_val = cpu.regs[ESP] & 0xFFFFFFFF
    ebp_val = cpu.regs[EBP] & 0xFFFFFFFF
    stack_status = "valid" if cpu.memory.is_valid_address(esp_val) else "INVALID"
    logger.error("exception", f"Stack: ESP=0x{esp_val:08x} EBP=0x{ebp_val:08x} ({stack_status})")
    logger.error("exception", "Execution stopped.")
