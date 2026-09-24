"""Callers whose contract promises zeroed memory must not get simple_alloc's
default 0xCD debug-heap fill.

Each test dirties a block, frees it, then makes the call under test so the
allocator hands the same (dirty) block back -- the case where "fresh bump
memory happens to be zero" cannot hide a missing zero-fill. Memory is
attached to the state so the fill actually runs (test fixtures that leave
`state.memory` unset skip filling entirely).
"""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.kernel32_io import register_kernel32_io_handlers
from tew.api.msvcrt_handlers import register_msvcrt_handlers
from tew.hardware.cpu_zig import EAX, ESP
from tew.hardware.memory import Memory

MEM_SIZE = 128 * 1024 * 1024  # must clear the heap base (simple_alloc starts at 0x04000000)
STACK = 0x200000
PAGE_READWRITE = 0x04
FILE_MAP_WRITE = 0x0002


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


@pytest.fixture
def env():
    mem = Memory(MEM_SIZE)
    state = CRTState()
    state.memory = mem
    stubs = _StubHandlers()
    register_msvcrt_handlers(stubs, mem, state)
    register_kernel32_io_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    return cpu, mem, state, stubs


def call(stubs, cpu, mem, dll, name, args):
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    for i, val in enumerate(args):
        mem.write32(STACK + 4 + i * 4, val & 0xFFFFFFFF)
    stubs.get(dll, name)(cpu)


def test_calloc_returns_zeroed_memory_on_a_reused_dirty_block(env):
    cpu, mem, state, stubs = env
    dirty = state.simple_alloc(64)
    mem.load(dirty, b"\xaa" * 64)
    state.simple_free(dirty)

    call(stubs, cpu, mem, "msvcrt.dll", "calloc", [8, 8])

    assert cpu.regs[EAX] == dirty, "test setup: calloc should have reused the freed block"
    assert mem.read_bytes(dirty, 64) == b"\x00" * 64


def test_malloc_keeps_the_debug_heap_fill(env):
    cpu, mem, state, stubs = env

    call(stubs, cpu, mem, "msvcrt.dll", "malloc", [32])

    assert mem.read_bytes(cpu.regs[EAX], 32) == b"\xcd" * 32


def test_anonymous_map_view_is_zero_on_a_reused_dirty_block(env):
    cpu, mem, state, stubs = env
    dirty = state.simple_alloc(4096)
    mem.load(dirty, b"\xaa" * 4096)
    state.simple_free(dirty)

    call(stubs, cpu, mem, "kernel32.dll", "CreateFileMappingA", [0xFFFFFFFF, 0, PAGE_READWRITE, 0, 4096, 0])
    h_map = cpu.regs[EAX]
    call(stubs, cpu, mem, "kernel32.dll", "MapViewOfFile", [h_map, FILE_MAP_WRITE, 0, 0, 4096])
    base = cpu.regs[EAX]

    assert base == dirty, "test setup: the view should have reused the freed block"
    assert mem.read_bytes(base, 4096) == b"\x00" * 4096
