"""Single ctypes.CDLL handle for libcpu.so, shared by cpu_zig.py, memory_zig.py,
and alloc_zig.py -- one dlopen instead of three. Each importer still sets its own
argtypes/restype; no symbol overlap (cpu_*, mem_*, bump_alloc_next are disjoint)."""

from __future__ import annotations

import ctypes
from pathlib import Path

LIB_PATH = Path(__file__).parent.parent.parent / "cpu" / "zig-out" / "lib" / "libcpu.so"
_lib = ctypes.CDLL(str(LIB_PATH))
