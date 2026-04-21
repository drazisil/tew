"""Tests for tew.kernel.kernel_structures.KernelStructures."""

import pytest
from tew.hardware.memory import Memory
from tew.kernel.kernel_structures import KernelStructures


@pytest.fixture
def ks():
    mem = Memory(0x1000000)
    k = KernelStructures(mem)
    k.initialize_kernel_structures(stack_base=0x00200000, stack_limit=0x001F0000)
    return k, mem


@pytest.fixture
def ks_with_heap():
    mem = Memory(0x1000000)
    k = KernelStructures(mem)
    k.initialize_kernel_structures(
        stack_base=0x00200000, stack_limit=0x001F0000, process_heap=0x9000
    )
    return k, mem


class TestTEBLayout:
    def test_teb_at_0x00320000(self, ks):
        k, mem = ks
        teb = k.get_teb()
        assert teb is not None
        assert teb.base_address == 0x00320000

    def test_exception_list_sentinel(self, ks):
        _, mem = ks
        # ExceptionList at TEB+0x00 should be 0xFFFFFFFF (no handler)
        assert mem.read32(0x00320000) == 0xFFFFFFFF

    def test_stack_base_written(self, ks):
        _, mem = ks
        # StackBase at TEB+0x04
        assert mem.read32(0x00320004) == 0x00200000

    def test_stack_limit_written(self, ks):
        _, mem = ks
        # StackLimit at TEB+0x08
        assert mem.read32(0x00320008) == 0x001F0000

    def test_self_pointer(self, ks):
        _, mem = ks
        # Self pointer at TEB+0x18
        assert mem.read32(0x00320018) == 0x00320000

    def test_peb_pointer(self, ks):
        _, mem = ks
        # PEB at TEB+0x30
        assert mem.read32(0x00320030) == 0x00300000


class TestPEBLayout:
    def test_peb_at_0x00300000(self, ks):
        k, _ = ks
        peb = k.get_peb()
        assert peb is not None
        assert peb.base_address == 0x00300000

    def test_process_heap_written(self, ks_with_heap):
        _, mem = ks_with_heap
        # ProcessHeap at PEB+0x18
        assert mem.read32(0x00300018) == 0x9000

    def test_process_heap_default_zero(self, ks):
        _, mem = ks
        assert mem.read32(0x00300018) == 0


class TestFSBase:
    def test_fs_base_is_teb(self, ks):
        k, _ = ks
        assert k.get_fs_base() == 0x00320000

    def test_resolve_fs_offset(self, ks):
        k, _ = ks
        # FS:[0x30] should map to 0x00320030
        assert k.resolve_fs_relative_address(0x30) == 0x00320030

    def test_gs_base_mirrors_fs(self, ks):
        k, _ = ks
        assert k.get_gs_base() == k.get_fs_base()
