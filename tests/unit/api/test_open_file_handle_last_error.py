"""Regression test for CreateFile's missing SetLastError -- previously
open_file_handle logged failures but never set the real Win32 last-error
code (TEB+0x34), so GetLastError() after a failed CreateFile always read
whatever unrelated call happened to set it last.

Confirmed live this was exactly why DAO/Jet's own database-creation logic
for 'C:\\SaveData\\DB\\Tmp.MDB' (a file genuinely meant to be created fresh,
not pre-provisioned) gave up after one honest OPEN_EXISTING failure instead
of retrying with a create-capable disposition: real "check if exists, else
create" guest code relies on GetLastError() == ERROR_FILE_NOT_FOUND to know
a missing-file failure is expected/recoverable, not fatal.
"""
from __future__ import annotations

from tew.api._state import (
    CRTState, EmulatorConfig, TEB_BASE,
    CREATE_NEW, OPEN_EXISTING,
)
from tew.api.win32_errors import Win32Error
from tew.hardware.memory import Memory

INVALID_HANDLE = 0xFFFFFFFF
MEM_SIZE = 4 * 1024 * 1024  # must cover TEB_BASE (0x00320000) + 0x34


def _state(tmp_path, monkeypatch) -> CRTState:
    monkeypatch.chdir(tmp_path)
    config = EmulatorConfig(
        path_mappings={"c:/": str(tmp_path) + "/"},
        interactive_on_missing_file=False,
    )
    return CRTState(config=config)


def _last_error(mem: Memory) -> int:
    return mem.read32(TEB_BASE + 0x34)


class TestOpenExistingOnMissingFile:
    def test_sets_error_file_not_found(self, tmp_path, monkeypatch):
        """The exact scenario that broke Tmp.MDB creation: OPEN_EXISTING on
        a genuinely-missing file must set ERROR_FILE_NOT_FOUND so the
        caller's own retry-with-create logic can detect it."""
        state = _state(tmp_path, monkeypatch)
        mem = Memory(MEM_SIZE)

        handle = state.open_file_handle(
            "C:\\SaveData\\DB\\Tmp.MDB", writable=True, memory=mem,
            no_create_prompt=True, disposition=OPEN_EXISTING,
        )

        assert handle == INVALID_HANDLE
        assert _last_error(mem) == Win32Error.ERROR_FILE_NOT_FOUND

    def test_subsequent_create_always_then_succeeds(self, tmp_path, monkeypatch):
        """The real-world retry sequence this fix enables: after the honest
        OPEN_EXISTING failure above, a real caller would retry with
        CREATE_ALWAYS/CREATE_NEW -- confirm that actually creates the file."""
        state = _state(tmp_path, monkeypatch)
        mem = Memory(MEM_SIZE)

        first = state.open_file_handle(
            "C:\\SaveData\\DB\\Tmp.MDB", writable=True, memory=mem,
            no_create_prompt=True, disposition=OPEN_EXISTING,
        )
        assert first == INVALID_HANDLE

        second = state.open_file_handle(
            "C:\\SaveData\\DB\\Tmp.MDB", writable=True, memory=mem,
            no_create_prompt=True, disposition=CREATE_NEW,
        )
        assert second != INVALID_HANDLE
        assert (tmp_path / "SaveData" / "DB" / "Tmp.MDB").exists()


class TestCreateNewOnExistingFile:
    def test_sets_error_already_exists(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)
        mem = Memory(MEM_SIZE)
        (tmp_path / "already.txt").write_text("x")

        handle = state.open_file_handle(
            "C:\\already.txt", writable=True, memory=mem,
            disposition=CREATE_NEW,
        )

        assert handle == INVALID_HANDLE
        assert _last_error(mem) == Win32Error.ERROR_ALREADY_EXISTS


class TestReadPathMissingFile:
    def test_sets_error_file_not_found(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)
        mem = Memory(MEM_SIZE)

        handle = state.open_file_handle(
            "C:\\does_not_exist.txt", writable=False, memory=mem,
        )

        assert handle == INVALID_HANDLE
        assert _last_error(mem) == Win32Error.ERROR_FILE_NOT_FOUND
