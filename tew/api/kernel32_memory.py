"""kernel32.dll memory handlers — heap and virtual memory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers
    from tew.api._state import CRTState

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import cleanup_stdcall
from tew.logger import logger

# ── Heap flag constants ───────────────────────────────────────────────────────

_HEAP_NO_SERIALIZE          = 0x00000001
_HEAP_ZERO_MEMORY           = 0x00000008
_HEAP_REALLOC_IN_PLACE_ONLY = 0x00000010
_HEAP_KNOWN_ALLOC_FLAGS     = _HEAP_NO_SERIALIZE | _HEAP_ZERO_MEMORY
_HEAP_KNOWN_REALLOC_FLAGS   = _HEAP_NO_SERIALIZE | _HEAP_ZERO_MEMORY | _HEAP_REALLOC_IN_PLACE_ONLY
_HEAP_KNOWN_CREATE_FLAGS    = _HEAP_NO_SERIALIZE

# ── VirtualAlloc flag constants ───────────────────────────────────────────────

_PAGE_SIZE              = 4096
_MEM_COMMIT             = 0x00001000
_MEM_RESERVE            = 0x00002000
_PAGE_READWRITE         = 0x04
_PAGE_EXECUTE_READWRITE = 0x40
_KNOWN_PROTECT_FLAGS    = _PAGE_READWRITE | _PAGE_EXECUTE_READWRITE
_KNOWN_ALLOC_TYPES      = _MEM_COMMIT | _MEM_RESERVE


def register_kernel32_memory_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register heap and virtual memory handlers."""

    # ── Heap management ───────────────────────────────────────────────────────

    def _heap_create(cpu: "CPU") -> None:
        fl = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        unsupported = fl & ~_HEAP_KNOWN_CREATE_FLAGS
        if unsupported:
            logger.error("handlers",
                f"[UNIMPLEMENTED] HeapCreate — unsupported flag(s) 0x{unsupported:x} — halting")
            cpu.halted = True
            return
        h = state.next_heap_handle
        state.next_heap_handle += 1
        state.heap_handles.add(h)
        cpu.regs[EAX] = h
        cleanup_stdcall(cpu, memory, 12)

    def _get_process_heap(cpu: "CPU") -> None:
        cpu.regs[EAX] = state.process_heap

    def _heap_alloc(cpu: "CPU") -> None:
        h_heap   = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        dw_bytes = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if h_heap not in state.heap_handles:
            logger.error("handlers", f"[HeapAlloc] invalid heap 0x{h_heap:x} — halting")
            cpu.halted = True
            return
        unsupported = dw_flags & ~_HEAP_KNOWN_ALLOC_FLAGS
        if unsupported:
            logger.error("handlers",
                f"[UNIMPLEMENTED] HeapAlloc — unsupported flags 0x{unsupported:x} — halting")
            cpu.halted = True
            return
        size = dw_bytes or 1
        addr = state.simple_alloc(size)
        state.heap_alloc_owner[addr] = h_heap
        if dw_flags & _HEAP_ZERO_MEMORY:
            for i in range(size):
                memory.write8(addr + i, 0)
        cpu.regs[EAX] = addr
        cleanup_stdcall(cpu, memory, 12)

    def _heap_free(cpu: "CPU") -> None:
        h_heap   = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        lp_mem   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if h_heap not in state.heap_handles:
            logger.error("handlers", f"[HeapFree] invalid heap 0x{h_heap:x} — halting")
            cpu.halted = True
            return
        unsupported = dw_flags & ~_HEAP_NO_SERIALIZE
        if unsupported:
            logger.error("handlers",
                f"[UNIMPLEMENTED] HeapFree — unsupported flags 0x{unsupported:x} — halting")
            cpu.halted = True
            return
        if lp_mem == 0:
            cpu.regs[EAX] = 1
            cleanup_stdcall(cpu, memory, 12)
            return
        if lp_mem not in state.heap_alloc_sizes:
            logger.error("handlers", f"[HeapFree] untracked pointer 0x{lp_mem:x} — halting")
            cpu.halted = True
            return
        del state.heap_alloc_sizes[lp_mem]
        state.heap_alloc_owner.pop(lp_mem, None)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 12)

    def _heap_realloc(cpu: "CPU") -> None:
        h_heap   = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        lp_mem   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        dw_bytes = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        if h_heap not in state.heap_handles:
            logger.error("handlers", f"[HeapReAlloc] invalid heap 0x{h_heap:x} — halting")
            cpu.halted = True
            return
        unsupported = dw_flags & ~_HEAP_KNOWN_REALLOC_FLAGS
        if unsupported:
            logger.error("handlers",
                f"[UNIMPLEMENTED] HeapReAlloc — unsupported flags 0x{unsupported:x} — halting")
            cpu.halted = True
            return
        if dw_flags & _HEAP_REALLOC_IN_PLACE_ONLY:
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 16)
            return
        old_size = state.heap_alloc_sizes.get(lp_mem, 0) if lp_mem != 0 else 0
        if lp_mem != 0 and lp_mem not in state.heap_alloc_sizes:
            logger.error("handlers", f"[HeapReAlloc] untracked pointer 0x{lp_mem:x} — halting")
            cpu.halted = True
            return
        new_size = dw_bytes or 1
        new_addr = state.simple_alloc(new_size)
        state.heap_alloc_owner[new_addr] = h_heap
        copy_len = min(old_size, new_size)
        for i in range(copy_len):
            memory.write8(new_addr + i, memory.read8(lp_mem + i))
        if (dw_flags & _HEAP_ZERO_MEMORY) and new_size > old_size:
            for i in range(old_size, new_size):
                memory.write8(new_addr + i, 0)
        if lp_mem != 0:
            state.heap_alloc_sizes.pop(lp_mem, None)
            state.heap_alloc_owner.pop(lp_mem, None)
        cpu.regs[EAX] = new_addr
        cleanup_stdcall(cpu, memory, 16)

    def _heap_size(cpu: "CPU") -> None:
        h_heap   = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        lp_mem   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if h_heap not in state.heap_handles:
            logger.error("handlers", f"[HeapSize] invalid heap 0x{h_heap:x} — halting")
            cpu.halted = True
            return
        unsupported = dw_flags & ~_HEAP_NO_SERIALIZE
        if unsupported:
            logger.error("handlers",
                f"[UNIMPLEMENTED] HeapSize — unsupported flags 0x{unsupported:x} — halting")
            cpu.halted = True
            return
        sz = state.heap_alloc_sizes.get(lp_mem)
        cpu.regs[EAX] = sz if sz is not None else 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 12)

    def _heap_validate(cpu: "CPU") -> None:
        h_heap  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        # dw_flags (ESP+8) and lp_mem (ESP+12) are intentionally unused:
        # our bump allocator has no fragmentation or corruption to check.
        if h_heap not in state.heap_handles:
            logger.error("handlers", f"[HeapValidate] invalid heap 0x{h_heap:x} — halting")
            cpu.halted = True
            return
        cpu.regs[EAX] = 1  # TRUE — heap is always valid
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("kernel32.dll", "HeapCreate",     _heap_create)
    stubs.register_handler("kernel32.dll", "GetProcessHeap", _get_process_heap)
    stubs.register_handler("kernel32.dll", "HeapAlloc",      _heap_alloc)
    stubs.register_handler("kernel32.dll", "HeapFree",       _heap_free)
    stubs.register_handler("kernel32.dll", "HeapReAlloc",    _heap_realloc)
    stubs.register_handler("kernel32.dll", "HeapSize",       _heap_size)
    stubs.register_handler("kernel32.dll", "HeapValidate",   _heap_validate)

    # ── VirtualAlloc / VirtualFree ────────────────────────────────────────────

    def _virtual_alloc(cpu: "CPU") -> None:
        lp_addr  = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_size  = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        fl_type  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        fl_prot  = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        unk_type = fl_type & ~_KNOWN_ALLOC_TYPES
        if unk_type:
            logger.error("handlers",
                f"[UNIMPLEMENTED] VirtualAlloc — unsupported flAllocationType 0x{unk_type:x} — halting")
            cpu.halted = True
            return
        unk_prot = fl_prot & ~_KNOWN_PROTECT_FLAGS
        if unk_prot:
            logger.error("handlers",
                f"[UNIMPLEMENTED] VirtualAlloc — unsupported flProtect 0x{unk_prot:x} — halting")
            cpu.halted = True
            return
        page_size = ((dw_size + _PAGE_SIZE - 1) & ~(_PAGE_SIZE - 1)) & 0xFFFFFFFF
        if (fl_type & _MEM_COMMIT) and not (fl_type & _MEM_RESERVE):
            if lp_addr == 0:
                logger.error("handlers", "[VirtualAlloc] MEM_COMMIT with NULL address — halting")
                cpu.halted = True
                return
            in_reserved = any(
                base <= lp_addr < base + sz
                for base, sz in state.virtual_reserved.items()
            )
            if not in_reserved:
                logger.error("handlers",
                    f"[VirtualAlloc] MEM_COMMIT on unreserved 0x{lp_addr:x} — halting")
                cpu.halted = True
                return
            state.virtual_committed[lp_addr] = page_size
            cpu.regs[EAX] = lp_addr
            cleanup_stdcall(cpu, memory, 16)
            return
        if lp_addr != 0:
            addr = lp_addr
            end = (lp_addr + page_size + _PAGE_SIZE - 1) & ~(_PAGE_SIZE - 1)
            if end > state.next_virtual_alloc:
                state.next_virtual_alloc = end & 0xFFFFFFFF
        else:
            addr = state.next_virtual_alloc
            state.next_virtual_alloc = (
                (state.next_virtual_alloc + page_size + _PAGE_SIZE - 1) & ~(_PAGE_SIZE - 1)
            ) & 0xFFFFFFFF
        if fl_type & _MEM_RESERVE:
            state.virtual_reserved[addr] = page_size
        if fl_type & _MEM_COMMIT:
            state.virtual_committed[addr] = page_size
        cpu.regs[EAX] = addr
        cleanup_stdcall(cpu, memory, 16)

    def _virtual_free(cpu: "CPU") -> None:
        lp_addr  = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_size  = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        dw_type  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        MEM_DECOMMIT = 0x4000
        MEM_RELEASE  = 0x8000
        if dw_type == MEM_RELEASE:
            if dw_size != 0:
                logger.error("handlers", "[VirtualFree] MEM_RELEASE requires dwSize=0 — halting")
                cpu.halted = True
                return
            if lp_addr not in state.virtual_reserved:
                logger.error("handlers",
                    f"[VirtualFree] MEM_RELEASE on unreserved 0x{lp_addr:x} — halting")
                cpu.halted = True
                return
            del state.virtual_reserved[lp_addr]
            state.virtual_committed.pop(lp_addr, None)
        elif dw_type == MEM_DECOMMIT:
            state.virtual_committed.pop(lp_addr, None)
        else:
            logger.error("handlers",
                f"[UNIMPLEMENTED] VirtualFree — unsupported type 0x{dw_type:x} — halting")
            cpu.halted = True
            return
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("kernel32.dll", "VirtualAlloc", _virtual_alloc)
    stubs.register_handler("kernel32.dll", "VirtualFree",  _virtual_free)
