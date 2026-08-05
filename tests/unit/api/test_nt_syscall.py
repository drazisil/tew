"""Tests for NtSyscallDispatcher: registration, dispatch, and error paths."""
from __future__ import annotations

import pytest

from tew.api.nt_syscall import NtSyscallDispatcher
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.eip = 0x401000


MEM_SIZE = 4 * 1024 * 1024

NT_CLOSE = 0x019           # known syscall number, per the XP syscall table
UNKNOWN_SYSCALL = 0xFFF    # not present in the XP syscall table


@pytest.fixture
def env():
    mem = Memory(MEM_SIZE)
    dispatcher = NtSyscallDispatcher(mem)
    cpu = _FakeCPU()
    return cpu, mem, dispatcher


class TestRegisterAndDispatch:

    def test_registered_handler_is_invoked(self, env):
        cpu, mem, dispatcher = env
        calls = []

        def handler(c, m):
            calls.append((c, m))
            c.regs[EAX] = 0

        dispatcher.register(NT_CLOSE, handler)
        cpu.regs[EAX] = NT_CLOSE
        dispatcher.dispatch(cpu)
        assert calls == [(cpu, mem)]

    def test_two_handlers_do_not_clash(self, env):
        cpu, mem, dispatcher = env
        seen = []
        dispatcher.register(NT_CLOSE, lambda c, m: seen.append("close"))
        dispatcher.register(0x116, lambda c, m: seen.append("write_file"))

        cpu.regs[EAX] = NT_CLOSE
        dispatcher.dispatch(cpu)
        cpu.regs[EAX] = 0x116
        dispatcher.dispatch(cpu)

        assert seen == ["close", "write_file"]


class TestUnregisteredSyscall:

    def test_known_number_raises_with_resolved_name(self, env):
        cpu, mem, dispatcher = env
        cpu.regs[EAX] = NT_CLOSE
        with pytest.raises(RuntimeError, match="NtClose"):
            dispatcher.dispatch(cpu)

    def test_unknown_number_raises_with_fallback_name(self, env):
        cpu, mem, dispatcher = env
        cpu.regs[EAX] = UNKNOWN_SYSCALL
        with pytest.raises(RuntimeError, match=r"Unknown_0xfff"):
            dispatcher.dispatch(cpu)
