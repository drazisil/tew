"""Tests for oleaut32.dll!VariantChangeType (also Ordinal 12) -- previously
unimplemented (hard halt). Confirmed live via DAO350.DLL's FUN_04488948
(a Field value-setter): converts VT_I4(0) -> VT_I2. Covers the well-defined
VT_I2<->VT_I4<->VT_BOOL numeric/bool subset implemented; anything else halts
loudly rather than guess.
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
SRC      = 0x201000
DEST     = 0x202000

S_OK = 0
DISP_E_OVERFLOW = 0x8002000A

VT_I2   = 2
VT_I4   = 3
VT_BOOL = 11


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_oleaut32_ole32_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # return address
    return cpu, mem, stubs


def write_i2(mem, addr, val):
    mem.write16(addr, VT_I2)
    mem.write16(addr + 8, val & 0xFFFF)


def write_i4(mem, addr, val):
    mem.write16(addr, VT_I4)
    mem.write32(addr + 8, val & 0xFFFFFFFF)


def write_bool(mem, addr, val):
    mem.write16(addr, VT_BOOL)
    mem.write16(addr + 8, 0xFFFF if val else 0x0000)


def read_i2(mem, addr):
    v = mem.read16(addr + 8)
    return v - 0x10000 if v >= 0x8000 else v


def call(cpu, mem, stubs, pvar_src, target_vt, dll="oleaut32.dll", name="VariantChangeType"):
    mem.write32(STACK + 4, DEST)
    mem.write32(STACK + 8, pvar_src)
    mem.write32(STACK + 12, 0)  # wFlags
    mem.write32(STACK + 16, target_vt)
    stubs.get(dll, name)(cpu)


class TestI4ToI2:
    def test_zero_confirmed_live_case(self, env):
        """The exact call captured live: VT_I4(0) -> VT_I2."""
        cpu, mem, stubs = env
        write_i4(mem, SRC, 0)
        call(cpu, mem, stubs, SRC, VT_I2)
        assert cpu.regs[EAX] == S_OK
        assert mem.read16(DEST) == VT_I2
        assert read_i2(mem, DEST) == 0

    def test_boundary_fits(self, env):
        cpu, mem, stubs = env
        write_i4(mem, SRC, 32767)
        call(cpu, mem, stubs, SRC, VT_I2)
        assert cpu.regs[EAX] == S_OK
        assert read_i2(mem, DEST) == 32767

    def test_negative_boundary_fits(self, env):
        cpu, mem, stubs = env
        write_i4(mem, SRC, -32768)
        call(cpu, mem, stubs, SRC, VT_I2)
        assert cpu.regs[EAX] == S_OK
        assert read_i2(mem, DEST) == -32768

    def test_overflow_returns_disp_e_overflow(self, env):
        cpu, mem, stubs = env
        write_i4(mem, SRC, 32768)
        call(cpu, mem, stubs, SRC, VT_I2)
        assert cpu.regs[EAX] == DISP_E_OVERFLOW

    def test_negative_overflow_returns_disp_e_overflow(self, env):
        cpu, mem, stubs = env
        write_i4(mem, SRC, -32769)
        call(cpu, mem, stubs, SRC, VT_I2)
        assert cpu.regs[EAX] == DISP_E_OVERFLOW


class TestI2ToI4:
    def test_widening_always_succeeds(self, env):
        cpu, mem, stubs = env
        write_i2(mem, SRC, -1234)
        call(cpu, mem, stubs, SRC, VT_I4)
        assert cpu.regs[EAX] == S_OK
        assert mem.read16(DEST) == VT_I4
        assert mem.read_signed32(DEST + 8) == -1234


class TestBool:
    def test_bool_true_to_i4_is_negative_one(self, env):
        """VARIANT_BOOL True is canonically -1, not 1 -- real
        VariantChangeType preserves that raw value on conversion to a
        numeric type, it doesn't remap to 1."""
        cpu, mem, stubs = env
        write_bool(mem, SRC, True)
        call(cpu, mem, stubs, SRC, VT_I4)
        assert cpu.regs[EAX] == S_OK
        assert mem.read_signed32(DEST + 8) == -1

    def test_bool_false_to_i4_is_zero(self, env):
        cpu, mem, stubs = env
        write_bool(mem, SRC, False)
        call(cpu, mem, stubs, SRC, VT_I4)
        assert cpu.regs[EAX] == S_OK
        assert mem.read_signed32(DEST + 8) == 0

    def test_nonzero_i4_to_bool_is_true(self, env):
        cpu, mem, stubs = env
        write_i4(mem, SRC, 42)
        call(cpu, mem, stubs, SRC, VT_BOOL)
        assert cpu.regs[EAX] == S_OK
        assert mem.read16(DEST) == VT_BOOL
        assert mem.read16(DEST + 8) == 0xFFFF

    def test_zero_i4_to_bool_is_false(self, env):
        cpu, mem, stubs = env
        write_i4(mem, SRC, 0)
        call(cpu, mem, stubs, SRC, VT_BOOL)
        assert cpu.regs[EAX] == S_OK
        assert mem.read16(DEST + 8) == 0x0000


class TestOrdinal12MatchesNamedExport:
    def test_ordinal_12_is_the_same_real_function(self, env):
        cpu, mem, stubs = env
        write_i4(mem, SRC, 5)
        call(cpu, mem, stubs, SRC, VT_I2, name="Ordinal #12")
        assert cpu.regs[EAX] == S_OK
        assert read_i2(mem, DEST) == 5


class TestUnhandledTypesHaltLoudly:
    def test_unhandled_source_vt_halts(self, env):
        cpu, mem, stubs = env
        mem.write16(SRC, 8)  # VT_BSTR -- not implemented
        call(cpu, mem, stubs, SRC, VT_I4)
        assert cpu.halted
        assert cpu.fatal_halt

    def test_unhandled_target_vt_halts(self, env):
        cpu, mem, stubs = env
        write_i4(mem, SRC, 1)
        call(cpu, mem, stubs, SRC, 8)  # VT_BSTR -- not implemented
        assert cpu.halted
        assert cpu.fatal_halt
