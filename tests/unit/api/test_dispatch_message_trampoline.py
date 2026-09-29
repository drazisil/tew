"""DispatchMessageA runs the window proc as ordinary guest code.

It used to call the proc through a nested _invoke_emulated_proc run with a
5M-step budget, then rewind the CPU if the budget ran out -- which a game
WndProc sitting in a long loop (the lobby) legitimately does, and which left
other threads with rewound stack state (seen live 2026-09-29). Now the
handler rewrites the stack so the proc runs on the caller's own thread and
returns through a post-dispatch stub that hands back its EAX and restores
ESP whatever convention the proc used.
"""
from __future__ import annotations

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from tew.api._state import CRTState
from tew.api.user32_handlers import register_user32_gdi32_handlers
from tew.api.win32_handlers import Win32Handlers
from tew.hardware.cpu_zig import ZigCPU as CPU, EAX, ESP
from tew.hardware.memory import Memory

MEM_SIZE   = 8 * 1024 * 1024
STACK      = 0x200000
HEAP_START = 0x600000
CALLER     = 0x500000
PROC       = 0x500100
MSG1       = 0x510000
MSG2       = 0x510020
WM_COMMAND = 0x0111


def _env():
    mem = Memory(MEM_SIZE)
    cpu = CPU(mem)
    state = CRTState()
    state.memory = mem
    state.next_heap_alloc = HEAP_START
    stubs = Win32Handlers(mem)
    register_user32_gdi32_handlers(stubs, mem, state)
    stubs.install(cpu)
    wm = state.window_manager
    assert wm.initialize()
    hwnd = wm.create_window("TewTest", "t", 0, 0, 0, 0, 100, 100, 0, PROC)  # not visible: no SDL window
    assert hwnd
    return cpu, mem, stubs, hwnd


def _write_msg(mem, at, hwnd, msg, wparam=0, lparam=0):
    for i, v in enumerate((hwnd, msg, wparam, lparam)):
        mem.write32(at + 4 * i, v)


def _call(cpu, mem, target, arg):
    """push arg; call target; hlt -- run it, return EAX, check ESP balanced."""
    rel = (target - (CALLER + 10)) & 0xFFFFFFFF
    mem.load(CALLER, b"\x68" + arg.to_bytes(4, "little") + b"\xE8" + rel.to_bytes(4, "little") + b"\xF4")
    cpu.regs[ESP] = STACK
    cpu.eip = CALLER
    cpu.halted = False
    cpu.run(10_000)
    assert cpu.halted and cpu.eip in (CALLER + 10, CALLER + 11)
    assert cpu.regs[ESP] == STACK
    return cpu.regs[EAX]


def test_stdcall_proc_result_comes_back():
    cpu, mem, stubs, hwnd = _env()
    # mov eax, [esp+8] (uMsg); add eax, 0x1000; ret 16
    mem.load(PROC, bytes.fromhex("8b442408" "0500100000" "c21000"))
    _write_msg(mem, MSG1, hwnd, WM_COMMAND)
    assert _call(cpu, mem, stubs.lookup_handler_address("user32.dll", "DispatchMessageA"), MSG1) == 0x1111


def test_proc_with_wrong_convention_still_returns_cleanly():
    cpu, mem, stubs, hwnd = _env()
    # mov eax, 7; ret   -- leaves its 4 args on the stack
    mem.load(PROC, bytes.fromhex("b807000000" "c3"))
    _write_msg(mem, MSG1, hwnd, WM_COMMAND)
    assert _call(cpu, mem, stubs.lookup_handler_address("user32.dll", "DispatchMessageA"), MSG1) == 7


def test_nested_dispatch_from_inside_the_proc():
    """A proc running its own message loop: it dispatches a second message
    (MSG2, uMsg 0x22) while handling the first (uMsg 0x11), then returns the
    inner result plus its own uMsg."""
    cpu, mem, stubs, hwnd = _env()
    dispatch = stubs.lookup_handler_address("user32.dll", "DispatchMessageA")
    # PROC:
    #   mov eax, [esp+8]            ; uMsg
    #   cmp eax, 0x11
    #   jne leaf
    #   push MSG2
    #   call DispatchMessageA       ; -> 0x22 (leaf below)
    #   add eax, 0x11
    #   ret 16
    # leaf:
    #   ret 16                      ; EAX = uMsg
    head = bytes.fromhex("8b442408" "83f811")      # mov eax, [esp+8]; cmp eax, 0x11
    call_at = PROC + len(head) + 2 + 5              # after jne (2) and push (5)
    taken = (b"\x68" + MSG2.to_bytes(4, "little")  # push MSG2
             + b"\xE8" + ((dispatch - (call_at + 5)) & 0xFFFFFFFF).to_bytes(4, "little")
             + bytes.fromhex("83c011" "c21000"))    # add eax, 0x11; ret 16
    body = head + bytes([0x75, len(taken)]) + taken + bytes.fromhex("c21000")  # jne leaf; ...; leaf: ret 16
    mem.load(PROC, bytes(body))
    _write_msg(mem, MSG1, hwnd, 0x11)
    _write_msg(mem, MSG2, hwnd, 0x22)
    assert _call(cpu, mem, dispatch, MSG1) == 0x33


def test_unknown_hwnd_returns_zero_without_calling_anything():
    cpu, mem, stubs, hwnd = _env()
    _write_msg(mem, MSG1, 0xDEAD, WM_COMMAND)
    assert _call(cpu, mem, stubs.lookup_handler_address("user32.dll", "DispatchMessageA"), MSG1) == 0


def test_long_running_proc_is_not_cut_off():
    """The old nested call gave up after 5M guest steps and returned 0 (the
    lobby's WndProc loops far longer than that). A 3M-iteration loop is ~6M
    steps."""
    cpu, mem, stubs, hwnd = _env()
    # mov ecx, 3000000; loop: dec ecx; jnz loop; mov eax, 0x42; ret 16
    mem.load(PROC, bytes.fromhex("b9c0c62d00" "49" "75fd" "b842000000" "c21000"))
    _write_msg(mem, MSG1, hwnd, WM_COMMAND)
    rel = (stubs.lookup_handler_address("user32.dll", "DispatchMessageA") - (CALLER + 10)) & 0xFFFFFFFF
    mem.load(CALLER, b"\x68" + MSG1.to_bytes(4, "little") + b"\xE8" + rel.to_bytes(4, "little") + b"\xF4")
    cpu.regs[ESP] = STACK
    cpu.eip = CALLER
    cpu.halted = False
    cpu.run(20_000_000)
    assert cpu.halted and cpu.regs[ESP] == STACK
    assert cpu.regs[EAX] == 0x42
