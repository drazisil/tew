"""Filesystem path helpers shared across the loader and API layers."""

from __future__ import annotations

import os
from typing import Optional


def find_file_ci(linux_path: str) -> Optional[str]:
    """Case-insensitive file lookup for Linux (Windows paths are case-insensitive).
    Returns the real on-disk path if found (any case), or None if not found.
    Resolves every path component case-insensitively, not just the final one.
    """
    if os.path.exists(linux_path):
        return linux_path
    head, tail = os.path.split(linux_path)
    if not tail:
        # Root or bare separator — exists check above already failed.
        return None
    resolved_dir = find_file_ci(head)
    if resolved_dir is None:
        return None
    tail_lower = tail.lower()
    try:
        for entry in os.listdir(resolved_dir):
            if entry.lower() == tail_lower:
                return os.path.join(resolved_dir, entry)
    except OSError as e:
        from tew.logger import logger
        logger.debug("fileio", f"find_file_ci: cannot list {resolved_dir!r}: {e}")
    return None
