"""Automation events: facts about what the game *is* doing, for scripted input.

Win32 messages say what the game was asked to do; an automation event says
what state it is in (which dialog just closed, with what exit code, where it
sits on screen), read out of guest memory by an event source such as
tew.automation.gui_events. Consumers register with
AutomationEventEmitter.subscribe(): the logger below today, the auto-click
chain next (instead of fixed delays and log-text triggers).
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from tew.logger import logger


@dataclass(frozen=True)
class AutomationEvent:
    name: str
    data: dict[str, Any] = field(default_factory=dict)


Subscriber = Callable[[AutomationEvent], None]


class AutomationEventEmitter:
    """In-process event bus. Subscribers run in registration order, on the
    thread that emits (for guest-memory sources that is the CPU thread, from
    inside a logpoint), so they must be quick and must not run guest code."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    def emit(self, name: str, /, **data: Any) -> None:
        # `name` is positional-only so a payload field can also be called "name".
        event = AutomationEvent(name, data)
        for fn in self._subscribers:
            try:
                fn(event)
            except Exception:
                # One bad subscriber must not silence the others, and nothing
                # here may fail quietly: log it with the event it choked on.
                logger.error(
                    "automation",
                    f"subscriber {fn!r} raised on '{name}' {data!r}:\n{traceback.format_exc()}")


def format_event(event: AutomationEvent) -> str:
    """`name key=value ...`: ints as 0x%08x (addresses, exit codes), strings
    quoted, anything else (e.g. a Bounds) via str()."""
    def fmt(v: Any) -> str:
        if isinstance(v, bool):
            return str(v)
        if isinstance(v, int):
            return f"0x{v & 0xFFFFFFFF:08x}"
        if isinstance(v, str):
            return f'"{v}"'
        return str(v)
    return " ".join([event.name, *(f"{k}={fmt(v)}" for k, v in event.data.items())])


class EventLogger:
    """Subscriber that logs every event at INFO under the "automation"
    category, except those `skip` returns True for (a noise filter for human
    reading; other subscribers still see every event)."""

    def __init__(self, skip: Callable[[AutomationEvent], bool] | None = None) -> None:
        self._skip = skip

    def __call__(self, event: AutomationEvent) -> None:
        if self._skip is not None and self._skip(event):
            return
        logger.info("automation", format_event(event))
