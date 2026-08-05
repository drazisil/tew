"""Tests for version.dll handlers: GetFileVersionInfoSizeA, GetFileVersionInfoA, VerQueryValueA."""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.version_handlers import register_version_handlers
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


MEM_SIZE = 4 * 1024 * 1024
STACK    = 0x200000
FILENAME_ADDR = 0x300000
HANDLE_ADDR   = 0x300100


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_version_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # fake return address
    return cpu, mem, stubs


def write_cstring(mem, addr, s: str) -> None:
    data = s.encode("ascii") + b"\x00"
    for i, b in enumerate(data):
        mem.write8(addr + i, b)


class TestGetFileVersionInfoSizeA:

    def test_real_filename_returns_zero(self, env):
        cpu, mem, stubs = env
        write_cstring(mem, FILENAME_ADDR, "MCity_d.exe")
        mem.write32(STACK + 4, FILENAME_ADDR)
        mem.write32(STACK + 8, 0)
        stubs.get("version.dll", "GetFileVersionInfoSizeA")(cpu)
        assert cpu.regs[EAX] == 0

    def test_null_filename_does_not_crash(self, env):
        cpu, mem, stubs = env
        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, 0)
        stubs.get("version.dll", "GetFileVersionInfoSizeA")(cpu)
        assert cpu.regs[EAX] == 0

    def test_nonzero_handle_ptr_written_zero(self, env):
        cpu, mem, stubs = env
        write_cstring(mem, FILENAME_ADDR, "MCity_d.exe")
        mem.write32(STACK + 4, FILENAME_ADDR)
        mem.write32(STACK + 8, HANDLE_ADDR)
        mem.write32(HANDLE_ADDR, 0xFFFFFFFF)  # pre-dirty
        stubs.get("version.dll", "GetFileVersionInfoSizeA")(cpu)
        assert mem.read32(HANDLE_ADDR) == 0

    def test_null_handle_ptr_not_written(self, env):
        cpu, mem, stubs = env
        write_cstring(mem, FILENAME_ADDR, "MCity_d.exe")
        mem.write32(STACK + 4, FILENAME_ADDR)
        mem.write32(STACK + 8, 0)
        stubs.get("version.dll", "GetFileVersionInfoSizeA")(cpu)
        # nothing to assert on memory (no target), just confirm no crash and EAX==0
        assert cpu.regs[EAX] == 0

    def test_stdcall_cleanup(self, env):
        cpu, mem, stubs = env
        write_cstring(mem, FILENAME_ADDR, "MCity_d.exe")
        mem.write32(STACK + 4, FILENAME_ADDR)
        mem.write32(STACK + 8, 0)
        stubs.get("version.dll", "GetFileVersionInfoSizeA")(cpu)
        assert cpu.regs[ESP] == STACK + 8
        assert mem.read32(STACK) == 0xDEAD


class TestGetFileVersionInfoA:

    def test_halts(self, env):
        cpu, mem, stubs = env
        stubs.get("version.dll", "GetFileVersionInfoA")(cpu)
        assert cpu.halted is True

    def test_halt_skips_stdcall_cleanup(self, env):
        cpu, mem, stubs = env
        stubs.get("version.dll", "GetFileVersionInfoA")(cpu)
        assert cpu.regs[ESP] == STACK


class TestVerQueryValueA:

    def test_halts(self, env):
        cpu, mem, stubs = env
        stubs.get("version.dll", "VerQueryValueA")(cpu)
        assert cpu.halted is True

    def test_halt_skips_stdcall_cleanup(self, env):
        cpu, mem, stubs = env
        stubs.get("version.dll", "VerQueryValueA")(cpu)
        assert cpu.regs[ESP] == STACK
