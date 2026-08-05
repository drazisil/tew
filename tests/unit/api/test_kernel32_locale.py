"""Tests for kernel32.dll locale/codepage/string-conversion handlers."""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.kernel32_locale import register_kernel32_locale_handlers
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


MEM_SIZE  = 4 * 1024 * 1024
STACK     = 0x200000
BUF_A     = 0x300000
BUF_B     = 0x310000


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_kernel32_locale_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # fake return address
    return cpu, mem, stubs


def call(stubs, cpu, mem, name, args):
    """args: list of 32-bit values to write at STACK+4, STACK+8, ..."""
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    for i, val in enumerate(args):
        mem.write32(STACK + 4 + i * 4, val)
    stubs.get("kernel32.dll", name)(cpu)


def write_wide(mem, addr, s: str) -> None:
    for i, ch in enumerate(s):
        mem.write16(addr + i * 2, ord(ch))
    mem.write16(addr + len(s) * 2, 0)


def read_wide(mem, addr, count) -> str:
    return "".join(chr(mem.read16(addr + i * 2)) for i in range(count))


# ── GetACP / GetCPInfo / IsValidCodePage / IsDBCSLeadByte ─────────────────────

class TestGetACP:

    def test_returns_1252(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "GetACP", [])
        assert cpu.regs[EAX] == 1252


class TestGetCPInfo:

    def test_max_char_size_and_default_char(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "GetCPInfo", [1252, BUF_A])
        assert mem.read32(BUF_A) == 1
        assert mem.read8(BUF_A + 4) == 0x3F
        assert mem.read8(BUF_A + 5) == 0

    def test_all_lead_byte_ranges_empty_for_1252(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "GetCPInfo", [1252, BUF_A])
        for i in range(6):
            lo = mem.read8(BUF_A + 6 + i * 2)
            hi = mem.read8(BUF_A + 7 + i * 2)
            assert (lo, hi) == (0, 0)

    def test_returns_true_and_cleans_up(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "GetCPInfo", [1252, BUF_A])
        assert cpu.regs[EAX] == 1
        assert cpu.regs[ESP] == STACK + 8


class TestIsValidCodePage:

    def test_always_returns_true(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "IsValidCodePage", [1252])
        assert cpu.regs[EAX] == 1

    def test_stdcall_cleanup(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "IsValidCodePage", [1252])
        assert cpu.regs[ESP] == STACK + 4


class TestIsDBCSLeadByte:

    def test_ascii_byte_is_not_lead_byte(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "IsDBCSLeadByte", [ord("A")])
        assert cpu.regs[EAX] == 0

    def test_cp932_lead_byte_value_still_false_under_cp1252(self, env):
        """0x81 is a Shift-JIS lead byte, but this process reports cp1252 (single-byte)."""
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "IsDBCSLeadByte", [0x81])
        assert cpu.regs[EAX] == 0

    def test_stdcall_cleanup(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "IsDBCSLeadByte", [0x81])
        assert cpu.regs[ESP] == STACK + 4


# ── MultiByteToWideChar / WideCharToMultiByte ─────────────────────────────────

class TestMultiByteToWideChar:

    def test_size_query_mode(self, env):
        cpu, mem, stubs = env
        for i, b in enumerate(b"hello"):
            mem.write8(BUF_A + i, b)
        call(stubs, cpu, mem, "MultiByteToWideChar", [0, 0, BUF_A, 5, 0, 0])
        assert cpu.regs[EAX] == 5

    def test_size_query_mode_does_not_write(self, env):
        cpu, mem, stubs = env
        for i, b in enumerate(b"hello"):
            mem.write8(BUF_A + i, b)
        mem.write16(BUF_B, 0xBEEF)
        call(stubs, cpu, mem, "MultiByteToWideChar", [0, 0, BUF_A, 5, 0, 0])
        assert mem.read16(BUF_B) == 0xBEEF

    def test_normal_conversion(self, env):
        cpu, mem, stubs = env
        for i, b in enumerate(b"hi"):
            mem.write8(BUF_A + i, b)
        call(stubs, cpu, mem, "MultiByteToWideChar", [0, 0, BUF_A, 2, BUF_B, 2])
        assert read_wide(mem, BUF_B, 2) == "hi"
        assert cpu.regs[EAX] == 2

    def test_destination_smaller_than_source_truncates(self, env):
        cpu, mem, stubs = env
        for i, b in enumerate(b"hello"):
            mem.write8(BUF_A + i, b)
        call(stubs, cpu, mem, "MultiByteToWideChar", [0, 0, BUF_A, 5, BUF_B, 3])
        assert cpu.regs[EAX] == 3
        assert read_wide(mem, BUF_B, 3) == "hel"

    def test_stdcall_cleanup(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "MultiByteToWideChar", [0, 0, BUF_A, 0, BUF_B, 0])
        assert cpu.regs[ESP] == STACK + 24


class TestWideCharToMultiByte:

    def test_size_query_mode(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "hi")
        call(stubs, cpu, mem, "WideCharToMultiByte", [0, 0, BUF_A, 2, 0, 0, 0, 0])
        assert cpu.regs[EAX] == 2

    def test_normal_conversion(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "hi")
        call(stubs, cpu, mem, "WideCharToMultiByte", [0, 0, BUF_A, 2, BUF_B, 2, 0, 0])
        assert cpu.regs[EAX] == 2
        assert mem.read8(BUF_B) == ord("h")
        assert mem.read8(BUF_B + 1) == ord("i")

    def test_unmappable_wide_char_becomes_question_mark(self, env):
        cpu, mem, stubs = env
        mem.write16(BUF_A, 0x3042)  # a non-Latin1 wide char
        call(stubs, cpu, mem, "WideCharToMultiByte", [0, 0, BUF_A, 1, BUF_B, 1, 0, 0])
        assert mem.read8(BUF_B) == 0x3F

    def test_stdcall_cleanup(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "hi")
        call(stubs, cpu, mem, "WideCharToMultiByte", [0, 0, BUF_A, 0, BUF_B, 0, 0, 0])
        assert cpu.regs[ESP] == STACK + 32


# ── GetStringTypeW / LCMapStringW ─────────────────────────────────────────────

class TestGetStringTypeW:

    def test_unsupported_info_type_halts(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "A")
        call(stubs, cpu, mem, "GetStringTypeW", [2, BUF_A, 1, BUF_B])  # CT_CTYPE2
        assert cpu.halted is True

    def test_supported_ctype1_does_not_halt(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "A")
        call(stubs, cpu, mem, "GetStringTypeW", [1, BUF_A, 1, BUF_B])  # CT_CTYPE1
        assert cpu.halted is False
        assert cpu.regs[EAX] == 1

    def test_supported_ctype1_cleans_up_stack(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "A")
        call(stubs, cpu, mem, "GetStringTypeW", [1, BUF_A, 1, BUF_B])
        assert cpu.regs[ESP] == STACK + 16


class TestLCMapStringW:

    def test_unsupported_flags_halt(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "Hi")
        call(stubs, cpu, mem, "LCMapStringW", [0, 0x400, BUF_A, 2, BUF_B, 2])  # LCMAP_SORTKEY
        assert cpu.halted is True

    def test_supported_lowercase_does_not_halt(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "Hi")
        call(stubs, cpu, mem, "LCMapStringW", [0, 0x100, BUF_A, 2, BUF_B, 2])  # LCMAP_LOWERCASE
        assert cpu.halted is False
        assert cpu.regs[EAX] == 2
        assert read_wide(mem, BUF_B, 2) == "hi"

    def test_supported_flags_clean_up_stack(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "Hi")
        call(stubs, cpu, mem, "LCMapStringW", [0, 0x100, BUF_A, 2, BUF_B, 2])
        assert cpu.regs[ESP] == STACK + 24


class TestGetLocaleInfoA:

    def test_returns_zero(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "GetLocaleInfoA", [0, 0, BUF_A, 0])
        assert cpu.regs[EAX] == 0

    def test_does_not_halt(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "GetLocaleInfoA", [0, 0, BUF_A, 0])
        assert cpu.halted is False

    def test_stdcall_cleanup(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "GetLocaleInfoA", [0, 0, BUF_A, 0])
        assert cpu.regs[ESP] == STACK + 16


# ── Fiber-local storage (unimplemented) ───────────────────────────────────────

FLS_NAMES = ["FlsAlloc", "FlsSetValue", "FlsGetValue", "FlsFree"]


class TestFiberLocalStorage:

    @pytest.mark.parametrize("name", FLS_NAMES)
    def test_halts(self, env, name):
        cpu, mem, stubs = env
        stubs.get("kernel32.dll", name)(cpu)
        assert cpu.halted is True

    @pytest.mark.parametrize("name", FLS_NAMES)
    def test_sets_fatal_halt(self, env, name):
        cpu, mem, stubs = env
        stubs.get("kernel32.dll", name)(cpu)
        assert cpu.fatal_halt is True

    @pytest.mark.parametrize("name", FLS_NAMES)
    def test_skips_stdcall_cleanup(self, env, name):
        cpu, mem, stubs = env
        stubs.get("kernel32.dll", name)(cpu)
        assert cpu.regs[ESP] == STACK
