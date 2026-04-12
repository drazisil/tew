"""Logic instructions: OR, AND, XOR, TEST and accumulator-immediate variants."""

from __future__ import annotations
from typing import TYPE_CHECKING

from tew.hardware.cpu import EAX, CF_BIT
from tew.emulator.opcodes._helpers import u32, read_eaxv, write_eaxv

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU


def register_logic(cpu: "CPU") -> None:
    # OR r/m8, r8
    def _or_rm8_r8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm8_resolved(d["mod"], d["rm"])
        result = (res["value"] | cpu.read_reg8(d["reg"])) & 0xFF
        cpu.write_rm8_resolved(res["is_reg"], res["addr"], result)
        cpu.update_flags_logic(result)
    cpu.register(0x08, _or_rm8_r8)

    # OR r8, r/m8
    def _or_r8_rm8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        result = (cpu.read_reg8(d["reg"]) | cpu.read_rm8(d["mod"], d["rm"])) & 0xFF
        cpu.write_reg8(d["reg"], result)
        cpu.update_flags_logic(result)
    cpu.register(0x0A, _or_r8_rm8)

    # AND r/m8, r8
    def _and_rm8_r8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm8_resolved(d["mod"], d["rm"])
        result = (res["value"] & cpu.read_reg8(d["reg"])) & 0xFF
        cpu.write_rm8_resolved(res["is_reg"], res["addr"], result)
        cpu.update_flags_logic(result)
    cpu.register(0x20, _and_rm8_r8)

    # AND r/m32, r32
    def _and_rm32_r32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm32_resolved(d["mod"], d["rm"])
        result = u32(res["value"] & cpu.regs[d["reg"]])
        cpu.write_rmv_resolved(res["is_reg"], res["addr"], result)
        cpu.update_flags_logic(result)
    cpu.register(0x21, _and_rm32_r32)

    # AND r32, r/m32
    def _and_r32_rm32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        result = u32(cpu.regs[d["reg"]] & cpu.read_rm32(d["mod"], d["rm"]))
        cpu.regs[d["reg"]] = result
        cpu.update_flags_logic(result)
    cpu.register(0x23, _and_r32_rm32)

    # OR r/m32, r32
    def _or_rm32_r32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm32_resolved(d["mod"], d["rm"])
        result = u32(res["value"] | cpu.regs[d["reg"]])
        cpu.write_rmv_resolved(res["is_reg"], res["addr"], result)
        cpu.update_flags_logic(result)
    cpu.register(0x09, _or_rm32_r32)

    # OR r32, r/m32
    def _or_r32_rm32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        result = u32(cpu.regs[d["reg"]] | cpu.read_rm32(d["mod"], d["rm"]))
        cpu.regs[d["reg"]] = result
        cpu.update_flags_logic(result)
    cpu.register(0x0B, _or_r32_rm32)

    # XOR r/m8, r8
    def _xor_rm8_r8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm8_resolved(d["mod"], d["rm"])
        result = (res["value"] ^ cpu.read_reg8(d["reg"])) & 0xFF
        cpu.write_rm8_resolved(res["is_reg"], res["addr"], result)
        cpu.update_flags_logic(result)
    cpu.register(0x30, _xor_rm8_r8)

    # XOR r/m32, r32
    def _xor_rm32_r32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm32_resolved(d["mod"], d["rm"])
        result = u32(res["value"] ^ cpu.regs[d["reg"]])
        cpu.write_rmv_resolved(res["is_reg"], res["addr"], result)
        cpu.update_flags_logic(result)
    cpu.register(0x31, _xor_rm32_r32)

    # XOR r8, r/m8
    def _xor_r8_rm8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        result = (cpu.read_reg8(d["reg"]) ^ cpu.read_rm8(d["mod"], d["rm"])) & 0xFF
        cpu.write_reg8(d["reg"], result)
        cpu.update_flags_logic(result)
    cpu.register(0x32, _xor_r8_rm8)

    # XOR r32, r/m32
    def _xor_r32_rm32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        result = u32(cpu.regs[d["reg"]] ^ cpu.read_rm32(d["mod"], d["rm"]))
        cpu.regs[d["reg"]] = result
        cpu.update_flags_logic(result)
    cpu.register(0x33, _xor_r32_rm32)

    # TEST r/m8, r8
    def _test_rm8_r8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        result = (cpu.read_rm8(d["mod"], d["rm"]) & cpu.read_reg8(d["reg"])) & 0xFF
        cpu.update_flags_logic(result)
    cpu.register(0x84, _test_rm8_r8)

    # TEST r/m32, r32
    def _test_rm32_r32(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        result = u32(cpu.read_rm32(d["mod"], d["rm"]) & cpu.regs[d["reg"]])
        cpu.update_flags_logic(result)
    cpu.register(0x85, _test_rm32_r32)

    # Accumulator-immediate operations (honor 66h prefix)

    # ADD EAX/AX, imm32/imm16
    def _add_eax_imm(cpu: "CPU") -> None:
        a = read_eaxv(cpu)
        imm = cpu.fetch_immediate()
        result = u32(a + imm)
        cpu.update_flags_arith(a + imm, a, imm, False)
        write_eaxv(cpu, result)
    cpu.register(0x05, _add_eax_imm)

    # OR AL, imm8
    def _or_al_imm(cpu: "CPU") -> None:
        imm = cpu.fetch8()
        al = cpu.regs[EAX] & 0xFF
        result = al | imm
        cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFFFF00) | result
        cpu.update_flags_logic(result)
    cpu.register(0x0C, _or_al_imm)

    # OR EAX/AX, imm32/imm16
    def _or_eax_imm(cpu: "CPU") -> None:
        a = read_eaxv(cpu)
        imm = cpu.fetch_immediate()
        result = u32(a | imm)
        cpu.update_flags_logic(result)
        write_eaxv(cpu, result)
    cpu.register(0x0D, _or_eax_imm)

    # ADC AL, imm8
    def _adc_al_imm(cpu: "CPU") -> None:
        imm = cpu.fetch8()
        al = cpu.regs[EAX] & 0xFF
        carry = 1 if cpu.get_flag(CF_BIT) else 0
        result = (al + imm + carry) & 0xFF
        cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFFFF00) | result
        cpu.update_flags_arith(al + imm + carry, al, imm + carry, False)
    cpu.register(0x14, _adc_al_imm)

    # ADC EAX/AX, imm32/imm16
    def _adc_eax_imm(cpu: "CPU") -> None:
        a = read_eaxv(cpu)
        imm = cpu.fetch_immediate()
        carry = 1 if cpu.get_flag(CF_BIT) else 0
        result = u32(a + imm + carry)
        cpu.update_flags_arith(a + imm + carry, a, imm + carry, False)
        write_eaxv(cpu, result)
    cpu.register(0x15, _adc_eax_imm)

    # SBB AL, imm8
    def _sbb_al_imm(cpu: "CPU") -> None:
        imm = cpu.fetch8()
        al = cpu.regs[EAX] & 0xFF
        borrow = 1 if cpu.get_flag(CF_BIT) else 0
        result = (al - imm - borrow) & 0xFF
        cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFFFF00) | result
        cpu.update_flags_arith(al - imm - borrow, al, imm + borrow, True)
    cpu.register(0x1C, _sbb_al_imm)

    # SBB EAX/AX, imm32/imm16
    def _sbb_eax_imm(cpu: "CPU") -> None:
        a = read_eaxv(cpu)
        imm = cpu.fetch_immediate()
        borrow = 1 if cpu.get_flag(CF_BIT) else 0
        result = u32(a - imm - borrow)
        cpu.update_flags_arith(a - imm - borrow, a, imm + borrow, True)
        write_eaxv(cpu, result)
    cpu.register(0x1D, _sbb_eax_imm)

    # AND AL, imm8
    def _and_al_imm(cpu: "CPU") -> None:
        imm = cpu.fetch8()
        al = cpu.regs[EAX] & 0xFF
        result = al & imm
        cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFFFF00) | result
        cpu.update_flags_logic(result)
    cpu.register(0x24, _and_al_imm)

    # AND EAX/AX, imm32/imm16
    def _and_eax_imm(cpu: "CPU") -> None:
        a = read_eaxv(cpu)
        imm = cpu.fetch_immediate()
        result = u32(a & imm)
        cpu.update_flags_logic(result)
        write_eaxv(cpu, result)
    cpu.register(0x25, _and_eax_imm)

    # SUB AL, imm8
    def _sub_al_imm(cpu: "CPU") -> None:
        imm = cpu.fetch8()
        al = cpu.regs[EAX] & 0xFF
        result = (al - imm) & 0xFF
        cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFFFF00) | result
        cpu.update_flags_arith(al - imm, al, imm, True)
    cpu.register(0x2C, _sub_al_imm)

    # SUB EAX/AX, imm32/imm16
    def _sub_eax_imm(cpu: "CPU") -> None:
        a = read_eaxv(cpu)
        imm = cpu.fetch_immediate()
        result = u32(a - imm)
        cpu.update_flags_arith(a - imm, a, imm, True)
        write_eaxv(cpu, result)
    cpu.register(0x2D, _sub_eax_imm)

    # XOR EAX/AX, imm32/imm16
    def _xor_eax_imm(cpu: "CPU") -> None:
        a = read_eaxv(cpu)
        imm = cpu.fetch_immediate()
        result = u32(a ^ imm)
        cpu.update_flags_logic(result)
        write_eaxv(cpu, result)
    cpu.register(0x35, _xor_eax_imm)

    # CMP AL, imm8
    def _cmp_al_imm(cpu: "CPU") -> None:
        imm = cpu.fetch8()
        al = cpu.regs[EAX] & 0xFF
        cpu.update_flags_arith(al - imm, al, imm, True)
    cpu.register(0x3C, _cmp_al_imm)

    # CMP EAX/AX, imm32/imm16
    def _cmp_eax_imm(cpu: "CPU") -> None:
        a = read_eaxv(cpu)
        imm = cpu.fetch_immediate()
        cpu.update_flags_arith(a - imm, a, imm, True)
    cpu.register(0x3D, _cmp_eax_imm)
