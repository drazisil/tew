"""Tests for oleaut32.dll!DispCallFunc -- previously unimplemented (hard halt).

Background: expsrv.dll's own init (ordinal #2000) probes for this via
GetProcAddress before doing anything else -- see the DAO-3075 investigation,
memory/status.md "2026-08-18". Implemented as the leading fix candidate for
the universal function-call-recognition failure found there (msjet35.dll's
real parser hits internal error 0x271e on every `identifier(` construct).

Two families of test here, matching the two kinds of code path DispCallFunc
has:
  - Validation failures that halt loudly *before* ever invoking the target
    (bad calling convention, unsupported argument VARTYPE) -- these use a
    bare _FakeCPU, same convention as test_ole32_com.py, since the code
    never reaches cpu.run().
  - Real invocations -- these need a genuine ZigCPU (save_state/run/
    restore_state) plus a real CRTState (for the scheduler + dialog
    sentinel), matching test_invoke_emulated_proc_thread_death.py's pattern,
    since _DispCallFunc calls _invoke_emulated_proc for real.
"""
from __future__ import annotations

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pytest

import tew.api.user32_handlers as _user32_handlers
from tew.api._state import CRTState
from tew.api.oleaut32_handlers import register_oleaut32_ole32_handlers
from tew.hardware.cpu_zig import EAX, ESP
from tew.hardware.cpu_zig import ZigCPU as CPU
from tew.hardware.memory import Memory


class _StubHandlers:
    def __init__(self):
        self._h: dict = {}

    def register_handler(self, dll, name, fn):
        self._h[(dll, name)] = fn

    def get(self, dll, name):
        return self._h[(dll, name)]


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False
        self.fatal_halt = False


MEM_SIZE   = 8 * 1024 * 1024
STACK      = 0x200000
ARGS       = 0x300000   # scratch for VARIANT structs / arg arrays
PROC       = 0x500000   # scratch for the emulated callee stub
HEAP_START = 0x600000   # for CRTState.simple_alloc (_get_dialog_sentinel's HLT byte);
                         # default 0x04000000 is past MEM_SIZE here

CC_CDECL   = 1
CC_STDCALL = 4

VT_I4      = 3
VT_R8      = 5
VT_BSTR    = 8
VT_DECIMAL = 14        # deliberately unhandled VARTYPE, for the halt test
VT_BYREF   = 0x4000

S_OK = 0


def _write_variant_i4(mem: Memory, addr: int, value: int) -> None:
    mem.write16(addr, VT_I4)
    mem.write32(addr + 8, value & 0xFFFFFFFF)


def _write_add_one_stub(mem: Memory, addr: int) -> None:
    # MOV EAX, [ESP+4]; ADD EAX, 1; RET
    code = bytes([0x8B, 0x44, 0x24, 0x04, 0x83, 0xC0, 0x01, 0xC3])
    for i, b in enumerate(code):
        mem.write8(addr + i, b)


def _write_sum_two_stub(mem: Memory, addr: int) -> None:
    # MOV EAX, [ESP+4]; ADD EAX, [ESP+8]; RET
    code = bytes([0x8B, 0x44, 0x24, 0x04, 0x03, 0x44, 0x24, 0x08, 0xC3])
    for i, b in enumerate(code):
        mem.write8(addr + i, b)


def _write_byref_increment_stub(mem: Memory, addr: int) -> None:
    # MOV EAX, [ESP+4]; MOV ECX, [EAX]; INC ECX; MOV [EAX], ECX; XOR EAX, EAX; RET
    code = bytes([
        0x8B, 0x44, 0x24, 0x04,
        0x8B, 0x08,
        0x41,
        0x89, 0x08,
        0x31, 0xC0,
        0xC3,
    ])
    for i, b in enumerate(code):
        mem.write8(addr + i, b)


@pytest.fixture
def fake_env():
    """For validation-halt paths that never reach _invoke_emulated_proc."""
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_oleaut32_ole32_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # return address
    return cpu, mem, state, stubs


@pytest.fixture
def real_env():
    """For real invocations -- needs a genuine ZigCPU + scheduler."""
    mem   = Memory(MEM_SIZE)
    state = CRTState()  # constructs state.scheduler with a main thread already
    state.next_heap_alloc = HEAP_START
    # _get_dialog_sentinel caches its allocated address in a module-level
    # global and only writes the HLT byte the first time it's ever called
    # in this process -- reset it so every test gets a fresh HLT byte
    # written into ITS OWN mem instance, not a stale address that may or
    # may not still hold 0xF4 in a differently-allocated Memory buffer.
    _user32_handlers._DIALOG_SENTINEL_ADDR = 0
    stubs = _StubHandlers()
    register_oleaut32_ole32_handlers(stubs, mem, state)
    cpu = CPU(mem)
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # return address
    return cpu, mem, state, stubs


def _set_call_args(mem: Memory, base: int, pv_instance: int, o_vft: int, cc: int,
                    vt_return: int, c_actuals: int, prgvt: int, prgpvarg: int,
                    pvarg_result: int) -> None:
    mem.write32(base + 4,  pv_instance)
    mem.write32(base + 8,  o_vft)
    mem.write32(base + 12, cc)
    mem.write32(base + 16, vt_return)
    mem.write32(base + 20, c_actuals)
    mem.write32(base + 24, prgvt)
    mem.write32(base + 28, prgpvarg)
    mem.write32(base + 32, pvarg_result)


class TestDirectInvocation:
    """pvInstance == 0 -- oVft IS the function address directly."""

    def test_calls_target_and_marshals_i4_result(self, real_env):
        cpu, mem, state, stubs = real_env
        _write_add_one_stub(mem, PROC)

        prgvt    = ARGS
        prgpvarg = ARGS + 0x100
        arg0     = ARGS + 0x200
        result   = ARGS + 0x300

        mem.write16(prgvt, VT_I4)
        mem.write32(prgpvarg, arg0)
        _write_variant_i4(mem, arg0, 41)

        _set_call_args(mem, STACK, pv_instance=0, o_vft=PROC, cc=CC_STDCALL,
                        vt_return=VT_I4, c_actuals=1, prgvt=prgvt,
                        prgpvarg=prgpvarg, pvarg_result=result)

        stubs.get("oleaut32.dll", "DispCallFunc")(cpu)

        assert cpu.fatal_halt is False
        assert cpu.regs[EAX] == S_OK
        assert mem.read16(result) == VT_I4
        assert mem.read32(result + 8) == 42

    def test_calls_target_with_two_actuals(self, real_env):
        cpu, mem, state, stubs = real_env
        _write_sum_two_stub(mem, PROC)

        prgvt    = ARGS
        prgpvarg = ARGS + 0x100
        arg0     = ARGS + 0x200
        arg1     = ARGS + 0x210
        result   = ARGS + 0x300

        mem.write16(prgvt, VT_I4)
        mem.write16(prgvt + 2, VT_I4)
        mem.write32(prgpvarg, arg0)
        mem.write32(prgpvarg + 4, arg1)
        _write_variant_i4(mem, arg0, 10)
        _write_variant_i4(mem, arg1, 32)

        _set_call_args(mem, STACK, pv_instance=0, o_vft=PROC, cc=CC_CDECL,
                        vt_return=VT_I4, c_actuals=2, prgvt=prgvt,
                        prgpvarg=prgpvarg, pvarg_result=result)

        stubs.get("oleaut32.dll", "DispCallFunc")(cpu)

        assert cpu.regs[EAX] == S_OK
        assert mem.read32(result + 8) == 42

    def test_byref_argument_passes_pointer_through(self, real_env):
        cpu, mem, state, stubs = real_env
        _write_byref_increment_stub(mem, PROC)

        prgvt    = ARGS
        prgpvarg = ARGS + 0x100
        arg0     = ARGS + 0x200
        target_cell = ARGS + 0x400
        result   = ARGS + 0x300

        mem.write32(target_cell, 99)
        mem.write16(prgvt, VT_I4 | VT_BYREF)
        mem.write32(prgpvarg, arg0)
        mem.write16(arg0, VT_I4 | VT_BYREF)
        mem.write32(arg0 + 8, target_cell)  # value slot holds the pointer

        _set_call_args(mem, STACK, pv_instance=0, o_vft=PROC, cc=CC_STDCALL,
                        vt_return=VT_I4, c_actuals=1, prgvt=prgvt,
                        prgpvarg=prgpvarg, pvarg_result=result)

        stubs.get("oleaut32.dll", "DispCallFunc")(cpu)

        assert cpu.regs[EAX] == S_OK
        assert mem.read32(target_cell) == 100  # callee incremented *pointer


class TestVtableDispatch:
    """pvInstance != 0 -- target is *(int*)(*(int*)pvInstance + oVft)."""

    def test_resolves_through_vtable(self, real_env):
        cpu, mem, state, stubs = real_env
        _write_add_one_stub(mem, PROC)

        vtable  = ARGS + 0x1000
        obj     = ARGS + 0x1100
        mem.write32(vtable + 8, PROC)  # slot at byte offset 8
        mem.write32(obj, vtable)

        prgvt    = ARGS
        prgpvarg = ARGS + 0x100
        arg0     = ARGS + 0x200
        result   = ARGS + 0x300
        mem.write16(prgvt, VT_I4)
        mem.write32(prgpvarg, arg0)
        _write_variant_i4(mem, arg0, 7)

        _set_call_args(mem, STACK, pv_instance=obj, o_vft=8, cc=CC_STDCALL,
                        vt_return=VT_I4, c_actuals=1, prgvt=prgvt,
                        prgpvarg=prgpvarg, pvarg_result=result)

        stubs.get("oleaut32.dll", "DispCallFunc")(cpu)

        assert cpu.regs[EAX] == S_OK
        assert mem.read32(result + 8) == 8


class TestUnhandledInputsHaltLoudly:
    def test_unsupported_calling_convention_halts(self, fake_env):
        cpu, mem, state, stubs = fake_env
        _set_call_args(mem, STACK, pv_instance=0, o_vft=PROC, cc=99,
                        vt_return=VT_I4, c_actuals=0, prgvt=0, prgpvarg=0,
                        pvarg_result=0)

        stubs.get("oleaut32.dll", "DispCallFunc")(cpu)

        assert cpu.halted is True
        assert cpu.fatal_halt is True

    def test_unsupported_argument_vartype_halts(self, fake_env):
        cpu, mem, state, stubs = fake_env
        prgvt    = ARGS
        prgpvarg = ARGS + 0x100
        arg0     = ARGS + 0x200
        mem.write16(prgvt, VT_DECIMAL)
        mem.write32(prgpvarg, arg0)
        mem.write16(arg0, VT_DECIMAL)

        _set_call_args(mem, STACK, pv_instance=0, o_vft=PROC, cc=CC_STDCALL,
                        vt_return=VT_I4, c_actuals=1, prgvt=prgvt,
                        prgpvarg=prgpvarg, pvarg_result=0)

        stubs.get("oleaut32.dll", "DispCallFunc")(cpu)

        assert cpu.halted is True
        assert cpu.fatal_halt is True

    def test_unsupported_return_vartype_halts(self, real_env):
        cpu, mem, state, stubs = real_env
        _write_add_one_stub(mem, PROC)

        prgvt    = ARGS
        prgpvarg = ARGS + 0x100
        arg0     = ARGS + 0x200
        result   = ARGS + 0x300
        mem.write16(prgvt, VT_I4)
        mem.write32(prgpvarg, arg0)
        _write_variant_i4(mem, arg0, 1)

        # VT_R8 is a real VARTYPE but this handler deliberately doesn't
        # support float returns (would need _invoke_emulated_proc to expose
        # FPU ST(0) before its own cpu.restore_state() discards it).
        _set_call_args(mem, STACK, pv_instance=0, o_vft=PROC, cc=CC_STDCALL,
                        vt_return=VT_R8, c_actuals=1, prgvt=prgvt,
                        prgpvarg=prgpvarg, pvarg_result=result)

        stubs.get("oleaut32.dll", "DispCallFunc")(cpu)

        assert cpu.halted is True
        assert cpu.fatal_halt is True
