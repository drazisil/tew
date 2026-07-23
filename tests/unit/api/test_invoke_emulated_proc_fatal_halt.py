"""Regression test for _invoke_emulated_proc propagating a fatal halt as a
raised exception instead of returning 0 (tew/api/user32_handlers.py).

Background (see memory/status.md, 2026-07-23 sessions): _invoke_emulated_proc
used to fall back to returning a bare `0` whenever its nested call didn't
genuinely complete, fatal halts included. That `0` is a safe sentinel for
DllMain-style callers (0 means FALSE) but is indistinguishable from a real
S_OK to any HRESULT-returning caller -- exactly what disguised the
CoGetMalloc/oleaut32-ordinal-15/21 aborts as fake DllGetClassObject
successes with a NULL *ppv, the bug that took several sessions to root-cause.

Fix: cpu.run()/cpu.step() (tew/hardware/cpu_zig.py) now raise FatalHaltError
the instant cpu.fatal_halt newly becomes true during a call -- the single
chokepoint every caller funnels through. _invoke_emulated_proc no longer
needs (or has) any of its own fatal-halt detection; the exception simply
propagates out of its polling loop.
"""
from __future__ import annotations

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pytest

from tew.api._state import CRTState
from tew.api.user32_handlers import _invoke_emulated_proc
from tew.api.win32_handlers import Win32Handlers
from tew.hardware.cpu_zig import ZigCPU as CPU, FatalHaltError
from tew.hardware.memory import Memory

MEM_SIZE  = 8 * 1024 * 1024
STACK     = 0x200000
PROC_ADDR = 0x500000
SENTINEL  = 0x510000


def test_invoke_emulated_proc_raises_when_nested_call_fatally_halts():
    mem = Memory(MEM_SIZE)
    state = CRTState()  # constructs state.scheduler with a main thread already

    cpu = CPU(mem)
    cpu.regs[4] = STACK  # ESP

    stubs = Win32Handlers(mem)

    def _fake_unimplemented(c: "CPU") -> None:
        c.halted = True
        c.fatal_halt = True

    stubs.register_handler("test", "FakeUnimplemented", _fake_unimplemented)
    stubs.install(cpu)

    halt_addr = stubs.get_handler_address("test", "FakeUnimplemented")
    assert halt_addr is not None

    # mov edx, halt_addr ; call edx
    proc = bytes([0xBA]) + halt_addr.to_bytes(4, "little") + bytes([0xFF, 0xD2])
    for i, b in enumerate(proc):
        mem.write8(PROC_ADDR + i, b)
    mem.write8(SENTINEL, 0xF4)  # HLT -- never reached, the call never returns

    with pytest.raises(FatalHaltError):
        _invoke_emulated_proc(
            cpu, mem,
            proc_addr=PROC_ADDR,
            args=[],
            sentinel=SENTINEL,
            max_steps=10_000,
            scheduler=state.scheduler,
        )

    assert cpu.fatal_halt is True
