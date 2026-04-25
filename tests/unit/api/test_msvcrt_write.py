"""Tests for _write — verifies bytes are actually written to file/stdout/stderr."""
from __future__ import annotations

import os
import tempfile
import pytest

from tew.api._state import CRTState, FileHandleEntry
from tew.api.msvcrt_handlers import register_msvcrt_handlers
from tew.hardware.memory import Memory
from tew.hardware.cpu import EAX, ESP


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
BUF_ADDR = 0x300000


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


def write_call(cpu, mem, stubs, fd, buf_addr, count):
    mem.write32(STACK + 4, fd)
    mem.write32(STACK + 8, buf_addr)
    mem.write32(STACK + 12, count)
    stubs.get("msvcrt.dll", "_write")(cpu)
    return cpu.regs[EAX]


def put_bytes(mem, addr, data: bytes):
    for i, b in enumerate(data):
        mem.write8(addr + i, b)


class TestWriteToFile:
    def test_writes_bytes_to_host_file(self, env):
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
            payload = b"hello, world"
            put_bytes(mem, BUF_ADDR, payload)
            n = write_call(cpu, mem, stubs, handle, BUF_ADDR, len(payload))
            os.close(fd_host)
            assert n == len(payload)
            with open(path, "rb") as f:
                assert f.read() == payload
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
            payload = b"abcdef"
            put_bytes(mem, BUF_ADDR, payload)
            write_call(cpu, mem, stubs, handle, BUF_ADDR, len(payload))
            os.close(fd_host)
            assert entry.position == len(payload)
        finally:
            os.unlink(path)

    def test_returns_minus_one_for_read_only_handle(self, env):
        cpu, mem, state, stubs = env
        handle = state.next_file_handle
        state.next_file_handle += 1
        state.file_handle_map[handle] = FileHandleEntry(
            path="/dev/null", data=b"x", position=0, writable=False, fd=None
        )
        put_bytes(mem, BUF_ADDR, b"test")
        n = write_call(cpu, mem, stubs, handle, BUF_ADDR, 4)
        assert n == 0xFFFFFFFF

    def test_returns_minus_one_for_unknown_fd(self, env):
        cpu, mem, state, stubs = env
        put_bytes(mem, BUF_ADDR, b"data")
        n = write_call(cpu, mem, stubs, 0x9999, BUF_ADDR, 4)
        assert n == 0xFFFFFFFF


class TestWriteToStdio:
    def test_write_to_stdout_succeeds(self, env, capsys):
        cpu, mem, state, stubs = env
        # fd=1, not in file_handle_map — should write to real stdout
        put_bytes(mem, BUF_ADDR, b"hi\n")
        n = write_call(cpu, mem, stubs, 1, BUF_ADDR, 3)
        assert n == 3

    def test_write_to_stderr_succeeds(self, env, capsys):
        cpu, mem, state, stubs = env
        put_bytes(mem, BUF_ADDR, b"err\n")
        n = write_call(cpu, mem, stubs, 2, BUF_ADDR, 4)
        assert n == 4
