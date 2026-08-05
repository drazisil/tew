"""Tests for core NT native API handlers: NtWriteFile, NtTerminateProcess."""
from __future__ import annotations

import pytest

from tew.api.nt_handlers import register_nt_handlers, STATUS_SUCCESS
from tew.api.nt_syscall import NtSyscallDispatcher
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, EDX


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False
        self.eip = 0x401000


MEM_SIZE = 4 * 1024 * 1024
ARGS     = 0x300000
IO_STATUS = 0x310000
BUF       = 0x320000

NT_WRITE_FILE = 0x116
NT_TERMINATE_PROCESS = 0x103


@pytest.fixture
def env():
    mem = Memory(MEM_SIZE)
    dispatcher = NtSyscallDispatcher(mem)
    register_nt_handlers(dispatcher)
    cpu = _FakeCPU()
    return cpu, mem, dispatcher


def set_write_file_args(mem, file_handle, io_status_ptr, buf_ptr, length):
    mem.write32(ARGS + 0 * 4, file_handle)      # arg1: FileHandle
    mem.write32(ARGS + 1 * 4, 0)                 # arg2: Event
    mem.write32(ARGS + 2 * 4, 0)                 # arg3: ApcRoutine
    mem.write32(ARGS + 3 * 4, 0)                 # arg4: ApcContext
    mem.write32(ARGS + 4 * 4, io_status_ptr)      # arg5: IoStatusBlock
    mem.write32(ARGS + 5 * 4, buf_ptr)            # arg6: Buffer
    mem.write32(ARGS + 6 * 4, length)             # arg7: Length


class TestNtWriteFile:

    def test_stdout_writes_to_sys_stdout(self, env, capsys):
        cpu, mem, dispatcher = env
        text = b"hello"
        for i, b in enumerate(text):
            mem.write8(BUF + i, b)
        set_write_file_args(mem, 1, 0, BUF, len(text))
        cpu.regs[EDX] = ARGS
        cpu.regs[EAX] = NT_WRITE_FILE
        dispatcher.dispatch(cpu)
        assert capsys.readouterr().out == "hello"

    def test_stderr_writes_to_sys_stderr(self, env, capsys):
        cpu, mem, dispatcher = env
        text = b"oops"
        for i, b in enumerate(text):
            mem.write8(BUF + i, b)
        set_write_file_args(mem, 2, 0, BUF, len(text))
        cpu.regs[EDX] = ARGS
        cpu.regs[EAX] = NT_WRITE_FILE
        dispatcher.dispatch(cpu)
        assert capsys.readouterr().err == "oops"

    def test_unknown_handle_is_dropped_silently(self, env, capsys):
        cpu, mem, dispatcher = env
        text = b"nope"
        for i, b in enumerate(text):
            mem.write8(BUF + i, b)
        set_write_file_args(mem, 3, 0, BUF, len(text))
        cpu.regs[EDX] = ARGS
        cpu.regs[EAX] = NT_WRITE_FILE
        dispatcher.dispatch(cpu)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_null_io_status_ptr_not_written(self, env):
        cpu, mem, dispatcher = env
        set_write_file_args(mem, 1, 0, BUF, 0)
        cpu.regs[EDX] = ARGS
        cpu.regs[EAX] = NT_WRITE_FILE
        dispatcher.dispatch(cpu)
        assert cpu.regs[EAX] == STATUS_SUCCESS

    def test_nonzero_io_status_ptr_populated(self, env):
        cpu, mem, dispatcher = env
        text = b"hi"
        for i, b in enumerate(text):
            mem.write8(BUF + i, b)
        set_write_file_args(mem, 1, IO_STATUS, BUF, len(text))
        cpu.regs[EDX] = ARGS
        cpu.regs[EAX] = NT_WRITE_FILE
        dispatcher.dispatch(cpu)
        assert mem.read32(IO_STATUS) == STATUS_SUCCESS
        assert mem.read32(IO_STATUS + 4) == len(text)

    def test_invalid_utf8_does_not_raise(self, env):
        cpu, mem, dispatcher = env
        bad = bytes([0xFF, 0xFE])
        for i, b in enumerate(bad):
            mem.write8(BUF + i, b)
        set_write_file_args(mem, 1, 0, BUF, len(bad))
        cpu.regs[EDX] = ARGS
        cpu.regs[EAX] = NT_WRITE_FILE
        dispatcher.dispatch(cpu)  # must not raise
        assert cpu.regs[EAX] == STATUS_SUCCESS

    def test_sets_eax_to_status_success(self, env):
        cpu, mem, dispatcher = env
        set_write_file_args(mem, 1, 0, BUF, 0)
        cpu.regs[EDX] = ARGS
        cpu.regs[EAX] = NT_WRITE_FILE
        dispatcher.dispatch(cpu)
        assert cpu.regs[EAX] == STATUS_SUCCESS


class TestNtTerminateProcess:

    def test_halts_regardless_of_exit_code(self, env):
        cpu, mem, dispatcher = env
        mem.write32(ARGS + 0, 0xFFFFFFFF)  # current process
        mem.write32(ARGS + 4, 0)           # exit status 0
        cpu.regs[EDX] = ARGS
        cpu.regs[EAX] = NT_TERMINATE_PROCESS
        dispatcher.dispatch(cpu)
        assert cpu.halted is True

    def test_halts_with_nonzero_exit_code(self, env):
        cpu, mem, dispatcher = env
        mem.write32(ARGS + 0, 0xFFFFFFFF)
        mem.write32(ARGS + 4, 42)
        cpu.regs[EDX] = ARGS
        cpu.regs[EAX] = NT_TERMINATE_PROCESS
        dispatcher.dispatch(cpu)
        assert cpu.halted is True

    def test_sets_eax_to_status_success(self, env):
        cpu, mem, dispatcher = env
        mem.write32(ARGS + 0, 0xFFFFFFFF)
        mem.write32(ARGS + 4, 0)
        cpu.regs[EDX] = ARGS
        cpu.regs[EAX] = NT_TERMINATE_PROCESS
        dispatcher.dispatch(cpu)
        assert cpu.regs[EAX] == STATUS_SUCCESS
