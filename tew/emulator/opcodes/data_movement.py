"""Data movement instructions: MOV, LEA, XCHG, LES, LDS, segment push/pop."""

from __future__ import annotations
from typing import TYPE_CHECKING

from tew.hardware.cpu import EAX

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU


def register_data_movement(cpu: "CPU") -> None:
    # MOV r/m32, r32  (or r/m16, r16 with 0x66 prefix)
    def _mov_rm_r(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        mod, reg, rm = d["mod"], d["reg"], d["rm"]
        if cpu.operand_size_override:
            cpu.write_rmv(mod, rm, cpu.regs[reg] & 0xFFFF)
        else:
            cpu.write_rm32(mod, rm, cpu.regs[reg])
    cpu.register(0x89, _mov_rm_r)

    # MOV r32, r/m32  (or r16, r/m16 with 0x66 prefix)
    def _mov_r_rm(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        mod, reg, rm = d["mod"], d["reg"], d["rm"]
        if cpu.operand_size_override:
            val = cpu.read_rmv(mod, rm)
            cpu.regs[reg] = (cpu.regs[reg] & 0xFFFF0000) | (val & 0xFFFF)
        else:
            cpu.regs[reg] = cpu.read_rm32(mod, rm)
    cpu.register(0x8B, _mov_r_rm)

    # MOV r32, imm32  (0xB8 + rd)
    for _r in range(8):
        def _mov_r_imm32(cpu: "CPU", r: int = _r) -> None:
            cpu.regs[r] = cpu.fetch32()
        cpu.register(0xB8 + _r, _mov_r_imm32)

    # MOV r/m32, imm32  (or r/m16, imm16 with 0x66 prefix)
    def _mov_rm_imm(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        mod, rm = d["mod"], d["rm"]
        resolved = cpu.resolve_rm(mod, rm)
        if cpu.operand_size_override:
            imm = cpu.fetch16()
            if resolved["is_reg"]:
                cpu.regs[resolved["addr"]] = (cpu.regs[resolved["addr"]] & 0xFFFF0000) | imm
            else:
                cpu.memory.write16(cpu._apply_segment_override(resolved["addr"]), imm)
        else:
            imm = cpu.fetch32()
            if resolved["is_reg"]:
                cpu.regs[resolved["addr"]] = imm & 0xFFFFFFFF
            else:
                cpu.memory.write32(cpu._apply_segment_override(resolved["addr"]), imm & 0xFFFFFFFF)
    cpu.register(0xC7, _mov_rm_imm)

    # MOV AL, [disp32]
    def _mov_al_mem(cpu: "CPU") -> None:
        addr = cpu._apply_segment_override(cpu.fetch32())
        cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFFFF00) | cpu.memory.read8(addr)
    cpu.register(0xA0, _mov_al_mem)

    # MOV EAX, [disp32]
    def _mov_eax_mem(cpu: "CPU") -> None:
        addr = cpu._apply_segment_override(cpu.fetch32())
        cpu.regs[EAX] = cpu.memory.read32(addr)
    cpu.register(0xA1, _mov_eax_mem)

    # MOV [disp32], AL
    def _mov_mem_al(cpu: "CPU") -> None:
        addr = cpu._apply_segment_override(cpu.fetch32())
        cpu.memory.write8(addr, cpu.regs[EAX] & 0xFF)
    cpu.register(0xA2, _mov_mem_al)

    # MOV [disp32], EAX
    def _mov_mem_eax(cpu: "CPU") -> None:
        addr = cpu._apply_segment_override(cpu.fetch32())
        cpu.memory.write32(addr, cpu.regs[EAX])
    cpu.register(0xA3, _mov_mem_eax)

    # LEA r32, [r/m32]
    def _lea(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        resolved = cpu.resolve_rm(d["mod"], d["rm"])
        cpu.regs[d["reg"]] = resolved["addr"] & 0xFFFFFFFF
    cpu.register(0x8D, _lea)

    # MOV r/m8, r8
    def _mov_rm8_r8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        cpu.write_rm8(d["mod"], d["rm"], cpu.read_reg8(d["reg"]))
    cpu.register(0x88, _mov_rm8_r8)

    # MOV r8, r/m8
    def _mov_r8_rm8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        cpu.write_reg8(d["reg"], cpu.read_rm8(d["mod"], d["rm"]))
    cpu.register(0x8A, _mov_r8_rm8)

    # MOV r8, imm8  (0xB0 + rb)
    for _r in range(8):
        def _mov_r8_imm(cpu: "CPU", r: int = _r) -> None:
            imm = cpu.fetch8()
            if r < 4:
                cpu.regs[r] = (cpu.regs[r] & 0xFFFFFF00) | imm
            else:
                cpu.regs[r - 4] = (cpu.regs[r - 4] & 0xFFFF00FF) | (imm << 8)
        cpu.register(0xB0 + _r, _mov_r8_imm)

    # MOV r/m8, imm8  (0xC6 /0)
    def _mov_rm8_imm(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        resolved = cpu.resolve_rm(d["mod"], d["rm"])
        imm = cpu.fetch8()
        if resolved["is_reg"]:
            r = resolved["addr"]
            cpu.regs[r] = (cpu.regs[r] & 0xFFFFFF00) | imm
        else:
            cpu.memory.write8(cpu._apply_segment_override(resolved["addr"]), imm)
    cpu.register(0xC6, _mov_rm8_imm)

    # PUSH CS  (flat model: CS = 0x1b)
    cpu.register(0x0E, lambda cpu: cpu.push32(0x1B))

    # POP DS, POP ES, POP SS  (flat model: discard)
    cpu.register(0x1F, lambda cpu: cpu.pop32())
    cpu.register(0x07, lambda cpu: cpu.pop32())
    cpu.register(0x17, lambda cpu: cpu.pop32())

    # PUSH DS, PUSH ES, PUSH SS  (flat model: DS = ES = SS = 0x23)
    cpu.register(0x1E, lambda cpu: cpu.push32(0x23))
    cpu.register(0x06, lambda cpu: cpu.push32(0x23))
    cpu.register(0x16, lambda cpu: cpu.push32(0x23))

    # LES r32, m16:32  (flat model: load 32-bit offset, ignore segment)
    def _les(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        resolved = cpu.resolve_rm(d["mod"], d["rm"])
        if not resolved["is_reg"]:
            cpu.regs[d["reg"]] = cpu.memory.read32(cpu._apply_segment_override(resolved["addr"]))
    cpu.register(0xC4, _les)

    # LDS r32, m16:32  (flat model: load 32-bit offset, ignore segment)
    def _lds(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        resolved = cpu.resolve_rm(d["mod"], d["rm"])
        if not resolved["is_reg"]:
            cpu.regs[d["reg"]] = cpu.memory.read32(cpu._apply_segment_override(resolved["addr"]))
    cpu.register(0xC5, _lds)
