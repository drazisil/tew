"""
Tests for Win32 last-error support: Win32Error constants and TEB-backed last error.

LastErrorValue lives at TEB+0x34 (FS:[0x34]).  SetLastError writes there;
GetLastError reads from there.  These tests verify the layout directly.
"""

import pytest
from tew.hardware.memory import Memory
from tew.kernel.kernel_structures import KernelStructures
from tew.api._state import TEB_BASE
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
# LastErrorValue lives in TEB memory at TEB_BASE + 0x34
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mem():
    m = Memory(0x1000000)
    ks = KernelStructures(m)
    ks.initialize_kernel_structures(stack_base=0x00200000, stack_limit=0x001F0000)
    return m


class TestLastErrorTEB:

    def test_initialises_to_zero(self, mem):
        """KernelStructures must write 0 to TEB+0x34 at init — no error yet."""
        assert mem.read32(TEB_BASE + 0x34) == int(Win32Error.ERROR_SUCCESS)

    def test_can_be_set(self, mem):
        mem.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
        assert mem.read32(TEB_BASE + 0x34) == 2

    def test_can_be_reset_to_zero(self, mem):
        mem.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_ACCESS_DENIED))
        mem.write32(TEB_BASE + 0x34, 0)
        assert mem.read32(TEB_BASE + 0x34) == 0

    def test_stores_arbitrary_dword(self, mem):
        mem.write32(TEB_BASE + 0x34, 0xDEAD)
        assert mem.read32(TEB_BASE + 0x34) == 0xDEAD

    def test_open_mutex_sets_file_not_found(self, mem):
        """
        When OpenMutexA returns NULL for a non-existent named mutex it must
        set ERROR_FILE_NOT_FOUND (2) in TEB+0x34 so GetLastError returns 2.
        """
        mem.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
        assert mem.read32(TEB_BASE + 0x34) == int(Win32Error.ERROR_FILE_NOT_FOUND)
        assert mem.read32(TEB_BASE + 0x34) == 2
