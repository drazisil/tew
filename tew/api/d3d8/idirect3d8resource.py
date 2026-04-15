"""IDirect3DResource8 (generic resource) COM vtable — 18 slots.

Vtable slot order (matches d3d8.h):
  [0]  QueryInterface(REFIID, void**)
  [1]  AddRef()
  [2]  Release()
  [3]  GetDevice(IDirect3DDevice8**)
  [4]  SetPrivateData(REFGUID, pData, SizeOfData, Flags)
  [5]  GetPrivateData(REFGUID, pData, pSizeOfData)
  [6]  FreePrivateData(REFGUID)
  [7]  SetPriority(PriorityNew) -> DWORD
  [8]  GetPriority() -> DWORD
  [9]  PreLoad()
  [10] GetType() -> D3DRESOURCETYPE
  [11] Surface::GetContainer(REFIID, void**)
  [12] Surface::GetDesc(D3DSURFACE_DESC*)
  [13] Surface::LockRect(D3DLOCKED_RECT*, CONST RECT*, DWORD)
  [14] Surface::UnlockRect()
  [15] Buffer::GetDesc(void*)
  [16] Buffer::Lock(OffsetToLock, SizeToLock, BYTE** ppbData, Flags)
  [17] Buffer::Unlock()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers

from tew.hardware.cpu import EAX, ECX, ESP
from tew.api.d3d8._layout import D3DDEV_OBJ, D3DERR_NOTAVAIL, S_OK
from tew.api.d3d8._helpers import _com_stub, _set_eax


def make_vtable(stubs: "Win32Handlers", memory: "Memory") -> list[int]:
    """Return the 18 trampoline addresses for the generic resource vtable."""

    # [3] GetDevice — writes D3DDEV_OBJ into the out-pointer
    def _get_device(cpu: "CPU", mem: "Memory") -> None:
        pp_device = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if pp_device:
            mem.write32(pp_device, D3DDEV_OBJ)
        cpu.regs[EAX] = S_OK

    # [13] Surface::LockRect — returns pitch + data pointer from the resource object
    def _surface_lock_rect(cpu: "CPU", mem: "Memory") -> None:
        this_ptr = cpu.regs[ECX]
        p_locked  = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        data_ptr  = mem.read32((this_ptr + 4) & 0xFFFFFFFF)
        if p_locked:
            mem.write32(p_locked,     800 * 4)   # Pitch (800 px wide × 4 bpp)
            mem.write32(p_locked + 4, data_ptr)  # pBits
        cpu.regs[EAX] = S_OK

    # [16] Buffer::Lock — writes data pointer into ppbData
    def _buffer_lock(cpu: "CPU", mem: "Memory") -> None:
        this_ptr = cpu.regs[ECX]
        ppb_data  = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        data_ptr  = mem.read32((this_ptr + 4) & 0xFFFFFFFF)
        if ppb_data:
            mem.write32(ppb_data, data_ptr)
        cpu.regs[EAX] = S_OK

    return [
        # [0]  QueryInterface
        _com_stub(stubs, "d3d8res", "Res::QueryInterface",
            lambda cpu, mem: _set_eax(cpu, 0x80004002), 8, memory),
        # [1]  AddRef
        _com_stub(stubs, "d3d8res", "Res::AddRef",
            lambda cpu, mem: _set_eax(cpu, 1), 0, memory),
        # [2]  Release
        _com_stub(stubs, "d3d8res", "Res::Release",
            lambda cpu, mem: _set_eax(cpu, 0), 0, memory),
        # [3]  GetDevice
        _com_stub(stubs, "d3d8res", "Res::GetDevice",
            _get_device, 4, memory),
        # [4]  SetPrivateData
        _com_stub(stubs, "d3d8res", "Res::SetPrivateData",
            lambda cpu, mem: _set_eax(cpu, S_OK), 16, memory),
        # [5]  GetPrivateData
        _com_stub(stubs, "d3d8res", "Res::GetPrivateData",
            lambda cpu, mem: _set_eax(cpu, S_OK), 12, memory),
        # [6]  FreePrivateData
        _com_stub(stubs, "d3d8res", "Res::FreePrivateData",
            lambda cpu, mem: _set_eax(cpu, S_OK), 16, memory),
        # [7]  SetPriority
        _com_stub(stubs, "d3d8res", "Res::SetPriority",
            lambda cpu, mem: _set_eax(cpu, 0), 4, memory),
        # [8]  GetPriority
        _com_stub(stubs, "d3d8res", "Res::GetPriority",
            lambda cpu, mem: _set_eax(cpu, 0), 0, memory),
        # [9]  PreLoad
        _com_stub(stubs, "d3d8res", "Res::PreLoad",
            lambda cpu, mem: None, 0, memory),
        # [10] GetType
        _com_stub(stubs, "d3d8res", "Res::GetType",
            lambda cpu, mem: _set_eax(cpu, 0), 0, memory),
        # [11] Surface::GetContainer
        _com_stub(stubs, "d3d8res", "Surface::GetContainer",
            lambda cpu, mem: _set_eax(cpu, D3DERR_NOTAVAIL), 8, memory),
        # [12] Surface::GetDesc
        _com_stub(stubs, "d3d8res", "Surface::GetDesc",
            lambda cpu, mem: _set_eax(cpu, S_OK), 4, memory),
        # [13] Surface::LockRect
        _com_stub(stubs, "d3d8res", "Surface::LockRect",
            _surface_lock_rect, 12, memory),
        # [14] Surface::UnlockRect
        _com_stub(stubs, "d3d8res", "Surface::UnlockRect",
            lambda cpu, mem: _set_eax(cpu, S_OK), 0, memory),
        # [15] Buffer::GetDesc
        _com_stub(stubs, "d3d8res", "Buffer::GetDesc",
            lambda cpu, mem: _set_eax(cpu, S_OK), 4, memory),
        # [16] Buffer::Lock
        _com_stub(stubs, "d3d8res", "Buffer::Lock",
            _buffer_lock, 16, memory),
        # [17] Buffer::Unlock
        _com_stub(stubs, "d3d8res", "Buffer::Unlock",
            lambda cpu, mem: _set_eax(cpu, S_OK), 0, memory),
    ]
