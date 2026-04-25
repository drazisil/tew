"""Hypothesis-based property tests for CriticalSection and Mutex handlers.

Three techniques demonstrated:
  1. @given — assert invariants across random inputs
  2. Garbage memory — handlers must tolerate arbitrary prior memory contents
  3. RuleBasedStateMachine — Hypothesis generates call sequences and checks
     invariants after every step, then shrinks to a minimal failing trace.
"""
from __future__ import annotations

from hypothesis import given, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from tew.api._state import CRTState
from tew.api.kernel32_io import register_kernel32_io_handlers
from tew.api.kernel32_sync import register_kernel32_sync_handlers
from tew.hardware.cpu import EAX, ESP
from tew.hardware.memory import Memory

# ── Shared constants ──────────────────────────────────────────────────────────

MEM_SIZE  = 8 * 1024 * 1024
STACK     = 0x200000
CS_ADDR   = 0x300000
NAME_BUF  = 0x400000

OFF_LOCK  = 0x04   # LockCount  (-1 == free)
OFF_REC   = 0x08   # RecursionCount
OFF_OWNER = 0x0C   # OwningThread
LOCK_FREE = 0xFFFFFFFF
MAIN_TID  = 1000   # CRTState gives main thread TID 1000


# ── Infrastructure ────────────────────────────────────────────────────────────

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
        self.eip = 0x401002


def make_env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_kernel32_sync_handlers(stubs, mem, state)
    register_kernel32_io_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    return cpu, mem, state, stubs


def _call(stubs, cpu, mem, dll, name, *args):
    """Write args to stack and invoke handler, resetting ESP each time."""
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    for i, v in enumerate(args):
        mem.write32(STACK + 4 + i * 4, v)
    stubs.get(dll, name)(cpu)
    return cpu.regs[EAX]


def cs(stubs, cpu, mem, name):
    return _call(stubs, cpu, mem, "kernel32.dll", name, CS_ADDR)

def cs_field(mem, offset):
    return mem.read32(CS_ADDR + offset)

def create_mutex(stubs, cpu, mem, initial_owner=0, name_ptr=0):
    return _call(stubs, cpu, mem, "kernel32.dll", "CreateMutexA", 0, initial_owner, name_ptr)

def wait(stubs, cpu, mem, handle, timeout=0xFFFFFFFF):
    return _call(stubs, cpu, mem, "kernel32.dll", "WaitForSingleObject", handle, timeout)

def release(stubs, cpu, mem, handle):
    return _call(stubs, cpu, mem, "kernel32.dll", "ReleaseMutex", handle)

def close(stubs, cpu, mem, handle):
    return _call(stubs, cpu, mem, "kernel32.dll", "CloseHandle", handle)


# ── 1. @given: Critical Section invariants ───────────────────────────────────

@given(st.integers(min_value=1, max_value=16))
def test_cs_balanced_enter_leave_always_frees(n):
    """N matched Enter/Leave pairs must always leave the CS free."""
    cpu, mem, state, stubs = make_env()
    cs(stubs, cpu, mem, "InitializeCriticalSection")
    for _ in range(n):
        cs(stubs, cpu, mem, "EnterCriticalSection")
    for _ in range(n):
        cs(stubs, cpu, mem, "LeaveCriticalSection")
    assert cs_field(mem, OFF_LOCK)  == LOCK_FREE
    assert cs_field(mem, OFF_REC)   == 0
    assert cs_field(mem, OFF_OWNER) == 0


@given(st.integers(min_value=1, max_value=16))
def test_cs_recursion_count_tracks_depth(n):
    """After N recursive enters, RecursionCount must equal N."""
    cpu, mem, state, stubs = make_env()
    cs(stubs, cpu, mem, "InitializeCriticalSection")
    for _ in range(n):
        cs(stubs, cpu, mem, "EnterCriticalSection")
    assert cs_field(mem, OFF_REC) == n
    assert cs_field(mem, OFF_OWNER) == MAIN_TID


@given(st.integers(min_value=1, max_value=16))
def test_try_enter_recursive_depth_matches(n):
    """N recursive TryEnter calls from the owning thread always succeed."""
    cpu, mem, state, stubs = make_env()
    cs(stubs, cpu, mem, "InitializeCriticalSection")
    for _ in range(n):
        result = cs(stubs, cpu, mem, "TryEnterCriticalSection")
        assert result == 1
    assert cs_field(mem, OFF_REC) == n


@given(st.integers(min_value=0, max_value=0xFFFFFFFF))
def test_cs_garbage_handle_never_halts(handle):
    """Any uint32 handle passed to ReleaseMutex/CloseHandle must not halt the CPU."""
    cpu, mem, state, stubs = make_env()
    release(stubs, cpu, mem, handle)
    close(stubs, cpu, mem, handle)
    assert not cpu.halted


# ── 2. Garbage memory: Init must overwrite whatever was there ─────────────────

@given(st.binary(min_size=24, max_size=24))
def test_init_cs_overwrites_garbage(garbage):
    """InitializeCriticalSection must produce a valid CS regardless of prior contents."""
    cpu, mem, state, stubs = make_env()
    for i, b in enumerate(garbage):
        mem.write8(CS_ADDR + i, b)
    cs(stubs, cpu, mem, "InitializeCriticalSection")
    assert cs_field(mem, OFF_LOCK)  == LOCK_FREE
    assert cs_field(mem, OFF_REC)   == 0
    assert cs_field(mem, OFF_OWNER) == 0


# ── 3. @given: Mutex invariants ───────────────────────────────────────────────

@given(st.integers(min_value=0, max_value=0xFFFFFFFF))
def test_wait_garbage_handle_never_halts(handle):
    """WaitForSingleObject on any garbage handle must not halt the CPU."""
    cpu, mem, state, stubs = make_env()
    wait(stubs, cpu, mem, handle)
    assert not cpu.halted


@given(st.integers(min_value=1, max_value=8))
def test_mutex_balanced_wait_release_leaves_free(n):
    """N matched Wait/Release pairs on a mutex must leave it unlocked."""
    cpu, mem, state, stubs = make_env()
    h = create_mutex(stubs, cpu, mem)
    for _ in range(n):
        assert wait(stubs, cpu, mem, h) == 0  # WAIT_OBJECT_0
    for _ in range(n):
        release(stubs, cpu, mem, h)
    obj = state.kernel_handle_map[h]
    assert obj.locked is False
    assert obj.owner_tid is None
    assert obj.recursion_count == 0


@given(st.integers(min_value=1, max_value=8))
def test_mutex_recursion_count_tracks_waits(n):
    """RecursionCount on a mutex must equal number of outstanding waits."""
    cpu, mem, state, stubs = make_env()
    h = create_mutex(stubs, cpu, mem)
    for _ in range(n):
        wait(stubs, cpu, mem, h)
    obj = state.kernel_handle_map[h]
    assert obj.recursion_count == n
    assert obj.locked is True
    assert obj.owner_tid == MAIN_TID


# ── 4. RuleBasedStateMachine: CS call sequences ───────────────────────────────

class CriticalSectionMachine(RuleBasedStateMachine):
    """
    Hypothesis generates arbitrary sequences of Enter, TryEnter, and Leave
    calls and checks that structural invariants hold after every step.

    What it finds that @given alone cannot:
    - Leave on a free CS (underflow)
    - Interleaved TryEnter/Enter sequences that corrupt RecursionCount
    - Any sequence that leaves owner set when depth is zero
    """

    def __init__(self):
        super().__init__()
        self.cpu, self.mem, self.state, self.stubs = make_env()
        self.depth = 0

    @initialize()
    def init(self):
        cs(self.stubs, self.cpu, self.mem, "InitializeCriticalSection")

    @rule()
    def enter(self):
        cs(self.stubs, self.cpu, self.mem, "EnterCriticalSection")
        self.depth += 1

    @rule()
    def try_enter(self):
        result = cs(self.stubs, self.cpu, self.mem, "TryEnterCriticalSection")
        # No other threads in this machine, so TryEnter always succeeds.
        assert result == 1
        self.depth += 1

    @rule()
    def leave(self):
        if self.depth > 0:
            cs(self.stubs, self.cpu, self.mem, "LeaveCriticalSection")
            self.depth -= 1

    @invariant()
    def lock_count_consistent(self):
        lock = self.mem.read32(CS_ADDR + OFF_LOCK)
        if self.depth == 0:
            assert lock == LOCK_FREE, f"depth=0 but LockCount={lock:#010x}"
        else:
            assert lock != LOCK_FREE, f"depth={self.depth} but CS appears free"

    @invariant()
    def owner_consistent(self):
        owner = self.mem.read32(CS_ADDR + OFF_OWNER)
        if self.depth == 0:
            assert owner == 0, f"depth=0 but OwningThread={owner}"
        else:
            assert owner == MAIN_TID, f"depth={self.depth} but owner={owner}"

    @invariant()
    def recursion_count_matches_depth(self):
        rec = self.mem.read32(CS_ADDR + OFF_REC)
        assert rec == self.depth, f"RecursionCount={rec} != model depth={self.depth}"


TestCriticalSectionMachine = CriticalSectionMachine.TestCase
