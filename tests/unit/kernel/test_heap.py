"""
Tests for heap-management state invariants used by HeapCreate / HeapValidate.

HeapValidate has no extractable pure logic — the handler is a thin wrapper that
checks state.heap_handles and returns 1.  These tests verify the CRTState
invariants the handler relies on so that regressions in state setup are caught
before they manifest as emulator halts.
"""

import pytest
from tew.api._state import CRTState


@pytest.fixture
def state():
    return CRTState()


class TestHeapValidate:

    def test_process_heap_registered_at_init(self, state):
        """The process heap handle must be in heap_handles from the start."""
        assert state.process_heap in state.heap_handles

    def test_process_heap_handle_is_nonzero(self, state):
        """A zero handle would be ambiguous with NULL — must never happen."""
        assert state.process_heap != 0

    def test_created_heap_is_valid(self, state):
        """HeapCreate adds a handle; HeapValidate must see it as valid."""
        new_handle = state.next_heap_handle
        state.heap_handles.add(new_handle)
        assert new_handle in state.heap_handles

    def test_unknown_handle_is_invalid(self, state):
        """An arbitrary handle not registered by HeapCreate must not be valid."""
        assert 0xDEADBEEF not in state.heap_handles

    def test_multiple_heaps_all_valid(self, state):
        """Each heap created independently must be independently valid."""
        handles = []
        for _ in range(5):
            h = state.next_heap_handle
            state.next_heap_handle += 1
            state.heap_handles.add(h)
            handles.append(h)

        for h in handles:
            assert h in state.heap_handles

    def test_heap_handles_are_unique(self, state):
        """No two heap creation calls should produce the same handle."""
        before = state.next_heap_handle
        h1 = state.next_heap_handle;  state.next_heap_handle += 1
        h2 = state.next_heap_handle;  state.next_heap_handle += 1
        assert h1 != h2
        assert h1 >= before
        assert h2 > h1


class TestSimpleAllocFree:

    def test_freed_block_reused_by_same_size_alloc(self, state):
        """simple_free must return the block to circulation, not just drop it --
        a later simple_alloc of the same size should get the same address back
        instead of bumping the cursor further."""
        addr1 = state.simple_alloc(64)
        state.simple_free(addr1)
        addr2 = state.simple_alloc(64)
        assert addr2 == addr1

    def test_free_null_is_a_noop(self, state):
        """free(NULL)/HeapFree(..., NULL) is legitimate per spec -- must not raise."""
        state.simple_free(0)  # must not raise

    def test_free_untracked_pointer_raises(self, state):
        """A pointer simple_alloc never handed out (or a double free) is a real
        bug -- must raise loudly, not silently no-op."""
        with pytest.raises(RuntimeError):
            state.simple_free(0xDEADBEEF)

    def test_double_free_raises(self, state):
        addr = state.simple_alloc(64)
        state.simple_free(addr)
        with pytest.raises(RuntimeError):
            state.simple_free(addr)

    def test_freeing_larger_block_splits_remainder_back_into_free_list(self, state):
        """Allocating a smaller size than a freed block must not waste the
        rest of it -- the leftover has to come back as its own free block."""
        addr1 = state.simple_alloc(128)
        state.simple_free(addr1)
        addr2 = state.simple_alloc(32)
        assert addr2 == addr1
        # the remaining 96 bytes (at addr1+32) must still be reusable
        addr3 = state.simple_alloc(96)
        assert addr3 == addr1 + 32
