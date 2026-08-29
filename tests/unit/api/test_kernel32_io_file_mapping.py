"""Tests for kernel32.dll!CreateFileMappingA/MapViewOfFile/UnmapViewOfFile.

Covers both real usage shapes: an anonymous (page-file-backed) mapping,
and a file-backed mapping that reads real host-file bytes at map time and
flushes writable changes back to the real file on unmap.
"""
from __future__ import annotations

import os
import tempfile
import pytest

from tew.api._state import CRTState, FileHandleEntry, TEB_BASE
from tew.api.kernel32_io import register_kernel32_io_handlers
from tew.api.win32_errors import Win32Error
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP

MEM_SIZE = 128 * 1024 * 1024  # must clear the heap base (simple_alloc starts at 0x04000000)
STACK    = 0x200000

PAGE_READWRITE = 0x04
PAGE_READONLY  = 0x02
FILE_MAP_WRITE = 0x0002
FILE_MAP_READ  = 0x0004
INVALID_HANDLE_VALUE = 0xFFFFFFFF


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


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_kernel32_io_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    return cpu, mem, state, stubs


def call(stubs, cpu, mem, name, args):
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    for i, val in enumerate(args):
        mem.write32(STACK + 4 + i * 4, val & 0xFFFFFFFF)
    stubs.get("kernel32.dll", name)(cpu)


def open_real_file(state, content: bytes, writable: bool = True) -> tuple[int, str]:
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        path = tf.name
        tf.write(content)
    flags = os.O_RDWR if writable else os.O_RDONLY
    fd = os.open(path, flags)
    handle = state.next_file_handle
    state.next_file_handle += 1
    state.file_handle_map[handle] = FileHandleEntry(
        path=path, data=b"", position=0, writable=writable, fd=fd, readable=True
    )
    return handle, path


class TestCreateFileMappingA:
    def test_anonymous_mapping_returns_handle(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "CreateFileMappingA",
             [0, 0, PAGE_READWRITE, 0, 4096, 0])
        assert cpu.regs[EAX] != 0
        assert cpu.regs[EAX] in state.file_mapping_map

    def test_invalid_handle_fails_returns_null(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "CreateFileMappingA",
             [0x9999, 0, PAGE_READWRITE, 0, 4096, 0])
        assert cpu.regs[EAX] == 0
        assert mem.read32(TEB_BASE + 0x34) == int(Win32Error.ERROR_INVALID_HANDLE)

    def test_anonymous_zero_size_fails(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "CreateFileMappingA",
             [0, 0, PAGE_READWRITE, 0, 0, 0])
        assert cpu.regs[EAX] == 0
        assert mem.read32(TEB_BASE + 0x34) == int(Win32Error.ERROR_INVALID_PARAMETER)

    def test_stdcall_cleanup(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "CreateFileMappingA",
             [0, 0, PAGE_READWRITE, 0, 4096, 0])
        assert cpu.regs[ESP] == STACK + 24


class TestMapViewOfFile:
    def test_anonymous_view_is_zero_filled(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "CreateFileMappingA", [0, 0, PAGE_READWRITE, 0, 16, 0])
        h_map = cpu.regs[EAX]
        call(stubs, cpu, mem, "MapViewOfFile",
             [h_map, FILE_MAP_WRITE, 0, 0, 16])
        base = cpu.regs[EAX]
        assert base != 0
        assert mem.read32(base) == 0

    def test_file_backed_view_reads_real_content(self, env):
        cpu, mem, state, stubs = env
        handle, path = open_real_file(state, b"hello, mapped world!")
        try:
            call(stubs, cpu, mem, "CreateFileMappingA", [handle, 0, PAGE_READWRITE, 0, 0, 0])
            h_map = cpu.regs[EAX]
            call(stubs, cpu, mem, "MapViewOfFile", [h_map, FILE_MAP_WRITE, 0, 0, 0])
            base = cpu.regs[EAX]
            assert base != 0
            content = bytes(mem.read8(base + i) for i in range(len(b"hello, mapped world!")))
            assert content == b"hello, mapped world!"
        finally:
            os.unlink(path)

    def test_file_backed_view_respects_offset(self, env):
        cpu, mem, state, stubs = env
        handle, path = open_real_file(state, b"0123456789")
        try:
            call(stubs, cpu, mem, "CreateFileMappingA", [handle, 0, PAGE_READWRITE, 0, 0, 0])
            h_map = cpu.regs[EAX]
            call(stubs, cpu, mem, "MapViewOfFile", [h_map, FILE_MAP_WRITE, 0, 5, 5])
            base = cpu.regs[EAX]
            content = bytes(mem.read8(base + i) for i in range(5))
            assert content == b"56789"
        finally:
            os.unlink(path)

    def test_invalid_mapping_handle_fails(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "MapViewOfFile", [0x9999, FILE_MAP_WRITE, 0, 0, 16])
        assert cpu.regs[EAX] == 0
        assert mem.read32(TEB_BASE + 0x34) == int(Win32Error.ERROR_INVALID_HANDLE)

    def test_stdcall_cleanup(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "CreateFileMappingA", [0, 0, PAGE_READWRITE, 0, 16, 0])
        h_map = cpu.regs[EAX]
        call(stubs, cpu, mem, "MapViewOfFile", [h_map, FILE_MAP_WRITE, 0, 0, 16])
        assert cpu.regs[ESP] == STACK + 20


class TestUnmapViewOfFile:
    def test_writable_view_flushes_to_real_file(self, env):
        cpu, mem, state, stubs = env
        handle, path = open_real_file(state, b"aaaaaaaaaa")
        try:
            call(stubs, cpu, mem, "CreateFileMappingA", [handle, 0, PAGE_READWRITE, 0, 0, 0])
            h_map = cpu.regs[EAX]
            call(stubs, cpu, mem, "MapViewOfFile", [h_map, FILE_MAP_WRITE, 0, 0, 0])
            base = cpu.regs[EAX]
            for i, ch in enumerate(b"bbbbb"):
                mem.write8(base + i, ch)
            call(stubs, cpu, mem, "UnmapViewOfFile", [base])
            assert cpu.regs[EAX] == 1
            with open(path, "rb") as f:
                assert f.read() == b"bbbbbaaaaa"
        finally:
            os.unlink(path)

    def test_unmapping_unknown_address_fails(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "UnmapViewOfFile", [0x12345678])
        assert cpu.regs[EAX] == 0
        assert mem.read32(TEB_BASE + 0x34) == int(Win32Error.ERROR_INVALID_PARAMETER)

    def test_double_unmap_fails_second_time(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "CreateFileMappingA", [0, 0, PAGE_READWRITE, 0, 16, 0])
        h_map = cpu.regs[EAX]
        call(stubs, cpu, mem, "MapViewOfFile", [h_map, FILE_MAP_WRITE, 0, 0, 16])
        base = cpu.regs[EAX]
        call(stubs, cpu, mem, "UnmapViewOfFile", [base])
        assert cpu.regs[EAX] == 1
        call(stubs, cpu, mem, "UnmapViewOfFile", [base])
        assert cpu.regs[EAX] == 0


class TestCloseHandleReleasesMapping:
    def test_close_handle_removes_mapping_entry(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "CreateFileMappingA", [0, 0, PAGE_READWRITE, 0, 16, 0])
        h_map = cpu.regs[EAX]
        assert h_map in state.file_mapping_map
        call(stubs, cpu, mem, "CloseHandle", [h_map])
        assert h_map not in state.file_mapping_map
