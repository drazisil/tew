"""Tests for kernel32.dll!CompareStringA/CompareStringW's locale handling.

Root cause this guards against: neither handler used to read the Locale
argument at all, so ANY value (including 0, "no locale specified") silently
"succeeded" with a real string comparison. Found live: msjet35.dll's own
default-collating-order fallback logic (FUN_7a84c830) deliberately probes
CompareStringA(0, ...) specifically to detect failure and substitute a safe
default; tew's always-succeeds behavior meant that probe never failed, so a
database opened with no explicit locale ended up with no valid
collating-order id at all.

locale == 0 is genuinely invalid on real (NT4/2000/XP-era) Windows via
IsValidLocale, so it's the one value that must still fail here -- but an
earlier, narrower fix over-corrected by rejecting every locale except
0x0409, which broke three separate real, legitimate LCIDs found live over
several sessions (0x0400/0x0800 LOCALE_USER/SYSTEM_DEFAULT, 0x007F
LOCALE_INVARIANT, 0x0009 MAKELANGID(LANG_ENGLISH, SUBLANG_NEUTRAL)). Real
CompareStringA/W essentially never fails for a plausible nonzero LCID, so
every nonzero value now succeeds; only 0 is rejected.
"""
from __future__ import annotations

import pytest

from tew.api._state import CRTState, TEB_BASE
from tew.api.kernel32_io import register_kernel32_io_handlers
from tew.api.win32_errors import Win32Error
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


MEM_SIZE = 4 * 1024 * 1024
STACK    = 0x200000
BUF_A    = 0x300000
BUF_B    = 0x310000

VALID_LCID = 0x0409  # the one locale this emulator models (en-US)


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_kernel32_io_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    return cpu, mem, stubs


def call(stubs, cpu, mem, name, args):
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    for i, val in enumerate(args):
        mem.write32(STACK + 4 + i * 4, val)
    stubs.get("kernel32.dll", name)(cpu)


def write_ansi(mem, addr, s: str) -> None:
    for i, ch in enumerate(s):
        mem.write8(addr + i, ord(ch))
    mem.write8(addr + len(s), 0)


def write_wide(mem, addr, s: str) -> None:
    for i, ch in enumerate(s):
        mem.write16(addr + i * 2, ord(ch))
    mem.write16(addr + len(s) * 2, 0)


class TestCompareStringAValidLocale:

    def test_equal_strings(self, env):
        cpu, mem, stubs = env
        write_ansi(mem, BUF_A, "abc")
        write_ansi(mem, BUF_B, "abc")
        call(stubs, cpu, mem, "CompareStringA", [VALID_LCID, 0, BUF_A, 3, BUF_B, 3])
        assert cpu.regs[EAX] == 2  # CSTR_EQUAL

    def test_less_than(self, env):
        cpu, mem, stubs = env
        write_ansi(mem, BUF_A, "abc")
        write_ansi(mem, BUF_B, "abd")
        call(stubs, cpu, mem, "CompareStringA", [VALID_LCID, 0, BUF_A, 3, BUF_B, 3])
        assert cpu.regs[EAX] == 1  # CSTR_LESS_THAN

    def test_greater_than(self, env):
        cpu, mem, stubs = env
        write_ansi(mem, BUF_A, "abd")
        write_ansi(mem, BUF_B, "abc")
        call(stubs, cpu, mem, "CompareStringA", [VALID_LCID, 0, BUF_A, 3, BUF_B, 3])
        assert cpu.regs[EAX] == 3  # CSTR_GREATER_THAN

    def test_stdcall_cleanup(self, env):
        cpu, mem, stubs = env
        write_ansi(mem, BUF_A, "a")
        write_ansi(mem, BUF_B, "a")
        call(stubs, cpu, mem, "CompareStringA", [VALID_LCID, 0, BUF_A, 1, BUF_B, 1])
        assert cpu.regs[ESP] == STACK + 24


class TestCompareStringAInvalidLocale:

    def test_zero_locale_fails(self, env):
        """The exact live scenario: msjet35.dll probing with no locale specified."""
        cpu, mem, stubs = env
        write_ansi(mem, BUF_A, "x")
        write_ansi(mem, BUF_B, "x")
        call(stubs, cpu, mem, "CompareStringA", [0, 0, BUF_A, 1, BUF_B, 1])
        assert cpu.regs[EAX] == 0

    def test_zero_locale_sets_last_error(self, env):
        cpu, mem, stubs = env
        write_ansi(mem, BUF_A, "x")
        write_ansi(mem, BUF_B, "x")
        call(stubs, cpu, mem, "CompareStringA", [0, 0, BUF_A, 1, BUF_B, 1])
        assert mem.read32(TEB_BASE + 0x34) == int(Win32Error.ERROR_INVALID_PARAMETER)

    def test_unrelated_nonzero_locale_succeeds(self, env):
        """Real, non-en-US LCIDs must NOT fail -- only locale 0 is special
        (see module docstring / msjet35.dll's default-collating-order probe).
        0x0400/0x0800 (LOCALE_USER/SYSTEM_DEFAULT), 0x007F (LOCALE_INVARIANT),
        and 0x0009 (MAKELANGID(LANG_ENGLISH, SUBLANG_NEUTRAL)) each turned
        out to be real LCIDs an earlier, narrower allowlist wrongly rejected
        -- de-DE (0x0407) is just as real and must succeed too."""
        cpu, mem, stubs = env
        write_ansi(mem, BUF_A, "x")
        write_ansi(mem, BUF_B, "x")
        call(stubs, cpu, mem, "CompareStringA", [0x0407, 0, BUF_A, 1, BUF_B, 1])  # de-DE
        assert cpu.regs[EAX] == 2  # CSTR_EQUAL — real comparison, not a rejection

    def test_invalid_locale_still_cleans_up_stack(self, env):
        cpu, mem, stubs = env
        write_ansi(mem, BUF_A, "x")
        write_ansi(mem, BUF_B, "x")
        call(stubs, cpu, mem, "CompareStringA", [0, 0, BUF_A, 1, BUF_B, 1])
        assert cpu.regs[ESP] == STACK + 24


class TestCompareStringWValidLocale:

    def test_equal_strings(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "abc")
        write_wide(mem, BUF_B, "abc")
        call(stubs, cpu, mem, "CompareStringW", [VALID_LCID, 0, BUF_A, 3, BUF_B, 3])
        assert cpu.regs[EAX] == 2


class TestCompareStringWInvalidLocale:

    def test_zero_locale_fails(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "x")
        write_wide(mem, BUF_B, "x")
        call(stubs, cpu, mem, "CompareStringW", [0, 0, BUF_A, 1, BUF_B, 1])
        assert cpu.regs[EAX] == 0

    def test_zero_locale_sets_last_error(self, env):
        cpu, mem, stubs = env
        write_wide(mem, BUF_A, "x")
        write_wide(mem, BUF_B, "x")
        call(stubs, cpu, mem, "CompareStringW", [0, 0, BUF_A, 1, BUF_B, 1])
        assert mem.read32(TEB_BASE + 0x34) == int(Win32Error.ERROR_INVALID_PARAMETER)
