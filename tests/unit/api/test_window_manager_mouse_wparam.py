"""
WindowManager must derive WM_LBUTTONDOWN/UP/MOUSEMOVE wParam MK_* bits from the
button events it has actually seen, never from SDL_GetMouseState().

Regression coverage for the click-delivery bug found live 2026-09-18: a guest
probe on FUN_00780d80 (the game's WM_*BUTTON* handler, which builds its whole
button mask from wParam's MK_LBUTTON bit alone) showed a real click delivering
wParam=0x1 and a synthetic SDL_PushEvent click delivering wParam=0x0 -- same
hwnd, same lParam, same message. SDL_GetMouseState() is the state at POLL time
(a quick real click under emulator lag can already be released) and is never
updated by an injected event.

These feed events straight to _handle_sdl_event without going through SDL at
all, so SDL's own global mouse state stays "nothing down" throughout -- the old
implementation would produce wParam=0 for every case below.
"""

from sdl2 import (
    SDL_Event, SDL_MOUSEBUTTONDOWN, SDL_MOUSEBUTTONUP, SDL_MOUSEMOTION,
    SDL_WINDOWEVENT, SDL_WINDOWEVENT_FOCUS_LOST,
    SDL_BUTTON_LEFT, SDL_BUTTON_RIGHT,
)

from tew.api.window_manager import (
    WindowManager, WindowEntry,
    WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE,
)

HWND = 0x1034
WINDOW_ID = 7
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002


def make_wm() -> WindowManager:
    wm = WindowManager()
    wm._windows[HWND] = WindowEntry(
        hwnd=HWND, class_name="MCity", title="Motor City Online",
        style=0, ex_style=0, x=0, y=0, cx=640, cy=480, parent_hwnd=0,
    )
    wm._sdl_window_id_to_hwnd[WINDOW_ID] = HWND
    return wm


def button_event(etype: int, button: int, x: int = 387, y: int = 491) -> SDL_Event:
    ev = SDL_Event()
    ev.type = etype
    ev.button.windowID = WINDOW_ID
    ev.button.button = button
    ev.button.x = x
    ev.button.y = y
    return ev


def motion_event(state: int = 0, x: int = 387, y: int = 491) -> SDL_Event:
    ev = SDL_Event()
    ev.type = SDL_MOUSEMOTION
    ev.motion.windowID = WINDOW_ID
    ev.motion.state = state
    ev.motion.x = x
    ev.motion.y = y
    return ev


def messages(wm: WindowManager, msg: int) -> list[tuple[int, int, int, int]]:
    return [m for m in wm._message_queue if m[1] == msg]


def test_left_button_down_carries_mk_lbutton():
    wm = make_wm()
    wm._handle_sdl_event(button_event(SDL_MOUSEBUTTONDOWN, SDL_BUTTON_LEFT))
    (hwnd, msg, wparam, lparam), = messages(wm, WM_LBUTTONDOWN)
    assert hwnd == HWND
    assert wparam == MK_LBUTTON
    assert lparam == (387 | (491 << 16))


def test_left_button_up_omits_mk_lbutton():
    wm = make_wm()
    wm._handle_sdl_event(button_event(SDL_MOUSEBUTTONDOWN, SDL_BUTTON_LEFT))
    wm._handle_sdl_event(button_event(SDL_MOUSEBUTTONUP, SDL_BUTTON_LEFT))
    (_, _, wparam, _), = messages(wm, WM_LBUTTONUP)
    assert wparam == 0


def test_left_down_while_right_held_carries_both_bits():
    wm = make_wm()
    # Right button is not forwarded as its own message, but must still be tracked.
    wm._handle_sdl_event(button_event(SDL_MOUSEBUTTONDOWN, SDL_BUTTON_RIGHT))
    wm._handle_sdl_event(button_event(SDL_MOUSEBUTTONDOWN, SDL_BUTTON_LEFT))
    (_, _, wparam, _), = messages(wm, WM_LBUTTONDOWN)
    assert wparam == MK_LBUTTON | MK_RBUTTON


def test_left_up_while_right_still_held_keeps_mk_rbutton():
    wm = make_wm()
    wm._handle_sdl_event(button_event(SDL_MOUSEBUTTONDOWN, SDL_BUTTON_RIGHT))
    wm._handle_sdl_event(button_event(SDL_MOUSEBUTTONDOWN, SDL_BUTTON_LEFT))
    wm._handle_sdl_event(button_event(SDL_MOUSEBUTTONUP, SDL_BUTTON_LEFT))
    (_, _, wparam, _), = messages(wm, WM_LBUTTONUP)
    assert wparam == MK_RBUTTON


def test_mousemove_during_held_button_carries_mk_lbutton_even_if_event_state_is_zero():
    # A synthetic motion event has no meaningful .state (it's built by hand,
    # not by SDL from real hardware) -- the held button must still show up.
    wm = make_wm()
    wm._handle_sdl_event(button_event(SDL_MOUSEBUTTONDOWN, SDL_BUTTON_LEFT))
    wm._handle_sdl_event(motion_event(state=0))
    (_, _, wparam, _), = messages(wm, WM_MOUSEMOVE)
    assert wparam == MK_LBUTTON


def test_mousemove_with_no_button_held_has_no_mk_bits():
    wm = make_wm()
    wm._handle_sdl_event(motion_event(state=0))
    (_, _, wparam, _), = messages(wm, WM_MOUSEMOVE)
    assert wparam == 0


def test_mousemove_after_release_has_no_mk_bits():
    wm = make_wm()
    wm._handle_sdl_event(button_event(SDL_MOUSEBUTTONDOWN, SDL_BUTTON_LEFT))
    wm._handle_sdl_event(button_event(SDL_MOUSEBUTTONUP, SDL_BUTTON_LEFT))
    wm._handle_sdl_event(motion_event(state=0))
    (_, _, wparam, _), = messages(wm, WM_MOUSEMOVE)
    assert wparam == 0


def test_focus_loss_clears_held_buttons():
    # An up delivered to another window must not leave a stuck MK_LBUTTON.
    wm = make_wm()
    wm._handle_sdl_event(button_event(SDL_MOUSEBUTTONDOWN, SDL_BUTTON_LEFT))
    lost = SDL_Event()
    lost.type = SDL_WINDOWEVENT
    lost.window.event = SDL_WINDOWEVENT_FOCUS_LOST
    lost.window.windowID = WINDOW_ID
    wm._handle_sdl_event(lost)
    wm._handle_sdl_event(motion_event(state=0))
    (_, _, wparam, _), = messages(wm, WM_MOUSEMOVE)
    assert wparam == 0
