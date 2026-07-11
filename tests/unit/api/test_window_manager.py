"""
Tests for WindowManager's programmatic dialog control injection
(click_control, set_dialog_step_hook) -- synthetic (non-SDL) equivalents
of a real mouse click, for scripted/headless dialog interaction.

No SDL needed: these construct a WindowManager() directly and populate
_windows by hand, bypassing initialize()/create_dialog() entirely -- same
spirit as test_pe_resources.py's hand-verified dialog-114 layout.
"""

from tew.api.window_manager import WindowManager, WindowEntry, WM_COMMAND

DLG_HWND = 0x1000
BUTTON_HWND = 0x1001
CHECKBOX_HWND = 0x1002
EDIT_HWND = 0x1003

BS_AUTOCHECKBOX = 0x03


def make_dialog() -> WindowManager:
    wm = WindowManager()
    dlg = WindowEntry(
        hwnd=DLG_HWND, class_name="#32770", title="Test Dialog",
        style=0, ex_style=0, x=0, y=0, cx=200, cy=100, parent_hwnd=0,
    )
    button = WindowEntry(
        hwnd=BUTTON_HWND, class_name="BUTTON", title="Continue",
        style=0, ex_style=0, x=10, y=10, cx=50, cy=14, parent_hwnd=DLG_HWND,
    )
    checkbox = WindowEntry(
        hwnd=CHECKBOX_HWND, class_name="BUTTON", title="Remember",
        style=BS_AUTOCHECKBOX, ex_style=0, x=10, y=30, cx=50, cy=14, parent_hwnd=DLG_HWND,
    )
    dlg.children[0x0001] = BUTTON_HWND
    dlg.children[0x0414] = CHECKBOX_HWND
    dlg.children_list = [(0x0001, BUTTON_HWND), (0x0414, CHECKBOX_HWND)]
    wm._windows[DLG_HWND] = dlg
    wm._windows[BUTTON_HWND] = button
    wm._windows[CHECKBOX_HWND] = checkbox
    return wm


def test_click_control_posts_wm_command_for_push_button():
    wm = make_dialog()
    assert wm.click_control(DLG_HWND, 0x0001) is True
    assert wm.peek_message() == (DLG_HWND, WM_COMMAND, 0x0001, BUTTON_HWND)


def test_click_control_toggles_checkbox_in_place():
    wm = make_dialog()
    assert wm._windows[CHECKBOX_HWND].check_state == 0
    assert wm.click_control(DLG_HWND, 0x0414) is True
    assert wm._windows[CHECKBOX_HWND].check_state == 1
    assert wm.peek_message() is None  # checkbox toggle doesn't post WM_COMMAND


def test_click_control_unknown_dialog_returns_false():
    wm = WindowManager()
    assert wm.click_control(0xDEAD, 0x0001) is False


def test_click_control_unknown_ctrl_id_returns_false():
    wm = make_dialog()
    assert wm.click_control(DLG_HWND, 0x9999) is False


def test_click_control_non_button_returns_false():
    wm = make_dialog()
    edit = WindowEntry(
        hwnd=EDIT_HWND, class_name="EDIT", title="",
        style=0, ex_style=0, x=10, y=50, cx=50, cy=14, parent_hwnd=DLG_HWND,
    )
    wm._windows[EDIT_HWND] = edit
    wm._windows[DLG_HWND].children[0x0412] = EDIT_HWND
    assert wm.click_control(DLG_HWND, 0x0412) is False


def test_dialog_step_hook_fires_exactly_once():
    wm = make_dialog()
    calls: list[int] = []

    def hook(w: WindowManager, dlg_hwnd: int) -> None:
        calls.append(dlg_hwnd)

    wm.set_dialog_step_hook(hook)

    # Simulate exactly what _DialogBoxParamA's modal loop does each
    # iteration: consume-then-invoke.
    for _ in range(2):
        h, wm._dialog_step_hook = wm._dialog_step_hook, None
        if h is not None:
            h(wm, DLG_HWND)

    assert calls == [DLG_HWND]


def test_dialog_step_hook_can_rearm_itself():
    """A hook that isn't ready to act (wrong dialog) re-registers via
    set_dialog_step_hook, giving simple wait-then-act semantics."""
    wm = make_dialog()
    calls: list[int] = []

    def hook(w: WindowManager, dlg_hwnd: int) -> None:
        if dlg_hwnd != DLG_HWND:
            w.set_dialog_step_hook(hook)
            return
        calls.append(dlg_hwnd)

    wm.set_dialog_step_hook(hook)

    # First iteration: wrong dialog, hook re-arms itself.
    h, wm._dialog_step_hook = wm._dialog_step_hook, None
    h(wm, 0x9999)
    assert calls == []
    assert wm._dialog_step_hook is hook

    # Second iteration: right dialog, hook fires for real and does not re-arm.
    h, wm._dialog_step_hook = wm._dialog_step_hook, None
    h(wm, DLG_HWND)
    assert calls == [DLG_HWND]
    assert wm._dialog_step_hook is None


# ── Real dialog-114 layout (login dialog) ────────────────────────────────────
# IDs verified against the real MCity_d.exe resource in
# tests/unit/pe/test_pe_resources.py: Continue button id=0x0001.

def test_click_control_against_real_login_dialog_continue_id():
    wm = WindowManager()
    dlg = WindowEntry(
        hwnd=DLG_HWND, class_name="#32770", title="Motor City Online Login",
        style=0, ex_style=0, x=0, y=0, cx=372, cy=346, parent_hwnd=0,
    )
    continue_btn = WindowEntry(
        hwnd=BUTTON_HWND, class_name="BUTTON", title="Continue",
        style=0, ex_style=0, x=0, y=0, cx=50, cy=14, parent_hwnd=DLG_HWND,
    )
    dlg.children[0x0001] = BUTTON_HWND
    dlg.children_list = [(0x0001, BUTTON_HWND)]
    wm._windows[DLG_HWND] = dlg
    wm._windows[BUTTON_HWND] = continue_btn

    assert wm.click_control(DLG_HWND, 0x0001) is True
    assert wm.peek_message() == (DLG_HWND, WM_COMMAND, 0x0001, BUTTON_HWND)
