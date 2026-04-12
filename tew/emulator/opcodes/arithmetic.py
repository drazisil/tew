"""Arithmetic instructions: ADD, SUB, ADC, SBB, CMP, INC, DEC, MUL, IMUL, DIV, IDIV,
NEG, NOT, XCHG, CDQ, ENTER, LEAVE, RET imm16, TEST, Group 1/3 handlers."""

from __future__ import annotations
from typing import TYPE_CHECKING

from tew.hardware.cpu import EAX, ECX, EDX, EBX, ESP, EBP, ESI, EDI, CF_BIT, ZF_BIT

from tew.emulator.opcodes._helpers import s32, u32, read_eaxv, write_eaxv

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU


def _do_group1(cpu: "CPU", is_reg: bool, addr: int, op_ext: int, op1: int, op2: int) -> None:
    """Execute Group 1 (0x81/0x83) arithmetic/logic on 16- or 32-bit operands."""
    if op_ext == 0:    # ADD
        result = u32(op1 + op2)
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.update_flags_arith(op1 + op2, op1, op2, False)
    elif op_ext == 1:  # OR
        result = u32(op1 | op2)
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.update_flags_logic(result)
    elif op_ext == 2:  # ADC
        carry = 1 if cpu.get_flag(CF_BIT) else 0
        result = u32(op1 + op2 + carry)
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.update_flags_arith(op1 + op2 + carry, op1, op2 + carry, False)
    elif op_ext == 3:  # SBB
        borrow = 1 if cpu.get_flag(CF_BIT) else 0
        result = u32(op1 - op2 - borrow)
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.update_flags_arith(op1 - op2 - borrow, op1, op2 + borrow, True)
    elif op_ext == 4:  # AND
        result = u32(op1 & op2)
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.update_flags_logic(result)
    elif op_ext == 5:  # SUB
        result = u32(op1 - op2)
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.update_flags_arith(op1 - op2, op1, op2, True)
    elif op_ext == 6:  # XOR
        result = u32(op1 ^ op2)
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.update_flags_logic(result)
    elif op_ext == 7:  # CMP (no write)
        cpu.update_flags_arith(op1 - op2, op1, op2, True)
    else:
        raise RuntimeError(f"Unsupported Group 1 extension: /{op_ext}")


def register_arithmetic(cpu: "CPU") -> None:  # noqa: C901
    # ADD r/m8, r8
    def _add_rm8_r8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm8_resolved(d["mod"], d["rm"])
        op1, is_reg, addr = res["value"], res["is_reg"], res["addr"]
        op2 = cpu.read_reg8(d["reg"])
        result = (op1 + op2) & 0xFF
        cpu.write_rm8_resolved(is_reg, addr, result)
        cpu.update_flags_arith(op1 + op2, op1, op2, False)
    cpu.register(0x00, _add_rm8_r8)

    # ADD r/m32, r32
    def _add_rm32_r32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm32_resolved(d["mod"], d["rm"])
        op1, is_reg, addr = res["value"], res["is_reg"], res["addr"]
        op2 = cpu.regs[d["reg"]]
        result = u32(op1 + op2)
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.update_flags_arith(op1 + op2, op1, op2, False)
    cpu.register(0x01, _add_rm32_r32)

    # ADD r8, r/m8
    def _add_r8_rm8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = cpu.read_reg8(d["reg"])
        op2 = cpu.read_rm8(d["mod"], d["rm"])
        result = (op1 + op2) & 0xFF
        cpu.write_reg8(d["reg"], result)
        cpu.update_flags_arith(op1 + op2, op1, op2, False)
    cpu.register(0x02, _add_r8_rm8)

    # ADD r32, r/m32
    def _add_r32_rm32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = cpu.regs[d["reg"]]
        op2 = cpu.read_rm32(d["mod"], d["rm"])
        result = u32(op1 + op2)
        cpu.regs[d["reg"]] = result
        cpu.update_flags_arith(op1 + op2, op1, op2, False)
    cpu.register(0x03, _add_r32_rm32)

    # ADD AL, imm8
    def _add_al_imm(cpu: "CPU") -> None:
        imm = cpu.fetch8()
        al = cpu.regs[EAX] & 0xFF
        result = (al + imm) & 0xFF
        cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFFFF00) | result
        cpu.update_flags_arith(al + imm, al, imm, False)
    cpu.register(0x04, _add_al_imm)

    # ADC r/m8, r8
    def _adc_rm8_r8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm8_resolved(d["mod"], d["rm"])
        op1, is_reg, addr = res["value"], res["is_reg"], res["addr"]
        op2 = cpu.read_reg8(d["reg"])
        carry = 1 if cpu.get_flag(CF_BIT) else 0
        result = (op1 + op2 + carry) & 0xFF
        cpu.write_rm8_resolved(is_reg, addr, result)
        cpu.update_flags_arith(op1 + op2 + carry, op1, op2 + carry, False)
    cpu.register(0x10, _adc_rm8_r8)

    # ADC r/m32, r32
    def _adc_rm32_r32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm32_resolved(d["mod"], d["rm"])
        op1, is_reg, addr = res["value"], res["is_reg"], res["addr"]
        op2 = cpu.regs[d["reg"]]
        carry = 1 if cpu.get_flag(CF_BIT) else 0
        result = u32(op1 + op2 + carry)
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.update_flags_arith(op1 + op2 + carry, op1, op2 + carry, False)
    cpu.register(0x11, _adc_rm32_r32)

    # ADC r8, r/m8
    def _adc_r8_rm8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = cpu.read_reg8(d["reg"])
        op2 = cpu.read_rm8(d["mod"], d["rm"])
        carry = 1 if cpu.get_flag(CF_BIT) else 0
        result = (op1 + op2 + carry) & 0xFF
        cpu.write_reg8(d["reg"], result)
        cpu.update_flags_arith(op1 + op2 + carry, op1, op2 + carry, False)
    cpu.register(0x12, _adc_r8_rm8)

    # ADC r32, r/m32
    def _adc_r32_rm32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = cpu.regs[d["reg"]]
        op2 = cpu.read_rm32(d["mod"], d["rm"])
        carry = 1 if cpu.get_flag(CF_BIT) else 0
        result = u32(op1 + op2 + carry)
        cpu.regs[d["reg"]] = result
        cpu.update_flags_arith(op1 + op2 + carry, op1, op2 + carry, False)
    cpu.register(0x13, _adc_r32_rm32)

    # SBB r/m8, r8
    def _sbb_rm8_r8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm8_resolved(d["mod"], d["rm"])
        op1, is_reg, addr = res["value"], res["is_reg"], res["addr"]
        op2 = cpu.read_reg8(d["reg"])
        borrow = 1 if cpu.get_flag(CF_BIT) else 0
        result = (op1 - op2 - borrow) & 0xFF
        cpu.write_rm8_resolved(is_reg, addr, result)
        cpu.update_flags_arith(op1 - op2 - borrow, op1, op2 + borrow, True)
    cpu.register(0x18, _sbb_rm8_r8)

    # SBB r/m32, r32
    def _sbb_rm32_r32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm32_resolved(d["mod"], d["rm"])
        op1, is_reg, addr = res["value"], res["is_reg"], res["addr"]
        op2 = cpu.regs[d["reg"]]
        borrow = 1 if cpu.get_flag(CF_BIT) else 0
        result = u32(op1 - op2 - borrow)
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.update_flags_arith(op1 - op2 - borrow, op1, op2 + borrow, True)
    cpu.register(0x19, _sbb_rm32_r32)

    # SBB r8, r/m8
    def _sbb_r8_rm8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = cpu.read_reg8(d["reg"])
        op2 = cpu.read_rm8(d["mod"], d["rm"])
        borrow = 1 if cpu.get_flag(CF_BIT) else 0
        result = (op1 - op2 - borrow) & 0xFF
        cpu.write_reg8(d["reg"], result)
        cpu.update_flags_arith(op1 - op2 - borrow, op1, op2 + borrow, True)
    cpu.register(0x1A, _sbb_r8_rm8)

    # SBB r32, r/m32
    def _sbb_r32_rm32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = cpu.regs[d["reg"]]
        op2 = cpu.read_rm32(d["mod"], d["rm"])
        borrow = 1 if cpu.get_flag(CF_BIT) else 0
        result = u32(op1 - op2 - borrow)
        cpu.regs[d["reg"]] = result
        cpu.update_flags_arith(op1 - op2 - borrow, op1, op2 + borrow, True)
    cpu.register(0x1B, _sbb_r32_rm32)

    # AND r8, r/m8
    def _and_r8_rm8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        result = (cpu.read_reg8(d["reg"]) & cpu.read_rm8(d["mod"], d["rm"])) & 0xFF
        cpu.write_reg8(d["reg"], result)
        cpu.update_flags_logic(result)
    cpu.register(0x22, _and_r8_rm8)

    # SUB r/m8, r8
    def _sub_rm8_r8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm8_resolved(d["mod"], d["rm"])
        op1, is_reg, addr = res["value"], res["is_reg"], res["addr"]
        op2 = cpu.read_reg8(d["reg"])
        result = (op1 - op2) & 0xFF
        cpu.write_rm8_resolved(is_reg, addr, result)
        cpu.update_flags_arith(op1 - op2, op1, op2, True)
    cpu.register(0x28, _sub_rm8_r8)

    # SUB r8, r/m8
    def _sub_r8_rm8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = cpu.read_reg8(d["reg"])
        op2 = cpu.read_rm8(d["mod"], d["rm"])
        result = (op1 - op2) & 0xFF
        cpu.write_reg8(d["reg"], result)
        cpu.update_flags_arith(op1 - op2, op1, op2, True)
    cpu.register(0x2A, _sub_r8_rm8)

    # SUB r/m32, r32
    def _sub_rm32_r32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm32_resolved(d["mod"], d["rm"])
        op1, is_reg, addr = res["value"], res["is_reg"], res["addr"]
        op2 = cpu.regs[d["reg"]]
        result = u32(op1 - op2)
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.update_flags_arith(op1 - op2, op1, op2, True)
    cpu.register(0x29, _sub_rm32_r32)

    # SUB r32, r/m32
    def _sub_r32_rm32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = cpu.regs[d["reg"]]
        op2 = cpu.read_rm32(d["mod"], d["rm"])
        result = u32(op1 - op2)
        cpu.regs[d["reg"]] = result
        cpu.update_flags_arith(op1 - op2, op1, op2, True)
    cpu.register(0x2B, _sub_r32_rm32)

    # CMP r/m32, r32
    def _cmp_rm32_r32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = cpu.read_rm32(d["mod"], d["rm"])
        op2 = cpu.regs[d["reg"]]
        cpu.update_flags_arith(op1 - op2, op1, op2, True)
    cpu.register(0x39, _cmp_rm32_r32)

    # CMP r32, r/m32
    def _cmp_r32_rm32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = cpu.regs[d["reg"]]
        op2 = cpu.read_rm32(d["mod"], d["rm"])
        cpu.update_flags_arith(op1 - op2, op1, op2, True)
    cpu.register(0x3B, _cmp_r32_rm32)

    # CMP r/m8, r8
    def _cmp_rm8_r8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = cpu.read_rm8(d["mod"], d["rm"])
        op2 = cpu.read_reg8(d["reg"])
        cpu.update_flags_arith(op1 - op2, op1, op2, True)
    cpu.register(0x38, _cmp_rm8_r8)

    # CMP r8, r/m8
    def _cmp_r8_rm8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = cpu.read_reg8(d["reg"])
        op2 = cpu.read_rm8(d["mod"], d["rm"])
        cpu.update_flags_arith(op1 - op2, op1, op2, True)
    cpu.register(0x3A, _cmp_r8_rm8)

    # Group 1: 0x81 — op r/m16/32, imm16/32
    def _group1_81(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rmv_resolved(d["mod"], d["rm"])
        _do_group1(cpu, res["is_reg"], res["addr"], d["reg"], res["value"], cpu.fetch_immediate())
    cpu.register(0x81, _group1_81)

    # Group 1: 0x83 — op r/m16/32, imm8 (sign-extended)
    def _group1_83(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rmv_resolved(d["mod"], d["rm"])
        imm = u32(cpu.fetch_signed8())
        _do_group1(cpu, res["is_reg"], res["addr"], d["reg"], res["value"], imm)
    cpu.register(0x83, _group1_83)

    # Group 1 byte: 0x80 — op r/m8, imm8
    def _group1_80(cpu: "CPU") -> None:  # noqa: C901
        d = cpu.decode_mod_rm()
        res = cpu.read_rm8_resolved(d["mod"], d["rm"])
        op1, is_reg, addr = res["value"], res["is_reg"], res["addr"]
        imm = cpu.fetch8()
        op_ext = d["reg"]
        if op_ext == 0:    # ADD
            result = (op1 + imm) & 0xFF
            cpu.write_rm8_resolved(is_reg, addr, result)
            cpu.update_flags_arith(op1 + imm, op1, imm, False)
        elif op_ext == 1:  # OR
            result = (op1 | imm) & 0xFF
            cpu.write_rm8_resolved(is_reg, addr, result)
            cpu.update_flags_logic(result)
        elif op_ext == 2:  # ADC
            carry = 1 if cpu.get_flag(CF_BIT) else 0
            result = (op1 + imm + carry) & 0xFF
            cpu.write_rm8_resolved(is_reg, addr, result)
            cpu.update_flags_arith(op1 + imm + carry, op1, imm + carry, False)
        elif op_ext == 3:  # SBB
            borrow = 1 if cpu.get_flag(CF_BIT) else 0
            result = (op1 - imm - borrow) & 0xFF
            cpu.write_rm8_resolved(is_reg, addr, result)
            cpu.update_flags_arith(op1 - imm - borrow, op1, imm + borrow, True)
        elif op_ext == 4:  # AND
            result = (op1 & imm) & 0xFF
            cpu.write_rm8_resolved(is_reg, addr, result)
            cpu.update_flags_logic(result)
        elif op_ext == 5:  # SUB
            result = (op1 - imm) & 0xFF
            cpu.write_rm8_resolved(is_reg, addr, result)
            cpu.update_flags_arith(op1 - imm, op1, imm, True)
        elif op_ext == 6:  # XOR
            result = (op1 ^ imm) & 0xFF
            cpu.write_rm8_resolved(is_reg, addr, result)
            cpu.update_flags_logic(result)
        elif op_ext == 7:  # CMP (no write)
            cpu.update_flags_arith(op1 - imm, op1, imm, True)
    cpu.register(0x80, _group1_80)

    # INC r32  (0x40 + rd)
    for _r in range(8):
        def _inc_r32(cpu: "CPU", r: int = _r) -> None:
            op1 = cpu.regs[r]
            cpu.regs[r] = u32(op1 + 1)
            saved_cf = cpu.get_flag(CF_BIT)
            cpu.update_flags_arith(op1 + 1, op1, 1, False)
            cpu.set_flag(CF_BIT, saved_cf)
        cpu.register(0x40 + _r, _inc_r32)

    # DEC r32  (0x48 + rd)
    for _r in range(8):
        def _dec_r32(cpu: "CPU", r: int = _r) -> None:
            op1 = cpu.regs[r]
            cpu.regs[r] = u32(op1 - 1)
            saved_cf = cpu.get_flag(CF_BIT)
            cpu.update_flags_arith(op1 - 1, op1, 1, True)
            cpu.set_flag(CF_BIT, saved_cf)
        cpu.register(0x48 + _r, _dec_r32)

    # Group 2: 0xC1 — shift/rotate r/m32, imm8
    def _group2_c1(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm32_resolved(d["mod"], d["rm"])
        count = cpu.fetch8() & 0x1F
        _do_group2(cpu, res["is_reg"], res["addr"], d["reg"], res["value"], count)
    cpu.register(0xC1, _group2_c1)

    # Group 2: 0xD1 — shift/rotate r/m32, 1
    def _group2_d1(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm32_resolved(d["mod"], d["rm"])
        _do_group2(cpu, res["is_reg"], res["addr"], d["reg"], res["value"], 1)
    cpu.register(0xD1, _group2_d1)

    # Group 2: 0xD3 — shift/rotate r/m32, CL
    def _group2_d3(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm32_resolved(d["mod"], d["rm"])
        count = cpu.regs[ECX] & 0x1F
        _do_group2(cpu, res["is_reg"], res["addr"], d["reg"], res["value"], count)
    cpu.register(0xD3, _group2_d3)

    # IMUL r32, r/m32, imm8
    def _imul_r32_rm32_imm8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = s32(cpu.read_rm32(d["mod"], d["rm"]))
        imm = cpu.fetch_signed8()
        result32 = u32(op1 * imm)
        cpu.regs[d["reg"]] = result32
        full = op1 * imm
        cpu.set_flag(CF_BIT, full != s32(result32))
        from tew.hardware.cpu import OF_BIT
        cpu.set_flag(OF_BIT, full != s32(result32))
    cpu.register(0x6B, _imul_r32_rm32_imm8)

    # IMUL r32, r/m32, imm32
    def _imul_r32_rm32_imm32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        op1 = s32(cpu.read_rm32(d["mod"], d["rm"]))
        imm = cpu.fetch_signed32()
        result32 = u32(op1 * imm)
        cpu.regs[d["reg"]] = result32
        full = op1 * imm
        from tew.hardware.cpu import OF_BIT
        cpu.set_flag(CF_BIT, full != s32(result32))
        cpu.set_flag(OF_BIT, full != s32(result32))
    cpu.register(0x69, _imul_r32_rm32_imm32)

    # Group 3: 0xF7 — NOT/NEG/MUL/IMUL/DIV/IDIV r/m16/32
    def _group3_f7(cpu: "CPU") -> None:  # noqa: C901
        d = cpu.decode_mod_rm()
        is16 = cpu.operand_size_override
        op_ext = d["reg"]
        mod, rm = d["mod"], d["rm"]
        from tew.hardware.cpu import OF_BIT
        if op_ext == 0:    # TEST r/m, imm
            if is16:
                op1 = cpu.read_rmv(mod, rm)
                imm = cpu.fetch16()
                cpu.update_flags_logic((op1 & imm) & 0xFFFF)
            else:
                op1 = cpu.read_rm32(mod, rm)
                imm = cpu.fetch32()
                cpu.update_flags_logic(u32(op1 & imm))
        elif op_ext == 2:  # NOT
            if is16:
                cpu.write_rmv(mod, rm, (~cpu.read_rmv(mod, rm)) & 0xFFFF)
            else:
                cpu.write_rm32(mod, rm, (~cpu.read_rm32(mod, rm)) & 0xFFFFFFFF)
        elif op_ext == 3:  # NEG
            if is16:
                val = cpu.read_rmv(mod, rm)
                result = (-val) & 0xFFFF
                cpu.write_rmv(mod, rm, result)
                cpu.set_flag(CF_BIT, val != 0)
                cpu.update_flags_arith(-val, 0, val, False)
            else:
                val = cpu.read_rm32(mod, rm)
                result = u32(0 - val)
                cpu.write_rm32(mod, rm, result)
                cpu.set_flag(CF_BIT, val != 0)
                cpu.update_flags_arith(0 - val, 0, val, True)
        elif op_ext == 4:  # MUL (unsigned)
            if is16:
                op1 = cpu.regs[EAX] & 0xFFFF
                op2 = cpu.read_rmv(mod, rm) & 0xFFFF
                result = op1 * op2
                cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF0000) | (result & 0xFFFF)
                cpu.regs[EDX] = (cpu.regs[EDX] & 0xFFFF0000) | ((result >> 16) & 0xFFFF)
                overflow = (result >> 16) != 0
                cpu.set_flag(CF_BIT, overflow); cpu.set_flag(OF_BIT, overflow)
            else:
                op1 = cpu.regs[EAX] & 0xFFFFFFFF
                op2 = cpu.read_rm32(mod, rm) & 0xFFFFFFFF
                result = op1 * op2
                cpu.regs[EAX] = result & 0xFFFFFFFF
                cpu.regs[EDX] = (result >> 32) & 0xFFFFFFFF
                overflow = cpu.regs[EDX] != 0
                cpu.set_flag(CF_BIT, overflow); cpu.set_flag(OF_BIT, overflow)
        elif op_ext == 5:  # IMUL (signed)
            if is16:
                op1 = s32(cpu.regs[EAX] & 0xFFFF) if not (cpu.regs[EAX] & 0x8000) else (cpu.regs[EAX] & 0xFFFF) - 0x10000
                op2_raw = cpu.read_rmv(mod, rm) & 0xFFFF
                op2 = op2_raw if not (op2_raw & 0x8000) else op2_raw - 0x10000
                result = op1 * op2
                cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF0000) | (result & 0xFFFF)
                cpu.regs[EDX] = (cpu.regs[EDX] & 0xFFFF0000) | ((result >> 16) & 0xFFFF)
                sign_ext = 0xFFFF if (result & 0x8000) else 0
                cpu.set_flag(CF_BIT, ((result >> 16) & 0xFFFF) != sign_ext)
                cpu.set_flag(OF_BIT, ((result >> 16) & 0xFFFF) != sign_ext)
            else:
                op1 = s32(cpu.regs[EAX])
                op2 = s32(cpu.read_rm32(mod, rm))
                result = op1 * op2
                cpu.regs[EAX] = result & 0xFFFFFFFF
                cpu.regs[EDX] = (result >> 32) & 0xFFFFFFFF
                sign_ext = 0xFFFFFFFF if (cpu.regs[EAX] & 0x80000000) else 0
                cpu.set_flag(CF_BIT, cpu.regs[EDX] != sign_ext)
                cpu.set_flag(OF_BIT, cpu.regs[EDX] != sign_ext)
        elif op_ext == 6:  # DIV (unsigned)
            if is16:
                divisor = cpu.read_rmv(mod, rm) & 0xFFFF
                if divisor == 0:
                    raise ZeroDivisionError("DIV by zero (16-bit)")
                dx = cpu.regs[EDX] & 0xFFFF
                ax = cpu.regs[EAX] & 0xFFFF
                dividend = (dx << 16) | ax
                quotient = dividend // divisor
                remainder = dividend % divisor
                cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF0000) | (quotient & 0xFFFF)
                cpu.regs[EDX] = (cpu.regs[EDX] & 0xFFFF0000) | (remainder & 0xFFFF)
            else:
                divisor = cpu.read_rm32(mod, rm) & 0xFFFFFFFF
                if divisor == 0:
                    raise ZeroDivisionError("DIV by zero (32-bit)")
                dividend = ((cpu.regs[EDX] & 0xFFFFFFFF) << 32) | (cpu.regs[EAX] & 0xFFFFFFFF)
                quotient = dividend // divisor
                remainder = dividend % divisor
                if quotient > 0xFFFFFFFF:
                    raise OverflowError("DIV overflow")
                cpu.regs[EAX] = quotient & 0xFFFFFFFF
                cpu.regs[EDX] = remainder & 0xFFFFFFFF
        elif op_ext == 7:  # IDIV (signed)
            if is16:
                raw = cpu.read_rmv(mod, rm) & 0xFFFF
                divisor = raw - 0x10000 if raw & 0x8000 else raw
                if divisor == 0:
                    raise ZeroDivisionError("IDIV by zero (16-bit)")
                dx = cpu.regs[EDX] & 0xFFFF
                ax = cpu.regs[EAX] & 0xFFFF
                raw_dividend = (dx << 16) | ax
                dividend = raw_dividend - 0x100000000 if raw_dividend & 0x80000000 else raw_dividend
                quotient = int(dividend / divisor)
                remainder = dividend - quotient * divisor
                cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF0000) | (quotient & 0xFFFF)
                cpu.regs[EDX] = (cpu.regs[EDX] & 0xFFFF0000) | (remainder & 0xFFFF)
            else:
                divisor = s32(cpu.read_rm32(mod, rm))
                if divisor == 0:
                    raise ZeroDivisionError("IDIV by zero (32-bit)")
                edx_s = s32(cpu.regs[EDX])
                eax_u = cpu.regs[EAX] & 0xFFFFFFFF
                dividend = (edx_s << 32) | eax_u
                quotient = int(dividend / divisor)
                remainder = dividend - quotient * divisor
                cpu.regs[EAX] = quotient & 0xFFFFFFFF
                cpu.regs[EDX] = remainder & 0xFFFFFFFF
        else:
            raise RuntimeError(f"Unsupported Group 3 extension: /{op_ext}")
    cpu.register(0xF7, _group3_f7)

    # Group 3 byte: 0xF6 — NOT/NEG/MUL/IMUL/DIV/IDIV r/m8
    def _group3_f6(cpu: "CPU") -> None:  # noqa: C901
        d = cpu.decode_mod_rm()
        val = cpu.read_rm8(d["mod"], d["rm"])
        op_ext = d["reg"]
        from tew.hardware.cpu import OF_BIT
        if op_ext == 0:   # TEST r/m8, imm8
            imm = cpu.fetch8()
            cpu.update_flags_logic((val & imm) & 0xFF)
        elif op_ext == 2: # NOT r/m8
            cpu.write_rm8(d["mod"], d["rm"], (~val) & 0xFF)
        elif op_ext == 3: # NEG r/m8
            result = (0 - val) & 0xFF
            cpu.write_rm8(d["mod"], d["rm"], result)
            cpu.set_flag(CF_BIT, val != 0)
            cpu.update_flags_arith(0 - val, 0, val, True)
        elif op_ext == 4: # MUL AL, r/m8
            al = cpu.regs[EAX] & 0xFF
            result = al * val
            cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF0000) | (result & 0xFFFF)
            cpu.set_flag(CF_BIT, (result & 0xFF00) != 0)
            cpu.set_flag(OF_BIT, (result & 0xFF00) != 0)
        elif op_ext == 5: # IMUL AL, r/m8 (signed)
            al = s32(cpu.regs[EAX] & 0xFF) if not (cpu.regs[EAX] & 0x80) else (cpu.regs[EAX] & 0xFF) - 0x100
            sval = val if not (val & 0x80) else val - 0x100
            result = al * sval
            cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF0000) | (result & 0xFFFF)
            sign_ext = (result & 0xFF) - 0x100 if (result & 0x80) else result & 0xFF
            cpu.set_flag(CF_BIT, result != sign_ext)
            cpu.set_flag(OF_BIT, result != sign_ext)
        elif op_ext == 6: # DIV AL, r/m8
            if val == 0:
                raise ZeroDivisionError("DIV by zero (byte)")
            ax = cpu.regs[EAX] & 0xFFFF
            quot = (ax // val) & 0xFF
            rem = (ax % val) & 0xFF
            cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF0000) | (rem << 8) | quot
        elif op_ext == 7: # IDIV AL, r/m8 (signed)
            sval8 = val if not (val & 0x80) else val - 0x100
            if sval8 == 0:
                raise ZeroDivisionError("IDIV by zero (signed byte)")
            ax_raw = cpu.regs[EAX] & 0xFFFF
            ax = ax_raw if not (ax_raw & 0x8000) else ax_raw - 0x10000
            quot = int(ax / sval8) & 0xFF
            rem = (ax - quot * sval8) & 0xFF
            cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF0000) | (rem << 8) | quot
        else:
            raise RuntimeError(f"Unsupported Group 3 byte extension: /{op_ext}")
    cpu.register(0xF6, _group3_f6)

    # CDQ — sign-extend EAX into EDX:EAX
    cpu.register(0x99, lambda cpu: cpu.regs.__setitem__(EDX, 0xFFFFFFFF if cpu.regs[EAX] & 0x80000000 else 0))

    # XCHG r8, r/m8
    def _xchg_r8_rm8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        v1 = cpu.read_reg8(d["reg"])
        v2 = cpu.read_rm8(d["mod"], d["rm"])
        cpu.write_reg8(d["reg"], v2)
        cpu.write_rm8(d["mod"], d["rm"], v1)
    cpu.register(0x86, _xchg_r8_rm8)

    # XCHG r32, r/m32
    def _xchg_r32_rm32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        v1 = cpu.regs[d["reg"]]
        v2 = cpu.read_rm32(d["mod"], d["rm"])
        cpu.regs[d["reg"]] = v2
        cpu.write_rm32(d["mod"], d["rm"], v1)
    cpu.register(0x87, _xchg_r32_rm32)

    # XCHG EAX, r32  (0x91-0x97; 0x90=NOP registered in misc)
    for _r in range(1, 8):
        def _xchg_eax_r(cpu: "CPU", r: int = _r) -> None:
            tmp = cpu.regs[EAX]
            cpu.regs[EAX] = cpu.regs[r]
            cpu.regs[r] = tmp
        cpu.register(0x90 + _r, _xchg_eax_r)

    # TEST EAX/AX, imm32/imm16
    def _test_eax_imm(cpu: "CPU") -> None:
        a = read_eaxv(cpu)
        imm = cpu.fetch_immediate()
        cpu.update_flags_logic(u32(a & imm))
    cpu.register(0xA9, _test_eax_imm)

    # TEST AL, imm8
    def _test_al_imm(cpu: "CPU") -> None:
        imm = cpu.fetch8()
        cpu.update_flags_logic((cpu.regs[EAX] & imm) & 0xFF)
    cpu.register(0xA8, _test_al_imm)

    # RET imm16  (return + pop N bytes)
    def _ret_imm16(cpu: "CPU") -> None:
        ret_addr = cpu.pop32()
        imm = cpu.fetch16()
        cpu.regs[ESP] = u32(cpu.regs[ESP] + imm)
        cpu.eip = ret_addr
    cpu.register(0xC2, _ret_imm16)

    # ENTER allocSize, nestingLevel
    def _enter(cpu: "CPU") -> None:
        alloc_size = cpu.fetch16()
        nesting = cpu.fetch8() & 0x1F
        cpu.push32(cpu.regs[EBP])
        frame_temp = cpu.regs[ESP]
        if nesting > 0:
            for _ in range(1, nesting):
                cpu.regs[EBP] = u32(cpu.regs[EBP] - 4)
                cpu.push32(cpu.memory.read32(cpu.regs[EBP]))
            cpu.push32(frame_temp)
        cpu.regs[EBP] = frame_temp
        cpu.regs[ESP] = u32(cpu.regs[ESP] - alloc_size)
    cpu.register(0xC8, _enter)

    # LEAVE
    cpu.register(0xC9, lambda cpu: (
        cpu.regs.__setitem__(ESP, cpu.regs[EBP]),
        cpu.regs.__setitem__(EBP, cpu.pop32()),
    ) and None)


def _do_group2(cpu: "CPU", is_reg: bool, addr: int, op_ext: int, val: int, count: int) -> None:  # noqa: C901
    """Execute Group 2 shift/rotate operations on 32-bit operands."""
    from tew.hardware.cpu import CF_BIT, OF_BIT
    if count == 0:
        cpu.write_rm32_resolved(is_reg, addr, val)
        return

    if op_ext == 0:    # ROL
        result = u32((val << count) | (val >> (32 - count)))
        new_cf = (result & 1) != 0
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.set_flag(CF_BIT, new_cf)
        if count == 1:
            msb = (result & 0x80000000) != 0
            cpu.set_flag(OF_BIT, msb != (((result >> 1) & 0x40000000) != 0))
    elif op_ext == 1:  # ROR
        result = u32((val >> count) | (val << (32 - count)))
        new_cf = (result & 0x80000000) != 0
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.set_flag(CF_BIT, new_cf)
        if count == 1:
            msb = (result & 0x80000000) != 0
            cpu.set_flag(OF_BIT, msb != ((val >> 31) & 1 != 0))
    elif op_ext == 2:  # RCL
        temp = u32(val << count)
        if cpu.get_flag(CF_BIT):
            temp |= (1 << (count - 1))
        new_cf = ((val >> (32 - count)) & 1) != 0
        cpu.write_rmv_resolved(is_reg, addr, temp)
        cpu.set_flag(CF_BIT, new_cf)
    elif op_ext == 3:  # RCR
        temp = val >> count
        if cpu.get_flag(CF_BIT):
            temp |= (1 << (32 - count))
        new_cf = ((val >> (count - 1)) & 1) != 0
        cpu.write_rmv_resolved(is_reg, addr, temp & 0xFFFFFFFF)
        cpu.set_flag(CF_BIT, new_cf)
    elif op_ext == 4:  # SHL/SAL
        result = u32(val << count)
        new_cf = ((val >> (32 - count)) & 1) != 0
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.set_flag(CF_BIT, new_cf)
        cpu.update_flags_logic(result)
    elif op_ext == 5:  # SHR
        result = val >> count
        new_cf = ((val >> (count - 1)) & 1) != 0
        cpu.write_rmv_resolved(is_reg, addr, result)
        cpu.set_flag(CF_BIT, new_cf)
        cpu.update_flags_logic(result)
    elif op_ext == 7:  # SAR
        sign = -1 if (val & 0x80000000) else 0
        result = u32((sign << (32 - count)) | (val >> count))
        new_cf = ((val >> (count - 1)) & 1) != 0
        cpu.write_rm32_resolved(is_reg, addr, result)
        cpu.set_flag(CF_BIT, new_cf)
        cpu.update_flags_logic(result)
    else:
        raise RuntimeError(f"Unsupported Group 2 extension: /{op_ext}")
