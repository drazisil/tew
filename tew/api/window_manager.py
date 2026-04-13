"""SDL2-backed Win32 window and dialog manager.

Maintains a registry of window classes and window entries, and maps
Win32 window management calls onto real SDL2 windows.  One SDL2 window
is created per top-level WS_VISIBLE Win32 window.

Message queue semantics mirror Win32 PostMessage / PeekMessage at a
per-process level (sufficient for MCO's single-threaded UI loop).
"""

from __future__ import annotations

import ctypes
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tew.api.pe_resources import PEResources

from sdl2 import (
    SDL_Init, SDL_Quit, SDL_INIT_VIDEO, SDL_INIT_EVENTS,
    SDL_CreateWindow, SDL_DestroyWindow,
    SDL_CreateRenderer, SDL_DestroyRenderer,
    SDL_WINDOW_SHOWN, SDL_WINDOW_RESIZABLE,
    SDL_RENDERER_ACCELERATED, SDL_RENDERER_PRESENTVSYNC,
    SDL_PollEvent, SDL_Event,
    SDL_QUIT,
    SDL_KEYDOWN, SDL_KEYUP,
    SDL_TEXTINPUT,
    SDL_MOUSEBUTTONDOWN, SDL_MOUSEBUTTONUP,
    SDL_WINDOWEVENT,
    SDL_WINDOWEVENT_CLOSE,
    SDL_GetWindowID,
    SDL_RaiseWindow,
    SDLK_BACKSPACE, SDLK_RETURN, SDLK_KP_ENTER, SDLK_TAB,
    SDLK_ESCAPE, SDLK_DELETE,
    SDL_BUTTON_LEFT,
)

from tew.logger import logger
from tew.api.pe_resources import DialogTemplate


# ── Win32 message constants ────────────────────────────────────────────────────

WM_CREATE       = 0x0001
WM_DESTROY      = 0x0002
WM_SETTEXT      = 0x000C
WM_GETTEXT      = 0x000D
WM_CLOSE        = 0x0010
WM_COMMAND      = 0x0111
WM_INITDIALOG   = 0x0110
BM_GETCHECK     = 0x00F0
BM_SETCHECK     = 0x00F1
EM_GETTEXT      = 0x000D   # same value as WM_GETTEXT; edit controls respond to both
EM_LIMITTEXT    = 0x00C5

# Win32 style flags
WS_VISIBLE      = 0x10000000
WS_CHILD        = 0x40000000
WS_POPUP        = 0x80000000
DS_SETFONT      = 0x0040

# Button state constants
BST_UNCHECKED   = 0
BST_CHECKED     = 1

# Dialog unit base dimensions for MS Sans Serif 8pt at 96 DPI
DU_BASE_X = 6
DU_BASE_Y = 13

# Height of the simulated Win32 title bar drawn inside the SDL client area.
# Controls are rendered TITLE_BAR_H pixels below their dialog-unit y coordinate;
# hit testing must apply the same offset.
TITLE_BAR_H = 18


def du_to_px_x(du: int) -> int:
    return (du * DU_BASE_X + 2) // 4


def du_to_px_y(du: int) -> int:
    return (du * DU_BASE_Y + 4) // 8


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class WindowClass:
    name: str
    atom: int
    wnd_proc_addr: int    # emulator address of WNDPROC
    background: int       # brush handle (informational; rendering uses fixed palette)
    icon: int
    cursor: int


@dataclass
class WindowEntry:
    hwnd: int
    class_name: str
    title: str
    style: int
    ex_style: int
    x: int
    y: int
    cx: int
    cy: int
    parent_hwnd: int                           # 0 = top-level
    children: dict[int, int] = field(default_factory=dict)  # ctrl_id → child hwnd
    wnd_proc_addr: int = 0                     # WNDPROC addr in emulator, if registered
    dlg_proc_addr: int = 0                     # DLGPROC addr, non-zero means this is a dialog
    dlg_result: int = 0
    dlg_done: bool = False
    check_state: int = 0                       # for BUTTON checkboxes (BST_UNCHECKED / BST_CHECKED)
    sdl_window: Optional[object] = None        # SDL_Window* for top-level windows
    sdl_renderer: Optional[object] = None      # SDL_Renderer* for top-level windows
    bitmap_texture: Optional[object] = None    # SDL_Texture* for SS_BITMAP STATIC controls


# ── Window manager ─────────────────────────────────────────────────────────────

class WindowManager:
    """Manages Win32 window classes and window instances backed by SDL2."""

    def __init__(self) -> None:
        self._initialized: bool = False
        self._classes: dict[int, WindowClass] = {}       # atom → class
        self._class_by_name: dict[str, int] = {}          # lower name → atom
        self._windows: dict[int, WindowEntry] = {}        # hwnd → entry
        self._next_hwnd: int = 0x1000
        self._next_atom: int = 0xC001
        # Message queue: (hwnd, msg, wparam, lparam)
        self._message_queue: deque[tuple[int, int, int, int]] = deque()
        # Currently focused edit control hwnd (receives keyboard input)
        self._focused_hwnd: int = 0
        # SDL window ID → top-level hwnd
        self._sdl_window_id_to_hwnd: dict[int, int] = {}
        # PE resources for loading bitmap textures (set by run_exe.py after load)
        self._pe_resources: Optional["PEResources"] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def initialize(self) -> bool:
        """Initialize SDL2 video and event subsystems.  Must be called before
        creating any windows.  Safe to call multiple times."""
        if self._initialized:
            return True
        rc = SDL_Init(SDL_INIT_VIDEO | SDL_INIT_EVENTS)
        if rc != 0:
            logger.error("window", f"[WindowManager] SDL_Init failed: {rc}")
            return False
        self._initialized = True
        logger.info("window", "[WindowManager] SDL2 initialized")
        return True

    def shutdown(self) -> None:
        """Destroy all SDL2 resources and shut down SDL2."""
        for entry in list(self._windows.values()):
            if entry.bitmap_texture is not None:
                from sdl2 import SDL_DestroyTexture
                SDL_DestroyTexture(entry.bitmap_texture)
            if entry.sdl_renderer is not None:
                SDL_DestroyRenderer(entry.sdl_renderer)
            if entry.sdl_window is not None:
                SDL_DestroyWindow(entry.sdl_window)
        self._windows.clear()
        self._classes.clear()
        self._class_by_name.clear()
        self._sdl_window_id_to_hwnd.clear()
        SDL_Quit()
        self._initialized = False
        logger.info("window", "[WindowManager] SDL2 shut down")

    def set_pe_resources(self, pe_resources: "PEResources") -> None:
        """Provide PE resources so bitmap STATIC controls can load their textures."""
        self._pe_resources = pe_resources

    # ── Class registration ────────────────────────────────────────────────────

    def register_class(
        self,
        name: str,
        wnd_proc_addr: int,
        background: int = 0,
        icon: int = 0,
        cursor: int = 0,
    ) -> int:
        """Register a window class.  Returns the atom (>= 0xC001) on success,
        or 0 on failure (e.g. name already registered)."""
        key = name.lower()
        if key in self._class_by_name:
            logger.warn("window", f"[WindowManager] Class '{name}' already registered — returning existing atom")
            return self._class_by_name[key]

        atom = self._next_atom
        self._next_atom += 1

        wc = WindowClass(
            name=name,
            atom=atom,
            wnd_proc_addr=wnd_proc_addr,
            background=background,
            icon=icon,
            cursor=cursor,
        )
        self._classes[atom] = wc
        self._class_by_name[key] = atom
        logger.debug("window", f"[WindowManager] RegisterClass '{name}' -> atom 0x{atom:04x}")
        return atom

    def get_class_atom(self, name: str) -> int:
        """Return the atom for a registered class name, or 0 if not registered."""
        return self._class_by_name.get(name.lower(), 0)

    # ── Window creation ───────────────────────────────────────────────────────

    def create_window(
        self,
        class_name: str,
        title: str,
        style: int,
        ex_style: int,
        x: int,
        y: int,
        cx: int,
        cy: int,
        parent_hwnd: int,
        wnd_proc_addr: int,
    ) -> int:
        """Create a window entry.  For WS_VISIBLE top-level windows (no parent)
        an SDL2 window is created.  Returns a non-zero HWND or 0 on failure."""
        if not self._initialized:
            logger.error("window", "[WindowManager] create_window called before initialize()")
            return 0

        hwnd = self._alloc_hwnd()
        entry = WindowEntry(
            hwnd=hwnd,
            class_name=class_name,
            title=title,
            style=style,
            ex_style=ex_style,
            x=x, y=y, cx=cx, cy=cy,
            parent_hwnd=parent_hwnd,
            wnd_proc_addr=wnd_proc_addr,
        )

        is_top_level = (parent_hwnd == 0)
        is_visible = bool(style & WS_VISIBLE)

        if is_top_level and is_visible:
            px_w = du_to_px_x(cx) if cx > 0 else 640
            px_h = du_to_px_y(cy) if cy > 0 else 480
            sdl_win = SDL_CreateWindow(
                title.encode("utf-8"),
                x if x >= 0 else 100,
                y if y >= 0 else 100,
                px_w,
                px_h,
                SDL_WINDOW_SHOWN,
            )
            if not sdl_win:
                logger.error("window", f"[WindowManager] SDL_CreateWindow failed for '{title}'")
                return 0
            sdl_rend = SDL_CreateRenderer(sdl_win, -1, SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC)
            if not sdl_rend:
                # Fall back to software renderer
                from sdl2 import SDL_RENDERER_SOFTWARE
                sdl_rend = SDL_CreateRenderer(sdl_win, -1, SDL_RENDERER_SOFTWARE)
                if not sdl_rend:
                    logger.error("window", "[WindowManager] SDL_CreateRenderer failed")
                    SDL_DestroyWindow(sdl_win)
                    return 0
            entry.sdl_window = sdl_win
            entry.sdl_renderer = sdl_rend
            win_id = SDL_GetWindowID(sdl_win)
            self._sdl_window_id_to_hwnd[win_id] = hwnd
            SDL_RaiseWindow(sdl_win)
            logger.info("window", f"[WindowManager] Created SDL window '{title}' ({px_w}x{px_h}) hwnd=0x{hwnd:x}")
        else:
            logger.debug("window",
                f"[WindowManager] CreateWindow '{title}' class='{class_name}' "
                f"hwnd=0x{hwnd:x} (no SDL window)"
            )

        # Register as child of parent
        if parent_hwnd != 0:
            parent = self._windows.get(parent_hwnd)
            if parent is not None:
                # ctrl_id is in the low 16 bits of the HMENU parameter — callers
                # that need to register by ID should call register_child() separately.
                pass

        self._windows[hwnd] = entry
        self._message_queue.append((hwnd, WM_CREATE, 0, 0))
        return hwnd

    def register_child(self, parent_hwnd: int, ctrl_id: int, child_hwnd: int) -> None:
        """Register a child window under a parent with a given control ID."""
        parent = self._windows.get(parent_hwnd)
        if parent is None:
            logger.warn("window", f"[WindowManager] register_child: parent hwnd=0x{parent_hwnd:x} not found")
            return
        parent.children[ctrl_id] = child_hwnd

    # ── Dialog creation ───────────────────────────────────────────────────────

    def create_dialog(
        self,
        template: DialogTemplate,
        parent_hwnd: int,
        dlg_proc_addr: int,
        init_param: int,
    ) -> int:
        """Create a dialog and all its child controls from a DialogTemplate.
        Returns the HWND of the dialog window (>= 0x1000) or 0 on failure."""
        if not self._initialized:
            logger.error("dialog", "[WindowManager] create_dialog called before initialize()")
            return 0

        # Add TITLE_BAR_H so controls near the dialog's bottom edge aren't clipped.
        # The title bar is drawn inside the SDL client area, consuming that space.
        px_w = du_to_px_x(template.cx)
        px_h = du_to_px_y(template.cy) + TITLE_BAR_H

        # Dialog style: WS_POPUP | WS_VISIBLE plus what the template says
        dlg_style = template.style | WS_POPUP | WS_VISIBLE

        hwnd = self._alloc_hwnd()
        sdl_win = SDL_CreateWindow(
            template.title.encode("utf-8"),
            100,   # SDL_WINDOWPOS_UNDEFINED would be 0x1FFF0000 — use a fixed offset for now
            100,
            px_w,
            px_h,
            SDL_WINDOW_SHOWN,
        )
        if not sdl_win:
            logger.error("dialog", f"[WindowManager] SDL_CreateWindow failed for dialog '{template.title}'")
            return 0

        sdl_rend = SDL_CreateRenderer(sdl_win, -1, SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC)
        if not sdl_rend:
            from sdl2 import SDL_RENDERER_SOFTWARE
            sdl_rend = SDL_CreateRenderer(sdl_win, -1, SDL_RENDERER_SOFTWARE)
            if not sdl_rend:
                logger.error("dialog", "[WindowManager] SDL_CreateRenderer failed for dialog")
                SDL_DestroyWindow(sdl_win)
                return 0

        entry = WindowEntry(
            hwnd=hwnd,
            class_name="#32770",    # Win32 predefined dialog class
            title=template.title,
            style=dlg_style,
            ex_style=0,
            x=template.x, y=template.y,
            cx=template.cx, cy=template.cy,
            parent_hwnd=parent_hwnd,
            dlg_proc_addr=dlg_proc_addr,
            sdl_window=sdl_win,
            sdl_renderer=sdl_rend,
        )
        self._windows[hwnd] = entry

        win_id = SDL_GetWindowID(sdl_win)
        self._sdl_window_id_to_hwnd[win_id] = hwnd
        SDL_RaiseWindow(sdl_win)
        logger.info("dialog",
            f"[WindowManager] Created dialog '{template.title}' "
            f"hwnd=0x{hwnd:x} ({px_w}x{px_h})"
        )

        # Create child controls
        for ctrl in template.controls:
            child_hwnd = self._alloc_hwnd()
            child = WindowEntry(
                hwnd=child_hwnd,
                class_name=ctrl.class_name,
                title=ctrl.title,
                style=ctrl.style,
                ex_style=ctrl.ex_style,
                x=ctrl.x, y=ctrl.y,
                cx=ctrl.cx, cy=ctrl.cy,
                parent_hwnd=hwnd,
            )
            self._windows[child_hwnd] = child
            if ctrl.id != 0xFFFF:
                entry.children[ctrl.id] = child_hwnd
                logger.debug("dialog",
                    f"[WindowManager]   ctrl id=0x{ctrl.id:04x} '{ctrl.class_name}' "
                    f"'{ctrl.title}' hwnd=0x{child_hwnd:x}"
                )
            else:
                logger.debug("dialog",
                    f"[WindowManager]   ctrl id=0xFFFF '{ctrl.class_name}' "
                    f"'{ctrl.title}' hwnd=0x{child_hwnd:x}"
                )

            # Pre-load bitmap texture for SS_BITMAP STATIC controls.
            # The title is "#N" (from _read_var_field 0xFFFF ordinal encoding)
            # when the control is supposed to display a bitmap resource.
            if (
                ctrl.class_name == "STATIC"
                and ctrl.title.startswith("#")
                and self._pe_resources is not None
                and entry.sdl_renderer is not None
            ):
                try:
                    bitmap_id = int(ctrl.title[1:])
                    from tew.api.bitmap_loader import load_bitmap_texture
                    child.bitmap_texture = load_bitmap_texture(
                        entry.sdl_renderer, bitmap_id, self._pe_resources
                    )
                except ValueError:
                    pass   # title like "#abc" — not a valid bitmap ID, skip

        # Auto-focus the first EDIT control
        for ctrl in template.controls:
            if ctrl.class_name == "EDIT" and ctrl.id != 0xFFFF:
                child_hwnd = entry.children.get(ctrl.id, 0)
                if child_hwnd:
                    self._focused_hwnd = child_hwnd
                    break

        # Post WM_INITDIALOG so the emulator's DLGPROC gets a chance to initialize
        self._message_queue.append((hwnd, WM_INITDIALOG, 0, init_param))
        return hwnd

    # ── Window destruction ────────────────────────────────────────────────────

    def destroy_window(self, hwnd: int) -> bool:
        """Destroy a window and all its children.  Returns True if the window existed."""
        entry = self._windows.get(hwnd)
        if entry is None:
            logger.warn("window", f"[WindowManager] destroy_window: unknown hwnd=0x{hwnd:x}")
            return False

        # Destroy children first
        for _ctrl_id, child_hwnd in list(entry.children.items()):
            self.destroy_window(child_hwnd)

        # Tear down SDL2 resources
        if entry.bitmap_texture is not None:
            from sdl2 import SDL_DestroyTexture
            SDL_DestroyTexture(entry.bitmap_texture)
        if entry.sdl_renderer is not None:
            SDL_DestroyRenderer(entry.sdl_renderer)
        if entry.sdl_window is not None:
            win_id = SDL_GetWindowID(entry.sdl_window)
            self._sdl_window_id_to_hwnd.pop(win_id, None)
            SDL_DestroyWindow(entry.sdl_window)

        # Remove from focused hwnd if applicable
        if self._focused_hwnd == hwnd:
            self._focused_hwnd = 0

        del self._windows[hwnd]
        self._message_queue.append((hwnd, WM_DESTROY, 0, 0))
        logger.debug("window", f"[WindowManager] Destroyed hwnd=0x{hwnd:x}")
        return True

    # ── Control accessors ─────────────────────────────────────────────────────

    def get_dlg_item(self, dlg_hwnd: int, ctrl_id: int) -> int:
        """Return the child HWND registered under ctrl_id, or 0 if not found."""
        entry = self._windows.get(dlg_hwnd)
        if entry is None:
            return 0
        return entry.children.get(ctrl_id, 0)

    def get_window_text(self, hwnd: int) -> str:
        """Return the title/text of a window or control."""
        entry = self._windows.get(hwnd)
        if entry is None:
            logger.warn("window", f"[WindowManager] get_window_text: unknown hwnd=0x{hwnd:x}")
            return ""
        return entry.title

    def set_window_text(self, hwnd: int, text: str) -> bool:
        """Set the title/text of a window or control.  Returns False if unknown."""
        entry = self._windows.get(hwnd)
        if entry is None:
            logger.warn("window", f"[WindowManager] set_window_text: unknown hwnd=0x{hwnd:x}")
            return False
        entry.title = text
        return True

    def get_check_state(self, hwnd: int) -> int:
        """Return BST_UNCHECKED (0) or BST_CHECKED (1) for a checkbox button."""
        entry = self._windows.get(hwnd)
        if entry is None:
            logger.warn("window", f"[WindowManager] get_check_state: unknown hwnd=0x{hwnd:x}")
            return BST_UNCHECKED
        return entry.check_state

    def set_check_state(self, hwnd: int, state: int) -> None:
        """Set the check state of a checkbox button."""
        entry = self._windows.get(hwnd)
        if entry is None:
            logger.warn("window", f"[WindowManager] set_check_state: unknown hwnd=0x{hwnd:x}")
            return
        entry.check_state = state

    # ── Message queue ─────────────────────────────────────────────────────────

    def post_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> bool:
        """Append a message to the queue.  Returns False if hwnd is unknown."""
        if hwnd not in self._windows:
            logger.warn("window", f"[WindowManager] post_message to unknown hwnd=0x{hwnd:x}")
            return False
        self._message_queue.append((hwnd, msg, wparam, lparam))
        return True

    def peek_message(self) -> tuple[int, int, int, int] | None:
        """Return and remove the next (hwnd, msg, wparam, lparam) or None."""
        if self._message_queue:
            return self._message_queue.popleft()
        return None

    # ── Dialog control ────────────────────────────────────────────────────────

    def end_dialog(self, hwnd: int, result: int) -> bool:
        """Signal dialog completion.  Returns False if hwnd is unknown."""
        entry = self._windows.get(hwnd)
        if entry is None:
            logger.warn("dialog", f"[WindowManager] end_dialog: unknown hwnd=0x{hwnd:x}")
            return False
        entry.dlg_result = result
        entry.dlg_done = True
        logger.debug("dialog", f"[WindowManager] EndDialog hwnd=0x{hwnd:x} result={result}")
        return True

    def is_window(self, hwnd: int) -> bool:
        return hwnd in self._windows

    def get_window(self, hwnd: int) -> WindowEntry | None:
        return self._windows.get(hwnd)

    # ── SDL2 event pump ───────────────────────────────────────────────────────

    def pump_sdl_events(self) -> bool:
        """Poll SDL2 events, convert to Win32 messages, post to queue.
        Returns False if SDL_QUIT was received (caller should exit)."""
        event = SDL_Event()
        while SDL_PollEvent(ctypes.byref(event)) != 0:
            if event.type == SDL_QUIT:
                logger.info("window", "[WindowManager] SDL_QUIT received")
                return False
            self._handle_sdl_event(event)
        return True

    def _handle_sdl_event(self, event: SDL_Event) -> None:
        """Convert a single SDL event to Win32 message(s) and post them."""
        etype = event.type

        if etype == SDL_WINDOWEVENT:
            we = event.window
            if we.event == SDL_WINDOWEVENT_CLOSE:
                hwnd = self._sdl_window_id_to_hwnd.get(we.windowID, 0)
                if hwnd:
                    self._message_queue.append((hwnd, WM_CLOSE, 0, 0))

        elif etype == SDL_KEYDOWN:
            key = event.key
            hwnd = self._focused_hwnd
            if hwnd == 0:
                return
            sym = key.keysym.sym

            if sym == SDLK_BACKSPACE:
                entry = self._windows.get(hwnd)
                if entry is not None and len(entry.title) > 0:
                    entry.title = entry.title[:-1]

            elif sym in (SDLK_RETURN, SDLK_KP_ENTER):
                # Find parent dialog and simulate pressing the default button (id=1)
                entry = self._windows.get(hwnd)
                if entry is not None:
                    parent = self._windows.get(entry.parent_hwnd)
                    if parent is not None and parent.dlg_proc_addr != 0:
                        btn_hwnd = parent.children.get(1, 0)
                        if btn_hwnd:
                            self._message_queue.append((entry.parent_hwnd, WM_COMMAND, 1, btn_hwnd))

            elif sym == SDLK_ESCAPE:
                entry = self._windows.get(hwnd)
                if entry is not None:
                    parent = self._windows.get(entry.parent_hwnd)
                    if parent is not None and parent.dlg_proc_addr != 0:
                        btn_hwnd = parent.children.get(2, 0)
                        if btn_hwnd:
                            self._message_queue.append((entry.parent_hwnd, WM_COMMAND, 2, btn_hwnd))

            elif sym == SDLK_TAB:
                self._cycle_focus(hwnd)

            elif sym == SDLK_DELETE:
                entry = self._windows.get(hwnd)
                if entry is not None:
                    entry.title = ""

        elif etype == SDL_TEXTINPUT:
            hwnd = self._focused_hwnd
            if hwnd == 0:
                return
            entry = self._windows.get(hwnd)
            if entry is None:
                return
            # SDL provides UTF-8 text
            try:
                text = bytes(event.text.text).rstrip(b"\x00").decode("utf-8")
            except UnicodeDecodeError:
                return

            # Mask input for password fields (ES_PASSWORD = 0x20)
            if entry.style & 0x20:
                # Store actual text; display masking is done by the renderer
                entry.title += text
            else:
                entry.title += text

        elif etype == SDL_MOUSEBUTTONDOWN:
            btn = event.button
            if btn.button != SDL_BUTTON_LEFT:
                return
            win_hwnd = self._sdl_window_id_to_hwnd.get(btn.windowID, 0)
            if win_hwnd == 0:
                return
            self._handle_mouse_click(win_hwnd, btn.x, btn.y)

    def _handle_mouse_click(self, dlg_hwnd: int, px: int, py: int) -> None:
        """Determine which child control was clicked and post appropriate message."""
        dlg_entry = self._windows.get(dlg_hwnd)
        if dlg_entry is None:
            return

        logger.debug("dialog",
            f"[WindowManager] click ({px},{py}) on dlg=0x{dlg_hwnd:x} "
            f"({len(dlg_entry.children)} children)"
        )

        for ctrl_id, child_hwnd in dlg_entry.children.items():
            child = self._windows.get(child_hwnd)
            if child is None:
                continue

            # Convert dialog-unit coordinates to pixels for hit testing.
            # Add TITLE_BAR_H because render_dialog draws controls below the
            # simulated title bar, and SDL mouse coordinates start at y=0 in
            # the client area (i.e. at the top of the title bar we drew).
            cx0 = du_to_px_x(child.x)
            cy0 = du_to_px_y(child.y) + TITLE_BAR_H
            cx1 = cx0 + du_to_px_x(child.cx)
            cy1 = cy0 + du_to_px_y(child.cy)

            logger.debug("dialog",
                f"[WindowManager]   ctrl 0x{ctrl_id:04x} '{child.class_name}' "
                f"box=({cx0},{cy0})-({cx1},{cy1})"
            )

            if cx0 <= px < cx1 and cy0 <= py < cy1:
                class_lower = child.class_name.lower()
                logger.debug("dialog",
                    f"[WindowManager]   HIT ctrl 0x{ctrl_id:04x} '{child.class_name}' '{child.title}'"
                )
                if class_lower == "button":
                    # Toggle checkbox; for push buttons post WM_COMMAND
                    if child.style & 0x0F in (0x02, 0x03):  # BS_CHECKBOX, BS_AUTOCHECKBOX
                        child.check_state ^= 1
                    else:
                        self._message_queue.append((dlg_hwnd, WM_COMMAND, ctrl_id, child_hwnd))
                elif class_lower == "edit":
                    self._focused_hwnd = child_hwnd
                    logger.debug("dialog", f"[WindowManager] Focus -> edit hwnd=0x{child_hwnd:x}")
                return

        logger.debug("dialog", f"[WindowManager]   no hit for click ({px},{py})")

    def _cycle_focus(self, current_hwnd: int) -> None:
        """Move keyboard focus to the next EDIT control in the parent dialog."""
        current = self._windows.get(current_hwnd)
        if current is None:
            return
        parent = self._windows.get(current.parent_hwnd)
        if parent is None:
            return

        edit_hwnds = [
            child_hwnd
            for child_hwnd in parent.children.values()
            if (
                child_hwnd in self._windows
                and self._windows[child_hwnd].class_name.lower() == "edit"
            )
        ]
        if len(edit_hwnds) <= 1:
            return

        try:
            idx = edit_hwnds.index(current_hwnd)
            self._focused_hwnd = edit_hwnds[(idx + 1) % len(edit_hwnds)]
        except ValueError:
            self._focused_hwnd = edit_hwnds[0]

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _alloc_hwnd(self) -> int:
        hwnd = self._next_hwnd
        self._next_hwnd += 4   # HWNDs are multiples of 4 in practice
        return hwnd
