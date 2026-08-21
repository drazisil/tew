"""Tests for tew.logger's level/category filtering, and the `always()`
bypass added to guarantee crash-diagnostic context (e.g. "here is the last
valid EIP before this jump went bad") is never silently dropped by
whatever LOG_LEVEL/LOG_CATEGORIES an operator happened to pick.

Root cause this fixes: run_exe.py's runaway-detector logs the fault EIP
and the last-known-good EIP via logger.warn("seh", ...) -- WARN is
filtered out entirely at LOG_LEVEL=error (the level ERROR is already
exempt from category filtering, per _emit's existing comment, but WARN/
INFO/DEBUG were not exempt from *either* filter). Two real investigation
sessions missed this exact diagnostic line because of it.
"""
from __future__ import annotations

import pytest

from tew import logger as logger_module
from tew.logger import ERROR, WARN, INFO, DEBUG, configure_logger, logger, set_emit_hook


@pytest.fixture
def captured():
    lines: list[tuple[int, str]] = []

    def hook(level: int, line: str) -> None:
        lines.append((level, line))

    set_emit_hook(hook)
    yield lines
    set_emit_hook(None)


@pytest.fixture(autouse=True)
def isolate_logger_config():
    saved_level = logger_module._active_level
    saved_categories = logger_module._active_categories
    yield
    logger_module._active_level = saved_level
    logger_module._active_categories = saved_categories


class TestNormalFilteringUnchanged:
    """Regression guards -- always()'s bypass must not weaken the
    existing, intentional filtering for ordinary log calls."""

    def test_warn_dropped_when_level_is_error(self, captured):
        configure_logger(level="error", categories="*")
        logger.warn("seh", "should not appear")
        assert captured == []

    def test_warn_dropped_when_category_excluded(self, captured):
        configure_logger(level="warn", categories="cpu")
        logger.warn("seh", "should not appear")
        assert captured == []

    def test_warn_shown_when_level_and_category_match(self, captured):
        configure_logger(level="warn", categories="seh")
        logger.warn("seh", "should appear")
        assert len(captured) == 1

    def test_error_still_bypasses_category_filter(self, captured):
        # Pre-existing exemption -- must survive the always() addition.
        configure_logger(level="error", categories="cpu")
        logger.error("seh", "errors always show")
        assert len(captured) == 1


class TestAlwaysBypassesBothFilters:
    def test_bypasses_level_filter(self, captured):
        configure_logger(level="error", categories="*")
        logger.always(WARN, "seh", "must appear regardless of level")
        assert len(captured) == 1
        assert "must appear regardless of level" in captured[0][1]

    def test_bypasses_category_filter(self, captured):
        configure_logger(level="warn", categories="cpu")
        logger.always(WARN, "seh", "must appear regardless of category")
        assert len(captured) == 1

    def test_bypasses_both_at_once(self, captured):
        configure_logger(level="error", categories="cpu")
        logger.always(DEBUG, "seh", "must appear regardless of both")
        assert len(captured) == 1

    def test_printed_prefix_reflects_given_level_not_error(self, captured):
        # always() forcing visibility shouldn't misrepresent a WARN as an
        # ERROR in the printed output.
        configure_logger(level="error", categories="*")
        logger.always(WARN, "seh", "warn-level always message")
        assert "[WARN]" in captured[0][1]
        assert "[ERROR]" not in captured[0][1]

    def test_info_level_always_message(self, captured):
        configure_logger(level="error", categories="*")
        logger.always(INFO, "seh", "info-level always message")
        assert len(captured) == 1
        assert "[INFO]" in captured[0][1]
