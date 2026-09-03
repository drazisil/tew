"""Regression guard: the fixed-address COM vtable/object region (0x00220000+,
shared by D3D8, DirectInput, and DirectSound) must never have two regions
overlap.

Historical bug: DI_DEV_VTABLE (tew/api/dinput_handlers.py) was extended from
18 to 26 slots to match the real IDirectInputDevice2A spec, growing its end
from 0x00220368 to 0x00220388 -- but DS_VTABLE (tew/api/dsound_handlers.py)
still started at the old boundary, 0x00220370, so DI_DEV_VTABLE's last 6
slots silently overlapped DS_VTABLE's first 6. Since dsound_handlers.py
registers after dinput_handlers.py, DS_VTABLE's writes clobbered
DI_DEV_VTABLE's trampolines at the shared addresses -- confirmed live: a
real DirectInput Poll() call (DI slot 25) landed on DS::DuplicateSoundBuffer
(DS slot 5) instead, surfacing as an "invalid this" halt that had nothing to
do with DirectSound at all. This test would have caught it.
"""
from __future__ import annotations

from tew.api.d3d8._layout import (
    D3D8_VTABLE, D3D8_OBJ, D3DDEV_VTABLE, D3DDEV_OBJ,
    D3DRES_VTABLE, D3DSURF_VTABLE, D3DTEX_VTABLE,
)
from tew.api.dinput_handlers import DI_VTABLE, DI_OBJ, DI_DEV_VTABLE
from tew.api.dsound_handlers import DS_VTABLE, DS_OBJ, DS_BUF_VTABLE

# (name, start, byte_size) for every fixed COM region in this address space.
_REGIONS: list[tuple[str, int, int]] = [
    ("D3D8_VTABLE",   D3D8_VTABLE,    16 * 4),
    ("D3D8_OBJ",      D3D8_OBJ,       4),
    ("D3DDEV_VTABLE", D3DDEV_VTABLE,  97 * 4),
    ("D3DDEV_OBJ",    D3DDEV_OBJ,     4),
    ("D3DRES_VTABLE", D3DRES_VTABLE,  14 * 4),
    ("D3DSURF_VTABLE", D3DSURF_VTABLE, 11 * 4),
    ("D3DTEX_VTABLE", D3DTEX_VTABLE,  18 * 4),
    ("DI_VTABLE",     DI_VTABLE,      9 * 4),
    ("DI_OBJ",        DI_OBJ,         4),
    ("DI_DEV_VTABLE", DI_DEV_VTABLE,  26 * 4),
    ("DS_VTABLE",     DS_VTABLE,      11 * 4),
    ("DS_OBJ",        DS_OBJ,         4),
    ("DS_BUF_VTABLE", DS_BUF_VTABLE,  21 * 4),
]


def test_no_two_fixed_com_regions_overlap():
    sorted_regions = sorted(_REGIONS, key=lambda r: r[1])
    for (name_a, start_a, size_a), (name_b, start_b, _size_b) in zip(
        sorted_regions, sorted_regions[1:]
    ):
        end_a = start_a + size_a
        assert end_a <= start_b, (
            f"{name_a} (0x{start_a:x}-0x{end_a:x}) overlaps "
            f"{name_b} (starts 0x{start_b:x})"
        )


def test_di_dev_vtable_ends_before_ds_vtable_starts():
    """Direct regression check for the exact bug: DI's growth must never
    reach into DS's fixed range again without DS being moved to match."""
    assert DI_DEV_VTABLE + 26 * 4 <= DS_VTABLE
