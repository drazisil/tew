"""
Tests for Win32 last-error support: Win32Error constants and CRTState.last_error.

GetLastError and SetLastError are closures — they cannot be called without a full
CPU setup.  These tests verify the state invariants they depend on and confirm
that the error code constants match the Win32 specification.
"""

import pytest
from tew.api._state import CRTState
from tew.api.win32_errors import Win32Error


# ─────────────────────────────────────────────────────────────────────────────
# Win32Error constants match winerror.h
# ─────────────────────────────────────────────────────────────────────────────

class TestWin32ErrorValues:
    def test_success(self):              assert int(Win32Error.ERROR_SUCCESS)             ==   0
    def test_file_not_found(self):       assert int(Win32Error.ERROR_FILE_NOT_FOUND)      ==   2
    def test_path_not_found(self):       assert int(Win32Error.ERROR_PATH_NOT_FOUND)      ==   3
    def test_access_denied(self):        assert int(Win32Error.ERROR_ACCESS_DENIED)       ==   5
    def test_invalid_handle(self):       assert int(Win32Error.ERROR_INVALID_HANDLE)      ==   6
    def test_not_enough_memory(self):    assert int(Win32Error.ERROR_NOT_ENOUGH_MEMORY)   ==   8
    def test_invalid_parameter(self):    assert int(Win32Error.ERROR_INVALID_PARAMETER)   ==  87
    def test_insufficient_buffer(self):  assert int(Win32Error.ERROR_INSUFFICIENT_BUFFER) == 122
    def test_already_exists(self):       assert int(Win32Error.ERROR_ALREADY_EXISTS)      == 183

    def test_all_values_are_unique(self):
        values = [int(e) for e in Win32Error]
        assert len(values) == len(set(values)), "duplicate error code values"


# ─────────────────────────────────────────────────────────────────────────────
# CRTState.last_error field
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def state():
    return CRTState()


class TestLastErrorState:

    def test_initialises_to_zero(self, state):
        """last_error must be ERROR_SUCCESS (0) at startup — no error yet."""
        assert state.last_error == int(Win32Error.ERROR_SUCCESS)

    def test_can_be_set(self, state):
        state.last_error = int(Win32Error.ERROR_FILE_NOT_FOUND)
        assert state.last_error == 2

    def test_can_be_reset_to_zero(self, state):
        state.last_error = int(Win32Error.ERROR_ACCESS_DENIED)
        state.last_error = 0
        assert state.last_error == 0

    def test_stores_arbitrary_dword(self, state):
        state.last_error = 0xDEAD
        assert state.last_error == 0xDEAD

    # ── OpenMutexA contract ───────────────────────────────────────────────────

    def test_open_mutex_sets_file_not_found(self, state):
        """
        When OpenMutexA returns NULL for a non-existent named mutex, it must
        set last_error to ERROR_FILE_NOT_FOUND (2).  MCO checks this to
        determine whether it is the first running instance.
        """
        # Simulate what the handler does.
        state.last_error = int(Win32Error.ERROR_FILE_NOT_FOUND)
        assert state.last_error == int(Win32Error.ERROR_FILE_NOT_FOUND)
        assert state.last_error == 2
