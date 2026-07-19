"""Tests for user32.dll!IsIconic — previously unimplemented (hard halt)."""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.user32_handlers import register_user32_gdi32_handlers
from tew.hardware.memory import Memory
from tew.hardware.cpu import EAX, ESP


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


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_user32_gdi32_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # return address
    return cpu, mem, state, stubs


class TestIsIconic:
    def test_returns_false(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, 0x1034)  # hWnd

        stubs.get("user32.dll", "IsIconic")(cpu)

        assert cpu.regs[EAX] == 0

    def test_does_not_halt(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, 0x1034)

        stubs.get("user32.dll", "IsIconic")(cpu)

        assert cpu.halted is False

    def test_cleans_up_stdcall_args(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, 0x1034)

        stubs.get("user32.dll", "IsIconic")(cpu)

        assert cpu.regs[ESP] == STACK + 4
        assert mem.read32(STACK + 4) == 0xDEAD
