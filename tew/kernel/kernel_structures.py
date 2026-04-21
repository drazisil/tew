"""TEB/PEB kernel structure simulation for the x86-32 emulator."""

from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.memory import Memory


@dataclass
class TEBStructure:
    base_address: int
    stack_base: int
    stack_limit: int
    peb_address: int


@dataclass
class PEBStructure:
    base_address: int


class KernelStructures:
    """
    Allocates and manages TEB/PEB structures in emulator memory.
    Sets up the FS segment base so FS:[offset] addressing works.
    """

    def __init__(self, memory: "Memory") -> None:
        self._memory = memory
        self._teb: TEBStructure | None = None
        self._peb: PEBStructure | None = None
        self._fs_base: int = 0

    def initialize_kernel_structures(
        self, stack_base: int, stack_limit: int, process_heap: int = 0
    ) -> None:
        """
        Allocate and initialise TEB/PEB at fixed addresses just below the main executable.
        PEB at 0x00300000, TEB at 0x00320000.
        """
        peb_addr = 0x00300000
        self._peb = PEBStructure(base_address=peb_addr)

        teb_addr = 0x00320000
        self._teb = TEBStructure(
            base_address=teb_addr,
            stack_base=stack_base,
            stack_limit=stack_limit,
            peb_address=peb_addr,
        )
        self._fs_base = teb_addr
        self._write_teb_to_memory()
        self._write_peb_to_memory(process_heap)

    def _write_teb_to_memory(self) -> None:
        if not self._teb:
            return
        teb = self._teb
        addr = teb.base_address

        # NT_TIB portion of TEB (x86-32 layout):
        self._memory.write32(addr + 0x0000, 0xFFFFFFFF)  # ExceptionList (no handler)
        self._memory.write32(addr + 0x0004, teb.stack_base)
        self._memory.write32(addr + 0x0008, teb.stack_limit)
        self._memory.write32(addr + 0x000C, 0)           # SubSystemTib
        self._memory.write32(addr + 0x0010, 0)           # FiberData/Version
        self._memory.write32(addr + 0x0014, 0)           # ArbitraryUserPointer
        self._memory.write32(addr + 0x0018, addr)        # Self (pointer to TEB)
        self._memory.write32(addr + 0x001C, 0)           # EnvironmentPointer
        self._memory.write32(addr + 0x0020, 0x00000004)  # ClientId.ProcessId = 4
        self._memory.write32(addr + 0x0024, 0x00000001)  # ClientId.ThreadId  = 1
        self._memory.write32(addr + 0x0030, teb.peb_address)
        self._memory.write32(addr + 0x0034, 0)           # LastErrorValue

    def _write_peb_to_memory(self, process_heap: int) -> None:
        if not self._peb:
            return
        addr = self._peb.base_address
        self._memory.write32(addr + 0x0018, process_heap)   # ProcessHeap

    def resolve_fs_relative_address(self, offset: int) -> int:
        return (self._fs_base + offset) & 0xFFFFFFFF

    def resolve_gs_relative_address(self, offset: int) -> int:
        return (self._fs_base + offset) & 0xFFFFFFFF

    def get_fs_base(self) -> int:
        return self._fs_base

    def get_gs_base(self) -> int:
        return self._fs_base

    def get_teb(self) -> TEBStructure | None:
        return self._teb

    def get_peb(self) -> PEBStructure | None:
        return self._peb
