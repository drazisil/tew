"""Automation events from the game's GUI objects (MCity_d.exe).

`gui_exit` is emitted every time GUI::OnExit is entered: a dialog, message box
or screen is closing, or a child is being told its parent is. Everything is
read out of guest memory from inside a logpoint (which runs in cpu_run), so no
guest code is called; ClassName() is recovered statically instead.

Addresses and layouts come from Ghidra (project debug_clean):
  - GUI::OnExit(this, teGEXITCODE code, GUI *sender), __thiscall, vtable slot
    29. 0x00405a5b is an incremental-link thunk that JMPs to 0x00571270.
    Exit codes seen: 2 OK/accept, 3 cancel, 4 next screen, 6 quit, 7 error,
    8 timeout, 0xd "parent is exiting" (sent to every child).
  - GUI::ReadProperties (0x00af0800): GUI.mBounds is a GRect at +0x60
    (vftable, x, y, w, h), GUI.mText a GUIStr at +0x8c, GUI.mToolTip one at
    +0xf0. The instance name from the `.gui` header (`[<name>.<class>]`) is
    the GUIStr at +0x48.
  - ClassName() is vtable slot 1 (+4). On GUI itself that slot is the
    pure-virtual one.
"""

from __future__ import annotations

import ctypes
from typing import TYPE_CHECKING, Callable, NamedTuple

from tew.automation.events import AutomationEvent, AutomationEventEmitter
from tew.hardware.cpu_zig import ECX, ESP
from tew.logger import logger

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU

GUI_ONEXIT_THUNK = 0x00405A5B
GUI_VTABLE = 0x01204BD8            # ??_7GUI@@6B@

# Fields of a GUI object.
_NAME_OFFSET = 0x48                # GUIStr: instance name
_TEXT_OFFSET = 0x8C                # GUIStr: GUI.mText
_TIP_OFFSET = 0xF0                 # GUIStr: GUI.mToolTip
_BOUNDS_OFFSET = 0x60              # GRect: vftable, then x, y, w, h

# Classes the log subscriber skips because they dominate every teardown burst.
# A deny-list, not a whitelist: a class not named here is always logged, so a
# new one can't be missed in the noise. Add one class per line.
LOG_SKIP_CLASSES = frozenset({
    "GStaticImage",
    "GText",
    "GButton",
    "GImageBar",
    "MAudioFX",
    "GFrame",
})

Reader = Callable[[int, int], bytes]    # rd(addr, n); raises ValueError if addr is out of range


class Bounds(NamedTuple):
    """GUI.mBounds, rendered in the .gui file's own syntax: `[x, y] w, h`."""
    x: int
    y: int
    w: int
    h: int

    def __str__(self) -> str:
        return f"[{self.x}, {self.y}] {self.w}, {self.h}"


class GuestGuiReader:
    """Reads GUI objects through `rd`. `class_cache` (vtable -> class name) is
    shared across readers because a vtable's answer never changes."""

    def __init__(self, rd: Reader, class_cache: dict[int, str]) -> None:
        self._rd = rd
        self._class_cache = class_cache

    def u32(self, addr: int) -> int:
        return int.from_bytes(self._rd(addr, 4), "little")

    def _follow_thunks(self, addr: int) -> int:
        for _ in range(4):
            if self._rd(addr, 1) != b"\xe9":
                break
            rel = int.from_bytes(self._rd(addr + 1, 4), "little", signed=True)
            addr = (addr + 5 + rel) & 0xFFFFFFFF
        return addr

    def _string_literal(self, fn: int) -> str | None:
        """The C string loaded by a `mov eax, imm32` near the start of fn."""
        code = self._rd(fn, 0x80)
        for i in range(len(code) - 4):
            if code[i] != 0xB8:
                continue
            imm = int.from_bytes(code[i + 1:i + 5], "little")
            try:
                text, sep, _ = self._rd(imm, 65).partition(b"\0")
            except ValueError:
                continue    # e.g. the 0xcccccccc stack fill in a debug prologue
            if sep and 0 < len(text) <= 64 and all(0x20 <= b < 0x7F for b in text):
                return text.decode("ascii")
        return None

    def class_name(self, obj: int) -> str:
        """ClassName() without running it: it is `return Type();` and Type()
        returns a string literal, so follow the JMP thunks and take the
        `mov eax, <string>`. Anything unresolvable comes back as `?...`."""
        if obj == 0:
            return "NULL"
        try:
            vtable = self.u32(obj)
            if vtable in self._class_cache:
                return self._class_cache[vtable]
            fn = self.u32(vtable + 4)
            if fn == self.u32(GUI_VTABLE + 4):
                name = "GUI (purecall)"
            else:
                name = self._resolve_class_name(fn)
        except ValueError as e:
            return f"?{e}"
        self._class_cache[vtable] = name
        return name

    def _resolve_class_name(self, fn: int) -> str:
        body = self._follow_thunks(fn)
        name = self._string_literal(body)    # ClassName returning a literal itself
        code = self._rd(body, 0x80)
        for i in range(len(code) - 4):
            if name is not None:
                break
            if code[i] == 0xE8:
                rel = int.from_bytes(code[i + 1:i + 5], "little", signed=True)
                callee = self._follow_thunks((body + i + 5 + rel) & 0xFFFFFFFF)
                try:
                    name = self._string_literal(callee)
                except ValueError:
                    continue
        return name if name is not None else f"?no string found in ClassName@0x{fn:08x}"

    def _peek(self, ptr: int) -> str | None:
        try:
            raw = self._rd(ptr, 129).partition(b"\0")[0]
        except ValueError:
            return None
        return raw.decode("ascii") if raw and all(0x20 <= b < 0x7F for b in raw) else None

    def guistr(self, obj: int, off: int) -> str:
        """Text of the GUIStr at obj+off. Ghidra types only its vftable and the
        length at +12, so try the dwords at +4 and +8 as the buffer pointer and
        accept one whose text matches the length. If neither does, return the
        raw values (visible in the log) rather than guess."""
        base = obj + off
        length = self.u32(base + 12)
        slots = [(slot, self.u32(base + slot)) for slot in (4, 8)]
        for _, ptr in slots:
            text = self._peek(ptr)
            if text is not None and abs(len(text) - length) <= 1:
                return text
        if length == 0:
            return ""
        return f"?(len={length} " + " ".join(
            f"+{slot}=0x{ptr:08x}->{self._peek(ptr)!r}" for slot, ptr in slots) + ")"

    def bounds(self, obj: int) -> Bounds:
        x, y, w, h = (int.from_bytes(self._rd(obj + _BOUNDS_OFFSET + 4 + 4 * i, 4), "little", signed=True)
                      for i in range(4))
        return Bounds(x, y, w, h)

    def describe(self, obj: int) -> dict:
        """name, text, tip and bounds of a GUI object; if reading it fails the
        error text goes in every field (visible in the event, never silent)."""
        try:
            return {
                "name": self.guistr(obj, _NAME_OFFSET),
                "text": self.guistr(obj, _TEXT_OFFSET),
                "tip": self.guistr(obj, _TIP_OFFSET),
                "bounds": self.bounds(obj),
            }
        except ValueError as e:
            err = f"?{e}"
            return {"name": err, "text": err, "tip": err, "bounds": err}


def skip_noisy_classes(event: AutomationEvent) -> bool:
    """EventLogger skip predicate: hide gui_exit events for LOG_SKIP_CLASSES."""
    return event.name == "gui_exit" and event.data.get("cls") in LOG_SKIP_CLASSES


def install_gui_exit_events(cpu: "CPU", emitter: AutomationEventEmitter) -> None:
    """Emit `gui_exit` (this, cls, name, text, tip, bounds, code, sender,
    sender_cls, ret) whenever GUI::OnExit is entered. Emits every call; noise
    filtering belongs to subscribers."""
    class_cache: dict[int, str] = {}

    def on_exit(eip, regs, mem_ptr, mem_size):
        base = ctypes.addressof(mem_ptr.contents)

        def rd(addr: int, n: int) -> bytes:
            if not 0 <= addr < mem_size:
                raise ValueError(f"0x{addr:08x} outside guest memory")
            return ctypes.string_at(base + addr, min(n, mem_size - addr))

        esp = regs[ESP]
        if esp + 12 > mem_size:
            logger.error("automation", f"GUI::OnExit: ESP=0x{esp:08x} outside guest memory")
            return
        reader = GuestGuiReader(rd, class_cache)
        this = regs[ECX]
        ret, code, sender = (reader.u32(esp + off) for off in (0, 4, 8))
        emitter.emit(
            "gui_exit",
            this=this, cls=reader.class_name(this), **reader.describe(this),
            code=code, sender=sender, sender_cls=reader.class_name(sender), ret=ret)

    cpu.add_logpoint(GUI_ONEXIT_THUNK, on_exit)
