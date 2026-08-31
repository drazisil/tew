"""Tests for msvcrt.dll!_wcsicmp -- case-insensitive, whole-string wide
compare (cdecl, no length arg, unlike wcsncmp).
"""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.msvcrt_handlers import register_msvcrt_handlers
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP


class _StubHandlers:
    def __init__(self):
        self._h: dict = {}

    def register_handler(self, dll, name, fn):
        self._h[(dll, name)] = fn

    def get(self, dll, name):
        return self._h[(dll, name)]


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False


MEM_SIZE = 8 * 1024 * 1024
STACK    = 0x200000
BUF_A    = 0x300000
BUF_B    = 0x310000


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_msvcrt_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # return address
    return cpu, mem, stubs


def write_wide(mem, addr, s: str) -> None:
    for i, ch in enumerate(s):
        mem.write16(addr + i * 2, ord(ch))
    mem.write16(addr + len(s) * 2, 0)


def wcsicmp_call(cpu, mem, stubs, s1_addr, s2_addr):
    mem.write32(STACK + 4, s1_addr)
    mem.write32(STACK + 8, s2_addr)
    stubs.get("msvcrt.dll", "_wcsicmp")(cpu)
    return cpu.regs[EAX] & 0xFFFFFFFF


class TestWcsicmp:
    def test_equal_ignoring_case(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "Hello")
        write_wide(mem, BUF_B, "hello")
        assert wcsicmp_call(cpu, mem, stubs, BUF_A, BUF_B) == 0

    def test_equal_same_case(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "same")
        write_wide(mem, BUF_B, "same")
        assert wcsicmp_call(cpu, mem, stubs, BUF_A, BUF_B) == 0

    def test_less_than(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "ABC")
        write_wide(mem, BUF_B, "abd")
        result = wcsicmp_call(cpu, mem, stubs, BUF_A, BUF_B)
        assert result == 0xFFFFFFFF  # -1

    def test_greater_than(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "ABD")
        write_wide(mem, BUF_B, "abc")
        assert wcsicmp_call(cpu, mem, stubs, BUF_A, BUF_B) == 1

    def test_different_lengths_shorter_is_less(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "ab")
        write_wide(mem, BUF_B, "abc")
        result = wcsicmp_call(cpu, mem, stubs, BUF_A, BUF_B)
        assert result == 0xFFFFFFFF  # -1
