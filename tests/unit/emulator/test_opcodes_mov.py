"""Black-box data-movement opcode tests against ZigCPU."""
import pytest
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import ZigCPU, EAX, ECX, EDX, EBX, ESP, EBP, ESI, EDI


@pytest.fixture
def cpu():
    mem = Memory(0x10000)
    c = ZigCPU(mem)
    c.regs[ESP] = 0x8000
    return c


def load(cpu, addr, data):
    cpu.memory.load(addr, bytes(data))
    cpu.eip = addr


class TestMovR32Imm32:
    def test_eax(self, cpu):
        load(cpu, 0, [0xB8, 0x78, 0x56, 0x34, 0x12])  # MOV EAX, 0x12345678
        cpu.step()
        assert cpu.regs[EAX] == 0x12345678
        assert cpu.eip == 5

    def test_ecx(self, cpu):
        load(cpu, 0, [0xB9, 0xEF, 0xBE, 0xAD, 0xDE])  # MOV ECX, 0xDEADBEEF
        cpu.step()
        assert cpu.regs[ECX] == 0xDEADBEEF

    def test_all_registers(self, cpu):
        values = [0x11111111, 0x22222222, 0x33333333, 0x44444444,
                  0x55555555, 0x66666666, 0x77777777, 0x88888888]
        code = []
        for r, v in enumerate(values):
            code.append(0xB8 + r)
            code += list(v.to_bytes(4, "little"))
        load(cpu, 0, code)
        for _ in range(8):
            cpu.step()
        for r, v in enumerate(values):
            assert cpu.regs[r] == v


class TestMovR16Imm16:
    """Regression tests for the 0x66-prefixed (16-bit) form of MOV r32, imm32
    (opcodes 0xB8-0xBF): the handler used to ignore op_size_ovr entirely and
    always fetch32() a 4-byte immediate, reading 2 bytes too many and writing
    the full 32-bit register instead of just the low 16 bits. That silently
    desynced EIP from the real instruction stream -- found live via a
    MSJET35.DLL access-violation halt whose actual root cause was this
    opcode, not the DLL: the emulator kept "executing" whatever garbage bytes
    the extra 2-byte overread landed on until it hit truly invalid memory
    several pseudo-instructions later. See changelog.md, 2026-08-02."""

    def test_ax_honors_66_prefix(self, cpu):
        cpu.regs[EAX] = 0x12340000
        load(cpu, 0, [0x66, 0xB8, 0x01, 0x00])  # MOV AX, 1
        cpu.step()
        assert cpu.regs[EAX] == 0x12340001  # upper 16 bits preserved
        assert cpu.eip == 4  # exactly 4 bytes consumed, not 6

    def test_cx_honors_66_prefix(self, cpu):
        cpu.regs[ECX] = 0xABCD0000
        load(cpu, 0, [0x66, 0xB9, 0xEF, 0xBE])  # MOV CX, 0xBEEF
        cpu.step()
        assert cpu.regs[ECX] == 0xABCDBEEF
        assert cpu.eip == 4

    def test_does_not_overread_following_bytes(self, cpu):
        # The exact shape of the live bug: two bytes past the real 4-byte
        # instruction belong to the *next* real instruction and must be
        # left completely unconsumed/unexecuted by this one.
        load(cpu, 0, [0x66, 0xB8, 0x01, 0x00, 0x0C, 0x99])  # MOV AX,1 ; OR AL,0x99 (next instr)
        cpu.step()
        assert cpu.eip == 4
        assert cpu.regs[EAX] & 0xFFFF == 0x0001


class TestMovRm32R32:
    def test_reg_to_reg(self, cpu):
        cpu.regs[EAX] = 0x12345678
        load(cpu, 0, [0x89, 0xC1])  # MOV ECX, EAX
        cpu.step()
        assert cpu.regs[ECX] == 0x12345678

    def test_reg_to_mem(self, cpu):
        cpu.regs[EAX] = 0xCAFEBABE
        load(cpu, 0, [0x89, 0x05, 0x00, 0x10, 0x00, 0x00])  # MOV [0x1000], EAX
        cpu.step()
        assert cpu.memory.read32(0x1000) == 0xCAFEBABE


class TestMovR32Rm32:
    def test_reg_to_reg(self, cpu):
        cpu.regs[EBX] = 0x87654321
        load(cpu, 0, [0x8B, 0xC3])  # MOV EAX, EBX
        cpu.step()
        assert cpu.regs[EAX] == 0x87654321

    def test_mem_to_reg(self, cpu):
        cpu.memory.write32(0x2000, 0x11223344)
        load(cpu, 0, [0x8B, 0x05, 0x00, 0x20, 0x00, 0x00])  # MOV EAX, [0x2000]
        cpu.step()
        assert cpu.regs[EAX] == 0x11223344


class TestMovRm32Imm32:
    def test_reg_imm(self, cpu):
        load(cpu, 0, [0xC7, 0xC0, 0xDD, 0xCC, 0xBB, 0xAA])  # MOV EAX, 0xAABBCCDD
        cpu.step()
        assert cpu.regs[EAX] == 0xAABBCCDD

    def test_mem_imm(self, cpu):
        load(cpu, 0, [0xC7, 0x05, 0x00, 0x30, 0x00, 0x00, 0xAD, 0xDE, 0x00, 0x00])
        cpu.step()
        assert cpu.memory.read32(0x3000) == 0xDEAD

    def test_reg_indirect_disp8(self, cpu):
        # C7 42 FC AD DE AD DE  =  MOV DWORD PTR [EDX-4], 0xDEADDEAD
        # This is the exact encoding seen in SNDMEMI_alloc (0xa5453a).
        cpu.regs[EDX] = 0x4000           # [EDX-4] = 0x3FFC
        load(cpu, 0, [0xC7, 0x42, 0xFC, 0xAD, 0xDE, 0xAD, 0xDE])
        cpu.step()
        assert cpu.memory.read32(0x3FFC) == 0xDEADDEAD
        assert cpu.eip == 7

    def test_reg_indirect_disp8_zero_offset(self, cpu):
        # C7 40 00 78 56 34 12  =  MOV DWORD PTR [EAX+0], 0x12345678
        cpu.regs[EAX] = 0x5000
        load(cpu, 0, [0xC7, 0x40, 0x00, 0x78, 0x56, 0x34, 0x12])
        cpu.step()
        assert cpu.memory.read32(0x5000) == 0x12345678


class TestMovALMem:
    def test_mov_al_from_mem(self, cpu):
        cpu.memory.write8(0x4000, 0xAB)
        load(cpu, 0, [0xA0, 0x00, 0x40, 0x00, 0x00])  # MOV AL, [0x4000]
        cpu.step()
        assert cpu.regs[EAX] & 0xFF == 0xAB

    def test_mov_eax_from_mem(self, cpu):
        cpu.memory.write32(0x5000, 0x12345678)
        load(cpu, 0, [0xA1, 0x00, 0x50, 0x00, 0x00])  # MOV EAX, [0x5000]
        cpu.step()
        assert cpu.regs[EAX] == 0x12345678


class TestLea:
    def test_lea_reg_plus_disp8(self, cpu):
        cpu.regs[EBX] = 0x1000
        load(cpu, 0, [0x8D, 0x43, 0x10])  # LEA EAX, [EBX+0x10]
        cpu.step()
        assert cpu.regs[EAX] == 0x1010


class TestXchg:
    def test_xchg_r32_rm32(self, cpu):
        cpu.regs[EAX] = 0x111
        cpu.regs[EBX] = 0x222
        load(cpu, 0, [0x87, 0xC3])  # XCHG EAX, EBX
        cpu.step()
        assert cpu.regs[EAX] == 0x222
        assert cpu.regs[EBX] == 0x111

    def test_xchg_eax_r32_short(self, cpu):
        cpu.regs[EAX] = 0xAAA
        cpu.regs[ECX] = 0xBBB
        load(cpu, 0, [0x91])  # XCHG EAX, ECX
        cpu.step()
        assert cpu.regs[EAX] == 0xBBB
        assert cpu.regs[ECX] == 0xAAA


class TestMovR8Imm8:
    def test_mov_al(self, cpu):
        cpu.regs[EAX] = 0xFFFFFFFF
        load(cpu, 0, [0xB0, 0x42])  # MOV AL, 0x42
        cpu.step()
        assert cpu.regs[EAX] == 0xFFFFFF42

    def test_mov_ah(self, cpu):
        cpu.regs[EAX] = 0xFFFFFFFF
        load(cpu, 0, [0xB4, 0x42])  # MOV AH, 0x42
        cpu.step()
        assert cpu.regs[EAX] == 0xFFFF42FF
