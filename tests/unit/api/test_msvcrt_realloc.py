"""Tests for _realloc — verifies data is copied from the old allocation."""
from __future__ import annotations

import pytest
from tew.api._state import CRTState
from tew.api.msvcrt_handlers import register_msvcrt_handlers
from tew.hardware.memory import Memory
from tew.hardware.cpu import EAX, ESP


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


MEM_SIZE   = 8 * 1024 * 1024   # 8 MB
HEAP_START = 0x500000          # override heap start to keep it within MEM_SIZE


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    state.next_heap_alloc = HEAP_START
    stubs = _StubHandlers()
    register_msvcrt_handlers(stubs, mem, state)
    cpu   = _FakeCPU()
    cpu.regs[ESP] = 0x200000
    mem.write32(cpu.regs[ESP], 0xDEAD)  # return address
    return cpu, mem, state, stubs


def call_realloc(cpu, mem, ptr, size):
    mem.write32(cpu.regs[ESP] + 4, ptr)
    mem.write32(cpu.regs[ESP] + 8, size)
    fn = _StubHandlers.__new__(_StubHandlers)  # avoid re-registering
    # Retrieve from the already-registered stubs via env fixture's stubs object
    # (caller passes stubs in directly — see tests below)
    raise NotImplementedError("use env fixture stubs directly")


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestReallocNullPtr:
    def test_null_ptr_acts_like_malloc(self, env):
        cpu, mem, state, stubs = env
        mem.write32(cpu.regs[ESP] + 4, 0)   # ptr = NULL
        mem.write32(cpu.regs[ESP] + 8, 64)  # size = 64
        stubs.get("msvcrt.dll", "realloc")(cpu)
        assert cpu.regs[EAX] != 0
        assert state.heap_alloc_sizes[cpu.regs[EAX]] == 64

    def test_null_ptr_size_zero_returns_null(self, env):
        cpu, mem, state, stubs = env
        mem.write32(cpu.regs[ESP] + 4, 0)
        mem.write32(cpu.regs[ESP] + 8, 0)
        stubs.get("msvcrt.dll", "realloc")(cpu)
        assert cpu.regs[EAX] == 0


class TestReallocShrink:
    def test_data_copied_up_to_new_size(self, env):
        cpu, mem, state, stubs = env
        old_ptr = state.simple_alloc(8)
        for i in range(8):
            mem.write8(old_ptr + i, 0xA0 + i)

        mem.write32(cpu.regs[ESP] + 4, old_ptr)
        mem.write32(cpu.regs[ESP] + 8, 4)  # shrink to 4
        stubs.get("msvcrt.dll", "realloc")(cpu)
        new_ptr = cpu.regs[EAX]

        assert new_ptr != 0
        for i in range(4):
            assert mem.read8(new_ptr + i) == 0xA0 + i

    def test_new_allocation_tracked(self, env):
        cpu, mem, state, stubs = env
        old_ptr = state.simple_alloc(8)
        mem.write32(cpu.regs[ESP] + 4, old_ptr)
        mem.write32(cpu.regs[ESP] + 8, 4)
        stubs.get("msvcrt.dll", "realloc")(cpu)
        new_ptr = cpu.regs[EAX]
        assert state.heap_alloc_sizes.get(new_ptr) == 4


class TestReallocGrow:
    def test_all_old_bytes_copied(self, env):
        cpu, mem, state, stubs = env
        old_ptr = state.simple_alloc(4)
        for i in range(4):
            mem.write8(old_ptr + i, 0xBB)

        mem.write32(cpu.regs[ESP] + 4, old_ptr)
        mem.write32(cpu.regs[ESP] + 8, 16)  # grow
        stubs.get("msvcrt.dll", "realloc")(cpu)
        new_ptr = cpu.regs[EAX]

        for i in range(4):
            assert mem.read8(new_ptr + i) == 0xBB

    def test_returns_new_pointer(self, env):
        cpu, mem, state, stubs = env
        old_ptr = state.simple_alloc(4)
        mem.write32(cpu.regs[ESP] + 4, old_ptr)
        mem.write32(cpu.regs[ESP] + 8, 64)
        stubs.get("msvcrt.dll", "realloc")(cpu)
        assert cpu.regs[EAX] != 0
        assert cpu.regs[EAX] != old_ptr  # bump allocator always moves forward


class TestReallocSizeZero:
    def test_non_null_ptr_size_zero_returns_null(self, env):
        cpu, mem, state, stubs = env
        old_ptr = state.simple_alloc(8)
        mem.write32(cpu.regs[ESP] + 4, old_ptr)
        mem.write32(cpu.regs[ESP] + 8, 0)
        stubs.get("msvcrt.dll", "realloc")(cpu)
        assert cpu.regs[EAX] == 0


class TestReallocUnknownPtr:
    def test_unknown_ptr_copies_zero_bytes(self, env):
        """Pointer not in heap_alloc_sizes: old_size=0, copy_len=0, still returns new block."""
        cpu, mem, state, stubs = env
        mem.write32(cpu.regs[ESP] + 4, 0x12345678)  # untracked ptr
        mem.write32(cpu.regs[ESP] + 8, 32)
        stubs.get("msvcrt.dll", "realloc")(cpu)
        assert cpu.regs[EAX] != 0
        assert state.heap_alloc_sizes.get(cpu.regs[EAX]) == 32
