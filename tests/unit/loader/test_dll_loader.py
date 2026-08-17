"""Tests for tew.loader.dll_loader — address mapping and search logic."""

import pytest
from tew.hardware.memory import Memory
from tew.loader.dll_loader import DLLLoader, LoadedDLL, apply_base_relocations, should_invoke_dependency_dllmain


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


class TestShouldInvokeDependencyDllmain:
    """Regression coverage for the dependency-DllMain fix: a DLL loaded only
    as another DLL's PE-import dependency (e.g. msjint35.dll pulled in by
    msjter35.dll) previously never ran its own DllMain at all, so CRT
    startup globals like "my own HINSTANCE" stayed at their zero default.
    """

    def _dll(self, entry_point: int = 0x1234) -> LoadedDLL:
        return LoadedDLL(name="dep.dll", base_address=0x19000000, size=0x1000000, entry_point=entry_point)

    def test_fresh_dependency_with_entry_point_and_callback(self):
        assert should_invoke_dependency_dllmain(False, self._dll(), lambda dep: None) is True

    def test_already_loaded_dependency_does_not_fire_again(self):
        """Two DLLs sharing a dependency must not double-invoke its DllMain."""
        assert should_invoke_dependency_dllmain(True, self._dll(), lambda dep: None) is False

    def test_no_callback_supplied_is_unchanged_behavior(self):
        """Startup-time static-import loading never passes a callback --
        must stay a pure no-op, identical to pre-fix behavior."""
        assert should_invoke_dependency_dllmain(False, self._dll(), None) is False

    def test_failed_load_does_not_fire(self):
        assert should_invoke_dependency_dllmain(False, None, lambda dep: None) is False

    def test_dependency_with_no_entry_point_does_not_fire(self):
        """Pure resource/data-only DLLs have no real entry point to call."""
        assert should_invoke_dependency_dllmain(False, self._dll(entry_point=0), lambda dep: None) is False


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


class TestLoadDllPropagatesFatalHalt:
    """load_dll's on_dependency_loaded callback can run arbitrary guest code
    (a dependency's real DllMain, via a nested cpu.run()). If something live
    in the emulator -- on ANY thread, not necessarily this DLL's own code --
    hits a genuine fatal_halt condition during that window, load_dll's own
    exception handling must not downgrade it to an ordinary "DLL failed to
    load" warning: fatal_halt means the whole emulator session must stop.
    Live-verified regression (2026-08-16): a FatalHaltError raised deep
    inside a dependency-DllMain call for MSJINT35.dll got caught here and
    logged as "Failed to load MSJTER35.DLL: fatal halt..." even though both
    DLLs had already mapped and cached successfully -- silently swallowing a
    fatal condition and letting the caller (LoadLibraryA) continue as if the
    DLL just wasn't found.
    """

    def _raise_fatal_halt(self, *args, **kwargs):
        from tew.hardware.cpu_zig import FatalHaltError
        raise FatalHaltError("fatal halt at EIP=0xdeadbeef")

    def _raise_value_error(self, *args, **kwargs):
        raise ValueError("corrupt PE")

    def test_fatal_halt_error_propagates_not_swallowed(self, monkeypatch):
        from tew.hardware.cpu_zig import FatalHaltError
        loader = DLLLoader()
        monkeypatch.setattr(loader, "find_dll_file", lambda name: "/fake/path.dll")
        monkeypatch.setattr("tew.pe.exe_file.EXEFile", self._raise_fatal_halt)
        mem = Memory(0x1000000)

        with pytest.raises(FatalHaltError):
            loader.load_dll("FAKE.DLL", mem)

    def test_ordinary_exception_still_returns_none(self, monkeypatch):
        """Regression: genuine PE-parsing failures (corrupt file, etc.) must
        still be caught and reported as a normal load failure, not raised."""
        loader = DLLLoader()
        monkeypatch.setattr(loader, "find_dll_file", lambda name: "/fake/path.dll")
        monkeypatch.setattr("tew.pe.exe_file.EXEFile", self._raise_value_error)
        mem = Memory(0x1000000)

        result = loader.load_dll("FAKE.DLL", mem)

        assert result is None
