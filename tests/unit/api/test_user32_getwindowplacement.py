"""Tests for user32.dll!GetWindowPlacement — previously unimplemented (hard halt)."""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.user32_handlers import register_user32_gdi32_handlers
from tew.api.window_manager import WindowEntry, WS_VISIBLE
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
        self.fatal_halt = False


MEM_SIZE = 8 * 1024 * 1024
STACK    = 0x200000
HWND     = 0x1034
WNDPL    = 0x300000


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_user32_gdi32_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # return address
    return cpu, mem, state, stubs


def _put_window(state, style=WS_VISIBLE):
    entry = WindowEntry(
        hwnd=HWND, class_name="TestClass", title="Test", style=style, ex_style=0,
        x=10, y=20, cx=100, cy=50, parent_hwnd=0,
    )
    state.window_manager._windows[HWND] = entry
    return entry


class TestGetWindowPlacement:
    def test_fills_placement_for_visible_window(self, env):
        cpu, mem, state, stubs = env
        _put_window(state, style=WS_VISIBLE)
        mem.write32(STACK + 4, HWND)
        mem.write32(STACK + 8, WNDPL)
        mem.write32(WNDPL, 44)  # length

        stubs.get("user32.dll", "GetWindowPlacement")(cpu)

        assert cpu.regs[EAX] == 1  # TRUE
        assert mem.read32(WNDPL + 4) == 0        # flags
        assert mem.read32(WNDPL + 8) == 1        # showCmd = SW_SHOWNORMAL
        assert mem.read32(WNDPL + 12) == 0xFFFFFFFF  # ptMinPosition.x = -1
        assert mem.read32(WNDPL + 16) == 0xFFFFFFFF  # ptMinPosition.y = -1
        assert mem.read32(WNDPL + 20) == 0xFFFFFFFF  # ptMaxPosition.x = -1
        assert mem.read32(WNDPL + 24) == 0xFFFFFFFF  # ptMaxPosition.y = -1
        assert mem.read32(WNDPL + 28) > 0        # rcNormalPosition.left (px_x)
        assert mem.read32(WNDPL + 36) > mem.read32(WNDPL + 28)  # right > left

    def test_hidden_window_reports_sw_hide(self, env):
        cpu, mem, state, stubs = env
        _put_window(state, style=0)  # not WS_VISIBLE
        mem.write32(STACK + 4, HWND)
        mem.write32(STACK + 8, WNDPL)
        mem.write32(WNDPL, 44)

        stubs.get("user32.dll", "GetWindowPlacement")(cpu)

        assert cpu.regs[EAX] == 1  # TRUE — the call itself still succeeds
        assert mem.read32(WNDPL + 8) == 0  # showCmd = SW_HIDE

    def test_unknown_hwnd_returns_false(self, env):
        cpu, mem, state, stubs = env
        mem.write32(STACK + 4, 0x9999)  # never registered
        mem.write32(STACK + 8, WNDPL)
        mem.write32(WNDPL, 44)

        stubs.get("user32.dll", "GetWindowPlacement")(cpu)

        assert cpu.regs[EAX] == 0  # FALSE

    def test_wrong_length_returns_false(self, env):
        cpu, mem, state, stubs = env
        _put_window(state)
        mem.write32(STACK + 4, HWND)
        mem.write32(STACK + 8, WNDPL)
        mem.write32(WNDPL, 40)  # wrong length, real Windows requires 44

        stubs.get("user32.dll", "GetWindowPlacement")(cpu)

        assert cpu.regs[EAX] == 0  # FALSE

    def test_does_not_halt(self, env):
        cpu, mem, state, stubs = env
        _put_window(state)
        mem.write32(STACK + 4, HWND)
        mem.write32(STACK + 8, WNDPL)
        mem.write32(WNDPL, 44)

        stubs.get("user32.dll", "GetWindowPlacement")(cpu)

        assert cpu.halted is False

    def test_cleans_up_stdcall_args(self, env):
        cpu, mem, state, stubs = env
        _put_window(state)
        mem.write32(STACK + 4, HWND)
        mem.write32(STACK + 8, WNDPL)
        mem.write32(WNDPL, 44)

        stubs.get("user32.dll", "GetWindowPlacement")(cpu)

        assert cpu.regs[ESP] == STACK + 8
        assert mem.read32(STACK + 8) == 0xDEAD
