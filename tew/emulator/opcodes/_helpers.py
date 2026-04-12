"""Shared arithmetic helpers used across opcode groups."""

from __future__ import annotations


def u32(val: int) -> int:
    """Truncate to unsigned 32-bit integer."""
    return val & 0xFFFFFFFF


def s32(val: int) -> int:
    """Reinterpret as signed 32-bit integer."""
    val = val & 0xFFFFFFFF
    return val - 0x100000000 if val >= 0x80000000 else val


def clz32(val: int) -> int:
    """Count leading zeros in a 32-bit unsigned value."""
    if val == 0:
        return 32
    return 32 - (val & 0xFFFFFFFF).bit_length()


def read_eaxv(cpu) -> int:  # type: ignore[no-untyped-def]
    """Read EAX (32-bit) or AX (16-bit) based on operand-size prefix."""
    from tew.hardware.cpu import EAX
    return cpu.regs[EAX] & 0xFFFF if cpu.operand_size_override else cpu.regs[EAX]


def write_eaxv(cpu, val: int) -> None:  # type: ignore[no-untyped-def]
    """Write to EAX (32-bit) or AX (preserving high 16 bits) based on operand-size prefix."""
    from tew.hardware.cpu import EAX
    if cpu.operand_size_override:
        cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF0000) | (val & 0xFFFF)
    else:
        cpu.regs[EAX] = val & 0xFFFFFFFF
