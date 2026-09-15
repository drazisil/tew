"""dinput.dll / dinput8.dll handler registrations.

Implements DirectInputCreateA and minimal IDirectInput2A + IDirectInputDevice2A stubs
sufficient for the game to complete DI initialisation and proceed to the render loop.

COM vtable addresses (fixed-data region, after D3DTEX_VTABLE ends at 0x002202D8):
    DI_VTABLE     = 0x002202E0  (IDirectInput2A,       9 slots × 4 =  36 bytes → 0x00220304)
    DI_OBJ        = 0x00220310  (IDirectInput2A object, 4 bytes)
    DI_DEV_VTABLE = 0x00220320  (IDirectInputDevice2A, 26 slots × 4 = 104 bytes → 0x00220388)
    Device objects are bump-allocated from the D3D8 heap (8 bytes each).

    The vtable's real slot count matters, not just "enough methods to boot":
    real IDirectInputDevice2 has 26 methods (indices 0-25, through Poll at
    offset 0x64) -- slots 18-25 (CreateEffect through Poll) were missing
    entirely until a real, confirmed live crash (EIP=0x00000000, unhandled
    CPU fault) traced a compiled Poll()/Acquire()-retry helper
    (_INPUT_getdevicedata, 0x00a73d40) calling vtable+0x64 -- 32 bytes past
    the vtable's own end, into whatever memory happened to follow it.

All handlers read `this` from ESP+4 (dx8z / game push `this` on stack, not ECX).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory
    from tew.api._state import CRTState

import ctypes

from tew.hardware.cpu_zig import EAX, ESP
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall
from tew.api.d3d8._helpers import _com_stub, _heap_alloc, _set_eax
from tew.logger import logger

# SDL scancode -> DIK_* code, for the keys a game actually polls in practice
# (letters, digits, common punctuation, function keys, arrows, modifiers).
# DIK_* values ARE the real PC/AT set-1 keyboard scancodes (per dinput.h) --
# not an arbitrary DirectInput invention -- so this table is fixed, not
# guessed. Extended keys (arrows, ctrl/alt right-hand variants, navigation
# cluster) use their real DIK_* extended-scancode values, which happen to
# already be single bytes >= 0x80 in DirectInput's numbering (no separate
# 0xE0 prefix byte the way raw PS/2 set 1 needs) -- values taken directly
# from dinput.h, not derived.
_SDL_SCANCODE_TO_DIK: dict[int, int] = {}


def _build_scancode_table() -> dict[int, int]:
    import sdl2 as _sdl2
    t: dict[int, int] = {
        _sdl2.SDL_SCANCODE_ESCAPE: 0x01,
        _sdl2.SDL_SCANCODE_1: 0x02, _sdl2.SDL_SCANCODE_2: 0x03,
        _sdl2.SDL_SCANCODE_3: 0x04, _sdl2.SDL_SCANCODE_4: 0x05,
        _sdl2.SDL_SCANCODE_5: 0x06, _sdl2.SDL_SCANCODE_6: 0x07,
        _sdl2.SDL_SCANCODE_7: 0x08, _sdl2.SDL_SCANCODE_8: 0x09,
        _sdl2.SDL_SCANCODE_9: 0x0A, _sdl2.SDL_SCANCODE_0: 0x0B,
        _sdl2.SDL_SCANCODE_MINUS: 0x0C, _sdl2.SDL_SCANCODE_EQUALS: 0x0D,
        _sdl2.SDL_SCANCODE_BACKSPACE: 0x0E, _sdl2.SDL_SCANCODE_TAB: 0x0F,
        _sdl2.SDL_SCANCODE_Q: 0x10, _sdl2.SDL_SCANCODE_W: 0x11,
        _sdl2.SDL_SCANCODE_E: 0x12, _sdl2.SDL_SCANCODE_R: 0x13,
        _sdl2.SDL_SCANCODE_T: 0x14, _sdl2.SDL_SCANCODE_Y: 0x15,
        _sdl2.SDL_SCANCODE_U: 0x16, _sdl2.SDL_SCANCODE_I: 0x17,
        _sdl2.SDL_SCANCODE_O: 0x18, _sdl2.SDL_SCANCODE_P: 0x19,
        _sdl2.SDL_SCANCODE_LEFTBRACKET: 0x1A, _sdl2.SDL_SCANCODE_RIGHTBRACKET: 0x1B,
        _sdl2.SDL_SCANCODE_RETURN: 0x1C, _sdl2.SDL_SCANCODE_LCTRL: 0x1D,
        _sdl2.SDL_SCANCODE_A: 0x1E, _sdl2.SDL_SCANCODE_S: 0x1F,
        _sdl2.SDL_SCANCODE_D: 0x20, _sdl2.SDL_SCANCODE_F: 0x21,
        _sdl2.SDL_SCANCODE_G: 0x22, _sdl2.SDL_SCANCODE_H: 0x23,
        _sdl2.SDL_SCANCODE_J: 0x24, _sdl2.SDL_SCANCODE_K: 0x25,
        _sdl2.SDL_SCANCODE_L: 0x26, _sdl2.SDL_SCANCODE_SEMICOLON: 0x27,
        _sdl2.SDL_SCANCODE_APOSTROPHE: 0x28, _sdl2.SDL_SCANCODE_GRAVE: 0x29,
        _sdl2.SDL_SCANCODE_LSHIFT: 0x2A, _sdl2.SDL_SCANCODE_BACKSLASH: 0x2B,
        _sdl2.SDL_SCANCODE_Z: 0x2C, _sdl2.SDL_SCANCODE_X: 0x2D,
        _sdl2.SDL_SCANCODE_C: 0x2E, _sdl2.SDL_SCANCODE_V: 0x2F,
        _sdl2.SDL_SCANCODE_B: 0x30, _sdl2.SDL_SCANCODE_N: 0x31,
        _sdl2.SDL_SCANCODE_M: 0x32, _sdl2.SDL_SCANCODE_COMMA: 0x33,
        _sdl2.SDL_SCANCODE_PERIOD: 0x34, _sdl2.SDL_SCANCODE_SLASH: 0x35,
        _sdl2.SDL_SCANCODE_RSHIFT: 0x36, _sdl2.SDL_SCANCODE_KP_MULTIPLY: 0x37,
        _sdl2.SDL_SCANCODE_LALT: 0x38, _sdl2.SDL_SCANCODE_SPACE: 0x39,
        _sdl2.SDL_SCANCODE_CAPSLOCK: 0x3A,
        _sdl2.SDL_SCANCODE_F1: 0x3B, _sdl2.SDL_SCANCODE_F2: 0x3C,
        _sdl2.SDL_SCANCODE_F3: 0x3D, _sdl2.SDL_SCANCODE_F4: 0x3E,
        _sdl2.SDL_SCANCODE_F5: 0x3F, _sdl2.SDL_SCANCODE_F6: 0x40,
        _sdl2.SDL_SCANCODE_F7: 0x41, _sdl2.SDL_SCANCODE_F8: 0x42,
        _sdl2.SDL_SCANCODE_F9: 0x43, _sdl2.SDL_SCANCODE_F10: 0x44,
        _sdl2.SDL_SCANCODE_NUMLOCKCLEAR: 0x45, _sdl2.SDL_SCANCODE_SCROLLLOCK: 0x46,
        _sdl2.SDL_SCANCODE_KP_7: 0x47, _sdl2.SDL_SCANCODE_KP_8: 0x48,
        _sdl2.SDL_SCANCODE_KP_9: 0x49, _sdl2.SDL_SCANCODE_KP_MINUS: 0x4A,
        _sdl2.SDL_SCANCODE_KP_4: 0x4B, _sdl2.SDL_SCANCODE_KP_5: 0x4C,
        _sdl2.SDL_SCANCODE_KP_6: 0x4D, _sdl2.SDL_SCANCODE_KP_PLUS: 0x4E,
        _sdl2.SDL_SCANCODE_KP_1: 0x4F, _sdl2.SDL_SCANCODE_KP_2: 0x50,
        _sdl2.SDL_SCANCODE_KP_3: 0x51, _sdl2.SDL_SCANCODE_KP_0: 0x52,
        _sdl2.SDL_SCANCODE_KP_PERIOD: 0x53,
        _sdl2.SDL_SCANCODE_F11: 0x57, _sdl2.SDL_SCANCODE_F12: 0x58,
        _sdl2.SDL_SCANCODE_KP_ENTER: 0x9C, _sdl2.SDL_SCANCODE_RCTRL: 0x9D,
        _sdl2.SDL_SCANCODE_KP_DIVIDE: 0xB5, _sdl2.SDL_SCANCODE_RALT: 0xB8,
        _sdl2.SDL_SCANCODE_HOME: 0xC7, _sdl2.SDL_SCANCODE_UP: 0xC8,
        _sdl2.SDL_SCANCODE_PAGEUP: 0xC9, _sdl2.SDL_SCANCODE_LEFT: 0xCB,
        _sdl2.SDL_SCANCODE_RIGHT: 0xCD, _sdl2.SDL_SCANCODE_END: 0xCF,
        _sdl2.SDL_SCANCODE_DOWN: 0xD0, _sdl2.SDL_SCANCODE_PAGEDOWN: 0xD1,
        _sdl2.SDL_SCANCODE_INSERT: 0xD2, _sdl2.SDL_SCANCODE_DELETE: 0xD3,
    }
    return t


# Real, event-driven mouse state (2026-09-13 redesign). Previously this
# module polled SDL_GetMouseState() on demand from inside GetDeviceState/
# GetDeviceData -- which meant that if the game never called either of
# those (confirmed live: it doesn't, during the persona-select screen),
# tew never sampled the mouse at all, and a debug tool faking a click by
# overriding what the *next poll* would return was fixing nothing real,
# since nothing was ever polling.
#
# Real DirectInput doesn't work by polling hardware on demand either -- the
# driver tracks state continuously in the background from real input
# events, independent of whether/when the app asks. notify_mouse_motion/
# notify_mouse_button are window_manager.py's real SDL event pump calling
# straight into this module as those events actually arrive (see
# _handle_sdl_event's SDL_MOUSEMOTION/BUTTONDOWN/BUTTONUP handling), which
# is the same real event stream Win32 WM_MOUSEMOVE/WM_LBUTTONDOWN messages
# come from -- not a separate side channel.
_mouse_pos: list[int] = [0, 0]
_mouse_buttons: list[int] = [0]

# Absolute position as of the last GetDeviceState call specifically, kept
# separate from _mouse_pos -- GetDeviceState reports lX/lY as the delta
# since *it* was last called (DirectInput's relative-axis convention),
# which is a different consumer than GetDeviceData's queue below.
_last_polled_pos: list[int] = [0, 0]

# Buffered DIDEVICEOBJECTDATA queue for GetDeviceData: (dwOfs, dwData)
# pairs, appended in real time as notify_mouse_motion/notify_mouse_button
# observe an actual transition, and drained by GetDeviceData.
_mouse_dod_queue: list[tuple[int, int]] = []

# hEvent handles registered via SetEventNotification, keyed by device
# object address (`this`) -- signaled for real by _signal_registered_events()
# whenever a real mouse event arrives. Needs the CRTState set by
# register_dinput_handlers to reach kernel_handle_map/scheduler.
_registered_events: dict[int, int] = {}
_state_ref = None


def get_mouse_buttons() -> int:
    """Current real mouse button bitmask (SDL_BUTTON(...) mask shape) for
    consumers that just want current state, not GetDeviceState's own
    per-call delta bookkeeping -- e.g. GetKeyState/GetAsyncKeyState's
    mouse-button VK handling in user32_handlers.py."""
    return _mouse_buttons[0]


def notify_mouse_motion(x: int, y: int) -> None:
    """Real SDL_MOUSEMOTION arrived (window_manager.py's event pump) --
    not a polling entrypoint. Updates tracked position, queues an axis
    DIDEVICEOBJECTDATA entry, and signals any registered event."""
    dx = x - _mouse_pos[0]
    dy = y - _mouse_pos[1]
    _mouse_pos[0] = x
    _mouse_pos[1] = y
    if not (dx or dy):
        return
    if dx:
        _mouse_dod_queue.append((0, dx & 0xFFFFFFFF))
    if dy:
        _mouse_dod_queue.append((4, dy & 0xFFFFFFFF))
    _signal_registered_events()


def notify_mouse_button(sdl_button: int, is_down: bool) -> None:
    """Real SDL_MOUSEBUTTONDOWN/UP arrived (window_manager.py's event
    pump) -- not a polling entrypoint. Updates the tracked button bitmask,
    queues a button DIDEVICEOBJECTDATA entry, and signals any registered
    event."""
    import sdl2 as _sdl2
    ofs = {
        _sdl2.SDL_BUTTON_LEFT:   12,
        _sdl2.SDL_BUTTON_RIGHT:  13,
        _sdl2.SDL_BUTTON_MIDDLE: 14,
    }.get(sdl_button)
    if ofs is None:
        return
    mask = _sdl2.SDL_BUTTON(sdl_button)
    was_down = bool(_mouse_buttons[0] & mask)
    if was_down == is_down:
        return
    if is_down:
        _mouse_buttons[0] |= mask
    else:
        _mouse_buttons[0] &= ~mask
    logger.debug("handlers",
        f"[dinput] real mouse button {sdl_button} {'down' if is_down else 'up'} at ({_mouse_pos[0]},{_mouse_pos[1]})")
    _mouse_dod_queue.append((ofs, 0x80 if is_down else 0x00))
    _signal_registered_events()


def _signal_registered_events() -> None:
    if _state_ref is None or not _registered_events:
        return
    from tew.api._state import EventHandle
    for h_event in set(_registered_events.values()):
        if not h_event:
            continue
        obj = _state_ref.kernel_handle_map.get(h_event)
        if isinstance(obj, EventHandle):
            obj.signaled = True
            _state_ref.scheduler.unblock_handle(h_event)


def _sample_for_get_device_state() -> tuple[int, int, int, int, int]:
    """GetDeviceState's own view of the real, event-driven state: current
    position/buttons plus the delta since GetDeviceState was last called."""
    x, y = _mouse_pos[0], _mouse_pos[1]
    dx = x - _last_polled_pos[0]
    dy = y - _last_polled_pos[1]
    _last_polled_pos[0] = x
    _last_polled_pos[1] = y
    return x, y, _mouse_buttons[0], dx, dy

# ── Fixed COM object addresses ────────────────────────────────────────────────
DI_VTABLE     = 0x002202E0   # IDirectInput2A vtable     (9  × 4 = 36 bytes → 0x00220304)
DI_OBJ        = 0x00220310   # IDirectInput2A singleton  (4 bytes)
DI_DEV_VTABLE = 0x00220320   # IDirectInputDevice2A vtable (26 × 4 = 104 bytes → 0x00220388)

# ── DirectInput error / status codes ─────────────────────────────────────────
DI_OK                  = 0x00000000
DI_NOTATTACHED         = 0x00000001
DI_POLLEDDEVICE        = 0x00000002
E_NOTIMPL              = 0x80004001
E_NOINTERFACE          = 0x80004002
DIERR_UNSUPPORTED      = 0x80004001   # same as E_NOTIMPL for DI
DIERR_DEVICENOTREG     = 0x80040154
DIERR_OBJECTNOTFOUND   = 0x80040181


def register_dinput_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register all DirectInput COM stubs and write vtable pointers into memory."""
    global _state_ref
    _state_ref = state

    # ── IDirectInput2A vtable ─────────────────────────────────────────────────

    def _di_query_interface(cpu: "CPU", mem: "Memory") -> None:
        ppv = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if ppv:
            mem.write32(ppv, 0)
        cpu.regs[EAX] = E_NOINTERFACE

    def _di_create_device(cpu: "CPU", mem: "Memory") -> None:
        # CreateDevice(REFGUID, lplpDID, pUnkOuter) — arg_bytes=12
        pp_dev = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if pp_dev:
            obj = _heap_alloc(8, "dinput_obj")
            mem.write32(obj,     DI_DEV_VTABLE)
            mem.write32(obj + 4, 0)
            mem.write32(pp_dev, obj)
            logger.debug("handlers", f"DI::CreateDevice -> dev_obj=0x{obj:08x}")
        cpu.regs[EAX] = DI_OK

    di_vtable = [
        # [0] QueryInterface(REFIID, void**)
        _com_stub(stubs, "dinput.dll", "DI::QueryInterface",
                  _di_query_interface, 8, memory, DI_OBJ),
        # [1] AddRef()
        _com_stub(stubs, "dinput.dll", "DI::AddRef",
                  lambda cpu, mem: _set_eax(cpu, 2), 0, memory, DI_OBJ),
        # [2] Release()
        _com_stub(stubs, "dinput.dll", "DI::Release",
                  lambda cpu, mem: _set_eax(cpu, 1), 0, memory, DI_OBJ),
        # [3] CreateDevice(REFGUID, lplpDID, pUnkOuter)
        _com_stub(stubs, "dinput.dll", "DI::CreateDevice",
                  _di_create_device, 12, memory, DI_OBJ),
        # [4] EnumDevices(dwType, lpCallback, pvRef, dwFlags)
        _com_stub(stubs, "dinput.dll", "DI::EnumDevices",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 16, memory, DI_OBJ),
        # [5] GetDeviceStatus(REFGUID)
        _com_stub(stubs, "dinput.dll", "DI::GetDeviceStatus",
                  lambda cpu, mem: _set_eax(cpu, DI_NOTATTACHED), 4, memory, DI_OBJ),
        # [6] RunControlPanel(hwnd, dwFlags)
        _com_stub(stubs, "dinput.dll", "DI::RunControlPanel",
                  lambda cpu, mem: _set_eax(cpu, E_NOTIMPL), 8, memory, DI_OBJ),
        # [7] Initialize(hInst, dwVersion)
        _com_stub(stubs, "dinput.dll", "DI::Initialize",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 8, memory, DI_OBJ),
        # [8] FindDevice(REFGUID, ptszName, pguidOut)
        _com_stub(stubs, "dinput.dll", "DI::FindDevice",
                  lambda cpu, mem: _set_eax(cpu, DIERR_DEVICENOTREG), 12, memory, DI_OBJ),
    ]
    for i, addr in enumerate(di_vtable):
        memory.write32(DI_VTABLE + i * 4, addr)
    memory.write32(DI_OBJ, DI_VTABLE)

    # ── IDirectInputDevice2A vtable ───────────────────────────────────────────

    def _dev_query_interface(cpu: "CPU", mem: "Memory") -> None:
        # Game QIs for IDirectInputDevice2A from the device it just created —
        # return the same object (AddRef implicit, device is our singleton stub).
        this_ptr = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        ppv = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if ppv:
            mem.write32(ppv, this_ptr)
        cpu.regs[EAX] = DI_OK

    def _dev_get_caps(cpu: "CPU", mem: "Memory") -> None:
        # GetCapabilities(LPDIDEVCAPS lpDIDevCaps) — zero-fill struct
        p_caps = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_caps:
            # DIDEVCAPS: dwSize(4) + dwFlags(4) + dwDevType(4) + dwAxes(4)
            #            + dwButtons(4) + dwPOVs(4) + dwFFSamplePeriod(4)
            #            + dwFFMinTimeResolution(4) + dwFirmwareRevision(4)
            #            + dwHardwareRevision(4) + dwFFDriverVersion(4) = 44 bytes
            dw_size = mem.read32(p_caps & 0xFFFFFFFF)
            size = max(dw_size, 44) if dw_size else 44
            for off in range(0, size, 4):
                mem.write32((p_caps + off) & 0xFFFFFFFF, 0)
        cpu.regs[EAX] = DI_OK

    def _dev_get_device_state(cpu: "CPU", mem: "Memory") -> None:
        # GetDeviceState(DWORD cbData, LPVOID lpvData) -- real SDL polling.
        # This device object is generic (CreateDevice doesn't distinguish
        # keyboard vs. mouse by REFGUID -- see its own comment), so which
        # real device to poll is inferred from cbData, the one thing the
        # caller always tells us: 256 means the 256-byte DirectInput
        # keyboard buffer, anything else (16 for DIMOUSESTATE, 20 for
        # DIMOUSESTATE2) means the mouse.
        cb_data  = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        lpv_data = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if not (lpv_data and cb_data):
            cpu.regs[EAX] = DI_OK
            return

        import sdl2 as _sdl2
        if not _SDL_SCANCODE_TO_DIK:
            _SDL_SCANCODE_TO_DIK.update(_build_scancode_table())

        if cb_data == 256:
            for off in range(256):
                mem.write8((lpv_data + off) & 0xFFFFFFFF, 0)
            num_keys = ctypes.c_int(0)
            state_ptr = _sdl2.SDL_GetKeyboardState(ctypes.byref(num_keys))
            state = ctypes.cast(state_ptr, ctypes.POINTER(ctypes.c_uint8 * num_keys.value)).contents
            for scancode, dik in _SDL_SCANCODE_TO_DIK.items():
                if scancode < num_keys.value and state[scancode]:
                    mem.write8((lpv_data + dik) & 0xFFFFFFFF, 0x80)
        else:
            _x, _y, buttons, dx, dy = _sample_for_get_device_state()
            mem.write32(lpv_data,      dx & 0xFFFFFFFF)          # lX
            mem.write32((lpv_data + 4) & 0xFFFFFFFF, dy & 0xFFFFFFFF)  # lY
            mem.write32((lpv_data + 8) & 0xFFFFFFFF, 0)          # lZ (wheel, not tracked)
            btn_bytes = [
                0x80 if buttons & _sdl2.SDL_BUTTON(_sdl2.SDL_BUTTON_LEFT) else 0,
                0x80 if buttons & _sdl2.SDL_BUTTON(_sdl2.SDL_BUTTON_RIGHT) else 0,
                0x80 if buttons & _sdl2.SDL_BUTTON(_sdl2.SDL_BUTTON_MIDDLE) else 0,
                0,
            ]
            for i, b in enumerate(btn_bytes):
                if 12 + i < cb_data:
                    mem.write8((lpv_data + 12 + i) & 0xFFFFFFFF, b)
            for off in range(16, cb_data):
                mem.write8((lpv_data + off) & 0xFFFFFFFF, 0)
        cpu.regs[EAX] = DI_OK

    # FIXED (2026-09-13): was a lying no-op that always reported zero
    # buffered events, regardless of any real mouse/keyboard activity --
    # confirmed live investigating why the persona-select screen never
    # reacted to a click: GetDeviceState's mouse branch was never even
    # being called (no log line ever appeared across a full run), meaning
    # this game's FEDC GUI system reads its click events through buffered
    # mode instead, if it uses DirectInput at all. _mouse_dod_queue is now
    # populated in real time by notify_mouse_motion/notify_mouse_button
    # (window_manager.py's real SDL event pump) rather than lazily on
    # poll, so buffered events exist even if GetDeviceState is never
    # called. Only the mouse's transitions are tracked -- this device
    # object doesn't distinguish keyboard vs. mouse (see CreateDevice's
    # own comment), and nothing in this project has needed buffered
    # keyboard events yet.
    _DIGDD_PEEK = 0x00000001

    def _dev_get_device_data(cpu: "CPU", mem: "Memory") -> None:
        # GetDeviceData(cbObjectData, rgdod, pdwInOut, dwFlags)
        cb_object_data = mem.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        p_rgdod        = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        pdw_in_out     = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        dw_flags       = mem.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)

        if not p_rgdod:
            # NULL rgdod: caller is just asking how many events are queued.
            if pdw_in_out:
                mem.write32(pdw_in_out, len(_mouse_dod_queue))
            cpu.regs[EAX] = DI_OK
            return

        requested = mem.read32(pdw_in_out & 0xFFFFFFFF) if pdw_in_out else len(_mouse_dod_queue)
        n = min(requested, len(_mouse_dod_queue)) if cb_object_data else 0
        peek = bool(dw_flags & _DIGDD_PEEK)

        for i in range(n):
            ofs, data = _mouse_dod_queue[i] if peek else _mouse_dod_queue.pop(0)
            base = (p_rgdod + i * cb_object_data) & 0xFFFFFFFF
            if cb_object_data >= 4:
                mem.write32(base, ofs)                                    # dwOfs
            if cb_object_data >= 8:
                mem.write32((base + 4) & 0xFFFFFFFF, data)                # dwData
            if cb_object_data >= 12:
                mem.write32((base + 8) & 0xFFFFFFFF, 0)                   # dwTimeStamp
            if cb_object_data >= 16:
                mem.write32((base + 12) & 0xFFFFFFFF, i)                  # dwSequence

        if pdw_in_out:
            mem.write32(pdw_in_out, n)
        cpu.regs[EAX] = DI_OK

    # FIXED (2026-09-13): accepted the registration and returned success,
    # but never stored hEvent anywhere -- so a game thread doing
    # WaitForSingleObject/WaitForMultipleObjects on it to be woken by real
    # input would wait forever for that specific reason (while still
    # legitimately waking for its other wait conditions, which is why a
    # game stuck this way doesn't look hung). Now stored per-device and
    # actually signaled by _signal_registered_events() whenever
    # notify_mouse_motion/notify_mouse_button observes a real transition.
    def _dev_set_event_notification(cpu: "CPU", mem: "Memory") -> None:
        # SetEventNotification(hEvent) — hEvent=NULL means polled
        this    = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        h_event = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if h_event:
            _registered_events[this] = h_event
            logger.debug("handlers",
                f"[dinput] device 0x{this:08x} registered event notification hEvent=0x{h_event:x}")
        else:
            _registered_events.pop(this, None)
        cpu.regs[EAX] = DI_POLLEDDEVICE if h_event == 0 else DI_OK

    def _dev_get_device_info(cpu: "CPU", mem: "Memory") -> None:
        # GetDeviceInfo(LPDIDEVICEINSTANCEA) — zero-fill struct
        p_info = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_info:
            # DIDEVICEINSTANCEA: dwSize + many fields; zero first 80 bytes
            dw_size = mem.read32(p_info & 0xFFFFFFFF)
            size = max(dw_size, 80) if dw_size else 80
            for off in range(0, size, 4):
                mem.write32((p_info + off) & 0xFFFFFFFF, 0)
        cpu.regs[EAX] = DI_OK

    dev_vtable = [
        # [0] QueryInterface(REFIID, void**)
        _com_stub(stubs, "dinput.dll", "Dev::QueryInterface",
                  _dev_query_interface, 8, memory),
        # [1] AddRef()
        _com_stub(stubs, "dinput.dll", "Dev::AddRef",
                  lambda cpu, mem: _set_eax(cpu, 2), 0, memory),
        # [2] Release()
        _com_stub(stubs, "dinput.dll", "Dev::Release",
                  lambda cpu, mem: _set_eax(cpu, 1), 0, memory),
        # [3] GetCapabilities(LPDIDEVCAPS)
        _com_stub(stubs, "dinput.dll", "Dev::GetCapabilities",
                  _dev_get_caps, 4, memory),
        # [4] EnumObjects(lpCallback, pvRef, dwFlags)
        _com_stub(stubs, "dinput.dll", "Dev::EnumObjects",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 12, memory),
        # [5] GetProperty(REFGUID, pdiph)
        _com_stub(stubs, "dinput.dll", "Dev::GetProperty",
                  lambda cpu, mem: _set_eax(cpu, DIERR_UNSUPPORTED), 8, memory),
        # [6] SetProperty(REFGUID, pdiph)
        _com_stub(stubs, "dinput.dll", "Dev::SetProperty",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 8, memory),
        # [7] Acquire()
        _com_stub(stubs, "dinput.dll", "Dev::Acquire",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 0, memory),
        # [8] Unacquire()
        _com_stub(stubs, "dinput.dll", "Dev::Unacquire",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 0, memory),
        # [9] GetDeviceState(cbData, lpvData)
        _com_stub(stubs, "dinput.dll", "Dev::GetDeviceState",
                  _dev_get_device_state, 8, memory),
        # [10] GetDeviceData(cbObjectData, rgdod, pdwInOut, dwFlags)
        _com_stub(stubs, "dinput.dll", "Dev::GetDeviceData",
                  _dev_get_device_data, 16, memory),
        # [11] SetDataFormat(LPCDIDATAFORMAT)
        _com_stub(stubs, "dinput.dll", "Dev::SetDataFormat",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 4, memory),
        # [12] SetEventNotification(HANDLE)
        _com_stub(stubs, "dinput.dll", "Dev::SetEventNotification",
                  _dev_set_event_notification, 4, memory),
        # [13] SetCooperativeLevel(hwnd, dwFlags)
        _com_stub(stubs, "dinput.dll", "Dev::SetCooperativeLevel",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 8, memory),
        # [14] GetObjectInfo(pdidoi, dwObj, dwHow)
        _com_stub(stubs, "dinput.dll", "Dev::GetObjectInfo",
                  lambda cpu, mem: _set_eax(cpu, DIERR_OBJECTNOTFOUND), 12, memory),
        # [15] GetDeviceInfo(LPDIDEVICEINSTANCEA)
        _com_stub(stubs, "dinput.dll", "Dev::GetDeviceInfo",
                  _dev_get_device_info, 4, memory),
        # [16] RunControlPanel(hwnd, dwFlags)
        _com_stub(stubs, "dinput.dll", "Dev::RunControlPanel",
                  lambda cpu, mem: _set_eax(cpu, E_NOTIMPL), 8, memory),
        # [17] Initialize(hinst, dwVersion, REFGUID)
        _com_stub(stubs, "dinput.dll", "Dev::Initialize",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 12, memory),
        # [18] CreateEffect(REFGUID, LPCDIEFFECT, LPDIRECTINPUTEFFECT*, LPUNKNOWN)
        _com_stub(stubs, "dinput.dll", "Dev::CreateEffect",
                  lambda cpu, mem: _set_eax(cpu, DIERR_UNSUPPORTED), 16, memory),
        # [19] EnumEffects(LPDIENUMEFFECTSCALLBACK, LPVOID, DWORD) -- nothing to enumerate
        _com_stub(stubs, "dinput.dll", "Dev::EnumEffects",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 12, memory),
        # [20] GetEffectInfo(LPDIEFFECTINFO, REFGUID)
        _com_stub(stubs, "dinput.dll", "Dev::GetEffectInfo",
                  lambda cpu, mem: _set_eax(cpu, DIERR_UNSUPPORTED), 8, memory),
        # [21] GetForceFeedbackState(LPDWORD)
        _com_stub(stubs, "dinput.dll", "Dev::GetForceFeedbackState",
                  lambda cpu, mem: _set_eax(cpu, DIERR_UNSUPPORTED), 4, memory),
        # [22] SendForceFeedbackCommand(DWORD)
        _com_stub(stubs, "dinput.dll", "Dev::SendForceFeedbackCommand",
                  lambda cpu, mem: _set_eax(cpu, DIERR_UNSUPPORTED), 4, memory),
        # [23] EnumCreatedEffectObjects(LPDIENUMCREATEDEFFECTOBJECTSCALLBACK, LPVOID, DWORD)
        # -- no effects were ever created (CreateEffect always fails above), so
        # there's genuinely nothing to enumerate; DI_OK is the honest answer,
        # not a stand-in for unimplemented force feedback.
        _com_stub(stubs, "dinput.dll", "Dev::EnumCreatedEffectObjects",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 12, memory),
        # [24] Escape(LPDIEFFESCAPE) -- driver-specific passthrough, no real driver here
        _com_stub(stubs, "dinput.dll", "Dev::Escape",
                  lambda cpu, mem: _set_eax(cpu, DIERR_UNSUPPORTED), 4, memory),
        # [25] Poll() -- real, confirmed bug this fixes: this slot didn't exist
        # at all until now (DI_DEV_VTABLE was only 18 slots, offsets 0-0x44),
        # so _INPUT_getdevicedata's real compiled Poll()-then-Acquire()-retry
        # idiom (0x00a73d40) read a null function pointer 32 bytes past the
        # vtable's own end and jumped to EIP=0 -- confirmed live via a real
        # unhandled CPU fault. GetDeviceState/GetDeviceData already report
        # device state synchronously and unconditionally on every call (no
        # internal queue to advance), so there is nothing for a real poll to
        # do here; DI_OK is the correct, honest "state is already current"
        # answer, not a placeholder.
        _com_stub(stubs, "dinput.dll", "Dev::Poll",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 0, memory),
    ]
    for i, addr in enumerate(dev_vtable):
        memory.write32(DI_DEV_VTABLE + i * 4, addr)

    # ── DirectInputCreateA DLL export ─────────────────────────────────────────

    def _direct_input_create_a(cpu: "CPU") -> None:
        # DirectInputCreateA(hInst, dwVersion, lplpDirectInput, pUnkOuter)
        # lplpDirectInput at ESP+12
        pp_di = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if pp_di:
            memory.write32(pp_di, DI_OBJ)
        logger.info("handlers", f"DirectInputCreateA -> DI_OBJ=0x{DI_OBJ:08x}")
        cpu.regs[EAX] = DI_OK
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("dinput.dll",  "DirectInputCreateA", _direct_input_create_a)
    stubs.register_handler("dinput8.dll", "DirectInputCreateA", _direct_input_create_a)

    def _direct_input8_create(cpu: "CPU") -> None:
        # DirectInput8Create(hInst, dwVersion, riidltf, ppvOut, punkOuter) — 5 args, 20 bytes
        # ppvOut at ESP+16 (arg 4)
        pp_di = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        if pp_di:
            memory.write32(pp_di, DI_OBJ)
        logger.info("handlers", f"DirectInput8Create -> DI_OBJ=0x{DI_OBJ:08x}")
        cpu.regs[EAX] = DI_OK
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("dinput8.dll", "DirectInput8Create", _direct_input8_create)

    logger.info("handlers", "DirectInput handlers registered — IDirectInput2A + IDirectInputDevice2A stubs wired")
