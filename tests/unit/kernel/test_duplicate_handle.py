"""
Tests for DuplicateHandle handle-table logic.

DuplicateHandle is a closure inside register_kernel32_io_handlers; it cannot
be exercised here without a full CPU/Memory setup.  These tests instead verify
_duplicate_handle_entry — the module-level pure-state helper that the handler
delegates to — covering every source-handle category the emulator recognises.
"""

import pytest

from tew.api._state import CRTState, FileHandleEntry, MutexHandle, EventHandle
from tew.api.kernel32_io import _duplicate_handle_entry


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def state() -> CRTState:
    return CRTState()


def _file_entry(path: str = "/tmp/x", data: bytes = b"") -> FileHandleEntry:
    return FileHandleEntry(path=path, data=data, position=0, writable=False, fd=None)


# ─────────────────────────────────────────────────────────────────────────────
# File handles
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateFileHandle:
    # Use a handle number strictly below the counter's starting value (0x5000)
    # so that the duplicate always gets a different number.
    SOURCE_H = 0x4FFF

    def test_returns_new_handle_number(self, state: CRTState) -> None:
        state.file_handle_map[self.SOURCE_H] = _file_entry()
        new_h = _duplicate_handle_entry(state, self.SOURCE_H, close_source=False)
        assert new_h != self.SOURCE_H

    def test_new_handle_is_registered(self, state: CRTState) -> None:
        state.file_handle_map[self.SOURCE_H] = _file_entry()
        new_h = _duplicate_handle_entry(state, self.SOURCE_H, close_source=False)
        assert new_h in state.file_handle_map

    def test_new_handle_shares_same_entry_object(self, state: CRTState) -> None:
        """Duplicated file handles must share file position (Win32 semantics)."""
        entry = _file_entry(data=b"hello")
        state.file_handle_map[self.SOURCE_H] = entry
        new_h = _duplicate_handle_entry(state, self.SOURCE_H, close_source=False)
        assert state.file_handle_map[new_h] is entry

    def test_source_persists_when_close_source_is_false(self, state: CRTState) -> None:
        state.file_handle_map[self.SOURCE_H] = _file_entry()
        _duplicate_handle_entry(state, self.SOURCE_H, close_source=False)
        assert self.SOURCE_H in state.file_handle_map

    def test_close_source_removes_original(self, state: CRTState) -> None:
        state.file_handle_map[self.SOURCE_H] = _file_entry()
        _duplicate_handle_entry(state, self.SOURCE_H, close_source=True)
        assert self.SOURCE_H not in state.file_handle_map

    def test_close_source_preserves_duplicate(self, state: CRTState) -> None:
        state.file_handle_map[self.SOURCE_H] = _file_entry()
        new_h = _duplicate_handle_entry(state, self.SOURCE_H, close_source=True)
        assert new_h in state.file_handle_map

    def test_file_handle_counter_advances(self, state: CRTState) -> None:
        state.file_handle_map[self.SOURCE_H] = _file_entry()
        before = state.next_file_handle
        new_h = _duplicate_handle_entry(state, self.SOURCE_H, close_source=False)
        assert new_h == before
        assert state.next_file_handle == before + 1


# ─────────────────────────────────────────────────────────────────────────────
# Kernel object handles (mutex, event)
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateKernelHandle:

    def test_duplicate_event_returns_new_handle(self, state: CRTState) -> None:
        state.kernel_handle_map[0x7001] = EventHandle()
        new_h = _duplicate_handle_entry(state, 0x7001, close_source=False)
        assert new_h != 0x7001

    def test_duplicate_event_registered(self, state: CRTState) -> None:
        state.kernel_handle_map[0x7001] = EventHandle()
        new_h = _duplicate_handle_entry(state, 0x7001, close_source=False)
        assert new_h in state.kernel_handle_map

    def test_duplicate_event_shares_object(self, state: CRTState) -> None:
        """Duplicated event handles must reflect the same signaled state."""
        event = EventHandle(signaled=False, manual_reset=True)
        state.kernel_handle_map[0x7001] = event
        new_h = _duplicate_handle_entry(state, 0x7001, close_source=False)
        assert state.kernel_handle_map[new_h] is event

    def test_duplicate_mutex_shares_object(self, state: CRTState) -> None:
        mutex = MutexHandle(locked=True)
        state.kernel_handle_map[0x7002] = mutex
        new_h = _duplicate_handle_entry(state, 0x7002, close_source=False)
        assert state.kernel_handle_map[new_h] is mutex

    def test_close_source_removes_original_kernel_handle(self, state: CRTState) -> None:
        state.kernel_handle_map[0x7003] = EventHandle()
        _duplicate_handle_entry(state, 0x7003, close_source=True)
        assert 0x7003 not in state.kernel_handle_map

    def test_kernel_handle_counter_advances(self, state: CRTState) -> None:
        state.kernel_handle_map[0x7001] = EventHandle()
        before = state.next_kernel_handle
        new_h = _duplicate_handle_entry(state, 0x7001, close_source=False)
        assert new_h == before
        assert state.next_kernel_handle == before + 1


# ─────────────────────────────────────────────────────────────────────────────
# Pseudo-handles (GetCurrentProcess / GetCurrentThread)
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicatePseudoHandle:

    def test_process_pseudo_handle_returns_real_handle(self, state: CRTState) -> None:
        new_h = _duplicate_handle_entry(state, 0xFFFFFFFF, close_source=False)
        assert new_h in state.kernel_handle_map

    def test_thread_pseudo_handle_returns_real_handle(self, state: CRTState) -> None:
        new_h = _duplicate_handle_entry(state, 0xFFFFFFFE, close_source=False)
        assert new_h in state.kernel_handle_map

    def test_pseudo_handle_result_is_event_type(self, state: CRTState) -> None:
        """Pseudo-handles are wrapped in EventHandle so WaitForSingleObject works."""
        new_h = _duplicate_handle_entry(state, 0xFFFFFFFF, close_source=False)
        assert isinstance(state.kernel_handle_map[new_h], EventHandle)

    def test_two_duplicates_of_process_handle_are_unique(self, state: CRTState) -> None:
        h1 = _duplicate_handle_entry(state, 0xFFFFFFFF, close_source=False)
        h2 = _duplicate_handle_entry(state, 0xFFFFFFFF, close_source=False)
        assert h1 != h2

    def test_process_and_thread_pseudo_handles_produce_unique_results(self, state: CRTState) -> None:
        h1 = _duplicate_handle_entry(state, 0xFFFFFFFF, close_source=False)
        h2 = _duplicate_handle_entry(state, 0xFFFFFFFE, close_source=False)
        assert h1 != h2


# ─────────────────────────────────────────────────────────────────────────────
# Unknown handles (thread handles stored only in pending_threads, etc.)
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateUnknownHandle:

    def test_unknown_handle_returns_registered_handle(self, state: CRTState) -> None:
        """Thread handles are not in any lookup table; result must be closeable."""
        new_h = _duplicate_handle_entry(state, 0xBEEF, close_source=False)
        assert new_h in state.kernel_handle_map

    def test_unknown_handle_registered_as_event(self, state: CRTState) -> None:
        """Dummy must be an EventHandle so WaitForSingleObject sees it as signaled."""
        new_h = _duplicate_handle_entry(state, 0xBEEF, close_source=False)
        assert isinstance(state.kernel_handle_map[new_h], EventHandle)

    def test_close_source_on_unknown_does_not_raise(self, state: CRTState) -> None:
        """DUPLICATE_CLOSE_SOURCE on an untracked handle must not throw."""
        new_h = _duplicate_handle_entry(state, 0xBEEF, close_source=True)
        assert new_h in state.kernel_handle_map

    def test_two_unknown_handles_produce_unique_results(self, state: CRTState) -> None:
        h1 = _duplicate_handle_entry(state, 0xBEEF, close_source=False)
        h2 = _duplicate_handle_entry(state, 0xBEF0, close_source=False)
        assert h1 != h2
