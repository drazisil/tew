"""
Regression tests for CRTState.open_file_handle's handling of relative
(driveless) Windows paths, e.g. MCity_d.exe's own diagnostic trace writer
opening "trace000.txt" with no directory component at all (confirmed in
Ghidra: format string "trace%03d.txt" at 0x011a3a20, called from mono.c's
FUN_005ce2b0), or WinMain's fopen("stdout.txt","wt").

Two bugs covered here, both hit by the same "no drive letter" input:

1. os.path.dirname("trace000.txt") == "", and os.makedirs("", exist_ok=True)
   raises FileNotFoundError rather than being a no-op -- real CreateFile
   needs no directory creation for this case either, since the process's
   current directory already exists.

2. FIXED 2026-08-07: translate_windows_path() used to return a driveless
   path completely untranslated, so it resolved relative to the *host*
   Python process's cwd instead of the *guest*'s tracked current directory
   (CRTState.current_directory, default "C:\\MCity") -- confirmed live via
   WinMain's fopen("stdout.txt","wt") landing in the tew repo's own working
   directory instead of alongside every other guest file under the emulated
   filesystem root. translate_windows_path now prepends current_directory
   to any path with no drive letter before applying the normal C:\\ mapping.
"""
from __future__ import annotations

from tew.api._state import CRTState, EmulatorConfig
from tew.hardware.memory import Memory

INVALID_HANDLE = 0xFFFFFFFF
MEM_SIZE = 1 * 1024 * 1024


def _state(tmp_path, monkeypatch) -> CRTState:
    monkeypatch.chdir(tmp_path)
    # path_mappings keys are forward-slash, lowercase -- matching how
    # load_emulator_config() normalizes them (win.replace("\\","/").lower()).
    # Real emulator.json always configures this mapping; CRTState.current_directory
    # defaults to "C:\\MCity", so a bare relative filename resolves under
    # <tmp_path>/MCity/, matching real Windows current-directory semantics.
    config = EmulatorConfig(
        path_mappings={"c:/": str(tmp_path) + "/"},
        interactive_on_missing_file=False,
    )
    return CRTState(config=config)


def test_bare_relative_filename_opens_successfully(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    mem = Memory(MEM_SIZE)

    handle = state.open_file_handle("trace000.txt", writable=True, memory=mem)

    assert handle != INVALID_HANDLE
    entry = state.file_handle_map[handle]
    assert entry.writable
    assert (tmp_path / "MCity" / "trace000.txt").exists()


def test_bare_relative_filename_is_actually_writable(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    mem = Memory(MEM_SIZE)
    handle = state.open_file_handle("trace000.txt", writable=True, memory=mem)
    entry = state.file_handle_map[handle]

    import os
    os.write(entry.fd, b"hello")
    os.close(entry.fd)

    assert (tmp_path / "MCity" / "trace000.txt").read_bytes() == b"hello"


def test_bare_relative_filename_anchors_to_current_directory_not_host_cwd(tmp_path, monkeypatch):
    """The actual 2026-08-07 bug: a driveless path must resolve against the
    guest's CRTState.current_directory, not wherever the host Python process
    happened to be launched from."""
    state = _state(tmp_path, monkeypatch)
    mem = Memory(MEM_SIZE)
    state.current_directory = "C:\\SomeOtherDir"

    handle = state.open_file_handle("stdout.txt", writable=True, memory=mem)

    assert handle != INVALID_HANDLE
    assert (tmp_path / "SomeOtherDir" / "stdout.txt").exists()
    assert not (tmp_path / "stdout.txt").exists()


def test_path_with_directory_component_still_creates_it(tmp_path, monkeypatch):
    """Regression guard: the normal case (a real directory prefix) must
    still work -- this exercises the `if dirname:` branch's True side."""
    state = _state(tmp_path, monkeypatch)
    mem = Memory(MEM_SIZE)

    handle = state.open_file_handle("C:\\MCity\\Logs\\foo.txt", writable=True, memory=mem)

    assert handle != INVALID_HANDLE
    assert (tmp_path / "MCity" / "Logs" / "foo.txt").exists()
