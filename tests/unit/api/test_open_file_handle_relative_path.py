"""
Regression test for CRTState.open_file_handle's os.makedirs("") crash on
a bare relative filename (e.g. MCity_d.exe's own diagnostic trace writer
opening "trace000.txt" with no directory component at all -- confirmed in
Ghidra: format string "trace%03d.txt" at 0x011a3a20, called from mono.c's
FUN_005ce2b0).

os.path.dirname("trace000.txt") == "", and os.makedirs("", exist_ok=True)
raises FileNotFoundError rather than being a no-op -- real CreateFile
needs no directory creation for this case either, since the process's
current directory already exists.
"""
from __future__ import annotations

from tew.api._state import CRTState, EmulatorConfig
from tew.hardware.memory import Memory

INVALID_HANDLE = 0xFFFFFFFF
MEM_SIZE = 1 * 1024 * 1024


def _state(tmp_path, monkeypatch) -> CRTState:
    monkeypatch.chdir(tmp_path)
    config = EmulatorConfig(path_mappings={}, interactive_on_missing_file=False)
    return CRTState(config=config)


def test_bare_relative_filename_opens_successfully(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    mem = Memory(MEM_SIZE)

    handle = state.open_file_handle("trace000.txt", writable=True, memory=mem)

    assert handle != INVALID_HANDLE
    entry = state.file_handle_map[handle]
    assert entry.writable
    assert (tmp_path / "trace000.txt").exists()


def test_bare_relative_filename_is_actually_writable(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    mem = Memory(MEM_SIZE)
    handle = state.open_file_handle("trace000.txt", writable=True, memory=mem)
    entry = state.file_handle_map[handle]

    import os
    os.write(entry.fd, b"hello")
    os.close(entry.fd)

    assert (tmp_path / "trace000.txt").read_bytes() == b"hello"


def test_path_with_directory_component_still_creates_it(tmp_path, monkeypatch):
    """Regression guard: the normal case (a real directory prefix) must
    still work -- this exercises the `if dirname:` branch's True side."""
    state = _state(tmp_path, monkeypatch)
    mem = Memory(MEM_SIZE)
    # path_mappings keys are forward-slash, lowercase -- matching how
    # load_emulator_config() normalizes them (win.replace("\\","/").lower()).
    config = EmulatorConfig(
        path_mappings={"c:/": str(tmp_path) + "/"},
        interactive_on_missing_file=False,
    )
    state.config = config

    handle = state.open_file_handle("C:\\MCity\\Logs\\foo.txt", writable=True, memory=mem)

    assert handle != INVALID_HANDLE
    assert (tmp_path / "MCity" / "Logs" / "foo.txt").exists()
