"""D3D8 internal helpers: heap allocator, COM stack cleanup, stub registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers

from tew.hardware.cpu import EAX, ESP
from tew.api.d3d8._layout import D3DRES_VTABLE

# ── D3D8 private bump-heap (separate from CRT heap at 0x04000000) ─────────────
_next_heap_addr: int = 0x04800000


def _heap_alloc(size: int) -> int:
    """Bump-allocate from the D3D8 private heap (16-byte aligned)."""
    global _next_heap_addr
    addr = _next_heap_addr
    _next_heap_addr = (_next_heap_addr + size + 15) & ~15
    return addr


def _cleanup_com(cpu: "CPU", memory: "Memory", arg_bytes: int) -> None:
    """stdcall stack cleanup for COM methods (this in ECX, args on stack)."""
    ret_addr = memory.read32(cpu.regs[ESP] & 0xFFFFFFFF)
    cpu.regs[ESP] = (cpu.regs[ESP] + 4 + arg_bytes) & 0xFFFFFFFF
    memory.write32(cpu.regs[ESP], ret_addr)


def _com_stub(
    stubs: "Win32Handlers",
    dll_name: str,
    name: str,
    handler,
    arg_bytes: int,
    memory: "Memory",
) -> int:
    """Register a COM vtable handler and return its trampoline address."""
    def _h(cpu: "CPU") -> None:
        handler(cpu, memory)
        _cleanup_com(cpu, memory, arg_bytes)

    stubs.register_handler(dll_name, name, _h)
    return stubs.get_handler_address(dll_name, name) or 0


def _alloc_resource_obj(data_size: int, memory: "Memory") -> int:
    """Allocate and initialise a generic D3D resource COM object.

    Layout (12 bytes): [0] vtable ptr, [4] data ptr, [8] size.
    """
    data_ptr = _heap_alloc(data_size or 4)
    obj = _heap_alloc(12)
    memory.write32(obj,     D3DRES_VTABLE)
    memory.write32(obj + 4, data_ptr)
    memory.write32(obj + 8, data_size)
    return obj


def _set_eax(cpu: "CPU", value: int) -> None:
    """Set EAX; used as a single-expression handler body."""
    cpu.regs[EAX] = value
