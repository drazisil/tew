"""Regression test for _invoke_emulated_proc's "calling thread died mid-call"
detection (tew/api/user32_handlers.py).

Background (see memory/status.md, 2026-07-19/21 sessions): this helper
synchronously invokes emulated x86 code (e.g. a DLL's DllMain) by pushing a
sentinel return address on top of whatever thread is currently running, then
polling cpu.run() in chunks until that thread halts at the sentinel. DAO's
real DllMain-calling worker thread (tid=1012) was observed to die mid-call --
its stack unwound straight past the pushed sentinel and landed back at its
own THREAD_SENTINEL instead, without ever raising an exception. The polling
loop used to have no way to detect this and would burn the entire max_steps
budget waiting for a thread that scheduler._pick_next_ready can never
schedule again (DEAD threads are permanently excluded).

The fix: watch scheduler.threads[started_thread_idx].status and bail out
with a clear diagnostic (returning 0) the instant it goes DEAD, instead of
reading cpu.regs[EAX] -- which at that point holds leftover state from
whatever the scheduler switched to, not anything the intended call computed.

This test reproduces "thread dies mid-call" directly (via ExitThread, the
simplest real way to make scheduler.mark_current_dead fire) rather than via
the still-unexplained DAO/DllMain stack-skip -- the root cause of *why*
tid=1012 dies is a separate, still-open investigation (see status.md); this
guards the *detection*, which is already fixed and must not regress.
"""
from __future__ import annotations

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from tew.api._state import CRTState
from tew.api.kernel32_io import register_kernel32_io_handlers
from tew.api.user32_handlers import _invoke_emulated_proc
from tew.api.win32_handlers import Win32Handlers
from tew.hardware.cpu_zig import ZigCPU as CPU
from tew.hardware.memory import Memory
from tew.kernel.scheduler import ThreadStatus

MEM_SIZE   = 8 * 1024 * 1024
STACK      = 0x200000
PROC_ADDR  = 0x500000
SENTINEL   = 0x510000
GARBAGE_EAX = 0xDEADBEEF


def _build_dying_proc(exit_thread_addr: int) -> bytes:
    """A DllMain-shaped stub that never returns to its caller: it sets EAX to
    a distinctive garbage value (proving the *real* call never completes normally)
    then calls ExitThread directly, killing its own thread instead of doing RET.
    """
    mov_eax_garbage = bytes([0xB8]) + GARBAGE_EAX.to_bytes(4, "little")  # mov eax, GARBAGE_EAX
    push_exit_code  = bytes([0x6A, 0x00])                                # push 0
    mov_edx_addr    = bytes([0xBA]) + exit_thread_addr.to_bytes(4, "little")  # mov edx, exit_thread_addr
    call_edx        = bytes([0xFF, 0xD2])                                # call edx
    hlt             = bytes([0xF4])                                      # should never execute
    return mov_eax_garbage + push_exit_code + mov_edx_addr + call_edx + hlt


def test_invoke_emulated_proc_returns_zero_when_calling_thread_dies_mid_call():
    mem = Memory(MEM_SIZE)
    state = CRTState()  # constructs state.scheduler with a main thread (tid=1000) already

    cpu = CPU(mem)
    cpu.regs[4] = STACK  # ESP

    stubs = Win32Handlers(mem)
    register_kernel32_io_handlers(stubs, mem, state)
    stubs.install(cpu)

    exit_thread_addr = stubs.get_handler_address("kernel32.dll", "ExitThread")
    assert exit_thread_addr is not None

    proc = _build_dying_proc(exit_thread_addr)
    for i, b in enumerate(proc):
        mem.write8(PROC_ADDR + i, b)
    mem.write8(SENTINEL, 0xF4)  # HLT -- never actually reached in this scenario

    started_thread_idx = state.scheduler.current_idx

    result = _invoke_emulated_proc(
        cpu, mem,
        proc_addr=PROC_ADDR,
        args=[],
        sentinel=SENTINEL,
        max_steps=10_000,
        scheduler=state.scheduler,
    )

    # The thread that made the call must actually be dead -- otherwise this
    # test isn't exercising the scenario it claims to.
    assert state.scheduler.threads[started_thread_idx].status == ThreadStatus.DEAD

    # Must NOT be the garbage EAX the dying code set right before it died --
    # that's exactly the misattribution bug this detection prevents.
    assert result == 0
    assert result != GARBAGE_EAX

    # The whole point of detecting this eagerly: it must not have consumed
    # anywhere near the full max_steps budget polling a thread that can
    # never come back.
    assert cpu.fatal_halt is False
