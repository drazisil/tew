"""user32.dll and gdi32.dll handler registrations.

All handlers are stubs that fake the win32 windowing/GDI surface for headless
emulation.  No real window system exists — return values are documented per
handler with the rationale for the chosen value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall, DIALOG_TRAMPOLINE
from tew.api._state import CRTState
from tew.logger import logger


def register_user32_gdi32_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register all user32.dll and gdi32.dll handlers."""

    # ── user32.dll ────────────────────────────────────────────────────────────

    # MessageBoxA(HWND hWnd, LPCSTR lpText, LPCSTR lpCaption, UINT uType) -> int
    def _MessageBoxA(cpu: "CPU") -> None:
        lp_text    = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        lp_caption = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        u_type     = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        text: str = ""
        caption: str = ""
        for i in range(1024):
            ch = memory.read8(lp_text + i)
            if ch == 0:
                break
            text += chr(ch)
        for i in range(256):
            ch = memory.read8(lp_caption + i)
            if ch == 0:
                break
            caption += chr(ch)
        logger.debug("handlers", f'[Win32] MessageBoxA("{caption}", "{text.replace(chr(10), "\\n")}")')
        # MB_ABORTRETRYIGNORE (uType & 0xF == 2): return IDIGNORE=5 to continue past assertions
        # MB_OK (uType & 0xF == 0): return IDOK=1
        btn_type = u_type & 0xF
        cpu.regs[EAX] = 5 if btn_type == 2 else 1
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "MessageBoxA", _MessageBoxA)

    # MessageBoxW(HWND hWnd, LPCWSTR lpText, LPCWSTR lpCaption, UINT uType) -> int
    def _MessageBoxW(cpu: "CPU") -> None:
        logger.debug("handlers", "[Win32] MessageBoxW() called")
        cpu.regs[EAX] = 1  # IDOK
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "MessageBoxW", _MessageBoxW)

    # GetActiveWindow() -> HWND
    def _GetActiveWindow(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # NULL (no active window)

    stubs.register_handler("user32.dll", "GetActiveWindow", _GetActiveWindow)

    # GetDesktopWindow() -> HWND
    def _GetDesktopWindow(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x1000  # fake desktop HWND; no args, stdcall

    stubs.register_handler("user32.dll", "GetDesktopWindow", _GetDesktopWindow)

    # GetForegroundWindow() -> HWND
    def _GetForegroundWindow(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xABCD  # fake HWND

    stubs.register_handler("user32.dll", "GetForegroundWindow", _GetForegroundWindow)

    # SetForegroundWindow(HWND hWnd) -> BOOL
    def _SetForegroundWindow(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "SetForegroundWindow", _SetForegroundWindow)

    # SetWindowPos(HWND, HWND, int, int, int, int, UINT) -> BOOL
    def _SetWindowPos(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] SetWindowPos: no real window — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE; 7 stdcall args
        cleanup_stdcall(cpu, memory, 28)

    stubs.register_handler("user32.dll", "SetWindowPos", _SetWindowPos)

    # MoveWindow(HWND, int, int, int, int, BOOL) -> BOOL
    def _MoveWindow(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] MoveWindow: no real window — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE; 6 stdcall args
        cleanup_stdcall(cpu, memory, 24)

    stubs.register_handler("user32.dll", "MoveWindow", _MoveWindow)

    # GetWindowRect(HWND hWnd, LPRECT lpRect) -> BOOL
    def _GetWindowRect(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # FALSE (no real window)
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "GetWindowRect", _GetWindowRect)

    # GetClientRect(HWND hWnd, LPRECT lpRect) -> BOOL
    def _GetClientRect(cpu: "CPU") -> None:
        lp_rect = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if lp_rect:
            memory.write32(lp_rect,      0)
            memory.write32(lp_rect + 4,  0)
            memory.write32(lp_rect + 8,  800)
            memory.write32(lp_rect + 12, 600)
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "GetClientRect", _GetClientRect)

    # GetSystemMetrics(int nIndex) -> int
    def _GetSystemMetrics(cpu: "CPU") -> None:
        n_index = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        # SM_CXSCREEN=0, SM_CYSCREEN=1
        if n_index == 0:
            cpu.regs[EAX] = 800
        elif n_index == 1:
            cpu.regs[EAX] = 600
        else:
            cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "GetSystemMetrics", _GetSystemMetrics)

    # UpdateWindow(HWND hWnd) -> BOOL
    def _UpdateWindow(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] UpdateWindow: no real window — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE; 1 stdcall arg
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "UpdateWindow", _UpdateWindow)

    # InvalidateRect(HWND hWnd, RECT*, BOOL) -> BOOL
    def _InvalidateRect(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] InvalidateRect: no real window — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE; 3 stdcall args
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("user32.dll", "InvalidateRect", _InvalidateRect)

    # SetWindowTextA(HWND hWnd, LPCSTR lpString) -> BOOL
    def _SetWindowTextA(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] SetWindowTextA: no real window — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE; 2 stdcall args
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "SetWindowTextA", _SetWindowTextA)

    # SetWindowTextW(HWND hWnd, LPCWSTR lpString) -> BOOL
    def _SetWindowTextW(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] SetWindowTextW: no real window — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE; 2 stdcall args
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "SetWindowTextW", _SetWindowTextW)

    # GetWindowTextA(HWND hWnd, LPSTR lpString, int nMaxCount) -> int
    # Returns "admin" — used by the login dialog proc to read edit control text.
    def _GetWindowTextA(cpu: "CPU") -> None:
        lp_string  = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        n_max      = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cred = "admin"
        length = min(len(cred), n_max - 1) if n_max > 0 else 0
        if lp_string != 0 and n_max > 0:
            for i in range(length):
                memory.write8((lp_string + i) & 0xFFFFFFFF, ord(cred[i]))
            memory.write8((lp_string + length) & 0xFFFFFFFF, 0)
        cpu.regs[EAX] = length
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("user32.dll", "GetWindowTextA", _GetWindowTextA)

    # GetWindowLongA(HWND, int) -> LONG
    def _GetWindowLongA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # 2 stdcall args
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "GetWindowLongA", _GetWindowLongA)

    # SetWindowLongA(HWND, int, LONG) -> LONG (previous value)
    def _SetWindowLongA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # previous value; 3 stdcall args
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("user32.dll", "SetWindowLongA", _SetWindowLongA)

    # LoadCursorA(hInstance, lpCursorName) -> HCURSOR
    def _LoadCursorA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x1001  # fake HCURSOR
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "LoadCursorA", _LoadCursorA)

    # LoadIconA(hInstance, lpIconName) -> HICON
    def _LoadIconA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x1002  # fake HICON
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "LoadIconA", _LoadIconA)

    # SetCursor(HCURSOR hCursor) -> HCURSOR
    def _SetCursor(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x1001  # previous cursor
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "SetCursor", _SetCursor)

    # ShowCursor(BOOL bShow) -> int
    def _ShowCursor(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # display counter; 1 stdcall arg
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "ShowCursor", _ShowCursor)

    # PeekMessageA(LPMSG lpMsg, HWND, UINT, UINT, UINT) -> BOOL
    def _PeekMessageA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # FALSE = no message; 5 stdcall args
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("user32.dll", "PeekMessageA", _PeekMessageA)

    # GetMessageA(LPMSG lpMsg, HWND, UINT, UINT) -> BOOL
    def _GetMessageA(cpu: "CPU") -> None:
        # Return WM_QUIT (0x12) to exit message loops
        lp_msg = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if lp_msg:
            memory.write32(lp_msg,     0xABCD)  # HWND
            memory.write32(lp_msg + 4, 0x12)    # WM_QUIT
        cpu.regs[EAX] = 0  # FALSE = WM_QUIT
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "GetMessageA", _GetMessageA)

    # TranslateMessage(MSG*) -> BOOL
    def _TranslateMessage(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # 1 stdcall arg
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "TranslateMessage", _TranslateMessage)

    # DispatchMessageA(MSG*) -> LRESULT
    def _DispatchMessageA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # 1 stdcall arg
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "DispatchMessageA", _DispatchMessageA)

    # PostQuitMessage(int nExitCode) -> void
    def _PostQuitMessage(cpu: "CPU") -> None:
        logger.debug("handlers", "[Win32] PostQuitMessage()")
        cleanup_stdcall(cpu, memory, 4)  # 1 stdcall arg, void return

    stubs.register_handler("user32.dll", "PostQuitMessage", _PostQuitMessage)

    # GetLastActivePopup(HWND hWnd) -> HWND
    def _GetLastActivePopup(cpu: "CPU") -> None:
        h_wnd = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = h_wnd  # return the same handle passed in
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "GetLastActivePopup", _GetLastActivePopup)

    # DialogBoxParamA(hInstance, lpTemplateName, hWndParent, lpDialogFunc, dwInitParam) -> INT_PTR
    def _DialogBoxParamA(cpu: "CPU") -> None:
        lp_template = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        name: str = f"#{lp_template}" if lp_template <= 0xFFFF else ""
        if lp_template > 0xFFFF:
            for i in range(64):
                c = memory.read8(lp_template + i)
                if not c:
                    break
                name += chr(c)

        # Login dialog (#114): invoke the dialog proc with WM_COMMAND/IDOK so it
        # calls GetDlgItemTextA (which we stub to return "admin") and stores credentials.
        # Stack trick: instead of cleanupStdcall + return IDOK directly, we set up the
        # stack so that the stub's RET jumps to lpDialogFunc. After the dialog proc
        # returns via RET 16, execution lands at DIALOG_TRAMPOLINE which sets EAX=1 (IDOK)
        # and then RET returns to the original DialogBoxParamA call site.
        if name == "#114":
            ret_addr       = memory.read32((cpu.regs[ESP])      & 0xFFFFFFFF)
            lp_dialog_func = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
            logger.debug(
                "handlers",
                f"[Win32] DialogBoxParamA(\"{name}\") - invoking dialog proc "
                f"0x{lp_dialog_func:08x} with WM_COMMAND/IDOK",
            )
            # Shrink ESP by 4 to create one extra stack slot, then write:
            #   [ESP+ 0] lpDialogFunc      <- stub RET pops → EIP jumps to dialog proc
            #   [ESP+ 4] DIALOG_TRAMPOLINE <- return address seen by dialog proc (RET 16 pops this)
            #   [ESP+ 8] 0xABCD            <- hwnd (arg1)
            #   [ESP+12] 0x111             <- WM_COMMAND (arg2)
            #   [ESP+16] 1                 <- wParam = IDOK (arg3)
            #   [ESP+20] 0                 <- lParam (arg4)
            #   [ESP+24] retAddr           <- trampoline's RET jumps here → original call site
            cpu.regs[ESP] = (cpu.regs[ESP] - 4) & 0xFFFFFFFF
            memory.write32((cpu.regs[ESP] +  0) & 0xFFFFFFFF, lp_dialog_func)
            memory.write32((cpu.regs[ESP] +  4) & 0xFFFFFFFF, DIALOG_TRAMPOLINE)
            memory.write32((cpu.regs[ESP] +  8) & 0xFFFFFFFF, 0xABCD)
            memory.write32((cpu.regs[ESP] + 12) & 0xFFFFFFFF, 0x111)
            memory.write32((cpu.regs[ESP] + 16) & 0xFFFFFFFF, 1)
            memory.write32((cpu.regs[ESP] + 20) & 0xFFFFFFFF, 0)
            memory.write32((cpu.regs[ESP] + 24) & 0xFFFFFFFF, ret_addr)
            # Do NOT call cleanup_stdcall — RET at stub+2 goes directly to lpDialogFunc
            return

        logger.debug("handlers", f'[Win32] DialogBoxParamA("{name}") -> IDOK')
        cpu.regs[EAX] = 1  # IDOK=1
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("user32.dll", "DialogBoxParamA", _DialogBoxParamA)

    # DialogBoxParamW(hInstance, lpTemplateName, hWndParent, lpDialogFunc, dwInitParam) -> INT_PTR
    def _DialogBoxParamW(cpu: "CPU") -> None:
        logger.debug("handlers", "[Win32] DialogBoxParamW() -> 0")
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("user32.dll", "DialogBoxParamW", _DialogBoxParamW)

    # DialogBoxIndirectParamA(hInstance, hDialogTemplate, hWndParent, lpDialogFunc, dwInitParam) -> INT_PTR
    def _DialogBoxIndirectParamA(cpu: "CPU") -> None:
        logger.debug("handlers", "[Win32] DialogBoxIndirectParamA() -> 0")
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("user32.dll", "DialogBoxIndirectParamA", _DialogBoxIndirectParamA)

    # CreateDialogParamA(hInstance, lpTemplateName, hWndParent, lpDialogFunc, dwInitParam) -> HWND
    def _CreateDialogParamA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xABCE  # fake HWND for the main game dialog
        logger.debug("handlers", "[Win32] CreateDialogParamA() -> 0xABCE")
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("user32.dll", "CreateDialogParamA", _CreateDialogParamA)

    # EndDialog(HWND hDlg, INT_PTR nResult) -> BOOL
    def _EndDialog(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] EndDialog: no real dialog — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "EndDialog", _EndDialog)

    # RegisterClassA(WNDCLASSA*) -> ATOM
    def _RegisterClassA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xC001  # fake ATOM
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "RegisterClassA", _RegisterClassA)

    # RegisterClassExA(WNDCLASSEXA*) -> ATOM
    def _RegisterClassExA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xC001  # fake ATOM
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "RegisterClassExA", _RegisterClassExA)

    # RegisterClassW(WNDCLASSW*) -> ATOM
    def _RegisterClassW(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xC002  # fake ATOM
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "RegisterClassW", _RegisterClassW)

    # RegisterClassExW(WNDCLASSEXW*) -> ATOM
    def _RegisterClassExW(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xC002  # fake ATOM
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "RegisterClassExW", _RegisterClassExW)

    # UnregisterClassA(LPCSTR lpClassName, HINSTANCE hInstance) -> BOOL
    def _UnregisterClassA(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] UnregisterClassA: no real window class registry — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "UnregisterClassA", _UnregisterClassA)

    # CreateWindowExA(dwExStyle, lpClassName, lpWindowName, dwStyle, X, Y, nWidth, nHeight,
    #                 hWndParent, hMenu, hInstance, lpParam) -> HWND
    def _CreateWindowExA(cpu: "CPU") -> None:
        lp_class_name = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        name: str
        if lp_class_name > 0xFFFF:
            name = ""
            for i in range(64):
                c = memory.read8(lp_class_name + i)
                if not c:
                    break
                name += chr(c)
        else:
            name = f"#{lp_class_name}"
        logger.debug("handlers", f'[Win32] CreateWindowExA("{name}") -> 0xABCD')
        cpu.regs[EAX] = 0xABCD  # fake HWND
        cleanup_stdcall(cpu, memory, 48)

    stubs.register_handler("user32.dll", "CreateWindowExA", _CreateWindowExA)

    # CreateWindowExW(dwExStyle, lpClassName, lpWindowName, dwStyle, X, Y, nWidth, nHeight,
    #                 hWndParent, hMenu, hInstance, lpParam) -> HWND
    def _CreateWindowExW(cpu: "CPU") -> None:
        logger.debug("handlers", "[Win32] CreateWindowExW() -> 0xABCD")
        cpu.regs[EAX] = 0xABCD  # fake HWND
        cleanup_stdcall(cpu, memory, 48)

    stubs.register_handler("user32.dll", "CreateWindowExW", _CreateWindowExW)

    # DestroyWindow(HWND hWnd) -> BOOL
    def _DestroyWindow(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] DestroyWindow: no real window — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "DestroyWindow", _DestroyWindow)

    # ShowWindow(HWND hWnd, int nCmdShow) -> BOOL
    def _ShowWindow(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # previously hidden
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "ShowWindow", _ShowWindow)

    # GetDC(HWND hWnd) -> HDC
    def _GetDC(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x1DC  # fake HDC
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "GetDC", _GetDC)

    # ReleaseDC(HWND hWnd, HDC hDC) -> int
    def _ReleaseDC(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] ReleaseDC: no real DC — returning 0")
        cpu.regs[EAX] = 0  # not released (no real DC)
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "ReleaseDC", _ReleaseDC)

    # GetWindowDC(HWND hWnd) -> HDC
    def _GetWindowDC(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x1DC  # fake HDC; 1 stdcall arg
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "GetWindowDC", _GetWindowDC)

    # SendMessageA(HWND, UINT, WPARAM, LPARAM) -> LRESULT
    def _SendMessageA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "SendMessageA", _SendMessageA)

    # SendMessageW(HWND, UINT, WPARAM, LPARAM) -> LRESULT
    def _SendMessageW(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "SendMessageW", _SendMessageW)

    # PostMessageA(HWND, UINT, WPARAM, LPARAM) -> BOOL
    def _PostMessageA(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] PostMessageA: no real message queue — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "PostMessageA", _PostMessageA)

    # PostMessageW(HWND, UINT, WPARAM, LPARAM) -> BOOL
    def _PostMessageW(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] PostMessageW: no real message queue — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "PostMessageW", _PostMessageW)

    # GetDlgItem(HWND hDlg, int nIDDlgItem) -> HWND
    def _GetDlgItem(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xABCF  # fake child HWND
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "GetDlgItem", _GetDlgItem)

    # SetDlgItemTextA(HWND hDlg, int nIDDlgItem, LPCSTR lpString) -> BOOL
    def _SetDlgItemTextA(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] SetDlgItemTextA: no real dialog — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("user32.dll", "SetDlgItemTextA", _SetDlgItemTextA)

    # GetDlgItemTextA(HWND hDlg, int nIDDlgItem, LPSTR lpString, int nMaxCount) -> UINT
    # Returns "admin" for all controls — used by login dialog (#114) for username and password.
    def _GetDlgItemTextA(cpu: "CPU") -> None:
        n_id_dlg_item = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        lp_string     = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        n_max_count   = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        cred = "admin"
        length = min(len(cred), n_max_count - 1) if n_max_count > 0 else 0
        if lp_string != 0 and n_max_count > 0:
            for i in range(length):
                memory.write8((lp_string + i) & 0xFFFFFFFF, ord(cred[i]))
            memory.write8((lp_string + length) & 0xFFFFFFFF, 0)
        logger.debug("handlers", f'[Win32] GetDlgItemTextA(ctrl={n_id_dlg_item}) -> "{cred}"')
        cpu.regs[EAX] = length
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "GetDlgItemTextA", _GetDlgItemTextA)

    # SendDlgItemMessageA(HWND, int, UINT, WPARAM, LPARAM) -> LRESULT
    def _SendDlgItemMessageA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("user32.dll", "SendDlgItemMessageA", _SendDlgItemMessageA)

    # EnableWindow(HWND hWnd, BOOL bEnable) -> BOOL
    def _EnableWindow(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # was not disabled
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "EnableWindow", _EnableWindow)

    # IsWindow(HWND hWnd) -> BOOL
    def _IsWindow(cpu: "CPU") -> None:
        hwnd = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = 1 if hwnd != 0 else 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "IsWindow", _IsWindow)

    # SetFocus(HWND hWnd) -> HWND (previous focus)
    def _SetFocus(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xABCD  # previous focus
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "SetFocus", _SetFocus)

    # GetFocus() -> HWND
    def _GetFocus(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xABCE  # fake focused HWND
        cleanup_stdcall(cpu, memory, 0)

    stubs.register_handler("user32.dll", "GetFocus", _GetFocus)

    # DefWindowProcA(HWND, UINT, WPARAM, LPARAM) -> LRESULT
    def _DefWindowProcA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "DefWindowProcA", _DefWindowProcA)

    # DefWindowProcW(HWND, UINT, WPARAM, LPARAM) -> LRESULT
    def _DefWindowProcW(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "DefWindowProcW", _DefWindowProcW)

    # DefDlgProcA(HWND, UINT, WPARAM, LPARAM) -> LRESULT
    def _DefDlgProcA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "DefDlgProcA", _DefDlgProcA)

    # IsDialogMessageA(HWND hDlg, LPMSG lpMsg) -> BOOL
    def _IsDialogMessageA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # FALSE - not a dialog message
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "IsDialogMessageA", _IsDialogMessageA)

    # CallWindowProcA(WNDPROC, HWND, UINT, WPARAM, LPARAM) -> LRESULT
    def _CallWindowProcA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("user32.dll", "CallWindowProcA", _CallWindowProcA)

    # CheckDlgButton(HWND, int, UINT) -> BOOL
    def _CheckDlgButton(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] CheckDlgButton: no real dialog — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("user32.dll", "CheckDlgButton", _CheckDlgButton)

    # IsDlgButtonChecked(HWND, int) -> UINT
    def _IsDlgButtonChecked(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # BST_UNCHECKED
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "IsDlgButtonChecked", _IsDlgButtonChecked)

    # SetDlgItemInt(HWND, int, UINT, BOOL) -> BOOL
    def _SetDlgItemInt(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] SetDlgItemInt: no real dialog — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "SetDlgItemInt", _SetDlgItemInt)

    # GetDlgItemInt(HWND, int, BOOL*, BOOL) -> UINT
    def _GetDlgItemInt(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "GetDlgItemInt", _GetDlgItemInt)

    # RedrawWindow(HWND, RECT*, HRGN, UINT) -> BOOL
    def _RedrawWindow(cpu: "CPU") -> None:
        logger.warn("handlers", "[USER32] RedrawWindow: no real window — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "RedrawWindow", _RedrawWindow)

    # ── gdi32.dll ─────────────────────────────────────────────────────────────

    # DeleteDC(HDC hDC) -> BOOL
    def _DeleteDC(cpu: "CPU") -> None:
        logger.warn("handlers", "[GDI32] DeleteDC: no real DC — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE; 1 stdcall arg
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("gdi32.dll", "DeleteDC", _DeleteDC)

    # CreateCompatibleDC(HDC hDC) -> HDC
    def _CreateCompatibleDC(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x1DC  # fake HDC; 1 stdcall arg
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("gdi32.dll", "CreateCompatibleDC", _CreateCompatibleDC)

    # CreateCompatibleBitmap(HDC hDC, int cx, int cy) -> HBITMAP
    def _CreateCompatibleBitmap(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x1DB  # fake HBITMAP; 3 stdcall args
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("gdi32.dll", "CreateCompatibleBitmap", _CreateCompatibleBitmap)

    # SelectObject(HDC hDC, HGDIOBJ h) -> HGDIOBJ
    def _SelectObject(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # NULL (previous object); 2 stdcall args
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("gdi32.dll", "SelectObject", _SelectObject)

    # DeleteObject(HGDIOBJ ho) -> BOOL
    def _DeleteObject(cpu: "CPU") -> None:
        logger.warn("handlers", "[GDI32] DeleteObject: no real GDI object — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE; 1 stdcall arg
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("gdi32.dll", "DeleteObject", _DeleteObject)

    # BitBlt(HDC hDC, int x, int y, int cx, int cy, HDC hSrcDC, int x1, int y1, DWORD rop) -> BOOL
    def _BitBlt(cpu: "CPU") -> None:
        logger.warn("handlers", "[GDI32] BitBlt: no real DC — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE; 9 stdcall args
        cleanup_stdcall(cpu, memory, 36)

    stubs.register_handler("gdi32.dll", "BitBlt", _BitBlt)

    # GetDeviceCaps(HDC hDC, int nIndex) -> int
    def _GetDeviceCaps(cpu: "CPU") -> None:
        n_index = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        # HORZRES=8, VERTRES=10, BITSPIXEL=12, PLANES=14, LOGPIXELSX=88, LOGPIXELSY=90
        if n_index == 8:
            val = 800
        elif n_index == 10:
            val = 600
        elif n_index == 12:
            val = 32
        elif n_index == 14:
            val = 1
        elif n_index in (88, 90):
            val = 96
        else:
            val = 0
        cpu.regs[EAX] = val
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("gdi32.dll", "GetDeviceCaps", _GetDeviceCaps)

    # SetDIBitsToDevice(...) -> int (13 stdcall args)
    def _SetDIBitsToDevice(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # 13 stdcall args
        cleanup_stdcall(cpu, memory, 52)

    stubs.register_handler("gdi32.dll", "SetDIBitsToDevice", _SetDIBitsToDevice)

    # StretchDIBits(...) -> int (13 stdcall args)
    def _StretchDIBits(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # 13 stdcall args
        cleanup_stdcall(cpu, memory, 52)

    stubs.register_handler("gdi32.dll", "StretchDIBits", _StretchDIBits)

    # CreateDIBSection(HDC, BITMAPINFO*, UINT, void**, HANDLE, DWORD) -> HBITMAP
    def _CreateDIBSection(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # NULL HBITMAP; 6 stdcall args
        cleanup_stdcall(cpu, memory, 24)

    stubs.register_handler("gdi32.dll", "CreateDIBSection", _CreateDIBSection)

    # GetStockObject(int fnObject) -> HGDIOBJ
    def _GetStockObject(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x1DA  # fake HGDIOBJ; 1 stdcall arg
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("gdi32.dll", "GetStockObject", _GetStockObject)

    # CreateSolidBrush(COLORREF color) -> HBRUSH
    def _CreateSolidBrush(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x1D9  # fake HBRUSH; 1 stdcall arg
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("gdi32.dll", "CreateSolidBrush", _CreateSolidBrush)

    # CreateFontA(...) -> HFONT (14 stdcall args)
    def _CreateFontA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x1D8  # fake HFONT; 14 stdcall args
        cleanup_stdcall(cpu, memory, 56)

    stubs.register_handler("gdi32.dll", "CreateFontA", _CreateFontA)

    # TextOutA(HDC hDC, int x, int y, LPCSTR lpString, int c) -> BOOL
    def _TextOutA(cpu: "CPU") -> None:
        logger.warn("handlers", "[GDI32] TextOutA: no real DC — returning FALSE")
        cpu.regs[EAX] = 0  # FALSE; 5 stdcall args
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("gdi32.dll", "TextOutA", _TextOutA)

    # SetBkMode(HDC hDC, int iMode) -> int
    def _SetBkMode(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # previous mode; 2 stdcall args
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("gdi32.dll", "SetBkMode", _SetBkMode)

    # SetTextColor(HDC hDC, COLORREF color) -> COLORREF
    def _SetTextColor(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # previous color; 2 stdcall args
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("gdi32.dll", "SetTextColor", _SetTextColor)
