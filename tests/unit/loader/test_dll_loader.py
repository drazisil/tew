"""Tests for tew.loader.dll_loader — address mapping and search logic."""

import pytest
from tew.hardware.memory import Memory
from tew.loader.dll_loader import DLLLoader, apply_base_relocations


class TestAddressMapping:
    def test_no_mappings_initially(self):
        loader = DLLLoader()
        assert loader.get_address_mappings() == []

    def test_is_in_dll_range_empty(self):
        loader = DLLLoader()
        assert loader.is_in_dll_range(0x10000000) is False

    def test_find_dll_for_address_empty(self):
        loader = DLLLoader()
        assert loader.find_dll_for_address(0x10000000) is None

    def test_add_search_path(self):
        loader = DLLLoader(["/tmp"])
        paths = loader._search_paths
        assert "/tmp" in paths

    def test_add_search_path_no_duplicate(self):
        loader = DLLLoader(["/tmp"])
        loader.add_search_path("/tmp")
        assert loader._search_paths.count("/tmp") == 1


class TestApplyBaseRelocations:
    def test_no_reloc_when_same_base(self):
        mem = Memory(0x100000)
        mem.write32(0x1000, 0x400010)
        # base == preferred → no change
        apply_base_relocations(mem, [], 0x400000, 0x400000)
        assert mem.read32(0x1000) == 0x400010

    def test_highlow_reloc_applied(self):
        mem = Memory(0x200000)
        mem.write32(0x10000, 0x00401000)  # original absolute addr

        class FakeEntry:
            type = 3
            offset = 0

        class FakeBlock:
            page_rva = 0x0000
            entries = [FakeEntry()]

        apply_base_relocations(mem, [FakeBlock()], base_address=0x10000, preferred_base=0x00400000)
        # delta = 0x10000 - 0x400000 = -0x3F0000 (wraps), but let's check arithmetic
        delta = (0x10000 - 0x00400000) & 0xFFFFFFFF
        expected = (0x00401000 + delta) & 0xFFFFFFFF
        assert mem.read32(0x10000) == expected


class TestDLLLoaderNoFiles:
    def test_load_dll_missing_returns_none(self):
        mem = Memory(0x1000000)
        loader = DLLLoader(["/nonexistent_path_xyz"])
        result = loader.load_dll("missing.dll", mem)
        assert result is None

    def test_get_export_address_missing(self):
        loader = DLLLoader()
        assert loader.get_export_address("fake.dll", "FakeFunc") is None

    def test_get_dll_missing(self):
        loader = DLLLoader()
        assert loader.get_dll("fake.dll") is None
