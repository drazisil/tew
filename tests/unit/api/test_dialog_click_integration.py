"""
Integration test proving WindowManager's programmatic dialog-step hook
actually fires *inside* the real, blocking _DialogBoxParamA modal loop
(tew/api/user32_handlers.py) -- not just against hand-built WindowEntry
state (see test_window_manager.py for that unit-level coverage).

Uses the real dialog 114 (login dialog) resource template from the real
MCity_d.exe, so click_control is exercised against the same control IDs
the game actually ships (Continue button id=0x0001, verified in
tests/unit/pe/test_pe_resources.py). The DLGPROC itself is a small
hand-assembled stub (not the game's real, far more complex login-validation
proc) -- this test is about proving the *hook mechanism* fires inside the
real loop, not about replicating the game's own dialog logic.

Runs SDL2 with the dummy video/audio drivers so it doesn't need a real
display.
"""
from __future__ import annotations

import json
import os

import pytest

# Must be set before WindowManager.initialize() calls SDL_Init.
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from tew.api._state import CRTState
from tew.api.pe_resources import PEResources
from tew.api.user32_handlers import register_user32_gdi32_handlers
from tew.api.win32_handlers import Win32Handlers
from tew.hardware.cpu_zig import ZigCPU as CPU, EAX, ESP
from tew.hardware.memory import Memory


def _exe_path() -> str | None:
    try:
        cfg_path = os.path.join(os.getcwd(), "emulator.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("exePath")
    except Exception:
        return None


def _load_exe() -> bytes | None:
    path = _exe_path()
    if path is None or not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


_EXE_DATA = _load_exe()

needs_exe = pytest.mark.skipif(
    _EXE_DATA is None,
    reason="MCity_d.exe not found — skipping live dialog integration test",
)

MEM_SIZE = 16 * 1024 * 1024
STACK = 0x200000
DLGPROC_ADDR = 0x500000
HEAP_START = 0x600000  # for CRTState.simple_alloc (e.g. _get_dialog_sentinel's HLT byte)
WM_COMMAND = 0x0111
CONTINUE_ID = 0x0001  # dialog 114's real Continue button ID


def _build_dlgproc(end_dialog_addr: int) -> bytes:
    """DLGPROC(hDlg, uMsg, wParam, lParam) [stdcall]:
    on WM_COMMAND(wParam=CONTINUE_ID), calls EndDialog(hDlg, 1) and returns
    TRUE; otherwise returns FALSE. Minimal on purpose -- this test is about
    the hook mechanism, not about replicating real DLGPROC logic."""
    mov_ecx_hdlg = bytes([0x8B, 0x4C, 0x24, 0x04])          # mov ecx, [esp+4]
    mov_eax_umsg = bytes([0x8B, 0x44, 0x24, 0x08])          # mov eax, [esp+8]
    cmp_eax_wmcmd = bytes([0x3D]) + WM_COMMAND.to_bytes(4, "little")   # cmp eax, WM_COMMAND
    mov_eax_wparam = bytes([0x8B, 0x44, 0x24, 0x0C])        # mov eax, [esp+12]
    cmp_eax_id = bytes([0x3D]) + CONTINUE_ID.to_bytes(4, "little")     # cmp eax, CONTINUE_ID

    push_1 = bytes([0x6A, 0x01])                             # push 1
    push_ecx = bytes([0x51])                                 # push ecx
    mov_edx_enddlg = bytes([0xBA]) + end_dialog_addr.to_bytes(4, "little")  # mov edx, end_dialog_addr
    call_edx = bytes([0xFF, 0xD2])                           # call edx
    mov_eax_1 = bytes([0xB8, 0x01, 0x00, 0x00, 0x00])        # mov eax, 1
    ret16 = bytes([0xC2, 0x10, 0x00])                        # ret 16
    true_path = push_1 + push_ecx + mov_edx_enddlg + call_edx + mov_eax_1 + ret16

    xor_eax = bytes([0x31, 0xC0])                            # xor eax, eax
    false_path = xor_eax + ret16

    jne_id = bytes([0x75, len(true_path)])                                    # jne false_path
    jne_cmd = bytes([0x75, len(mov_eax_wparam) + len(cmp_eax_id) + len(jne_id) + len(true_path)])  # jne false_path

    return (
        mov_ecx_hdlg + mov_eax_umsg + cmp_eax_wmcmd + jne_cmd
        + mov_eax_wparam + cmp_eax_id + jne_id
        + true_path + false_path
    )


@needs_exe
def test_dialog_step_hook_fires_inside_real_modal_loop_and_clicks_continue():
    mem = Memory(MEM_SIZE)
    state = CRTState()
    state.next_heap_alloc = HEAP_START  # default (0x04000000) is past MEM_SIZE here
    state.pe_resources = PEResources(_EXE_DATA)

    cpu = CPU(mem)
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # fake return address for DialogBoxParamA itself

    stubs = Win32Handlers(mem)
    register_user32_gdi32_handlers(stubs, mem, state)
    stubs.install(cpu)

    end_dialog_addr = stubs.get_handler_address("user32.dll", "EndDialog")
    assert end_dialog_addr is not None

    dlgproc = _build_dlgproc(end_dialog_addr)
    for i, b in enumerate(dlgproc):
        mem.write8(DLGPROC_ADDR + i, b)

    # Register the hook BEFORE the modal loop starts -- click Continue as
    # soon as any dialog appears.
    clicked = {"done": False}

    def _click_continue(wm, dlg_hwnd):
        ok = wm.click_control(dlg_hwnd, CONTINUE_ID)
        clicked["done"] = ok

    state.window_manager.set_dialog_step_hook(_click_continue)

    # DialogBoxParamA(hInstance, lpTemplateName=114, hWndParent=0, lpDialogFunc, dwInitParam) [stdcall]
    mem.write32(STACK + 4, 0)                 # hInstance (unused)
    mem.write32(STACK + 8, 114)               # lpTemplateName (dialog 114, ordinal)
    mem.write32(STACK + 12, 0)                # hWndParent
    mem.write32(STACK + 16, DLGPROC_ADDR)     # lpDialogFunc
    mem.write32(STACK + 20, 0)                # dwInitParam

    dialog_box_param_a_addr = stubs.get_handler_address("user32.dll", "DialogBoxParamA")
    assert dialog_box_param_a_addr is not None
    cpu.eip = dialog_box_param_a_addr
    cpu.halted = False
    while not cpu.halted and cpu.eip != 0xDEAD:
        cpu.step()

    assert clicked["done"] is True
    assert cpu.regs[EAX] == 1  # DLGPROC returned TRUE from EndDialog(hDlg, 1)
