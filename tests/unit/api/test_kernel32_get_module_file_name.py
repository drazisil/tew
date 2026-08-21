"""Tests for kernel32.dll!GetModuleFileNameW. Live-confirmed crash: expsrv.dll
(the VBA runtime) called it and no handler existed at all (a deliberate
_halt() placeholder) -- unlike GetModuleFileNameA, which is fully
implemented. Same semantics as the A version, except nSize is a WCHAR count
(not bytes) and the output is null-terminated UTF-16LE.
"""
from __future__ import annotations

import pytest

from tew.api._state import CRTState, DynamicModule
from tew.api.kernel32_io import register_kernel32_io_handlers
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
BUF      = 0x300000


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_kernel32_io_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    return cpu, mem, state, stubs


def call(stubs, cpu, mem, h_module, buf, n_size):
    mem.write32(STACK + 4, h_module)
    mem.write32(STACK + 8, buf)
    mem.write32(STACK + 12, n_size)
    stubs.get("kernel32.dll", "GetModuleFileNameW")(cpu)


def read_wide(mem, addr, max_chars=260):
    out = []
    for i in range(max_chars):
        lo = mem.read8(addr + i * 2)
        hi = mem.read8(addr + i * 2 + 1)
        cp = lo | (hi << 8)
        if cp == 0:
            break
        out.append(chr(cp))
    return "".join(out)


class TestNullHModule:

    def test_returns_exe_path_as_utf16(self, env):
        cpu, mem, state, stubs = env
        state.exe_path = "/data/games/MCity/MCity_d.exe"
        call(stubs, cpu, mem, 0, BUF, 260)
        assert cpu.halted is False
        result = read_wide(mem, BUF)
        assert result == state.reverse_translate_path(state.exe_path)

    def test_return_value_is_char_count_excluding_null(self, env):
        cpu, mem, state, stubs = env
        state.exe_path = "/data/games/MCity/MCity_d.exe"
        call(stubs, cpu, mem, 0, BUF, 260)
        expected = state.reverse_translate_path(state.exe_path)
        assert cpu.regs[EAX] == len(expected)

    def test_unset_exe_path_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, 0, BUF, 260)
        assert cpu.halted is True
        assert cpu.fatal_halt is True

    def test_stdcall_cleanup(self, env):
        cpu, mem, state, stubs = env
        state.exe_path = "/data/games/MCity/MCity_d.exe"
        call(stubs, cpu, mem, 0, BUF, 260)
        assert cpu.regs[ESP] == STACK + 12


class TestKnownModuleHandle:

    def test_returns_dll_path_when_known(self, env):
        cpu, mem, state, stubs = env
        handle = 0x17000000
        state.dynamic_modules[handle] = DynamicModule(
            dll_name="expsrv.dll", base_address=handle, dll_path="C:\\Windows\\System32\\expsrv.dll"
        )
        call(stubs, cpu, mem, handle, BUF, 260)
        assert cpu.halted is False
        assert read_wide(mem, BUF) == "C:\\Windows\\System32\\expsrv.dll"

    def test_falls_back_to_bare_name_when_path_unknown(self, env):
        cpu, mem, state, stubs = env
        handle = 0x17000000
        state.dynamic_modules[handle] = DynamicModule(dll_name="expsrv.dll", base_address=handle)
        call(stubs, cpu, mem, handle, BUF, 260)
        assert read_wide(mem, BUF) == "expsrv.dll"

    def test_unknown_handle_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, 0xDEADBEEF, BUF, 260)
        assert cpu.halted is True
        assert cpu.fatal_halt is True


class TestTruncation:

    def test_buffer_too_small_truncates_and_returns_n_size(self, env):
        cpu, mem, state, stubs = env
        handle = 0x17000000
        state.dynamic_modules[handle] = DynamicModule(
            dll_name="expsrv.dll", base_address=handle, dll_path="C:\\Windows\\System32\\expsrv.dll"
        )
        call(stubs, cpu, mem, handle, BUF, 5)  # room for 4 chars + null
        assert cpu.regs[EAX] == 5
        assert read_wide(mem, BUF) == "C:\\W"

    def test_truncated_output_is_still_null_terminated(self, env):
        cpu, mem, state, stubs = env
        handle = 0x17000000
        state.dynamic_modules[handle] = DynamicModule(
            dll_name="expsrv.dll", base_address=handle, dll_path="C:\\Windows\\System32\\expsrv.dll"
        )
        call(stubs, cpu, mem, handle, BUF, 5)
        assert mem.read16(BUF + 4 * 2) == 0
