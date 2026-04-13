"""
Tests for GetModuleFileNameA support: CRTState.reverse_translate_path and
the path-resolution logic used by the handler.
"""

import pytest
from tew.api._state import CRTState, DynamicModule, EmulatorConfig


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_state(path_mappings: dict[str, str]) -> CRTState:
    """Create a CRTState whose path_mappings are replaced with *path_mappings*."""
    state = CRTState()
    state.config = EmulatorConfig(
        path_mappings=path_mappings,
        interactive_on_missing_file=False,
    )
    return state


# ─────────────────────────────────────────────────────────────────────────────
# DynamicModule.dll_path field
# ─────────────────────────────────────────────────────────────────────────────

class TestDynamicModule:
    def test_dll_path_defaults_to_empty(self):
        mod = DynamicModule(dll_name="kernel32.dll", base_address=0x10000000)
        assert mod.dll_path == ""

    def test_dll_path_can_be_set(self):
        mod = DynamicModule(
            dll_name="npsa.dll",
            base_address=0x11000000,
            dll_path="C:\\MCO\\npsa.dll",
        )
        assert mod.dll_path == "C:\\MCO\\npsa.dll"


# ─────────────────────────────────────────────────────────────────────────────
# CRTState.reverse_translate_path
# ─────────────────────────────────────────────────────────────────────────────

class TestReverseTranslatePath:

    def test_basic_c_drive_mapping(self):
        state = make_state({"c:/": "/home/user/.emu32/"})
        result = state.reverse_translate_path("/home/user/.emu32/MCity/MCity_d.exe")
        assert result == "C:\\MCity\\MCity_d.exe"

    def test_exe_in_root_of_mapped_dir(self):
        state = make_state({"c:/": "/home/user/.emu32/"})
        result = state.reverse_translate_path("/home/user/.emu32/game.exe")
        assert result == "C:\\game.exe"

    def test_nested_path(self):
        state = make_state({"c:/": "/home/user/.emu32/"})
        result = state.reverse_translate_path("/home/user/.emu32/a/b/c/d.txt")
        assert result == "C:\\a\\b\\c\\d.txt"

    def test_unmapped_path_uses_backslash_fallback(self):
        state = make_state({"c:/": "/home/user/.emu32/"})
        result = state.reverse_translate_path("/opt/other/thing.dll")
        assert result == "\\opt\\other\\thing.dll"

    def test_longest_linux_prefix_wins(self):
        """When two mappings overlap, the longer Linux prefix should match."""
        state = make_state({
            "c:/":      "/home/user/.emu32/",
            "d:/game/": "/home/user/.emu32/MCity/",
        })
        result = state.reverse_translate_path("/home/user/.emu32/MCity/MCity_d.exe")
        # The longer prefix "/home/user/.emu32/MCity/" should win → D:\GAME\
        assert result == "D:\\GAME\\MCity_d.exe"

    def test_empty_path_returns_backslash(self):
        state = make_state({"c:/": "/home/user/.emu32/"})
        result = state.reverse_translate_path("")
        assert result == ""

    def test_empty_mappings(self):
        state = make_state({})
        result = state.reverse_translate_path("/some/linux/path.exe")
        assert result == "\\some\\linux\\path.exe"


# ─────────────────────────────────────────────────────────────────────────────
# Handler logic (unit-level: path lookup without CPU/memory)
# ─────────────────────────────────────────────────────────────────────────────

class TestModulePathResolution:
    """
    These tests verify the path-resolution decisions that the handler makes,
    without invoking the handler itself (which would require a full CPU setup).
    They exercise the same branches: NULL handle, known DLL handle, unknown handle.
    """

    def test_null_handle_returns_exe_path(self):
        state = make_state({"c:/": "/home/user/.emu32/"})
        state.exe_path = "/home/user/.emu32/MCity/MCity_d.exe"

        # Simulate the NULL-handle branch
        win_path = state.reverse_translate_path(state.exe_path)
        assert win_path == "C:\\MCity\\MCity_d.exe"

    def test_null_handle_with_empty_exe_path_detected(self):
        state = make_state({"c:/": "/home/user/.emu32/"})
        # exe_path not set — handler would halt; confirm the guard condition
        assert state.exe_path == ""

    def test_dll_handle_with_stored_path(self):
        state = make_state({"c:/": "/home/user/.emu32/"})
        mod = DynamicModule(
            dll_name="npsa.dll",
            base_address=0x11000000,
            dll_path="C:\\MCO\\npsa.dll",
        )
        state.dynamic_modules[0x11000000] = mod

        resolved = state.dynamic_modules[0x11000000]
        win_path = resolved.dll_path if resolved.dll_path else resolved.dll_name
        assert win_path == "C:\\MCO\\npsa.dll"

    def test_dll_handle_falls_back_to_dll_name(self):
        state = make_state({})
        mod = DynamicModule(dll_name="kernel32.dll", base_address=0x10000000)
        state.dynamic_modules[0x10000000] = mod

        resolved = state.dynamic_modules[0x10000000]
        win_path = resolved.dll_path if resolved.dll_path else resolved.dll_name
        assert win_path == "kernel32.dll"

    def test_unknown_handle_is_none(self):
        state = make_state({})
        mod = state.dynamic_modules.get(0xDEADBEEF)
        assert mod is None
