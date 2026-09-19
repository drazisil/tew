"""Unit-test conftest.

Several modules here build a 128-272 MB `Memory` per test. Anything holding
one in a reference cycle (handler closures and CRTState pointing at each
other, for example) is freed only when the cyclic GC happens to run, and the
GC's trigger counts allocated *objects*, not bytes -- a few hundred tests
that each strand a big buffer climbed a full run to ~8 GB and got it
OOM-killed. Collecting after every test bounds the peak to about one test's
worth of memory.
"""
from __future__ import annotations

import gc

import pytest


@pytest.fixture(autouse=True)
def _collect_garbage_after_test():
    yield
    gc.collect()
