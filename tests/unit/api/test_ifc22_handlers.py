"""Tests for ifc22.dll (Immersion Force Feedback) handler stubs."""
from __future__ import annotations

import pytest

from tew.api.ifc22_handlers import register_ifc22_handlers
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ECX, ESP


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


MEM_SIZE = 4 * 1024 * 1024
STACK    = 0x200000


@pytest.fixture
def env():
    mem = Memory(MEM_SIZE)
    stubs = _StubHandlers()
    register_ifc22_handlers(stubs, mem)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # fake return address
    return cpu, mem, stubs


CTOR_NAMES = [
    "??0CImmMouse@@QAE@XZ",
    "??0CImmProject@@QAE@XZ",
    "??0CImmPeriodic@@QAE@XZ",
]

DTOR_NAMES = [
    "??1CImmMouse@@UAE@XZ",
    "??1CImmProject@@QAE@XZ",
    "??1CImmPeriodic@@UAE@XZ",
]

FFB_HALT_NAMES = [
    "?UsesWin32MouseServices@CImmDevice@@QAEHH@Z",
    "?OpenFile@CImmProject@@QAEHPBDPAVCImmDevice@@@Z",
    "?Start@CImmProject@@QAEHPBDKKPAVCImmDevice@@@Z",
    "?ChangeParameters@CImmPeriodic@@QAEHKKKJJJKPAUFEELIT_ENVELOPE@@@Z",
]


class TestConstructors:

    @pytest.mark.parametrize("name", CTOR_NAMES)
    def test_ctor_returns_this_pointer(self, env, name):
        cpu, mem, stubs = env
        cpu.regs[ECX] = 0xDEADBEEF
        stubs.get("ifc22.dll", name)(cpu)
        assert cpu.regs[EAX] == 0xDEADBEEF

    @pytest.mark.parametrize("name", CTOR_NAMES)
    def test_ctor_does_not_halt(self, env, name):
        cpu, mem, stubs = env
        cpu.regs[ECX] = 0x1234
        stubs.get("ifc22.dll", name)(cpu)
        assert cpu.halted is False


class TestDestructors:

    @pytest.mark.parametrize("name", DTOR_NAMES)
    def test_dtor_is_noop(self, env, name):
        cpu, mem, stubs = env
        cpu.regs[EAX] = 0x5555
        stubs.get("ifc22.dll", name)(cpu)
        assert cpu.regs[EAX] == 0x5555
        assert cpu.halted is False


class TestInitialize:

    def test_returns_zero(self, env):
        cpu, mem, stubs = env
        stubs.get("ifc22.dll", "?Initialize@CImmMouse@@QAEHPAX0KH@Z")(cpu)
        assert cpu.regs[EAX] == 0

    def test_stdcall_cleanup_16_bytes(self, env):
        cpu, mem, stubs = env
        stubs.get("ifc22.dll", "?Initialize@CImmMouse@@QAEHPAX0KH@Z")(cpu)
        assert cpu.regs[ESP] == STACK + 16
        assert mem.read32(STACK) == 0xDEAD

    def test_does_not_halt(self, env):
        cpu, mem, stubs = env
        stubs.get("ifc22.dll", "?Initialize@CImmMouse@@QAEHPAX0KH@Z")(cpu)
        assert cpu.halted is False


class TestFFBHaltStubs:

    @pytest.mark.parametrize("name", FFB_HALT_NAMES)
    def test_halts(self, env, name):
        cpu, mem, stubs = env
        stubs.get("ifc22.dll", name)(cpu)
        assert cpu.halted is True

    @pytest.mark.parametrize("name", FFB_HALT_NAMES)
    def test_halt_skips_stdcall_cleanup(self, env, name):
        cpu, mem, stubs = env
        stubs.get("ifc22.dll", name)(cpu)
        assert cpu.regs[ESP] == STACK
