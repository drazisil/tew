"""Regression test for a vertex-color channel swap in DrawPrimitive
(2026-09-05 fix).

Diffuse color was packed as (b, g, r, a) into a plain vec4 vertex
attribute, matching D3DCOLOR's 0xAARRGGBB byte layout -- but the GPU
attribute fetch doesn't know "BGRA" semantics, it just fills the vec4's
.xyzw in memory order, and the fragment shader multiplies it straight
into the output with no reinterpretation. That silently swapped red and
blue for any non-gray vertex color (white/gray is swap-invariant, which
is why it went unnoticed for a whole session of visual verification).

`_d3dcolor_to_rgba` is the extracted, directly-testable piece of that
fix: it must return (r, g, b, a) so a plain vec4's .xyzw order already
means the right thing.
"""
from __future__ import annotations

import pytest

from tew.api.d3d8.idirect3d8device import _d3dcolor_to_rgba


class TestD3DColorToRGBA:

    def test_pure_red(self):
        # D3DCOLOR 0xAARRGGBB = opaque red
        assert _d3dcolor_to_rgba(0xFFFF0000) == (1.0, 0.0, 0.0, 1.0)

    def test_pure_green(self):
        assert _d3dcolor_to_rgba(0xFF00FF00) == (0.0, 1.0, 0.0, 1.0)

    def test_pure_blue(self):
        # This is the exact bug: a pure-blue D3DCOLOR must come back with
        # blue in the third (b) slot, not swapped into the first (r) slot.
        assert _d3dcolor_to_rgba(0xFF0000FF) == (0.0, 0.0, 1.0, 1.0)

    def test_opaque_white_is_swap_invariant(self):
        # White/gray colors are unaffected by the R/B swap bug -- this is
        # exactly why the bug went unnoticed during visual verification.
        assert _d3dcolor_to_rgba(0xFFFFFFFF) == (1.0, 1.0, 1.0, 1.0)

    def test_alpha_channel(self):
        r, g, b, a = _d3dcolor_to_rgba(0x80FF0000)
        assert a == pytest.approx(0x80 / 255.0)

    def test_distinct_rgb_values_land_in_correct_channels(self):
        # ARGB = (A=0xFF, R=0x10, G=0x20, B=0x30) -- each channel a
        # distinct value, so any channel permutation bug would be caught.
        dif = 0xFF102030
        r, g, b, a = _d3dcolor_to_rgba(dif)
        assert (r, g, b, a) == (
            0x10 / 255.0, 0x20 / 255.0, 0x30 / 255.0, 0xFF / 255.0,
        )
