"""Unit tests for tew.hardware.scheduler_zig.ZigScheduler.

Port of the remaining Python-facing-contract coverage from
tests/unit/kernel/test_scheduler.py (see ~/.claude/plans/vast-drifting-pike.md,
Stage 5) -- the underlying context-switch/blocking/wake/TLS logic already has
153 colocated Zig-native tests in cpu/src/scheduler.zig; this file exists to
cover what those CAN'T: the Python-level orchestration in scheduler_zig.py
itself (the two-call kernel-tick retry protocol, the fatal_halt/reentrancy
pre-checks that decide whether to call scheduler_pick_next_ready at all, the
tri-state terminate_thread mapping, the _CurrentThreadProxy).

Uses real ZigCPU/ZigMemory throughout, not MagicMock -- see
test_cpu_zig_fatal_halt.py's docstring for why a mocked CPU can't catch this
class of bug (the _py_halted staleness bug found during Stage 4 is a second,
independent confirmation of the same lesson).
"""
from __future__ import annotations

import pytest

from tew.hardware.cpu_zig import ZigCPU as CPU, EAX, ESP
from tew.hardware.memory import Memory
from tew.hardware.scheduler_zig import ZigScheduler, ThreadStatus

# Real THREAD_STACK_BASE (0x08000000) + room for a couple of THREAD_STACK_SIZE
# background-thread stacks, plus TEB_BASE (0x00320000) for TLS/last-error --
# large enough that create_thread's real stack init never goes out of bounds.
MEM_SIZE = 0x08100000

BG_START = 0x9F0000
RET_ADDR = 0x401234


def _make_env() -> tuple[CPU, Memory, ZigScheduler]:
    mem = Memory(MEM_SIZE)
    cpu = CPU(mem)
    sched = ZigScheduler()
    return cpu, mem, sched


class _TickCounter:
    """Stand-in for Kernel -- counts .tick() calls without doing real I/O."""
    def __init__(self) -> None:
        self.calls = 0

    def tick(self) -> None:
        self.calls += 1


# ── Construction / queries ───────────────────────────────────────────────────

class TestConstruction:
    def test_starts_with_no_current_thread(self):
        _, _, sched = _make_env()
        assert sched.current_idx == -1
        assert sched.thread_count == 0

    def test_create_main_thread_sets_current_idx_zero(self):
        _, _, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        assert sched.current_idx == 0
        assert sched.thread_count == 1

    def test_create_thread_increments_thread_count(self):
        _, _, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)
        assert sched.thread_count == 2

    def test_virtual_ticks_ms_round_trip(self):
        _, _, sched = _make_env()
        assert sched.virtual_ticks_ms == 0
        sched.virtual_ticks_ms = 12345
        assert sched.virtual_ticks_ms == 12345


# ── current_thread() proxy ────────────────────────────────────────────────────

class TestCurrentThreadProxy:
    def test_thread_id(self):
        _, _, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        assert sched.current_thread().thread_id == 1000

    def test_wait_timed_out_round_trip(self):
        _, _, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        current = sched.current_thread()
        assert current.wait_timed_out is False
        current.wait_timed_out = True
        assert sched.current_thread().wait_timed_out is True

    def test_raises_without_current_thread(self):
        _, _, sched = _make_env()
        with pytest.raises(RuntimeError):
            sched.current_thread()


# ── status_at_idx (translator for the old scheduler.threads[idx].status) ────

class TestStatusAtIdx:
    def test_returns_status_for_valid_idx(self):
        _, _, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        assert sched.status_at_idx(0) == ThreadStatus.READY

    def test_returns_none_for_out_of_range_idx(self):
        _, _, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        assert sched.status_at_idx(5) is None


# ── Handle-keyed accessors ────────────────────────────────────────────────────

class TestHandleKeyedAccessors:
    def test_get_set_suspended(self):
        _, _, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)
        assert sched.get_suspended(0xBEF0) is False
        sched.set_suspended(0xBEF0, True)
        assert sched.get_suspended(0xBEF0) is True

    def test_get_completed_reflects_dead_status(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)
        assert sched.get_completed(0xBEF0) is False
        result = sched.terminate_thread(cpu, mem, 0xBEF0)  # different thread, no swap
        assert result is True
        assert sched.get_completed(0xBEF0) is True

    def test_get_thread_id(self):
        _, _, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)
        assert sched.get_thread_id(0xBEF0) == 1001
        assert sched.get_thread_id(0xDEADBEEF) is None


# ── TLS bitset ────────────────────────────────────────────────────────────────

class TestTlsBitset:
    def test_alloc_free_allocated_round_trip(self):
        _, _, sched = _make_env()
        assert sched.tls_slot_allocated(3) is False
        sched.tls_alloc_slot(3)
        assert sched.tls_slot_allocated(3) is True
        sched.tls_free_slot(3)
        assert sched.tls_slot_allocated(3) is False


# ── any_runnable ──────────────────────────────────────────────────────────────

class TestAnyRunnable:
    def test_true_with_ready_thread(self):
        _, _, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        assert sched.any_runnable() is True

    def test_false_with_only_dead(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.mark_current_dead(cpu, mem)  # halts -- only thread, none left
        assert sched.any_runnable() is False

    def test_false_with_only_sleeping(self):
        # The old suite poked s.threads[0].status = SLEEPING directly -- no
        # such raw field access exists anymore. Reach the equivalent real
        # state instead: a genuinely SLEEPING thread (via sleep_current,
        # with a second thread present so the single-thread self-reload
        # path -- which resets status straight back to READY -- doesn't
        # apply), with that second thread also taken out of the running
        # pool (suspended) so nothing at all is runnable.
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)
        sched.sleep_current(cpu, mem, return_eip=RET_ADDR, eax_val=0, sleep_ms=50)
        assert sched.status_at_idx(0) == ThreadStatus.SLEEPING
        sched.set_suspended(0xBEF0, True)
        assert sched.any_runnable() is False

    def test_false_with_suspended_ready(self):
        _, _, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.set_suspended(0xBEEF, True)
        assert sched.any_runnable() is False


# ── switch_to / preempt_slice ─────────────────────────────────────────────────

class TestSwitchToAndPreemptSlice:
    def test_switch_to_moves_to_fresh_thread(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)

        result = sched.switch_to(cpu, mem, 1)

        assert result is True
        assert sched.current_idx == 1
        assert cpu.eip == BG_START

    def test_preempt_slice_switches_to_next_ready(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)

        result = sched.preempt_slice(cpu, mem)

        assert result is True
        assert sched.current_idx == 1

    def test_preempt_slice_does_nothing_when_fatally_halted(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)
        cpu.fatal_halt = True

        result = sched.preempt_slice(cpu, mem)

        assert result is False
        assert sched.current_idx == 0


# ── block_current_on_cs (full two-call Python orchestration) ────────────────

class TestBlockCurrentOnCs:
    def test_blocks_and_switches_to_next_thread(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)

        sched.block_current_on_cs(cpu, mem, cs_ptr=0x1234, retry_eip=0x401000)

        assert sched.current_idx == 1
        assert sched.status_at_idx(0) == ThreadStatus.BLOCKED_CS

    def test_retries_when_no_other_thread(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)

        sched.block_current_on_cs(cpu, mem, cs_ptr=0x1234, retry_eip=0x401000)

        assert cpu.eip == 0x401000
        assert sched.status_at_idx(0) == ThreadStatus.READY
        assert cpu.halted is False

    def test_refused_while_reentrant_still_redirects_eip(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)
        sched.enter_reentrant_call()

        sched.block_current_on_cs(cpu, mem, cs_ptr=0x1234, retry_eip=0x401000)

        assert cpu.eip == 0x401000
        assert sched.current_idx == 0  # no swap happened
        assert sched.status_at_idx(0) == ThreadStatus.READY  # never marked blocked


# ── block_current_on_handles ──────────────────────────────────────────────────

class TestBlockCurrentOnHandles:
    def test_blocks_and_switches_to_next_thread(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)

        sched.block_current_on_handles(cpu, mem, frozenset([0x700B]), retry_eip=0x401000, deadline_ms=500)

        assert sched.current_idx == 1
        assert sched.status_at_idx(0) == ThreadStatus.BLOCKED_HANDLES

    def test_retries_when_no_other_thread(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)

        sched.block_current_on_handles(cpu, mem, frozenset([0x700B]), retry_eip=0x401000)

        assert cpu.eip == 0x401000
        assert sched.status_at_idx(0) == ThreadStatus.READY
        assert cpu.halted is False

    def test_refused_while_reentrant_still_redirects_eip(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)
        sched.enter_reentrant_call()

        sched.block_current_on_handles(cpu, mem, frozenset([0x700B]), retry_eip=0x401000)

        assert cpu.eip == 0x401000
        assert sched.current_idx == 0
        assert sched.status_at_idx(0) == ThreadStatus.READY


# ── sleep_current ──────────────────────────────────────────────────────────────

class TestSleepCurrent:
    def test_sleeps_and_switches(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)

        sched.sleep_current(cpu, mem, return_eip=RET_ADDR, eax_val=0, sleep_ms=50)

        assert sched.current_idx == 1

    def test_stays_on_current_if_no_others(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)

        sched.sleep_current(cpu, mem, return_eip=RET_ADDR, eax_val=0, sleep_ms=50)

        assert cpu.eip == RET_ADDR
        assert cpu.regs[EAX] == 0
        assert sched.status_at_idx(0) == ThreadStatus.READY
        assert cpu.halted is False

    def test_does_not_clear_fatal_halt_if_no_others(self):
        # Same regression class as TestSwitchTo's fatal_halt tests in
        # scheduler.zig -- verified again here at the Python orchestration
        # layer specifically because Stage 4 found a real bug (_py_halted
        # staleness) that only a real-CPU test at THIS layer could catch.
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        cpu.halted = True
        cpu.fatal_halt = True

        sched.sleep_current(cpu, mem, return_eip=RET_ADDR, eax_val=0, sleep_ms=50)

        assert cpu.halted is True

    def test_calls_kernel_tick_once_when_nothing_ready(self):
        # The two-call kernel-tick retry protocol (Design Decision 2) is
        # Python-only orchestration -- scheduler.zig's own tests can't see
        # this at all, since scheduler_pick_next_ready has no Kernel
        # awareness by design.
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        counter = _TickCounter()
        sched._kernel = counter

        sched.sleep_current(cpu, mem, return_eip=RET_ADDR, eax_val=0, sleep_ms=50)

        assert counter.calls == 1

    def test_refused_while_reentrant_still_redirects_eip_and_eax(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)
        sched.enter_reentrant_call()

        sched.sleep_current(cpu, mem, return_eip=RET_ADDR, eax_val=0, sleep_ms=50)

        assert cpu.eip == RET_ADDR
        assert cpu.regs[EAX] == 0
        assert sched.current_idx == 0


# ── mark_current_dead ──────────────────────────────────────────────────────────

class TestMarkCurrentDead:
    def test_kills_current_and_switches(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)

        sched.mark_current_dead(cpu, mem)

        assert sched.status_at_idx(0) == ThreadStatus.DEAD
        assert sched.current_idx == 1

    def test_halts_when_no_threads_left(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)

        sched.mark_current_dead(cpu, mem)

        assert cpu.halted is True

    def test_still_swaps_while_reentrant(self):
        # Deliberately NOT guarded by reentrant_depth -- a thread dying
        # mid-nested-call must still hand off the CPU.
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)
        sched.enter_reentrant_call()

        sched.mark_current_dead(cpu, mem)

        assert sched.status_at_idx(0) == ThreadStatus.DEAD
        assert sched.current_idx == 1


# ── terminate_thread (tri-state Optional[bool], Python-level mapping) ───────

class TestTerminateThread:
    def test_returns_none_for_unknown_handle(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        assert sched.terminate_thread(cpu, mem, 0xDEADBEEF) is None

    def test_terminates_different_thread_returns_true_no_switch(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)

        result = sched.terminate_thread(cpu, mem, 0xBEF0)

        assert result is True
        assert sched.status_at_idx(1) == ThreadStatus.DEAD
        assert sched.current_idx == 0  # never switched

    def test_terminating_current_thread_returns_false_and_switches(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)

        result = sched.terminate_thread(cpu, mem, 0xBEEF)  # main thread's own handle

        assert result is False
        assert sched.status_at_idx(0) == ThreadStatus.DEAD
        assert sched.current_idx == 1

    def test_halts_when_terminating_current_thread_and_none_left(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)

        result = sched.terminate_thread(cpu, mem, 0xBEEF)

        assert result is False
        assert cpu.halted is True


# ── unblock_cs / unblock_handle / tick ────────────────────────────────────────

class TestUnblockAndTick:
    def test_unblock_cs_marks_waiting_thread_ready(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)
        sched.block_current_on_cs(cpu, mem, cs_ptr=0x1234, retry_eip=0x401000)  # main -> BLOCKED_CS

        sched.unblock_cs(0x1234)

        assert sched.status_at_idx(0) == ThreadStatus.READY

    def test_unblock_handle_returns_count(self):
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)
        sched.block_current_on_handles(cpu, mem, frozenset([0x700B]), retry_eip=0x401000)

        n = sched.unblock_handle(0x700B)

        assert n == 1
        assert sched.status_at_idx(0) == ThreadStatus.READY

    def test_tick_advances_clock(self):
        _, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.tick(50, mem)
        assert sched.virtual_ticks_ms == 50


# ── Reentrancy orchestration smoke test ───────────────────────────────────────

class TestReentrancyOrchestration:
    def test_refusal_does_not_raise_across_all_guarded_methods(self):
        # "Fail loudly" here means log + record, not a Python exception --
        # this fires deep inside a Win32 stub handler; raising across that
        # boundary is not an option.
        cpu, mem, sched = _make_env()
        sched.create_main_thread(1000, 0xBEEF)
        sched.create_thread(1001, 0xBEF0, BG_START, 0x0)
        sched.enter_reentrant_call()

        sched.switch_to(cpu, mem, 1)
        sched.block_current_on_cs(cpu, mem, cs_ptr=0x1234, retry_eip=0x401000)
        sched.block_current_on_handles(cpu, mem, frozenset([0x700B]), retry_eip=0x401000)
        sched.sleep_current(cpu, mem, return_eip=RET_ADDR, eax_val=0, sleep_ms=50)
        sched.preempt_slice(cpu, mem)
        # no exception raised getting here


# ── pending_threads handle-list shape (_state.py integration) ────────────────

class TestPendingThreadsShape:
    """state.pending_threads is now list[int] (handles), not
    list[ThreadState] -- see kernel32_io.py's _create_thread/_resume_thread/
    _suspend_thread/_get_exit_code_thread, which all now look membership up
    via `handle in state.pending_threads` then dispatch to the handle-keyed
    accessors, matching this pattern exactly."""

    def test_create_thread_then_append_handle(self):
        import os
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
        from tew.api._state import CRTState

        state = CRTState()
        handle = 0xBEF0
        state.scheduler.create_thread(1001, handle, BG_START, 0x0)
        state.pending_threads.append(handle)

        assert handle in state.pending_threads
        assert isinstance(state.pending_threads[0], int)
        assert state.scheduler.get_suspended(handle) is False
        assert state.scheduler.get_completed(handle) is False
