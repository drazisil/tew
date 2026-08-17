"""Tests for user32.dll!IsCharAlphaNumericA — previously unimplemented (hard halt)."""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.user32_handlers import register_user32_gdi32_handlers
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


class TestIsCharAlphaNumericA:
    @pytest.mark.parametrize("ch", [ord("A"), ord("Z"), ord("a"), ord("z"), ord("0"), ord("9")])
    def test_true_for_alphanumeric(self, env, ch):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, ch)

        stubs.get("user32.dll", "IsCharAlphaNumericA")(cpu)

        assert cpu.regs[EAX] == 1

    @pytest.mark.parametrize("ch", [ord(" "), ord("!"), ord("."), ord("_"), 0])
    def test_false_for_non_alphanumeric(self, env, ch):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, ch)

        stubs.get("user32.dll", "IsCharAlphaNumericA")(cpu)

        assert cpu.regs[EAX] == 0

    def test_only_low_byte_of_stack_slot_matters(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, 0xDEAD0000 | ord("Q"))  # garbage in high bytes

        stubs.get("user32.dll", "IsCharAlphaNumericA")(cpu)

        assert cpu.regs[EAX] == 1

    def test_does_not_halt(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, ord("A"))

        stubs.get("user32.dll", "IsCharAlphaNumericA")(cpu)

        assert cpu.halted is False

    def test_cleans_up_stdcall_args(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, ord("A"))

        stubs.get("user32.dll", "IsCharAlphaNumericA")(cpu)

        assert cpu.regs[ESP] == STACK + 4
        assert mem.read32(STACK + 4) == 0xDEAD
