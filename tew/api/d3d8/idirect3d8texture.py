"""IDirect3DTexture8 COM vtable — 18 slots.

Vtable slot order (matches d3d8.h IDirect3DTexture8):
  IUnknown:
  [0]  QueryInterface(REFIID, void**)
  [1]  AddRef()
  [2]  Release()
  IDirect3DResource8:
  [3]  GetDevice(IDirect3DDevice8**)
  [4]  SetPrivateData(REFGUID, pData, SizeOfData, Flags)
  [5]  GetPrivateData(REFGUID, pData, pSizeOfData)
  [6]  FreePrivateData(REFGUID)
  [7]  SetPriority(PriorityNew) -> DWORD
  [8]  GetPriority() -> DWORD
  [9]  PreLoad()
  [10] GetType() -> D3DRESOURCETYPE
  IDirect3DBaseTexture8:
  [11] SetLOD(LODNew) -> DWORD
  [12] GetLOD() -> DWORD
  [13] GetLevelCount() -> DWORD
  IDirect3DTexture8:
  [14] GetLevelDesc(Level, D3DSURFACE_DESC*)
  [15] GetSurfaceLevel(Level, IDirect3DSurface8**)
  [16] LockRect(Level, D3DLOCKED_RECT*, CONST RECT*, Flags)
  [17] UnlockRect(Level)

Texture object layout (28 + levels*4 bytes, allocated by _alloc_texture_obj):
  [+0]  vtable ptr → D3DTEX_VTABLE
  [+4]  mip-0 data ptr
  [+8]  mip-0 data size
  [+12] base width
  [+16] base height
  [+20] D3DFORMAT
  [+24] level_count
  [+28 + i*4] IDirect3DSurface8* for mip level i

COM dispatch convention (dx8z-style):
  ESP+0 = return address
  ESP+4 = this (texture object address)
  ESP+8 = first arg, ESP+12 = second arg, ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers

from tew.hardware.cpu_zig import EAX, ESP
from tew.api.d3d8._layout import D3DDEV_OBJ, S_OK, D3DERR_NOTAVAIL
from tew.api.d3d8._helpers import _com_stub, _set_eax
from tew.api.d3d8.idirect3d8resource import _add_ref, _release

# D3DSURFACE_DESC field offsets (matches idirect3d8surface.py)
_DESC_FORMAT      = 0
_DESC_TYPE        = 4
_DESC_USAGE       = 8
_DESC_POOL        = 12
_DESC_SIZE        = 16
_DESC_MULTISAMPLE = 20
_DESC_WIDTH       = 24
_DESC_HEIGHT      = 28

# Texture object field offsets
_OBJ_DATA        = 4
_OBJ_DATASIZE    = 8
_OBJ_WIDTH       = 12
_OBJ_HEIGHT      = 16
_OBJ_FORMAT      = 20
_OBJ_LEVELCOUNT  = 24
_OBJ_SURF_BASE   = 28   # surface ptr for level i at +28 + i*4

# Surface object field offsets (IDirect3DSurface8 layout from idirect3d8surface.py)
_SURF_DATA    = 4
_SURF_DATASIZE = 8
_SURF_WIDTH   = 12
_SURF_HEIGHT  = 16
_SURF_FORMAT  = 20


def _get_surface_ptr(this: int, level: int, mem: "Memory") -> int:
    """Return the IDirect3DSurface8* stored at texture_obj[+28 + level*4]."""
    level_count = mem.read32((this + _OBJ_LEVELCOUNT) & 0xFFFFFFFF)
    if level >= level_count:
        return 0
    return mem.read32((this + _OBJ_SURF_BASE + level * 4) & 0xFFFFFFFF)


def make_vtable(stubs: "Win32Handlers", memory: "Memory") -> list[int]:
    """Return the 18 trampoline addresses for the IDirect3DTexture8 vtable."""

    from tew.logger import logger as _log
    _qi_seen: set = set()

    # [0] QueryInterface
    def _query_interface(cpu: "CPU", mem: "Memory") -> None:
        riid_ptr = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        ppv      = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        try:
            b = bytes(mem.read8((riid_ptr + i) & 0xFFFFFFFF) for i in range(16))
            iid_str = (f"{int.from_bytes(b[0:4],'little'):08x}-"
                       f"{int.from_bytes(b[4:6],'little'):04x}-"
                       f"{int.from_bytes(b[6:8],'little'):04x}-"
                       f"{b[8]:02x}{b[9]:02x}-"
                       f"{''.join(f'{x:02x}' for x in b[10:16])}")
        except Exception:
            iid_str = f"0x{riid_ptr:08x}"
        if iid_str not in _qi_seen:
            _qi_seen.add(iid_str)
            _log.info("d3d8", f"Texture::QueryInterface riid={{{iid_str}}} -> E_NOINTERFACE")
        if ppv:
            mem.write32(ppv, 0)
        cpu.regs[EAX] = 0x80004002  # E_NOINTERFACE

    # [3] GetDevice
    def _get_device(cpu: "CPU", mem: "Memory") -> None:
        pp_device = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if pp_device:
            mem.write32(pp_device, D3DDEV_OBJ)
        cpu.regs[EAX] = S_OK

    # [13] GetLevelCount() -> DWORD
    def _get_level_count(cpu: "CPU", mem: "Memory") -> None:
        this = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = mem.read32((this + _OBJ_LEVELCOUNT) & 0xFFFFFFFF)

    # [14] GetLevelDesc(UINT Level, D3DSURFACE_DESC* pDesc)
    def _get_level_desc(cpu: "CPU", mem: "Memory") -> None:
        this   = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        level  = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        p_desc = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        surf = _get_surface_ptr(this, level, mem)
        if not surf:
            cpu.regs[EAX] = D3DERR_NOTAVAIL
            return
        if p_desc:
            fmt  = mem.read32((surf + _SURF_FORMAT)   & 0xFFFFFFFF)
            w    = mem.read32((surf + _SURF_WIDTH)    & 0xFFFFFFFF)
            h    = mem.read32((surf + _SURF_HEIGHT)   & 0xFFFFFFFF)
            size = mem.read32((surf + _SURF_DATASIZE) & 0xFFFFFFFF)
            mem.write32(p_desc + _DESC_FORMAT,      fmt)
            mem.write32(p_desc + _DESC_TYPE,        4)   # D3DRTYPE_SURFACE
            mem.write32(p_desc + _DESC_USAGE,       0)
            mem.write32(p_desc + _DESC_POOL,        0)   # D3DPOOL_DEFAULT
            mem.write32(p_desc + _DESC_SIZE,        size)
            mem.write32(p_desc + _DESC_MULTISAMPLE, 0)
            mem.write32(p_desc + _DESC_WIDTH,       w)
            mem.write32(p_desc + _DESC_HEIGHT,      h)
        cpu.regs[EAX] = S_OK

    # [15] GetSurfaceLevel(UINT Level, IDirect3DSurface8** ppSurfaceLevel)
    def _get_surface_level(cpu: "CPU", mem: "Memory") -> None:
        this    = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        level   = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        pp_surf = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        surf = _get_surface_ptr(this, level, mem)
        if not surf:
            _log.error("d3d8", f"Texture::GetSurfaceLevel level={level} out of range — halting")
            cpu.halted = True
            return
        if pp_surf:
            mem.write32(pp_surf, surf)
        _log.debug("d3d8", f"Texture::GetSurfaceLevel level={level} -> surf=0x{surf:08x}")
        cpu.regs[EAX] = S_OK

    # [16] LockRect(UINT Level, D3DLOCKED_RECT* pLockedRect, CONST RECT* pRect, DWORD Flags)
    def _lock_rect(cpu: "CPU", mem: "Memory") -> None:
        this     = mem.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        level    = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        p_locked = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        surf = _get_surface_ptr(this, level, mem)
        if not surf:
            cpu.regs[EAX] = D3DERR_NOTAVAIL
            return
        if p_locked:
            w        = mem.read32((surf + _SURF_WIDTH) & 0xFFFFFFFF)
            data_ptr = mem.read32((surf + _SURF_DATA)  & 0xFFFFFFFF)
            mem.write32(p_locked,     w * 4)     # Pitch
            mem.write32(p_locked + 4, data_ptr)  # pBits
        cpu.regs[EAX] = S_OK

    return [
        # [0]  QueryInterface(REFIID, void**)
        _com_stub(stubs, "d3d8tex", "Texture::QueryInterface",
            _query_interface, 8, memory),
        # [1]  AddRef()
        _com_stub(stubs, "d3d8tex", "Texture::AddRef",
            _add_ref, 0, memory),
        # [2]  Release()
        _com_stub(stubs, "d3d8tex", "Texture::Release",
            _release, 0, memory),
        # [3]  GetDevice(IDirect3DDevice8**)
        _com_stub(stubs, "d3d8tex", "Texture::GetDevice",
            _get_device, 4, memory),
        # [4]  SetPrivateData(REFGUID, pData, SizeOfData, Flags)
        _com_stub(stubs, "d3d8tex", "Texture::SetPrivateData",
            lambda cpu, mem: _set_eax(cpu, S_OK), 16, memory),
        # [5]  GetPrivateData(REFGUID, pData, pSizeOfData)
        _com_stub(stubs, "d3d8tex", "Texture::GetPrivateData",
            lambda cpu, mem: _set_eax(cpu, S_OK), 12, memory),
        # [6]  FreePrivateData(REFGUID)
        _com_stub(stubs, "d3d8tex", "Texture::FreePrivateData",
            lambda cpu, mem: _set_eax(cpu, S_OK), 16, memory),
        # [7]  SetPriority(PriorityNew) -> DWORD — no-op for non-managed textures
        _com_stub(stubs, "d3d8tex", "Texture::SetPriority",
            lambda cpu, mem: _set_eax(cpu, 0), 4, memory),
        # [8]  GetPriority() -> DWORD
        _com_stub(stubs, "d3d8tex", "Texture::GetPriority",
            lambda cpu, mem: _set_eax(cpu, 0), 0, memory),
        # [9]  PreLoad()
        _com_stub(stubs, "d3d8tex", "Texture::PreLoad",
            lambda cpu, mem: None, 0, memory),
        # [10] GetType() -> D3DRESOURCETYPE (6 = D3DRTYPE_TEXTURE)
        _com_stub(stubs, "d3d8tex", "Texture::GetType",
            lambda cpu, mem: _set_eax(cpu, 6), 0, memory),
        # [11] SetLOD(LODNew) -> DWORD — no-op for non-D3DPOOL_MANAGED; returns 0
        _com_stub(stubs, "d3d8tex", "Texture::SetLOD",
            lambda cpu, mem: _set_eax(cpu, 0), 4, memory),
        # [12] GetLOD() -> DWORD
        _com_stub(stubs, "d3d8tex", "Texture::GetLOD",
            lambda cpu, mem: _set_eax(cpu, 0), 0, memory),
        # [13] GetLevelCount() -> DWORD
        _com_stub(stubs, "d3d8tex", "Texture::GetLevelCount",
            _get_level_count, 0, memory),
        # [14] GetLevelDesc(UINT Level, D3DSURFACE_DESC* pDesc)
        _com_stub(stubs, "d3d8tex", "Texture::GetLevelDesc",
            _get_level_desc, 8, memory),
        # [15] GetSurfaceLevel(UINT Level, IDirect3DSurface8** ppSurfaceLevel)
        _com_stub(stubs, "d3d8tex", "Texture::GetSurfaceLevel",
            _get_surface_level, 8, memory),
        # [16] LockRect(UINT Level, D3DLOCKED_RECT*, CONST RECT*, DWORD)
        _com_stub(stubs, "d3d8tex", "Texture::LockRect",
            _lock_rect, 16, memory),
        # [17] UnlockRect(UINT Level)
        _com_stub(stubs, "d3d8tex", "Texture::UnlockRect",
            lambda cpu, mem: _set_eax(cpu, S_OK), 4, memory),
    ]
