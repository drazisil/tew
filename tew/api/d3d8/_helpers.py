"""D3D8 internal helpers: heap allocator, COM stack cleanup, stub registration."""

from __future__ import annotations

import threading
import time


def vk_pump(fn):
    """Run fn() in a background thread while pumping SDL events in the main thread.

    Mesa's Wayland Vulkan WSI calls wl_display_roundtrip internally for surface
    and swapchain operations. Those roundtrips block until the Wayland compositor
    replies, but SDL owns the event loop and won't dispatch events while we're
    inside a Python callback. Running the Vulkan call on a background thread lets
    the main thread keep pumping events until it completes.
    """
    from sdl2 import SDL_PumpEvents

    result: list = [None]
    exc:    list = [None]

    def _run() -> None:
        try:
            result[0] = fn()
        except Exception as e:
            exc[0] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    while t.is_alive():
        SDL_PumpEvents()
        time.sleep(0.001)
    t.join()

    if exc[0] is not None:
        raise exc[0]
    return result[0]

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers

from tew.hardware.cpu import EAX, ESP
from tew.api.d3d8._layout import D3DRES_VTABLE, D3DSURF_VTABLE, D3DTEX_VTABLE

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
    expected_this: "int | None" = None,
) -> int:
    """Register a COM vtable handler and return its trampoline address."""
    from tew.logger import logger as _logger

    def _h(cpu: "CPU") -> None:
        if expected_this is not None:
            this = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
            if this != expected_this:
                _logger.error(
                    "d3d8",
                    f"{name}: invalid this=0x{this:08x} (expected 0x{expected_this:08x}) — halting",
                )
                cpu.halted = True
                return
        handler(cpu, memory)
        _cleanup_com(cpu, memory, arg_bytes)

    stubs.register_handler(dll_name, name, _h)
    return stubs.get_handler_address(dll_name, name) or 0


def _alloc_resource_obj(data_size: int, memory: "Memory") -> int:
    """Allocate a generic D3D resource COM object (for vertex/index buffers).

    Layout (12 bytes): [0] vtable ptr, [4] data ptr, [8] size.
    """
    data_ptr = _heap_alloc(data_size or 4)
    obj = _heap_alloc(12)
    memory.write32(obj,     D3DRES_VTABLE)
    memory.write32(obj + 4, data_ptr)
    memory.write32(obj + 8, data_size)
    return obj


def _alloc_surface_obj(w: int, h: int, fmt: int, memory: "Memory") -> int:
    """Allocate an IDirect3DSurface8 COM object with stored dimensions and format.

    Layout (24 bytes): [0] vtable ptr, [4] data ptr, [8] size,
                       [12] width, [16] height, [20] D3DFORMAT.
    """
    data_ptr = _heap_alloc((w * h * 4) or 4)
    obj = _heap_alloc(24)
    memory.write32(obj,      D3DSURF_VTABLE)
    memory.write32(obj + 4,  data_ptr)
    memory.write32(obj + 8,  w * h * 4)
    memory.write32(obj + 12, w)
    memory.write32(obj + 16, h)
    memory.write32(obj + 20, fmt)
    return obj


def _alloc_texture_obj(w: int, h: int, fmt: int, levels: int, memory: "Memory") -> int:
    """Allocate an IDirect3DTexture8 COM object with one IDirect3DSurface8 per mip level.

    Layout: [0] vtable ptr, [4] mip0 data ptr, [8] mip0 data size,
            [12] width, [16] height, [20] D3DFORMAT, [24] level_count,
            [28 + i*4] IDirect3DSurface8* for mip level i.

    Levels=0 means "full mip chain" — we allocate 1 level only (no actual mip generation).
    """
    actual_levels = max(levels, 1)
    obj_size = 28 + actual_levels * 4
    obj = _heap_alloc(obj_size)

    # Allocate mip-level surface objects and store their addresses in the texture object.
    for i in range(actual_levels):
        mip_w = max(w >> i, 1)
        mip_h = max(h >> i, 1)
        surf = _alloc_surface_obj(mip_w, mip_h, fmt, memory)
        memory.write32(obj + 28 + i * 4, surf)

    # Populate header fields using mip-0 surface data.
    surf0 = memory.read32((obj + 28) & 0xFFFFFFFF)
    data_ptr  = memory.read32((surf0 + 4) & 0xFFFFFFFF)
    data_size = memory.read32((surf0 + 8) & 0xFFFFFFFF)

    memory.write32(obj,      D3DTEX_VTABLE)
    memory.write32(obj + 4,  data_ptr)
    memory.write32(obj + 8,  data_size)
    memory.write32(obj + 12, w)
    memory.write32(obj + 16, h)
    memory.write32(obj + 20, fmt)
    memory.write32(obj + 24, actual_levels)
    return obj


def _set_eax(cpu: "CPU", value: int) -> None:
    """Set EAX; used as a single-expression handler body."""
    cpu.regs[EAX] = value
