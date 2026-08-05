"""Tests for kernel32.dll heap and virtual-memory handlers."""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.kernel32_memory import register_kernel32_memory_handlers
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


MEM_SIZE = 128 * 1024 * 1024  # must exceed CRTState.next_heap_alloc (0x04000000)
STACK    = 0x200000
BUF      = 0x300000

INVALID_HEAP = 0xDEAD0000

HEAP_NO_SERIALIZE          = 0x00000001
HEAP_ZERO_MEMORY            = 0x00000008
HEAP_REALLOC_IN_PLACE_ONLY  = 0x00000010

MEM_COMMIT      = 0x00001000
MEM_RESERVE     = 0x00002000
MEM_DECOMMIT    = 0x00004000
MEM_RELEASE     = 0x00008000
PAGE_NOACCESS   = 0x01
PAGE_READWRITE  = 0x04
PAGE_SIZE       = 4096


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_kernel32_memory_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    return cpu, mem, state, stubs


def call(stubs, cpu, mem, name, args):
    """args: list of 32-bit values to write at STACK+4, STACK+8, ..."""
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    for i, val in enumerate(args):
        mem.write32(STACK + 4 + i * 4, val)
    stubs.get("kernel32.dll", name)(cpu)


# ── HeapCreate ─────────────────────────────────────────────────────────────────

class TestHeapCreate:

    def test_supported_flags_returns_new_handle(self, env):
        cpu, mem, state, stubs = env
        before = state.next_heap_handle
        call(stubs, cpu, mem, "HeapCreate", [0, 0, 0])
        assert cpu.regs[EAX] == before

    def test_no_serialize_flag_supported(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapCreate", [HEAP_NO_SERIALIZE, 0, 0])
        assert cpu.halted is False

    def test_next_heap_handle_incremented(self, env):
        cpu, mem, state, stubs = env
        before = state.next_heap_handle
        call(stubs, cpu, mem, "HeapCreate", [0, 0, 0])
        assert state.next_heap_handle == before + 1

    def test_handle_added_to_heap_handles(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapCreate", [0, 0, 0])
        assert cpu.regs[EAX] in state.heap_handles

    def test_unsupported_flag_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapCreate", [HEAP_ZERO_MEMORY, 0, 0])
        assert cpu.halted is True

    def test_unsupported_flag_does_not_add_handle(self, env):
        cpu, mem, state, stubs = env
        before = set(state.heap_handles)
        call(stubs, cpu, mem, "HeapCreate", [HEAP_ZERO_MEMORY, 0, 0])
        assert state.heap_handles == before


# ── GetProcessHeap ─────────────────────────────────────────────────────────────

class TestGetProcessHeap:

    def test_returns_process_heap(self, env):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        stubs.get("kernel32.dll", "GetProcessHeap")(cpu)
        assert cpu.regs[EAX] == state.process_heap

    def test_no_stdcall_cleanup(self, env):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        stubs.get("kernel32.dll", "GetProcessHeap")(cpu)
        assert cpu.regs[ESP] == STACK


# ── HeapAlloc ──────────────────────────────────────────────────────────────────

class TestHeapAlloc:

    def test_valid_alloc_returns_nonzero(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 64])
        assert cpu.regs[EAX] != 0

    def test_alloc_owner_tracked(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 64])
        addr = cpu.regs[EAX]
        assert state.heap_alloc_owner[addr] == state.process_heap

    def test_zero_byte_alloc_gets_one_byte(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 0])
        addr = cpu.regs[EAX]
        assert state.heap_alloc_sizes[addr] == 1

    def test_zero_memory_flag_zeroes_buffer(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, HEAP_ZERO_MEMORY, 32])
        addr = cpu.regs[EAX]
        for i in range(32):
            mem.write8(addr + i, 0xFF)  # dirty it first (allocator may reuse space)
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, HEAP_ZERO_MEMORY, 32])
        addr2 = cpu.regs[EAX]
        assert all(mem.read8(addr2 + i) == 0 for i in range(32))

    def test_invalid_heap_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [INVALID_HEAP, 0, 64])
        assert cpu.halted is True

    def test_unsupported_flag_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, HEAP_REALLOC_IN_PLACE_ONLY, 64])
        assert cpu.halted is True

    def test_stdcall_cleanup(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 64])
        assert cpu.regs[ESP] == STACK + 12


# ── HeapFree ───────────────────────────────────────────────────────────────────

class TestHeapFree:

    def test_null_pointer_is_noop_success(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapFree", [state.process_heap, 0, 0])
        assert cpu.regs[EAX] == 1
        assert cpu.halted is False

    def test_valid_pointer_returns_true(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 16])
        addr = cpu.regs[EAX]
        call(stubs, cpu, mem, "HeapFree", [state.process_heap, 0, addr])
        assert cpu.regs[EAX] == 1

    def test_valid_pointer_removed_from_tracking(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 16])
        addr = cpu.regs[EAX]
        call(stubs, cpu, mem, "HeapFree", [state.process_heap, 0, addr])
        assert addr not in state.heap_alloc_sizes
        assert addr not in state.heap_alloc_owner

    def test_untracked_pointer_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapFree", [state.process_heap, 0, 0x12345678])
        assert cpu.halted is True

    def test_invalid_heap_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapFree", [INVALID_HEAP, 0, 0])
        assert cpu.halted is True

    def test_unsupported_flag_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapFree", [state.process_heap, HEAP_ZERO_MEMORY, 0])
        assert cpu.halted is True


# ── HeapReAlloc ────────────────────────────────────────────────────────────────

class TestHeapReAlloc:

    def test_grow_preserves_data(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 4])
        addr = cpu.regs[EAX]
        for i in range(4):
            mem.write8(addr + i, 0xAA + i)
        call(stubs, cpu, mem, "HeapReAlloc", [state.process_heap, 0, addr, 16])
        new_addr = cpu.regs[EAX]
        assert [mem.read8(new_addr + i) for i in range(4)] == [0xAA, 0xAB, 0xAC, 0xAD]

    def test_grow_removes_old_pointer_from_tracking(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 4])
        addr = cpu.regs[EAX]
        call(stubs, cpu, mem, "HeapReAlloc", [state.process_heap, 0, addr, 16])
        assert addr not in state.heap_alloc_sizes
        assert addr not in state.heap_alloc_owner

    def test_shrink_copies_only_new_size(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 16])
        addr = cpu.regs[EAX]
        for i in range(16):
            mem.write8(addr + i, i)
        call(stubs, cpu, mem, "HeapReAlloc", [state.process_heap, 0, addr, 4])
        new_addr = cpu.regs[EAX]
        assert [mem.read8(new_addr + i) for i in range(4)] == [0, 1, 2, 3]
        assert state.heap_alloc_sizes[new_addr] == 4

    def test_realloc_in_place_only_returns_false(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 4])
        addr = cpu.regs[EAX]
        call(stubs, cpu, mem, "HeapReAlloc",
             [state.process_heap, HEAP_REALLOC_IN_PLACE_ONLY, addr, 16])
        assert cpu.regs[EAX] == 0

    def test_realloc_in_place_only_does_not_halt(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 4])
        addr = cpu.regs[EAX]
        call(stubs, cpu, mem, "HeapReAlloc",
             [state.process_heap, HEAP_REALLOC_IN_PLACE_ONLY, addr, 16])
        assert cpu.halted is False

    def test_zero_memory_zeroes_grown_tail(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 4])
        addr = cpu.regs[EAX]
        for i in range(4):
            mem.write8(addr + i, 0xFF)
        call(stubs, cpu, mem, "HeapReAlloc",
             [state.process_heap, HEAP_ZERO_MEMORY, addr, 8])
        new_addr = cpu.regs[EAX]
        assert [mem.read8(new_addr + i) for i in range(4, 8)] == [0, 0, 0, 0]

    def test_untracked_pointer_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapReAlloc", [state.process_heap, 0, 0x12345678, 16])
        assert cpu.halted is True

    def test_null_pointer_acts_as_fresh_alloc(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapReAlloc", [state.process_heap, 0, 0, 16])
        assert cpu.halted is False
        assert cpu.regs[EAX] != 0
        assert state.heap_alloc_sizes[cpu.regs[EAX]] == 16

    def test_invalid_heap_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapReAlloc", [INVALID_HEAP, 0, 0, 16])
        assert cpu.halted is True

    def test_unsupported_flag_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapReAlloc", [state.process_heap, 0x2, 0, 16])
        assert cpu.halted is True

    def test_stdcall_cleanup(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 4])
        addr = cpu.regs[EAX]
        call(stubs, cpu, mem, "HeapReAlloc", [state.process_heap, 0, addr, 16])
        assert cpu.regs[ESP] == STACK + 16


# ── HeapSize ───────────────────────────────────────────────────────────────────

class TestHeapSize:

    def test_tracked_pointer_returns_size(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapAlloc", [state.process_heap, 0, 40])
        addr = cpu.regs[EAX]
        call(stubs, cpu, mem, "HeapSize", [state.process_heap, 0, addr])
        assert cpu.regs[EAX] == 40

    def test_untracked_pointer_returns_sentinel(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapSize", [state.process_heap, 0, 0x12345678])
        assert cpu.regs[EAX] == 0xFFFFFFFF

    def test_invalid_heap_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapSize", [INVALID_HEAP, 0, 0])
        assert cpu.halted is True

    def test_unsupported_flag_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapSize", [state.process_heap, HEAP_ZERO_MEMORY, 0])
        assert cpu.halted is True


# ── HeapValidate ───────────────────────────────────────────────────────────────

class TestHeapValidate:

    def test_valid_heap_returns_true(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapValidate", [state.process_heap, 0, 0])
        assert cpu.regs[EAX] == 1

    def test_invalid_heap_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HeapValidate", [INVALID_HEAP, 0, 0])
        assert cpu.halted is True


# ── VirtualAlloc ───────────────────────────────────────────────────────────────

class TestVirtualAlloc:

    def test_commit_only_null_addr_auto_reserves(self, env):
        cpu, mem, state, stubs = env
        cursor_before = state.next_virtual_alloc
        call(stubs, cpu, mem, "VirtualAlloc", [0, 1, MEM_COMMIT, PAGE_READWRITE])
        addr = cpu.regs[EAX]
        assert addr == cursor_before
        assert state.virtual_reserved[addr] == PAGE_SIZE
        assert state.virtual_committed[addr] == PAGE_SIZE

    def test_commit_only_null_addr_advances_cursor(self, env):
        cpu, mem, state, stubs = env
        cursor_before = state.next_virtual_alloc
        call(stubs, cpu, mem, "VirtualAlloc", [0, 1, MEM_COMMIT, PAGE_READWRITE])
        assert state.next_virtual_alloc == cursor_before + PAGE_SIZE

    def test_commit_on_existing_reserved_region(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0, 8192, MEM_RESERVE, PAGE_READWRITE])
        base = cpu.regs[EAX]
        cursor_after_reserve = state.next_virtual_alloc
        call(stubs, cpu, mem, "VirtualAlloc", [base, 1, MEM_COMMIT, PAGE_READWRITE])
        assert cpu.regs[EAX] == base
        assert state.virtual_committed[base] == PAGE_SIZE
        assert state.next_virtual_alloc == cursor_after_reserve

    def test_commit_on_unreserved_address_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0x99990000, 1, MEM_COMMIT, PAGE_READWRITE])
        assert cpu.halted is True

    def test_reserve_only_null_addr_allocates_at_cursor(self, env):
        cpu, mem, state, stubs = env
        cursor_before = state.next_virtual_alloc
        call(stubs, cpu, mem, "VirtualAlloc", [0, 1, MEM_RESERVE, PAGE_READWRITE])
        assert cpu.regs[EAX] == cursor_before
        assert state.virtual_reserved[cursor_before] == PAGE_SIZE
        assert cursor_before not in state.virtual_committed

    def test_reserve_and_commit_together(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0, 1, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE])
        addr = cpu.regs[EAX]
        assert state.virtual_reserved[addr] == PAGE_SIZE
        assert state.virtual_committed[addr] == PAGE_SIZE

    def test_reserve_explicit_addr_beyond_cursor_pulls_cursor_forward(self, env):
        cpu, mem, state, stubs = env
        explicit_addr = state.next_virtual_alloc + 0x10000000
        call(stubs, cpu, mem, "VirtualAlloc", [explicit_addr, 1, MEM_RESERVE, PAGE_READWRITE])
        assert cpu.regs[EAX] == explicit_addr
        assert state.virtual_reserved[explicit_addr] == PAGE_SIZE
        assert state.next_virtual_alloc == explicit_addr + PAGE_SIZE

    def test_unsupported_alloc_type_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0, 1, 0x00100000, PAGE_READWRITE])  # MEM_TOP_DOWN
        assert cpu.halted is True

    def test_unsupported_protect_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0, 1, MEM_RESERVE, 0x02])  # PAGE_READONLY
        assert cpu.halted is True

    def test_page_size_rounding(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0, 1, MEM_RESERVE, PAGE_READWRITE])
        addr = cpu.regs[EAX]
        assert state.virtual_reserved[addr] == PAGE_SIZE

    def test_stdcall_cleanup(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0, 1, MEM_RESERVE, PAGE_READWRITE])
        assert cpu.regs[ESP] == STACK + 16

    def test_explicit_addr_within_existing_range_does_not_advance_cursor(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0, 8192, MEM_RESERVE, PAGE_READWRITE])
        base = cpu.regs[EAX]
        cursor_after_first = state.next_virtual_alloc
        call(stubs, cpu, mem, "VirtualAlloc", [base, 1, MEM_RESERVE, PAGE_READWRITE])
        assert state.next_virtual_alloc == cursor_after_first

    def test_no_alloc_type_bits_set_records_nothing(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0, 1, 0, PAGE_READWRITE])
        addr = cpu.regs[EAX]
        assert addr not in state.virtual_reserved
        assert addr not in state.virtual_committed


# ── VirtualFree ────────────────────────────────────────────────────────────────

class TestVirtualFree:

    def test_release_with_nonzero_size_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0, 4096, MEM_RESERVE, PAGE_READWRITE])
        addr = cpu.regs[EAX]
        call(stubs, cpu, mem, "VirtualFree", [addr, 4096, MEM_RELEASE])
        assert cpu.halted is True

    def test_release_on_unreserved_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualFree", [0x12345000, 0, MEM_RELEASE])
        assert cpu.halted is True

    def test_release_removes_from_both_dicts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0, 4096, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE])
        addr = cpu.regs[EAX]
        call(stubs, cpu, mem, "VirtualFree", [addr, 0, MEM_RELEASE])
        assert addr not in state.virtual_reserved
        assert addr not in state.virtual_committed

    def test_decommit_removes_from_committed_only(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0, 4096, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE])
        addr = cpu.regs[EAX]
        call(stubs, cpu, mem, "VirtualFree", [addr, 4096, MEM_DECOMMIT])
        assert addr not in state.virtual_committed
        assert addr in state.virtual_reserved

    def test_unsupported_type_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0, 4096, MEM_RESERVE, PAGE_READWRITE])
        addr = cpu.regs[EAX]
        call(stubs, cpu, mem, "VirtualFree", [addr, 0, 0x1234])
        assert cpu.halted is True

    def test_release_returns_true_and_cleans_up(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "VirtualAlloc", [0, 4096, MEM_RESERVE, PAGE_READWRITE])
        addr = cpu.regs[EAX]
        call(stubs, cpu, mem, "VirtualFree", [addr, 0, MEM_RELEASE])
        assert cpu.regs[EAX] == 1
        assert cpu.regs[ESP] == STACK + 12
