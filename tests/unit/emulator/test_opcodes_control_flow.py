"""Tests for control-flow opcodes: CALL, RET, JMP, Jcc, LOOP."""

import pytest
from tew.hardware.memory import Memory
from tew.hardware.cpu import CPU, EAX, ECX, ESP, CF_BIT, ZF_BIT, SF_BIT, OF_BIT
from tew.emulator.opcodes import register_all_opcodes
import struct


@pytest.fixture
def cpu():
    m = Memory(0x20000)
    c = CPU(m)
    register_all_opcodes(c)
    c.regs[ESP] = 0x10000
    return c


def load(cpu, addr, data):
    cpu.memory.load(addr, bytes(data))
    cpu.eip = addr


class TestCall:
    def test_call_pushes_return_addr(self, cpu):
        # CALL +5 (E8 05 00 00 00) at address 0x1000
        # rel32 = 5, target = 0x1000 + 5 + 5 = 0x100A
        code = [0xE8] + list((5).to_bytes(4, "little"))
        load(cpu, 0x1000, code)
        cpu.step()
        assert cpu.eip == 0x100A
        # return address = 0x1005 was pushed
        assert cpu.memory.read32(cpu.regs[ESP]) == 0x1005

    def test_ret_pops_return_addr(self, cpu):
        cpu.push32(0x2000)
        load(cpu, 0x1000, [0xC3])  # RET
        cpu.step()
        assert cpu.eip == 0x2000


class TestJmp:
    def test_jmp_rel32(self, cpu):
        # JMP +10 (E9 0A 00 00 00) at 0x1000
        code = [0xE9] + list((10).to_bytes(4, "little"))
        load(cpu, 0x1000, code)
        cpu.step()
        assert cpu.eip == 0x1000 + 5 + 10

    def test_jmp_rel8(self, cpu):
        # JMP -2 (EB FE) — jumps back to itself (self-loop), but we just test EIP
        # Use +5 instead
        load(cpu, 0x1000, [0xEB, 5])
        cpu.step()
        assert cpu.eip == 0x1000 + 2 + 5

    def test_jmp_rel8_backward(self, cpu):
        load(cpu, 0x1000, [0xEB, 0xFE])  # -2 → jump to 0x1000 (tight loop)
        cpu.step()
        # -2 as signed = -2, EIP was at 0x1002 after fetch, so 0x1002 + (-2) = 0x1000
        assert cpu.eip == 0x1000


class TestJcc:
    def test_je_taken(self, cpu):
        cpu.set_flag(ZF_BIT, True)
        load(cpu, 0x1000, [0x74, 10])   # JE +10
        cpu.step()
        assert cpu.eip == 0x1000 + 2 + 10

    def test_je_not_taken(self, cpu):
        cpu.set_flag(ZF_BIT, False)
        load(cpu, 0x1000, [0x74, 10])
        cpu.step()
        assert cpu.eip == 0x1002    # fall through

    def test_jne_taken(self, cpu):
        cpu.set_flag(ZF_BIT, False)
        load(cpu, 0x1000, [0x75, 5])
        cpu.step()
        assert cpu.eip == 0x1007

    def test_jb_on_carry(self, cpu):
        cpu.set_flag(CF_BIT, True)
        load(cpu, 0x1000, [0x72, 3])    # JB +3
        cpu.step()
        assert cpu.eip == 0x1005

    def test_jge_taken_when_sf_eq_of(self, cpu):
        cpu.set_flag(SF_BIT, False); cpu.set_flag(OF_BIT, False)
        load(cpu, 0x1000, [0x7D, 4])    # JGE +4
        cpu.step()
        assert cpu.eip == 0x1006

    def test_jl_taken_when_sf_ne_of(self, cpu):
        cpu.set_flag(SF_BIT, True); cpu.set_flag(OF_BIT, False)
        load(cpu, 0x1000, [0x7C, 2])    # JL +2
        cpu.step()
        assert cpu.eip == 0x1004


class TestJccNearTwoByte:
    def test_jz_near_taken(self, cpu):
        cpu.set_flag(ZF_BIT, True)
        rel = 100
        code = [0x0F, 0x84] + list(rel.to_bytes(4, "little"))
        load(cpu, 0x1000, code)
        cpu.step()
        assert cpu.eip == 0x1000 + 6 + 100


class TestLoop:
    def test_loop_decrements_ecx_and_jumps(self, cpu):
        cpu.regs[ECX] = 3
        load(cpu, 0x1000, [0xE2, 0xFE])  # LOOP -2 → back to 0x1000
        cpu.step()
        assert cpu.regs[ECX] == 2
        assert cpu.eip == 0x1000  # jumped back

    def test_loop_falls_through_when_ecx_zero(self, cpu):
        cpu.regs[ECX] = 1
        load(cpu, 0x1000, [0xE2, 0xFE])
        cpu.step()
        assert cpu.regs[ECX] == 0
        assert cpu.eip == 0x1002  # fell through


class TestCallRm32:
    def test_call_register_indirect(self, cpu):
        # FF D0: CALL EAX
        cpu.regs[EAX] = 0x2000
        load(cpu, 0x1000, [0xFF, 0xD0])
        cpu.step()
        assert cpu.eip == 0x2000
        assert cpu.memory.read32(cpu.regs[ESP]) == 0x1002

    def test_jmp_register_indirect(self, cpu):
        # FF E0: JMP EAX
        cpu.regs[EAX] = 0x3000
        load(cpu, 0x1000, [0xFF, 0xE0])
        cpu.step()
        assert cpu.eip == 0x3000


class TestRetImm16:
    def test_ret_and_pop_bytes(self, cpu):
        # Stack state inside a stdcall callee: args are below the return address.
        # Push args first, then return address, so [ESP] = return addr, [ESP+4..11] = args.
        cpu.regs[ESP] -= 8   # 8 bytes of "stack args"
        cpu.push32(0x5000)   # return address on top
        load(cpu, 0x1000, [0xC2, 8, 0])   # RET 8
        cpu.step()
        assert cpu.eip == 0x5000
