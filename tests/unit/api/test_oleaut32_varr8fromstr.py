"""Tests for oleaut32.dll!VarR8FromStr (also Ordinal #84) -- previously
unimplemented (hard halt), the next halt exposed once VarI4FromStr
(Ordinal #64) was fixed. Same live call chain: MSJET35.DLL's expression
parser evaluating a numeric literal -- Jet tries the integer conversion
first and falls back to a real-number conversion. Only the well-defined,
non-locale-specific case (standard invariant-culture decimal/scientific
float literal) is implemented; anything else returns the real
DISP_E_TYPEMISMATCH HRESULT rather than guessing at locale-specific
formats never observed live.
"""
from __future__ import annotations

import struct

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
STR_BUF  = 0x300000
OUT      = 0x301000

S_OK = 0
DISP_E_TYPEMISMATCH = 0x80020005
DISP_E_OVERFLOW = 0x8002000A


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


def write_wide(mem, addr, s):
    for i, ch in enumerate(s):
        mem.write16(addr + i * 2, ord(ch))
    mem.write16(addr + len(s) * 2, 0)


def read_f64(mem, addr):
    lo = mem.read32(addr)
    hi = mem.read32(addr + 4)
    raw = lo.to_bytes(4, "little") + hi.to_bytes(4, "little")
    return struct.unpack("<d", raw)[0]


def call(cpu, mem, stubs, s, dll="oleaut32.dll", name="VarR8FromStr"):
    write_wide(mem, STR_BUF, s)
    mem.write32(STACK + 4, STR_BUF)   # strIn
    mem.write32(STACK + 8, 0)         # lcid
    mem.write32(STACK + 12, 0)        # dwFlags
    mem.write32(STACK + 16, OUT)      # pdblOut
    stubs.get(dll, name)(cpu)


class TestPlainDecimal:
    def test_integer_literal(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "251658241")
        assert cpu.regs[EAX] == S_OK
        assert read_f64(mem, OUT) == 251658241.0

    def test_fractional(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "3.14159")
        assert cpu.regs[EAX] == S_OK
        assert read_f64(mem, OUT) == pytest.approx(3.14159)

    def test_negative(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "-2.5")
        assert cpu.regs[EAX] == S_OK
        assert read_f64(mem, OUT) == -2.5

    def test_leading_decimal_point(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, ".5")
        assert cpu.regs[EAX] == S_OK
        assert read_f64(mem, OUT) == 0.5

    def test_trailing_decimal_point(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "5.")
        assert cpu.regs[EAX] == S_OK
        assert read_f64(mem, OUT) == 5.0

    def test_scientific_notation(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1.5e3")
        assert cpu.regs[EAX] == S_OK
        assert read_f64(mem, OUT) == 1500.0

    def test_surrounding_whitespace(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "  42.0  ")
        assert cpu.regs[EAX] == S_OK
        assert read_f64(mem, OUT) == 42.0

    def test_zero(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "0")
        assert cpu.regs[EAX] == S_OK
        assert read_f64(mem, OUT) == 0.0


class TestNonNumericReturnsTypeMismatch:
    def test_empty_string(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_alpha_text(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "hello")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_trailing_garbage(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "3.14xyz")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_bare_dot_not_accepted(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, ".")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_python_special_inf_not_accepted(self, env):
        # float("inf") is valid Python but not a real numeric string
        # literal in the OLE Automation sense -- must reject it via the
        # shape regex, not trust float() directly.
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "inf")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_python_special_nan_not_accepted(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "nan")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_thousands_separator_not_accepted(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1,234.5")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH


class TestNullStringPointer:
    def test_null_strin_returns_type_mismatch(self, env):
        cpu, mem, stubs = env
        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, 0)
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, OUT)
        stubs.get("oleaut32.dll", "VarR8FromStr")(cpu)
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH


class TestOrdinal84MatchesNamedExport:
    def test_ordinal_84_is_the_same_real_function(self, env):
        cpu, mem, stubs = env
        assert stubs.get("oleaut32.dll", "Ordinal #84") is stubs.get("oleaut32.dll", "VarR8FromStr")

    def test_ordinal_84_callable(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "2.5", name="Ordinal #84")
        assert cpu.regs[EAX] == S_OK
        assert read_f64(mem, OUT) == 2.5


class TestStdcallCleanup:
    def test_cleans_16_bytes(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "5")
        assert cpu.regs[ESP] == STACK + 16
