"""Tests for string operation opcodes: MOVS, STOS, LODS, SCAS, CMPS."""

import pytest
from tew.hardware.memory import Memory
from tew.hardware.cpu import CPU, EAX, ECX, EDX, ESI, EDI, ESP, ZF_BIT, CF_BIT
from tew.emulator.opcodes import register_all_opcodes


@pytest.fixture
def cpu():
    m = Memory(0x20000)
    c = CPU(m)
    register_all_opcodes(c)
    c.regs[ESP] = 0x18000
    return c


def step(cpu, *bytelist, addr=0x1000):
    cpu.memory.load(addr, bytes(bytelist))
    cpu.eip = addr
    cpu.step()


class TestStosb:
    def test_single(self, cpu):
        cpu.regs[EAX] = 0xFF
        cpu.regs[EDI] = 0x2000
        step(cpu, 0xAA)  # STOSB
        assert cpu.memory.read8(0x2000) == 0xFF
        assert cpu.regs[EDI] == 0x2001

    def test_rep(self, cpu):
        cpu.regs[EAX] = 0xAB
        cpu.regs[EDI] = 0x3000
        cpu.regs[ECX] = 4
        cpu.memory.load(0x1000, bytes([0xF3, 0xAA]))  # REP STOSB
        cpu.eip = 0x1000
        cpu.step()
        for i in range(4):
            assert cpu.memory.read8(0x3000 + i) == 0xAB
        assert cpu.regs[ECX] == 0
        assert cpu.regs[EDI] == 0x3004


class TestStosd:
    def test_single(self, cpu):
        cpu.regs[EAX] = 0x12345678
        cpu.regs[EDI] = 0x4000
        step(cpu, 0xAB)  # STOSD
        assert cpu.memory.read32(0x4000) == 0x12345678
        assert cpu.regs[EDI] == 0x4004

    def test_rep(self, cpu):
        cpu.regs[EAX] = 0xCAFEBABE
        cpu.regs[EDI] = 0x5000
        cpu.regs[ECX] = 3
        cpu.memory.load(0x1000, bytes([0xF3, 0xAB]))  # REP STOSD
        cpu.eip = 0x1000
        cpu.step()
        for i in range(3):
            assert cpu.memory.read32(0x5000 + i * 4) == 0xCAFEBABE
        assert cpu.regs[ECX] == 0


class TestMovsb:
    def test_single(self, cpu):
        cpu.memory.write8(0x6000, 0x55)
        cpu.regs[ESI] = 0x6000
        cpu.regs[EDI] = 0x7000
        step(cpu, 0xA4)  # MOVSB
        assert cpu.memory.read8(0x7000) == 0x55
        assert cpu.regs[ESI] == 0x6001
        assert cpu.regs[EDI] == 0x7001

    def test_rep(self, cpu):
        for i in range(5):
            cpu.memory.write8(0x8000 + i, i + 1)
        cpu.regs[ESI] = 0x8000
        cpu.regs[EDI] = 0x9000
        cpu.regs[ECX] = 5
        cpu.memory.load(0x1000, bytes([0xF3, 0xA4]))  # REP MOVSB
        cpu.eip = 0x1000
        cpu.step()
        for i in range(5):
            assert cpu.memory.read8(0x9000 + i) == i + 1


class TestScasb:
    def test_repne_finds_null(self, cpu):
        # Write "ABC\0" at 0xA000, scan for '\0'
        cpu.memory.load(0xA000, b"ABC\x00")
        cpu.regs[EAX] = 0x00   # scanning for 0
        cpu.regs[EDI] = 0xA000
        cpu.regs[ECX] = 10
        cpu.memory.load(0x1000, bytes([0xF2, 0xAE]))  # REPNE SCASB
        cpu.eip = 0x1000
        cpu.step()
        # Stops when ZF=1 (found the match)
        assert cpu.get_flag(ZF_BIT) is True
        assert cpu.regs[EDI] == 0xA004   # advanced past the '\0'


class TestCmpsb:
    def test_equal_strings(self, cpu):
        cpu.memory.load(0xB000, b"Hello")
        cpu.memory.load(0xC000, b"Hello")
        cpu.regs[ESI] = 0xB000
        cpu.regs[EDI] = 0xC000
        cpu.regs[ECX] = 5
        cpu.memory.load(0x1000, bytes([0xF3, 0xA6]))  # REPE CMPSB
        cpu.eip = 0x1000
        cpu.step()
        assert cpu.get_flag(ZF_BIT) is True
        assert cpu.regs[ECX] == 0

    def test_unequal_strings(self, cpu):
        cpu.memory.load(0xD000, b"Hello")
        cpu.memory.load(0xE000, b"World")
        cpu.regs[ESI] = 0xD000
        cpu.regs[EDI] = 0xE000
        cpu.regs[ECX] = 5
        cpu.memory.load(0x1000, bytes([0xF3, 0xA6]))  # REPE CMPSB
        cpu.eip = 0x1000
        cpu.step()
        assert cpu.get_flag(ZF_BIT) is False
        assert cpu.regs[ECX] < 5   # stopped before exhausting count
