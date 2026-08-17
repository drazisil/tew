"""Tests for oleaut32.dll!VariantClear and its Ordinal #9 alias.

Root cause: real callers (msjet35.dll, dao350.dll) import VariantClear by
ordinal, not by name -- Ordinal #9 had its own, separately-written
implementation that only zeroed the 4-byte vt/reserved header of the 16-byte
VARIANT, leaving the 8-byte value union (e.g. a BSTR pointer at +8)
untouched. A "cleared" VARIANT still held stale data for anything reading it
without checking vt first. Fixed by having Ordinal #9 delegate directly to
the named VariantClear handler instead of duplicating its logic, so the two
can't drift apart again.
"""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.oleaut32_handlers import register_oleaut32_ole32_handlers
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
VARIANT  = 0x201000


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_oleaut32_ole32_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # return address
    return mem, stubs, cpu


def _fill_variant_with_garbage(mem: Memory) -> None:
    """vt=8 (VT_BSTR) at +0, reserved bytes at +2..+7, and a fake BSTR
    pointer (0xAABBCCDD) sitting in the value union at +8."""
    mem.write16(VARIANT, 8)
    for i in range(2, 8):
        mem.write8(VARIANT + i, 0xFF)
    mem.write32(VARIANT + 8, 0xAABBCCDD)
    mem.write32(VARIANT + 12, 0xFFFFFFFF)


def _assert_fully_cleared(mem: Memory) -> None:
    for i in range(16):
        assert mem.read8(VARIANT + i) == 0, f"byte at +{i} not cleared"


class TestVariantClear:
    def test_named_handler_clears_all_16_bytes(self, env):
        mem, stubs, cpu = env
        _fill_variant_with_garbage(mem)
        mem.write32(STACK + 4, VARIANT)

        stubs.get("oleaut32.dll", "VariantClear")(cpu)

        assert cpu.regs[EAX] == 0  # S_OK
        _assert_fully_cleared(mem)

    def test_ordinal_9_clears_all_16_bytes(self, env):
        """The actual regression: real msjet35.dll/dao350.dll calls go
        through this ordinal-based entry point, not the named one."""
        mem, stubs, cpu = env
        _fill_variant_with_garbage(mem)
        mem.write32(STACK + 4, VARIANT)

        stubs.get("oleaut32.dll", "Ordinal #9")(cpu)

        assert cpu.regs[EAX] == 0  # S_OK
        _assert_fully_cleared(mem)

    def test_ordinal_9_and_named_handler_are_the_same_function(self, env):
        """Guards against the two implementations drifting apart again."""
        mem, stubs, cpu = env
        assert stubs.get("oleaut32.dll", "Ordinal #9") is stubs.get("oleaut32.dll", "VariantClear")

    def test_null_pointer_does_not_crash(self, env):
        mem, stubs, cpu = env
        mem.write32(STACK + 4, 0)

        stubs.get("oleaut32.dll", "Ordinal #9")(cpu)

        assert cpu.regs[EAX] == 0
        assert cpu.halted is False
