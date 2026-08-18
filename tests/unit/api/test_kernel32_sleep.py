"""Tests for kernel32.dll's Sleep handler (the Scheduler-driven half of
kernel32_system.py). Uses a real ZigCPU, not a MagicMock -- since the
scheduler-to-Zig port (see ~/.claude/plans/vast-drifting-pike.md, Stage 4),
ZigScheduler.sleep_current genuinely reads/writes CPU register state
through a real *CpuState handle (cpu.native_handle) over ctypes, which a
MagicMock cannot stand in for. Memory is already Zig-backed (tew.hardware.
memory.Memory is a ZigMemory alias), so only the CPU side needed to change.
"""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.kernel32_system import register_kernel32_system_handlers
from tew.api.win32_handlers import pending_timers
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import ZigCPU as CPU, EAX, ESP
from tew.hardware.scheduler_zig import ThreadStatus


MEM_SIZE = 4 * 1024 * 1024
STACK    = 0x200000
RET_ADDR = 0x401234


@pytest.fixture(autouse=True)
def _clear_pending_timers():
    """pending_timers is a module-level global shared with win32_handlers.py
    with no per-test reset hook — clear it before and after every test."""
    pending_timers.clear()
    yield
    pending_timers.clear()


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs_h: dict = {}

    class _StubHandlers:
        def register_handler(self, dll, name, fn):
            stubs_h[(dll, name)] = fn

        def get(self, dll, name):
            return stubs_h[(dll, name)]

    stubs = _StubHandlers()
    register_kernel32_system_handlers(stubs, mem, state)
    cpu = CPU(mem)
    return cpu, mem, state, stubs


def call_sleep(stubs, cpu, mem, dw_ms):
    cpu.regs[ESP] = STACK
    mem.write32(STACK, RET_ADDR)
    mem.write32(STACK + 4, dw_ms)
    stubs.get("kernel32.dll", "Sleep")(cpu)


class TestSleep:

    def test_advances_virtual_ticks(self, env):
        cpu, mem, state, stubs = env
        before = state.virtual_ticks_ms
        call_sleep(stubs, cpu, mem, 50)
        assert state.virtual_ticks_ms == before + 50

    def test_sets_eip_to_return_address(self, env):
        cpu, mem, state, stubs = env
        call_sleep(stubs, cpu, mem, 10)
        assert cpu.eip == RET_ADDR

    def test_single_thread_wakes_up_immediately(self, env):
        cpu, mem, state, stubs = env
        call_sleep(stubs, cpu, mem, 10)
        assert state.scheduler.status_at_idx(state.scheduler.current_idx) == ThreadStatus.READY

    def test_single_thread_clears_halted(self, env):
        cpu, mem, state, stubs = env
        cpu.halted = True
        call_sleep(stubs, cpu, mem, 10)
        assert cpu.halted is False

    def test_zero_ms_yield_resolves_cleanly(self, env):
        cpu, mem, state, stubs = env
        before = state.virtual_ticks_ms
        call_sleep(stubs, cpu, mem, 0)
        assert state.virtual_ticks_ms == before
        assert state.scheduler.status_at_idx(state.scheduler.current_idx) == ThreadStatus.READY

    def test_pops_stack_args(self, env):
        cpu, mem, state, stubs = env
        call_sleep(stubs, cpu, mem, 10)
        assert cpu.regs[ESP] == STACK + 8

    def test_no_crash_with_empty_pending_timers(self, env):
        cpu, mem, state, stubs = env
        assert pending_timers == {}
        call_sleep(stubs, cpu, mem, 5)  # must not raise
