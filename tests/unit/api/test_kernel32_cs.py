"""Tests for the critical-section APIs, run as the guest actually runs them.

Initialize/Delete are Python handlers that write XP's real
RTL_CRITICAL_SECTION (+ RTL_CRITICAL_SECTION_DEBUG); Enter/Leave/TryEnter
are guest x86 code that only traps to Python on contention. Every test
goes through a real ZigCPU and checks the guest struct itself.
"""
from __future__ import annotations

import pytest

from tew.api._state import EventHandle
from tew.hardware.cpu_zig import ESP, FatalHaltError
from tests.unit.api.cs_guest_env import (
    CODE_BASE, CS_ADDR, CS_ADDR2, LOCK_FREE, MAIN_TID, OFF_DEBUG, OFF_LOCK,
    OFF_OWNER, OFF_REC, OFF_SEMAPHORE, OFF_SPIN, STACK_TOP, make_cs_env,
)

DBG_CS    = 0x04  # RTL_CRITICAL_SECTION_DEBUG.CriticalSection
DBG_FLINK = 0x08  # .ProcessLocksList.Flink
DBG_BLINK = 0x0C  # .ProcessLocksList.Blink


@pytest.fixture
def env():
    e = make_cs_env()
    e.call("InitializeCriticalSection", CS_ADDR)
    return e


def assert_free(env, cs=CS_ADDR):
    assert env.field(OFF_LOCK, cs) == LOCK_FREE
    assert env.field(OFF_REC, cs) == 0
    assert env.field(OFF_OWNER, cs) == 0


def leave_with_one_waiter(env):
    """Enter, then count a (simulated) waiter in LockCount the way a
    contending thread's `lock inc` does, then Leave -> the wake path."""
    env.call("EnterCriticalSection", CS_ADDR)
    env.mem.write32(CS_ADDR + OFF_LOCK, 1)
    env.call("LeaveCriticalSection", CS_ADDR)


# ── Initialize / Delete ───────────────────────────────────────────────────────

class TestInitialize:
    def test_writes_the_real_struct_over_garbage(self):
        env = make_cs_env()
        env.mem.load(CS_ADDR, b"\xcd" * 0x18)  # debug-heap fill underneath
        assert env.call("InitializeCriticalSection", CS_ADDR) == 0
        assert_free(env)
        assert env.field(OFF_SEMAPHORE) == 0
        assert env.field(OFF_SPIN) == 0

    def test_debug_info_points_back_at_the_cs(self, env):
        debug = env.field(OFF_DEBUG)
        assert debug != 0
        assert env.mem.read16(debug) == 0             # Type = RTL_CRITSECT_TYPE
        assert env.mem.read32(debug + DBG_CS) == CS_ADDR
        assert env.mem.read32(debug + 0x10) == 0      # EntryCount
        assert env.mem.read32(debug + 0x14) == 0      # ContentionCount

    def test_and_spin_count_returns_true_and_forces_spin_zero(self):
        env = make_cs_env()
        assert env.call("InitializeCriticalSectionAndSpinCount", CS_ADDR, 4000) == 1
        assert env.field(OFF_SPIN) == 0  # one processor: XP keeps no spin count
        assert_free(env)

    def test_debug_blocks_linked_into_one_process_list(self, env):
        env.call("InitializeCriticalSection", CS_ADDR2)
        links1 = env.field(OFF_DEBUG) + DBG_FLINK
        links2 = env.field(OFF_DEBUG, CS_ADDR2) + DBG_FLINK
        head = env.mem.read32(links1 + 4)              # first entry's Blink is the head
        assert env.mem.read32(head) == links1          # head -> 1 -> 2 -> head
        assert env.mem.read32(links1) == links2
        assert env.mem.read32(links2) == head
        assert env.mem.read32(head + 4) == links2      # head's Blink is the tail
        assert env.mem.read32(links2 + 4) == links1


class TestDelete:
    def test_unlinks_frees_and_zeroes(self, env):
        env.call("InitializeCriticalSection", CS_ADDR2)
        debug1 = env.field(OFF_DEBUG)
        links2 = env.field(OFF_DEBUG, CS_ADDR2) + DBG_FLINK
        head = env.mem.read32(debug1 + DBG_BLINK)
        env.call("DeleteCriticalSection", CS_ADDR)
        assert env.mem.read_bytes(CS_ADDR, 0x18) == bytes(0x18)
        assert debug1 not in env.state.heap_alloc_sizes
        assert env.mem.read32(head) == links2 and env.mem.read32(links2 + 4) == head

    def test_second_delete_is_harmless(self, env):
        env.call("DeleteCriticalSection", CS_ADDR)
        env.call("DeleteCriticalSection", CS_ADDR)  # DebugInfo now 0
        assert env.mem.read_bytes(CS_ADDR, 0x18) == bytes(0x18)

    def test_closes_the_lock_semaphore(self, env):
        leave_with_one_waiter(env)  # wake path creates the event
        h = env.field(OFF_SEMAPHORE)
        assert isinstance(env.state.kernel_handle_map.get(h), EventHandle)
        env.call("DeleteCriticalSection", CS_ADDR)
        assert h not in env.state.kernel_handle_map

    def test_refuses_a_semaphore_tew_never_created(self, env):
        env.mem.write32(CS_ADDR + OFF_SEMAPHORE, 0x1234)
        with pytest.raises(FatalHaltError):
            env.call("DeleteCriticalSection", CS_ADDR)
        assert "LockSemaphore" in str(env.cpu.last_error)


# ── Enter / Leave / TryEnter, single thread ──────────────────────────────────

class TestEnterLeave:
    def test_enter_acquires_and_returns_zero(self, env):
        assert env.call("EnterCriticalSection", CS_ADDR) == 0
        assert env.field(OFF_LOCK) == 0
        assert env.field(OFF_REC) == 1
        assert env.field(OFF_OWNER) == MAIN_TID

    def test_uncontended_enter_leave_never_reaches_python(self, env):
        before = env.stubs.get_call_log()
        env.call("EnterCriticalSection", CS_ADDR)
        env.call("LeaveCriticalSection", CS_ADDR)
        assert env.stubs.get_call_log() == before

    def test_recursive_enter_counts_in_lock_count_and_recursion(self, env):
        env.call("EnterCriticalSection", CS_ADDR)
        env.call("EnterCriticalSection", CS_ADDR)
        assert env.field(OFF_REC) == 2
        assert env.field(OFF_LOCK) == 1  # XP counts recursions in LockCount too
        assert env.field(OFF_OWNER) == MAIN_TID

    @pytest.mark.parametrize("depth", [1, 2, 5])
    def test_balanced_enter_leave_frees(self, env, depth):
        for _ in range(depth):
            env.call("EnterCriticalSection", CS_ADDR)
        for i in range(depth - 1):
            assert env.call("LeaveCriticalSection", CS_ADDR) == 0
            assert env.field(OFF_OWNER) == MAIN_TID
            assert env.field(OFF_REC) == depth - 1 - i
        env.call("LeaveCriticalSection", CS_ADDR)
        assert_free(env)
        assert env.field(OFF_SEMAPHORE) == 0  # no waiters -> never created


class TestTryEnter:
    def test_free_cs_acquired(self, env):
        assert env.call("TryEnterCriticalSection", CS_ADDR) == 1
        assert env.field(OFF_LOCK) == 0
        assert env.field(OFF_REC) == 1
        assert env.field(OFF_OWNER) == MAIN_TID

    def test_recursive_try_enter(self, env):
        env.call("TryEnterCriticalSection", CS_ADDR)
        assert env.call("TryEnterCriticalSection", CS_ADDR) == 1
        assert env.field(OFF_REC) == 2
        assert env.field(OFF_LOCK) == 1

    def test_held_by_another_thread_returns_false_untouched(self, env):
        env.mem.write32(CS_ADDR + OFF_LOCK, 0)
        env.mem.write32(CS_ADDR + OFF_REC, 1)
        env.mem.write32(CS_ADDR + OFF_OWNER, 9999)
        assert env.call("TryEnterCriticalSection", CS_ADDR) == 0
        assert env.field(OFF_LOCK) == 0
        assert env.field(OFF_REC) == 1
        assert env.field(OFF_OWNER) == 9999

    def test_try_enter_then_leave_frees(self, env):
        env.call("TryEnterCriticalSection", CS_ADDR)
        env.call("LeaveCriticalSection", CS_ADDR)
        assert_free(env)


# ── Contention: two real threads ──────────────────────────────────────────────

FLAG = 0x00590000


def _asm_call(at: int, target: int, arg: int) -> bytes:
    """push arg; call target  (10 bytes, placed at `at`)."""
    rel = (target - (at + 10)) & 0xFFFFFFFF
    return b"\x68" + arg.to_bytes(4, "little") + b"\xE8" + rel.to_bytes(4, "little")


class TestContention:
    """Main (1000) owns the CS; thread 1001 contends, waits on LockSemaphore,
    and is handed ownership when main leaves -- XP's handoff, no retry."""

    def test_waiter_is_handed_ownership(self, env):
        enter, leave = env.addr("EnterCriticalSection"), env.addr("LeaveCriticalSection")
        main_code = CODE_BASE
        t1_code = CODE_BASE + 0x100

        # main: Enter; hlt; Leave; hlt
        code = _asm_call(main_code, enter, CS_ADDR) + b"\xF4"
        code += _asm_call(main_code + len(code), leave, CS_ADDR) + b"\xF4"
        env.mem.load(main_code, code)
        # t1: Enter; mov dword [FLAG], 1; Leave; hlt
        code = _asm_call(t1_code, enter, CS_ADDR)
        code += b"\xC7\x05" + FLAG.to_bytes(4, "little") + (1).to_bytes(4, "little")
        code += _asm_call(t1_code + len(code), leave, CS_ADDR) + b"\xF4"
        env.mem.load(t1_code, code)

        sched = env.state.scheduler
        sched.create_thread(1001, 0x5001, t1_code, 0)

        # 1. main takes the CS
        env.cpu.regs[ESP] = STACK_TOP - 0x100
        env.cpu.eip = main_code
        env.cpu.halted = False
        env.cpu.run(10_000)
        assert env.field(OFF_OWNER) == MAIN_TID

        # 2. t1 contends: counts itself in LockCount, blocks on the event;
        #    the scheduler resumes main, which leaves -> signals -> hlt
        assert sched.switch_to(env.cpu, env.mem, 1)
        env.cpu.run(10_000)
        assert sched.current_thread_id() == MAIN_TID
        assert env.mem.read32(FLAG) == 0            # t1 hasn't run its body yet
        assert env.field(OFF_LOCK) == 0             # the waiter's own count
        assert env.field(OFF_OWNER) == 0            # released, not yet taken
        event = env.state.kernel_handle_map[env.field(OFF_SEMAPHORE)]
        assert event.signaled and not event.manual_reset

        # 3. t1 resumes at its wait, takes the handoff, runs, leaves
        assert sched.switch_to(env.cpu, env.mem, 1)
        env.cpu.run(10_000)
        assert sched.current_thread_id() == 1001
        assert env.mem.read32(FLAG) == 1
        assert_free(env)
        assert not event.signaled                   # auto-reset, consumed by t1

    def test_signal_before_the_waiter_blocks_is_not_lost(self, env):
        """Waiter preempted between its `lock inc` and the wait: the owner's
        Leave signals first, and the waiter must still take the handoff."""
        leave_with_one_waiter(env)
        event = env.state.kernel_handle_map[env.field(OFF_SEMAPHORE)]
        assert event.signaled
        wait_addr = env.addr("EnterCriticalSection") + 0x2D
        env.cpu.regs[ESP] = STACK_TOP - 0x100
        env.mem.write32(STACK_TOP - 0x100 + 4, CS_ADDR)
        env.cpu.eip = wait_addr
        env.cpu.halted = False                      # still halted from call()'s hlt
        env.cpu.step()                              # the INT 0xFE hook
        assert not event.signaled
        assert env.cpu.eip == wait_addr + 2         # into the acquire path
