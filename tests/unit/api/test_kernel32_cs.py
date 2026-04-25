"""Tests for Critical Section handlers: Init, Enter, Leave, TryEnter, Delete."""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.kernel32_sync import register_kernel32_sync_handlers
from tew.hardware.memory import Memory
from tew.hardware.cpu import EAX, ESP


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
        self.eip = 0x401002  # past a 2-byte INT stub


MEM_SIZE = 4 * 1024 * 1024
STACK    = 0x200000
CS_ADDR  = 0x300000   # CRITICAL_SECTION struct in emulator memory

# CS memory layout offsets
OFF_LOCK      = 0x04  # LockCount      (-1 = free)
OFF_REC       = 0x08  # RecursionCount
OFF_OWNER     = 0x0C  # OwningThread
LOCK_FREE     = 0xFFFFFFFF
MAIN_TID      = 1000  # CRTState creates main thread with TID 1000
OTHER_TID     = 9999  # a TID that is not the current thread


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_kernel32_sync_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # return address
    return cpu, mem, state, stubs


def cs_call(stubs, cpu, mem, name, cs_ptr=CS_ADDR):
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4, cs_ptr)
    stubs.get("kernel32.dll", name)(cpu)
    return cpu.regs[EAX]


def cs_field(mem, offset) -> int:
    return mem.read32(CS_ADDR + offset)


# ── InitializeCriticalSection ─────────────────────────────────────────────────

class TestInitializeCriticalSection:

    def test_lock_count_set_to_minus_one(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        assert cs_field(mem, OFF_LOCK) == LOCK_FREE

    def test_recursion_count_zero(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        assert cs_field(mem, OFF_REC) == 0

    def test_owner_zero(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        assert cs_field(mem, OFF_OWNER) == 0

    def test_spin_count_zero(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        assert mem.read32(CS_ADDR + 0x14) == 0


class TestInitializeCriticalSectionAndSpinCount:

    def test_returns_true(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, CS_ADDR)
        mem.write32(STACK + 8, 4000)
        stubs.get("kernel32.dll", "InitializeCriticalSectionAndSpinCount")(cpu)
        assert cpu.regs[EAX] == 1

    def test_spin_count_stored(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, CS_ADDR)
        mem.write32(STACK + 8, 4000)
        stubs.get("kernel32.dll", "InitializeCriticalSectionAndSpinCount")(cpu)
        assert mem.read32(CS_ADDR + 0x14) == 4000

    def test_lock_count_still_free(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, CS_ADDR)
        mem.write32(STACK + 8, 4000)
        stubs.get("kernel32.dll", "InitializeCriticalSectionAndSpinCount")(cpu)
        assert cs_field(mem, OFF_LOCK) == LOCK_FREE


# ── EnterCriticalSection ──────────────────────────────────────────────────────

class TestEnterCriticalSection:

    def test_acquire_free_cs_sets_lock_count_zero(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        assert cs_field(mem, OFF_LOCK) == 0

    def test_acquire_sets_recursion_count_one(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        assert cs_field(mem, OFF_REC) == 1

    def test_acquire_sets_owner_to_current_tid(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        assert cs_field(mem, OFF_OWNER) == MAIN_TID

    def test_recursive_entry_deepens_recursion_count(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        assert cs_field(mem, OFF_REC) == 2

    def test_recursive_entry_does_not_change_owner(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        assert cs_field(mem, OFF_OWNER) == MAIN_TID


# ── LeaveCriticalSection ──────────────────────────────────────────────────────

class TestLeaveCriticalSection:

    def test_leave_resets_to_free(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "LeaveCriticalSection")
        assert cs_field(mem, OFF_LOCK)  == LOCK_FREE
        assert cs_field(mem, OFF_REC)   == 0
        assert cs_field(mem, OFF_OWNER) == 0

    def test_leave_recursive_decrements_recursion_count(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "LeaveCriticalSection")
        assert cs_field(mem, OFF_REC) == 1

    def test_leave_recursive_does_not_release_until_balanced(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "LeaveCriticalSection")
        # Still held after one leave
        assert cs_field(mem, OFF_OWNER) == MAIN_TID
        assert cs_field(mem, OFF_LOCK) != LOCK_FREE

    def test_fully_balanced_enter_leave_is_free(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "LeaveCriticalSection")
        cs_call(stubs, cpu, mem, "LeaveCriticalSection")
        assert cs_field(mem, OFF_LOCK) == LOCK_FREE


# ── TryEnterCriticalSection ───────────────────────────────────────────────────

class TestTryEnterCriticalSection:

    def test_acquire_free_cs_returns_true(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        result = cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert result == 1

    def test_acquire_free_cs_sets_owner(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert cs_field(mem, OFF_OWNER) == MAIN_TID

    def test_acquire_free_cs_sets_recursion_one(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert cs_field(mem, OFF_REC) == 1

    def test_acquire_free_cs_sets_lock_count_zero(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert cs_field(mem, OFF_LOCK) == 0

    def test_recursive_try_enter_returns_true(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        result = cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert result == 1

    def test_recursive_try_enter_deepens_recursion(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert cs_field(mem, OFF_REC) == 2

    def test_contested_cs_returns_false(self, env):
        """Simulate CS held by another thread; TryEnter must not block, returns FALSE."""
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        # Manually mark CS as held by a different thread
        mem.write32(CS_ADDR + OFF_LOCK,  0)          # LockCount = 0 (held)
        mem.write32(CS_ADDR + OFF_REC,   1)          # RecursionCount = 1
        mem.write32(CS_ADDR + OFF_OWNER, OTHER_TID)  # owned by someone else
        result = cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert result == 0

    def test_contested_cs_does_not_modify_state(self, env):
        """TryEnter on a held CS must leave the CS state unchanged."""
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        mem.write32(CS_ADDR + OFF_LOCK,  0)
        mem.write32(CS_ADDR + OFF_REC,   1)
        mem.write32(CS_ADDR + OFF_OWNER, OTHER_TID)
        cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert cs_field(mem, OFF_OWNER) == OTHER_TID
        assert cs_field(mem, OFF_REC)   == 1
        assert cs_field(mem, OFF_LOCK)  == 0

    def test_try_enter_after_leave_succeeds(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        cs_call(stubs, cpu, mem, "LeaveCriticalSection")
        result = cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert result == 1
