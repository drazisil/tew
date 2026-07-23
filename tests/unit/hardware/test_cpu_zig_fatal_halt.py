"""Regression tests for cpu.fatal_halt being a real, unclearable native lockup.

Background (see memory/status.md, 2026-07-22 session): ZigCPU.faulted's setter
used to call the native cpu_clear_halted unconditionally, which cleared native
`s.halted` even when a fatal halt had just fired -- letting later cpu.run()
calls (e.g. _invoke_emulated_proc's polling loop) genuinely execute more real
instructions after what was supposed to be a permanent, unbypassable stop.

The fix moved enforcement into the Zig CPU core itself: a dedicated
`fatal_halted` native flag, set only via cpu_set_fatal_halt, that
cpu_clear_halted refuses to clear, that cpu_run's own per-instruction loop
refuses to execute past, and that every register/eflags/FPU setter refuses to
write once set. These tests use the real ZigCPU (no MagicMock) because the
original bug was specifically a native-state desync that a mocked CPU cannot
reproduce -- see tests/unit/kernel/test_scheduler.py's own MagicMock-based
tests, which passed throughout even though this bug was live.
"""
from __future__ import annotations

import pytest

from tew.hardware.cpu_zig import ZigCPU as CPU, EAX, ESP, EBP, FatalHaltError
from tew.hardware.memory import Memory

MEM_SIZE = 1 * 1024 * 1024
PROC_ADDR = 0x100000 - 0x1000  # arbitrary valid address within MEM_SIZE


def _make_cpu() -> tuple[CPU, Memory]:
    mem = Memory(MEM_SIZE)
    cpu = CPU(mem)
    mem.write8(PROC_ADDR, 0xF4)  # HLT -- should never actually execute below
    cpu.eip = PROC_ADDR
    return cpu, mem


def test_faulted_clear_does_not_resurrect_a_fatal_halt():
    """The exact bug: cpu.faulted = False used to unconditionally clear
    native halted, even when fatal_halt was set moments earlier."""
    cpu, mem = _make_cpu()

    cpu.halted = True
    cpu.fatal_halt = True
    step_count_before = cpu.step_count

    # Mirrors run_exe.py's SEH-resume path: a fault handler decided "this was
    # handled", so it clears cpu.faulted to let execution continue.
    cpu.faulted = False

    assert cpu.halted is True
    assert cpu.fatal_halt is True

    cpu.run(100)

    assert cpu.step_count == step_count_before
    assert cpu.eip == PROC_ADDR


def test_run_and_step_are_noops_once_fatally_halted():
    cpu, mem = _make_cpu()
    cpu.fatal_halt = True
    step_count_before = cpu.step_count

    cpu.run(100)
    assert cpu.step_count == step_count_before
    assert cpu.eip == PROC_ADDR

    cpu.step()
    assert cpu.step_count == step_count_before
    assert cpu.eip == PROC_ADDR


def test_fatal_halt_cannot_be_cleared():
    cpu, mem = _make_cpu()
    cpu.fatal_halt = True

    cpu.halted = False
    cpu.faulted = False

    assert cpu.fatal_halt is True
    assert cpu.halted is True


def test_run_raises_when_fatal_halt_newly_fires_mid_call():
    """(2026-07-23) cpu.run()/cpu.step() are the single chokepoint into the
    native cpu_run FFI call -- the fix for the sentinel-collision bug
    (memory/status.md: _invoke_emulated_proc's bare-0-on-abort fallback was
    indistinguishable from a genuine S_OK to any HRESULT-returning caller,
    exactly what disguised the CoGetMalloc/ordinal-15/21 aborts as fake
    DllGetClassObject successes) is to raise here the instant fatal_halt
    newly becomes true during a call, instead of returning normally and
    leaving callers to notice via a polled flag."""
    cpu, mem = _make_cpu()
    mem.write8(PROC_ADDR, 0xCD)     # INT
    mem.write8(PROC_ADDR + 1, 0xFE)  # 0xFE -- triggers on_interrupt

    def _handler(int_num: int, c: CPU) -> None:
        c.halted = True
        c.fatal_halt = True

    cpu.on_interrupt(_handler)

    with pytest.raises(FatalHaltError):
        cpu.run(100)

    assert cpu.fatal_halt is True


def test_step_raises_when_fatal_halt_newly_fires():
    cpu, mem = _make_cpu()
    mem.write8(PROC_ADDR, 0xCD)
    mem.write8(PROC_ADDR + 1, 0xFE)

    def _handler(int_num: int, c: CPU) -> None:
        c.halted = True
        c.fatal_halt = True

    cpu.on_interrupt(_handler)

    with pytest.raises(FatalHaltError):
        cpu.step()

    assert cpu.fatal_halt is True


def test_run_does_not_raise_when_already_fatally_halted_at_entry():
    """The distinguishing condition is "newly" true -- was_fatal captured at
    entry. An already-halted CPU (the two tests above this one) must stay a
    silent no-op, never raise; only a fatal halt that occurs *during* this
    specific call should."""
    cpu, mem = _make_cpu()
    cpu.fatal_halt = True

    cpu.run(100)  # must not raise
    cpu.step()    # must not raise


def test_register_and_flag_writes_are_frozen_but_reads_still_work():
    cpu, mem = _make_cpu()
    cpu.regs[EAX] = 0x11111111
    cpu.eflags = 0x202
    original_eip = cpu.eip

    cpu.fatal_halt = True

    cpu.regs[EAX] = 0x22222222
    cpu.eip = 0xDEADBEEF
    cpu.eflags = 0

    # Writes were silently refused...
    assert cpu.regs[EAX] == 0x11111111
    assert cpu.eip == original_eip
    assert cpu.eflags == 0x202
    # ...but reads (the whole point -- "grab state, dump") still work fine.
    assert cpu.halted is True
    assert cpu.fatal_halt is True
