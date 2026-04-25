"""Black-box control-flow opcode tests against ZigCPU."""
import pytest
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import ZigCPU, EAX, ECX, ESP, CF_BIT, ZF_BIT, SF_BIT, OF_BIT


@pytest.fixture
def cpu():
    mem = Memory(0x20000)
    c = ZigCPU(mem)
    c.regs[ESP] = 0x10000
    return c


def load(cpu, addr, data):
    cpu.memory.load(addr, bytes(data))
    cpu.eip = addr


def push32(cpu, val):
    cpu.regs[ESP] = (cpu.regs[ESP] - 4) & 0xFFFFFFFF
    cpu.memory.write32(cpu.regs[ESP], val & 0xFFFFFFFF)


class TestCall:
    def test_call_pushes_return_addr(self, cpu):
        # CALL +5 (E8 05 00 00 00) at 0x1000; target = 0x1000 + 5 + 5 = 0x100A
        load(cpu, 0x1000, [0xE8] + list((5).to_bytes(4, "little")))
        cpu.step()
        assert cpu.eip == 0x100A
        assert cpu.memory.read32(cpu.regs[ESP]) == 0x1005

    def test_ret_pops_return_addr(self, cpu):
        push32(cpu, 0x2000)
        load(cpu, 0x1000, [0xC3])  # RET
        cpu.step()
        assert cpu.eip == 0x2000


class TestJmp:
    def test_jmp_rel32(self, cpu):
        load(cpu, 0x1000, [0xE9] + list((10).to_bytes(4, "little")))
        cpu.step()
        assert cpu.eip == 0x1000 + 5 + 10

    def test_jmp_rel8(self, cpu):
        load(cpu, 0x1000, [0xEB, 5])
        cpu.step()
        assert cpu.eip == 0x1000 + 2 + 5

    def test_jmp_rel8_backward(self, cpu):
        load(cpu, 0x1000, [0xEB, 0xFE])  # -2 → jumps to 0x1000
        cpu.step()
        assert cpu.eip == 0x1000


class TestJcc:
    def test_je_taken(self, cpu):
        cpu.set_flag(ZF_BIT, True)
        load(cpu, 0x1000, [0x74, 10])  # JE +10
        cpu.step()
        assert cpu.eip == 0x1000 + 2 + 10

    def test_je_not_taken(self, cpu):
        cpu.set_flag(ZF_BIT, False)
        load(cpu, 0x1000, [0x74, 10])
        cpu.step()
        assert cpu.eip == 0x1002

    def test_jne_taken(self, cpu):
        cpu.set_flag(ZF_BIT, False)
        load(cpu, 0x1000, [0x75, 5])
        cpu.step()
        assert cpu.eip == 0x1007

    def test_jb_on_carry(self, cpu):
        cpu.set_flag(CF_BIT, True)
        load(cpu, 0x1000, [0x72, 3])  # JB +3
        cpu.step()
        assert cpu.eip == 0x1005

    def test_jge_taken_when_sf_eq_of(self, cpu):
        cpu.set_flag(SF_BIT, False)
        cpu.set_flag(OF_BIT, False)
        load(cpu, 0x1000, [0x7D, 4])  # JGE +4
        cpu.step()
        assert cpu.eip == 0x1006

    def test_jl_taken_when_sf_ne_of(self, cpu):
        cpu.set_flag(SF_BIT, True)
        cpu.set_flag(OF_BIT, False)
        load(cpu, 0x1000, [0x7C, 2])  # JL +2
        cpu.step()
        assert cpu.eip == 0x1004


class TestJccNearTwoByte:
    def test_jz_near_taken(self, cpu):
        cpu.set_flag(ZF_BIT, True)
        rel = 100
        load(cpu, 0x1000, [0x0F, 0x84] + list(rel.to_bytes(4, "little")))
        cpu.step()
        assert cpu.eip == 0x1000 + 6 + 100


class TestLoop:
    def test_loop_decrements_ecx_and_jumps(self, cpu):
        cpu.regs[ECX] = 3
        load(cpu, 0x1000, [0xE2, 0xFE])  # LOOP -2 → back to 0x1000
        cpu.step()
        assert cpu.regs[ECX] == 2
        assert cpu.eip == 0x1000

    def test_loop_falls_through_when_ecx_zero(self, cpu):
        cpu.regs[ECX] = 1
        load(cpu, 0x1000, [0xE2, 0xFE])
        cpu.step()
        assert cpu.regs[ECX] == 0
        assert cpu.eip == 0x1002


class TestCallRm32:
    def test_call_register_indirect(self, cpu):
        cpu.regs[EAX] = 0x2000
        load(cpu, 0x1000, [0xFF, 0xD0])  # CALL EAX
        cpu.step()
        assert cpu.eip == 0x2000
        assert cpu.memory.read32(cpu.regs[ESP]) == 0x1002

    def test_jmp_register_indirect(self, cpu):
        cpu.regs[EAX] = 0x3000
        load(cpu, 0x1000, [0xFF, 0xE0])  # JMP EAX
        cpu.step()
        assert cpu.eip == 0x3000


class TestRetImm16:
    def test_ret_and_pop_bytes(self, cpu):
        # Simulate stdcall callee: 8 bytes of stack args below return address.
        cpu.regs[ESP] -= 8
        push32(cpu, 0x5000)          # return address on top
        load(cpu, 0x1000, [0xC2, 8, 0])  # RET 8
        cpu.step()
        assert cpu.eip == 0x5000
