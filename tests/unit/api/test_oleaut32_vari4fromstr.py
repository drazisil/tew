"""Tests for oleaut32.dll!VarI4FromStr (also Ordinal #64) -- previously
unimplemented (hard halt). Live-confirmed call: MSJET35.DLL's expression
parser converting a plain decimal numeric literal (e.g. "251658241")
inside a WHERE-clause expression to VT_I4. Only the well-defined case
(optional sign, ASCII digits, optional surrounding whitespace) is
implemented; anything else returns the real DISP_E_TYPEMISMATCH HRESULT
rather than guessing at locale-specific parsing never observed live.
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


def call(cpu, mem, stubs, s, dll="oleaut32.dll", name="VarI4FromStr"):
    write_wide(mem, STR_BUF, s)
    mem.write32(STACK + 4, STR_BUF)   # strIn
    mem.write32(STACK + 8, 0)         # lcid
    mem.write32(STACK + 12, 0)        # dwFlags
    mem.write32(STACK + 16, OUT)      # plOut
    stubs.get(dll, name)(cpu)


class TestPlainDecimal:
    def test_zero_confirmed_live_case(self, env):
        """Exact shape observed live: a plain positive decimal literal."""
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "251658241")
        assert cpu.regs[EAX] == S_OK
        assert mem.read_signed32(OUT) == 251658241

    def test_negative(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "-42")
        assert cpu.regs[EAX] == S_OK
        assert mem.read_signed32(OUT) == -42

    def test_explicit_plus_sign(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "+7")
        assert cpu.regs[EAX] == S_OK
        assert mem.read_signed32(OUT) == 7

    def test_surrounding_whitespace(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "  123  ")
        assert cpu.regs[EAX] == S_OK
        assert mem.read_signed32(OUT) == 123

    def test_zero(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "0")
        assert cpu.regs[EAX] == S_OK
        assert mem.read_signed32(OUT) == 0


class TestOverflow:
    def test_above_int32_max_returns_disp_e_overflow(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "2147483648")
        assert cpu.regs[EAX] == DISP_E_OVERFLOW

    def test_below_int32_min_returns_disp_e_overflow(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "-2147483649")
        assert cpu.regs[EAX] == DISP_E_OVERFLOW

    def test_int32_max_boundary_fits(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "2147483647")
        assert cpu.regs[EAX] == S_OK
        assert mem.read_signed32(OUT) == 2147483647

    def test_int32_min_boundary_fits(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "-2147483648")
        assert cpu.regs[EAX] == S_OK
        assert mem.read_signed32(OUT) == -2147483648


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
        call(cpu, mem, stubs, "123abc")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_decimal_point_not_accepted(self, env):
        # Real integer conversion, not a float coercion -- "1.5" isn't a
        # plain decimal integer literal.
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1.5")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_thousands_separator_not_accepted(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1,234")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH


class TestNullStringPointer:
    def test_null_strin_returns_type_mismatch(self, env):
        cpu, mem, stubs = env
        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, 0)
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, OUT)
        stubs.get("oleaut32.dll", "VarI4FromStr")(cpu)
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH


class TestOrdinal64MatchesNamedExport:
    def test_ordinal_64_is_the_same_real_function(self, env):
        cpu, mem, stubs = env
        assert stubs.get("oleaut32.dll", "Ordinal #64") is stubs.get("oleaut32.dll", "VarI4FromStr")

    def test_ordinal_64_callable(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "5", name="Ordinal #64")
        assert cpu.regs[EAX] == S_OK
        assert mem.read_signed32(OUT) == 5


class TestStdcallCleanup:
    def test_cleans_16_bytes(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "5")
        assert cpu.regs[ESP] == STACK + 16
