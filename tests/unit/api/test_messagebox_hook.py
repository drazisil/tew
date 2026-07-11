"""
Tests for WindowManager.set_messagebox_hook -- lets a script auto-answer
MessageBoxA calls (e.g. the real "run full screen?" MB_YESNO prompt,
FUN_006b13b0 in MCity_d.exe) without needing real input, bypassing the
blocking native SDL_ShowMessageBox entirely when the hook matches.

Unlike the DialogBoxParamA-based dialogs (test_dialog_click_integration.py),
MessageBoxA doesn't go through WindowManager's window/message-queue system
at all -- it's a single synchronous call straight to SDL_ShowMessageBox.
So these tests drive the real registered MessageBoxA handler directly
(no SDL init needed for the auto-answered case, since the hook short-
circuits before SDL_ShowMessageBox is ever called).
"""
from __future__ import annotations

from tew.api._state import CRTState
from tew.api.user32_handlers import register_user32_gdi32_handlers
from tew.api.win32_handlers import Win32Handlers
from tew.hardware.cpu_zig import ZigCPU as CPU, EAX, ESP
from tew.hardware.memory import Memory

MEM_SIZE = 16 * 1024 * 1024
STACK = 0x100000
TEXT_ADDR = 0x200000
CAPTION_ADDR = 0x200100
HEAP_START = 0x600000  # default (0x04000000) is past MEM_SIZE here

IDYES = 6
IDNO = 7
MB_YESNO = 0x04


def _write_cstr(mem: Memory, addr: int, s: str) -> None:
    for i, ch in enumerate(s.encode("latin-1")):
        mem.write8(addr + i, ch)
    mem.write8(addr + len(s), 0)


def _call_messagebox_a(stubs: Win32Handlers, mem: Memory, cpu: CPU, text: str, caption: str, u_type: int) -> int:
    _write_cstr(mem, TEXT_ADDR, text)
    _write_cstr(mem, CAPTION_ADDR, caption)
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)          # return address
    mem.write32(STACK + 4, 0)            # hWnd
    mem.write32(STACK + 8, TEXT_ADDR)    # lpText
    mem.write32(STACK + 12, CAPTION_ADDR)  # lpCaption
    mem.write32(STACK + 16, u_type)      # uType
    handler = stubs._handlers["user32.dll!MessageBoxA"].handler
    handler(cpu)
    return cpu.regs[EAX]


def _env():
    mem = Memory(MEM_SIZE)
    state = CRTState()
    state.next_heap_alloc = HEAP_START
    cpu = CPU(mem)
    stubs = Win32Handlers(mem)
    register_user32_gdi32_handlers(stubs, mem, state)
    return mem, state, cpu, stubs


def test_hook_auto_answers_matching_messagebox():
    mem, state, cpu, stubs = _env()

    def hook(caption, text, u_type):
        if "full screen" in text:
            return IDNO
        return None

    state.window_manager.set_messagebox_hook(hook)

    result = _call_messagebox_a(
        stubs, mem, cpu,
        text="Do you want to run Motor City Online full screen?",
        caption="Motor City Online",
        u_type=MB_YESNO,
    )
    assert result == IDNO


def test_hook_returning_none_falls_through_and_is_not_asserted_here():
    """A hook that returns None for a non-matching call must not short-
    circuit -- this test only checks the hook itself gets consulted and
    returns None; it does NOT drive the real SDL fallback path (that would
    need a real display or SDL_VIDEODRIVER=dummy, see
    test_dialog_click_integration.py for that pattern)."""
    mem, state, cpu, stubs = _env()
    calls = []

    def hook(caption, text, u_type):
        calls.append((caption, text, u_type))
        return None

    state.window_manager.set_messagebox_hook(hook)
    assert state.window_manager._messagebox_hook is hook
    # Directly exercise just the hook-consultation logic without triggering
    # the real SDL_ShowMessageBox fallback:
    answer = state.window_manager._messagebox_hook("Some Caption", "Some text", 0)
    assert answer is None
    assert calls == [("Some Caption", "Some text", 0)]


def test_no_hook_registered_leaves_messagebox_hook_none():
    mem, state, cpu, stubs = _env()
    assert state.window_manager._messagebox_hook is None
