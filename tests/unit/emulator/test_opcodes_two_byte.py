"""Tests for two-byte opcodes (0x0F prefix): MOVZX, MOVSX, SETcc, CMOVcc, BSR/BSF."""

import pytest
from tew.hardware.memory import Memory
from tew.hardware.cpu import CPU, EAX, ECX, EDX, EBX, ESP, ZF_BIT, CF_BIT, SF_BIT, OF_BIT
from tew.emulator.opcodes import register_all_opcodes


@pytest.fixture
def cpu():
    m = Memory(0x10000)
    c = CPU(m)
    register_all_opcodes(c)
    c.regs[ESP] = 0x8000
    return c


def step(cpu, *bytelist, addr=0x1000):
    cpu.memory.load(addr, bytes(bytelist))
    cpu.eip = addr
    cpu.step()


class TestMovzx:
    def test_movzx_r32_r8(self, cpu):
        cpu.regs[EAX] = 0xFFFFFFFF
        cpu.regs[ECX] = 0xAB
        # MOVZX EAX, CL  (0F B6 C1)
        step(cpu, 0x0F, 0xB6, 0xC1)
        assert cpu.regs[EAX] == 0xAB

    def test_movzx_r32_r16(self, cpu):
        cpu.regs[EAX] = 0xFFFFFFFF
        cpu.regs[ECX] = 0x1234
        # MOVZX EAX, CX  (0F B7 C1)
        step(cpu, 0x0F, 0xB7, 0xC1)
        assert cpu.regs[EAX] == 0x1234


class TestMovsx:
    def test_movsx_r32_r8_negative(self, cpu):
        cpu.regs[ECX] = 0xFF  # -1 as signed byte
        # MOVSX EAX, CL  (0F BE C1)
        step(cpu, 0x0F, 0xBE, 0xC1)
        assert cpu.regs[EAX] == 0xFFFFFFFF

    def test_movsx_r32_r8_positive(self, cpu):
        cpu.regs[ECX] = 0x7F
        step(cpu, 0x0F, 0xBE, 0xC1)
        assert cpu.regs[EAX] == 0x7F

    def test_movsx_r32_r16_negative(self, cpu):
        cpu.regs[ECX] = 0x8000  # -32768
        # MOVSX EAX, CX  (0F BF C1)
        step(cpu, 0x0F, 0xBF, 0xC1)
        assert cpu.regs[EAX] == 0xFFFF8000


class TestSetcc:
    def test_sete_true(self, cpu):
        cpu.set_flag(ZF_BIT, True)
        # SETE AL  (0F 94 C0)
        step(cpu, 0x0F, 0x94, 0xC0)
        assert (cpu.regs[EAX] & 0xFF) == 1

    def test_sete_false(self, cpu):
        cpu.set_flag(ZF_BIT, False)
        step(cpu, 0x0F, 0x94, 0xC0)
        assert (cpu.regs[EAX] & 0xFF) == 0

    def test_setne(self, cpu):
        cpu.set_flag(ZF_BIT, False)
        # SETNE AL  (0F 95 C0)
        step(cpu, 0x0F, 0x95, 0xC0)
        assert (cpu.regs[EAX] & 0xFF) == 1


class TestCmovcc:
    def test_cmove_taken(self, cpu):
        cpu.set_flag(ZF_BIT, True)
        cpu.regs[ECX] = 0xBEEF
        # CMOVE EAX, ECX  (0F 44 C1)
        step(cpu, 0x0F, 0x44, 0xC1)
        assert cpu.regs[EAX] == 0xBEEF

    def test_cmove_not_taken(self, cpu):
        cpu.set_flag(ZF_BIT, False)
        cpu.regs[EAX] = 0xDEAD
        cpu.regs[ECX] = 0xBEEF
        step(cpu, 0x0F, 0x44, 0xC1)
        assert cpu.regs[EAX] == 0xDEAD  # unchanged


class TestBsr:
    def test_bsr_simple(self, cpu):
        cpu.regs[ECX] = 0b10110000
        # BSR EAX, ECX  (0F BD C1)
        step(cpu, 0x0F, 0xBD, 0xC1)
        assert cpu.regs[EAX] == 7  # highest set bit
        assert cpu.get_flag(ZF_BIT) is False

    def test_bsr_zero_sets_zf(self, cpu):
        cpu.regs[ECX] = 0
        step(cpu, 0x0F, 0xBD, 0xC1)
        assert cpu.get_flag(ZF_BIT) is True


class TestBsf:
    def test_bsf_simple(self, cpu):
        cpu.regs[ECX] = 0b00001100
        # BSF EAX, ECX  (0F BC C1)
        step(cpu, 0x0F, 0xBC, 0xC1)
        assert cpu.regs[EAX] == 2  # lowest set bit
        assert cpu.get_flag(ZF_BIT) is False

    def test_bsf_zero_sets_zf(self, cpu):
        cpu.regs[ECX] = 0
        step(cpu, 0x0F, 0xBC, 0xC1)
        assert cpu.get_flag(ZF_BIT) is True


class TestBswap:
    def test_bswap(self, cpu):
        cpu.regs[EAX] = 0x12345678
        # BSWAP EAX  (0F C8)
        step(cpu, 0x0F, 0xC8)
        assert cpu.regs[EAX] == 0x78563412
