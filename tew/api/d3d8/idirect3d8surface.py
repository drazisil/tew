"""IDirect3DSurface8 COM vtable — 11 slots.

Vtable slot order (matches d3d8.h IDirect3DSurface8):
  [0]  QueryInterface(REFIID, void**)
  [1]  AddRef()
  [2]  Release()
  [3]  GetDevice(IDirect3DDevice8**)
  [4]  SetPrivateData(REFGUID, pData, SizeOfData, Flags)
  [5]  GetPrivateData(REFGUID, pData, pSizeOfData)
  [6]  FreePrivateData(REFGUID)
  [7]  GetContainer(REFIID, void**)
  [8]  GetDesc(D3DSURFACE_DESC*)
  [9]  LockRect(D3DLOCKED_RECT*, CONST RECT*, DWORD)
  [10] UnlockRect()

dx8z calls these via COM vtable dispatch with 'this' pushed on the stack
(cdecl-style, not ECX thiscall), so stack layout at handler entry:
  ESP+0 = return address
  ESP+4 = this (surface object address)
  ESP+8 = first arg, ESP+12 = second arg, ...

Surface object layout (24 bytes, allocated by _alloc_surface_obj):
  [+0]  vtable ptr → D3DSURF_VTABLE
  [+4]  data ptr (pixel data)
  [+8]  data size in bytes
  [+12] width
  [+16] height
  [+20] D3DFORMAT (e.g. 0x16 = D3DFMT_X8R8G8B8)
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

# D3DSURFACE_DESC field offsets
_DESC_FORMAT        = 0   # D3DFORMAT
_DESC_TYPE          = 4   # D3DRESOURCETYPE (4 = D3DRTYPE_SURFACE)
_DESC_USAGE         = 8   # DWORD
_DESC_POOL          = 12  # D3DPOOL
_DESC_SIZE          = 16  # UINT
_DESC_MULTISAMPLE   = 20  # D3DMULTISAMPLE_TYPE
_DESC_WIDTH         = 24  # UINT
_DESC_HEIGHT        = 28  # UINT

# Surface object field offsets (relative to COM object base)
_OBJ_DATA           = 4
_OBJ_DATASIZE       = 8
_OBJ_WIDTH          = 12
_OBJ_HEIGHT         = 16
_OBJ_FORMAT         = 20


def make_vtable(stubs: "Win32Handlers", memory: "Memory") -> list[int]:
    """Return the 11 trampoline addresses for the IDirect3DSurface8 vtable."""

    # [3] GetDevice — writes D3DDEV_OBJ into the out-pointer
    def _get_device(cpu: "CPU", mem: "Memory") -> None:
        pp_device = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if pp_device:
            mem.write32(pp_device, D3DDEV_OBJ)
        cpu.regs[EAX] = S_OK

    # [7] GetContainer — surfaces created by us have no parent container
    def _get_container(cpu: "CPU", mem: "Memory") -> None:
        ppv = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if ppv:
            mem.write32(ppv, 0)
        cpu.regs[EAX] = D3DERR_NOTAVAIL

    # [8] GetDesc(D3DSURFACE_DESC*) — fills all 8 fields from stored object data
    def _get_desc(cpu: "CPU", mem: "Memory") -> None:
        this    = mem.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        p_desc  = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        if p_desc:
            fmt  = mem.read32((this + _OBJ_FORMAT) & 0xFFFFFFFF)
            w    = mem.read32((this + _OBJ_WIDTH)  & 0xFFFFFFFF)
            h    = mem.read32((this + _OBJ_HEIGHT) & 0xFFFFFFFF)
            size = mem.read32((this + _OBJ_DATASIZE) & 0xFFFFFFFF)
            mem.write32(p_desc + _DESC_FORMAT,      fmt)
            mem.write32(p_desc + _DESC_TYPE,        4)   # D3DRTYPE_SURFACE
            mem.write32(p_desc + _DESC_USAGE,       0)
            mem.write32(p_desc + _DESC_POOL,        0)   # D3DPOOL_DEFAULT
            mem.write32(p_desc + _DESC_SIZE,        size)
            mem.write32(p_desc + _DESC_MULTISAMPLE, 0)
            mem.write32(p_desc + _DESC_WIDTH,       w)
            mem.write32(p_desc + _DESC_HEIGHT,      h)
        cpu.regs[EAX] = S_OK

    # [9] LockRect(D3DLOCKED_RECT*, CONST RECT*, DWORD)
    def _lock_rect(cpu: "CPU", mem: "Memory") -> None:
        this       = mem.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        p_locked   = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        # pRect (ESP+12) and Flags (ESP+16) ignored — we always lock the whole surface
        if p_locked:
            w        = mem.read32((this + _OBJ_WIDTH) & 0xFFFFFFFF)
            data_ptr = mem.read32((this + _OBJ_DATA)  & 0xFFFFFFFF)
            mem.write32(p_locked,     w * 4)      # Pitch: width × 4 bytes/pixel
            mem.write32(p_locked + 4, data_ptr)   # pBits
        cpu.regs[EAX] = S_OK

    # [10] UnlockRect()
    def _unlock_rect(cpu: "CPU", mem: "Memory") -> None:
        cpu.regs[EAX] = S_OK

    from tew.logger import logger as _log
    _qi_seen: set = set()

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
            _log.info("d3d8", f"Surface::QueryInterface riid={{{iid_str}}} -> E_NOINTERFACE")
        if ppv:
            mem.write32(ppv, 0)
        cpu.regs[EAX] = 0x80004002  # E_NOINTERFACE

    return [
        # [0]  QueryInterface(REFIID, void**) — 2 non-this args
        _com_stub(stubs, "d3d8surf", "Surface::QueryInterface",
            _query_interface, 8, memory),
        # [1]  AddRef()
        _com_stub(stubs, "d3d8surf", "Surface::AddRef",
            _add_ref, 0, memory),
        # [2]  Release()
        _com_stub(stubs, "d3d8surf", "Surface::Release",
            _release, 0, memory),
        # [3]  GetDevice(IDirect3DDevice8**)
        _com_stub(stubs, "d3d8surf", "Surface::GetDevice",
            _get_device, 4, memory),
        # [4]  SetPrivateData(REFGUID, pData, SizeOfData, Flags)
        _com_stub(stubs, "d3d8surf", "Surface::SetPrivateData",
            lambda cpu, mem: _set_eax(cpu, S_OK), 16, memory),
        # [5]  GetPrivateData(REFGUID, pData, pSizeOfData)
        _com_stub(stubs, "d3d8surf", "Surface::GetPrivateData",
            lambda cpu, mem: _set_eax(cpu, S_OK), 12, memory),
        # [6]  FreePrivateData(REFGUID)
        _com_stub(stubs, "d3d8surf", "Surface::FreePrivateData",
            lambda cpu, mem: _set_eax(cpu, S_OK), 16, memory),
        # [7]  GetContainer(REFIID, void**)
        _com_stub(stubs, "d3d8surf", "Surface::GetContainer",
            _get_container, 8, memory),
        # [8]  GetDesc(D3DSURFACE_DESC*)
        _com_stub(stubs, "d3d8surf", "Surface::GetDesc",
            _get_desc, 4, memory),
        # [9]  LockRect(D3DLOCKED_RECT*, CONST RECT*, DWORD)
        _com_stub(stubs, "d3d8surf", "Surface::LockRect",
            _lock_rect, 12, memory),
        # [10] UnlockRect()
        _com_stub(stubs, "d3d8surf", "Surface::UnlockRect",
            _unlock_rect, 0, memory),
    ]
