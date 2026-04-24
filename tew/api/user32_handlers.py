"""user32.dll and gdi32.dll handler registrations.

Window management is backed by SDL2 via WindowManager.  Dialog templates are
parsed from the PE resource section via PEResources.  Rendering uses
dialog_renderer.py.

Handlers that require a real window system but whose implementation is not yet
complete halt with [UNIMPLEMENTED] so the missing path can be identified and
implemented.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall
from tew.api._state import CRTState
from tew.api.window_manager import (
    WindowManager,
    WM_INITDIALOG, BM_GETCHECK, BM_SETCHECK,
    du_to_px_x, du_to_px_y,
)
from tew.logger import logger


# ── Dialog proc sentinel ──────────────────────────────────────────────────────
# A HLT instruction (0xF4) written into emulator memory at a stable address.
# Used as the return address when invoking dialog procs via _invoke_emulated_proc;
# when the proc does RET it jumps here, the HLT fires, cpu.halted is set, and our
# sub-loop knows the call completed.
#
# Allocated lazily on first use via state.simple_alloc so that _state doesn't
# need to know about the sentinel at construction time.
_DIALOG_SENTINEL_ADDR: int = 0   # set by _get_dialog_sentinel()


class _GdiObj:
    """GDI object record in the emulator's handle table."""
    __slots__ = ("kind", "color", "style", "is_stock")

    def __init__(
        self, kind: str, color: int = 0, style: int = 0, *, is_stock: bool = False
    ) -> None:
        self.kind     = kind      # "brush" | "pen" | "font" | "palette"
        self.color    = color     # COLORREF (0x00BBGGRR)
        self.style    = style     # BS_NULL=1, PS_NULL=5; 0 = solid
        self.is_stock = is_stock


def _get_dialog_sentinel(state: "CRTState", memory: "Memory") -> int:
    global _DIALOG_SENTINEL_ADDR
    if _DIALOG_SENTINEL_ADDR == 0:
        _DIALOG_SENTINEL_ADDR = state.simple_alloc(4)
        memory.write8(_DIALOG_SENTINEL_ADDR, 0xF4)   # HLT
        logger.debug("dialog", f"[DialogSentinel] allocated at 0x{_DIALOG_SENTINEL_ADDR:08x}")
    return _DIALOG_SENTINEL_ADDR


def _invoke_emulated_proc(
    cpu: "CPU",
    memory: "Memory",
    proc_addr: int,
    args: list[int],
    sentinel: int,
    max_steps: int = 5_000_000,
) -> int:
    """Call emulated x86 code (stdcall) and return EAX.

    Saves and restores full CPU state around the call so side effects are
    limited to memory and CRTState (registry, heap, sockets, etc.).

    args is the argument list in left-to-right (C) order; they are pushed
    right-to-left onto the stack as stdcall requires.
    """
    saved = cpu.save_state()

    # Build a fresh stack frame on top of the current stack.
    # Calling convention (stdcall / cdecl):
    #   1. Caller pushes args right-to-left (last arg at highest address).
    #   2. CALL instruction pushes the return address on top (lowest address).
    # At function entry the callee therefore sees:
    #   [ESP+0]  = return address
    #   [ESP+4]  = first arg
    #   [ESP+8]  = second arg   ...
    esp = cpu.regs[ESP]

    # Step 1 — push args right-to-left (last arg pushed first)
    for arg in reversed(args):
        esp = (esp - 4) & 0xFFFFFFFF
        memory.write32(esp, arg & 0xFFFFFFFF)

    # Step 2 — push return address on top (simulates the CALL instruction)
    esp = (esp - 4) & 0xFFFFFFFF
    memory.write32(esp, sentinel)

    cpu.regs[ESP] = esp
    cpu.eip = proc_addr & 0xFFFFFFFF
    cpu.halted = False

    steps = 0
    while not cpu.halted and steps < max_steps:
        if cpu.eip == sentinel:
            break
        cpu.step()
        steps += 1

    if steps >= max_steps:
        logger.warn("dialog", f"[_invoke_emulated_proc] max_steps reached calling 0x{proc_addr:08x}")

    result = cpu.regs[EAX]

    cpu.restore_state(saved)
    cpu.halted = False
    return result


def register_user32_gdi32_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register all user32.dll and gdi32.dll handlers."""

    wm: WindowManager = state.window_manager

    # ── Win32 hook infrastructure ─────────────────────────────────────────────
    # idHook constants
    _WH_KEYBOARD   = 2
    _WH_CBT        = 5
    _WH_MOUSE      = 7
    _WH_GETMESSAGE = 8
    _HC_ACTION     = 0
    _PM_REMOVE     = 1

    # Active hooks: hhook handle → (idHook, lpfn)
    _winhooks: dict[int, tuple[int, int]] = {}
    _next_hhook = [0xA000]

    def _halt(name: str):
        """Return a handler that halts with an UNIMPLEMENTED log."""
        def _h(cpu: "CPU") -> None:
            logger.error("handlers", f"[UNIMPLEMENTED] {name} — halting")
            cpu.halted = True
        return _h

    # ── user32.dll ────────────────────────────────────────────────────────────

    # MessageBoxA / MessageBoxW — show a real SDL2 dialog so errors are visible.
    #
    # Win32 button-type → list of (label, buttonid, flags) tuples.
    # buttonid is the Win32 IDOK/IDYES/etc. value returned to the caller.
    # SDL_MESSAGEBOX_BUTTON_RETURNKEY_DEFAULT marks the Enter key default.
    # SDL_MESSAGEBOX_BUTTON_ESCAPEKEY_DEFAULT marks the Escape key default.
    from sdl2 import (
        SDL_ShowMessageBox,
        SDL_MessageBoxData,
        SDL_MessageBoxButtonData,
        SDL_MESSAGEBOX_ERROR,
        SDL_MESSAGEBOX_WARNING,
        SDL_MESSAGEBOX_INFORMATION,
        SDL_MESSAGEBOX_BUTTON_RETURNKEY_DEFAULT,
        SDL_MESSAGEBOX_BUTTON_ESCAPEKEY_DEFAULT,
    )
    import ctypes as _ctypes

    # (label, win32_id, sdl_flags)
    _MSGBOX_BUTTONS: dict[int, list[tuple[bytes, int, int]]] = {
        0: [(b"OK",     1, SDL_MESSAGEBOX_BUTTON_RETURNKEY_DEFAULT | SDL_MESSAGEBOX_BUTTON_ESCAPEKEY_DEFAULT)],
        1: [(b"OK",     1, SDL_MESSAGEBOX_BUTTON_RETURNKEY_DEFAULT),
            (b"Cancel", 2, SDL_MESSAGEBOX_BUTTON_ESCAPEKEY_DEFAULT)],
        2: [(b"Abort",  3, 0),
            (b"Retry",  4, SDL_MESSAGEBOX_BUTTON_RETURNKEY_DEFAULT),
            (b"Ignore", 5, SDL_MESSAGEBOX_BUTTON_ESCAPEKEY_DEFAULT)],
        3: [(b"Yes",    6, SDL_MESSAGEBOX_BUTTON_RETURNKEY_DEFAULT),
            (b"No",     7, 0),
            (b"Cancel", 2, SDL_MESSAGEBOX_BUTTON_ESCAPEKEY_DEFAULT)],
        4: [(b"Yes",    6, SDL_MESSAGEBOX_BUTTON_RETURNKEY_DEFAULT),
            (b"No",     7, SDL_MESSAGEBOX_BUTTON_ESCAPEKEY_DEFAULT)],
        5: [(b"Retry",  4, SDL_MESSAGEBOX_BUTTON_RETURNKEY_DEFAULT),
            (b"Cancel", 2, SDL_MESSAGEBOX_BUTTON_ESCAPEKEY_DEFAULT)],
    }

    # MB_ICON* flags occupy bits 4-7 of uType.
    _MSGBOX_ICON_FLAGS: dict[int, int] = {
        0x10: SDL_MESSAGEBOX_ERROR,        # MB_ICONERROR / MB_ICONSTOP
        0x20: SDL_MESSAGEBOX_ERROR,        # MB_ICONHAND
        0x30: SDL_MESSAGEBOX_WARNING,      # MB_ICONWARNING / MB_ICONEXCLAMATION
        0x40: SDL_MESSAGEBOX_INFORMATION,  # MB_ICONINFORMATION / MB_ICONASTERISK
    }

    def _show_messagebox(caption: str, text: str, u_type: int) -> int:
        btn_type  = u_type & 0x0F
        icon_key  = u_type & 0x70
        sdl_flags = _MSGBOX_ICON_FLAGS.get(icon_key, SDL_MESSAGEBOX_INFORMATION)
        buttons   = _MSGBOX_BUTTONS.get(btn_type, _MSGBOX_BUTTONS[0])

        btn_array = (SDL_MessageBoxButtonData * len(buttons))()
        for i, (label, bid, bflags) in enumerate(buttons):
            btn_array[i].flags    = bflags
            btn_array[i].buttonid = bid
            btn_array[i].text     = label

        data = SDL_MessageBoxData()
        data.flags      = sdl_flags
        data.window     = None
        data.title      = caption.encode("latin-1", errors="replace")
        data.message    = text.encode("latin-1", errors="replace")
        data.numbuttons = len(buttons)
        data.buttons    = btn_array
        data.colorScheme = None

        button_id = _ctypes.c_int(-1)
        ret = SDL_ShowMessageBox(data, _ctypes.byref(button_id))
        if ret < 0:
            logger.error("handlers", f"[Win32] SDL_ShowMessageBox failed (ret={ret}); defaulting to first button")
            return buttons[0][1]
        return button_id.value

    def _read_cstr(addr: int, max_len: int = 1024) -> str:
        out = []
        for i in range(max_len):
            ch = memory.read8(addr + i)
            if ch == 0:
                break
            out.append(chr(ch))
        return "".join(out)

    def _read_wstr(addr: int, max_len: int = 512) -> str:
        out = []
        for i in range(max_len):
            lo = memory.read8(addr + i * 2)
            hi = memory.read8(addr + i * 2 + 1)
            cp = lo | (hi << 8)
            if cp == 0:
                break
            out.append(chr(cp))
        return "".join(out)

    def _MessageBoxA(cpu: "CPU") -> None:
        lp_text    = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        lp_caption = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        u_type     = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        text    = _read_cstr(lp_text)
        caption = _read_cstr(lp_caption)
        result  = _show_messagebox(caption, text, u_type)
        logger.info(
            "handlers",
            f'[Win32] MessageBoxA("{caption}", "{text.replace(chr(10), "\\n")}")'
            f" type=0x{u_type:x} -> {result}",
        )
        cpu.regs[EAX] = result
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "MessageBoxA", _MessageBoxA)

    # MessageBoxW(HWND hWnd, LPCWSTR lpText, LPCWSTR lpCaption, UINT uType) -> int
    def _MessageBoxW(cpu: "CPU") -> None:
        lp_text    = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        lp_caption = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        u_type     = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        text    = _read_wstr(lp_text)
        caption = _read_wstr(lp_caption)
        result  = _show_messagebox(caption, text, u_type)
        logger.info(
            "handlers",
            f'[Win32] MessageBoxW("{caption}", "{text.replace(chr(10), "\\n")}")'
            f" type=0x{u_type:x} -> {result}",
        )
        cpu.regs[EAX] = result
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "MessageBoxW", _MessageBoxW)

    # GetActiveWindow() -> HWND  (no args — NULL means no active window)
    def _GetActiveWindow(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # NULL

    stubs.register_handler("user32.dll", "GetActiveWindow", _GetActiveWindow)

    # GetDesktopWindow() -> HWND
    def _GetDesktopWindow(cpu: "CPU") -> None:
        # Return a stable fake desktop HWND.  The desktop is never passed to
        # GetDlgItem or SendMessage so a constant value is fine here.
        cpu.regs[EAX] = 0x0001

    stubs.register_handler("user32.dll", "GetDesktopWindow", _GetDesktopWindow)

    # GetForegroundWindow() -> HWND
    def _GetForegroundWindow(cpu: "CPU") -> None:
        # Return the first visible top-level window, or NULL.
        cpu.regs[EAX] = 0   # no windows yet

    stubs.register_handler("user32.dll", "GetForegroundWindow", _GetForegroundWindow)

    # SetForegroundWindow(HWND hWnd) -> BOOL
    # SDL2 owns the window; Win32 focus mechanics have no effect in this emulator.
    def _SetForegroundWindow(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "SetForegroundWindow", _SetForegroundWindow)

    # SetActiveWindow(HWND hWnd) -> HWND  (previously active window)
    def _SetActiveWindow(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # NULL — no previously active window
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "SetActiveWindow", _SetActiveWindow)

    # SetWindowsHookExA(int idHook, HOOKPROC lpfn, HINSTANCE hmod, DWORD dwThreadId) -> HHOOK
    def _SetWindowsHookExA(cpu: "CPU") -> None:
        id_hook = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_fn   = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        handle  = _next_hhook[0]
        _next_hhook[0] += 1
        _winhooks[handle] = (id_hook, lp_fn)
        logger.debug("handlers", f"SetWindowsHookExA(idHook={id_hook}, lpfn=0x{lp_fn:x}) -> 0x{handle:x}")
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "SetWindowsHookExA", _SetWindowsHookExA)

    # UnhookWindowsHookEx(HHOOK hhk) -> BOOL
    def _UnhookWindowsHookEx(cpu: "CPU") -> None:
        hhk = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        _winhooks.pop(hhk, None)
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "UnhookWindowsHookEx", _UnhookWindowsHookEx)

    # CallNextHookEx(HHOOK hhk, int nCode, WPARAM wParam, LPARAM lParam) -> LRESULT
    # We maintain a flat hook list (no chain), so there is no next hook.
    def _CallNextHookEx(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "CallNextHookEx", _CallNextHookEx)

    # SystemParametersInfoA(UINT uiAction, UINT uiParam, PVOID pvParam, UINT fWinIni) -> BOOL
    _SPI_GETSCREENSAVEACTIVE = 0x0010
    _SPI_GETWORKAREA         = 0x0030

    def _SystemParametersInfoA(cpu: "CPU") -> None:
        ui_action = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        pv_param  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if ui_action == _SPI_GETSCREENSAVEACTIVE:
            if pv_param:
                memory.write32(pv_param & 0xFFFFFFFF, 0)  # FALSE — screensaver off
        elif ui_action == _SPI_GETWORKAREA:
            if pv_param:
                # RECT {left, top, right, bottom}
                memory.write32((pv_param +  0) & 0xFFFFFFFF, 0)
                memory.write32((pv_param +  4) & 0xFFFFFFFF, 0)
                memory.write32((pv_param +  8) & 0xFFFFFFFF, _SM_CXSCREEN_MAX)
                memory.write32((pv_param + 12) & 0xFFFFFFFF, _SM_CYSCREEN_MAX)
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "SystemParametersInfoA", _SystemParametersInfoA)

    # SetWindowPos(HWND, HWND insertAfter, int X, int Y, int cx, int cy, UINT flags) -> BOOL
    def _SetWindowPos(cpu: "CPU") -> None:
        hwnd  = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        x     = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        y     = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        cx    = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        cy    = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        entry = wm.get_window(hwnd)
        if entry is not None:
            entry.x, entry.y, entry.cx, entry.cy = x, y, cx, cy
            logger.debug("handlers", f"[Win32] SetWindowPos(0x{hwnd:x}) -> ({x},{y}) {cx}x{cy}")
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 28)

    stubs.register_handler("user32.dll", "SetWindowPos", _SetWindowPos)

    # MoveWindow(HWND, int X, int Y, int nWidth, int nHeight, BOOL repaint) -> BOOL
    def _MoveWindow(cpu: "CPU") -> None:
        hwnd  = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        x     = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        y     = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cx    = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        cy    = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        entry = wm.get_window(hwnd)
        if entry is not None:
            entry.x, entry.y, entry.cx, entry.cy = x, y, cx, cy
            logger.debug("handlers", f"[Win32] MoveWindow(0x{hwnd:x}) -> ({x},{y}) {cx}x{cy}")
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 24)

    stubs.register_handler("user32.dll", "MoveWindow", _MoveWindow)

    # GetWindowRect(HWND hWnd, LPRECT lpRect) -> BOOL
    def _GetWindowRect(cpu: "CPU") -> None:
        h_wnd   = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_rect = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        entry = wm.get_window(h_wnd)
        if entry is not None and lp_rect:
            px_x  = du_to_px_x(entry.x)
            px_y  = du_to_px_y(entry.y)
            px_cx = du_to_px_x(entry.cx)
            px_cy = du_to_px_y(entry.cy)
            memory.write32(lp_rect,       px_x)
            memory.write32(lp_rect + 4,   px_y)
            memory.write32(lp_rect + 8,   px_x + px_cx)
            memory.write32(lp_rect + 12,  px_y + px_cy)
            cpu.regs[EAX] = 1  # TRUE
        else:
            cpu.regs[EAX] = 0  # FALSE
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "GetWindowRect", _GetWindowRect)

    # GetClientRect(HWND hWnd, LPRECT lpRect) -> BOOL
    def _GetClientRect(cpu: "CPU") -> None:
        h_wnd   = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_rect = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        entry = wm.get_window(h_wnd)
        if entry is not None and lp_rect:
            memory.write32(lp_rect,       0)
            memory.write32(lp_rect + 4,   0)
            memory.write32(lp_rect + 8,   du_to_px_x(entry.cx))
            memory.write32(lp_rect + 12,  du_to_px_y(entry.cy))
            cpu.regs[EAX] = 1  # TRUE
        else:
            cpu.regs[EAX] = 0  # FALSE
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "GetClientRect", _GetClientRect)

    # GetSystemMetrics(int nIndex) -> int
    # Cap SM_CXSCREEN/SM_CYSCREEN at 1024x768; the game sets its render target from
    # these values and a full-resolution window (e.g. 5160x2340) wastes resources.
    _SM_CXSCREEN_MAX = 1024
    _SM_CYSCREEN_MAX = 768

    def _GetSystemMetrics(cpu: "CPU") -> None:
        n_index = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if n_index == 0:        # SM_CXSCREEN
            cpu.regs[EAX] = _SM_CXSCREEN_MAX
        elif n_index == 1:      # SM_CYSCREEN
            cpu.regs[EAX] = _SM_CYSCREEN_MAX
        else:
            cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "GetSystemMetrics", _GetSystemMetrics)

    # GetKeyState(int nVirtKey) -> SHORT
    # Returns key state: high bit set = key down, low bit = toggle state.
    # We have no real keyboard input path; report all keys up and untoggled.
    def _GetKeyState(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "GetKeyState", _GetKeyState)

    # UpdateWindow(HWND hWnd) -> BOOL
    # Triggers WM_PAINT; we re-render via SDL on every DispatchMessageA call,
    # so no additional action is needed here.
    def _UpdateWindow(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "UpdateWindow", _UpdateWindow)

    # InvalidateRect(HWND hWnd, RECT*, BOOL) -> BOOL
    # Marks a region as needing repaint; SDL renders continuously, so no-op.
    def _InvalidateRect(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("user32.dll", "InvalidateRect", _InvalidateRect)

    # SetWindowTextA(HWND hWnd, LPCSTR lpString) -> BOOL
    def _SetWindowTextA(cpu: "CPU") -> None:
        h_wnd     = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_string = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        text = ""
        for i in range(256):
            ch = memory.read8((lp_string + i) & 0xFFFFFFFF)
            if ch == 0:
                break
            text += chr(ch)
        ok = wm.set_window_text(h_wnd, text)
        cpu.regs[EAX] = 1 if ok else 0
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "SetWindowTextA", _SetWindowTextA)

    # SetWindowTextW(HWND hWnd, LPCWSTR lpString) -> BOOL
    def _SetWindowTextW(cpu: "CPU") -> None:
        h_wnd     = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_string = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        from tew.api._state import read_wide_string
        text = read_wide_string(lp_string, memory)
        ok = wm.set_window_text(h_wnd, text)
        cpu.regs[EAX] = 1 if ok else 0
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "SetWindowTextW", _SetWindowTextW)

    # GetWindowTextA(HWND hWnd, LPSTR lpString, int nMaxCount) -> int
    # For real windows, returns the actual title stored in the window entry.
    # The login dialog relies on this for username/password controls.
    def _GetWindowTextA(cpu: "CPU") -> None:
        h_wnd      = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        lp_string  = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        n_max      = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        text = wm.get_window_text(h_wnd)
        if not text:
            # Fall back to "admin" for unknown HWNDs (login dialog compatibility)
            text = "admin"
        length = min(len(text), n_max - 1) if n_max > 0 else 0
        if lp_string != 0 and n_max > 0:
            for i in range(length):
                memory.write8((lp_string + i) & 0xFFFFFFFF, ord(text[i]))
            memory.write8((lp_string + length) & 0xFFFFFFFF, 0)
        cpu.regs[EAX] = length
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("user32.dll", "GetWindowTextA", _GetWindowTextA)

    # GetWindowLongA(HWND, int) -> LONG
    def _GetWindowLongA(cpu: "CPU") -> None:
        h_wnd  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        n_index = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        entry = wm.get_window(h_wnd)
        GWL_WNDPROC = -4
        GWL_STYLE   = -16
        if entry is not None:
            if n_index == GWL_WNDPROC:
                cpu.regs[EAX] = entry.wnd_proc_addr
            elif n_index == GWL_STYLE:
                cpu.regs[EAX] = entry.style
            else:
                cpu.regs[EAX] = 0
        else:
            cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "GetWindowLongA", _GetWindowLongA)

    # GetWindowThreadProcessId(HWND, LPDWORD) -> DWORD
    # Returns the TID that created the window; optionally writes PID to lpdwProcessId.
    def _GetWindowThreadProcessId(cpu: "CPU") -> None:
        h_wnd           = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lpdw_process_id = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        entry = wm.get_window(h_wnd)
        if entry is None:
            logger.error("handlers", f"[GetWindowThreadProcessId] unknown hwnd=0x{h_wnd:08x} — halting")
            cpu.halted = True
            return
        if lpdw_process_id:
            memory.write32(lpdw_process_id & 0xFFFFFFFF, 1)  # our fake PID
        cpu.regs[EAX] = entry.creator_tid
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "GetWindowThreadProcessId", _GetWindowThreadProcessId)

    # SetWindowLongA(HWND, int, LONG) -> LONG (previous value)
    def _SetWindowLongA(cpu: "CPU") -> None:
        h_wnd    = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        n_index  = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        new_long = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        entry = wm.get_window(h_wnd)
        GWL_WNDPROC = -4
        GWL_STYLE   = -16
        prev = 0
        if entry is not None:
            if n_index == GWL_WNDPROC:
                prev = entry.wnd_proc_addr
                entry.wnd_proc_addr = new_long
            elif n_index == GWL_STYLE:
                prev = entry.style
                entry.style = new_long
        cpu.regs[EAX] = prev
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("user32.dll", "SetWindowLongA", _SetWindowLongA)

    # LoadCursorA(hInstance, lpCursorName) -> HCURSOR
    def _LoadCursorA(cpu: "CPU") -> None:
        # Return a sentinel; SDL cursor is set separately via SetCursor
        cpu.regs[EAX] = 0x1001
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "LoadCursorA", _LoadCursorA)

    # LoadIconA(hInstance, lpIconName) -> HICON
    def _LoadIconA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x1002
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "LoadIconA", _LoadIconA)

    # SetCursor(HCURSOR hCursor) -> HCURSOR (previous cursor)
    def _SetCursor(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x1001
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "SetCursor", _SetCursor)

    # ShowCursor(BOOL bShow) -> int (display counter)
    def _ShowCursor(cpu: "CPU") -> None:
        from sdl2 import SDL_ShowCursor, SDL_ENABLE, SDL_DISABLE
        b_show = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        SDL_ShowCursor(SDL_ENABLE if b_show else SDL_DISABLE)
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "ShowCursor", _ShowCursor)

    def _dispatch_winhooks(cpu: "CPU", msg_id: int, wparam: int, lp_msg: int) -> None:
        """Call any registered hooks appropriate for this message."""
        if not _winhooks:
            return
        sentinel = _get_dialog_sentinel(state, memory)
        for id_hook, lp_fn in list(_winhooks.values()):
            if id_hook == _WH_GETMESSAGE:
                _invoke_emulated_proc(
                    cpu, memory, lp_fn,
                    [_HC_ACTION, _PM_REMOVE, lp_msg], sentinel,
                )
            elif id_hook == _WH_KEYBOARD and msg_id in (0x0100, 0x0101):  # WM_KEYDOWN/WM_KEYUP
                _invoke_emulated_proc(
                    cpu, memory, lp_fn,
                    [_HC_ACTION, wparam, 0], sentinel,
                )

    # PeekMessageA(LPMSG lpMsg, HWND, UINT, UINT, UINT) -> BOOL
    # Pump SDL events and check our message queue; return FALSE if nothing there.
    def _PeekMessageA(cpu: "CPU") -> None:
        lp_msg = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if not wm.pump_sdl_events():
            # SDL_QUIT — post WM_QUIT so the caller's loop sees it
            if lp_msg:
                memory.write32(lp_msg,      0)
                memory.write32(lp_msg + 4,  0x0012)  # WM_QUIT
                memory.write32(lp_msg + 8,  0)
                memory.write32(lp_msg + 12, 0)
                memory.write32(lp_msg + 16, 0)
                memory.write32(lp_msg + 20, 0)
                memory.write32(lp_msg + 24, 0)
            cpu.regs[EAX] = 1  # message available (WM_QUIT)
            cleanup_stdcall(cpu, memory, 20)
            return
        msg = wm.peek_message()
        if msg is not None and lp_msg:
            hwnd_msg, msg_id, wparam, lparam = msg
            memory.write32(lp_msg,      hwnd_msg)
            memory.write32(lp_msg + 4,  msg_id)
            memory.write32(lp_msg + 8,  wparam)
            memory.write32(lp_msg + 12, lparam)
            memory.write32(lp_msg + 16, 0)  # time
            memory.write32(lp_msg + 20, 0)  # pt.x
            memory.write32(lp_msg + 24, 0)  # pt.y
            _dispatch_winhooks(cpu, msg_id, wparam, lp_msg)
            cpu.regs[EAX] = 1  # TRUE
        else:
            cpu.regs[EAX] = 0  # FALSE = no message
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("user32.dll", "PeekMessageA", _PeekMessageA)

    # GetMessageA(LPMSG lpMsg, HWND hWnd, UINT wMsgFilterMin, UINT wMsgFilterMax) -> BOOL
    # Blocking: pumps SDL events until a message is available or SDL_QUIT fires.
    # When running as a cooperative background thread (state.is_running_thread),
    # does one pump-and-check then yields back to the scheduler so the main
    # thread can make progress. Re-executes from INT 0xFE on next slice.
    def _GetMessageA(cpu: "CPU") -> None:
        import time as _time
        lp_msg = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)

        def _write_quit() -> None:
            if lp_msg:
                memory.write32(lp_msg,      0)
                memory.write32(lp_msg + 4,  0x0012)  # WM_QUIT
                memory.write32(lp_msg + 8,  0)
                memory.write32(lp_msg + 12, 0)
                memory.write32(lp_msg + 16, 0)
                memory.write32(lp_msg + 20, 0)
                memory.write32(lp_msg + 24, 0)

        def _write_msg(hwnd_msg: int, msg_id: int, wparam: int, lparam: int) -> None:
            if lp_msg:
                memory.write32(lp_msg,      hwnd_msg)
                memory.write32(lp_msg + 4,  msg_id)
                memory.write32(lp_msg + 8,  wparam)
                memory.write32(lp_msg + 12, lparam)
                memory.write32(lp_msg + 16, 0)
                memory.write32(lp_msg + 20, 0)
                memory.write32(lp_msg + 24, 0)

        if state.scheduler.current_idx != 0:
            # Cooperative path: one pump-and-check, then yield.
            if not wm.pump_sdl_events():
                _write_quit()
                cpu.regs[EAX] = 0
                cleanup_stdcall(cpu, memory, 16)
                return
            msg = wm.peek_message()
            if msg is not None:
                hwnd_msg, msg_id, wparam, lparam = msg
                _write_msg(hwnd_msg, msg_id, wparam, lparam)
                _dispatch_winhooks(cpu, msg_id, wparam, lp_msg)
                cpu.regs[EAX] = 1
                cleanup_stdcall(cpu, memory, 16)
                return
            # No message yet — rewind to INT 0xFE and yield to scheduler.
            # The thread will re-enter GetMessageA on its next slice.
            cpu.eip = (cpu.eip - 2) & 0xFFFFFFFF
            cpu.halted = True
            state.thread_yield_requested = True
            return

        # Main-thread blocking path: sleep until a message or SDL_QUIT.
        caller_eip = memory.read32(cpu.regs[ESP] & 0xFFFFFFFF)
        idle_iters = 0
        while True:
            if not wm.pump_sdl_events():
                _write_quit()
                cpu.regs[EAX] = 0
                cleanup_stdcall(cpu, memory, 16)
                return
            msg = wm.peek_message()
            if msg is not None:
                hwnd_msg, msg_id, wparam, lparam = msg
                _write_msg(hwnd_msg, msg_id, wparam, lparam)
                _dispatch_winhooks(cpu, msg_id, wparam, lp_msg)
                cpu.regs[EAX] = 1
                cleanup_stdcall(cpu, memory, 16)
                return
            idle_iters += 1
            if idle_iters % 500 == 0:
                logger.debug("handlers", f"[GetMessageA] idle {idle_iters} iters, caller EIP=0x{caller_eip:08x}, ESP=0x{cpu.regs[ESP]:08x}")
            _time.sleep(0.001)

    stubs.register_handler("user32.dll", "GetMessageA", _GetMessageA)

    # TranslateMessage(MSG*) -> BOOL
    def _TranslateMessage(cpu: "CPU") -> None:
        # For our purposes (no IME, no dead keys), TranslateMessage is a no-op.
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "TranslateMessage", _TranslateMessage)

    # DispatchMessageA(const MSG* lpMsg) -> LRESULT
    # Routes the message to the registered window proc or dialog proc.
    def _DispatchMessageA(cpu: "CPU") -> None:
        lp_msg = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)

        hwnd   = memory.read32(lp_msg        & 0xFFFFFFFF)
        msg_id = memory.read32((lp_msg + 4)  & 0xFFFFFFFF)
        wparam = memory.read32((lp_msg + 8)  & 0xFFFFFFFF)
        lparam = memory.read32((lp_msg + 12) & 0xFFFFFFFF)

        logger.debug("handlers",
            f"[Win32] DispatchMessageA hwnd=0x{hwnd:x} msg=0x{msg_id:04x} "
            f"wp=0x{wparam:x} lp=0x{lparam:x}"
        )

        # Stack must be cleaned before _invoke_emulated_proc modifies it
        cleanup_stdcall(cpu, memory, 4)

        result = 0
        entry = wm.get_window(hwnd)
        if entry is not None:
            proc = entry.dlg_proc_addr or entry.wnd_proc_addr
            if proc:
                sentinel = _get_dialog_sentinel(state, memory)
                result = _invoke_emulated_proc(
                    cpu, memory, proc,
                    [hwnd, msg_id, wparam, lparam],
                    sentinel,
                )
                # Re-render after each dispatch so the window reflects any state changes
                if entry.sdl_renderer is not None:
                    from tew.api.dialog_renderer import render_dialog
                    render_dialog(wm, hwnd)
            else:
                logger.debug("handlers",
                    f"[Win32] DispatchMessageA: hwnd=0x{hwnd:x} msg=0x{msg_id:04x} — no proc, skipping"
                )
        else:
            logger.debug("handlers",
                f"[Win32] DispatchMessageA: unknown hwnd=0x{hwnd:x}, ignoring"
            )

        cpu.regs[EAX] = result & 0xFFFFFFFF

    stubs.register_handler("user32.dll", "DispatchMessageA", _DispatchMessageA)

    # PostQuitMessage(int nExitCode) -> void
    def _PostQuitMessage(cpu: "CPU") -> None:
        logger.debug("handlers", "[Win32] PostQuitMessage()")
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "PostQuitMessage", _PostQuitMessage)

    # GetLastActivePopup(HWND hWnd) -> HWND
    def _GetLastActivePopup(cpu: "CPU") -> None:
        h_wnd = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = h_wnd  # no popups — return the input handle
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "GetLastActivePopup", _GetLastActivePopup)

    # DialogBoxParamA(hInstance, lpTemplateName, hWndParent, lpDialogFunc, dwInitParam) -> INT_PTR
    def _DialogBoxParamA(cpu: "CPU") -> None:
        lp_template    = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        h_wnd_parent   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_dialog_func = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        dw_init_param  = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)

        # lp_template is either an ordinal (≤ 0xFFFF) or a pointer to a name string
        template_id = lp_template & 0xFFFF if lp_template <= 0xFFFF else 0
        template_name = f"#{template_id}" if template_id else "?"

        if state.pe_resources is None:
            logger.error("dialog", "[Win32] DialogBoxParamA: pe_resources not available — halting")
            cpu.halted = True
            return

        template = state.pe_resources.find_dialog(template_id)
        if template is None:
            logger.error("dialog",
                f"[UNIMPLEMENTED] DialogBoxParamA({template_name}): "
                f"dialog template not found — halting")
            cpu.halted = True
            return

        logger.debug("dialog",
            f"[Win32] DialogBoxParamA({template_name}) proc=0x{lp_dialog_func:08x}")

        # Initialize SDL2 if it hasn't been already
        if not wm.initialize():
            logger.error("dialog", "[Win32] DialogBoxParamA: SDL2 init failed — halting")
            cpu.halted = True
            return

        # Create the dialog and all its child controls
        dlg_hwnd = wm.create_dialog(template, h_wnd_parent, lp_dialog_func, dw_init_param,
                                    creator_tid=state.tls_current_thread_id())
        if dlg_hwnd == 0:
            logger.error("dialog", "[Win32] DialogBoxParamA: create_dialog failed — halting")
            cpu.halted = True
            return

        # Clean up the stdcall arguments.  After this, EAX will be set at the
        # end of the modal loop and the stub's RET will return to the call site.
        cleanup_stdcall(cpu, memory, 20)

        sentinel = _get_dialog_sentinel(state, memory)

        # Import renderer here to avoid circular imports at module level
        from tew.api.dialog_renderer import render_dialog

        # ── Modal loop ────────────────────────────────────────────────────────
        # Deliver WM_INITDIALOG first (already in the queue from create_dialog),
        # then pump SDL events and dispatch WM_COMMAND messages as they arrive.
        import time as _time
        while True:
            dlg_entry = wm.get_window(dlg_hwnd)
            if dlg_entry is None or dlg_entry.dlg_done:
                break

            # Pump SDL events → updates window manager's message queue
            if not wm.pump_sdl_events():
                # SDL_QUIT
                wm.end_dialog(dlg_hwnd, -1)
                break

            # Render the current dialog state
            render_dialog(wm, dlg_hwnd)

            # Dispatch one pending message (if any)
            msg = wm.peek_message()
            if msg is None:
                _time.sleep(0.016)  # ~60 fps; yield to system
                continue

            hwnd_msg, msg_id, wparam, lparam = msg

            # Only deliver messages addressed to this dialog
            if hwnd_msg != dlg_hwnd:
                continue

            logger.debug("dialog",
                f"[DialogLoop] msg=0x{msg_id:04x} wparam=0x{wparam:04x} "
                f"lparam=0x{lparam:08x} → proc 0x{lp_dialog_func:08x}")

            # Invoke the dialog proc: DLGPROC(HWND hDlg, UINT uMsg, WPARAM, LPARAM)
            _invoke_emulated_proc(
                cpu, memory, lp_dialog_func,
                [dlg_hwnd, msg_id, wparam, lparam],
                sentinel,
            )

            # Check again after proc may have called EndDialog
            dlg_entry = wm.get_window(dlg_hwnd)
            if dlg_entry is not None and dlg_entry.dlg_done:
                break

        # Retrieve result before destroying
        dlg_entry = wm.get_window(dlg_hwnd)
        result = dlg_entry.dlg_result if dlg_entry is not None else -1

        wm.destroy_window(dlg_hwnd)

        logger.debug("dialog",
            f"[Win32] DialogBoxParamA({template_name}) -> {result}")
        cpu.regs[EAX] = result & 0xFFFFFFFF

    stubs.register_handler("user32.dll", "DialogBoxParamA", _DialogBoxParamA)

    # DialogBoxParamW(hInstance, lpTemplateName, hWndParent, lpDialogFunc, dwInitParam) -> INT_PTR
    stubs.register_handler("user32.dll", "DialogBoxParamW", _halt("DialogBoxParamW"))

    # DialogBoxIndirectParamA(...) -> INT_PTR
    stubs.register_handler("user32.dll", "DialogBoxIndirectParamA", _halt("DialogBoxIndirectParamA"))

    # CreateDialogParamA(hInstance, lpTemplateName, hWndParent, lpDialogFunc, dwInitParam) -> HWND
    # Modeless dialog: creates the window, fires WM_INITDIALOG, returns HWND immediately.
    # The caller owns the message loop; DispatchMessageA routes subsequent messages.
    def _CreateDialogParamA(cpu: "CPU") -> None:
        lp_template    = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        h_wnd_parent   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_dialog_func = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        dw_init_param  = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)

        template_id = lp_template & 0xFFFF if lp_template <= 0xFFFF else 0
        template_name = f"#{template_id}" if template_id else "?"

        if state.pe_resources is None:
            logger.error("dialog", "[Win32] CreateDialogParamA: pe_resources not available — halting")
            cpu.halted = True
            return

        template = state.pe_resources.find_dialog(template_id)
        if template is None:
            logger.error("dialog",
                f"[UNIMPLEMENTED] CreateDialogParamA({template_name}): "
                f"dialog template not found — halting")
            cpu.halted = True
            return

        logger.debug("dialog",
            f"[Win32] CreateDialogParamA({template_name}) proc=0x{lp_dialog_func:08x}")

        if not wm.initialize():
            logger.error("dialog", "[Win32] CreateDialogParamA: SDL2 init failed — halting")
            cpu.halted = True
            return

        dlg_hwnd = wm.create_dialog(template, h_wnd_parent, lp_dialog_func, dw_init_param,
                                    creator_tid=state.tls_current_thread_id())
        if dlg_hwnd == 0:
            logger.error("dialog", "[Win32] CreateDialogParamA: create_dialog failed — halting")
            cpu.halted = True
            return

        # Clean up stdcall args before any _invoke_emulated_proc call
        cleanup_stdcall(cpu, memory, 20)

        sentinel = _get_dialog_sentinel(state, memory)

        # Deliver WM_INITDIALOG now (create_dialog posted it to the queue).
        # Drain the queue until we find it: re-save messages for live HWNDs,
        # silently drop anything else (e.g. messages posted before this dialog
        # was created that arrived out of order).
        requeue: list[tuple[int, int, int, int]] = []
        while True:
            pending = wm.peek_message()
            if pending is None:
                break
            hwnd_msg, msg_id, wparam, lparam = pending
            if hwnd_msg == dlg_hwnd and msg_id == WM_INITDIALOG:
                for saved in requeue:
                    wm.post_message(*saved)
                if lp_dialog_func != 0:
                    _invoke_emulated_proc(
                        cpu, memory, lp_dialog_func,
                        [dlg_hwnd, msg_id, wparam, lparam],
                        sentinel,
                    )
                break
            elif wm.is_window(hwnd_msg):
                requeue.append(pending)
            # else: message for a non-existent HWND — discard

        # Render the initial state so something is visible right away
        from tew.api.dialog_renderer import render_dialog
        render_dialog(wm, dlg_hwnd)

        cpu.regs[EAX] = dlg_hwnd

    stubs.register_handler("user32.dll", "CreateDialogParamA", _CreateDialogParamA)

    # EndDialog(HWND hDlg, INT_PTR nResult) -> BOOL
    def _EndDialog(cpu: "CPU") -> None:
        h_dlg    = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        n_result = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        ok = wm.end_dialog(h_dlg, n_result)
        if not ok:
            logger.warn("handlers", f"[USER32] EndDialog(0x{h_dlg:x}): unknown dialog")
        cpu.regs[EAX] = 1 if ok else 0
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "EndDialog", _EndDialog)

    # RegisterClassA(WNDCLASSA*) -> ATOM
    def _RegisterClassA(cpu: "CPU") -> None:
        lp_wndclass = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        # WNDCLASSA layout: cbSize(optional for ExA), style, lpfnWndProc, cbClsExtra,
        #   cbWndExtra, hInstance, hIcon, hCursor, hbrBackground, lpszMenuName, lpszClassName
        # For RegisterClassA (not Ex): no cbSize field.
        # Offsets: [0]=style, [4]=lpfnWndProc, [8]=cbClsExtra, [12]=cbWndExtra,
        #          [16]=hInstance, [20]=hIcon, [24]=hCursor, [28]=hbrBackground,
        #          [32]=lpszMenuName, [36]=lpszClassName
        lp_fn_wnd_proc   = memory.read32((lp_wndclass + 4)  & 0xFFFFFFFF)
        h_br_background  = memory.read32((lp_wndclass + 28) & 0xFFFFFFFF)
        lp_sz_class_name = memory.read32((lp_wndclass + 36) & 0xFFFFFFFF)
        name = ""
        for i in range(64):
            ch = memory.read8((lp_sz_class_name + i) & 0xFFFFFFFF)
            if ch == 0:
                break
            name += chr(ch)
        atom = wm.register_class(name, lp_fn_wnd_proc, background=h_br_background)
        cpu.regs[EAX] = atom
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "RegisterClassA", _RegisterClassA)

    # RegisterClassExA(WNDCLASSEXA*) -> ATOM
    def _RegisterClassExA(cpu: "CPU") -> None:
        lp_wndclass = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        # WNDCLASSEXA: [0]=cbSize, [4]=style, [8]=lpfnWndProc, [12]=cbClsExtra,
        #   [16]=cbWndExtra, [20]=hInstance, [24]=hIcon, [28]=hCursor,
        #   [32]=hbrBackground, [36]=lpszMenuName, [40]=lpszClassName, [44]=hIconSm
        lp_fn_wnd_proc   = memory.read32((lp_wndclass + 8)  & 0xFFFFFFFF)
        h_br_background  = memory.read32((lp_wndclass + 32) & 0xFFFFFFFF)
        lp_sz_class_name = memory.read32((lp_wndclass + 40) & 0xFFFFFFFF)
        name = ""
        for i in range(64):
            ch = memory.read8((lp_sz_class_name + i) & 0xFFFFFFFF)
            if ch == 0:
                break
            name += chr(ch)
        atom = wm.register_class(name, lp_fn_wnd_proc, background=h_br_background)
        cpu.regs[EAX] = atom
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "RegisterClassExA", _RegisterClassExA)

    # RegisterClassW(WNDCLASSW*) -> ATOM
    def _RegisterClassW(cpu: "CPU") -> None:
        lp_wndclass = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_fn_wnd_proc   = memory.read32((lp_wndclass + 4)  & 0xFFFFFFFF)
        h_br_background  = memory.read32((lp_wndclass + 28) & 0xFFFFFFFF)
        lp_sz_class_name = memory.read32((lp_wndclass + 36) & 0xFFFFFFFF)
        from tew.api._state import read_wide_string
        name = read_wide_string(lp_sz_class_name, memory, 64)
        atom = wm.register_class(name, lp_fn_wnd_proc, background=h_br_background)
        cpu.regs[EAX] = atom
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "RegisterClassW", _RegisterClassW)

    # RegisterClassExW(WNDCLASSEXW*) -> ATOM
    def _RegisterClassExW(cpu: "CPU") -> None:
        lp_wndclass = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_fn_wnd_proc   = memory.read32((lp_wndclass + 8)  & 0xFFFFFFFF)
        h_br_background  = memory.read32((lp_wndclass + 32) & 0xFFFFFFFF)
        lp_sz_class_name = memory.read32((lp_wndclass + 40) & 0xFFFFFFFF)
        from tew.api._state import read_wide_string
        name = read_wide_string(lp_sz_class_name, memory, 64)
        atom = wm.register_class(name, lp_fn_wnd_proc, background=h_br_background)
        cpu.regs[EAX] = atom
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "RegisterClassExW", _RegisterClassExW)

    # UnregisterClassA(LPCSTR lpClassName, HINSTANCE hInstance) -> BOOL
    def _UnregisterClassA(cpu: "CPU") -> None:
        from tew.api._state import read_cstring
        lp_class = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        name = read_cstring(lp_class, memory) if lp_class > 0xFFFF else f"#{lp_class}"
        atom = wm.get_class_atom(name)
        if atom:
            wm._classes.pop(atom, None)
            wm._class_by_name.pop(name.lower(), None)
            logger.debug("handlers", f"[Win32] UnregisterClassA('{name}') -> TRUE")
        else:
            logger.debug("handlers", f"[Win32] UnregisterClassA('{name}') -> FALSE (not found)")
        cpu.regs[EAX] = 1 if atom else 0
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "UnregisterClassA", _UnregisterClassA)

    # CreateWindowExA(dwExStyle, lpClassName, lpWindowName, dwStyle, X, Y,
    #                 nWidth, nHeight, hWndParent, hMenu, hInstance, lpParam) -> HWND
    def _CreateWindowExA(cpu: "CPU") -> None:
        dw_ex_style    = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        lp_class_name  = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        lp_window_name = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        dw_style       = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        x              = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        y              = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        n_width        = memory.read32((cpu.regs[ESP] + 28) & 0xFFFFFFFF)
        n_height       = memory.read32((cpu.regs[ESP] + 32) & 0xFFFFFFFF)
        h_wnd_parent   = memory.read32((cpu.regs[ESP] + 36) & 0xFFFFFFFF)
        h_menu         = memory.read32((cpu.regs[ESP] + 40) & 0xFFFFFFFF)

        # lp_class_name ≤ 0xFFFF means the value IS an atom (MAKEINTATOM pattern).
        # Look it up directly in the atom→class dict; don't stringify and do a
        # name lookup, because "#N" was never inserted into _class_by_name.
        if lp_class_name <= 0xFFFF:
            class_name = f"#{lp_class_name}"
            atom = lp_class_name if lp_class_name in wm._classes else 0
        else:
            class_name = ""
            for i in range(64):
                c = memory.read8((lp_class_name + i) & 0xFFFFFFFF)
                if not c:
                    break
                class_name += chr(c)
            atom = wm.get_class_atom(class_name)

        window_name = ""
        if lp_window_name:
            for i in range(256):
                c = memory.read8((lp_window_name + i) & 0xFFFFFFFF)
                if not c:
                    break
                window_name += chr(c)

        wnd_proc = wm._classes[atom].wnd_proc_addr if atom in wm._classes else 0

        # x/y/cx/cy may be CW_USEDEFAULT (0x80000000) — treat as 0
        px_x = 0 if x >= 0x80000000 else x
        px_y = 0 if y >= 0x80000000 else y
        px_cx = 640 if n_width >= 0x80000000 else n_width
        px_cy = 480 if n_height >= 0x80000000 else n_height

        if not wm.initialize():
            logger.error("window", "[CreateWindowExA] SDL2 init failed — halting")
            cpu.halted = True
            return

        hwnd = wm.create_window(
            class_name, window_name, dw_style, dw_ex_style,
            px_x, px_y, px_cx, px_cy,
            h_wnd_parent, wnd_proc,
            creator_tid=state.tls_current_thread_id(),
        )
        if hwnd == 0:
            logger.error("window",
                f"[CreateWindowExA] create_window failed for class '{class_name}' — halting")
            cpu.halted = True
            return

        # Register as child with ctrl_id = low word of hMenu if this is a child window
        if h_wnd_parent and (dw_style & 0x40000000):  # WS_CHILD
            wm.register_child(h_wnd_parent, h_menu & 0xFFFF, hwnd)

        logger.debug("window",
            f"[CreateWindowExA] class='{class_name}' title='{window_name}' "
            f"hwnd=0x{hwnd:x}")
        cpu.regs[EAX] = hwnd
        cleanup_stdcall(cpu, memory, 48)

    stubs.register_handler("user32.dll", "CreateWindowExA", _CreateWindowExA)

    # CreateWindowExW — same logic, wide strings
    stubs.register_handler("user32.dll", "CreateWindowExW",  _halt("CreateWindowExW"))
    stubs.register_handler("user32.dll", "FindWindowA",      _halt("FindWindowA"))
    stubs.register_handler("user32.dll", "FindWindowW",      _halt("FindWindowW"))
    stubs.register_handler("user32.dll", "FindWindowExA",    _halt("FindWindowExA"))
    stubs.register_handler("user32.dll", "FindWindowExW",    _halt("FindWindowExW"))

    # DestroyWindow(HWND hWnd) -> BOOL
    def _DestroyWindow(cpu: "CPU") -> None:
        h_wnd = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        ok = wm.destroy_window(h_wnd)
        if not ok:
            logger.warn("handlers", f"[USER32] DestroyWindow(0x{h_wnd:x}): unknown HWND")
        cpu.regs[EAX] = 1 if ok else 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "DestroyWindow", _DestroyWindow)

    # ShowWindow(HWND hWnd, int nCmdShow) -> BOOL
    def _ShowWindow(cpu: "CPU") -> None:
        from sdl2 import SDL_ShowWindow, SDL_HideWindow
        h_wnd     = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        n_cmd_show = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        entry = wm.get_window(h_wnd)
        if entry is not None and entry.sdl_window is not None:
            if n_cmd_show == 0:  # SW_HIDE
                SDL_HideWindow(entry.sdl_window)
            else:
                SDL_ShowWindow(entry.sdl_window)
        cpu.regs[EAX] = 0  # was previously hidden
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "ShowWindow", _ShowWindow)

    # ── GDI object table ─────────────────────────────────────────────────────
    # Maps HGDIOBJ → _GdiObj.  Stock objects are pre-populated here with
    # stable handles (0x2001 + fnObject index) so GetStockObject returns a
    # traceable handle backed by a real record.  Dynamic objects (brushes,
    # fonts, etc.) use handles from _next_hgdi and are removed by DeleteObject.
    _gdi_objects:   dict[int, _GdiObj] = {}
    _next_hgdi:     list[int]          = [0x3001]
    _stock_handles: dict[int, int]     = {}   # fnObject → HGDIOBJ

    for _fn, _kd, _cl, _st in [
        (0,  "brush",   0x00FFFFFF, 0),   # WHITE_BRUSH
        (1,  "brush",   0x00C0C0C0, 0),   # LTGRAY_BRUSH
        (2,  "brush",   0x00808080, 0),   # GRAY_BRUSH
        (3,  "brush",   0x00404040, 0),   # DKGRAY_BRUSH
        (4,  "brush",   0x00000000, 0),   # BLACK_BRUSH
        (5,  "brush",   0x00000000, 1),   # NULL_BRUSH   (BS_NULL)
        (6,  "pen",     0x00FFFFFF, 0),   # WHITE_PEN
        (7,  "pen",     0x00000000, 0),   # BLACK_PEN
        (8,  "pen",     0x00000000, 5),   # NULL_PEN     (PS_NULL)
        (10, "font",    0,           0),  # OEM_FIXED_FONT
        (11, "font",    0,           0),  # ANSI_FIXED_FONT
        (12, "font",    0,           0),  # ANSI_VAR_FONT
        (13, "font",    0,           0),  # SYSTEM_FONT
        (14, "font",    0,           0),  # DEVICE_DEFAULT_FONT
        (15, "palette", 0,           0),  # DEFAULT_PALETTE
        (16, "font",    0,           0),  # SYSTEM_FIXED_FONT
        (17, "font",    0,           0),  # DEFAULT_GUI_FONT
        (18, "brush",   0x00000000, 0),  # DC_BRUSH
        (19, "pen",     0x00000000, 0),  # DC_PEN
    ]:
        _h = 0x2001 + _fn
        _gdi_objects[_h] = _GdiObj(_kd, _cl, _st, is_stock=True)
        _stock_handles[_fn] = _h

    # Per-DC selected GDI objects: hdc → {kind: HGDIOBJ}.
    # Win32 defaults: white brush, black pen, system font.
    _dc_selected: dict[int, dict[str, int]] = {}

    # ── Device context (DC) handle pool ──────────────────────────────────────
    _next_hdc:   list[int]       = [0xDC01]
    _live_hdcs:  dict[int, int]  = {}   # hdc → hwnd

    def _alloc_hdc(hwnd: int) -> int:
        hdc = _next_hdc[0]
        _next_hdc[0] += 1
        _live_hdcs[hdc] = hwnd
        _dc_selected[hdc] = {
            "brush":   _stock_handles[0],   # WHITE_BRUSH
            "pen":     _stock_handles[7],   # BLACK_PEN
            "font":    _stock_handles[13],  # SYSTEM_FONT
        }
        return hdc

    # GetDC(HWND hWnd) -> HDC  — client-area device context
    def _GetDC(cpu: "CPU") -> None:
        hwnd = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        hdc  = _alloc_hdc(hwnd)
        logger.debug("handlers", f"[Win32] GetDC(hwnd=0x{hwnd:x}) -> 0x{hdc:x}")
        cpu.regs[EAX] = hdc
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "GetDC", _GetDC)

    # GetWindowDC(HWND hWnd) -> HDC  — whole-window device context
    # Used by Platform_SysStartUp (0x6b13b0) to probe HORZRES/VERTRES/BITSPIXEL
    # via GetDeviceCaps and then immediately ReleaseDC — never drawn through.
    def _GetWindowDC(cpu: "CPU") -> None:
        hwnd = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        hdc  = _alloc_hdc(hwnd)
        logger.debug("handlers", f"[Win32] GetWindowDC(hwnd=0x{hwnd:x}) -> 0x{hdc:x}")
        cpu.regs[EAX] = hdc
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "GetWindowDC", _GetWindowDC)

    # ReleaseDC(HWND hWnd, HDC hDC) -> int  — 1 = released
    def _ReleaseDC(cpu: "CPU") -> None:
        hwnd = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        hdc  = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        _live_hdcs.pop(hdc, None)
        _dc_selected.pop(hdc, None)
        logger.debug("handlers", f"[Win32] ReleaseDC(hwnd=0x{hwnd:x}, hdc=0x{hdc:x})")
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "ReleaseDC", _ReleaseDC)

    # SendMessageA(HWND, UINT, WPARAM, LPARAM) -> LRESULT
    def _SendMessageA(cpu: "CPU") -> None:
        h_wnd  = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        msg    = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        wparam = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lparam = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)

        result = 0
        if msg == BM_GETCHECK:
            result = wm.get_check_state(h_wnd)
        elif msg == BM_SETCHECK:
            wm.set_check_state(h_wnd, wparam)
        elif msg == 0x000D:  # WM_GETTEXT / EM_GETTEXT
            n_max  = wparam
            lp_buf = lparam
            text = wm.get_window_text(h_wnd)
            length = min(len(text), n_max - 1) if n_max > 0 else 0
            if lp_buf and n_max > 0:
                for i in range(length):
                    memory.write8((lp_buf + i) & 0xFFFFFFFF, ord(text[i]))
                memory.write8((lp_buf + length) & 0xFFFFFFFF, 0)
            result = length
        elif msg == 0x000C:  # WM_SETTEXT / EM_SETTEXT
            if lparam:
                text = ""
                for i in range(256):
                    ch = memory.read8((lparam + i) & 0xFFFFFFFF)
                    if ch == 0:
                        break
                    text += chr(ch)
                wm.set_window_text(h_wnd, text)
            result = 1
        else:
            logger.debug("handlers",
                f"[Win32] SendMessageA(hwnd=0x{h_wnd:x}, msg=0x{msg:04x}, "
                f"wp=0x{wparam:x}, lp=0x{lparam:x}) -> 0 (unhandled)")

        cpu.regs[EAX] = result
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "SendMessageA", _SendMessageA)

    # SendMessageW(HWND, UINT, WPARAM, LPARAM) -> LRESULT
    stubs.register_handler("user32.dll", "SendMessageW", _halt("SendMessageW"))

    # PostMessageA(HWND, UINT, WPARAM, LPARAM) -> BOOL
    def _PostMessageA(cpu: "CPU") -> None:
        hwnd   = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        msg    = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        wparam = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lparam = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        ok = wm.post_message(hwnd, msg, wparam, lparam)
        if not ok:
            logger.warn("handlers",
                f"[Win32] PostMessageA FAILED: hwnd=0x{hwnd:x} msg=0x{msg:04x} "
                f"wp=0x{wparam:x} lp=0x{lparam:x} (unknown hwnd)"
            )
        else:
            logger.debug("handlers",
                f"[Win32] PostMessageA(hwnd=0x{hwnd:x}, msg=0x{msg:04x}, "
                f"wp=0x{wparam:x}, lp=0x{lparam:x}) -> TRUE"
            )
        cpu.regs[EAX] = 1 if ok else 0
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "PostMessageA", _PostMessageA)

    # PostMessageW(HWND, UINT, WPARAM, LPARAM) -> BOOL
    def _PostMessageW(cpu: "CPU") -> None:
        hwnd   = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        msg    = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        wparam = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lparam = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        ok = wm.post_message(hwnd, msg, wparam, lparam)
        logger.debug("handlers",
            f"[Win32] PostMessageW(hwnd=0x{hwnd:x}, msg=0x{msg:04x}) -> {'TRUE' if ok else 'FALSE (unknown hwnd)'}"
        )
        cpu.regs[EAX] = 1 if ok else 0
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "PostMessageW", _PostMessageW)

    # GetDlgItem(HWND hDlg, int nIDDlgItem) -> HWND
    def _GetDlgItem(cpu: "CPU") -> None:
        h_dlg       = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        n_id_dlg    = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        child_hwnd  = wm.get_dlg_item(h_dlg, n_id_dlg)
        cpu.regs[EAX] = child_hwnd
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "GetDlgItem", _GetDlgItem)

    # SetDlgItemTextA(HWND hDlg, int nIDDlgItem, LPCSTR lpString) -> BOOL
    def _SetDlgItemTextA(cpu: "CPU") -> None:
        h_dlg    = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        n_id     = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        lp_str   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        child_hwnd = wm.get_dlg_item(h_dlg, n_id)
        if child_hwnd and lp_str:
            text = ""
            for i in range(256):
                ch = memory.read8((lp_str + i) & 0xFFFFFFFF)
                if ch == 0:
                    break
                text += chr(ch)
            wm.set_window_text(child_hwnd, text)
        cpu.regs[EAX] = 1 if child_hwnd else 0
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("user32.dll", "SetDlgItemTextA", _SetDlgItemTextA)

    # GetDlgItemTextA(HWND hDlg, int nIDDlgItem, LPSTR lpString, int nMaxCount) -> UINT
    def _GetDlgItemTextA(cpu: "CPU") -> None:
        h_dlg       = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        n_id        = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        lp_string   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        n_max_count = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        child_hwnd = wm.get_dlg_item(h_dlg, n_id)
        text = wm.get_window_text(child_hwnd) if child_hwnd else ""
        if not text:
            text = "admin"  # fallback for unknown controls
        length = min(len(text), n_max_count - 1) if n_max_count > 0 else 0
        if lp_string and n_max_count > 0:
            for i in range(length):
                memory.write8((lp_string + i) & 0xFFFFFFFF, ord(text[i]))
            memory.write8((lp_string + length) & 0xFFFFFFFF, 0)
        logger.debug("handlers",
            f"[Win32] GetDlgItemTextA(dlg=0x{h_dlg:x}, ctrl=0x{n_id:x}) -> {repr(text)}")
        cpu.regs[EAX] = length
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "GetDlgItemTextA", _GetDlgItemTextA)

    # SendDlgItemMessageA(HWND, int, UINT, WPARAM, LPARAM) -> LRESULT
    def _SendDlgItemMessageA(cpu: "CPU") -> None:
        h_dlg  = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        n_id   = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        msg    = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        wparam = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        child_hwnd = wm.get_dlg_item(h_dlg, n_id)
        result = 0
        if child_hwnd:
            if msg == BM_GETCHECK:
                result = wm.get_check_state(child_hwnd)
            elif msg == BM_SETCHECK:
                wm.set_check_state(child_hwnd, wparam)
        cpu.regs[EAX] = result
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("user32.dll", "SendDlgItemMessageA", _SendDlgItemMessageA)

    # EnableWindow(HWND hWnd, BOOL bEnable) -> BOOL
    def _EnableWindow(cpu: "CPU") -> None:
        # Return 0 (was not previously disabled); we don't track enabled state yet
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "EnableWindow", _EnableWindow)

    # IsWindow(HWND hWnd) -> BOOL
    def _IsWindow(cpu: "CPU") -> None:
        hwnd = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = 1 if wm.is_window(hwnd) else 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "IsWindow", _IsWindow)

    # SetFocus(HWND hWnd) -> HWND (previous focus)
    def _SetFocus(cpu: "CPU") -> None:
        h_wnd = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        prev = wm._focused_hwnd
        if wm.is_window(h_wnd):
            wm._focused_hwnd = h_wnd
        cpu.regs[EAX] = prev
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("user32.dll", "SetFocus", _SetFocus)

    # GetFocus() -> HWND
    def _GetFocus(cpu: "CPU") -> None:
        cpu.regs[EAX] = wm._focused_hwnd

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
        cpu.regs[EAX] = 0  # FALSE
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "IsDialogMessageA", _IsDialogMessageA)

    # CallWindowProcA(WNDPROC, HWND, UINT, WPARAM, LPARAM) -> LRESULT
    stubs.register_handler("user32.dll", "CallWindowProcA", _halt("CallWindowProcA"))

    # CheckDlgButton(HWND hDlg, int nIDButton, UINT uCheck) -> BOOL
    def _CheckDlgButton(cpu: "CPU") -> None:
        dlg_hwnd  = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        id_button = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        u_check   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        btn_hwnd  = wm.get_dlg_item(dlg_hwnd, id_button)
        if btn_hwnd:
            wm.set_check_state(btn_hwnd, u_check)
            logger.debug("handlers",
                f"[Win32] CheckDlgButton(dlg=0x{dlg_hwnd:x}, id=0x{id_button:x}, check={u_check})"
            )
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("user32.dll", "CheckDlgButton", _CheckDlgButton)

    # IsDlgButtonChecked(HWND, int) -> UINT
    def _IsDlgButtonChecked(cpu: "CPU") -> None:
        h_dlg = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        n_id  = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        child_hwnd = wm.get_dlg_item(h_dlg, n_id)
        cpu.regs[EAX] = wm.get_check_state(child_hwnd) if child_hwnd else 0
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("user32.dll", "IsDlgButtonChecked", _IsDlgButtonChecked)

    # SetDlgItemInt(HWND hDlg, int nIDDlgItem, UINT uValue, BOOL bSigned) -> BOOL
    def _SetDlgItemInt(cpu: "CPU") -> None:
        dlg_hwnd = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        id_item  = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        u_value  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        b_signed = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        text = str(u_value if not b_signed else (u_value if u_value < 0x80000000 else u_value - 0x100000000))
        item_hwnd = wm.get_dlg_item(dlg_hwnd, id_item)
        if item_hwnd:
            wm.set_window_text(item_hwnd, text)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "SetDlgItemInt", _SetDlgItemInt)

    # GetDlgItemInt(HWND, int, BOOL*, BOOL) -> UINT
    stubs.register_handler("user32.dll", "GetDlgItemInt", _halt("GetDlgItemInt"))

    # RedrawWindow(HWND, RECT*, HRGN, UINT) -> BOOL
    # SDL renders continuously; no explicit repaint needed.
    def _RedrawWindow(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("user32.dll", "RedrawWindow", _RedrawWindow)

    # ── gdi32.dll ─────────────────────────────────────────────────────────────

    # DeleteDC(HDC hDC) -> BOOL
    def _DeleteDC(cpu: "CPU") -> None:
        hdc = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        _live_hdcs.pop(hdc, None)
        _dc_selected.pop(hdc, None)
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("gdi32.dll", "DeleteDC", _DeleteDC)

    # CreateCompatibleDC(HDC hDC) -> HDC
    stubs.register_handler("gdi32.dll", "CreateCompatibleDC", _halt("CreateCompatibleDC"))

    # CreateCompatibleBitmap(HDC hDC, int cx, int cy) -> HBITMAP
    stubs.register_handler("gdi32.dll", "CreateCompatibleBitmap", _halt("CreateCompatibleBitmap"))

    # SelectObject(HDC hDC, HGDIOBJ h) -> HGDIOBJ (previously selected obj of same type)
    def _SelectObject(cpu: "CPU") -> None:
        hdc  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        hgdi = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        obj = _gdi_objects.get(hgdi)
        if obj is None:
            logger.warn("handlers", f"[GDI] SelectObject: unknown HGDIOBJ 0x{hgdi:x}")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 8)
            return
        sel  = _dc_selected.get(hdc, {})
        prev = sel.get(obj.kind, 0)
        if hdc in _dc_selected:
            _dc_selected[hdc][obj.kind] = hgdi
        logger.debug("handlers",
            f"[GDI] SelectObject(hdc=0x{hdc:x}, 0x{hgdi:x} {obj.kind}) -> prev=0x{prev:x}")
        cpu.regs[EAX] = prev
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("gdi32.dll", "SelectObject", _SelectObject)

    # DeleteObject(HGDIOBJ ho) -> BOOL
    def _DeleteObject(cpu: "CPU") -> None:
        ho  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        obj = _gdi_objects.get(ho)
        if obj is not None and not obj.is_stock:
            del _gdi_objects[ho]
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("gdi32.dll", "DeleteObject", _DeleteObject)

    # BitBlt(HDC, int, int, int, int, HDC, int, int, DWORD) -> BOOL
    # GDI blit to our SDL surface is a no-op; rendering goes through SDL directly.
    def _BitBlt(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 36)

    stubs.register_handler("gdi32.dll", "BitBlt", _BitBlt)

    # GetDeviceCaps(HDC hDC, int nIndex) -> int
    # nIndex constants: HORZRES=8, VERTRES=10, BITSPIXEL=12, PLANES=14,
    #                   LOGPIXELSX=88, LOGPIXELSY=90
    _DEVCAPS_NAMES: dict[int, str] = {
        8: "HORZRES", 10: "VERTRES", 12: "BITSPIXEL",
        14: "PLANES", 88: "LOGPIXELSX", 90: "LOGPIXELSY",
    }

    def _GetDeviceCaps(cpu: "CPU") -> None:
        from sdl2 import SDL_GetDesktopDisplayMode, SDL_DisplayMode
        import ctypes
        hdc     = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        n_index = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        mode = SDL_DisplayMode()
        if SDL_GetDesktopDisplayMode(0, ctypes.byref(mode)) == 0:
            screen_w, screen_h = mode.w, mode.h
        else:
            screen_w, screen_h = 1024, 768
        if n_index == 8:
            val = screen_w
        elif n_index == 10:
            val = screen_h
        elif n_index == 12:
            val = 32
        elif n_index == 14:
            val = 1
        elif n_index in (88, 90):
            val = 96
        else:
            val = 0
        cap_name = _DEVCAPS_NAMES.get(n_index, str(n_index))
        logger.debug(
            "handlers",
            f"[Win32] GetDeviceCaps(hdc=0x{hdc:x}, {cap_name}) -> {val}",
        )
        cpu.regs[EAX] = val
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("gdi32.dll", "GetDeviceCaps", _GetDeviceCaps)

    # SetDIBitsToDevice(...) -> int  (13 stdcall args)
    stubs.register_handler("gdi32.dll", "SetDIBitsToDevice", _halt("SetDIBitsToDevice"))

    # StretchDIBits(...) -> int  (13 stdcall args)
    stubs.register_handler("gdi32.dll", "StretchDIBits", _halt("StretchDIBits"))

    # CreateDIBSection(HDC, BITMAPINFO*, UINT, void**, HANDLE, DWORD) -> HBITMAP
    stubs.register_handler("gdi32.dll", "CreateDIBSection", _halt("CreateDIBSection"))

    # GetStockObject(int fnObject) -> HGDIOBJ
    def _GetStockObject(cpu: "CPU") -> None:
        fn_object = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        handle = _stock_handles.get(fn_object, 0)
        if handle == 0:
            logger.warn("handlers", f"[GDI] GetStockObject({fn_object}) — unknown stock object")
        else:
            obj = _gdi_objects[handle]
            logger.debug("handlers",
                f"[GDI] GetStockObject({fn_object}) -> 0x{handle:x} ({obj.kind})")
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("gdi32.dll", "GetStockObject", _GetStockObject)

    # CreateSolidBrush(COLORREF color) -> HBRUSH
    def _CreateSolidBrush(cpu: "CPU") -> None:
        color  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        hbrush = _next_hgdi[0]
        _next_hgdi[0] += 1
        _gdi_objects[hbrush] = _GdiObj("brush", color, 0)
        logger.debug("handlers", f"[GDI] CreateSolidBrush(0x{color:06x}) -> 0x{hbrush:x}")
        cpu.regs[EAX] = hbrush
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("gdi32.dll", "CreateSolidBrush", _CreateSolidBrush)

    # CreateFontA(...) -> HFONT  (14 stdcall args)
    stubs.register_handler("gdi32.dll", "CreateFontA", _halt("CreateFontA"))

    # TextOutA(HDC hDC, int x, int y, LPCSTR lpString, int c) -> BOOL
    # GDI text output to our SDL surface is a no-op.
    def _TextOutA(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("gdi32.dll", "TextOutA", _TextOutA)

    # SetBkMode(HDC hDC, int iMode) -> int (previous mode)
    stubs.register_handler("gdi32.dll", "SetBkMode", _halt("SetBkMode"))

    # SetTextColor(HDC hDC, COLORREF color) -> COLORREF (previous color)
    stubs.register_handler("gdi32.dll", "SetTextColor", _halt("SetTextColor"))
