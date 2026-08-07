"""Tests for IDirect3D8::AddRef/Release refcounting.

Regression coverage for the 2026-08-07 fix: AddRef/Release were stubs that
always returned 1/0 regardless of how many references were actually
outstanding. That meant *any* Release call -- even one of several
outstanding references, e.g. the render thread releasing its own reference
while the main thread still held one -- told the game the object had just
hit zero, so the game tore down the object's internal mutex immediately.
The main thread then tripped over that as "MUTEX_free - FREEING A LOCKED
MUTEX" a few instructions later (confirmed live via /tmp/emu.log).

Tests call _add_ref/_release directly rather than going through make_vtable,
since those two functions don't touch the WindowManager/Vulkan setup that
the rest of the IDirect3D8 vtable (Direct3DCreate8, CreateDevice, ...)
needs -- keeping this test free of that setup entirely.
"""
from __future__ import annotations

import pytest

from tew.api.d3d8 import idirect3d8
from tew.api.d3d8.idirect3d8 import _add_ref, _release
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False


MEM_SIZE = 4 * 1024 * 1024
STACK    = 0x100000
OBJ_A    = 0x200000
OBJ_B    = 0x210000


@pytest.fixture(autouse=True)
def _reset_ref_counts():
    # Module-level dict, same convention as idirect3d8resource.py -- must
    # not leak state between tests.
    idirect3d8._ref_counts.clear()
    yield
    idirect3d8._ref_counts.clear()


@pytest.fixture
def env():
    mem = Memory(MEM_SIZE)
    cpu = _FakeCPU()
    return cpu, mem


def com_call(fn, cpu, mem, this_ptr):
    cpu.regs[ESP] = STACK
    mem.write32(STACK + 4, this_ptr)
    fn(cpu, mem)


class TestAddRefRelease:

    def test_first_add_ref_returns_2(self, env):
        # Object starts at refcount 1 (as returned by its creator); the
        # first AddRef bumps it to 2.
        cpu, mem = env
        com_call(_add_ref, cpu, mem, OBJ_A)
        assert cpu.regs[EAX] == 2

    def test_release_without_add_ref_returns_0(self, env):
        cpu, mem = env
        com_call(_release, cpu, mem, OBJ_A)
        assert cpu.regs[EAX] == 0

    def test_release_only_hits_zero_on_true_last_reference(self, env):
        # Two AddRefs (refcount 3), then two Releases must land back at 1,
        # not 0 -- this is the exact bug: the old stub reported 0 on every
        # single Release call.
        cpu, mem = env
        com_call(_add_ref, cpu, mem, OBJ_A)
        com_call(_add_ref, cpu, mem, OBJ_A)
        com_call(_release, cpu, mem, OBJ_A)
        assert cpu.regs[EAX] == 2
        com_call(_release, cpu, mem, OBJ_A)
        assert cpu.regs[EAX] == 1

    def test_release_never_goes_negative(self, env):
        cpu, mem = env
        com_call(_release, cpu, mem, OBJ_A)
        com_call(_release, cpu, mem, OBJ_A)
        assert cpu.regs[EAX] == 0

    def test_refcounts_are_tracked_per_object(self, env):
        cpu, mem = env
        com_call(_add_ref, cpu, mem, OBJ_A)
        com_call(_add_ref, cpu, mem, OBJ_A)
        com_call(_add_ref, cpu, mem, OBJ_B)
        assert idirect3d8._ref_counts[OBJ_A] == 3
        assert idirect3d8._ref_counts[OBJ_B] == 2

    def test_final_release_removes_entry(self, env):
        cpu, mem = env
        com_call(_add_ref, cpu, mem, OBJ_A)
        com_call(_release, cpu, mem, OBJ_A)
        com_call(_release, cpu, mem, OBJ_A)
        assert OBJ_A not in idirect3d8._ref_counts
