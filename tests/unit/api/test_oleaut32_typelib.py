"""Tests for oleaut32.dll's type-library probe chain: LoadTypeLibEx,
UnRegisterTypeLib, CreateTypeLib2.

Root cause (live-confirmed): expsrv.dll's own init resolves DispCallFunc,
LoadTypeLibEx, UnRegisterTypeLib, and CreateTypeLib2 one at a time via
GetProcAddress-by-name, caching each into a global function-pointer slot,
and bails the whole chain if any single lookup returns NULL. Real Windows
guarantees all four resolve (they've existed since Win95/NT4), so real
callers never NULL-check the cached pointers before calling through them.

LoadTypeLibEx already had a real Ordinal #154 handler, but GetProcAddress
does a strict string lookup and it was never also registered under its
real name -- same bug class as VariantClear's Ordinal #9 (see
test_oleaut32_variant_clear.py), just in the opposite direction (ordinal
existed, name didn't, instead of the reverse). UnRegisterTypeLib and
CreateTypeLib2 didn't exist under any key at all. The actual crash this
chain produces: expsrv.dll caches NULL for LoadTypeLibEx, then a real
lazy-load-ITypeLib helper (MSJET35.DLL -> expsrv.dll call chain) calls
through that cached NULL/near-NULL pointer with no NULL check, jumping to
invalid memory.
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

    def lookup_handler_address(self, dll, name):
        fn = self._h.get((dll, name))
        return id(fn) if fn is not None else None


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False
        self.fatal_halt = False


MEM_SIZE = 8 * 1024 * 1024
STACK    = 0x200000

E_NOTIMPL = 0x80004001


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_oleaut32_ole32_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    return cpu, mem, stubs


def call(cpu, mem, stubs, dll, name, arg_bytes):
    for i, val in enumerate(arg_bytes):
        mem.write32(STACK + 4 + i * 4, val)
    stubs.get(dll, name)(cpu)


class TestLoadTypeLibExResolvesByName:
    def test_name_and_ordinal_are_the_same_handler(self, env):
        cpu, mem, stubs = env
        assert stubs.get("oleaut32.dll", "LoadTypeLibEx") is stubs.get("oleaut32.dll", "Ordinal #154")

    def test_callable_by_name_returns_e_notimpl(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "oleaut32.dll", "LoadTypeLibEx", [0, 0, 0])
        assert cpu.regs[EAX] == E_NOTIMPL

    def test_stdcall_cleanup_3_args(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "oleaut32.dll", "LoadTypeLibEx", [0, 0, 0])
        assert cpu.regs[ESP] == STACK + 12


class TestUnRegisterTypeLib:
    def test_registered_by_name(self, env):
        cpu, mem, stubs = env
        assert stubs.get("oleaut32.dll", "UnRegisterTypeLib") is not None

    def test_returns_e_notimpl(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "oleaut32.dll", "UnRegisterTypeLib", [0, 1, 0, 0, 1])
        assert cpu.regs[EAX] == E_NOTIMPL

    def test_stdcall_cleanup_5_args(self, env):
        # HRESULT UnRegisterTypeLib(REFGUID, WORD major, WORD minor, LCID, SYSKIND)
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "oleaut32.dll", "UnRegisterTypeLib", [0, 1, 0, 0, 1])
        assert cpu.regs[ESP] == STACK + 20


class TestCreateTypeLib2:
    def test_registered_by_name(self, env):
        cpu, mem, stubs = env
        assert stubs.get("oleaut32.dll", "CreateTypeLib2") is not None

    def test_returns_e_notimpl(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "oleaut32.dll", "CreateTypeLib2", [1, 0, 0])
        assert cpu.regs[EAX] == E_NOTIMPL

    def test_stdcall_cleanup_3_args(self, env):
        # HRESULT CreateTypeLib2(SYSKIND, LPCOLESTR szFile, ICreateTypeLib2**)
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "oleaut32.dll", "CreateTypeLib2", [1, 0, 0])
        assert cpu.regs[ESP] == STACK + 12
