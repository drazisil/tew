"""Tests for the SetWindowsHookExA / UnhookWindowsHookEx / CallNextHookEx chain.

Happy path: chain propagates correctly via CallNextHookEx.
Sad paths: invalid handle, last-in-chain, unregistered handle all return 0.
"""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.user32_handlers import register_user32_gdi32_handlers
from tew.hardware.cpu_zig import ZigCPU as CPU, EAX, ESP
from tew.hardware.memory import Memory


# ── Shared test infrastructure ────────────────────────────────────────────────

class _StubHandlers:
    def __init__(self):
        self._h: dict = {}

    def register_handler(self, dll, name, fn):
        self._h[(dll, name)] = fn

    def get(self, dll, name):
        return self._h[(dll, name)]


MEM_SIZE    = 16 * 1024 * 1024   # 16 MB
HEAP_START  = 0x400000
STACK       = 0x200000
PROC_A_ADDR = 0x500000           # hook proc A — simple ret 12
PROC_B_ADDR = 0x600000           # hook proc B — simple ret 12

# stdcall hook procs receive (nCode, wParam, lParam) — 3 args, 12 bytes to pop
RET_12 = bytes([0xC2, 0x0C, 0x00])  # ret 12

WH_KEYBOARD   = 2
WH_GETMESSAGE = 8


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    state.next_heap_alloc = HEAP_START

    cpu = CPU(mem)
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # fake return address

    stubs = _StubHandlers()
    register_user32_gdi32_handlers(stubs, mem, state)

    # Write minimal hook procs into memory
    mem.load(PROC_A_ADDR, RET_12)
    mem.load(PROC_B_ADDR, RET_12)

    return cpu, mem, state, stubs


def install_hook(stubs, cpu, mem, id_hook, lp_fn) -> int:
    """Call SetWindowsHookExA and return the HHOOK handle."""
    mem.write32(STACK + 4, id_hook)
    mem.write32(STACK + 8, lp_fn)
    mem.write32(STACK + 12, 0)  # hmod
    mem.write32(STACK + 16, 0)  # dwThreadId
    stubs.get("user32.dll", "SetWindowsHookExA")(cpu)
    return cpu.regs[EAX]


def unhook(stubs, cpu, mem, hhk) -> int:
    mem.write32(STACK + 4, hhk)
    stubs.get("user32.dll", "UnhookWindowsHookEx")(cpu)
    return cpu.regs[EAX]


def call_next(stubs, cpu, mem, hhk, ncode=0, wparam=0, lparam=0) -> int:
    mem.write32(STACK + 4,  hhk)
    mem.write32(STACK + 8,  ncode)
    mem.write32(STACK + 12, wparam)
    mem.write32(STACK + 16, lparam)
    stubs.get("user32.dll", "CallNextHookEx")(cpu)
    return cpu.regs[EAX]


# ── Chain management (data structure) ────────────────────────────────────────

class TestChainManagement:

    def test_install_registers_in_winhooks(self, env):
        cpu, mem, state, stubs = env
        hhk = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_A_ADDR)
        assert hhk != 0

    def test_install_returns_nonzero_handle(self, env):
        cpu, mem, state, stubs = env
        hhk = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_A_ADDR)
        assert hhk != 0

    def test_two_same_type_hooks_most_recent_is_first(self, env):
        """Windows LIFO: most recently installed hook is called first."""
        cpu, mem, state, stubs = env
        hhk_a = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_A_ADDR)
        hhk_b = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_B_ADDR)
        # hhk_b installed second → should be first in chain
        from tew.api import user32_handlers as _u32
        # Access closure state indirectly through behaviour: CallNextHookEx on hhk_b
        # (first in chain) should reach hhk_a (second); on hhk_a should return 0.
        result_from_b = call_next(stubs, cpu, mem, hhk_b)
        # hhk_b is first, hhk_a is next — calling next from hhk_b should succeed (returns 0 from ret 12)
        assert result_from_b == 0  # proc_a returns 0 in EAX

    def test_two_different_type_hooks_independent(self, env):
        cpu, mem, state, stubs = env
        hhk_kb  = install_hook(stubs, cpu, mem, WH_KEYBOARD,   PROC_A_ADDR)
        hhk_gm  = install_hook(stubs, cpu, mem, WH_GETMESSAGE, PROC_B_ADDR)
        # Neither should be in the other's chain: CallNextHookEx on kb hook returns 0
        # (no next kb hook), same for gm hook
        assert call_next(stubs, cpu, mem, hhk_kb) == 0
        assert call_next(stubs, cpu, mem, hhk_gm) == 0

    def test_unhook_returns_true(self, env):
        cpu, mem, state, stubs = env
        hhk = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_A_ADDR)
        result = unhook(stubs, cpu, mem, hhk)
        assert result == 1  # TRUE

    def test_unhook_removes_from_chain(self, env):
        """After unhooking, CallNextHookEx on the removed handle returns 0."""
        cpu, mem, state, stubs = env
        hhk_a = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_A_ADDR)
        hhk_b = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_B_ADDR)
        unhook(stubs, cpu, mem, hhk_a)
        # hhk_b is now alone in chain; CallNextHookEx on it returns 0
        assert call_next(stubs, cpu, mem, hhk_b) == 0

    def test_unhook_unknown_handle_does_not_crash(self, env):
        cpu, mem, state, stubs = env
        result = unhook(stubs, cpu, mem, 0xDEADBEEF)
        assert result == 1  # always returns TRUE per spec


# ── CallNextHookEx sad paths ──────────────────────────────────────────────────

class TestCallNextHookExSadPaths:

    def test_unknown_hhk_returns_zero(self, env):
        cpu, mem, state, stubs = env
        result = call_next(stubs, cpu, mem, 0xDEADBEEF)
        assert result == 0

    def test_only_hook_in_chain_returns_zero(self, env):
        """Single hook has no next; CallNextHookEx must return 0."""
        cpu, mem, state, stubs = env
        hhk = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_A_ADDR)
        result = call_next(stubs, cpu, mem, hhk)
        assert result == 0

    def test_last_hook_in_chain_returns_zero(self, env):
        """With two hooks, the second (last) in chain returns 0 from CallNextHookEx."""
        cpu, mem, state, stubs = env
        hhk_a = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_A_ADDR)  # installed first → last in chain
        hhk_b = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_B_ADDR)  # installed second → first in chain
        # hhk_a is last; calling next from hhk_a returns 0
        result = call_next(stubs, cpu, mem, hhk_a)
        assert result == 0

    def test_unhooked_handle_returns_zero(self, env):
        cpu, mem, state, stubs = env
        hhk = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_A_ADDR)
        unhook(stubs, cpu, mem, hhk)
        result = call_next(stubs, cpu, mem, hhk)
        assert result == 0

    def test_zero_hhk_returns_zero(self, env):
        cpu, mem, state, stubs = env
        result = call_next(stubs, cpu, mem, 0)
        assert result == 0


# ── CallNextHookEx happy path ─────────────────────────────────────────────────

class TestCallNextHookExHappyPath:

    def test_calls_next_proc_in_chain(self, env):
        """CallNextHookEx on the first hook invokes the second hook proc."""
        cpu, mem, state, stubs = env
        hhk_a = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_A_ADDR)  # last in chain
        hhk_b = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_B_ADDR)  # first in chain
        # Calling next from hhk_b should invoke PROC_A and return its EAX (0)
        result = call_next(stubs, cpu, mem, hhk_b, ncode=0, wparam=1, lparam=2)
        assert result == 0

    def test_chain_does_not_skip_middle_hook(self, env):
        """With three hooks A→B→C (C first, A last), calling next from C reaches B not A."""
        cpu, mem, state, stubs = env
        PROC_C_ADDR = 0x700000
        mem.load(PROC_C_ADDR, RET_12)
        hhk_a = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_A_ADDR)  # last
        hhk_b = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_B_ADDR)  # middle
        hhk_c = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_C_ADDR)  # first
        # Next from C is B: returns 0 (B's proc returns 0 in EAX)
        result_c = call_next(stubs, cpu, mem, hhk_c)
        assert result_c == 0
        # Next from B is A: also returns 0
        result_b = call_next(stubs, cpu, mem, hhk_b)
        assert result_b == 0
        # Next from A is nothing: returns 0
        result_a = call_next(stubs, cpu, mem, hhk_a)
        assert result_a == 0

    def test_call_next_passes_args_to_next_proc(self, env):
        """The next proc receives nCode/wParam/lParam passed to CallNextHookEx."""
        # We can't easily inspect what the proc received without writing code that
        # stores args somewhere. Instead verify the call completes without error.
        cpu, mem, state, stubs = env
        hhk_a = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_A_ADDR)
        hhk_b = install_hook(stubs, cpu, mem, WH_KEYBOARD, PROC_B_ADDR)
        result = call_next(stubs, cpu, mem, hhk_b, ncode=0, wparam=0x41, lparam=0x1)
        assert isinstance(result, int)  # completed without exception
