"""IDirect3DResource8/Buffer COM vtable — 14 slots.

Used exclusively by vertex buffer and index buffer objects (_alloc_resource_obj).
Surface objects use D3DSURF_VTABLE (idirect3d8surface.py), not this vtable.

Vtable slot order (matches d3d8.h IDirect3DVertexBuffer8 / IDirect3DIndexBuffer8):
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
  [11] Lock(OffsetToLock, SizeToLock, BYTE**, Flags)
  [12] Unlock()
  [13] GetDesc(void*)                              — D3DVERTEXBUFFER_DESC or D3DINDEXBUFFER_DESC
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers

from tew.hardware.cpu_zig import EAX, ESP
from tew.api.d3d8._layout import D3DDEV_OBJ, S_OK
from tew.api.d3d8._helpers import _com_stub, _set_eax

# Per-object reference counts: obj_addr -> count (initial = 1 on first access)
_ref_counts: dict[int, int] = {}


def _add_ref(cpu: "CPU", mem: "Memory") -> None:
    this = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
    count = _ref_counts.get(this, 1) + 1
    _ref_counts[this] = count
    cpu.regs[EAX] = count


def _release(cpu: "CPU", mem: "Memory") -> None:
    this = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
    count = _ref_counts.get(this, 1) - 1
    if count > 0:
        _ref_counts[this] = count
    else:
        _ref_counts.pop(this, None)
    cpu.regs[EAX] = max(count, 0)


def make_vtable(stubs: "Win32Handlers", memory: "Memory") -> list[int]:
    """Return the 14 trampoline addresses for the buffer resource vtable."""

    # [3] GetDevice(IDirect3DDevice8**) — writes D3DDEV_OBJ into the out-pointer
    def _get_device(cpu: "CPU", mem: "Memory") -> None:
        pp_device = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if pp_device:
            mem.write32(pp_device, D3DDEV_OBJ)
        cpu.regs[EAX] = S_OK

    # [11] GetDesc(void* pDesc) — fills D3DVERTEXBUFFER_DESC / D3DINDEXBUFFER_DESC
    def _buffer_get_desc(cpu: "CPU", mem: "Memory") -> None:
        this_ptr = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        p_desc   = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_desc:
            size = mem.read32((this_ptr + 8) & 0xFFFFFFFF)  # stored at obj+8
            mem.write32(p_desc + 0,  0)    # FVF = 0 (vertex buffer) / Format = 0 (index buffer)
            mem.write32(p_desc + 4,  9)    # Type = D3DRTYPE_VERTEXBUFFER
            mem.write32(p_desc + 8,  0)    # Usage = 0
            mem.write32(p_desc + 12, 0)    # Pool = D3DPOOL_DEFAULT
            mem.write32(p_desc + 16, size) # Size in bytes
        cpu.regs[EAX] = S_OK

    # [12] Lock(OffsetToLock, SizeToLock, BYTE** ppbData, Flags) — writes data ptr
    def _buffer_lock(cpu: "CPU", mem: "Memory") -> None:
        this_ptr = mem.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        ppb_data = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        data_ptr = mem.read32((this_ptr + 4) & 0xFFFFFFFF)
        if ppb_data:
            mem.write32(ppb_data, data_ptr)
        cpu.regs[EAX] = S_OK

    from tew.logger import logger as _log
    _qi_seen: set = set()

    def _query_interface(cpu: "CPU", mem: "Memory") -> None:
        riid_ptr = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        ppv      = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
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
            _log.info("d3d8", f"Res::QueryInterface riid={{{iid_str}}} -> E_NOINTERFACE")
        if ppv:
            mem.write32(ppv, 0)
        cpu.regs[EAX] = 0x80004002  # E_NOINTERFACE

    return [
        # [0]  QueryInterface
        _com_stub(stubs, "d3d8res", "Res::QueryInterface",
            _query_interface, 8, memory),
        # [1]  AddRef
        _com_stub(stubs, "d3d8res", "Res::AddRef",
            _add_ref, 0, memory),
        # [2]  Release
        _com_stub(stubs, "d3d8res", "Res::Release",
            _release, 0, memory),
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
        # [11] Lock(OffsetToLock, SizeToLock, BYTE** ppbData, Flags)
        _com_stub(stubs, "d3d8res", "Buffer::Lock",
            _buffer_lock, 16, memory),
        # [12] Unlock()
        _com_stub(stubs, "d3d8res", "Buffer::Unlock",
            lambda cpu, mem: _set_eax(cpu, S_OK), 0, memory),
        # [13] GetDesc(D3DVERTEXBUFFER_DESC* / D3DINDEXBUFFER_DESC*)
        _com_stub(stubs, "d3d8res", "Buffer::GetDesc",
            _buffer_get_desc, 4, memory),
    ]
