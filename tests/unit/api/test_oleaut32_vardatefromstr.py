"""Tests for oleaut32.dll!VarDateFromStr (also Ordinal #94) -- previously
unimplemented (hard halt), the halt exposed once VarR8FromStr (Ordinal #84)
was fixed. Live-confirmed real input: MSJET35.DLL's expression parser
evaluating a WHERE-clause date literal comparison
(`(BrandedPart.MfgDate)<>#1/1/2010#`) calls this with the exact string
'1/1/2010' -- no '#' delimiters (Jet's own tokenizer strips them before
this call), no time component, no 2-digit year.

Only the well-defined M/D/YYYY numeric format (4-digit year, '/' separator)
is implemented, with real calendar validation (via datetime.date, not
hand-rolled day-per-month tables) and the documented OLE Automation /
Lotus-1-2-3-compatibility epoch quirk (1900 treated as a leap year for
backward compatibility, so dates >= 1900-03-01 need a +1 day correction).
Anything outside that exact shape returns the real DISP_E_TYPEMISMATCH
HRESULT rather than guessing at locale-specific date formats never
observed live.
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


def call(cpu, mem, stubs, s, dll="oleaut32.dll", name="VarDateFromStr"):
    write_wide(mem, STR_BUF, s)
    mem.write32(STACK + 4, STR_BUF)   # strIn
    mem.write32(STACK + 8, 0)         # lcid
    mem.write32(STACK + 12, 0)        # dwFlags
    mem.write32(STACK + 16, OUT)      # pdateOut
    stubs.get(dll, name)(cpu)


class TestPlainDate:
    def test_exact_live_case(self, env):
        """The exact real input captured live: 'MfgDate <> #1/1/2010#'."""
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1/1/2010")
        assert cpu.regs[EAX] == S_OK
        assert read_f64(mem, OUT) == 40180.0

    def test_double_digit_month_and_day(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "12/25/2010")
        assert cpu.regs[EAX] == S_OK

    def test_leading_zeros_match_no_leading_zeros(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "01/01/2010")
        val_padded = read_f64(mem, OUT)
        call(cpu, mem, stubs, "1/1/2010")
        val_unpadded = read_f64(mem, OUT)
        assert val_padded == val_unpadded


class TestEpochMath:
    """Pins the day-count formula and the Lotus-1900-leap-year correction."""

    def test_epoch_day_zero(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "12/30/1899")
        assert cpu.regs[EAX] == S_OK
        assert read_f64(mem, OUT) == 0.0

    def test_day_one(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "12/31/1899")
        assert read_f64(mem, OUT) == 1.0

    def test_jan_1_1900_is_day_two(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1/1/1900")
        assert read_f64(mem, OUT) == 2.0

    def test_feb_28_1900_is_day_sixty(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "2/28/1900")
        assert read_f64(mem, OUT) == 60.0

    def test_march_1_1900_gets_lotus_quirk_correction(self, env):
        # Naive (uncorrected) Gregorian day-count would be 61.0; the +1
        # correction for the fictitious Feb 29 1900 makes it 62.0.
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "3/1/1900")
        assert read_f64(mem, OUT) == 62.0


class TestLeapYears:
    def test_feb_29_on_real_leap_year_valid(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "2/29/2020")
        assert cpu.regs[EAX] == S_OK

    def test_feb_29_on_non_leap_year_invalid(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "2/29/2019")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH


class TestInvalidDates:
    def test_month_thirteen(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "13/1/2010")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_month_zero(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "0/1/2010")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_day_zero(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1/0/2010")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_day_thirty_two(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1/32/2010")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_february_thirty(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "2/30/2010")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH


class TestMalformedStrings:
    def test_empty_string(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_non_numeric(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "hello")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_dash_separator_not_accepted(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "2010-01-01")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_dot_separator_not_accepted(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1.1.2010")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_missing_year(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1/1")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_two_digit_year_out_of_scope(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1/1/10")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH

    def test_time_component_out_of_scope(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1/1/2010 3:30:00 PM")
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH


class TestNullStringPointer:
    def test_null_strin_returns_type_mismatch(self, env):
        cpu, mem, stubs = env
        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, 0)
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, OUT)
        stubs.get("oleaut32.dll", "VarDateFromStr")(cpu)
        assert cpu.regs[EAX] == DISP_E_TYPEMISMATCH


class TestOrdinal94MatchesNamedExport:
    def test_ordinal_94_is_the_same_real_function(self, env):
        cpu, mem, stubs = env
        assert stubs.get("oleaut32.dll", "Ordinal #94") is stubs.get("oleaut32.dll", "VarDateFromStr")

    def test_ordinal_94_callable(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1/1/2010", name="Ordinal #94")
        assert cpu.regs[EAX] == S_OK


class TestStdcallCleanup:
    def test_cleans_16_bytes(self, env):
        cpu, mem, stubs = env
        call(cpu, mem, stubs, "1/1/2010")
        assert cpu.regs[ESP] == STACK + 16
