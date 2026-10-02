"""AutomationEventEmitter / EventLogger / format_event."""
from __future__ import annotations

import pytest

from tew.automation import AutomationEvent, AutomationEventEmitter, EventLogger
from tew.automation.events import format_event
from tew.automation.gui_events import Bounds
from tew.logger import ERROR, INFO, set_emit_hook


@pytest.fixture
def captured():
    lines: list[tuple[int, str]] = []
    set_emit_hook(lambda level, line: lines.append((level, line)))
    yield lines
    set_emit_hook(None)


class TestEmitter:

    def test_subscribers_get_the_event_in_registration_order(self):
        emitter = AutomationEventEmitter()
        seen = []
        emitter.subscribe(lambda e: seen.append(("a", e)))
        emitter.subscribe(lambda e: seen.append(("b", e)))
        emitter.emit("gui_exit", code=2, cls="GMsgBox")
        assert [tag for tag, _ in seen] == ["a", "b"]
        assert seen[0][1] == AutomationEvent("gui_exit", {"code": 2, "cls": "GMsgBox"})

    def test_payload_may_have_a_field_called_name(self):
        emitter = AutomationEventEmitter()
        seen = []
        emitter.subscribe(seen.append)
        emitter.emit("gui_exit", name="Login")
        assert seen == [AutomationEvent("gui_exit", {"name": "Login"})]

    def test_emit_with_no_subscribers_is_fine(self):
        AutomationEventEmitter().emit("gui_exit", code=2)

    def test_failing_subscriber_is_logged_and_does_not_stop_the_rest(self, captured):
        emitter = AutomationEventEmitter()
        later = []

        def bad(event):
            raise RuntimeError("boom")

        emitter.subscribe(bad)
        emitter.subscribe(later.append)
        emitter.emit("gui_exit", code=4)
        assert len(later) == 1
        errors = [line for level, line in captured if level == ERROR]
        assert len(errors) == 1
        assert "[automation]" in errors[0]
        assert "gui_exit" in errors[0] and "boom" in errors[0] and "RuntimeError" in errors[0]


class TestFormatEvent:

    def test_ints_hex_strings_quoted_other_via_str(self):
        event = AutomationEvent("gui_exit", {
            "this": 0x45CDBE4, "name": "Generic", "bounds": Bounds(343, 153, 231, 120), "flag": True})
        assert format_event(event) == (
            'gui_exit this=0x045cdbe4 name="Generic" bounds=[343, 153] 231, 120 flag=True')

    def test_negative_int_is_masked_to_32_bits(self):
        assert format_event(AutomationEvent("e", {"v": -1})) == "e v=0xffffffff"

    def test_event_without_data_is_just_the_name(self):
        assert format_event(AutomationEvent("tick")) == "tick"


class TestEventLogger:

    def test_logs_at_info_under_automation_category(self, captured):
        emitter = AutomationEventEmitter()
        emitter.subscribe(EventLogger())
        emitter.emit("gui_exit", code=2)
        assert [(lvl, "[automation] gui_exit code=0x00000002" in line) for lvl, line in captured] == [(INFO, True)]

    def test_skip_predicate_hides_matching_events_from_the_log_only(self, captured):
        emitter = AutomationEventEmitter()
        other = []
        emitter.subscribe(EventLogger(skip=lambda e: e.data["code"] == 13))
        emitter.subscribe(other.append)
        emitter.emit("gui_exit", code=13)
        emitter.emit("gui_exit", code=2)
        assert len(captured) == 1 and "code=0x00000002" in captured[0][1]
        assert [e.data["code"] for e in other] == [13, 2]     # other subscribers still see all
