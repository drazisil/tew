"""bump_alloc_next — Zig-backed bump-pointer arithmetic for the guest heap."""

from __future__ import annotations

import ctypes

from tew.hardware._kernel_lib import _lib


def _bind_lib() -> ctypes.CDLL:
    lib = _lib

    lib.bump_alloc_next.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    lib.bump_alloc_next.restype = ctypes.c_uint32

    return lib


_bind_lib()


def bump_alloc_next(current: int, size: int) -> int:
    """Compute the next bump-allocator cursor after allocating `size` bytes
    at `current`.  Matches the original Python bump math exactly:
    (current + size + 15) & ~15 -- 16-byte aligned, always advances even for
    size=0.  The caller (CRTState.simple_alloc) still owns `current` as the
    allocated address and reassigns its cursor to this return value; no
    state lives on the Zig side.
    """
    return _lib.bump_alloc_next(current & 0xFFFFFFFF, size & 0xFFFFFFFF)
