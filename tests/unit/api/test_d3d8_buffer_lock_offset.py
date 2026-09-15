"""Regression test for IDirect3DVertexBuffer8/IndexBuffer8::Lock ignoring
OffsetToLock (2026-09-05 fix).

Real D3D8 apps append geometry into one large dynamic vertex/index buffer
via repeated Lock(offset, size, ..., D3DLOCK_NOOVERWRITE) calls at a
growing offset, writing new data past what's already there without
disturbing it. The old `Buffer::Lock` handler always handed back the
buffer's base `data_ptr` regardless of the requested offset, so every such
write landed at offset 0 (clobbering the previous write) while
DrawPrimitive correctly read from `base + StartVertex*stride` -- an
address that had never actually been written. Confirmed live: 904/905
sampled DrawPrimitive calls with StartVertex > 0 read all-zero vertex
data from an otherwise correctly-populated buffer.

`Buffer::Lock` is a closure inside `idirect3d8resource.make_vtable`, so
this test registers the real vtable (same as the running emulator does)
and invokes the registered handler by name, matching the convention in
test_dsound_buffer_lifecycle.py.
"""
from __future__ import annotations

import pytest

from tew.api.d3d8.idirect3d8resource import make_vtable
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP


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


MEM_SIZE = 4 * 1024 * 1024
STACK    = 0x100000
OBJ      = 0x200000
DATA_PTR = 0x210000


@pytest.fixture
def env():
    mem = Memory(MEM_SIZE)
    cpu = _FakeCPU()
    stubs = _StubHandlers()
    make_vtable(stubs, mem)
    # Object layout per _alloc_resource_obj: [0] vtable, [4] data_ptr, [8] size.
    mem.write32(OBJ + 4, DATA_PTR)
    return cpu, mem, stubs


def lock(cpu, mem, stubs, offset: int, size_to_lock: int = 0, flags: int = 0) -> int:
    """Call Buffer::Lock(OffsetToLock, SizeToLock, BYTE** ppbData, Flags)
    and return the pointer written back through ppbData."""
    ppb_data = STACK + 100
    cpu.regs[ESP] = STACK
    mem.write32(STACK + 4,  OBJ)      # this
    mem.write32(STACK + 8,  offset)
    mem.write32(STACK + 12, size_to_lock)
    mem.write32(STACK + 16, ppb_data)
    mem.write32(STACK + 20, flags)
    stubs.get("d3d8res", "Buffer::Lock")(cpu)
    return mem.read32(ppb_data)


class TestBufferLockHonorsOffset:

    def test_zero_offset_returns_base_pointer(self, env):
        cpu, mem, stubs = env
        assert lock(cpu, mem, stubs, 0) == DATA_PTR

    def test_nonzero_offset_returns_base_plus_offset(self, env):
        cpu, mem, stubs = env
        assert lock(cpu, mem, stubs, 0x1000) == DATA_PTR + 0x1000

    def test_growing_offsets_return_distinct_pointers(self, env):
        # This is the exact real-world pattern: repeated Lock() calls at a
        # growing offset must each get a distinct, correct pointer -- not
        # all collapse to the same base address.
        cpu, mem, stubs = env
        ptrs = [lock(cpu, mem, stubs, off) for off in (0, 96, 192, 6210 * 32)]
        assert ptrs == [DATA_PTR + off for off in (0, 96, 192, 6210 * 32)]

    def test_returns_s_ok(self, env):
        from tew.api.d3d8._layout import S_OK
        cpu, mem, stubs = env
        lock(cpu, mem, stubs, 64)
        assert cpu.regs[EAX] == S_OK
