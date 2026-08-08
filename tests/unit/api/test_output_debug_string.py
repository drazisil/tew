"""Tests for kernel32.dll!OutputDebugStringA -- added 2026-08-07 at Molly's
request ("route the SystemPrint to a file, as well as outputDebugString").

Like Channel_SystemPrint (patch_internals.py), OutputDebugStringA now also
writes its text into whatever real host file guest_stdout_handle points at
(CRTState.write_guest_stdout, factored out of Channel_SystemPrint's own
write for this) -- not just tew's own /tmp/emu.log via logger.info -- so
real debugger-visible text ends up on disk the same way. A trailing
newline is added when the guest string doesn't already end with one, so
consecutive OutputDebugString calls don't run together on one line.
"""
from __future__ import annotations

import os

from tew.api._state import CRTState, FileHandleEntry
from tew.api.kernel32_io import register_kernel32_io_handlers
from tew.api.win32_handlers import Win32Handlers
from tew.hardware.cpu_zig import ESP
from tew.hardware.memory import Memory
from tew import logger as logger_module

MEM_SIZE = 4 * 1024 * 1024
STACK    = 0x00200000


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False


def _env():
    mem = Memory(MEM_SIZE)
    state = CRTState()
    stubs = Win32Handlers(mem)
    register_kernel32_io_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    return cpu, mem, state, stubs


def _write_cstr(mem, addr, s: str) -> None:
    data = s.encode("latin-1") + b"\x00"
    for i, b in enumerate(data):
        mem.write8(addr + i, b)


def _call(stubs, cpu, mem, text: str) -> None:
    str_addr = 0x300000
    _write_cstr(mem, str_addr, text)
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4, str_addr)
    stubs._handlers["kernel32.dll!OutputDebugStringA"].handler(cpu)


class TestOutputDebugStringNoStdoutHandle:

    def test_does_not_crash_when_unset(self):
        cpu, mem, state, stubs = _env()
        assert state.guest_stdout_handle is None
        _call(stubs, cpu, mem, "plain message")  # must not raise

    def test_still_logs_to_tew_log(self):
        cpu, mem, state, stubs = _env()
        lines: list[str] = []
        logger_module.set_emit_hook(lambda level, line: lines.append(line))
        try:
            _call(stubs, cpu, mem, "plain message")
        finally:
            logger_module.set_emit_hook(None)
        assert any("plain message" in line for line in lines)


class TestOutputDebugStringWritesToGuestStdout:

    def _with_stdout_handle(self, cpu, mem, state, tmp_path):
        out_path = tmp_path / "stdout.txt"
        fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT, 0o644)
        state.file_handle_map[0x5000] = FileHandleEntry(
            path=str(out_path), data=b"", position=0, writable=True, fd=fd
        )
        state.guest_stdout_handle = 0x5000
        return out_path, fd

    def test_writes_real_text_to_the_real_file(self, tmp_path):
        cpu, mem, state, stubs = _env()
        out_path, fd = self._with_stdout_handle(cpu, mem, state, tmp_path)
        _call(stubs, cpu, mem, "hello from OutputDebugString")
        os.close(fd)
        assert out_path.read_text() == "hello from OutputDebugString\n"

    def test_appends_across_multiple_calls(self, tmp_path):
        cpu, mem, state, stubs = _env()
        out_path, fd = self._with_stdout_handle(cpu, mem, state, tmp_path)
        _call(stubs, cpu, mem, "first")
        _call(stubs, cpu, mem, "second")
        os.close(fd)
        assert out_path.read_text() == "first\nsecond\n"

    def test_does_not_double_newline_when_string_already_ends_with_one(self, tmp_path):
        cpu, mem, state, stubs = _env()
        out_path, fd = self._with_stdout_handle(cpu, mem, state, tmp_path)
        _call(stubs, cpu, mem, "already terminated\n")
        os.close(fd)
        assert out_path.read_text() == "already terminated\n"

    def test_read_only_handle_is_not_written(self, tmp_path):
        cpu, mem, state, stubs = _env()
        out_path = tmp_path / "stdout.txt"
        fd = os.open(str(out_path), os.O_RDONLY | os.O_CREAT, 0o644)
        state.file_handle_map[0x5000] = FileHandleEntry(
            path=str(out_path), data=b"", position=0, writable=False, fd=fd
        )
        state.guest_stdout_handle = 0x5000
        _call(stubs, cpu, mem, "should not appear")  # must not raise
        os.close(fd)
        assert out_path.read_text() == ""
