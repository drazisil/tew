"""Tests for dsound.dll's Buf::Lock circular-buffer cursor math -- the
densest arithmetic in dsound_handlers.py, isolated from the rest of the
buffer lifecycle (test_dsound_buffer_lifecycle.py) because a wrap-math
bug here is exactly the kind of thing that silently drifts.
"""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.dsound_handlers import (
    register_dsound_handlers,
    DS_OBJ,
    DS_OK,
    DSERR_INVALIDPARAM,
    DSBCAPS_PRIMARYBUFFER,
    DSBLOCK_ENTIREBUFFER,
)
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


MEM_SIZE = 128 * 1024 * 1024  # must exceed the D3D8 private heap base (0x04800000)
STACK    = 0x200000
BUF_A    = 0x300000
DESC_ADDR = 0x320000
WFX_ADDR  = 0x330000

# Buf::Lock output slots, arbitrary scratch addresses
PTR1 = 0x340000
BYTES1 = 0x340004
PTR2 = 0x340008
BYTES2 = 0x34000C


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_dsound_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    return cpu, mem, state, stubs


def com_call(stubs, cpu, mem, dll, name, this_ptr, args):
    """COM-style call: [ESP]=ret, [ESP+4]=this, [ESP+8..]=args."""
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4, this_ptr)
    for i, val in enumerate(args):
        mem.write32(STACK + 8 + i * 4, val)
    stubs.get(dll, name)(cpu)


def write_dsbufferdesc(mem, addr, flags=0, buf_bytes=0, wfx_ptr=0):
    mem.write32(addr, 0)
    mem.write32(addr + 4, flags)
    mem.write32(addr + 8, buf_bytes)
    mem.write32(addr + 16, wfx_ptr)


def create_secondary_buffer(stubs, cpu, mem, buf_bytes=1024):
    write_dsbufferdesc(mem, DESC_ADDR, flags=0, buf_bytes=buf_bytes, wfx_ptr=0)
    com_call(stubs, cpu, mem, "dsound.dll", "DS::CreateSoundBuffer", DS_OBJ, [DESC_ADDR, BUF_A, 0])
    return mem.read32(BUF_A)


def create_primary_buffer(stubs, cpu, mem):
    write_dsbufferdesc(mem, DESC_ADDR, flags=DSBCAPS_PRIMARYBUFFER)
    com_call(stubs, cpu, mem, "dsound.dll", "DS::CreateSoundBuffer", DS_OBJ, [DESC_ADDR, BUF_A, 0])
    return mem.read32(BUF_A)


def lock(stubs, cpu, mem, obj, dw_cursor, dw_bytes, dw_flags=0):
    for addr in (PTR1, BYTES1, PTR2, BYTES2):
        mem.write32(addr, 0xFFFFFFFF)
    com_call(stubs, cpu, mem, "dsound.dll", "Buf::Lock", obj,
             [dw_cursor, dw_bytes, PTR1, BYTES1, PTR2, BYTES2, dw_flags])


class TestBufLockPrimaryOrUnknown:

    def test_primary_buffer_zeroes_all_outputs(self, env):
        cpu, mem, state, stubs = env
        obj = create_primary_buffer(stubs, cpu, mem)
        lock(stubs, cpu, mem, obj, 0, 100)
        assert mem.read32(PTR1) == 0
        assert mem.read32(BYTES1) == 0
        assert mem.read32(PTR2) == 0
        assert mem.read32(BYTES2) == 0
        assert cpu.regs[EAX] == DS_OK

    def test_unknown_buffer_zeroes_all_outputs(self, env):
        cpu, mem, state, stubs = env
        # A COM-object-shaped struct whose idx field (offset 12) points nowhere real.
        mem.write32(BUF_A + 12, 0xFFFFFF)
        lock(stubs, cpu, mem, BUF_A, 0, 100)
        assert mem.read32(PTR1) == 0
        assert mem.read32(BYTES1) == 0
        assert cpu.regs[EAX] == DS_OK

    def test_null_obj_returns_invalidparam(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::Lock", 0,
                 [0, 100, PTR1, BYTES1, PTR2, BYTES2, 0])
        assert cpu.regs[EAX] == DSERR_INVALIDPARAM


class TestBufLockEntireBuffer:

    def test_forces_cursor_to_zero_and_full_size(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=1000)
        lock(stubs, cpu, mem, obj, 500, 1, dw_flags=DSBLOCK_ENTIREBUFFER)
        pcm_addr = mem.read32(obj + 4)
        assert mem.read32(PTR1) == pcm_addr
        assert mem.read32(BYTES1) == 1000
        assert mem.read32(PTR2) == 0
        assert mem.read32(BYTES2) == 0


class TestBufLockNonWrapping:

    def test_single_contiguous_region(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=1000)
        pcm_addr = mem.read32(obj + 4)
        lock(stubs, cpu, mem, obj, 100, 200)
        assert mem.read32(PTR1) == pcm_addr + 100
        assert mem.read32(BYTES1) == 200
        assert mem.read32(PTR2) == 0
        assert mem.read32(BYTES2) == 0

    def test_exactly_up_to_buffer_end_does_not_wrap(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=1000)
        lock(stubs, cpu, mem, obj, 800, 200)  # end == bs exactly
        assert mem.read32(BYTES1) == 200
        assert mem.read32(BYTES2) == 0


class TestBufLockWrapping:

    def test_splits_across_wrap_point(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=1000)
        pcm_addr = mem.read32(obj + 4)
        # start=900, dw_bytes=200 -> end=1100 > bs=1000 -> wraps
        lock(stubs, cpu, mem, obj, 900, 200)
        assert mem.read32(PTR1) == pcm_addr + 900
        assert mem.read32(BYTES1) == 100   # bs - start = 1000 - 900
        assert mem.read32(PTR2) == pcm_addr
        assert mem.read32(BYTES2) == 100   # dw_bytes - bytes1 = 200 - 100

    def test_wrap_split_sizes_sum_to_requested_bytes(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=1500)
        lock(stubs, cpu, mem, obj, 1400, 300)
        bytes1 = mem.read32(BYTES1)
        bytes2 = mem.read32(BYTES2)
        assert bytes1 + bytes2 == 300
        assert bytes1 == 100  # 1500 - 1400
        assert bytes2 == 200

    def test_cursor_already_at_or_past_buffer_size_wraps_before_split(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=1000)
        pcm_addr = mem.read32(obj + 4)
        # dw_cursor=2300 -> start = 2300 % 1000 = 300; end = 300+800=1100 > 1000 -> wraps
        lock(stubs, cpu, mem, obj, 2300, 800)
        assert mem.read32(PTR1) == pcm_addr + 300
        assert mem.read32(BYTES1) == 700   # 1000 - 300
        assert mem.read32(BYTES2) == 100   # 800 - 700
