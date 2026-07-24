"""Tests for ole32.dll!CoGetClassObject — previously unimplemented (hard halt)."""
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


MEM_SIZE   = 8 * 1024 * 1024
STACK      = 0x200000
RCLSID_BUF = 0x201000
RIID_BUF   = 0x202000
PPV_PTR    = 0x203000

REGDB_E_CLASSNOTREG = 0x80040154


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_oleaut32_ole32_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # return address
    return cpu, mem, state, stubs


class TestCoGetClassObject:
    def test_returns_regdb_e_classnotreg(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, RCLSID_BUF)   # rclsid
        mem.write32(STACK + 8, 1)            # dwClsContext = CLSCTX_INPROC_SERVER
        mem.write32(STACK + 12, 0)           # pServerInfo
        mem.write32(STACK + 16, RIID_BUF)    # riid
        mem.write32(STACK + 20, PPV_PTR)     # ppv

        stubs.get("ole32.dll", "CoGetClassObject")(cpu)

        assert cpu.regs[EAX] == REGDB_E_CLASSNOTREG

    def test_writes_null_to_ppv_on_failure(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, RCLSID_BUF)
        mem.write32(STACK + 8, 1)
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, RIID_BUF)
        mem.write32(STACK + 20, PPV_PTR)
        mem.write32(PPV_PTR, 0xCCCCCCCC)  # pre-fill with poison to prove it gets cleared

        stubs.get("ole32.dll", "CoGetClassObject")(cpu)

        assert mem.read32(PPV_PTR) == 0

    def test_does_not_halt(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, RCLSID_BUF)
        mem.write32(STACK + 8, 1)
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, RIID_BUF)
        mem.write32(STACK + 20, PPV_PTR)

        stubs.get("ole32.dll", "CoGetClassObject")(cpu)

        assert cpu.halted is False

    def test_cleans_up_stdcall_args(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, RCLSID_BUF)
        mem.write32(STACK + 8, 1)
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, RIID_BUF)
        mem.write32(STACK + 20, PPV_PTR)

        stubs.get("ole32.dll", "CoGetClassObject")(cpu)

        # cleanup_stdcall(cpu, memory, 20): ESP moves past the 20 bytes of
        # args, and the return address gets written back at the new ESP.
        assert cpu.regs[ESP] == STACK + 20
        assert mem.read32(STACK + 20) == 0xDEAD
