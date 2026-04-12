"""Tests for tew.hardware.cpu.CPU — flags, stack, and ModR/M basics."""

import pytest
from tew.hardware.memory import Memory
from tew.hardware.cpu import (
    CPU, EAX, ECX, EDX, EBX, ESP, EBP, ESI, EDI,
    CF_BIT, ZF_BIT, SF_BIT, OF_BIT, PF_BIT,
)
from tew.emulator.opcodes import register_all_opcodes


@pytest.fixture
def cpu():
    m = Memory(0x10000)
    c = CPU(m)
    register_all_opcodes(c)
    c.regs[ESP] = 0x8000
    return c


class TestFlags:
    def test_set_get_flag(self, cpu):
        cpu.set_flag(CF_BIT, True)
        assert cpu.get_flag(CF_BIT) is True
        cpu.set_flag(CF_BIT, False)
        assert cpu.get_flag(CF_BIT) is False

    def test_update_flags_arith_zero(self, cpu):
        cpu.update_flags_arith(0, 1, 1, True)
        assert cpu.get_flag(ZF_BIT) is True
        assert cpu.get_flag(SF_BIT) is False

    def test_update_flags_arith_negative(self, cpu):
        cpu.update_flags_arith(-1, 0, 1, True)
        assert cpu.get_flag(SF_BIT) is True
        assert cpu.get_flag(ZF_BIT) is False

    def test_update_flags_logic_clears_cf_of(self, cpu):
        cpu.set_flag(CF_BIT, True)
        cpu.set_flag(OF_BIT, True)
        cpu.update_flags_logic(0x5A)
        assert cpu.get_flag(CF_BIT) is False
        assert cpu.get_flag(OF_BIT) is False


class TestStack:
    def test_push_pop_roundtrip(self, cpu):
        cpu.push32(0xDEADBEEF)
        assert cpu.pop32() == 0xDEADBEEF

    def test_push_decrements_esp(self, cpu):
        orig = cpu.regs[ESP]
        cpu.push32(0)
        assert cpu.regs[ESP] == orig - 4

    def test_pop_increments_esp(self, cpu):
        cpu.push32(0)
        before = cpu.regs[ESP]
        cpu.pop32()
        assert cpu.regs[ESP] == before + 4

    def test_multiple_pushes(self, cpu):
        cpu.push32(1)
        cpu.push32(2)
        cpu.push32(3)
        assert cpu.pop32() == 3
        assert cpu.pop32() == 2
        assert cpu.pop32() == 1


class TestFetch:
    def test_fetch8(self, cpu):
        cpu.memory.write8(0x100, 0xAB)
        cpu.eip = 0x100
        assert cpu.fetch8() == 0xAB
        assert cpu.eip == 0x101

    def test_fetch32(self, cpu):
        cpu.memory.write32(0x200, 0x12345678)
        cpu.eip = 0x200
        assert cpu.fetch32() == 0x12345678
        assert cpu.eip == 0x204

    def test_fetch_signed8_negative(self, cpu):
        cpu.memory.write8(0x300, 0xFF)
        cpu.eip = 0x300
        assert cpu.fetch_signed8() == -1

    def test_fetch_immediate_32bit(self, cpu):
        cpu.memory.write32(0x400, 0xCAFE)
        cpu.eip = 0x400
        assert cpu.fetch_immediate() == 0xCAFE

    def test_fetch_immediate_16bit_with_prefix(self, cpu):
        cpu.memory.write16(0x500, 0x1234)
        cpu.eip = 0x500
        cpu._operand_size_override = True
        assert cpu.fetch_immediate() == 0x1234
        cpu._operand_size_override = False


class TestReg8Helpers:
    def test_read_reg8_low(self, cpu):
        cpu.regs[EAX] = 0x12345678
        assert cpu.read_reg8(0) == 0x78  # AL

    def test_read_reg8_high(self, cpu):
        cpu.regs[EAX] = 0x12345678
        assert cpu.read_reg8(4) == 0x56  # AH

    def test_write_reg8_low(self, cpu):
        cpu.regs[EAX] = 0x12345678
        cpu.write_reg8(0, 0xAB)          # AL
        assert cpu.regs[EAX] == 0x123456AB

    def test_write_reg8_high(self, cpu):
        cpu.regs[EAX] = 0x12345678
        cpu.write_reg8(4, 0xCD)          # AH
        assert cpu.regs[EAX] == 0x1234CD78


class TestModRMDecodeRegReg:
    def test_mov_reg_to_reg(self, cpu):
        # MOV ECX, EAX  (89 C1: mod=11, reg=0/EAX, rm=1/ECX)
        cpu.memory.write8(0, 0x89)
        cpu.memory.write8(1, 0xC1)
        cpu.regs[EAX] = 0x42
        cpu.eip = 0
        cpu.step()
        assert cpu.regs[ECX] == 0x42

    def test_mov_reg_to_mem_disp32(self, cpu):
        # MOV [0x1000], EAX  (89 05 00 10 00 00)
        cpu.memory.write8(0, 0x89)
        cpu.memory.write8(1, 0x05)
        cpu.memory.write32(2, 0x1000)
        cpu.regs[EAX] = 0xCAFEBABE
        cpu.eip = 0
        cpu.step()
        assert cpu.memory.read32(0x1000) == 0xCAFEBABE
