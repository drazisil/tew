"""Control flow: CALL, RET, JMP, Jcc (rel8 and rel32), LOOP/LOOPE/LOOPNE, JECXZ."""

from __future__ import annotations
from typing import TYPE_CHECKING

from tew.hardware.cpu import ECX, CF_BIT, ZF_BIT, SF_BIT, OF_BIT, PF_BIT
from tew.emulator.opcodes._helpers import u32

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU


def _eval_condition(cpu: "CPU", cond: int) -> bool:
    """Evaluate x86 condition code (0–15)."""
    cf = cpu.get_flag(CF_BIT)
    zf = cpu.get_flag(ZF_BIT)
    sf = cpu.get_flag(SF_BIT)
    of = cpu.get_flag(OF_BIT)
    pf = cpu.get_flag(PF_BIT)
    if cond == 0x0: return of                         # O
    if cond == 0x1: return not of                     # NO
    if cond == 0x2: return cf                         # B/C/NAE
    if cond == 0x3: return not cf                     # AE/NB/NC
    if cond == 0x4: return zf                         # E/Z
    if cond == 0x5: return not zf                     # NE/NZ
    if cond == 0x6: return cf or zf                   # BE/NA
    if cond == 0x7: return not cf and not zf          # A/NBE
    if cond == 0x8: return sf                         # S
    if cond == 0x9: return not sf                     # NS
    if cond == 0xA: return pf                         # P/PE
    if cond == 0xB: return not pf                     # NP/PO
    if cond == 0xC: return sf != of                   # L/NGE
    if cond == 0xD: return sf == of                   # GE/NL
    if cond == 0xE: return zf or (sf != of)           # LE/NG
    if cond == 0xF: return not zf and (sf == of)      # G/NLE
    return False


def register_control_flow(cpu: "CPU") -> None:
    # CALL rel32
    def _call_rel32(cpu: "CPU") -> None:
        rel = cpu.fetch_signed32()
        target = u32(cpu.eip + rel)
        cpu.push32(cpu.eip)
        cpu.eip = target
    cpu.register(0xE8, _call_rel32)

    # RET
    cpu.register(0xC3, lambda cpu: setattr(cpu, "eip", cpu.pop32()))

    # JMP rel32
    def _jmp_rel32(cpu: "CPU") -> None:
        rel = cpu.fetch_signed32()
        cpu.eip = u32(cpu.eip + rel)
    cpu.register(0xE9, _jmp_rel32)

    # JMP rel8
    def _jmp_rel8(cpu: "CPU") -> None:
        rel = cpu.fetch_signed8()
        cpu.eip = u32(cpu.eip + rel)
    cpu.register(0xEB, _jmp_rel8)

    # Jcc rel8 (0x70–0x7F)
    for _cond in range(16):
        def _jcc_rel8(cpu: "CPU", cond: int = _cond) -> None:
            rel = cpu.fetch_signed8()
            if _eval_condition(cpu, cond):
                cpu.eip = u32(cpu.eip + rel)
        cpu.register(0x70 + _cond, _jcc_rel8)

    # LOOP — decrement ECX, jump if ECX != 0
    def _loop(cpu: "CPU") -> None:
        rel = cpu.fetch_signed8()
        cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
        if cpu.regs[ECX] != 0:
            cpu.eip = u32(cpu.eip + rel)
    cpu.register(0xE2, _loop)

    # LOOPE/LOOPZ — decrement ECX, jump if ECX != 0 and ZF=1
    def _loope(cpu: "CPU") -> None:
        rel = cpu.fetch_signed8()
        cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
        if cpu.regs[ECX] != 0 and cpu.get_flag(ZF_BIT):
            cpu.eip = u32(cpu.eip + rel)
    cpu.register(0xE1, _loope)

    # LOOPNE/LOOPNZ — decrement ECX, jump if ECX != 0 and ZF=0
    def _loopne(cpu: "CPU") -> None:
        rel = cpu.fetch_signed8()
        cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
        if cpu.regs[ECX] != 0 and not cpu.get_flag(ZF_BIT):
            cpu.eip = u32(cpu.eip + rel)
    cpu.register(0xE0, _loopne)

    # JECXZ — jump if ECX == 0
    def _jecxz(cpu: "CPU") -> None:
        rel = cpu.fetch_signed8()
        if cpu.regs[ECX] == 0:
            cpu.eip = u32(cpu.eip + rel)
    cpu.register(0xE3, _jecxz)
