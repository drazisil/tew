"""Fire-once trigger that watches a growing text file for a piece of text.

Used by run_exe.py's TEW_CLICK_WHEN_FILE/TEW_CLICK_WHEN_TEXT to click when the
game itself reports a screen is ready (e.g. MCity_Log.txt's "Done Getting
Personas"), instead of guessing a wall-clock delay that varies wildly with
emulator speed and DB-thread timing.

Only bytes appended AFTER the trigger is constructed count: the game's log
files persist across runs, so a stale copy of the text from the previous run
must not fire immediately. A file that is truncated or replaced (size drops
below the read offset) is treated as brand new and read from the start.
"""

from __future__ import annotations

import os

from tew.logger import logger


class FileTextTrigger:
    def __init__(self, path: str, text: str) -> None:
        if not text:
            raise ValueError("FileTextTrigger needs non-empty text")
        self.path = path
        self._needle = text.encode("latin-1", errors="replace")
        self.fired = False
        self._offset = self._size()
        self._carry = b""  # tail of the previous chunk, so text split across reads still matches
        self._warned = False

    def _size(self) -> int:
        try:
            return os.stat(self.path).st_size
        except FileNotFoundError:
            return 0

    def poll(self) -> bool:
        """True exactly once: on the first poll after the text has appeared in
        newly appended content. False before that and forever after."""
        if self.fired:
            return False
        try:
            size = self._size()
            if size < self._offset:
                self._offset = 0
                self._carry = b""
            if size == self._offset:
                return False
            with open(self.path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read(size - self._offset)
        except OSError as e:
            if not self._warned:
                logger.error("startup", f"[file-trigger] cannot read {self.path}: {e}")
                self._warned = True
            return False
        self._offset += len(chunk)
        data = self._carry + chunk
        if self._needle in data:
            self.fired = True
            return True
        keep = len(self._needle) - 1
        self._carry = data[-keep:] if keep > 0 else b""
        return False
