"""Tests for kernel32.dll!SearchPathA and SearchPathW."""
from __future__ import annotations

import os
import pytest

from tew.api._state import CRTState, TEB_BASE, read_cstring, read_wide_string
from tew.api.kernel32_io import register_kernel32_io_handlers
from tew.api.win32_errors import Win32Error
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP


class _StubHandlers:
    def __init__(self):
        self._h: dict = {}

    def register_handler(self, dll, name, fn):
        self._h[(dll, name)] = fn

    def get(self, dll, name):
        return self._h[(dll, name)]


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False
        self.fatal_halt = False
        self.eip = 0x401002


MEM_SIZE = 8 * 1024 * 1024
STACK    = 0x200000
STR_PATH = 0x300000
STR_FILE = 0x301000
STR_EXT  = 0x302000
OUT_BUF  = 0x303000
OUT_PART = 0x304000


@pytest.fixture
def env(tmp_path):
    mem = Memory(MEM_SIZE)
    state = CRTState()
    # Map C:\ to tmp_path
    state.config.path_mappings = {"c:/": str(tmp_path).replace("\\", "/") + "/"}
    state.current_directory = "C:\\testdir"
    stubs = _StubHandlers()
    register_kernel32_io_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    return cpu, mem, state, stubs, tmp_path


def _write_cstring(mem: Memory, addr: int, text: str) -> None:
    for i, ch in enumerate(text):
        mem.write8(addr + i, ord(ch) & 0xFF)
    mem.write8(addr + len(text), 0)


def _write_wide_string(mem: Memory, addr: int, text: str) -> None:
    for i, ch in enumerate(text):
        mem.write16(addr + i * 2, ord(ch) & 0xFFFF)
    mem.write16(addr + len(text) * 2, 0)


class TestSearchPathA:
    def test_search_path_a_finds_file_with_path_override(self, env):
        cpu, mem, state, stubs, tmp_path = env
        # Create a test file under tmp_path/custom/target.dll
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        target_file = custom_dir / "target.dll"
        target_file.write_text("dummy")

        _write_cstring(mem, STR_PATH, "C:\\nonexistent;C:\\custom")
        _write_cstring(mem, STR_FILE, "target.dll")
        mem.write32(OUT_PART, 0)

        mem.write32(STACK + 4, STR_PATH)
        mem.write32(STACK + 8, STR_FILE)
        mem.write32(STACK + 12, 0)   # lpExtension
        mem.write32(STACK + 16, 260) # nBufferLength
        mem.write32(STACK + 20, OUT_BUF)
        mem.write32(STACK + 24, OUT_PART)

        stubs.get("kernel32.dll", "SearchPathA")(cpu)

        expected = "C:\\custom\\target.dll"
        assert cpu.regs[EAX] == len(expected)
        assert read_cstring(OUT_BUF, mem) == expected
        part_ptr = mem.read32(OUT_PART)
        assert part_ptr == OUT_BUF + len("C:\\custom\\")
        assert read_cstring(part_ptr, mem) == "target.dll"

    def test_search_path_a_appends_extension(self, env):
        cpu, mem, state, stubs, tmp_path = env
        # Create file under tmp_path/testdir/target.ext
        test_dir = tmp_path / "testdir"
        test_dir.mkdir()
        target_file = test_dir / "target.ext"
        target_file.write_text("dummy")

        _write_cstring(mem, STR_FILE, "target")
        _write_cstring(mem, STR_EXT, ".ext")

        mem.write32(STACK + 4, 0)        # lpPath (NULL -> standard search: current dir)
        mem.write32(STACK + 8, STR_FILE)
        mem.write32(STACK + 12, STR_EXT)
        mem.write32(STACK + 16, 260)
        mem.write32(STACK + 20, OUT_BUF)
        mem.write32(STACK + 24, 0)

        stubs.get("kernel32.dll", "SearchPathA")(cpu)

        expected = "C:\\testdir\\target.ext"
        assert cpu.regs[EAX] == len(expected)
        assert read_cstring(OUT_BUF, mem) == expected

    def test_search_path_a_buffer_too_small(self, env):
        cpu, mem, state, stubs, tmp_path = env
        test_dir = tmp_path / "testdir"
        test_dir.mkdir()
        target_file = test_dir / "file.txt"
        target_file.write_text("data")

        _write_cstring(mem, STR_FILE, "file.txt")

        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, STR_FILE)
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, 5) # Buffer length 5 is too small
        mem.write32(STACK + 20, OUT_BUF)
        mem.write32(STACK + 24, 0)

        stubs.get("kernel32.dll", "SearchPathA")(cpu)

        expected = "C:\\testdir\\file.txt"
        assert cpu.regs[EAX] == len(expected) + 1  # Required buffer size including null

    def test_search_path_a_file_not_found(self, env):
        cpu, mem, state, stubs, tmp_path = env
        _write_cstring(mem, STR_FILE, "nonexistent.dll")

        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, STR_FILE)
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, 260)
        mem.write32(STACK + 20, OUT_BUF)
        mem.write32(STACK + 24, 0)

        stubs.get("kernel32.dll", "SearchPathA")(cpu)

        assert cpu.regs[EAX] == 0
        assert mem.read32(TEB_BASE + 0x34) == int(Win32Error.ERROR_FILE_NOT_FOUND)

    def test_search_path_a_null_filename(self, env):
        cpu, mem, state, stubs, tmp_path = env

        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, 0) # NULL lpFileName
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, 260)
        mem.write32(STACK + 20, OUT_BUF)
        mem.write32(STACK + 24, 0)

        stubs.get("kernel32.dll", "SearchPathA")(cpu)

        assert cpu.regs[EAX] == 0
        assert mem.read32(TEB_BASE + 0x34) == int(Win32Error.ERROR_INVALID_PARAMETER)


class TestSearchPathW:
    def test_search_path_w_finds_file_in_system32(self, env):
        cpu, mem, state, stubs, tmp_path = env
        sys_dir = tmp_path / "WINDOWS" / "SYSTEM32"
        sys_dir.mkdir(parents=True)
        target_file = sys_dir / "expsrv.dll"
        target_file.write_text("dummy")

        _write_wide_string(mem, STR_FILE, "expsrv.dll")
        mem.write32(OUT_PART, 0)

        mem.write32(STACK + 4, 0)        # lpPath (NULL -> search app dir, CWD, System32, Windows)
        mem.write32(STACK + 8, STR_FILE)
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, 260)     # nBufferLength (WCHARs)
        mem.write32(STACK + 20, OUT_BUF)
        mem.write32(STACK + 24, OUT_PART)

        stubs.get("kernel32.dll", "SearchPathW")(cpu)

        expected = "C:\\WINDOWS\\SYSTEM32\\expsrv.dll"
        assert cpu.regs[EAX] == len(expected)
        assert read_wide_string(OUT_BUF, mem) == expected
        part_ptr = mem.read32(OUT_PART)
        assert part_ptr == OUT_BUF + len("C:\\WINDOWS\\SYSTEM32\\") * 2
        assert read_wide_string(part_ptr, mem) == "expsrv.dll"

    def test_search_path_w_file_not_found(self, env):
        cpu, mem, state, stubs, tmp_path = env
        _write_wide_string(mem, STR_FILE, "missing.tlb")

        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, STR_FILE)
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, 260)
        mem.write32(STACK + 20, OUT_BUF)
        mem.write32(STACK + 24, 0)

        stubs.get("kernel32.dll", "SearchPathW")(cpu)

        assert cpu.regs[EAX] == 0
        assert mem.read32(TEB_BASE + 0x34) == int(Win32Error.ERROR_FILE_NOT_FOUND)
