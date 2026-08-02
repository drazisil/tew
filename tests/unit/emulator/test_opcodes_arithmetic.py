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


class TestByteWidthAccumulatorOps:
    """8-bit (AL) forms of the accumulator-immediate opcodes. These are
    separate opcode numbers from the EAX/AX forms (never gated by the 0x66
    prefix), and unlike their 16/32-bit siblings (see
    TestAccumImmediate16BitFlags below) they already compute flags at the
    correct .w8 width -- these tests are coverage, not regression, for that."""

    def test_add_al_imm8_sets_sign_flag_at_bit7(self, cpu):
        cpu.regs[EAX] = 0x12345600 | 0x7F  # AL=0x7F, upper bytes non-zero
        step(cpu, 0x04, 0x01)  # ADD AL, 1 -> AL=0x80
        assert cpu.regs[EAX] == 0x12345680  # upper 24 bits untouched
        assert cpu.get_flag(SF_BIT) is True  # bit 7 set, not bit 31

    def test_or_al_imm8_preserves_upper_bits(self, cpu):
        cpu.regs[EAX] = 0xAABBCC00
        step(cpu, 0x0C, 0x04)  # OR AL, 0x04
        assert cpu.regs[EAX] == 0xAABBCC04

    def test_and_al_imm8(self, cpu):
        cpu.regs[EAX] = 0x000000FF
        step(cpu, 0x24, 0x0F)  # AND AL, 0x0F
        assert cpu.regs[EAX] == 0x0000000F

    def test_sbb_al_imm8(self, cpu):
        cpu.regs[EAX] = 0x00000005
        cpu.set_flag(CF_BIT, True)
        step(cpu, 0x1C, 0x01)  # SBB AL, 1 (with borrow)
        assert cpu.regs[EAX] == 0x00000003

    def test_sub_al_imm8_sets_sign_flag_at_bit7(self, cpu):
        cpu.regs[EAX] = 0x00000000
        step(cpu, 0x2C, 0x01)  # SUB AL, 1 -> AL=0xFF
        assert cpu.regs[EAX] == 0x000000FF
        assert cpu.get_flag(SF_BIT) is True  # bit 7 set, not bit 31
        assert cpu.get_flag(CF_BIT) is True

    def test_cmp_al_imm8_equal(self, cpu):
        cpu.regs[EAX] = 0x00000042
        step(cpu, 0x3C, 0x42)  # CMP AL, 0x42
        assert cpu.get_flag(ZF_BIT) is True
        assert cpu.regs[EAX] == 0x00000042  # CMP does not write back

    def test_test_al_imm8(self, cpu):
        cpu.regs[EAX] = 0x000000F0
        step(cpu, 0xA8, 0x0F)  # TEST AL, 0x0F -> AND result is 0
        assert cpu.get_flag(ZF_BIT) is True
        assert cpu.regs[EAX] == 0x000000F0  # TEST does not write back


class TestAccumImmediate16BitFlags:
    """Regression tests for the 0x66-prefixed (16-bit) accumulator-immediate
    opcodes: op05/15/1D/2D/3D/0D/25/35/A9 read/write AX correctly via
    readEaxv/writeEaxv/fetchImm, but used to pass the fixed .w32 width to the
    flags helper regardless of the 0x66 prefix -- so SF (checked at bit 31)
    was always False for a result whose real 16-bit sign bit (bit 15) was
    set. Each case below produces AX=0x8000 (bit 15 set, bit 31 clear),
    which only reads SF=True once flags are computed at the correct width.
    Same bug class as the old TypeScript emulator's 0x66-prefix fix and the
    already-fixed op85 (TEST rmv, rv)."""

    def test_add_ax_imm16_sign_flag(self, cpu):
        cpu.regs[EAX] = 0x00007FFF
        step(cpu, 0x66, 0x05, 0x01, 0x00)  # ADD AX, 1 -> AX=0x8000
        assert cpu.regs[EAX] == 0x00008000
        assert cpu.get_flag(SF_BIT) is True

    def test_adc_ax_imm16_sign_flag(self, cpu):
        cpu.regs[EAX] = 0x00007FFF
        cpu.set_flag(CF_BIT, False)
        step(cpu, 0x66, 0x15, 0x01, 0x00)  # ADC AX, 1 -> AX=0x8000
        assert cpu.regs[EAX] == 0x00008000
        assert cpu.get_flag(SF_BIT) is True

    def test_sbb_ax_imm16_sign_flag(self, cpu):
        cpu.regs[EAX] = 0x00008001
        cpu.set_flag(CF_BIT, False)
        step(cpu, 0x66, 0x1D, 0x01, 0x00)  # SBB AX, 1 -> AX=0x8000
        assert cpu.regs[EAX] == 0x00008000
        assert cpu.get_flag(SF_BIT) is True

    def test_sub_ax_imm16_sign_flag(self, cpu):
        cpu.regs[EAX] = 0x00008001
        step(cpu, 0x66, 0x2D, 0x01, 0x00)  # SUB AX, 1 -> AX=0x8000
        assert cpu.regs[EAX] == 0x00008000
        assert cpu.get_flag(SF_BIT) is True

    def test_cmp_ax_imm16_sign_flag(self, cpu):
        cpu.regs[EAX] = 0x00008000
        step(cpu, 0x66, 0x3D, 0x00, 0x00)  # CMP AX, 0 -> result 0x8000
        assert cpu.regs[EAX] == 0x00008000  # CMP does not write back
        assert cpu.get_flag(SF_BIT) is True

    def test_or_ax_imm16_sign_flag(self, cpu):
        cpu.regs[EAX] = 0x12340000
        step(cpu, 0x66, 0x0D, 0x00, 0x80)  # OR AX, 0x8000
        assert cpu.regs[EAX] == 0x12348000  # upper 16 bits untouched
        assert cpu.get_flag(SF_BIT) is True

    def test_and_ax_imm16_sign_flag(self, cpu):
        cpu.regs[EAX] = 0x0000FFFF
        step(cpu, 0x66, 0x25, 0x00, 0x80)  # AND AX, 0x8000
        assert cpu.regs[EAX] == 0x00008000
        assert cpu.get_flag(SF_BIT) is True

    def test_xor_ax_imm16_sign_flag(self, cpu):
        cpu.regs[EAX] = 0x00000000
        step(cpu, 0x66, 0x35, 0x00, 0x80)  # XOR AX, 0x8000
        assert cpu.regs[EAX] == 0x00008000
        assert cpu.get_flag(SF_BIT) is True

    def test_test_ax_imm16_sign_flag(self, cpu):
        cpu.regs[EAX] = 0x00008000
        step(cpu, 0x66, 0xA9, 0xFF, 0xFF)  # TEST AX, 0xFFFF -> result 0x8000
        assert cpu.regs[EAX] == 0x00008000  # TEST does not write back
        assert cpu.get_flag(SF_BIT) is True
