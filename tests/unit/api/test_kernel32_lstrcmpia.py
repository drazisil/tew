"""Tests for kernel32.dll!lstrcmpiA — previously unimplemented (hard halt)."""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.kernel32_io import register_kernel32_io_handlers
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
        self.fatal_halt = False


MEM_SIZE = 8 * 1024 * 1024
STACK    = 0x200000
STR1     = 0x201000
STR2     = 0x202000


def _write_cstr(mem: Memory, addr: int, s: str) -> None:
    for i, ch in enumerate(s.encode("latin-1")):
        mem.write8(addr + i, ch)
    mem.write8(addr + len(s), 0)


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_kernel32_io_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # return address
    return cpu, mem, state, stubs


def _call(env, s1: str, s2: str) -> int:
    cpu, mem, state, stubs = env
    _write_cstr(mem, STR1, s1)
    _write_cstr(mem, STR2, s2)
    mem.write32(STACK + 4, STR1)
    mem.write32(STACK + 8, STR2)
    stubs.get("kernel32.dll", "lstrcmpiA")(cpu)
    result = cpu.regs[EAX]
    return result - 0x100000000 if result >= 0x80000000 else result


class TestLstrcmpiA:
    def test_equal_strings_return_zero(self, env):
        assert _call(env, "hello", "hello") == 0

    def test_case_insensitive_equal_returns_zero(self, env):
        assert _call(env, "Tmp.MDB", "TMP.mdb") == 0

    def test_mixed_case_equal_returns_zero(self, env):
        assert _call(env, "Standard Jet DB", "sTANDARD jET db") == 0

    def test_less_than_returns_negative(self, env):
        assert _call(env, "apple", "banana") < 0

    def test_greater_than_returns_positive(self, env):
        assert _call(env, "banana", "apple") > 0

    def test_prefix_is_less_than_longer_string(self, env):
        assert _call(env, "abc", "abcd") < 0

    def test_does_not_halt(self, env):
        cpu, mem, state, stubs = env
        _call(env, "a", "a")
        assert cpu.halted is False

    def test_cleans_up_stdcall_args(self, env):
        cpu, mem, state, stubs = env
        _call(env, "a", "a")
        assert cpu.regs[ESP] == STACK + 8
        assert mem.read32(STACK + 8) == 0xDEAD
