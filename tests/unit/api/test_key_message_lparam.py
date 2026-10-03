"""WM_KEYDOWN/WM_KEYUP lParam must carry the PC scancode.

The game's window procedure files each key press under the scancode in
lParam bits 16-23 (`_kstate[(lParam >> 16) & 0x7f]`), and the in-race controls
(Input_keyPressed) read that table. tew used to post lParam=0, so every key
landed under scancode 0 and the arrow keys never reached the car.
"""
import sdl2

from tew.api.dinput_handlers import key_message_lparam


def scancode(lparam: int) -> int:
    return (lparam >> 16) & 0xFF


def test_arrow_keys_carry_set1_scancodes_with_extended_flag():
    for sdl, scan in ((sdl2.SDL_SCANCODE_UP, 0x48), (sdl2.SDL_SCANCODE_LEFT, 0x4B),
                      (sdl2.SDL_SCANCODE_RIGHT, 0x4D), (sdl2.SDL_SCANCODE_DOWN, 0x50)):
        lp = key_message_lparam(sdl, is_up=False)
        assert scancode(lp) == scan
        assert lp & (1 << 24), "arrows are extended keys"


def test_escape_is_not_extended():
    lp = key_message_lparam(sdl2.SDL_SCANCODE_ESCAPE, is_up=False)
    assert scancode(lp) == 0x01
    assert not lp & (1 << 24)


def test_key_down_has_repeat_count_one_and_no_transition_bits():
    lp = key_message_lparam(sdl2.SDL_SCANCODE_A, is_up=False)
    assert lp & 0xFFFF == 1
    assert not lp & (1 << 30)
    assert not lp & (1 << 31)


def test_auto_repeat_sets_previous_state_only():
    lp = key_message_lparam(sdl2.SDL_SCANCODE_A, is_up=False, is_repeat=True)
    assert lp & (1 << 30)
    assert not lp & (1 << 31)


def test_key_up_sets_previous_state_and_transition():
    lp = key_message_lparam(sdl2.SDL_SCANCODE_A, is_up=True)
    assert scancode(lp) == 0x1E
    assert lp & (1 << 30)
    assert lp & (1 << 31)


def test_space_and_letters_match_game_default_bindings():
    # Handbrake=Space, Shift up=A, Shift down=Z, Nitrous=N, Horn=H, Lights=L
    expected = {sdl2.SDL_SCANCODE_SPACE: 0x39, sdl2.SDL_SCANCODE_A: 0x1E, sdl2.SDL_SCANCODE_Z: 0x2C,
                sdl2.SDL_SCANCODE_N: 0x31, sdl2.SDL_SCANCODE_H: 0x23, sdl2.SDL_SCANCODE_L: 0x26}
    for sdl, scan in expected.items():
        assert scancode(key_message_lparam(sdl, is_up=False)) == scan


# ── through the real SDL event handler ─────────────────────────────────────

from sdl2 import SDL_KEYDOWN, SDL_KEYUP, SDL_Event

from tew.api.window_manager import WM_KEYDOWN, WM_KEYUP, WindowEntry, WindowManager

HWND = 0x1034


def _wm() -> WindowManager:
    wm = WindowManager()
    wm._windows[HWND] = WindowEntry(
        hwnd=HWND, class_name="MCity", title="Motor City Online",
        style=0, ex_style=0, x=0, y=0, cx=640, cy=480, parent_hwnd=0,
    )
    wm._focused_hwnd = HWND
    return wm


def _key_event(etype: int, sdl_scancode: int, sym: int, repeat: int = 0) -> SDL_Event:
    ev = SDL_Event()
    ev.type = etype
    ev.key.keysym.scancode = sdl_scancode
    ev.key.keysym.sym = sym
    ev.key.repeat = repeat
    return ev


def test_up_arrow_press_and_release_post_scancoded_messages():
    wm = _wm()
    wm._handle_sdl_event(_key_event(SDL_KEYDOWN, sdl2.SDL_SCANCODE_UP, sdl2.SDLK_UP))
    wm._handle_sdl_event(_key_event(SDL_KEYUP, sdl2.SDL_SCANCODE_UP, sdl2.SDLK_UP))
    down, up = list(wm._message_queue)[-2:]
    assert down[1] == WM_KEYDOWN and up[1] == WM_KEYUP
    assert scancode(down[3]) == 0x48 and scancode(up[3]) == 0x48
    assert not down[3] & (1 << 31) and up[3] & (1 << 31)


def test_keys_reach_the_sdl_window_when_no_control_has_focus():
    # The normal in-race state: no edit control focused, so _focused_hwnd == 0.
    wm = _wm()
    wm._focused_hwnd = 0
    wm._sdl_window_id_to_hwnd[7] = HWND
    for etype in (SDL_KEYDOWN, SDL_KEYUP):
        ev = _key_event(etype, sdl2.SDL_SCANCODE_UP, sdl2.SDLK_UP)
        ev.key.windowID = 7
        wm._handle_sdl_event(ev)
    down, up = list(wm._message_queue)[-2:]
    assert (down[0], down[1]) == (HWND, WM_KEYDOWN)
    assert (up[0], up[1]) == (HWND, WM_KEYUP)
    assert scancode(down[3]) == 0x48 and up[3] & (1 << 31)


def test_key_for_unknown_sdl_window_is_dropped_loudly(caplog):
    wm = _wm()
    wm._focused_hwnd = 0
    ev = _key_event(SDL_KEYDOWN, sdl2.SDL_SCANCODE_UP, sdl2.SDLK_UP)
    ev.key.windowID = 99
    before = len(wm._message_queue)
    wm._handle_sdl_event(ev)
    assert len(wm._message_queue) == before
