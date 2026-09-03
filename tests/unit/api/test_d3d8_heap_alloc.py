"""Tests for the D3D8/DirectSound/DirectInput private bump-heap allocator
(tew.api.d3d8._helpers._heap_alloc) -- specifically its bounded region.

Historical bug: this allocator started at 0x04800000, inside the CRT heap's
own valid range (0x04000000-THREAD_STACK_BASE=0x08000000, see
tew/api/_state.py), with no upper bound at all. Once the CRT heap's cursor
grew past 0x04800000 the two allocators silently handed out overlapping
addresses -- confirmed live to cause a real EIP=0x00000000 crash (a
_heap_alloc'd DirectInput device object's vtable pointer got clobbered by
unrelated CRT-heap writes). The fix gives _heap_alloc its own region
(0x09000000-0x10000000, between the thread-stack region and the DLL range)
and a loud bounds check, mirroring simple_alloc's own THREAD_STACK_BASE
check in tew/api/_state.py.
"""
from __future__ import annotations

import pytest

import tew.api.d3d8._helpers as helpers
from tew.api.d3d8._helpers import D3D8_HEAP_BASE, D3D8_HEAP_LIMIT, _heap_alloc


@pytest.fixture(autouse=True)
def _reset_cursor():
    """Each test gets a fresh bump cursor -- _next_heap_addr is module-global
    state shared across the whole process, same as CRTState.next_heap_alloc
    would be if this allocator were instance-owned instead."""
    saved = helpers._next_heap_addr
    helpers._next_heap_addr = D3D8_HEAP_BASE
    yield
    helpers._next_heap_addr = saved


def test_first_alloc_starts_at_base():
    assert _heap_alloc(16) == D3D8_HEAP_BASE


def test_bumps_by_16_byte_aligned_size():
    first = _heap_alloc(10)
    second = _heap_alloc(4)
    assert second == first + 16


def test_region_does_not_overlap_crt_heap_or_thread_stacks():
    # CRT heap: 0x04000000-0x08000000 (THREAD_STACK_BASE). Thread stacks:
    # 0x08000000-0x08FFFFFF. D3D8_HEAP_BASE must sit strictly above both.
    assert D3D8_HEAP_BASE >= 0x09000000
    # DLL range starts at 0x10000000 (tew/loader/dll_loader.py) -- the D3D8
    # heap must not be able to grow into it.
    assert D3D8_HEAP_LIMIT <= 0x10000000


def test_alloc_within_region_succeeds():
    addr = _heap_alloc(D3D8_HEAP_LIMIT - D3D8_HEAP_BASE - 16)
    assert D3D8_HEAP_BASE <= addr < D3D8_HEAP_LIMIT


def test_alloc_past_limit_raises_loudly():
    helpers._next_heap_addr = D3D8_HEAP_LIMIT - 8
    with pytest.raises(RuntimeError, match="D3D8 private heap exhausted"):
        _heap_alloc(16)
