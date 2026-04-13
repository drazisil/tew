"""Tests for tew.api.pe_resources — PE resource section parser.

These tests parse the real MCity_d.exe binary to verify that dialog 114
(the login dialog) is found and parsed correctly.  The emulator.json file
is used to locate the executable.

Tests that require MCity_d.exe are skipped automatically when the file is
not present (CI environments without the game assets).
"""

from __future__ import annotations

import json
import os
import struct

import pytest

from tew.api.pe_resources import (
    PEResources,
    DialogTemplate,
    DialogControl,
    _read_var_field,
    _align4,
    du_to_px_x,
    du_to_px_y,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _exe_path() -> str | None:
    """Return the path to MCity_d.exe from emulator.json, or None."""
    try:
        cfg_path = os.path.join(os.getcwd(), "emulator.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("exePath")
    except Exception:
        return None


def _load_exe() -> bytes | None:
    path = _exe_path()
    if path is None or not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


_EXE_DATA: bytes | None = _load_exe()

needs_exe = pytest.mark.skipif(
    _EXE_DATA is None,
    reason="MCity_d.exe not found — skipping live PE tests",
)


# ── Unit tests for pure helper functions (no file needed) ──────────────────────

class TestDialogUnitConversion:
    def test_du_x_zero(self):
        assert du_to_px_x(0) == 0

    def test_du_y_zero(self):
        assert du_to_px_y(0) == 0

    def test_du_x_dialog_width_248(self):
        # 248 dialog units wide at base_x=6: (248*6+2)//4 = 1490//4 = 372
        assert du_to_px_x(248) == 372

    def test_du_y_dialog_height_202(self):
        # (202*13+4)//8 = 2630//8 = 328
        assert du_to_px_y(202) == 328

    def test_du_x_proportional(self):
        # Larger value produces larger pixel result
        assert du_to_px_x(100) > du_to_px_x(50)

    def test_du_y_proportional(self):
        assert du_to_px_y(100) > du_to_px_y(50)

    def test_align4_already_aligned(self):
        assert _align4(0) == 0
        assert _align4(4) == 4
        assert _align4(100) == 100

    def test_align4_rounds_up(self):
        assert _align4(1) == 4
        assert _align4(2) == 4
        assert _align4(3) == 4
        assert _align4(5) == 8
        assert _align4(7) == 8


class TestReadVarField:
    def test_empty_string_on_double_zero(self):
        buf = b"\x00\x00"
        value, pos = _read_var_field(buf, 0)
        assert value == ""
        assert pos == 2

    def test_ordinal_on_ffff_word(self):
        buf = struct.pack("<HH", 0xFFFF, 130)
        value, pos = _read_var_field(buf, 0)
        assert value == "#130"
        assert pos == 4

    def test_utf16_string(self):
        # Encode "Hi" as UTF-16LE + null
        encoded = "Hi\x00".encode("utf-16-le")
        value, pos = _read_var_field(encoded, 0)
        assert value == "Hi"
        assert pos == 6   # 3 chars * 2 bytes each

    def test_buffer_too_short_returns_empty(self):
        buf = b"\x01"   # only 1 byte — cannot read a WORD
        value, pos = _read_var_field(buf, 0)
        assert value == ""
        assert pos == 0


# ── Integration tests against MCity_d.exe ─────────────────────────────────────

class TestPEResourcesInit:
    @needs_exe
    def test_parses_without_error(self):
        res = PEResources(_EXE_DATA)
        # If _valid is False the following dialog test will just return None —
        # but construction itself must not raise.
        assert res is not None

    def test_bad_magic_does_not_raise(self):
        res = PEResources(b"\x00" * 256)
        # Should handle gracefully; find_dialog returns None
        assert res.find_dialog(114) is None

    def test_empty_bytes_does_not_raise(self):
        res = PEResources(b"")
        assert res.find_dialog(114) is None


class TestFindDialog114:
    @needs_exe
    def test_dialog_114_found(self):
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None, "Dialog 114 must be present in MCity_d.exe"

    @needs_exe
    def test_dialog_114_title(self):
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None
        assert dlg.title == "Motor City Online Login"

    @needs_exe
    def test_dialog_114_dimensions(self):
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None
        assert dlg.cx == 248
        assert dlg.cy == 202

    @needs_exe
    def test_dialog_114_font(self):
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None
        assert dlg.font_pt == 8
        assert dlg.font_name == "MS Sans Serif"

    @needs_exe
    def test_dialog_114_has_ten_controls(self):
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None
        assert len(dlg.controls) == 10

    @needs_exe
    def test_dialog_114_username_edit(self):
        """Control 0x0412 is an EDIT control at (52, 100)."""
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None
        ctrl = _find_ctrl(dlg, 0x0412)
        assert ctrl is not None, "Username EDIT (id=0x0412) not found"
        assert ctrl.class_name == "EDIT"
        assert ctrl.x == 52
        assert ctrl.y == 100
        assert ctrl.cx == 141
        assert ctrl.cy == 14

    @needs_exe
    def test_dialog_114_password_edit(self):
        """Control 0x0411 is an EDIT control with ES_PASSWORD style."""
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None
        ctrl = _find_ctrl(dlg, 0x0411)
        assert ctrl is not None, "Password EDIT (id=0x0411) not found"
        assert ctrl.class_name == "EDIT"
        # ES_PASSWORD = 0x20
        assert ctrl.style & 0x20, "Password field must have ES_PASSWORD set"

    @needs_exe
    def test_dialog_114_remember_checkbox(self):
        """Control 0x0414 is an AUTOCHECKBOX button."""
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None
        ctrl = _find_ctrl(dlg, 0x0414)
        assert ctrl is not None, "Remember checkbox (id=0x0414) not found"
        assert ctrl.class_name == "BUTTON"
        assert ctrl.title == "Remember my password."

    @needs_exe
    def test_dialog_114_continue_button(self):
        """Control 0x0001 is the Continue button."""
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None
        ctrl = _find_ctrl(dlg, 0x0001)
        assert ctrl is not None, "Continue button (id=0x0001) not found"
        assert ctrl.class_name == "BUTTON"
        assert ctrl.title == "Continue"

    @needs_exe
    def test_dialog_114_cancel_button(self):
        """Control 0x0002 is the Cancel button."""
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None
        ctrl = _find_ctrl(dlg, 0x0002)
        assert ctrl is not None, "Cancel button (id=0x0002) not found"
        assert ctrl.class_name == "BUTTON"
        assert ctrl.title == "Cancel"

    @needs_exe
    def test_dialog_114_create_account_button(self):
        """Control 0x0415 is the Create Account button."""
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None
        ctrl = _find_ctrl(dlg, 0x0415)
        assert ctrl is not None, "Create Account button (id=0x0415) not found"
        assert ctrl.class_name == "BUTTON"
        assert ctrl.title == "Create Account"

    @needs_exe
    def test_dialog_114_static_controls(self):
        """STATIC labels should include the two field labels and the note."""
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None
        static_titles = {c.title for c in dlg.controls if c.class_name == "STATIC"}
        assert "Enter your password:" in static_titles
        assert "Enter your name:" in static_titles
        assert "NOTE: Name and password are case sensitive." in static_titles

    @needs_exe
    def test_dialog_114_all_class_names_resolved(self):
        """No control should have a raw '#NNN' ordinal as its class name."""
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None
        for ctrl in dlg.controls:
            assert ctrl.class_name in ("BUTTON", "EDIT", "STATIC", "LISTBOX",
                                       "SCROLLBAR", "COMBOBOX"), (
                f"Unexpected class_name '{ctrl.class_name}' for control id=0x{ctrl.id:04x}"
            )

    @needs_exe
    def test_dialog_114_returns_dialog_template_type(self):
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert isinstance(dlg, DialogTemplate)

    @needs_exe
    def test_dialog_114_controls_are_dialog_control_type(self):
        res = PEResources(_EXE_DATA)
        dlg = res.find_dialog(114)
        assert dlg is not None
        for ctrl in dlg.controls:
            assert isinstance(ctrl, DialogControl)


class TestFindDialogMissing:
    @needs_exe
    def test_nonexistent_dialog_id_returns_none(self):
        res = PEResources(_EXE_DATA)
        assert res.find_dialog(9999) is None

    @needs_exe
    def test_find_bitmap_missing_returns_none(self):
        res = PEResources(_EXE_DATA)
        assert res.find_bitmap(99999) is None


class TestFindBitmap:
    @needs_exe
    def test_find_bitmap_117(self):
        """Bitmap 117 is the MCO logo referenced by the #117 static control."""
        res = PEResources(_EXE_DATA)
        bmp = res.find_bitmap(117)
        # If bitmap 117 exists the result must be non-empty bytes
        if bmp is not None:
            assert isinstance(bmp, bytes)
            assert len(bmp) > 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_ctrl(dlg: DialogTemplate, ctrl_id: int) -> DialogControl | None:
    """Find a control in a dialog by its ID."""
    for ctrl in dlg.controls:
        if ctrl.id == ctrl_id:
            return ctrl
    return None
