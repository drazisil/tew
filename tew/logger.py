"""
Structured logger with level, category, and per-function filtering.

Control via environment variables:
  LOG_LEVEL=trace|debug|info|warn|error  (default: info)
  LOG_CATEGORIES=cpu,dll,loader,...       (default: * = all)

Categories: cpu, dll, loader, handlers, thread, wininet, d3d8,
            graphics, fileio, registry, exception, startup, scheduler, winsock, calls,
            window, dialog, channel, memory

Each comma-separated LOG_CATEGORIES token may be prefixed with `+` (default,
if omitted) or `-`, and may target a single Win32 function within a category
via `category.FuncName` (the function's own name, e.g. `CompareStringA` --
not the `dll!Func` form). Rules apply left to right, last match wins, so
`+handlers,-handlers.CompareStringA` means "log everything in the handlers
category except CompareStringA". A per-function rule only takes effect for
logging done while that function's own registered handler is on the call
stack (see set_current_handler / win32_handlers.py's dispatch loop) -- log
lines from anywhere else only ever match on the bare category.

`memory` is excluded even under the bare `*`/unset default (unlike every
other category) -- it's per-allocation HeapAlloc/HeapFree noise, useful
only when actually chasing an allocator bug. Opt in explicitly with
`+memory` (alone or alongside other categories).
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
    "window", "dialog", "channel", "memory",
]

# Categories that stay silent even under the bare "*"/unset LOG_CATEGORIES
# default -- must be explicitly opted into with "+<category>". See the
# "memory" note in the module docstring for why.
_DEFAULT_OFF_CATEGORIES = {"memory"}

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


# (include, category, subname) -- subname is the bare Win32 function name
# (HandlerEntry.func_name, e.g. "CompareStringA"), or None for a whole-
# category rule. Ordered: filtering applies rules left to right, last
# match wins.
CategoryRule = tuple[bool, str, str | None]


def _parse_categories(s: str | None) -> list[CategoryRule] | None:
    if not s or s == "*":
        return None  # None means all, no filtering at all
    rules: list[CategoryRule] = []
    for raw in s.split(","):
        tok = raw.strip()
        if not tok:
            continue
        include = True
        if tok[0] in "+-":
            include = tok[0] == "+"
            tok = tok[1:]
        if "." in tok:
            category, subname = tok.split(".", 1)
        else:
            category, subname = tok, None
        rules.append((include, category.strip().lower(), subname.strip() if subname else None))
    return rules


def _category_active(rules: list[CategoryRule], category: str, current_handler: str | None) -> bool:
    active = False
    for include, rule_category, rule_subname in rules:
        if rule_category != category:
            continue
        if rule_subname is not None and rule_subname != current_handler:
            continue
        active = include
    return active


_active_level: int = _parse_level(os.environ.get("LOG_LEVEL"))
_active_categories: list[CategoryRule] | None = _parse_categories(os.environ.get("LOG_CATEGORIES"))

# Name (HandlerEntry.func_name) of the Win32 handler currently executing, if
# any -- set by win32_handlers.py's dispatch loop around each handler call so
# per-function LOG_CATEGORIES rules (e.g. "handlers.CompareStringA") can be
# evaluated against whichever handler is actually on the call stack right
# now. None outside any dispatched handler call.
_current_handler_name: str | None = None


def set_current_handler(name: str | None) -> str | None:
    """Set the currently-executing handler's name for per-function log
    filtering; returns the PREVIOUS value so the caller can restore it
    (important for a handler that itself triggers a nested dispatched
    call -- the outer name must come back once the inner call returns)."""
    global _current_handler_name
    previous = _current_handler_name
    _current_handler_name = name
    return previous

_LEVEL_PREFIX: dict[int, str] = {
    ERROR: "[ERROR]",
    WARN:  "[WARN] ",
    INFO:  "[INFO] ",
    DEBUG: "[DEBUG]",
    TRACE: "[TRACE]",
}

EmitHook = Callable[[int, str], None]
_emit_hook: EmitHook | None = None

# Set once CRTState (and its scheduler) exists -- see set_thread_id_provider.
# A plain callback, not a direct import of _state.py, since _state.py
# already imports this module for its own logger.warn/error calls and a
# back-import would be circular.
ThreadIdProvider = Callable[[], int]
_thread_id_provider: ThreadIdProvider | None = None


def configure_logger(*, level: str | None = None, categories: str | None = None) -> None:
    global _active_level, _active_categories
    if level is not None:
        _active_level = _parse_level(level)
    if categories is not None:
        _active_categories = _parse_categories(categories)


def set_emit_hook(hook: EmitHook | None) -> None:
    global _emit_hook
    _emit_hook = hook


def set_thread_id_provider(provider: ThreadIdProvider | None) -> None:
    """Called once from run_exe.py right after CRTState is constructed, so
    every log line from that point on can be attributed to the thread that
    emitted it. Before this is called (early boot -- DLL loading, memory
    setup -- happens before CRTState exists), log lines carry no tid, same
    as they always did."""
    global _thread_id_provider
    _thread_id_provider = provider


def _category_permitted(level: int, category: str) -> bool:
    # ERROR is exempt from category filtering for every category, memory
    # included -- same as "exception" already was: every halt/fault this
    # emulator produces is required (CLAUDE.md's "halt loudly" rule) to log
    # an ERROR right before setting cpu.halted -- if that line could be
    # silently dropped by an unrelated LOG_CATEGORIES scope, the halt
    # diagnostic that follows would have no reason attached.
    if level == ERROR:
        return True
    # A default-off category (currently just "memory") needs an explicit
    # "+category" rule even under the bare "*"/unset default -- everything
    # else falls through to the normal "no filter means everything passes"
    # behavior.
    if category in _DEFAULT_OFF_CATEGORIES:
        return _active_categories is not None and _category_active(
            _active_categories, category, _current_handler_name
        )
    if _active_categories is None or category == "exception":
        return True
    return _category_active(_active_categories, category, _current_handler_name)


def _emit(level: int, category: str, msg: str, *, force: bool = False) -> None:
    if not force:
        if level > _active_level:
            return
        if not _category_permitted(level, category):
            return

    elapsed = time.monotonic() - _start_time
    ts = f"{elapsed:8.3f}s"
    tid_field = ""
    if _thread_id_provider is not None:
        tid_field = f" [tid={_thread_id_provider()}]"
    line = f"{ts} {_LEVEL_PREFIX[level]}{tid_field} [{category}] {msg}"
    print(line, flush=True)
    if _emit_hook is not None:
        _emit_hook(level, line)


def is_active(level: int, category: str) -> bool:
    if level > _active_level:
        return False
    return _category_permitted(level, category)


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

    def always(self, level: int, category: str, msg: str) -> None:
        """Bypasses BOTH level and category filtering entirely -- for
        crash-diagnostic context that must never be silently dropped by
        whatever LOG_LEVEL/LOG_CATEGORIES an operator happened to pick
        (e.g. "here is the last valid EIP before this jump went bad").
        `level` still controls the printed [ERROR]/[WARN]/[INFO] prefix
        -- this doesn't misrepresent severity, it only guarantees
        visibility. Genuine errors should still use .error() (already
        exempt from both filters); reach for this only for the handful of
        WARN/INFO-level lines that are load-bearing crash context, not as
        a general escape hatch from log configuration."""
        _emit(level, category, msg, force=True)

    def is_active(self, level: int, category: str) -> bool:
        return is_active(level, category)


logger = _Logger()
