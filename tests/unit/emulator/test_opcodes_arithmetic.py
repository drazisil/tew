"""Black-box arithmetic opcode tests against ZigCPU."""
import pytest
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import ZigCPU, EAX, ECX, EDX, EBX, ESP, CF_BIT, ZF_BIT, SF_BIT, OF_BIT


@pytest.fixture
def cpu():
    mem = Memory(0x10000)
    c = ZigCPU(mem)
    c.regs[ESP] = 0x8000
    return c


def step(cpu, *bytelist, addr=0x1000):
    cpu.memory.load(addr, bytes(bytelist))
    cpu.eip = addr
    cpu.step()


class TestAdd:
    def test_add_r32_rm32(self, cpu):
        cpu.regs[EAX] = 10
        cpu.regs[ECX] = 5
        step(cpu, 0x03, 0xC1)  # ADD EAX, ECX
        assert cpu.regs[EAX] == 15
        assert cpu.get_flag(ZF_BIT) is False

    def test_add_sets_zero_flag(self, cpu):
        cpu.regs[EAX] = 0xFFFFFFFF
        cpu.regs[ECX] = 1
        step(cpu, 0x03, 0xC1)
        assert cpu.regs[EAX] == 0
        assert cpu.get_flag(ZF_BIT) is True
        assert cpu.get_flag(CF_BIT) is True

    def test_add_eax_imm32(self, cpu):
        cpu.regs[EAX] = 100
        step(cpu, 0x05, 50, 0, 0, 0)  # ADD EAX, 50
        assert cpu.regs[EAX] == 150


class TestSub:
    def test_sub_r32_rm32(self, cpu):
        cpu.regs[EAX] = 100
        cpu.regs[ECX] = 40
        step(cpu, 0x2B, 0xC1)  # SUB EAX, ECX
        assert cpu.regs[EAX] == 60
        assert cpu.get_flag(CF_BIT) is False

    def test_sub_with_borrow(self, cpu):
        cpu.regs[EAX] = 5
        cpu.regs[ECX] = 10
        step(cpu, 0x2B, 0xC1)
        assert cpu.regs[EAX] == 0xFFFFFFFB
        assert cpu.get_flag(CF_BIT) is True
        assert cpu.get_flag(SF_BIT) is True

    def test_sub_imm8(self, cpu):
        cpu.regs[EAX] = 50
        step(cpu, 0x83, 0xE8, 20)  # SUB EAX, 20
        assert cpu.regs[EAX] == 30


class TestCmp:
    def test_equal(self, cpu):
        cpu.regs[EAX] = 42
        cpu.regs[ECX] = 42
        step(cpu, 0x3B, 0xC1)  # CMP EAX, ECX
        assert cpu.get_flag(ZF_BIT) is True
        assert cpu.get_flag(CF_BIT) is False

    def test_less_than(self, cpu):
        cpu.regs[EAX] = 5
        cpu.regs[ECX] = 10
        step(cpu, 0x3B, 0xC1)
        assert cpu.get_flag(ZF_BIT) is False
        assert cpu.get_flag(CF_BIT) is True


class TestIncDec:
    def test_inc_r32(self, cpu):
        cpu.regs[EAX] = 41
        step(cpu, 0x40)  # INC EAX
        assert cpu.regs[EAX] == 42

    def test_inc_does_not_affect_cf(self, cpu):
        cpu.set_flag(CF_BIT, True)
        cpu.regs[EAX] = 0
        step(cpu, 0x40)
        assert cpu.get_flag(CF_BIT) is True

    def test_dec_r32(self, cpu):
        cpu.regs[EBX] = 43
        step(cpu, 0x4B)  # DEC EBX
        assert cpu.regs[EBX] == 42

    def test_dec_does_not_affect_cf(self, cpu):
        cpu.set_flag(CF_BIT, False)
        cpu.regs[EBX] = 1
        step(cpu, 0x4B)
        assert cpu.get_flag(CF_BIT) is False


class TestImul:
    def test_imul_r32_rm32_imm8(self, cpu):
        cpu.regs[EAX] = 6
        step(cpu, 0x6B, 0xC0, 7)  # IMUL EAX, EAX, 7
        assert cpu.regs[EAX] == 42

    def test_imul_negative(self, cpu):
        cpu.regs[EAX] = 6
        step(cpu, 0x6B, 0xC0, 0xFF)  # IMUL EAX, EAX, -1
        assert cpu.regs[EAX] == 0xFFFFFFFA  # -6 as uint32


class TestGroup1:
    def test_add_rm32_imm32(self, cpu):
        cpu.regs[ECX] = 1000
        step(cpu, 0x81, 0xC1, 0xF4, 0x01, 0x00, 0x00)  # ADD ECX, 500
        assert cpu.regs[ECX] == 1500

    def test_and_rm32_imm8(self, cpu):
        cpu.regs[EAX] = 0xFF
        step(cpu, 0x83, 0xE0, 0x0F)  # AND EAX, 0x0F
        assert cpu.regs[EAX] == 0x0F

    def test_xor_rm32_imm8_self(self, cpu):
        cpu.regs[ECX] = 0x12345678
        step(cpu, 0x83, 0xF1, 0x00)  # XOR ECX, 0
        assert cpu.regs[ECX] == 0x12345678


class TestShifts:
    def test_shl_imm8(self, cpu):
        cpu.regs[EAX] = 1
        step(cpu, 0xC1, 0xE0, 4)  # SHL EAX, 4
        assert cpu.regs[EAX] == 16

    def test_shr_imm8(self, cpu):
        cpu.regs[EAX] = 16
        step(cpu, 0xC1, 0xE8, 4)  # SHR EAX, 4
        assert cpu.regs[EAX] == 1

    def test_sar_preserves_sign(self, cpu):
        cpu.regs[EAX] = 0x80000000
        step(cpu, 0xD1, 0xF8)  # SAR EAX, 1
        assert cpu.regs[EAX] == 0xC0000000

    def test_rol(self, cpu):
        cpu.regs[EAX] = 0x80000001
        step(cpu, 0xD1, 0xC0)  # ROL EAX, 1
        assert cpu.regs[EAX] == 0x00000003

    def test_ror(self, cpu):
        cpu.regs[EAX] = 0x00000003
        step(cpu, 0xD1, 0xC8)  # ROR EAX, 1
        assert cpu.regs[EAX] == 0x80000001


class TestCdq:
    def test_positive(self, cpu):
        cpu.regs[EAX] = 0x7FFFFFFF
        step(cpu, 0x99)  # CDQ
        assert cpu.regs[EDX] == 0

    def test_negative(self, cpu):
        cpu.regs[EAX] = 0x80000000
        step(cpu, 0x99)
        assert cpu.regs[EDX] == 0xFFFFFFFF
