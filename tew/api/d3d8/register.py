"""register_d3d8_handlers — wires all D3D8 COM vtables into emulator memory.

This module is purely wiring: it calls make_vtable() on each interface module,
writes the trampoline addresses into the correct memory regions, and registers
the d3d8.dll DLL-level exports.  No handler logic lives here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers
    from tew.api._state import CRTState

from tew.logger import logger
from tew.api.d3d8._layout import (
    D3D8_OBJ, D3D8_VTABLE,
    D3DDEV_OBJ, D3DDEV_VTABLE,
    D3DRES_VTABLE,
    S_OK,
)
from tew.api.d3d8._helpers import _cleanup_com, _set_eax
from tew.api.d3d8.idirect3d8resource import make_vtable as _make_res_vtable
from tew.api.d3d8.idirect3d8 import make_vtable as _make_d3d8_vtable, make_create8
from tew.api.d3d8.idirect3d8device import make_vtable as _make_dev_vtable


def register_d3d8_handlers(stubs: "Win32Handlers", memory: "Memory", state: "CRTState") -> None:
    """Register all D3D8 COM stubs and write vtable pointers into memory."""

    # ── Generic resource vtable ───────────────────────────────────────────────
    res_vtable = _make_res_vtable(stubs, memory)
    for i, addr in enumerate(res_vtable):
        memory.write32(D3DRES_VTABLE + i * 4, addr)

    # ── IDirect3D8 vtable + object ────────────────────────────────────────────
    d3d8_vtable = _make_d3d8_vtable(stubs, memory, state.window_manager)
    for i, addr in enumerate(d3d8_vtable):
        memory.write32(D3D8_VTABLE + i * 4, addr)
        logger.trace("d3d8", f"  IDirect3D8 vtable[{i}] @ 0x{D3D8_VTABLE + i * 4:08x} = 0x{addr:08x}")
    memory.write32(D3D8_OBJ, D3D8_VTABLE)

    # ── IDirect3DDevice8 vtable + object ──────────────────────────────────────
    dev_vtable = _make_dev_vtable(stubs, memory)
    for i, addr in enumerate(dev_vtable):
        memory.write32(D3DDEV_VTABLE + i * 4, addr or 0)
    memory.write32(D3DDEV_OBJ, D3DDEV_VTABLE)

    # ── d3d8.dll DLL-level exports ────────────────────────────────────────────

    # Direct3DCreate8(SDKVersion) -> IDirect3D8*  [stdcall, 1 DWORD arg]
    stubs.register_handler("d3d8.dll", "Direct3DCreate8", make_create8(memory))

    # DebugSetMute(bMute)  — silences D3D debug output; no-op here
    def _debug_set_mute(cpu: "CPU") -> None:  # type: ignore[name-defined]
        _cleanup_com(cpu, memory, 4)

    stubs.register_handler("d3d8.dll", "DebugSetMute", _debug_set_mute)

    # Direct3D8EnableMaximizedWindowedModeShim(bEnable) -> BOOL
    def _enable_maximized_windowed_shim(cpu: "CPU") -> None:  # type: ignore[name-defined]
        _set_eax(cpu, 0)  # FALSE (shim not active)
        _cleanup_com(cpu, memory, 4)

    stubs.register_handler(
        "d3d8.dll",
        "Direct3D8EnableMaximizedWindowedModeShim",
        _enable_maximized_windowed_shim,
    )

    # ValidatePixelShader(pPixelShader, pCaps, bReturn, pErrorString)
    def _validate_pixel_shader(cpu: "CPU") -> None:  # type: ignore[name-defined]
        _set_eax(cpu, S_OK)
        _cleanup_com(cpu, memory, 16)

    stubs.register_handler("d3d8.dll", "ValidatePixelShader", _validate_pixel_shader)

    # ValidateVertexShader(pVertexShader, pVertexDecl, pCaps, bReturn, pErrorString)
    def _validate_vertex_shader(cpu: "CPU") -> None:  # type: ignore[name-defined]
        _set_eax(cpu, S_OK)
        _cleanup_com(cpu, memory, 16)

    stubs.register_handler("d3d8.dll", "ValidateVertexShader", _validate_vertex_shader)

    logger.info("d3d8", "D3D8 handlers registered — COM vtables wired, Vulkan initialised on Direct3DCreate8")
    logger.trace("d3d8", f"  IDirect3D8       @ 0x{D3D8_OBJ:08x} (vtable @ 0x{D3D8_VTABLE:08x})")
    logger.trace("d3d8", f"  IDirect3DDevice8 @ 0x{D3DDEV_OBJ:08x} (vtable @ 0x{D3DDEV_VTABLE:08x})")
