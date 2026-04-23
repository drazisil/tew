"""Unit tests for tew.kernel.scheduler.

Uses MagicMock for CPU and Memory so tests are fast and don't require
the Zig shared library or a full PE load.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch
import pytest

from tew.kernel.scheduler import (
    Scheduler,
    ThreadState,
    ThreadStatus,
    THREAD_SENTINEL,
    THREAD_STACK_BASE,
    THREAD_STACK_SIZE,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_cpu(eip: int = 0x401000) -> MagicMock:
    cpu = MagicMock()
    cpu.eip = eip
    cpu.regs = [0] * 8
    cpu.eflags = 0x202
    cpu.halted = False
    saved = MagicMock(name="saved_state")
    cpu.save_state.return_value = saved
    return cpu


def make_memory(cs_owner: int = 0) -> MagicMock:
    mem = MagicMock()
    # Default: CS OwningThread field = 0 (CS free)
    mem.read32.return_value = cs_owner
    return mem


def make_scheduler(tls_slots=None) -> Scheduler:
    return Scheduler(tls_slots=tls_slots or set())


# ── create_main_thread ────────────────────────────────────────────────────────

class TestCreateMainThread:
    def test_adds_thread_at_index_0(self):
        s = make_scheduler()
        t = s.create_main_thread(thread_id=1000, handle=0xBEEF)
        assert len(s.threads) == 1
        assert s.current_idx == 0
        assert t.thread_id == 1000
        assert t.handle == 0xBEEF
        assert t.status == ThreadStatus.READY

    def test_raises_if_called_twice(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        with pytest.raises(AssertionError):
            s.create_main_thread(1001, 0xBEF0)


# ── create_thread ─────────────────────────────────────────────────────────────

class TestCreateThread:
    def test_appends_ready_thread(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        t = s.create_thread(1001, 0xBEF0, start_address=0x9F0000, parameter=0x40001DC0)
        assert t in s.threads
        assert t.status == ThreadStatus.READY
        assert t.saved_state is None

    def test_suspended_flag(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        t = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0, suspended=True)
        assert t.suspended is True

    def test_multiple_threads(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        for i in range(5):
            s.create_thread(1001 + i, 0xBEF0 + i, 0x9F0000, 0x0)
        assert len(s.threads) == 6


# ── _pick_next_ready ──────────────────────────────────────────────────────────

class TestPickNextReady:
    def _sched_with_two(self) -> tuple[Scheduler, MagicMock]:
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        mem = make_memory()
        return s, mem

    def test_picks_background_thread(self):
        s, mem = self._sched_with_two()
        idx = s._pick_next_ready(mem)
        assert idx == 1

    def test_skips_current(self):
        s, mem = self._sched_with_two()
        idx = s._pick_next_ready(mem)
        assert idx != s.current_idx

    def test_skips_dead(self):
        s, mem = self._sched_with_two()
        s.threads[1].status = ThreadStatus.DEAD
        idx = s._pick_next_ready(mem)
        assert idx is None

    def test_skips_suspended(self):
        s, mem = self._sched_with_two()
        s.threads[1].suspended = True
        idx = s._pick_next_ready(mem)
        assert idx is None

    def test_skips_sleeping_not_due(self):
        # Fallback: when no READY thread exists, _pick_next_ready wakes the
        # earliest sleeping thread early rather than returning None.
        s, mem = self._sched_with_two()
        s.threads[1].status = ThreadStatus.SLEEPING
        s.threads[1].sleep_until_ms = 9999
        s.virtual_ticks_ms = 0
        idx = s._pick_next_ready(mem)
        assert idx == 1
        assert s.threads[1].status == ThreadStatus.READY

    def test_wakes_sleeping_when_due(self):
        s, mem = self._sched_with_two()
        s.threads[1].status = ThreadStatus.SLEEPING
        s.threads[1].sleep_until_ms = 100
        s.virtual_ticks_ms = 100
        idx = s._pick_next_ready(mem)
        assert idx == 1
        assert s.threads[1].status == ThreadStatus.READY

    def test_skips_blocked_cs_owner_nonzero(self):
        s, mem = self._sched_with_two()
        s.threads[1].status = ThreadStatus.BLOCKED_CS
        s.threads[1].waiting_on_cs = 0x1234
        mem.read32.return_value = 0x3E9   # non-zero owner
        idx = s._pick_next_ready(mem)
        assert idx is None

    def test_unblocks_cs_when_free(self):
        s, mem = self._sched_with_two()
        s.threads[1].status = ThreadStatus.BLOCKED_CS
        s.threads[1].waiting_on_cs = 0x1234
        mem.read32.return_value = 0   # CS free
        idx = s._pick_next_ready(mem)
        assert idx == 1
        assert s.threads[1].status == ThreadStatus.READY
        assert s.threads[1].waiting_on_cs is None

    def test_falls_back_to_blocked_handles(self):
        # Fallback pass 2: when no READY or SLEEPING thread exists, wake a
        # BLOCKED_HANDLES thread so it can retry its wait from retry_eip.
        # The heartbeat advances virtual time between cpu.run batches, so
        # handles may be signaled before the retry fires.
        s, mem = self._sched_with_two()
        s.threads[1].status = ThreadStatus.BLOCKED_HANDLES
        s.threads[1].waiting_on_handles = frozenset([0x700B])
        s.threads[1].wait_deadline_ms = None
        idx = s._pick_next_ready(mem)
        assert idx == 1
        assert s.threads[1].status == ThreadStatus.READY

    def test_unblocks_handles_on_deadline(self):
        s, mem = self._sched_with_two()
        s.threads[1].status = ThreadStatus.BLOCKED_HANDLES
        s.threads[1].waiting_on_handles = frozenset([0x700B])
        s.threads[1].wait_deadline_ms = 50
        s.virtual_ticks_ms = 50
        idx = s._pick_next_ready(mem)
        assert idx == 1
        assert s.threads[1].wait_timed_out is True
        assert s.threads[1].status == ThreadStatus.READY

    def test_round_robin(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        s.create_thread(1002, 0xBEF1, 0x9F0000, 0x0)
        mem = make_memory()
        # After scheduling thread 1, round-robin should next pick thread 2
        s._last_scheduled_idx = 1
        idx = s._pick_next_ready(mem)
        assert idx == 2


# ── switch_to ─────────────────────────────────────────────────────────────────

class TestSwitchTo:
    def test_saves_current_and_loads_next(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        cpu = make_cpu()
        mem = make_memory()
        saved = cpu.save_state.return_value

        s.switch_to(cpu, mem, 1)

        cpu.save_state.assert_called_once()
        assert s.threads[0].saved_state is saved
        assert s.current_idx == 1
        # new thread has no saved state → _init_thread_stack was called
        assert cpu.eip == 0x9F0000

    def test_loads_saved_state_on_resume(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        saved_bg = MagicMock(name="bg_saved")
        bg.saved_state = saved_bg
        cpu = make_cpu()
        mem = make_memory()

        s.switch_to(cpu, mem, 1)

        cpu.restore_state.assert_called_once_with(saved_bg)
        assert s.current_idx == 1

    def test_clears_cpu_halted_after_load(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.saved_state = MagicMock()
        cpu = make_cpu()
        cpu.halted = True
        mem = make_memory()

        s.switch_to(cpu, mem, 1)

        assert cpu.halted is False

    def test_does_not_save_dead_thread(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        s.threads[0].status = ThreadStatus.DEAD
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.saved_state = MagicMock()
        cpu = make_cpu()
        mem = make_memory()

        s.switch_to(cpu, mem, 1)

        cpu.save_state.assert_not_called()


# ── block_current_on_cs ───────────────────────────────────────────────────────

class TestBlockCurrentOnCs:
    def test_sets_status_and_retry_eip(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        s.threads[1].saved_state = MagicMock()
        cpu = make_cpu(eip=0x401002)   # past INT 0xFE
        mem = make_memory()

        s.block_current_on_cs(cpu, mem, cs_ptr=0x1234, retry_eip=0x401000)

        assert s.threads[0].status == ThreadStatus.BLOCKED_CS
        assert s.threads[0].waiting_on_cs == 0x1234
        # saved EIP should be the retry address
        assert cpu.eip == 0x401000
        cpu.save_state.assert_called_once()

    def test_switches_to_next_thread(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.saved_state = MagicMock()
        cpu = make_cpu()
        mem = make_memory()

        s.block_current_on_cs(cpu, mem, cs_ptr=0x1234, retry_eip=0x401000)

        assert s.current_idx == 1

    def test_retries_on_no_other_thread(self):
        # When no other thread is runnable, the current thread is reloaded at
        # retry_eip so the CS wait retries rather than halting the CPU.
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        cpu = make_cpu()
        mem = make_memory()

        s.block_current_on_cs(cpu, mem, cs_ptr=0x1234, retry_eip=0x401000)

        assert cpu.halted is not True
        assert s.threads[0].status == ThreadStatus.READY


# ── block_current_on_handles ──────────────────────────────────────────────────

class TestBlockCurrentOnHandles:
    def test_sets_status_and_handles(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.saved_state = MagicMock()
        cpu = make_cpu()
        mem = make_memory()

        s.block_current_on_handles(cpu, mem,
            handles=frozenset([0x700B]),
            retry_eip=0x401000,
            deadline_ms=500)

        t = s.threads[0]
        assert t.status == ThreadStatus.BLOCKED_HANDLES
        assert t.waiting_on_handles == frozenset([0x700B])
        assert t.wait_deadline_ms == 500
        assert t.wait_timed_out is False

    def test_switches_to_next(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.saved_state = MagicMock()
        cpu = make_cpu()
        mem = make_memory()

        s.block_current_on_handles(cpu, mem, frozenset([0x700B]), 0x401000)

        assert s.current_idx == 1

    def test_retries_on_no_other_thread(self):
        # When no other thread is runnable, the current thread is reloaded at
        # retry_eip so the wait retries rather than halting the CPU.
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        cpu = make_cpu()
        mem = make_memory()

        s.block_current_on_handles(cpu, mem, frozenset([0x700B]), 0x401000)

        assert cpu.halted is not True
        assert s.threads[0].status == ThreadStatus.READY


# ── sleep_current ─────────────────────────────────────────────────────────────

class TestSleepCurrent:
    def test_sets_sleeping_status(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.saved_state = MagicMock()
        cpu = make_cpu()
        mem = make_memory()
        s.virtual_ticks_ms = 100

        s.sleep_current(cpu, mem, return_eip=0x401010, eax_val=0, sleep_ms=50)

        assert s.threads[0].status == ThreadStatus.SLEEPING
        assert s.threads[0].sleep_until_ms == 150

    def test_saves_return_eip_and_eax(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.saved_state = MagicMock()
        cpu = make_cpu()
        mem = make_memory()

        s.sleep_current(cpu, mem, return_eip=0x401010, eax_val=0, sleep_ms=10)

        assert cpu.eip == 0x401010
        assert cpu.regs[0] == 0   # EAX = 0

    def test_stays_on_current_if_no_others(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        saved_main = MagicMock(name="main_saved")
        cpu = make_cpu()
        cpu.save_state.return_value = saved_main
        mem = make_memory()

        s.sleep_current(cpu, mem, return_eip=0x401010, eax_val=0, sleep_ms=50)

        # Thread status reset to READY, state restored
        assert s.threads[0].status == ThreadStatus.READY
        cpu.restore_state.assert_called_once_with(saved_main)
        assert cpu.halted is False

    def test_switches_to_next_thread(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.saved_state = MagicMock()
        cpu = make_cpu()
        mem = make_memory()

        s.sleep_current(cpu, mem, return_eip=0x401010, eax_val=0, sleep_ms=50)

        assert s.current_idx == 1


# ── mark_current_dead ─────────────────────────────────────────────────────────

class TestMarkCurrentDead:
    def test_sets_dead_status(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.saved_state = MagicMock()
        cpu = make_cpu()
        mem = make_memory()
        s.current_idx = 1
        s._last_scheduled_idx = 1

        s.mark_current_dead(cpu, mem)

        assert s.threads[1].status == ThreadStatus.DEAD
        assert s.threads[1].saved_state is None

    def test_switches_to_next(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.saved_state = MagicMock()
        cpu = make_cpu()
        mem = make_memory()
        # main thread has a saved state so it can be restored
        s.threads[0].saved_state = MagicMock()
        s.current_idx = 1
        s._last_scheduled_idx = 1

        s.mark_current_dead(cpu, mem)

        assert s.current_idx == 0

    def test_halts_when_no_threads_left(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        cpu = make_cpu()
        mem = make_memory()

        s.mark_current_dead(cpu, mem)

        assert cpu.halted is True


# ── unblock_cs ────────────────────────────────────────────────────────────────

class TestUnblockCs:
    def test_marks_waiting_thread_ready(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.status = ThreadStatus.BLOCKED_CS
        bg.waiting_on_cs = 0x1234

        s.unblock_cs(0x1234)

        assert bg.status == ThreadStatus.READY
        assert bg.waiting_on_cs is None

    def test_does_not_affect_other_cs(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.status = ThreadStatus.BLOCKED_CS
        bg.waiting_on_cs = 0x5678

        s.unblock_cs(0x1234)

        assert bg.status == ThreadStatus.BLOCKED_CS

    def test_unblocks_multiple_waiters(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        for i in range(3):
            t = s.create_thread(1001 + i, 0xBEF0 + i, 0x9F0000, 0x0)
            t.status = ThreadStatus.BLOCKED_CS
            t.waiting_on_cs = 0x1234

        s.unblock_cs(0x1234)

        for t in s.threads[1:]:
            assert t.status == ThreadStatus.READY


# ── unblock_handle ────────────────────────────────────────────────────────────

class TestUnblockHandle:
    def test_marks_waiting_thread_ready(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.status = ThreadStatus.BLOCKED_HANDLES
        bg.waiting_on_handles = frozenset([0x700B])

        s.unblock_handle(0x700B)

        assert bg.status == ThreadStatus.READY
        assert bg.waiting_on_handles is None

    def test_does_not_affect_different_handle(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.status = ThreadStatus.BLOCKED_HANDLES
        bg.waiting_on_handles = frozenset([0x700B])

        s.unblock_handle(0x700C)

        assert bg.status == ThreadStatus.BLOCKED_HANDLES

    def test_does_not_affect_non_blocked_thread(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.status = ThreadStatus.READY

        s.unblock_handle(0x700B)  # should not crash

        assert bg.status == ThreadStatus.READY


# ── tick ──────────────────────────────────────────────────────────────────────

class TestTick:
    def test_advances_clock(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        mem = make_memory()
        s.virtual_ticks_ms = 0
        s.tick(50, mem)
        assert s.virtual_ticks_ms == 50

    def test_wakes_sleeping_thread(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.status = ThreadStatus.SLEEPING
        bg.sleep_until_ms = 100
        mem = make_memory()

        s.virtual_ticks_ms = 99
        s.tick(1, mem)

        assert bg.status == ThreadStatus.READY

    def test_does_not_wake_sleeping_before_due(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.status = ThreadStatus.SLEEPING
        bg.sleep_until_ms = 200
        mem = make_memory()

        s.virtual_ticks_ms = 0
        s.tick(100, mem)

        assert bg.status == ThreadStatus.SLEEPING

    def test_expires_wait_deadline(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.status = ThreadStatus.BLOCKED_HANDLES
        bg.waiting_on_handles = frozenset([0x700B])
        bg.wait_deadline_ms = 100
        mem = make_memory()

        s.virtual_ticks_ms = 99
        s.tick(1, mem)

        assert bg.status == ThreadStatus.READY
        assert bg.wait_timed_out is True
        assert bg.wait_deadline_ms is None
        assert bg.waiting_on_handles is None

    def test_does_not_expire_before_deadline(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        bg = s.create_thread(1001, 0xBEF0, 0x9F0000, 0x0)
        bg.status = ThreadStatus.BLOCKED_HANDLES
        bg.waiting_on_handles = frozenset([0x700B])
        bg.wait_deadline_ms = 500
        mem = make_memory()

        s.virtual_ticks_ms = 0
        s.tick(100, mem)

        assert bg.status == ThreadStatus.BLOCKED_HANDLES
        assert bg.wait_timed_out is False

    def test_clock_wraps_at_32bit(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        mem = make_memory()
        s.virtual_ticks_ms = 0xFFFFFFFF
        s.tick(1, mem)
        assert s.virtual_ticks_ms == 0


# ── ThreadState.completed property ───────────────────────────────────────────

class TestThreadStateCompleted:
    def test_completed_reflects_dead_status(self):
        t = ThreadState(thread_id=1, handle=1, start_address=0, parameter=0)
        assert t.completed is False
        t.status = ThreadStatus.DEAD
        assert t.completed is True

    def test_setting_completed_sets_dead(self):
        t = ThreadState(thread_id=1, handle=1, start_address=0, parameter=0)
        t.completed = True
        assert t.status == ThreadStatus.DEAD


# ── any_runnable ──────────────────────────────────────────────────────────────

class TestAnyRunnable:
    def test_true_with_ready_thread(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        assert s.any_runnable() is True

    def test_false_with_only_dead(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        s.threads[0].status = ThreadStatus.DEAD
        assert s.any_runnable() is False

    def test_false_with_only_sleeping(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        s.threads[0].status = ThreadStatus.SLEEPING
        assert s.any_runnable() is False

    def test_false_with_suspended_ready(self):
        s = make_scheduler()
        s.create_main_thread(1000, 0xBEEF)
        s.threads[0].suspended = True
        assert s.any_runnable() is False


# ── _init_thread_stack ────────────────────────────────────────────────────────

class TestInitThreadStack:
    def test_sets_eip_to_start_address(self):
        s = make_scheduler()
        t = ThreadState(thread_id=1, handle=1, start_address=0x9F5800, parameter=0x40001DC0)
        cpu = make_cpu()
        mem = make_memory()

        s._init_thread_stack(cpu, mem, t)

        assert cpu.eip == 0x9F5800

    def test_writes_sentinel_on_stack(self):
        s = make_scheduler()
        t = ThreadState(thread_id=1, handle=1, start_address=0x9F5800, parameter=0x12345678)
        cpu = make_cpu()
        mem = make_memory()

        s._init_thread_stack(cpu, mem, t)

        # Sentinel is written 4 bytes below the parameter
        calls = mem.write32.call_args_list
        written_values = [c[0][1] for c in calls]
        assert THREAD_SENTINEL in written_values
        assert 0x12345678 in written_values

    def test_advances_stack_next(self):
        s = make_scheduler()
        initial = s._thread_stack_next
        t = ThreadState(thread_id=1, handle=1, start_address=0, parameter=0)
        cpu = make_cpu()
        mem = make_memory()

        s._init_thread_stack(cpu, mem, t)

        assert s._thread_stack_next == initial + THREAD_STACK_SIZE
