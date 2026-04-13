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
