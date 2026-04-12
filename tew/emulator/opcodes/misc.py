"""Miscellaneous instructions: NOP, HLT, CLD, STD, INT, SAHF, PUSHFD, POPFD,
CWDE/CBW, WAIT."""

from __future__ import annotations
from typing import TYPE_CHECKING

from tew.hardware.cpu import EAX, DF_BIT
from tew.emulator.opcodes._helpers import u32

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU


def register_misc(cpu: "CPU") -> None:
    # NOP
    cpu.register(0x90, lambda cpu: None)

    # HLT
    cpu.register(0xF4, lambda cpu: setattr(cpu, "halted", True))

    # CLD — clear direction flag (forward string ops)
    cpu.register(0xFC, lambda cpu: cpu.set_flag(DF_BIT, False))

    # STD — set direction flag (backward string ops)
    cpu.register(0xFD, lambda cpu: cpu.set_flag(DF_BIT, True))

    # WAIT / FWAIT — wait for FPU (no-op in emulator)
    cpu.register(0x9B, lambda cpu: None)

    # INT imm8
    def _int_imm8(cpu: "CPU") -> None:
        cpu.trigger_interrupt(cpu.fetch8())
    cpu.register(0xCD, _int_imm8)

    # INT3 — software breakpoint
    cpu.register(0xCC, lambda cpu: cpu.trigger_interrupt(3))

    # SAHF — store AH into low byte of EFLAGS (SF, ZF, AF, PF, CF)
    def _sahf(cpu: "CPU") -> None:
        ah = (cpu.regs[EAX] >> 8) & 0xFF
        cpu.eflags = (cpu.eflags & ~0xD5) | (ah & 0xD5)
    cpu.register(0x9E, _sahf)

    # LAHF — load low byte of EFLAGS into AH
    def _lahf(cpu: "CPU") -> None:
        ah = cpu.eflags & 0xD5
        cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF00FF) | (ah << 8)
    cpu.register(0x9F, _lahf)

    # PUSHFD — push EFLAGS onto stack
    def _pushfd(cpu: "CPU") -> None:
        cpu.push32(cpu.eflags & 0xFCFFFF)   # mask reserved bits
    cpu.register(0x9C, _pushfd)

    # POPFD — pop EFLAGS from stack
    def _popfd(cpu: "CPU") -> None:
        cpu.eflags = cpu.pop32() & 0xFCFFFF
    cpu.register(0x9D, _popfd)

    # CWDE — sign-extend AX into EAX  (or CBW with 0x66: sign-extend AL into AX)
    def _cwde(cpu: "CPU") -> None:
        if cpu.operand_size_override:  # CBW: sign-extend AL → AX
            al = cpu.regs[EAX] & 0xFF
            ax = u32((al ^ 0x80) - 0x80) & 0xFFFF
            cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF0000) | ax
        else:                           # CWDE: sign-extend AX → EAX
            ax = cpu.regs[EAX] & 0xFFFF
            cpu.regs[EAX] = u32((ax ^ 0x8000) - 0x8000)
    cpu.register(0x98, _cwde)

    # STC — set carry flag
    cpu.register(0xF9, lambda cpu: cpu.set_flag(0, True))    # CF_BIT = 0

    # CLC — clear carry flag
    cpu.register(0xF8, lambda cpu: cpu.set_flag(0, False))

    # CMC — complement carry flag
    def _cmc(cpu: "CPU") -> None:
        cpu.set_flag(0, not cpu.get_flag(0))
    cpu.register(0xF5, _cmc)
