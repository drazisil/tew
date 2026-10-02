"""Regression test: msvcrt `_initterm` must not kill the thread it runs on.

`_initterm` runs each C++ static initializer through `_call_guest_void`,
which used to push THREAD_SENTINEL as the initializer's return address.
THREAD_SENTINEL is wired to the "spawned thread returned" handler, so every
initializer return marked the current thread dead. Live, that was
OLEAUT32.dll's real DllMain, called on the main thread before WinMain: the
main thread went DEAD in the scheduler, and `_invoke_emulated_proc` logged
"thread idx=0 ... has died" on every run.
"""
from __future__ import annotations

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from tew.api._state import THREAD_SENTINEL, CRTState
from tew.api.crt_handlers import _make_thread_return_handler
from tew.api.msvcrt_handlers import register_msvcrt_handlers
from tew.api.user32_handlers import _invoke_emulated_proc
from tew.api.win32_handlers import Win32Handlers
from tew.hardware.cpu_zig import ESP
from tew.hardware.cpu_zig import ZigCPU as CPU
from tew.hardware.memory import Memory
from tew.hardware.scheduler_zig import ThreadStatus

MEM_SIZE    = 8 * 1024 * 1024
STACK       = 0x200000
HEAP_START  = 0x600000
DLLMAIN     = 0x500000
INIT_A      = 0x500100
INIT_B      = 0x500140
TABLE       = 0x500200   # [INIT_A, 0, INIT_B] -- a null slot is skipped
FLAG_A      = 0x510000
FLAG_B      = 0x510004
SENTINEL    = 0x520000   # _invoke_emulated_proc's own return sentinel (HLT)


def _env():
    mem = Memory(MEM_SIZE)
    cpu = CPU(mem)
    state = CRTState()
    state.memory = mem
    state.next_heap_alloc = HEAP_START
    stubs = Win32Handlers(mem)
    register_msvcrt_handlers(stubs, mem, state)
    # The real thread-completion trampoline, as crt_handlers installs it.
    mem.load(THREAD_SENTINEL, bytes([0xCD, 0xFE, 0xC3]))
    stubs.patch_address(THREAD_SENTINEL, "_threadReturn", _make_thread_return_handler(state, mem))
    stubs.install(cpu)
    cpu.regs[ESP] = STACK
    return cpu, mem, state, stubs


def _dllmain(initterm: int) -> bytes:
    """push TABLE+12; push TABLE; call _initterm; add esp, 8; mov eax, 1; ret 12"""
    rel = (initterm - (DLLMAIN + 15)) & 0xFFFFFFFF
    return (b"\x68" + (TABLE + 12).to_bytes(4, "little")
            + b"\x68" + TABLE.to_bytes(4, "little")
            + b"\xE8" + rel.to_bytes(4, "little")
            + b"\x83\xC4\x08"
            + b"\xB8\x01\x00\x00\x00"
            + b"\xC2\x0C\x00")


def _set_flag(addr: int) -> bytes:
    """mov dword [addr], 1; ret"""
    return b"\xC7\x05" + addr.to_bytes(4, "little") + (1).to_bytes(4, "little") + b"\xC3"


def test_initializers_return_without_killing_the_calling_thread():
    cpu, mem, state, stubs = _env()
    mem.load(DLLMAIN, _dllmain(stubs.lookup_handler_address("msvcrt.dll", "_initterm")))
    mem.load(INIT_A, _set_flag(FLAG_A))
    mem.load(INIT_B, _set_flag(FLAG_B))
    for i, fn in enumerate((INIT_A, 0, INIT_B)):
        mem.write32(TABLE + 4 * i, fn)
    mem.write8(SENTINEL, 0xF4)

    result = _invoke_emulated_proc(
        cpu, mem, DLLMAIN, [0x10000000, 1, 0], SENTINEL, scheduler=state.scheduler)

    assert mem.read32(FLAG_A) == 1 and mem.read32(FLAG_B) == 1  # both initializers ran
    assert result == 1                                          # DllMain's real return
    assert state.scheduler.status_at_idx(0) == ThreadStatus.READY
    assert not cpu.fatal_halt
