"""Tests for INT3 (opcode 0xCC) routing through the real SEH dispatcher
instead of an unconditional fatal_halt.

Real Windows raises STATUS_BREAKPOINT (0x80000003) as a normal, dispatchable
structured exception when INT3 executes with no debugger attached -- not an
automatic crash. MCity_d.exe relies on exactly that: `_Nfs_DebuggerIsPresent`
is hardcoded to 1 in this debug build (WinMain, unconditional), so every one
of its ~1,780 assertion call sites throughout the binary executes a real
INT3 on purpose, expecting the game's own installed SEH frame to catch it,
log, and continue. These tests verify tew's win32_handlers.py INT3 dispatch
(not tew.kernel.seh's dispatcher itself, covered by test_seh.py) actually
gives the CPU's real SEH chain a chance before falling back to a halt.
"""

import pytest
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import ZigCPU, ESP, EAX, FatalHaltError
from tew.kernel.kernel_structures import KernelStructures
from tew.api.win32_handlers import Win32Handlers
from tew.kernel.seh import install as seh_install, register_seh_handlers

MEM_SIZE = 0x00400000
STACK_TOP = 0x00040000
CODE_BASE = 0x00050000
FRAME_A = 0x00041000


def write_bytes(mem: Memory, addr: int, data: bytes) -> None:
    for i, b in enumerate(data):
        mem.write8(addr + i, b)


def push_seh_frame(mem: Memory, fs_base: int, frame_addr: int, handler_addr: int, next_frame: int) -> None:
    mem.write32(frame_addr + 0x00, next_frame)
    mem.write32(frame_addr + 0x04, handler_addr)
    mem.write32(fs_base + 0x00, frame_addr)


@pytest.fixture
def cpu_env():
    mem = Memory(size_bytes=MEM_SIZE)
    cpu = ZigCPU(mem)
    ks = KernelStructures(mem)
    ks.initialize_kernel_structures(stack_base=STACK_TOP, stack_limit=STACK_TOP - 0x10000)
    cpu.kernel_structures = ks
    cpu.regs[ESP] = STACK_TOP - 0x100
    stubs = Win32Handlers(mem)
    seh_install(stubs, mem)
    register_seh_handlers(stubs, mem)
    stubs.install(cpu)
    return cpu, mem, ks, stubs


def test_int3_handled_by_seh_chain_does_not_halt(cpu_env):
    cpu, mem, ks, stubs = cpu_env
    # ContinueExecution handler: mov eax,0; ret
    handler = CODE_BASE
    write_bytes(mem, handler, bytes([0xB8, 0x00, 0x00, 0x00, 0x00, 0xC3]))
    push_seh_frame(mem, ks.get_fs_base(), FRAME_A, handler, 0xFFFFFFFF)

    write_bytes(mem, CODE_BASE + 0x100, bytes([0xCC]))  # INT3
    cpu.eip = CODE_BASE + 0x100
    cpu.step()

    assert cpu.halted is False
    assert cpu.fatal_halt is False


def test_int3_unhandled_falls_back_to_fatal_halt(cpu_env):
    cpu, mem, ks, stubs = cpu_env
    # Empty SEH chain (FS:[0] already terminates per initialize_kernel_structures)
    # -- dispatch_exception walks nothing and reports unhandled.
    write_bytes(mem, CODE_BASE + 0x100, bytes([0xCC]))  # INT3
    cpu.eip = CODE_BASE + 0x100

    # step() raises the instant fatal_halt newly becomes true (2026-07-23 fix)
    # rather than returning normally with the flag set.
    with pytest.raises(FatalHaltError):
        cpu.step()

    assert cpu.halted is True
    assert cpu.fatal_halt is True


def test_int3_exception_address_points_at_the_int3_not_past_it(cpu_env):
    cpu, mem, ks, stubs = cpu_env
    # ContinueSearch handler that records the ExceptionAddress it was given
    # via the EXCEPTION_RECORD pointer passed in [ESP+4] (first handler arg).
    handler = CODE_BASE
    write_bytes(mem, handler, bytes([
        0x8B, 0x44, 0x24, 0x04,        # mov eax, [esp+4]        ; pRecord
        0x8B, 0x40, 0x0C,              # mov eax, [eax+0xC]      ; ExceptionAddress field
        0xA3, 0x00, 0x60, 0x05, 0x00,  # mov [0x00056000], eax   ; stash for the test to read
        0xB8, 0x01, 0x00, 0x00, 0x00,  # mov eax, 1              ; ContinueSearch
        0xC3,                          # ret
    ]))
    push_seh_frame(mem, ks.get_fs_base(), FRAME_A, handler, 0xFFFFFFFF)

    int3_addr = CODE_BASE + 0x100
    write_bytes(mem, int3_addr, bytes([0xCC]))
    cpu.eip = int3_addr

    # The lone handler returns ContinueSearch, so the chain still ends up
    # unhandled overall (and step() still raises) -- but it must have run
    # and seen the correct ExceptionAddress before that happens.
    with pytest.raises(FatalHaltError):
        cpu.step()

    assert mem.read32(0x00056000) == int3_addr
