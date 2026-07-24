"""Virtual memory simulation for the x86-32 emulator.

Backed by libcpu.so's mem_* C ABI (see memory_zig.py) -- Memory is an alias
for ZigMemory, the same way tew.hardware.cpu_zig.ZigCPU is the live backend
for what used to be the pure-Python CPU in cpu.py.
"""

from __future__ import annotations

from tew.hardware.memory_zig import ZigMemory as Memory

__all__ = ["Memory"]
