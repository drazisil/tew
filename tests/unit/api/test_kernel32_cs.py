"""Tests for Critical Section handlers: Init, Enter, Leave, TryEnter, Delete."""
from __future__ import annotations

import pytest

from tew.api._state import CRTState, CriticalSectionEntry
from tew.api.kernel32_sync import register_kernel32_sync_handlers
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP


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
CS_ADDR2 = 0x300040   # a second one
HEAP_START = 0x380000 # RTL_CRITICAL_SECTION_DEBUG blocks come from here

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
    state.memory = mem
    state.next_heap_alloc = HEAP_START  # default (0x04000000) is past MEM_SIZE here
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


def cs_field(state, offset, cs_ptr=CS_ADDR) -> int:
    # 2026-09-14: CS state (LockCount/RecursionCount/OwningThread) moved out
    # of guest memory into state.critical_sections -- see kernel32_sync.py.
    attr = {OFF_LOCK: "lock_count", OFF_REC: "recursion_count", OFF_OWNER: "owner_tid"}[offset]
    return getattr(state.critical_sections[cs_ptr], attr)


# ── InitializeCriticalSection ─────────────────────────────────────────────────

class TestInitializeCriticalSection:

    def test_lock_count_set_to_minus_one(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        assert cs_field(state, OFF_LOCK) == LOCK_FREE

    def test_recursion_count_zero(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        assert cs_field(state, OFF_REC) == 0

    def test_owner_zero(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        assert cs_field(state, OFF_OWNER) == 0

    def test_init_creates_cs_entry(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        assert CS_ADDR in state.critical_sections


class TestInitializeCriticalSectionAndSpinCount:

    def test_returns_true(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, CS_ADDR)
        mem.write32(STACK + 8, 4000)
        stubs.get("kernel32.dll", "InitializeCriticalSectionAndSpinCount")(cpu)
        assert cpu.regs[EAX] == 1

    def test_spin_count_argument_has_no_effect(self, env):
        # spin_count is real Windows' own spin-wait tuning; this emulation
        # blocks cooperatively instead of spinning, so it's read but not
        # stored anywhere -- this just confirms the call still succeeds and
        # initializes the CS normally regardless of the value passed.
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, CS_ADDR)
        mem.write32(STACK + 8, 4000)
        stubs.get("kernel32.dll", "InitializeCriticalSectionAndSpinCount")(cpu)
        assert CS_ADDR in state.critical_sections

    def test_lock_count_still_free(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, CS_ADDR)
        mem.write32(STACK + 8, 4000)
        stubs.get("kernel32.dll", "InitializeCriticalSectionAndSpinCount")(cpu)
        assert cs_field(state, OFF_LOCK) == LOCK_FREE


# ── Guest-side struct (XP ntdll layout) ───────────────────────────────────────

OFF_DEBUG     = 0x00  # DebugInfo -> RTL_CRITICAL_SECTION_DEBUG
OFF_SEMAPHORE = 0x10  # LockSemaphore
OFF_SPIN      = 0x14  # SpinCount
DBG_CS        = 0x04  # RTL_CRITICAL_SECTION_DEBUG.CriticalSection
DBG_FLINK     = 0x08  # .ProcessLocksList.Flink
DBG_BLINK     = 0x0C  # .ProcessLocksList.Blink


class TestGuestStruct:
    def test_init_writes_the_real_struct(self, env):
        cpu, mem, state, stubs = env
        mem.load(CS_ADDR, b"\xcd" * 0x18)  # debug-heap garbage underneath
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        assert mem.read32(CS_ADDR + OFF_LOCK) == LOCK_FREE
        assert mem.read32(CS_ADDR + OFF_REC) == 0
        assert mem.read32(CS_ADDR + OFF_OWNER) == 0
        assert mem.read32(CS_ADDR + OFF_SEMAPHORE) == 0
        assert mem.read32(CS_ADDR + OFF_SPIN) == 0

    def test_debug_info_points_back_at_the_cs(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        debug = mem.read32(CS_ADDR + OFF_DEBUG)
        assert debug != 0
        assert mem.read16(debug) == 0            # Type = RTL_CRITSECT_TYPE
        assert mem.read32(debug + DBG_CS) == CS_ADDR
        assert mem.read32(debug + 0x10) == 0     # EntryCount
        assert mem.read32(debug + 0x14) == 0     # ContentionCount

    def test_spin_count_forced_to_zero_on_one_processor(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, CS_ADDR)
        mem.write32(STACK + 8, 4000)
        stubs.get("kernel32.dll", "InitializeCriticalSectionAndSpinCount")(cpu)
        assert mem.read32(CS_ADDR + OFF_SPIN) == 0

    def test_debug_blocks_linked_into_one_process_list(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection", CS_ADDR)
        cs_call(stubs, cpu, mem, "InitializeCriticalSection", CS_ADDR2)
        links1 = mem.read32(CS_ADDR + OFF_DEBUG) + DBG_FLINK
        links2 = mem.read32(CS_ADDR2 + OFF_DEBUG) + DBG_FLINK
        head = mem.read32(links1 + 4)             # first entry's Blink is the head
        assert mem.read32(head) == links1         # head -> 1 -> 2 -> head
        assert mem.read32(links1) == links2
        assert mem.read32(links2) == head
        assert mem.read32(head + 4) == links2     # head's Blink is the tail
        assert mem.read32(links2 + 4) == links1

    def test_delete_unlinks_frees_and_zeroes(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection", CS_ADDR)
        cs_call(stubs, cpu, mem, "InitializeCriticalSection", CS_ADDR2)
        debug1 = mem.read32(CS_ADDR + OFF_DEBUG)
        links2 = mem.read32(CS_ADDR2 + OFF_DEBUG) + DBG_FLINK
        head = mem.read32(debug1 + DBG_BLINK)
        cs_call(stubs, cpu, mem, "DeleteCriticalSection", CS_ADDR)
        assert mem.read_bytes(CS_ADDR, 0x18) == bytes(0x18)
        assert debug1 not in state.heap_alloc_sizes
        assert CS_ADDR not in state.critical_sections
        assert mem.read32(head) == links2 and mem.read32(links2 + 4) == head

    def test_second_delete_is_harmless(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "DeleteCriticalSection")
        cs_call(stubs, cpu, mem, "DeleteCriticalSection")  # DebugInfo now 0
        assert mem.read_bytes(CS_ADDR, 0x18) == bytes(0x18)

    def test_delete_refuses_a_semaphore_tew_never_created(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        mem.write32(CS_ADDR + OFF_SEMAPHORE, 0x1234)
        with pytest.raises(RuntimeError, match="LockSemaphore"):
            cs_call(stubs, cpu, mem, "DeleteCriticalSection")


# ── EnterCriticalSection ──────────────────────────────────────────────────────

class TestEnterCriticalSection:

    def test_acquire_free_cs_sets_lock_count_zero(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        assert cs_field(state, OFF_LOCK) == 0

    def test_acquire_sets_recursion_count_one(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        assert cs_field(state, OFF_REC) == 1

    def test_acquire_sets_owner_to_current_tid(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        assert cs_field(state, OFF_OWNER) == MAIN_TID

    def test_recursive_entry_deepens_recursion_count(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        assert cs_field(state, OFF_REC) == 2

    def test_recursive_entry_does_not_change_owner(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        assert cs_field(state, OFF_OWNER) == MAIN_TID


# ── LeaveCriticalSection ──────────────────────────────────────────────────────

class TestLeaveCriticalSection:

    def test_leave_resets_to_free(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "LeaveCriticalSection")
        assert cs_field(state, OFF_LOCK)  == LOCK_FREE
        assert cs_field(state, OFF_REC)   == 0
        assert cs_field(state, OFF_OWNER) == 0

    def test_leave_recursive_decrements_recursion_count(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "LeaveCriticalSection")
        assert cs_field(state, OFF_REC) == 1

    def test_leave_recursive_does_not_release_until_balanced(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "LeaveCriticalSection")
        # Still held after one leave
        assert cs_field(state, OFF_OWNER) == MAIN_TID
        assert cs_field(state, OFF_LOCK) != LOCK_FREE

    def test_fully_balanced_enter_leave_is_free(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "EnterCriticalSection")
        cs_call(stubs, cpu, mem, "LeaveCriticalSection")
        cs_call(stubs, cpu, mem, "LeaveCriticalSection")
        assert cs_field(state, OFF_LOCK) == LOCK_FREE


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
        assert cs_field(state, OFF_OWNER) == MAIN_TID

    def test_acquire_free_cs_sets_recursion_one(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert cs_field(state, OFF_REC) == 1

    def test_acquire_free_cs_sets_lock_count_zero(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert cs_field(state, OFF_LOCK) == 0

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
        assert cs_field(state, OFF_REC) == 2

    def test_contested_cs_returns_false(self, env):
        """Simulate CS held by another thread; TryEnter must not block, returns FALSE."""
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        # Manually mark CS as held by a different thread
        state.critical_sections[CS_ADDR] = CriticalSectionEntry(lock_count=0, recursion_count=1, owner_tid=OTHER_TID)
        result = cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert result == 0

    def test_contested_cs_does_not_modify_state(self, env):
        """TryEnter on a held CS must leave the CS state unchanged."""
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        state.critical_sections[CS_ADDR] = CriticalSectionEntry(lock_count=0, recursion_count=1, owner_tid=OTHER_TID)
        cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert cs_field(state, OFF_OWNER) == OTHER_TID
        assert cs_field(state, OFF_REC)   == 1
        assert cs_field(state, OFF_LOCK)  == 0

    def test_try_enter_after_leave_succeeds(self, env):
        cpu, mem, state, stubs = env
        cs_call(stubs, cpu, mem, "InitializeCriticalSection")
        cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        cs_call(stubs, cpu, mem, "LeaveCriticalSection")
        result = cs_call(stubs, cpu, mem, "TryEnterCriticalSection")
        assert result == 1
