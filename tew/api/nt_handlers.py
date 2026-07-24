"""Core NT native API handlers.

These are the first real implementations in the LSW kernel layer.
Each handler reads arguments from memory at EDX (= &arg1 at SYSENTER),
sets EAX to an NTSTATUS return value, and returns.

Argument convention: EDX points to arg1; arg_n is at [EDX + (n-1)*4].
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory
    from tew.api.nt_syscall import NtSyscallDispatcher

from tew.hardware.cpu_zig import EAX, EDX
from tew.logger import logger

STATUS_SUCCESS = 0x00000000


def _arg(cpu: "CPU", memory: "Memory", n: int) -> int:
    """Read the nth argument (1-based) from the syscall arg list at EDX."""
    return memory.read32((cpu.regs[EDX] + (n - 1) * 4) & 0xFFFFFFFF)


# ── NtWriteFile (0x116) ───────────────────────────────────────────────────────

def _nt_write_file(cpu: "CPU", memory: "Memory") -> None:
    """NtWriteFile(FileHandle, Event, ApcRoutine, ApcContext,
                   IoStatusBlock, Buffer, Length, ByteOffset, Key)

    Writes Length bytes from Buffer to FileHandle.
    Handles 1 (stdout) and 2 (stderr) map to the host's sys.stdout/stderr.
    """
    file_handle   = _arg(cpu, memory, 1)
    io_status_ptr = _arg(cpu, memory, 5)
    buf_ptr       = _arg(cpu, memory, 6)
    length        = _arg(cpu, memory, 7)

    data = bytes(memory.read8(buf_ptr + i) for i in range(length))

    if file_handle in (1, 2):
        stream = sys.stdout if file_handle == 1 else sys.stderr
        stream.write(data.decode("utf-8", errors="replace"))
        stream.flush()
    else:
        logger.debug("nt", f"NtWriteFile: unhandled handle 0x{file_handle:x}, dropping {length} bytes")

    if io_status_ptr:
        memory.write32(io_status_ptr,     STATUS_SUCCESS)
        memory.write32(io_status_ptr + 4, length)

    cpu.regs[EAX] = STATUS_SUCCESS
    logger.debug("nt", f"NtWriteFile(handle=0x{file_handle:x}, len={length}) -> STATUS_SUCCESS")


# ── NtTerminateProcess (0x103) ────────────────────────────────────────────────

def _nt_terminate_process(cpu: "CPU", memory: "Memory") -> None:
    """NtTerminateProcess(ProcessHandle, ExitStatus)

    ProcessHandle -1 means the current process. Halts the CPU cleanly.
    """
    process_handle = _arg(cpu, memory, 1)
    exit_status    = _arg(cpu, memory, 2)

    logger.info("nt", f"NtTerminateProcess(handle=0x{process_handle:x}, status=0x{exit_status:x})")
    cpu.regs[EAX] = STATUS_SUCCESS
    cpu.halted = True


# ── Registration ──────────────────────────────────────────────────────────────

def register_nt_handlers(dispatcher: "NtSyscallDispatcher") -> None:
    dispatcher.register(0x116, _nt_write_file)
    dispatcher.register(0x103, _nt_terminate_process)
