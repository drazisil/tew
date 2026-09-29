"""Shared real-CPU environment for the critical-section tests.

Enter/Leave/TryEnterCriticalSection are guest x86 code (kernel32_sync.py),
so they can only be exercised by actually running them on a real ZigCPU with
the real Win32Handlers dispatch installed -- a fake CPU never executes them.
"""
from __future__ import annotations

from dataclasses import dataclass

from tew.api._state import CRTState
from tew.api.kernel32_io import register_kernel32_io_handlers
from tew.api.kernel32_sync import register_kernel32_sync_handlers
from tew.api.win32_handlers import Win32Handlers
from tew.hardware.cpu_zig import ZigCPU, EAX, ESP
from tew.hardware.memory import Memory
from tew.kernel.kernel_structures import KernelStructures, MAIN_THREAD_ID

# Real THREAD_STACK_BASE (0x08000000) + room for a background thread's stack.
MEM_SIZE   = 0x08100000
# Enough for single-thread use (no background thread stacks).
SMALL_MEM_SIZE = 0x00800000
STACK_TOP  = 0x00100000
HEAP_START = 0x00600000  # RTL_CRITICAL_SECTION_DEBUG blocks come from here
CODE_BASE  = 0x00500000  # per-test guest code
CALL_SITE  = 0x004F0000  # scratch "push arg; call fn; hlt" sequence for call()
CS_ADDR    = 0x00580000
CS_ADDR2   = 0x00580040

# RTL_CRITICAL_SECTION fields
OFF_DEBUG     = 0x00
OFF_LOCK      = 0x04  # LockCount (-1 = free)
OFF_REC       = 0x08  # RecursionCount
OFF_OWNER     = 0x0C  # OwningThread
OFF_SEMAPHORE = 0x10  # LockSemaphore
OFF_SPIN      = 0x14  # SpinCount
LOCK_FREE     = 0xFFFFFFFF
MAIN_TID      = MAIN_THREAD_ID


@dataclass
class CsEnv:
    cpu: ZigCPU
    mem: Memory
    state: CRTState
    stubs: Win32Handlers

    def addr(self, name: str) -> int:
        return self.stubs.lookup_handler_address("kernel32.dll", name)

    def call(self, name: str, *args: int) -> int:
        """Call a kernel32 export stdcall from guest code and run until it
        returns; returns EAX. Asserts the stack was cleaned up."""
        code = bytearray()
        for arg in reversed(args):
            code += b"\x68" + (arg & 0xFFFFFFFF).to_bytes(4, "little")    # push imm32
        rel = (self.addr(name) - (CALL_SITE + len(code) + 5)) & 0xFFFFFFFF
        code += b"\xE8" + rel.to_bytes(4, "little")                      # call
        code += b"\xF4"                                                   # hlt
        self.mem.load(CALL_SITE, bytes(code))
        esp = self.cpu.regs[ESP] = STACK_TOP - 0x100
        self.cpu.eip = CALL_SITE
        self.cpu.halted = False
        self.cpu.run(10_000)
        assert self.cpu.halted, f"{name} did not return (EIP=0x{self.cpu.eip:08x})"
        assert self.cpu.regs[ESP] == esp, f"{name} left ESP unbalanced"
        return self.cpu.regs[EAX]

    def field(self, offset: int, cs: int = CS_ADDR) -> int:
        return self.mem.read32(cs + offset)


def make_cs_env(mem_size: int = MEM_SIZE) -> CsEnv:
    mem = Memory(mem_size)
    cpu = ZigCPU(mem)
    ks = KernelStructures(mem)
    ks.initialize_kernel_structures(stack_base=STACK_TOP, stack_limit=STACK_TOP - 0x10000)
    cpu.kernel_structures = ks
    state = CRTState()
    state.memory = mem
    state.next_heap_alloc = HEAP_START
    stubs = Win32Handlers(mem)
    register_kernel32_sync_handlers(stubs, mem, state)
    register_kernel32_io_handlers(stubs, mem, state)
    stubs.install(cpu)
    return CsEnv(cpu, mem, state, stubs)
