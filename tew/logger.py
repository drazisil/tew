"""
Structured logger with level and category filtering.

Control via environment variables:
  LOG_LEVEL=trace|debug|info|warn|error  (default: info)
  LOG_CATEGORIES=cpu,dll,loader,...       (default: * = all)

Categories: cpu, dll, loader, handlers, thread, wininet, d3d8,
            graphics, fileio, registry, exception, startup, scheduler, winsock, calls
"""

import os
import sys
import time
from typing import Callable, Literal

_start_time: float = time.monotonic()

LogCategory = Literal[
    "cpu", "dll", "loader", "handlers", "thread", "wininet",
    "d3d8", "graphics", "fileio", "registry", "exception",
    "startup", "scheduler", "winsock", "calls",
    "window", "dialog",
]

ERROR = 0
WARN = 1
INFO = 2
DEBUG = 3
TRACE = 4


def _parse_level(s: str | None) -> int:
    match (s or "").lower():
        case "error": return ERROR
        case "warn":  return WARN
        case "info":  return INFO
        case "debug": return DEBUG
        case "trace": return TRACE
        case _:       return INFO


def _parse_categories(s: str | None) -> set[str] | None:
    if not s or s == "*":
        return None  # None means all
    return set(c.strip().lower() for c in s.split(","))


_active_level: int = _parse_level(os.environ.get("LOG_LEVEL"))
_active_categories: set[str] | None = _parse_categories(os.environ.get("LOG_CATEGORIES"))

_LEVEL_PREFIX: dict[int, str] = {
    ERROR: "[ERROR]",
    WARN:  "[WARN] ",
    INFO:  "[INFO] ",
    DEBUG: "[DEBUG]",
    TRACE: "[TRACE]",
}

EmitHook = Callable[[int, str], None]
_emit_hook: EmitHook | None = None


def configure_logger(*, level: str | None = None, categories: str | None = None) -> None:
    global _active_level, _active_categories
    if level is not None:
        _active_level = _parse_level(level)
    if categories is not None:
        _active_categories = _parse_categories(categories)


def set_emit_hook(hook: EmitHook | None) -> None:
    global _emit_hook
    _emit_hook = hook


def _emit(level: int, category: str, msg: str) -> None:
    if level > _active_level:
        return
    if _active_categories is not None and category not in _active_categories and category != "exception":
        return

    elapsed = time.monotonic() - _start_time
    ts = f"{elapsed:8.3f}s"
    line = f"{ts} {_LEVEL_PREFIX[level]} [{category}] {msg}"
    if level == ERROR:
        print(line, file=sys.stderr, flush=True)
    elif level == WARN:
        print(line, file=sys.stderr, flush=True)
    else:
        print(line, flush=True)

    if _emit_hook is not None:
        _emit_hook(level, line)


def is_active(level: int, category: str) -> bool:
    if level > _active_level:
        return False
    if _active_categories is not None and category not in _active_categories:
        return False
    return True


class _Logger:
    def error(self, category: str, msg: str) -> None:
        _emit(ERROR, category, msg)

    def warn(self, category: str, msg: str) -> None:
        _emit(WARN, category, msg)

    def info(self, category: str, msg: str) -> None:
        _emit(INFO, category, msg)

    def debug(self, category: str, msg: str) -> None:
        _emit(DEBUG, category, msg)

    def trace(self, category: str, msg: str) -> None:
        _emit(TRACE, category, msg)

    def is_active(self, level: int, category: str) -> bool:
        return is_active(level, category)


logger = _Logger()
