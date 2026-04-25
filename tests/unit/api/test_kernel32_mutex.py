"""Tests for Mutex handlers: CreateMutexA, WaitForSingleObject, ReleaseMutex, CloseHandle."""
from __future__ import annotations

import pytest

from tew.api._state import CRTState, MutexHandle
from tew.api.kernel32_io import register_kernel32_io_handlers
from tew.hardware.memory import Memory
from tew.hardware.cpu import EAX, ESP

WAIT_OBJECT_0  = 0x00000000
WAIT_TIMEOUT   = 0x00000102
WAIT_INFINITE  = 0xFFFFFFFF
MAIN_TID       = 1000


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


MEM_SIZE  = 8 * 1024 * 1024
STACK     = 0x200000
NAME_BUF  = 0x300000


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_kernel32_io_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    return cpu, mem, state, stubs


def write_cstring(mem, addr, s: str) -> None:
    for i, c in enumerate(s):
        mem.write8(addr + i, ord(c))
    mem.write8(addr + len(s), 0)


def create_mutex(stubs, cpu, mem, initial_owner=0, name_ptr=0) -> int:
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4,  0)
    mem.write32(STACK + 8,  initial_owner)
    mem.write32(STACK + 12, name_ptr)
    stubs.get("kernel32.dll", "CreateMutexA")(cpu)
    return cpu.regs[EAX]


def wait_single(stubs, cpu, mem, handle, timeout=WAIT_INFINITE) -> int:
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4, handle)
    mem.write32(STACK + 8, timeout)
    stubs.get("kernel32.dll", "WaitForSingleObject")(cpu)
    return cpu.regs[EAX]


def release_mutex(stubs, cpu, mem, handle) -> int:
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4, handle)
    stubs.get("kernel32.dll", "ReleaseMutex")(cpu)
    return cpu.regs[EAX]


def close_handle(stubs, cpu, mem, handle) -> int:
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4, handle)
    stubs.get("kernel32.dll", "CloseHandle")(cpu)
    return cpu.regs[EAX]


# ── CreateMutexA ──────────────────────────────────────────────────────────────

class TestCreateMutexA:

    def test_returns_nonzero_handle(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem)
        assert h != 0

    def test_handle_in_kernel_map(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem)
        assert h in state.kernel_handle_map
        assert isinstance(state.kernel_handle_map[h], MutexHandle)

    def test_unowned_mutex_is_unlocked(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem, initial_owner=0)
        obj = state.kernel_handle_map[h]
        assert obj.locked is False
        assert obj.owner_tid is None

    def test_initial_owner_sets_locked(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem, initial_owner=1)
        obj = state.kernel_handle_map[h]
        assert obj.locked is True
        assert obj.owner_tid == MAIN_TID
        assert obj.recursion_count == 1

    def test_named_mutex_deduplication(self, env):
        cpu, mem, state, stubs = env
        write_cstring(mem, NAME_BUF, "TestMutex")
        h1 = create_mutex(stubs, cpu, mem, name_ptr=NAME_BUF)
        h2 = create_mutex(stubs, cpu, mem, name_ptr=NAME_BUF)
        assert h1 == h2

    def test_unnamed_mutex_not_deduplicated(self, env):
        cpu, mem, state, stubs = env
        h1 = create_mutex(stubs, cpu, mem)
        h2 = create_mutex(stubs, cpu, mem)
        assert h1 != h2

    def test_different_names_different_handles(self, env):
        cpu, mem, state, stubs = env
        NAME_B = NAME_BUF + 0x100
        write_cstring(mem, NAME_BUF, "MutexA")
        write_cstring(mem, NAME_B,   "MutexB")
        h1 = create_mutex(stubs, cpu, mem, name_ptr=NAME_BUF)
        h2 = create_mutex(stubs, cpu, mem, name_ptr=NAME_B)
        assert h1 != h2


# ── WaitForSingleObject (mutex path) ─────────────────────────────────────────

class TestWaitForSingleObjectMutex:

    def test_wait_on_free_mutex_returns_wait_object_0(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem, initial_owner=0)
        result = wait_single(stubs, cpu, mem, h)
        assert result == WAIT_OBJECT_0

    def test_wait_on_free_mutex_acquires_it(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem, initial_owner=0)
        wait_single(stubs, cpu, mem, h)
        obj = state.kernel_handle_map[h]
        assert obj.locked is True
        assert obj.owner_tid == MAIN_TID

    def test_recursive_wait_by_owner_returns_wait_object_0(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem, initial_owner=1)
        result = wait_single(stubs, cpu, mem, h)
        assert result == WAIT_OBJECT_0

    def test_recursive_wait_increments_recursion_count(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem, initial_owner=1)
        wait_single(stubs, cpu, mem, h)
        obj = state.kernel_handle_map[h]
        assert obj.recursion_count == 2

    def test_wait_on_unknown_handle_returns_wait_object_0(self, env):
        """Unknown handle treated as already-signaled per emulator convention."""
        cpu, mem, state, stubs = env
        result = wait_single(stubs, cpu, mem, 0xDEADBEEF)
        assert result == WAIT_OBJECT_0


# ── ReleaseMutex ──────────────────────────────────────────────────────────────

class TestReleaseMutex:

    def test_release_returns_true(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem, initial_owner=1)
        result = release_mutex(stubs, cpu, mem, h)
        assert result == 1

    def test_release_clears_owner(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem, initial_owner=1)
        release_mutex(stubs, cpu, mem, h)
        obj = state.kernel_handle_map[h]
        assert obj.owner_tid is None
        assert obj.locked is False

    def test_release_recursive_decrements_count(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem, initial_owner=1)
        wait_single(stubs, cpu, mem, h)   # recursion_count = 2
        release_mutex(stubs, cpu, mem, h)  # recursion_count = 1
        obj = state.kernel_handle_map[h]
        assert obj.recursion_count == 1
        assert obj.locked is True   # still held

    def test_release_recursive_final_clears_owner(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem, initial_owner=1)
        wait_single(stubs, cpu, mem, h)    # recursion_count = 2
        release_mutex(stubs, cpu, mem, h)  # recursion_count = 1
        release_mutex(stubs, cpu, mem, h)  # recursion_count = 0 → released
        obj = state.kernel_handle_map[h]
        assert obj.locked is False
        assert obj.owner_tid is None

    def test_release_unknown_handle_does_not_crash(self, env):
        cpu, mem, state, stubs = env
        result = release_mutex(stubs, cpu, mem, 0xDEADBEEF)
        assert result == 1  # always returns TRUE per implementation


# ── CloseHandle (mutex path) ──────────────────────────────────────────────────

class TestCloseHandleMutex:

    def test_close_returns_true(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem)
        result = close_handle(stubs, cpu, mem, h)
        assert result == 1

    def test_close_removes_from_kernel_map(self, env):
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem)
        close_handle(stubs, cpu, mem, h)
        assert h not in state.kernel_handle_map

    def test_close_unknown_handle_returns_true(self, env):
        cpu, mem, state, stubs = env
        result = close_handle(stubs, cpu, mem, 0xDEADBEEF)
        assert result == 1

    def test_close_locked_mutex_removes_handle(self, env):
        """Closing a still-locked mutex removes the handle; no WAIT_ABANDONED issued."""
        cpu, mem, state, stubs = env
        h = create_mutex(stubs, cpu, mem, initial_owner=1)
        close_handle(stubs, cpu, mem, h)
        assert h not in state.kernel_handle_map
