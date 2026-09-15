"""Regression test for IDirect3DSurface8::LockRect ignoring the pRect
sub-rectangle parameter (2026-09-05 fix).

Real D3D8 apps stream/decode large images in tiles via repeated
Lock(pRect)/Unlock cycles on different sub-rectangles of the same surface.
The old `Surface::LockRect` always handed back a pointer to the surface's
absolute origin (0,0) regardless of which sub-rectangle the game actually
requested, so every tile's real pixel data landed at buffer offset 0
instead of its real (left, top) position. Confirmed live: a background
image built from 8 separate Lock/Unlock cycles on the same surface
rendered as a blocky mosaic instead of a complete image.

Fixed by computing pBits = data_ptr + top*pitch + left*bpp from pRect.
"""
from __future__ import annotations

import pytest

from tew.api.d3d8.idirect3d8surface import make_vtable
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP

_OBJ_DATA   = 4
_OBJ_WIDTH  = 12
_OBJ_HEIGHT = 16
_OBJ_FORMAT = 20

D3DFMT_X8R8G8B8 = 0x16  # 4 bytes per pixel


class _StubHandlers:
    def __init__(self):
        self._h: dict = {}

    def register_handler(self, dll, name, fn):
        self._h[f"{dll.lower()}!{name}"] = fn

    def get(self, dll, name):
        return self._h[f"{dll.lower()}!{name}"]

    def get_handler_address(self, dll, name):
        return 0


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False


MEM_SIZE = 8 * 1024 * 1024
STACK    = 0x100000
OBJ      = 0x200000
DATA_PTR = 0x210000
WIDTH    = 256
HEIGHT   = 256


@pytest.fixture
def env():
    mem = Memory(MEM_SIZE)
    cpu = _FakeCPU()
    stubs = _StubHandlers()
    make_vtable(stubs, mem)
    mem.write32(OBJ + _OBJ_DATA,   DATA_PTR)
    mem.write32(OBJ + _OBJ_WIDTH,  WIDTH)
    mem.write32(OBJ + _OBJ_HEIGHT, HEIGHT)
    mem.write32(OBJ + _OBJ_FORMAT, D3DFMT_X8R8G8B8)
    return cpu, mem, stubs


def lock_rect(cpu, mem, stubs, rect: tuple[int, int, int, int] | None):
    """rect = (left, top, right, bottom), or None for a null pRect.
    Returns (pitch, pbits)."""
    p_locked = STACK + 100
    p_rect = 0
    if rect is not None:
        p_rect = STACK + 200
        left, top, right, bottom = rect
        mem.write32(p_rect + 0, left)
        mem.write32(p_rect + 4, top)
        mem.write32(p_rect + 8, right)
        mem.write32(p_rect + 12, bottom)
    cpu.regs[ESP] = STACK
    mem.write32(STACK + 4,  OBJ)
    mem.write32(STACK + 8,  p_locked)
    mem.write32(STACK + 12, p_rect)
    mem.write32(STACK + 16, 0)  # Flags
    stubs.get("d3d8surf", "Surface::LockRect")(cpu)
    return mem.read32(p_locked), mem.read32(p_locked + 4)


class TestLockRectHonorsPRect:

    def test_null_rect_locks_the_origin(self, env):
        cpu, mem, stubs = env
        pitch, pbits = lock_rect(cpu, mem, stubs, None)
        assert pitch == WIDTH * 4
        assert pbits == DATA_PTR

    def test_rect_at_origin_locks_the_origin(self, env):
        cpu, mem, stubs = env
        _, pbits = lock_rect(cpu, mem, stubs, (0, 0, WIDTH, 64))
        assert pbits == DATA_PTR

    def test_rect_offset_by_row_and_column(self, env):
        # A sub-rect starting at (left=16, top=8) must return a pointer
        # into the middle of the buffer -- top*pitch + left*bpp -- not the
        # buffer's absolute start. This is the exact bug: two different
        # tiles both got pbits == DATA_PTR before the fix.
        cpu, mem, stubs = env
        pitch, pbits = lock_rect(cpu, mem, stubs, (16, 8, 32, 16))
        expected = DATA_PTR + 8 * pitch + 16 * 4
        assert pbits == expected

    def test_distinct_sub_rects_get_distinct_pointers(self, env):
        cpu, mem, stubs = env
        _, first  = lock_rect(cpu, mem, stubs, (0, 0, 64, 64))
        _, second = lock_rect(cpu, mem, stubs, (0, 64, 64, 128))
        assert first != second

    def test_pitch_matches_full_surface_width_not_subrect_width(self, env):
        # Pitch describes the stride of the underlying full surface buffer
        # -- it must stay the surface's real width even when locking a
        # narrower sub-rect, since each row of the sub-rect is still laid
        # out at that same stride in memory.
        cpu, mem, stubs = env
        pitch, _ = lock_rect(cpu, mem, stubs, (16, 8, 32, 16))
        assert pitch == WIDTH * 4

    def test_returns_s_ok(self, env):
        from tew.api.d3d8._layout import S_OK
        cpu, mem, stubs = env
        lock_rect(cpu, mem, stubs, (0, 0, 64, 64))
        assert cpu.regs[EAX] == S_OK
