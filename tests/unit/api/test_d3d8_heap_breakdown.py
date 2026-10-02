"""The D3D8 heap-exhaustion error must say what fills the heap."""
import pytest

from tew.api.d3d8 import _helpers


def test_exhaustion_error_includes_breakdown(monkeypatch):
    monkeypatch.setattr(_helpers, "_next_heap_addr", _helpers.D3D8_HEAP_LIMIT - 64)
    monkeypatch.setattr(_helpers, "_alloc_registry", {
        0x1000: {"kind": "surface", "obj_size": 24, "data_ptr": 0x2000, "data_size": 1920000},
        0x1100: {"kind": "surface", "obj_size": 24, "data_ptr": 0x3000, "data_size": 1920000},
    })
    monkeypatch.setattr(_helpers, "_free_lists", {("d3d8_surf_data", 4096): [0x5000, 0x6000]})
    with pytest.raises(RuntimeError) as exc:
        _helpers._heap_alloc(1920000, "d3d8_surf_data")
    msg = str(exc.value)
    assert "heap exhausted" in msg
    assert "live surface x2 @ 1920000 bytes each = 3840000 bytes" in msg
    assert "8192 bytes sitting on free lists" in msg


def test_breakdown_groups_surfaces_by_origin(monkeypatch):
    monkeypatch.setattr(_helpers, "_alloc_registry", {
        0x1000: {"kind": "surface", "obj_size": 24, "data_ptr": 0x2000, "data_size": 1920000,
                 "origin": "GetBackBuffer ret=0x00401234"},
        0x1100: {"kind": "surface", "obj_size": 24, "data_ptr": 0x3000, "data_size": 1920000,
                 "origin": "GetBackBuffer ret=0x00401234"},
        0x1200: {"kind": "surface", "obj_size": 24, "data_ptr": 0x4000, "data_size": 1920000,
                 "origin": "CreateImageSurface ret=0x00405678"},
    })
    monkeypatch.setattr(_helpers, "_free_lists", {})
    msg = _helpers._heap_breakdown()
    assert "live surface x2 @ 1920000 bytes each = 3840000 bytes from GetBackBuffer ret=0x00401234" in msg
    assert "live surface x1 @ 1920000 bytes each = 1920000 bytes from CreateImageSurface ret=0x00405678" in msg
