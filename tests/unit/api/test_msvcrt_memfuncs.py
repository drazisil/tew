"""Tests for memcpy/memmove/memset/memcmp -- verifies both normal correctness
and the out-of-range guard added after a real hang investigation traced a
multi-minute "freeze" to _memmove looping `for idx in range(n)` with an
unchecked, garbage n (observed as ~0xFFFFFFFF): billions of individual
read8/write8 calls that eventually swept through and corrupted tew's own
0x200000+ Win32 API trampoline region, rather than a genuine CPU/scheduler
hang. See memory/changelog.md, "FUN_0448a033 hang" investigation.
"""
from __future__ import annotations

import pytest
from tew.api._state import CRTState
from tew.api.msvcrt_handlers import register_msvcrt_handlers
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP


# ── Shared test infrastructure ────────────────────────────────────────────────

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


MEM_SIZE = 8 * 1024 * 1024   # 8 MB -- matches test_msvcrt_realloc.py; must be
                              # big enough to cover fixed addresses (e.g.
                              # msvcrt's own _FMODE_ADDR ~0x21001c) that
                              # register_msvcrt_handlers writes at setup, but
                              # still small enough that an unguarded `range(n)`
                              # bug would make these tests hang instead of
                              # silently passing.


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_msvcrt_handlers(stubs, mem, state)
    cpu   = _FakeCPU()
    cpu.regs[ESP] = 0x1000
    mem.write32(cpu.regs[ESP], 0xDEAD)  # return address
    return cpu, mem, state, stubs


def call3(cpu, mem, stubs, dll_func, a0, a1, a2):
    mem.write32(cpu.regs[ESP] + 4, a0)
    mem.write32(cpu.regs[ESP] + 8, a1)
    mem.write32(cpu.regs[ESP] + 12, a2)
    stubs.get("msvcrt.dll", dll_func)(cpu)


# ── memcpy ───────────────────────────────────────────────────────────────────

class TestMemcpy:
    def test_copies_bytes_and_returns_dst(self, env):
        cpu, mem, state, stubs = env
        src, dst = 0x100, 0x200
        for i in range(8):
            mem.write8(src + i, 0xA0 + i)
        call3(cpu, mem, stubs, "memcpy", dst, src, 8)
        assert cpu.regs[EAX] == dst
        assert not cpu.halted
        for i in range(8):
            assert mem.read8(dst + i) == 0xA0 + i

    def test_huge_size_halts_instead_of_looping(self, env):
        """Regression: this used to loop `for idx in range(n)` unbounded,
        eventually sweeping into and corrupting the 0x200000+ trampoline
        region. Must now halt immediately instead."""
        cpu, mem, state, stubs = env
        call3(cpu, mem, stubs, "memcpy", 0x100, 0x200, 0xFFFFFFFF)
        assert cpu.halted
        assert cpu.fatal_halt

    def test_dst_plus_n_past_end_of_memory_halts(self, env):
        cpu, mem, state, stubs = env
        call3(cpu, mem, stubs, "memcpy", MEM_SIZE - 4, 0x100, 64)
        assert cpu.halted
        assert cpu.fatal_halt

    def test_src_plus_n_past_end_of_memory_halts(self, env):
        cpu, mem, state, stubs = env
        call3(cpu, mem, stubs, "memcpy", 0x100, MEM_SIZE - 4, 64)
        assert cpu.halted
        assert cpu.fatal_halt


# ── memmove ──────────────────────────────────────────────────────────────────

class TestMemmove:
    def test_forward_copy(self, env):
        cpu, mem, state, stubs = env
        src, dst = 0x100, 0x200
        for i in range(8):
            mem.write8(src + i, 0xB0 + i)
        call3(cpu, mem, stubs, "memmove", dst, src, 8)
        assert not cpu.halted
        for i in range(8):
            assert mem.read8(dst + i) == 0xB0 + i

    def test_overlapping_backward_copy(self, env):
        cpu, mem, state, stubs = env
        base = 0x100
        for i in range(8):
            mem.write8(base + i, 0xC0 + i)
        # dst is 2 bytes after src, within the same 8-byte run -- must not
        # clobber source bytes before they're read.
        call3(cpu, mem, stubs, "memmove", base + 2, base, 8)
        assert not cpu.halted
        for i in range(8):
            assert mem.read8(base + 2 + i) == 0xC0 + i

    def test_huge_size_halts_instead_of_looping(self, env):
        cpu, mem, state, stubs = env
        call3(cpu, mem, stubs, "memmove", 0x100, 0x200, 0xFFFFFFFF)
        assert cpu.halted
        assert cpu.fatal_halt


# ── memset ───────────────────────────────────────────────────────────────────

class TestMemset:
    def test_fills_bytes_and_returns_ptr(self, env):
        cpu, mem, state, stubs = env
        ptr = 0x100
        call3(cpu, mem, stubs, "memset", ptr, 0x41, 8)
        assert cpu.regs[EAX] == ptr
        assert not cpu.halted
        for i in range(8):
            assert mem.read8(ptr + i) == 0x41

    def test_huge_size_halts_instead_of_looping(self, env):
        cpu, mem, state, stubs = env
        call3(cpu, mem, stubs, "memset", 0x100, 0, 0xFFFFFFFF)
        assert cpu.halted
        assert cpu.fatal_halt

    def test_ptr_plus_n_past_end_of_memory_halts(self, env):
        cpu, mem, state, stubs = env
        call3(cpu, mem, stubs, "memset", MEM_SIZE - 4, 0, 64)
        assert cpu.halted
        assert cpu.fatal_halt


# ── memcmp ───────────────────────────────────────────────────────────────────

class TestMemcmp:
    def test_equal_returns_zero(self, env):
        cpu, mem, state, stubs = env
        p1, p2 = 0x100, 0x200
        for i in range(8):
            mem.write8(p1 + i, 0x55)
            mem.write8(p2 + i, 0x55)
        call3(cpu, mem, stubs, "memcmp", p1, p2, 8)
        assert not cpu.halted
        assert cpu.regs[EAX] == 0

    def test_first_difference_determines_sign(self, env):
        cpu, mem, state, stubs = env
        p1, p2 = 0x100, 0x200
        mem.write8(p1, 5)
        mem.write8(p2, 9)
        call3(cpu, mem, stubs, "memcmp", p1, p2, 1)
        assert cpu.regs[EAX] == 0xFFFFFFFF  # p1 < p2

    def test_huge_size_halts_instead_of_looping(self, env):
        cpu, mem, state, stubs = env
        call3(cpu, mem, stubs, "memcmp", 0x100, 0x200, 0xFFFFFFFF)
        assert cpu.halted
        assert cpu.fatal_halt
