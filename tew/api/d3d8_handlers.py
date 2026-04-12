"""D3D8Handlers — Fake IDirect3D8 + IDirect3DDevice8 COM objects.

Architecture:
  - IDirect3D8 object at 0x00220040 (vtable at 0x00220000, 16 methods)
  - IDirect3DDevice8 object at 0x00220200 (vtable at 0x00220050, 97 methods)
  - Generic resource vtable at 0x00220210 (18 methods)
  - Resource objects are allocated from a bump heap starting at 0x04800000

Calling convention: COM __thiscall — "this" in ECX, args on stack.
_cleanup_com handles stdcall-style stack cleanup (does not touch ECX).

Phase 1: All methods return S_OK / safe values. Lock() returns a real
emulator heap pointer so the game can write vertex data without crashing.
No actual rendering yet — that is Phase 2 (WebGL).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers

from tew.hardware.cpu import EAX, ECX, ESP
from tew.api.win32_handlers import cleanup_stdcall
from tew.logger import logger

# ── Memory layout for fake COM objects ───────────────────────────────────────
# IMPORTANT: Must be ABOVE the stub trampoline region (0x00200000–0x0021FFFF).
# MAX_HANDLERS=4096 × HANDLER_SIZE=32 = 128 KB → stubs end at 0x00220000.
# We place vtable data at 0x00220000+ so stub trampoline writes never collide.
D3D8_VTABLE   = 0x00220000  # IDirect3D8 vtable (16×4 = 64 bytes) → 0x00220040
D3D8_OBJ      = 0x00220040  # IDirect3D8 object (vtable ptr, 4 bytes)
D3DDEV_VTABLE = 0x00220050  # IDirect3DDevice8 vtable (97×4 = 388 bytes) → 0x002201D8
D3DDEV_OBJ    = 0x00220200  # IDirect3DDevice8 object (vtable ptr, 4 bytes)
D3DRES_VTABLE = 0x00220210  # Generic resource vtable (18×4 = 72 bytes) → 0x00220258
# Resource objects are allocated from the bump heap at 0x04800000+

# ── D3D error codes ──────────────────────────────────────────────────────────
S_OK             = 0x00000000
D3DERR_NOTAVAIL  = 0x8876086A

# ── Heap bump allocator (separate from main CRT heap at 0x04000000) ──────────
# Starts slightly above Win32Handlers heap region to avoid collision.
_next_heap_addr: int = 0x04800000


def _heap_alloc(size: int) -> int:
    """Bump-allocate from the D3D8 private heap (16-byte aligned)."""
    global _next_heap_addr
    addr = _next_heap_addr
    _next_heap_addr = (_next_heap_addr + size + 15) & ~15
    return addr


# ── COM stack cleanup ─────────────────────────────────────────────────────────

def _cleanup_com(cpu: "CPU", memory: "Memory", arg_bytes: int) -> None:
    """stdcall stack cleanup for COM methods (this in ECX, args on stack)."""
    ret_addr = memory.read32(cpu.regs[ESP] & 0xFFFFFFFF)
    cpu.regs[ESP] = (cpu.regs[ESP] + 4 + arg_bytes) & 0xFFFFFFFF
    memory.write32(cpu.regs[ESP], ret_addr)


# ── COM stub helper ───────────────────────────────────────────────────────────

def _com_stub(
    stubs: "Win32Handlers",
    dll_name: str,
    name: str,
    handler,
    arg_bytes: int,
    memory: "Memory",
) -> int:
    """Register a COM vtable stub and return its trampoline address."""
    def _h(cpu: "CPU") -> None:
        handler(cpu, memory)
        _cleanup_com(cpu, memory, arg_bytes)

    stubs.register_handler(dll_name, name, _h)
    return stubs.get_handler_address(dll_name, name) or 0


# ── Generic resource object ───────────────────────────────────────────────────
# Layout (12 bytes): [0] vtable ptr, [4] data ptr, [8] size

def _alloc_resource_obj(data_size: int, memory: "Memory") -> int:
    """Allocate and initialise a generic D3D resource COM object."""
    data_ptr = _heap_alloc(data_size or 4)
    obj = _heap_alloc(12)
    memory.write32(obj,     D3DRES_VTABLE)  # vtable ptr
    memory.write32(obj + 4, data_ptr)        # data ptr
    memory.write32(obj + 8, data_size)       # size
    return obj


# ── D3DCAPS8 struct fill-in (308 bytes) ──────────────────────────────────────

def _fill_d3d_caps8(p_caps: int, memory: "Memory") -> None:
    """Write fake D3DCAPS8 struct into emulator memory at p_caps."""
    if not p_caps:
        return
    # Zero the whole struct first
    for i in range(0, 308, 4):
        memory.write32(p_caps + i, 0)
    memory.write32(p_caps + 0,   1)           # DeviceType = D3DDEVTYPE_HAL
    memory.write32(p_caps + 4,   0)           # AdapterOrdinal
    memory.write32(p_caps + 8,   0x00000040)  # Caps = D3DCAPS_READ_SCANLINE
    memory.write32(p_caps + 12,  0x00020000)  # Caps2 = D3DCAPS2_DYNAMICTEXTURES
    memory.write32(p_caps + 16,  0x00000020)  # Caps3 = D3DCAPS3_ALPHA_FULLSCREEN_FLIP_OR_DISCARD
    memory.write32(p_caps + 20,  0x00000002)  # PresentationIntervals = D3DPRESENT_INTERVAL_ONE
    memory.write32(p_caps + 84,  2048)         # MaxTextureWidth
    memory.write32(p_caps + 88,  2048)         # MaxTextureHeight
    memory.write32(p_caps + 96,  0xFFFFFFFF)  # TextureCaps (accept all)
    memory.write32(p_caps + 204, 0x1FFFFF)    # MaxPrimitiveCount
    memory.write32(p_caps + 208, 0xFFFF)      # MaxVertexIndex
    memory.write32(p_caps + 212, 8)            # MaxStreams
    memory.write32(p_caps + 216, 256)          # MaxStreamStride
    memory.write32(p_caps + 220, 0xFFFE0000)  # VertexShaderVersion = VS 1.1
    memory.write32(p_caps + 224, 256)          # MaxVertexShaderConst
    memory.write32(p_caps + 228, 0xFFFF0100)  # PixelShaderVersion = PS 1.0


# ── D3DADAPTER_IDENTIFIER8 fill-in (~1256 bytes) ─────────────────────────────

def _fill_adapter_identifier(p_ident: int, memory: "Memory") -> None:
    """Write fake D3DADAPTER_IDENTIFIER8 struct into emulator memory."""
    if not p_ident:
        return

    def _write_str(offset: int, s: str) -> None:
        for i, ch in enumerate(s[:511]):
            memory.write8(p_ident + offset + i, ord(ch))
        memory.write8(p_ident + offset + min(len(s), 511), 0)

    _write_str(0,   "NVIDIA GeForce4 Ti 4200")   # Driver (512 bytes)
    _write_str(512, "NVIDIA GeForce4 Ti 4200")   # Description (512 bytes)
    # DeviceIdentifier GUID (16 bytes at offset 1024): leave zero
    memory.write32(p_ident + 1040, 6)  # WHQLLevel


# ── register_d3d8_handlers ────────────────────────────────────────────────────

def register_d3d8_handlers(stubs: "Win32Handlers", memory: "Memory") -> None:
    """Register all D3D8 COM stubs and write vtable pointers into memory."""

    # ── Generic resource vtable (IUnknown + resource methods) ────────────────
    res_vtable: list[int] = [0] * 18

    # [0] QueryInterface(REFIID, void**)
    res_vtable[0] = _com_stub(stubs, "d3d8res", "Res::QueryInterface",
        lambda cpu, mem: _set_eax(cpu, 0x80004002), 8, memory)
    # [1] AddRef()
    res_vtable[1] = _com_stub(stubs, "d3d8res", "Res::AddRef",
        lambda cpu, mem: _set_eax(cpu, 1), 0, memory)
    # [2] Release()
    res_vtable[2] = _com_stub(stubs, "d3d8res", "Res::Release",
        lambda cpu, mem: _set_eax(cpu, 0), 0, memory)
    # [3] GetDevice(IDirect3DDevice8**)
    def _res_get_device(cpu: "CPU", mem: "Memory") -> None:
        pp_device = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if pp_device:
            mem.write32(pp_device, D3DDEV_OBJ)
        cpu.regs[EAX] = S_OK
    res_vtable[3] = _com_stub(stubs, "d3d8res", "Res::GetDevice",
        _res_get_device, 4, memory)
    # [4] SetPrivateData(REFGUID, pData, SizeOfData, Flags)
    res_vtable[4] = _com_stub(stubs, "d3d8res", "Res::SetPrivateData",
        lambda cpu, mem: _set_eax(cpu, S_OK), 16, memory)
    # [5] GetPrivateData(REFGUID, pData, pSizeOfData)
    res_vtable[5] = _com_stub(stubs, "d3d8res", "Res::GetPrivateData",
        lambda cpu, mem: _set_eax(cpu, S_OK), 12, memory)
    # [6] FreePrivateData(REFGUID)
    res_vtable[6] = _com_stub(stubs, "d3d8res", "Res::FreePrivateData",
        lambda cpu, mem: _set_eax(cpu, S_OK), 16, memory)
    # [7] SetPriority(PriorityNew) -> DWORD (old priority)
    res_vtable[7] = _com_stub(stubs, "d3d8res", "Res::SetPriority",
        lambda cpu, mem: _set_eax(cpu, 0), 4, memory)
    # [8] GetPriority() -> DWORD
    res_vtable[8] = _com_stub(stubs, "d3d8res", "Res::GetPriority",
        lambda cpu, mem: _set_eax(cpu, 0), 0, memory)
    # [9] PreLoad()
    res_vtable[9] = _com_stub(stubs, "d3d8res", "Res::PreLoad",
        lambda cpu, mem: None, 0, memory)
    # [10] GetType() -> D3DRESOURCETYPE
    res_vtable[10] = _com_stub(stubs, "d3d8res", "Res::GetType",
        lambda cpu, mem: _set_eax(cpu, 0), 0, memory)
    # [11] Surface::GetContainer(REFIID, void**)
    res_vtable[11] = _com_stub(stubs, "d3d8res", "Surface::GetContainer",
        lambda cpu, mem: _set_eax(cpu, D3DERR_NOTAVAIL), 8, memory)
    # [12] Surface::GetDesc(D3DSURFACE_DESC*)
    res_vtable[12] = _com_stub(stubs, "d3d8res", "Surface::GetDesc",
        lambda cpu, mem: _set_eax(cpu, S_OK), 4, memory)
    # [13] Surface::LockRect(D3DLOCKED_RECT*, CONST RECT*, DWORD)
    def _surface_lock_rect(cpu: "CPU", mem: "Memory") -> None:
        this_ptr = cpu.regs[ECX]
        p_locked  = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        data_ptr  = mem.read32((this_ptr + 4) & 0xFFFFFFFF)
        if p_locked:
            mem.write32(p_locked,     800 * 4)   # Pitch (800 px wide × 4 bpp)
            mem.write32(p_locked + 4, data_ptr)  # pBits
        cpu.regs[EAX] = S_OK
    res_vtable[13] = _com_stub(stubs, "d3d8res", "Surface::LockRect",
        _surface_lock_rect, 12, memory)
    # [14] Surface::UnlockRect()
    res_vtable[14] = _com_stub(stubs, "d3d8res", "Surface::UnlockRect",
        lambda cpu, mem: _set_eax(cpu, S_OK), 0, memory)
    # [15] Buffer::GetDesc(void*)
    res_vtable[15] = _com_stub(stubs, "d3d8res", "Buffer::GetDesc",
        lambda cpu, mem: _set_eax(cpu, S_OK), 4, memory)
    # [16] Buffer::Lock(OffsetToLock, SizeToLock, BYTE** ppbData, Flags)
    def _buffer_lock(cpu: "CPU", mem: "Memory") -> None:
        this_ptr = cpu.regs[ECX]
        ppb_data  = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        data_ptr  = mem.read32((this_ptr + 4) & 0xFFFFFFFF)
        if ppb_data:
            mem.write32(ppb_data, data_ptr)
        cpu.regs[EAX] = S_OK
    res_vtable[16] = _com_stub(stubs, "d3d8res", "Buffer::Lock",
        _buffer_lock, 16, memory)
    # [17] Buffer::Unlock()
    res_vtable[17] = _com_stub(stubs, "d3d8res", "Buffer::Unlock",
        lambda cpu, mem: _set_eax(cpu, S_OK), 0, memory)

    # Write generic resource vtable into memory
    for i, addr in enumerate(res_vtable):
        memory.write32(D3DRES_VTABLE + i * 4, addr)

    # ── IDirect3D8 vtable (16 methods) ───────────────────────────────────────

    # [0] QueryInterface(REFIID, void**)
    def _d3d8_query_interface(cpu: "CPU", mem: "Memory") -> None:
        cpu.regs[EAX] = 0x80004002  # E_NOINTERFACE
    # [5] GetAdapterIdentifier(Adapter, Flags, D3DADAPTER_IDENTIFIER8*)
    def _d3d8_get_adapter_identifier(cpu: "CPU", mem: "Memory") -> None:
        p_ident = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        _fill_adapter_identifier(p_ident, mem)
        cpu.regs[EAX] = S_OK
    # [7] EnumAdapterModes(Adapter, Mode, D3DDISPLAYMODE*)
    def _d3d8_enum_adapter_modes(cpu: "CPU", mem: "Memory") -> None:
        p_mode = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if p_mode:
            mem.write32(p_mode,       800)   # Width
            mem.write32(p_mode + 4,   600)   # Height
            mem.write32(p_mode + 8,   60)    # RefreshRate
            mem.write32(p_mode + 12,  0x16)  # Format = D3DFMT_X8R8G8B8
        cpu.regs[EAX] = S_OK
    # [8] GetAdapterDisplayMode(Adapter, D3DDISPLAYMODE*)
    def _d3d8_get_adapter_display_mode(cpu: "CPU", mem: "Memory") -> None:
        p_mode = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_mode:
            mem.write32(p_mode,       800)
            mem.write32(p_mode + 4,   600)
            mem.write32(p_mode + 8,   60)
            mem.write32(p_mode + 12,  0x16)
        cpu.regs[EAX] = S_OK
    # [13] GetDeviceCaps(Adapter, DevType, D3DCAPS8*)
    def _d3d8_get_device_caps(cpu: "CPU", mem: "Memory") -> None:
        p_caps = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        _fill_d3d_caps8(p_caps, mem)
        cpu.regs[EAX] = S_OK
    # [15] CreateDevice(Adapter, DevType, hFocusWindow, BehaviorFlags, D3DPRESENT_PARAMETERS*, IDirect3DDevice8**)
    def _d3d8_create_device(cpu: "CPU", mem: "Memory") -> None:
        pp_device = mem.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        if pp_device:
            mem.write32(pp_device, D3DDEV_OBJ)
        logger.debug("d3d8", f"IDirect3D8::CreateDevice -> 0x{D3DDEV_OBJ:08x}")
        cpu.regs[EAX] = S_OK

    d3d8_methods: list[int] = [
        # [0] QueryInterface
        _com_stub(stubs, "d3d8", "IDirect3D8::QueryInterface",
            _d3d8_query_interface, 8, memory),
        # [1] AddRef
        _com_stub(stubs, "d3d8", "IDirect3D8::AddRef",
            lambda cpu, mem: _set_eax(cpu, 1), 0, memory),
        # [2] Release
        _com_stub(stubs, "d3d8", "IDirect3D8::Release",
            lambda cpu, mem: _set_eax(cpu, 0), 0, memory),
        # [3] RegisterSoftwareDevice(void*)
        _com_stub(stubs, "d3d8", "IDirect3D8::RegisterSoftwareDevice",
            lambda cpu, mem: _set_eax(cpu, D3DERR_NOTAVAIL), 4, memory),
        # [4] GetAdapterCount() -> UINT
        _com_stub(stubs, "d3d8", "IDirect3D8::GetAdapterCount",
            lambda cpu, mem: _set_eax(cpu, 1), 0, memory),
        # [5] GetAdapterIdentifier(Adapter, Flags, D3DADAPTER_IDENTIFIER8*)
        _com_stub(stubs, "d3d8", "IDirect3D8::GetAdapterIdentifier",
            _d3d8_get_adapter_identifier, 12, memory),
        # [6] GetAdapterModeCount(Adapter) -> UINT
        _com_stub(stubs, "d3d8", "IDirect3D8::GetAdapterModeCount",
            lambda cpu, mem: _set_eax(cpu, 1), 4, memory),
        # [7] EnumAdapterModes(Adapter, Mode, D3DDISPLAYMODE*)
        _com_stub(stubs, "d3d8", "IDirect3D8::EnumAdapterModes",
            _d3d8_enum_adapter_modes, 12, memory),
        # [8] GetAdapterDisplayMode(Adapter, D3DDISPLAYMODE*)
        _com_stub(stubs, "d3d8", "IDirect3D8::GetAdapterDisplayMode",
            _d3d8_get_adapter_display_mode, 8, memory),
        # [9] CheckDeviceType(Adapter, CheckType, DisplayFmt, BackFmt, Windowed)
        _com_stub(stubs, "d3d8", "IDirect3D8::CheckDeviceType",
            lambda cpu, mem: _set_eax(cpu, S_OK), 20, memory),
        # [10] CheckDeviceFormat(Adapter, DevType, AdapterFmt, Usage, RType, CheckFmt)
        _com_stub(stubs, "d3d8", "IDirect3D8::CheckDeviceFormat",
            lambda cpu, mem: _set_eax(cpu, S_OK), 24, memory),
        # [11] CheckDeviceMultiSampleType(Adapter, DevType, SurfaceFmt, Windowed, MultiSampleType)
        _com_stub(stubs, "d3d8", "IDirect3D8::CheckDeviceMultiSampleType",
            lambda cpu, mem: _set_eax(cpu, D3DERR_NOTAVAIL), 20, memory),
        # [12] CheckDepthStencilMatch(Adapter, DevType, AdapterFmt, RTFmt, DSFmt)
        _com_stub(stubs, "d3d8", "IDirect3D8::CheckDepthStencilMatch",
            lambda cpu, mem: _set_eax(cpu, S_OK), 20, memory),
        # [13] GetDeviceCaps(Adapter, DevType, D3DCAPS8*)
        _com_stub(stubs, "d3d8", "IDirect3D8::GetDeviceCaps",
            _d3d8_get_device_caps, 12, memory),
        # [14] GetAdapterMonitor(Adapter) -> HMONITOR
        _com_stub(stubs, "d3d8", "IDirect3D8::GetAdapterMonitor",
            lambda cpu, mem: _set_eax(cpu, 0x0D3D0001), 4, memory),
        # [15] CreateDevice(Adapter, DevType, hFocusWindow, BehaviorFlags, D3DPRESENT_PARAMETERS*, IDirect3DDevice8**)
        _com_stub(stubs, "d3d8", "IDirect3D8::CreateDevice",
            _d3d8_create_device, 24, memory),
    ]

    # Write IDirect3D8 vtable into memory
    for i, addr in enumerate(d3d8_methods):
        memory.write32(D3D8_VTABLE + i * 4, addr)
    # Write IDirect3D8 object (first DWORD = vtable ptr)
    memory.write32(D3D8_OBJ, D3D8_VTABLE)

    # ── IDirect3DDevice8 vtable (97 methods) ─────────────────────────────────
    # Helpers for common patterns
    def _dev_ok(name: str, arg_bytes: int) -> int:
        return _com_stub(stubs, "d3d8dev", name,
            lambda cpu, mem: _set_eax(cpu, S_OK), arg_bytes, memory)

    def _dev_void(name: str, arg_bytes: int) -> int:
        return _com_stub(stubs, "d3d8dev", name,
            lambda cpu, mem: None, arg_bytes, memory)

    def _dev_uint(name: str, arg_bytes: int, val: int) -> int:
        return _com_stub(stubs, "d3d8dev", name,
            lambda cpu, mem: _set_eax(cpu, val), arg_bytes, memory)

    dev_methods: list[int] = [0] * 97

    # IUnknown
    dev_methods[0] = _com_stub(stubs, "d3d8dev", "Dev::QueryInterface",
        lambda cpu, mem: _set_eax(cpu, 0x80004002), 8, memory)
    dev_methods[1] = _dev_uint("Dev::AddRef",  0, 1)
    dev_methods[2] = _dev_uint("Dev::Release", 0, 0)

    # IDirect3DDevice8
    dev_methods[3]  = _dev_ok  ("Dev::TestCooperativeLevel",        0)
    dev_methods[4]  = _dev_uint("Dev::GetAvailableTextureMem",      0, 128 * 1024 * 1024)
    dev_methods[5]  = _dev_ok  ("Dev::ResourceManagerDiscardBytes", 4)

    # [6] GetDirect3D(IDirect3D8** ppD3D8)
    def _dev_get_direct3d(cpu: "CPU", mem: "Memory") -> None:
        pp = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if pp:
            mem.write32(pp, D3D8_OBJ)
        cpu.regs[EAX] = S_OK
    dev_methods[6] = _com_stub(stubs, "d3d8dev", "Dev::GetDirect3D",
        _dev_get_direct3d, 4, memory)

    # [7] GetDeviceCaps(D3DCAPS8*)
    def _dev_get_device_caps(cpu: "CPU", mem: "Memory") -> None:
        p_caps = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        _fill_d3d_caps8(p_caps, mem)
        cpu.regs[EAX] = S_OK
    dev_methods[7] = _com_stub(stubs, "d3d8dev", "Dev::GetDeviceCaps",
        _dev_get_device_caps, 4, memory)

    # [8] GetDisplayMode(D3DDISPLAYMODE*)
    def _dev_get_display_mode(cpu: "CPU", mem: "Memory") -> None:
        p_mode = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if p_mode:
            mem.write32(p_mode,       800)
            mem.write32(p_mode + 4,   600)
            mem.write32(p_mode + 8,   60)
            mem.write32(p_mode + 12,  0x16)
        cpu.regs[EAX] = S_OK
    dev_methods[8] = _com_stub(stubs, "d3d8dev", "Dev::GetDisplayMode",
        _dev_get_display_mode, 4, memory)

    # [9] GetCreationParameters(D3DDEVICE_CREATION_PARAMETERS*)
    def _dev_get_creation_params(cpu: "CPU", mem: "Memory") -> None:
        p = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if p:
            mem.write32(p,      0)       # AdapterOrdinal
            mem.write32(p + 4,  1)       # DeviceType = D3DDEVTYPE_HAL
            mem.write32(p + 8,  0xABCD)  # hFocusWindow (fake HWND)
            mem.write32(p + 12, 0x40)    # BehaviorFlags = D3DCREATE_HARDWARE_VERTEXPROCESSING
        cpu.regs[EAX] = S_OK
    dev_methods[9] = _com_stub(stubs, "d3d8dev", "Dev::GetCreationParameters",
        _dev_get_creation_params, 4, memory)

    dev_methods[10] = _dev_ok  ("Dev::SetCursorProperties",       12)
    dev_methods[11] = _dev_void("Dev::SetCursorPosition",         12)
    dev_methods[12] = _dev_uint("Dev::ShowCursor",                 4, 0)  # returns BOOL prev state
    dev_methods[13] = _dev_ok  ("Dev::CreateAdditionalSwapChain",  8)

    # [14] Reset(D3DPRESENT_PARAMETERS*)
    def _dev_reset(cpu: "CPU", mem: "Memory") -> None:
        logger.debug("d3d8", "IDirect3DDevice8::Reset")
        cpu.regs[EAX] = S_OK
    dev_methods[14] = _com_stub(stubs, "d3d8dev", "Dev::Reset",
        _dev_reset, 4, memory)

    # [15] Present(pSrc, pDest, hWnd, pRegion)
    def _dev_present(cpu: "CPU", mem: "Memory") -> None:
        logger.debug("d3d8", "IDirect3DDevice8::Present")
        cpu.regs[EAX] = S_OK
    dev_methods[15] = _com_stub(stubs, "d3d8dev", "Dev::Present",
        _dev_present, 16, memory)

    # [16] GetBackBuffer(UINT BackBuffer, D3DBACKBUFFER_TYPE, IDirect3DSurface8**)
    def _dev_get_back_buffer(cpu: "CPU", mem: "Memory") -> None:
        pp_surface = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        surf = _alloc_resource_obj(800 * 600 * 4, mem)
        if pp_surface:
            mem.write32(pp_surface, surf)
        cpu.regs[EAX] = S_OK
    dev_methods[16] = _com_stub(stubs, "d3d8dev", "Dev::GetBackBuffer",
        _dev_get_back_buffer, 12, memory)

    dev_methods[17] = _dev_ok  ("Dev::GetRasterStatus",   4)
    dev_methods[18] = _dev_void("Dev::SetGammaRamp",      8)
    dev_methods[19] = _dev_void("Dev::GetGammaRamp",      4)

    # [20] CreateTexture(W, H, Levels, Usage, Fmt, Pool, IDirect3DTexture8**)
    def _dev_create_texture(cpu: "CPU", mem: "Memory") -> None:
        w          = mem.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        h          = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        pp_texture = mem.read32((cpu.regs[ESP] + 28) & 0xFFFFFFFF)
        tex = _alloc_resource_obj(w * h * 4 or 4, mem)
        if pp_texture:
            mem.write32(pp_texture, tex)
        cpu.regs[EAX] = S_OK
    dev_methods[20] = _com_stub(stubs, "d3d8dev", "Dev::CreateTexture",
        _dev_create_texture, 28, memory)

    # [21] CreateVolumeTexture(W, H, D, Levels, Usage, Fmt, Pool, IDirect3DVolumeTexture8**)
    def _dev_create_volume_texture(cpu: "CPU", mem: "Memory") -> None:
        pp_texture = mem.read32((cpu.regs[ESP] + 32) & 0xFFFFFFFF)
        if pp_texture:
            mem.write32(pp_texture, _alloc_resource_obj(4096, mem))
        cpu.regs[EAX] = S_OK
    dev_methods[21] = _com_stub(stubs, "d3d8dev", "Dev::CreateVolumeTexture",
        _dev_create_volume_texture, 32, memory)

    # [22] CreateCubeTexture(EdgeLength, Levels, Usage, Fmt, Pool, IDirect3DCubeTexture8**)
    def _dev_create_cube_texture(cpu: "CPU", mem: "Memory") -> None:
        pp_texture = mem.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        if pp_texture:
            mem.write32(pp_texture, _alloc_resource_obj(4096, mem))
        cpu.regs[EAX] = S_OK
    dev_methods[22] = _com_stub(stubs, "d3d8dev", "Dev::CreateCubeTexture",
        _dev_create_cube_texture, 24, memory)

    # [23] CreateVertexBuffer(Length, Usage, FVF, Pool, IDirect3DVertexBuffer8**)
    def _dev_create_vertex_buffer(cpu: "CPU", mem: "Memory") -> None:
        length = mem.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        pp_vb  = mem.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        vb = _alloc_resource_obj(length or 4, mem)
        if pp_vb:
            mem.write32(pp_vb, vb)
        cpu.regs[EAX] = S_OK
    dev_methods[23] = _com_stub(stubs, "d3d8dev", "Dev::CreateVertexBuffer",
        _dev_create_vertex_buffer, 20, memory)

    # [24] CreateIndexBuffer(Length, Usage, Fmt, Pool, IDirect3DIndexBuffer8**)
    def _dev_create_index_buffer(cpu: "CPU", mem: "Memory") -> None:
        length = mem.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        pp_ib  = mem.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        ib = _alloc_resource_obj(length or 4, mem)
        if pp_ib:
            mem.write32(pp_ib, ib)
        cpu.regs[EAX] = S_OK
    dev_methods[24] = _com_stub(stubs, "d3d8dev", "Dev::CreateIndexBuffer",
        _dev_create_index_buffer, 20, memory)

    # [25] CreateRenderTarget(W, H, Fmt, MultiSample, Lockable, IDirect3DSurface8**)
    def _dev_create_render_target(cpu: "CPU", mem: "Memory") -> None:
        w       = mem.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        h       = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        pp_surf = mem.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        if pp_surf:
            mem.write32(pp_surf, _alloc_resource_obj(w * h * 4 or 4, mem))
        cpu.regs[EAX] = S_OK
    dev_methods[25] = _com_stub(stubs, "d3d8dev", "Dev::CreateRenderTarget",
        _dev_create_render_target, 24, memory)

    # [26] CreateDepthStencilSurface(W, H, Fmt, MultiSample, IDirect3DSurface8**)
    def _dev_create_depth_stencil(cpu: "CPU", mem: "Memory") -> None:
        w       = mem.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        h       = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        pp_surf = mem.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        if pp_surf:
            mem.write32(pp_surf, _alloc_resource_obj(w * h * 4 or 4, mem))
        cpu.regs[EAX] = S_OK
    dev_methods[26] = _com_stub(stubs, "d3d8dev", "Dev::CreateDepthStencilSurface",
        _dev_create_depth_stencil, 20, memory)

    # [27] CreateImageSurface(W, H, Fmt, IDirect3DSurface8**)
    def _dev_create_image_surface(cpu: "CPU", mem: "Memory") -> None:
        w       = mem.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        h       = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        pp_surf = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        if pp_surf:
            mem.write32(pp_surf, _alloc_resource_obj(w * h * 4 or 4, mem))
        cpu.regs[EAX] = S_OK
    dev_methods[27] = _com_stub(stubs, "d3d8dev", "Dev::CreateImageSurface",
        _dev_create_image_surface, 16, memory)

    dev_methods[28] = _dev_ok  ("Dev::CopyRects",    20)
    dev_methods[29] = _dev_ok  ("Dev::UpdateTexture", 8)

    # [30] GetFrontBuffer(IDirect3DSurface8*) — surface already caller-allocated
    dev_methods[30] = _com_stub(stubs, "d3d8dev", "Dev::GetFrontBuffer",
        lambda cpu, mem: _set_eax(cpu, S_OK), 4, memory)

    dev_methods[31] = _dev_ok("Dev::SetRenderTarget", 8)

    # [32] GetRenderTarget(IDirect3DSurface8**)
    def _dev_get_render_target(cpu: "CPU", mem: "Memory") -> None:
        pp_surf = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if pp_surf:
            mem.write32(pp_surf, _alloc_resource_obj(800 * 600 * 4, mem))
        cpu.regs[EAX] = S_OK
    dev_methods[32] = _com_stub(stubs, "d3d8dev", "Dev::GetRenderTarget",
        _dev_get_render_target, 4, memory)

    # [33] GetDepthStencilSurface(IDirect3DSurface8**)
    def _dev_get_depth_stencil(cpu: "CPU", mem: "Memory") -> None:
        pp_surf = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if pp_surf:
            mem.write32(pp_surf, _alloc_resource_obj(800 * 600 * 4, mem))
        cpu.regs[EAX] = S_OK
    dev_methods[33] = _com_stub(stubs, "d3d8dev", "Dev::GetDepthStencilSurface",
        _dev_get_depth_stencil, 4, memory)

    dev_methods[34] = _dev_ok("Dev::BeginScene", 0)
    dev_methods[35] = _dev_ok("Dev::EndScene",   0)

    # [36] Clear(Count, pRects, Flags, Color, Z, Stencil)
    def _dev_clear(cpu: "CPU", mem: "Memory") -> None:
        color = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        logger.debug("d3d8", f"Clear color=0x{color & 0xFFFFFFFF:08x}")
        cpu.regs[EAX] = S_OK
    dev_methods[36] = _com_stub(stubs, "d3d8dev", "Dev::Clear",
        _dev_clear, 24, memory)

    dev_methods[37] = _dev_ok("Dev::SetTransform",      8)
    dev_methods[38] = _dev_ok("Dev::GetTransform",      8)
    dev_methods[39] = _dev_ok("Dev::MultiplyTransform",  8)
    dev_methods[40] = _dev_ok("Dev::SetViewport",        4)
    dev_methods[41] = _dev_ok("Dev::GetViewport",        4)
    dev_methods[42] = _dev_ok("Dev::SetMaterial",        4)
    dev_methods[43] = _dev_ok("Dev::GetMaterial",        4)
    dev_methods[44] = _dev_ok("Dev::SetLight",           8)
    dev_methods[45] = _dev_ok("Dev::GetLight",           8)
    dev_methods[46] = _dev_ok("Dev::LightEnable",        8)
    dev_methods[47] = _dev_ok("Dev::GetLightEnable",     8)
    dev_methods[48] = _dev_ok("Dev::SetClipPlane",       8)
    dev_methods[49] = _dev_ok("Dev::GetClipPlane",       8)
    dev_methods[50] = _dev_ok("Dev::SetRenderState",     8)

    # [51] GetRenderState(State, DWORD* pValue)
    def _dev_get_render_state(cpu: "CPU", mem: "Memory") -> None:
        p_val = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_val:
            mem.write32(p_val, 0)
        cpu.regs[EAX] = S_OK
    dev_methods[51] = _com_stub(stubs, "d3d8dev", "Dev::GetRenderState",
        _dev_get_render_state, 8, memory)

    dev_methods[52] = _dev_ok("Dev::BeginStateBlock", 0)

    # [53] EndStateBlock(DWORD* pToken)
    def _dev_end_state_block(cpu: "CPU", mem: "Memory") -> None:
        p_token = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if p_token:
            mem.write32(p_token, 0xD3D50001)
        cpu.regs[EAX] = S_OK
    dev_methods[53] = _com_stub(stubs, "d3d8dev", "Dev::EndStateBlock",
        _dev_end_state_block, 4, memory)

    dev_methods[54] = _dev_ok("Dev::ApplyStateBlock",   4)
    dev_methods[55] = _dev_ok("Dev::CaptureStateBlock", 4)
    dev_methods[56] = _dev_ok("Dev::DeleteStateBlock",  4)

    # [57] CreateStateBlock(Type, DWORD* pToken)
    def _dev_create_state_block(cpu: "CPU", mem: "Memory") -> None:
        p_token = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_token:
            mem.write32(p_token, 0xD3D50002)
        cpu.regs[EAX] = S_OK
    dev_methods[57] = _com_stub(stubs, "d3d8dev", "Dev::CreateStateBlock",
        _dev_create_state_block, 8, memory)

    dev_methods[58] = _dev_ok("Dev::SetClipStatus", 4)
    dev_methods[59] = _dev_ok("Dev::GetClipStatus", 4)

    # [60] GetTexture(Stage, IDirect3DBaseTexture8**)
    def _dev_get_texture(cpu: "CPU", mem: "Memory") -> None:
        pp_tex = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if pp_tex:
            mem.write32(pp_tex, 0)
        cpu.regs[EAX] = S_OK
    dev_methods[60] = _com_stub(stubs, "d3d8dev", "Dev::GetTexture",
        _dev_get_texture, 8, memory)

    dev_methods[61] = _dev_ok("Dev::SetTexture", 8)

    # [62] GetTextureStageState(Stage, Type, DWORD* pValue)
    def _dev_get_texture_stage_state(cpu: "CPU", mem: "Memory") -> None:
        p_val = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if p_val:
            mem.write32(p_val, 0)
        cpu.regs[EAX] = S_OK
    dev_methods[62] = _com_stub(stubs, "d3d8dev", "Dev::GetTextureStageState",
        _dev_get_texture_stage_state, 12, memory)

    dev_methods[63] = _dev_ok("Dev::SetTextureStageState", 12)

    # [64] ValidateDevice(DWORD* pNumPasses)
    def _dev_validate_device(cpu: "CPU", mem: "Memory") -> None:
        p_passes = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if p_passes:
            mem.write32(p_passes, 1)
        cpu.regs[EAX] = S_OK
    dev_methods[64] = _com_stub(stubs, "d3d8dev", "Dev::ValidateDevice",
        _dev_validate_device, 4, memory)

    dev_methods[65] = _dev_ok("Dev::GetInfo",                   12)
    dev_methods[66] = _dev_ok("Dev::SetPaletteEntries",          8)
    dev_methods[67] = _dev_ok("Dev::GetPaletteEntries",          8)
    dev_methods[68] = _dev_ok("Dev::SetCurrentTexturePalette",   4)
    dev_methods[69] = _dev_ok("Dev::GetCurrentTexturePalette",   4)

    # [70] DrawPrimitive(PrimType, StartVertex, PrimCount)
    def _dev_draw_primitive(cpu: "CPU", mem: "Memory") -> None:
        prim_type  = mem.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        prim_count = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        logger.debug("d3d8", f"DrawPrimitive type={prim_type} count={prim_count}")
        cpu.regs[EAX] = S_OK
    dev_methods[70] = _com_stub(stubs, "d3d8dev", "Dev::DrawPrimitive",
        _dev_draw_primitive, 12, memory)

    # [71] DrawIndexedPrimitive(PrimType, minIndex, NumVerts, startIndex, primCount)
    def _dev_draw_indexed_primitive(cpu: "CPU", mem: "Memory") -> None:
        prim_type  = mem.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        prim_count = mem.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        logger.debug("d3d8", f"DrawIndexedPrimitive type={prim_type} count={prim_count}")
        cpu.regs[EAX] = S_OK
    dev_methods[71] = _com_stub(stubs, "d3d8dev", "Dev::DrawIndexedPrimitive",
        _dev_draw_indexed_primitive, 20, memory)

    # [72] DrawPrimitiveUP(PrimType, PrimCount, pVertexStreamZeroData, VertexStreamZeroStride)
    dev_methods[72] = _com_stub(stubs, "d3d8dev", "Dev::DrawPrimitiveUP",
        lambda cpu, mem: _set_eax(cpu, S_OK), 16, memory)
    # [73] DrawIndexedPrimitiveUP(8 args)
    dev_methods[73] = _com_stub(stubs, "d3d8dev", "Dev::DrawIndexedPrimitiveUP",
        lambda cpu, mem: _set_eax(cpu, S_OK), 32, memory)

    dev_methods[74] = _dev_ok("Dev::ProcessVertices", 20)

    # [75] CreateVertexShader(pDecl, pFunction, DWORD* pHandle, Usage)
    def _dev_create_vertex_shader(cpu: "CPU", mem: "Memory") -> None:
        p_handle = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if p_handle:
            mem.write32(p_handle, 0xD3D30001)
        cpu.regs[EAX] = S_OK
    dev_methods[75] = _com_stub(stubs, "d3d8dev", "Dev::CreateVertexShader",
        _dev_create_vertex_shader, 16, memory)

    dev_methods[76] = _dev_ok("Dev::SetVertexShader", 4)

    # [77] GetVertexShader(DWORD* pHandle)
    def _dev_get_vertex_shader(cpu: "CPU", mem: "Memory") -> None:
        p_handle = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if p_handle:
            mem.write32(p_handle, 0xD3D30001)
        cpu.regs[EAX] = S_OK
    dev_methods[77] = _com_stub(stubs, "d3d8dev", "Dev::GetVertexShader",
        _dev_get_vertex_shader, 4, memory)

    dev_methods[78] = _dev_ok("Dev::DeleteVertexShader",         4)
    dev_methods[79] = _dev_ok("Dev::SetVertexShaderConstant",   12)
    dev_methods[80] = _dev_ok("Dev::GetVertexShaderConstant",   12)
    dev_methods[81] = _dev_ok("Dev::GetVertexShaderDeclaration", 12)
    dev_methods[82] = _dev_ok("Dev::GetVertexShaderFunction",    12)
    dev_methods[83] = _dev_ok("Dev::SetStreamSource",            12)

    # [84] GetStreamSource(StreamNum, IDirect3DVertexBuffer8**, UINT* pStride)
    def _dev_get_stream_source(cpu: "CPU", mem: "Memory") -> None:
        pp_vb = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if pp_vb:
            mem.write32(pp_vb, 0)
        cpu.regs[EAX] = S_OK
    dev_methods[84] = _com_stub(stubs, "d3d8dev", "Dev::GetStreamSource",
        _dev_get_stream_source, 12, memory)

    dev_methods[85] = _dev_ok("Dev::SetIndices", 8)
    dev_methods[86] = _dev_ok("Dev::GetIndices", 8)

    # [87] CreatePixelShader(pFunction, DWORD* pHandle)
    def _dev_create_pixel_shader(cpu: "CPU", mem: "Memory") -> None:
        p_handle = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_handle:
            mem.write32(p_handle, 0xD3D40001)
        cpu.regs[EAX] = S_OK
    dev_methods[87] = _com_stub(stubs, "d3d8dev", "Dev::CreatePixelShader",
        _dev_create_pixel_shader, 8, memory)

    dev_methods[88] = _dev_ok("Dev::SetPixelShader", 4)

    # [89] GetPixelShader(DWORD* pHandle)
    def _dev_get_pixel_shader(cpu: "CPU", mem: "Memory") -> None:
        p_handle = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if p_handle:
            mem.write32(p_handle, 0xD3D40001)
        cpu.regs[EAX] = S_OK
    dev_methods[89] = _com_stub(stubs, "d3d8dev", "Dev::GetPixelShader",
        _dev_get_pixel_shader, 4, memory)

    dev_methods[90] = _dev_ok("Dev::DeletePixelShader",          4)
    dev_methods[91] = _dev_ok("Dev::SetPixelShaderConstant",    12)
    dev_methods[92] = _dev_ok("Dev::GetPixelShaderConstant",    12)
    dev_methods[93] = _dev_ok("Dev::GetPixelShaderFunction",    12)
    dev_methods[94] = _dev_ok("Dev::DrawRectPatch",             12)
    dev_methods[95] = _dev_ok("Dev::DrawTriPatch",              12)
    dev_methods[96] = _dev_ok("Dev::DeletePatch",                4)

    # Write IDirect3DDevice8 vtable into memory
    for i, addr in enumerate(dev_methods):
        memory.write32(D3DDEV_VTABLE + i * 4, addr or 0)
    # Write IDirect3DDevice8 object (first DWORD = vtable ptr)
    memory.write32(D3DDEV_OBJ, D3DDEV_VTABLE)

    # ── d3d8.dll exported function stubs ─────────────────────────────────────

    # Direct3DCreate8(SDKVersion) -> IDirect3D8*  [stdcall, callee cleans 1 arg]
    def _direct3d_create8(cpu: "CPU") -> None:
        logger.warn(
            "d3d8",
            f"[FAKE] Direct3DCreate8() -> fake COM object at 0x{D3D8_OBJ:08x}"
            " — real dgVoodoo code not running",
        )
        cpu.regs[EAX] = D3D8_OBJ
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("d3d8.dll", "Direct3DCreate8", _direct3d_create8)

    def _debug_set_mute(cpu: "CPU") -> None:
        _cleanup_com(cpu, memory, 4)

    stubs.register_handler("d3d8.dll", "DebugSetMute", _debug_set_mute)

    def _enable_maximized_windowed_shim(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        _cleanup_com(cpu, memory, 4)

    stubs.register_handler(
        "d3d8.dll",
        "Direct3D8EnableMaximizedWindowedModeShim",
        _enable_maximized_windowed_shim,
    )

    def _validate_pixel_shader(cpu: "CPU") -> None:
        cpu.regs[EAX] = S_OK
        _cleanup_com(cpu, memory, 16)

    stubs.register_handler("d3d8.dll", "ValidatePixelShader", _validate_pixel_shader)

    def _validate_vertex_shader(cpu: "CPU") -> None:
        cpu.regs[EAX] = S_OK
        _cleanup_com(cpu, memory, 16)

    stubs.register_handler("d3d8.dll", "ValidateVertexShader", _validate_vertex_shader)

    logger.warn(
        "d3d8",
        "[FAKE] D3D8 stubs registered — fake COM objects will override real dgVoodoo d3d8.dll",
    )
    logger.trace("d3d8", f"  IDirect3D8 @ 0x{D3D8_OBJ:08x} (vtable @ 0x{D3D8_VTABLE:08x})")
    logger.trace("d3d8", f"  IDirect3DDevice8 @ 0x{D3DDEV_OBJ:08x} (vtable @ 0x{D3DDEV_VTABLE:08x})")


# ── Private helper ────────────────────────────────────────────────────────────

def _set_eax(cpu: "CPU", value: int) -> None:
    """Set EAX register; used as a single-expression handler body."""
    cpu.regs[EAX] = value
