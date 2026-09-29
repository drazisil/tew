"""Tests for msvcrt.dll!_stricmp -- "C"-locale case-insensitive compare.

Reached for real once DAO350.DLL stopped being mapped over the heap: its
compare helper uses _stricmp whenever the system code page has no DBCS
lead bytes (cp1252), which is the normal US path.
"""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.msvcrt_handlers import register_msvcrt_handlers
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
BUF_A    = 0x300000
BUF_B    = 0x310000


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    stubs = _StubHandlers()
    register_msvcrt_handlers(stubs, mem, CRTState())
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    return cpu, mem, stubs


def stricmp(cpu, mem, stubs, a: bytes | None, b: bytes | None) -> int:
    pa = pb = 0
    if a is not None:
        mem.load(BUF_A, a + b"\0")
        pa = BUF_A
    if b is not None:
        mem.load(BUF_B, b + b"\0")
        pb = BUF_B
    mem.write32(STACK + 4, pa)
    mem.write32(STACK + 8, pb)
    stubs.get("msvcrt.dll", "_stricmp")(cpu)
    v = cpu.regs[EAX] & 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def test_equal_ignoring_case(env):
    assert stricmp(*env, b"TableDefs", b"TABLEDEFS") == 0


def test_ordering_uses_folded_bytes(env):
    assert stricmp(*env, b"abc", b"ABD") < 0
    assert stricmp(*env, b"ABD", b"abc") > 0


def test_prefix_is_less(env):
    assert stricmp(*env, b"abc", b"ABCD") < 0


def test_returns_folded_byte_difference(env):
    # 'b' (0x62) vs 'A'->'a' (0x61)
    assert stricmp(*env, b"b", b"A") == 1


def test_non_letters_fold_to_lowercase_side(env):
    # '_' (0x5F) sits between 'Z' and 'a': lowercasing puts 'A' above it,
    # the real CRT's _stricmp ordering (unlike an uppercasing compare).
    assert stricmp(*env, b"_", b"A") < 0


def test_high_bytes_not_folded_in_c_locale(env):
    assert stricmp(*env, b"\xc0", b"\xe0") != 0


def test_null_argument_halts(env):
    cpu, mem, stubs = env
    stricmp(cpu, mem, stubs, None, b"x")
    assert cpu.halted and cpu.fatal_halt
