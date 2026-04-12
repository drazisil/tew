"""Stack instructions: PUSH, POP, PUSHAD, POPAD."""

from __future__ import annotations
from typing import TYPE_CHECKING

from tew.hardware.cpu import EAX, ECX, EDX, EBX, ESP, EBP, ESI, EDI
from tew.emulator.opcodes._helpers import u32

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU


def register_stack(cpu: "CPU") -> None:
    # PUSH r32  (0x50 + rd)
    for _r in range(8):
        def _push_r32(cpu: "CPU", r: int = _r) -> None:
            cpu.push32(cpu.regs[r])
        cpu.register(0x50 + _r, _push_r32)

    # POP r32  (0x58 + rd)
    for _r in range(8):
        def _pop_r32(cpu: "CPU", r: int = _r) -> None:
            cpu.regs[r] = cpu.pop32()
        cpu.register(0x58 + _r, _pop_r32)

    # PUSHAD — push all 32-bit registers (ESP pushed as original value)
    def _pushad(cpu: "CPU") -> None:
        orig_esp = cpu.regs[ESP]
        cpu.push32(cpu.regs[EAX])
        cpu.push32(cpu.regs[ECX])
        cpu.push32(cpu.regs[EDX])
        cpu.push32(cpu.regs[EBX])
        cpu.push32(orig_esp)
        cpu.push32(cpu.regs[EBP])
        cpu.push32(cpu.regs[ESI])
        cpu.push32(cpu.regs[EDI])
    cpu.register(0x60, _pushad)

    # POPAD — pop all 32-bit registers (ESP value is discarded)
    def _popad(cpu: "CPU") -> None:
        cpu.regs[EDI] = cpu.pop32()
        cpu.regs[ESI] = cpu.pop32()
        cpu.regs[EBP] = cpu.pop32()
        cpu.pop32()  # discard saved ESP
        cpu.regs[EBX] = cpu.pop32()
        cpu.regs[EDX] = cpu.pop32()
        cpu.regs[ECX] = cpu.pop32()
        cpu.regs[EAX] = cpu.pop32()
    cpu.register(0x61, _popad)

    # PUSH imm32
    cpu.register(0x68, lambda cpu: cpu.push32(cpu.fetch32()))

    # PUSH imm8 (sign-extended to 32 bits)
    cpu.register(0x6A, lambda cpu: cpu.push32(u32(cpu.fetch_signed8())))
