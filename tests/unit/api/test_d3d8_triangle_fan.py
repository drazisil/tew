"""DrawPrimitive turns a triangle fan into the triangle list the pipeline draws.

The game submits ~17,000 PrimType=6 draws in a minute of cockpit view (probe
2026-10-02), most with Z enabled; they were skipped, leaving holes in the world.
"""
from tew.api.d3d8.idirect3d8device import (
    D3DPT_TRIANGLEFAN,
    D3DPT_TRIANGLELIST,
    _triangle_vertex_indices,
)


def test_triangle_list_reads_vertices_in_order():
    assert _triangle_vertex_indices(D3DPT_TRIANGLELIST, 2) == [0, 1, 2, 3, 4, 5]


def test_single_triangle_fan_is_one_triangle():
    assert _triangle_vertex_indices(D3DPT_TRIANGLEFAN, 1) == [0, 1, 2]


def test_fan_shares_vertex_zero_and_advances_one_vertex_per_triangle():
    assert _triangle_vertex_indices(D3DPT_TRIANGLEFAN, 3) == [0, 1, 2, 0, 2, 3, 0, 3, 4]


def test_fan_reads_exactly_prim_count_plus_two_vertices():
    for n in (1, 2, 5, 12):
        assert max(_triangle_vertex_indices(D3DPT_TRIANGLEFAN, n)) == n + 1


def test_empty_primitives_yield_no_vertices():
    assert _triangle_vertex_indices(D3DPT_TRIANGLEFAN, 0) == []
    assert _triangle_vertex_indices(D3DPT_TRIANGLELIST, 0) == []


def test_unsupported_primitive_types_are_not_drawn():
    for prim in (1, 2, 3, 5):  # point list, line list, line strip, triangle strip
        assert _triangle_vertex_indices(prim, 4) is None
