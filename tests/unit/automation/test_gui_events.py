"""GuestGuiReader and the gui_exit logpoint, against a synthetic guest image.

The image mirrors what the debug build's GUI classes look like in memory:
object -> vtable -> slot 1 (ClassName) -> JMP thunk -> body that CALLs a
static Type() -> thunk -> `mov eax, <string>; ret`.
"""
from __future__ import annotations

import ctypes
import struct

import pytest

from tew.automation import AutomationEventEmitter
from tew.automation.events import AutomationEvent
from tew.automation.gui_events import (
    GUI_ONBEGIN, GUI_ONEXIT_THUNK, GUI_VTABLE, LOG_SKIP_CLASSES, Bounds, GuestGuiReader,
    install_gui_events, skip_noisy_classes)
from tew.hardware.cpu_zig import ECX, ESP
from tew.logger import ERROR, set_emit_hook

SIZE = GUI_VTABLE + 0x1000          # GUI's real vtable address must be inside the image

FOO_VTABLE, FOO_THUNK, FOO_BODY, TYPE_THUNK, TYPE_FN, FOO_STR = 0x1100, 0x3000, 0x3100, 0x3200, 0x3300, 0x4000
FOO_OBJ, GUI_OBJ, BAR_OBJ = 0x5000, 0x5100, 0x5200
BAR_VTABLE, BAR_THUNK, BAR_BODY = 0x1200, 0x3400, 0x3500
PURECALL = 0x2000


def u32(img: bytearray, addr: int, value: int) -> None:
    img[addr:addr + 4] = struct.pack("<I", value & 0xFFFFFFFF)


def jmp(img: bytearray, at: int, target: int) -> None:
    img[at] = 0xE9
    u32(img, at + 1, target - (at + 5))


def call(img: bytearray, at: int, target: int) -> None:
    img[at] = 0xE8
    u32(img, at + 1, target - (at + 5))


def put_guistr(img: bytearray, obj: int, off: int, text: str, slot: int = 4, buf: int = 0x6000) -> None:
    """A GUIStr at obj+off whose characters are at `buf`, pointed to from +slot."""
    u32(img, obj + off + slot, buf)
    img[buf:buf + len(text) + 1] = text.encode() + b"\0"
    u32(img, obj + off + 12, len(text))


@pytest.fixture
def img() -> bytearray:
    m = bytearray(SIZE)
    # GUI itself: slot 1 is the pure-virtual ClassName.
    u32(m, GUI_VTABLE + 4, PURECALL)
    # Foo: ClassName -> thunk -> body (debug prologue fills with 0xcccccccc,
    # then calls Type()) -> thunk -> Type() returns "Foo".
    u32(m, FOO_VTABLE + 4, FOO_THUNK)
    jmp(m, FOO_THUNK, FOO_BODY)
    m[FOO_BODY] = 0xB8
    u32(m, FOO_BODY + 1, 0xCCCCCCCC)
    call(m, FOO_BODY + 10, TYPE_THUNK)
    jmp(m, TYPE_THUNK, TYPE_FN)
    m[TYPE_FN] = 0xB8
    u32(m, TYPE_FN + 1, FOO_STR)
    m[TYPE_FN + 5] = 0xC3
    m[FOO_STR:FOO_STR + 4] = b"Foo\0"
    # Bar: a ClassName with no string anywhere in it.
    u32(m, BAR_VTABLE + 4, BAR_THUNK)
    jmp(m, BAR_THUNK, BAR_BODY)
    # Objects.
    u32(m, FOO_OBJ, FOO_VTABLE)
    u32(m, GUI_OBJ, GUI_VTABLE)
    u32(m, BAR_OBJ, BAR_VTABLE)
    return m


def reader_for(img: bytearray, cache=None) -> GuestGuiReader:
    def rd(addr: int, n: int) -> bytes:
        if not 0 <= addr < len(img):
            raise ValueError(f"0x{addr:08x} outside guest memory")
        return bytes(img[addr:addr + n])
    return GuestGuiReader(rd, {} if cache is None else cache)


class TestClassName:

    def test_resolves_through_thunks_and_a_called_type_function(self, img):
        assert reader_for(img).class_name(FOO_OBJ) == "Foo"

    def test_raw_gui_is_labelled_purecall(self, img):
        assert reader_for(img).class_name(GUI_OBJ) == "GUI (purecall)"

    def test_null_object(self, img):
        assert reader_for(img).class_name(0) == "NULL"

    def test_unreadable_object_says_why(self, img):
        name = reader_for(img).class_name(0xDEAD0000)
        assert name.startswith("?") and "outside guest memory" in name

    def test_unresolvable_class_name_is_reported_not_guessed(self, img):
        name = reader_for(img).class_name(BAR_OBJ)
        assert name == f"?no string found in ClassName@0x{BAR_THUNK:08x}"

    def test_result_is_cached_per_vtable_and_shared_between_readers(self, img):
        cache: dict[int, str] = {}
        reader_for(img, cache).class_name(FOO_OBJ)
        assert cache == {FOO_VTABLE: "Foo"}
        u32(img, FOO_VTABLE + 4, PURECALL)       # would now read as GUI (purecall)...
        assert reader_for(img, cache).class_name(FOO_OBJ) == "Foo"   # ...but the cache answers


class TestDescribe:

    def test_name_text_tip_and_bounds(self, img):
        put_guistr(img, FOO_OBJ, 0x48, "Login")
        put_guistr(img, FOO_OBJ, 0xF0, "kTxtHome", slot=8, buf=0x6100)     # pointer in the other slot
        for i, v in enumerate((343, 153, 231, 120)):
            u32(img, FOO_OBJ + 0x60 + 4 + 4 * i, v)
        d = reader_for(img).describe(FOO_OBJ)
        assert d == {"name": "Login", "text": "", "tip": "kTxtHome", "bounds": Bounds(343, 153, 231, 120)}
        assert str(d["bounds"]) == "[343, 153] 231, 120"

    def test_negative_bounds(self, img):
        u32(img, FOO_OBJ + 0x60 + 4, -5)
        assert reader_for(img).bounds(FOO_OBJ).x == -5

    def test_unrecognised_guistr_layout_shows_raw_values(self, img):
        u32(img, FOO_OBJ + 0x48 + 4, 0x6200)
        u32(img, FOO_OBJ + 0x48 + 8, 0x7777)
        u32(img, FOO_OBJ + 0x48 + 12, 99)
        text = reader_for(img).guistr(FOO_OBJ, 0x48)
        assert text.startswith("?(len=99") and "0x00006200" in text and "0x00007777" in text

    def test_unreadable_object_reports_the_error_in_every_field(self, img):
        d = reader_for(img).describe(0xDEAD0000)
        assert set(d) == {"name", "text", "tip", "bounds"}
        assert all("outside guest memory" in v for v in d.values())


class FakeCPU:
    def __init__(self):
        self.logpoints = {}

    def add_logpoint(self, eip, cb):
        self.logpoints[eip] = cb


class TestInstallGuiEvents:

    def run_logpoint(self, img, this, code=0, sender=0, esp=0x7000, at=GUI_ONEXIT_THUNK):
        emitter = AutomationEventEmitter()
        events: list[AutomationEvent] = []
        emitter.subscribe(events.append)
        cpu = FakeCPU()
        install_gui_events(cpu, emitter)
        if esp + 12 <= len(img):
            u32(img, esp, 0x00AED673)       # return address
            u32(img, esp + 4, code)
            u32(img, esp + 8, sender)
        regs = [0] * 8
        regs[ESP], regs[ECX] = esp, this
        buf = (ctypes.c_uint8 * len(img)).from_buffer(img)
        cpu.logpoints[at](at, regs, ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)), len(img))
        del buf
        return events

    def test_registers_on_onbegin_and_the_onexit_thunk(self):
        cpu = FakeCPU()
        install_gui_events(cpu, AutomationEventEmitter())
        assert sorted(cpu.logpoints) == [0x00405A5B, 0x00AEC5E0]

    def test_emits_gui_exit_with_object_code_sender_and_return(self, img):
        put_guistr(img, FOO_OBJ, 0x48, "Generic")
        (event,) = self.run_logpoint(img, this=FOO_OBJ, code=2, sender=BAR_OBJ)
        d = event.data
        assert event.name == "gui_exit"
        assert (d["this"], d["cls"], d["name"], d["code"], d["sender"], d["ret"]) == (
            FOO_OBJ, "Foo", "Generic", 2, BAR_OBJ, 0x00AED673)
        assert d["sender_cls"].startswith("?no string found")
        assert set(d) >= {"text", "tip", "bounds"}

    def test_a_root_closing_itself_is_its_own_sender(self, img):
        (event,) = self.run_logpoint(img, this=FOO_OBJ, code=4, sender=FOO_OBJ)
        assert event.data["cls"] == event.data["sender_cls"] == "Foo"

    def test_onbegin_emits_gui_begin_with_object_fields_and_return_address(self, img):
        put_guistr(img, FOO_OBJ, 0x48, "Login")
        for i, v in enumerate((0, 0, 800, 600)):
            u32(img, FOO_OBJ + 0x60 + 4 + 4 * i, v)
        (event,) = self.run_logpoint(img, this=FOO_OBJ, at=GUI_ONBEGIN)
        d = event.data
        assert event.name == "gui_begin"
        assert (d["this"], d["cls"], d["name"], d["bounds"], d["ret"]) == (
            FOO_OBJ, "Foo", "Login", Bounds(0, 0, 800, 600), 0x00AED673)
        assert "code" not in d and "sender" not in d      # OnBegin takes no arguments

    def test_esp_outside_memory_is_logged_as_an_error_and_emits_nothing(self, img):
        lines: list[tuple[int, str]] = []
        set_emit_hook(lambda level, line: lines.append((level, line)))
        try:
            events = self.run_logpoint(img, this=FOO_OBJ, code=2, sender=FOO_OBJ, esp=len(img) - 4)
        finally:
            set_emit_hook(None)
        assert events == []
        assert [lvl for lvl, _ in lines] == [ERROR]
        assert "outside guest memory" in lines[0][1]

    def test_onbegin_with_esp_outside_memory_is_also_an_error(self, img):
        lines: list[tuple[int, str]] = []
        set_emit_hook(lambda level, line: lines.append((level, line)))
        try:
            events = self.run_logpoint(img, this=FOO_OBJ, esp=len(img) - 4, at=GUI_ONBEGIN)
        finally:
            set_emit_hook(None)
        assert events == []
        assert "GUI::OnBegin" in lines[0][1] and "outside guest memory" in lines[0][1]


class TestSkipNoisyClasses:

    @pytest.mark.parametrize("cls", sorted(LOG_SKIP_CLASSES))
    def test_listed_classes_are_skipped(self, cls):
        assert skip_noisy_classes(AutomationEvent("gui_exit", {"cls": cls})) is True

    @pytest.mark.parametrize("name", ["gui_begin", "gui_exit"])
    def test_both_gui_events_share_the_skip_list(self, name):
        assert skip_noisy_classes(AutomationEvent(name, {"cls": "GText"})) is True
        assert skip_noisy_classes(AutomationEvent(name, {"cls": "GDialog"})) is False

    def test_other_events_are_never_skipped(self):
        assert skip_noisy_classes(AutomationEvent("other", {"cls": "GText"})) is False
