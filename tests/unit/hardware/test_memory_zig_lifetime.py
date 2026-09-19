"""ZigMemory must free its buffer by reference counting, not only via the cyclic GC.

ctypes.cast(array, POINTER(...)) stores the array in the result and forms a
reference cycle. The cyclic GC counts objects, not bytes, so a 272 MB buffer
in a cycle is effectively never collected between tests: a full unit run
climbed to ~8 GB and was OOM-killed. These tests turn the GC off and use
tracemalloc to check how many bytes survive, so a cycle shows up as a
failure instead of being quietly cleaned up later.

(A small ctypes array *type* object per instance is unavoidably cyclic; it is
a few KB and is deliberately not what these tests measure.)
"""
from __future__ import annotations

import gc
import tracemalloc

import pytest

from tew.hardware.memory_zig import ZigMemory

MB = 1024 * 1024


def _retained_bytes(action) -> int:
    """Bytes still allocated after `action()` returns, with the cyclic GC off."""
    gc.collect()
    gc.disable()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        action()
        return tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
        gc.enable()


def test_dropping_a_memory_frees_its_buffer_without_the_cyclic_gc():
    def build_and_drop():
        mem = ZigMemory(16 * MB)
        mem.write32(0x100, 0xDEADBEEF)

    assert _retained_bytes(build_and_drop) < 1 * MB


def test_load_does_not_retain_its_temporary_copy_of_the_data():
    mem = ZigMemory(4 * MB)
    payload = b"\xcd" * (512 * 1024)

    def load_repeatedly():
        for _ in range(20):
            mem.load(0x1000, payload)

    assert _retained_bytes(load_repeatedly) < 1 * MB
