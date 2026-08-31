"""Tests for msvcrt.dll!fputs -- verifies text is actually written to the
real host file for a writable, fd-backed FILE*, and routed to the logger
otherwise (unknown/stdio-like stream), mirroring test_msvcrt_write.py's
approach for _write.
"""
from __future__ import annotations

import os
import tempfile
import pytest

from tew.api._state import CRTState, FileHandleEntry
from tew.api.msvcrt_handlers import register_msvcrt_handlers
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


MEM_SIZE = 8 * 1024 * 1024
STACK    = 0x200000
STR_ADDR = 0x300000


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_msvcrt_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # return address
    return cpu, mem, state, stubs


def write_ansi(mem, addr, s: str) -> None:
    for i, ch in enumerate(s):
        mem.write8(addr + i, ord(ch))
    mem.write8(addr + len(s), 0)


def fputs_call(cpu, mem, stubs, str_addr, stream):
    mem.write32(STACK + 4, str_addr)
    mem.write32(STACK + 8, stream)
    stubs.get("msvcrt.dll", "fputs")(cpu)
    return cpu.regs[EAX]


class TestFputsToFile:
    def test_writes_text_to_host_file(self, env):
        cpu, mem, state, stubs = env
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            path = tf.name
        try:
            fd_host = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            handle = state.next_file_handle
            state.next_file_handle += 1
            state.file_handle_map[handle] = FileHandleEntry(
                path=path, data=b"", position=0, writable=True, fd=fd_host
            )
            write_ansi(mem, STR_ADDR, "hello file")
            fputs_call(cpu, mem, stubs, STR_ADDR, handle)
            os.close(fd_host)
            with open(path, "rb") as f:
                assert f.read() == b"hello file"
        finally:
            os.unlink(path)

    def test_does_not_append_newline(self, env):
        cpu, mem, state, stubs = env
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            path = tf.name
        try:
            fd_host = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            handle = state.next_file_handle
            state.next_file_handle += 1
            state.file_handle_map[handle] = FileHandleEntry(
                path=path, data=b"", position=0, writable=True, fd=fd_host
            )
            write_ansi(mem, STR_ADDR, "no newline")
            fputs_call(cpu, mem, stubs, STR_ADDR, handle)
            os.close(fd_host)
            with open(path, "rb") as f:
                assert f.read() == b"no newline"
        finally:
            os.unlink(path)

    def test_updates_position_after_write(self, env):
        cpu, mem, state, stubs = env
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            path = tf.name
        try:
            fd_host = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            handle = state.next_file_handle
            state.next_file_handle += 1
            entry = FileHandleEntry(path=path, data=b"", position=0, writable=True, fd=fd_host)
            state.file_handle_map[handle] = entry
            write_ansi(mem, STR_ADDR, "abcdef")
            fputs_call(cpu, mem, stubs, STR_ADDR, handle)
            os.close(fd_host)
            assert entry.position == 6
        finally:
            os.unlink(path)

    def test_returns_non_negative_on_success(self, env):
        cpu, mem, state, stubs = env
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            path = tf.name
        try:
            fd_host = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            handle = state.next_file_handle
            state.next_file_handle += 1
            state.file_handle_map[handle] = FileHandleEntry(
                path=path, data=b"", position=0, writable=True, fd=fd_host
            )
            write_ansi(mem, STR_ADDR, "x")
            result = fputs_call(cpu, mem, stubs, STR_ADDR, handle)
            os.close(fd_host)
            assert result != 0xFFFFFFFF  # not EOF
        finally:
            os.unlink(path)


class TestFputsUnknownStream:
    def test_unknown_handle_routes_to_logger_without_error(self, env):
        """No file_handle_map entry (e.g. stdout/stderr-like) -- must not
        raise, matching fwrite's fallback-to-logger behavior."""
        cpu, mem, state, stubs = env
        write_ansi(mem, STR_ADDR, "console text")
        result = fputs_call(cpu, mem, stubs, STR_ADDR, 0x9999)
        assert result != 0xFFFFFFFF
