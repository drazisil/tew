"""dinput.dll / dinput8.dll handler registrations.

Implements DirectInputCreateA and minimal IDirectInput2A + IDirectInputDevice2A stubs
sufficient for the game to complete DI initialisation and proceed to the render loop.

COM vtable addresses (fixed-data region, after D3DSURF_VTABLE ends at 0x0022028C):
    DI_VTABLE     = 0x00220290  (IDirectInput2A,       9 slots × 4 =  36 bytes)
    DI_OBJ        = 0x002202C0  (IDirectInput2A object, 4 bytes)
    DI_DEV_VTABLE = 0x002202D0  (IDirectInputDevice2A, 18 slots × 4 = 72 bytes)
    Device objects are bump-allocated from the D3D8 heap (8 bytes each).

All handlers read `this` from ESP+4 (dx8z / game push `this` on stack, not ECX).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall
from tew.api.d3d8._helpers import _com_stub, _heap_alloc, _set_eax
from tew.logger import logger

# ── Fixed COM object addresses ────────────────────────────────────────────────
DI_VTABLE     = 0x00220290   # IDirectInput2A vtable     (9  × 4 = 36 bytes)
DI_OBJ        = 0x002202C0   # IDirectInput2A singleton  (4 bytes)
DI_DEV_VTABLE = 0x002202D0   # IDirectInputDevice2A vtable (18 × 4 = 72 bytes)

# ── DirectInput error / status codes ─────────────────────────────────────────
DI_OK                  = 0x00000000
DI_NOTATTACHED         = 0x00000001
DI_POLLEDDEVICE        = 0x00000002
E_NOTIMPL              = 0x80004001
E_NOINTERFACE          = 0x80004002
DIERR_UNSUPPORTED      = 0x80004001   # same as E_NOTIMPL for DI
DIERR_DEVICENOTREG     = 0x80040154
DIERR_OBJECTNOTFOUND   = 0x80040181


def register_dinput_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
) -> None:
    """Register all DirectInput COM stubs and write vtable pointers into memory."""

    # ── IDirectInput2A vtable ─────────────────────────────────────────────────

    def _di_query_interface(cpu: "CPU", mem: "Memory") -> None:
        ppv = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if ppv:
            mem.write32(ppv, 0)
        cpu.regs[EAX] = E_NOINTERFACE

    def _di_create_device(cpu: "CPU", mem: "Memory") -> None:
        # CreateDevice(REFGUID, lplpDID, pUnkOuter) — arg_bytes=12
        pp_dev = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if pp_dev:
            obj = _heap_alloc(8)
            mem.write32(obj,     DI_DEV_VTABLE)
            mem.write32(obj + 4, 0)
            mem.write32(pp_dev, obj)
            logger.debug("handlers", f"DI::CreateDevice -> dev_obj=0x{obj:08x}")
        cpu.regs[EAX] = DI_OK

    di_vtable = [
        # [0] QueryInterface(REFIID, void**)
        _com_stub(stubs, "dinput.dll", "DI::QueryInterface",
                  _di_query_interface, 8, memory, DI_OBJ),
        # [1] AddRef()
        _com_stub(stubs, "dinput.dll", "DI::AddRef",
                  lambda cpu, mem: _set_eax(cpu, 2), 0, memory, DI_OBJ),
        # [2] Release()
        _com_stub(stubs, "dinput.dll", "DI::Release",
                  lambda cpu, mem: _set_eax(cpu, 1), 0, memory, DI_OBJ),
        # [3] CreateDevice(REFGUID, lplpDID, pUnkOuter)
        _com_stub(stubs, "dinput.dll", "DI::CreateDevice",
                  _di_create_device, 12, memory, DI_OBJ),
        # [4] EnumDevices(dwType, lpCallback, pvRef, dwFlags)
        _com_stub(stubs, "dinput.dll", "DI::EnumDevices",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 16, memory, DI_OBJ),
        # [5] GetDeviceStatus(REFGUID)
        _com_stub(stubs, "dinput.dll", "DI::GetDeviceStatus",
                  lambda cpu, mem: _set_eax(cpu, DI_NOTATTACHED), 4, memory, DI_OBJ),
        # [6] RunControlPanel(hwnd, dwFlags)
        _com_stub(stubs, "dinput.dll", "DI::RunControlPanel",
                  lambda cpu, mem: _set_eax(cpu, E_NOTIMPL), 8, memory, DI_OBJ),
        # [7] Initialize(hInst, dwVersion)
        _com_stub(stubs, "dinput.dll", "DI::Initialize",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 8, memory, DI_OBJ),
        # [8] FindDevice(REFGUID, ptszName, pguidOut)
        _com_stub(stubs, "dinput.dll", "DI::FindDevice",
                  lambda cpu, mem: _set_eax(cpu, DIERR_DEVICENOTREG), 12, memory, DI_OBJ),
    ]
    for i, addr in enumerate(di_vtable):
        memory.write32(DI_VTABLE + i * 4, addr)
    memory.write32(DI_OBJ, DI_VTABLE)

    # ── IDirectInputDevice2A vtable ───────────────────────────────────────────

    def _dev_query_interface(cpu: "CPU", mem: "Memory") -> None:
        ppv = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if ppv:
            mem.write32(ppv, 0)
        cpu.regs[EAX] = E_NOINTERFACE

    def _dev_get_caps(cpu: "CPU", mem: "Memory") -> None:
        # GetCapabilities(LPDIDEVCAPS lpDIDevCaps) — zero-fill struct
        p_caps = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_caps:
            # DIDEVCAPS: dwSize(4) + dwFlags(4) + dwDevType(4) + dwAxes(4)
            #            + dwButtons(4) + dwPOVs(4) + dwFFSamplePeriod(4)
            #            + dwFFMinTimeResolution(4) + dwFirmwareRevision(4)
            #            + dwHardwareRevision(4) + dwFFDriverVersion(4) = 44 bytes
            dw_size = mem.read32(p_caps & 0xFFFFFFFF)
            size = max(dw_size, 44) if dw_size else 44
            for off in range(0, size, 4):
                mem.write32((p_caps + off) & 0xFFFFFFFF, 0)
        cpu.regs[EAX] = DI_OK

    def _dev_get_device_state(cpu: "CPU", mem: "Memory") -> None:
        # GetDeviceState(DWORD cbData, LPVOID lpvData) — zero-fill output
        cb_data  = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        lpv_data = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if lpv_data and cb_data:
            for off in range(cb_data):
                mem.write8((lpv_data + off) & 0xFFFFFFFF, 0)
        cpu.regs[EAX] = DI_OK

    def _dev_get_device_data(cpu: "CPU", mem: "Memory") -> None:
        # GetDeviceData(cbObjectData, rgdod, pdwInOut, dwFlags)
        pdw_in_out = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        if pdw_in_out:
            mem.write32(pdw_in_out, 0)   # no buffered events
        cpu.regs[EAX] = DI_OK

    def _dev_set_event_notification(cpu: "CPU", mem: "Memory") -> None:
        # SetEventNotification(hEvent) — hEvent=NULL means polled
        h_event = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        cpu.regs[EAX] = DI_POLLEDDEVICE if h_event == 0 else DI_OK

    def _dev_get_device_info(cpu: "CPU", mem: "Memory") -> None:
        # GetDeviceInfo(LPDIDEVICEINSTANCEA) — zero-fill struct
        p_info = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_info:
            # DIDEVICEINSTANCEA: dwSize + many fields; zero first 80 bytes
            dw_size = mem.read32(p_info & 0xFFFFFFFF)
            size = max(dw_size, 80) if dw_size else 80
            for off in range(0, size, 4):
                mem.write32((p_info + off) & 0xFFFFFFFF, 0)
        cpu.regs[EAX] = DI_OK

    dev_vtable = [
        # [0] QueryInterface(REFIID, void**)
        _com_stub(stubs, "dinput.dll", "Dev::QueryInterface",
                  _dev_query_interface, 8, memory),
        # [1] AddRef()
        _com_stub(stubs, "dinput.dll", "Dev::AddRef",
                  lambda cpu, mem: _set_eax(cpu, 2), 0, memory),
        # [2] Release()
        _com_stub(stubs, "dinput.dll", "Dev::Release",
                  lambda cpu, mem: _set_eax(cpu, 1), 0, memory),
        # [3] GetCapabilities(LPDIDEVCAPS)
        _com_stub(stubs, "dinput.dll", "Dev::GetCapabilities",
                  _dev_get_caps, 4, memory),
        # [4] EnumObjects(lpCallback, pvRef, dwFlags)
        _com_stub(stubs, "dinput.dll", "Dev::EnumObjects",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 12, memory),
        # [5] GetProperty(REFGUID, pdiph)
        _com_stub(stubs, "dinput.dll", "Dev::GetProperty",
                  lambda cpu, mem: _set_eax(cpu, DIERR_UNSUPPORTED), 8, memory),
        # [6] SetProperty(REFGUID, pdiph)
        _com_stub(stubs, "dinput.dll", "Dev::SetProperty",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 8, memory),
        # [7] Acquire()
        _com_stub(stubs, "dinput.dll", "Dev::Acquire",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 0, memory),
        # [8] Unacquire()
        _com_stub(stubs, "dinput.dll", "Dev::Unacquire",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 0, memory),
        # [9] GetDeviceState(cbData, lpvData)
        _com_stub(stubs, "dinput.dll", "Dev::GetDeviceState",
                  _dev_get_device_state, 8, memory),
        # [10] GetDeviceData(cbObjectData, rgdod, pdwInOut, dwFlags)
        _com_stub(stubs, "dinput.dll", "Dev::GetDeviceData",
                  _dev_get_device_data, 16, memory),
        # [11] SetDataFormat(LPCDIDATAFORMAT)
        _com_stub(stubs, "dinput.dll", "Dev::SetDataFormat",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 4, memory),
        # [12] SetEventNotification(HANDLE)
        _com_stub(stubs, "dinput.dll", "Dev::SetEventNotification",
                  _dev_set_event_notification, 4, memory),
        # [13] SetCooperativeLevel(hwnd, dwFlags)
        _com_stub(stubs, "dinput.dll", "Dev::SetCooperativeLevel",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 8, memory),
        # [14] GetObjectInfo(pdidoi, dwObj, dwHow)
        _com_stub(stubs, "dinput.dll", "Dev::GetObjectInfo",
                  lambda cpu, mem: _set_eax(cpu, DIERR_OBJECTNOTFOUND), 12, memory),
        # [15] GetDeviceInfo(LPDIDEVICEINSTANCEA)
        _com_stub(stubs, "dinput.dll", "Dev::GetDeviceInfo",
                  _dev_get_device_info, 4, memory),
        # [16] RunControlPanel(hwnd, dwFlags)
        _com_stub(stubs, "dinput.dll", "Dev::RunControlPanel",
                  lambda cpu, mem: _set_eax(cpu, E_NOTIMPL), 8, memory),
        # [17] Initialize(hinst, dwVersion, REFGUID)
        _com_stub(stubs, "dinput.dll", "Dev::Initialize",
                  lambda cpu, mem: _set_eax(cpu, DI_OK), 12, memory),
    ]
    for i, addr in enumerate(dev_vtable):
        memory.write32(DI_DEV_VTABLE + i * 4, addr)

    # ── DirectInputCreateA DLL export ─────────────────────────────────────────

    def _direct_input_create_a(cpu: "CPU") -> None:
        # DirectInputCreateA(hInst, dwVersion, lplpDirectInput, pUnkOuter)
        # lplpDirectInput at ESP+12
        pp_di = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if pp_di:
            memory.write32(pp_di, DI_OBJ)
        logger.info("handlers", f"DirectInputCreateA -> DI_OBJ=0x{DI_OBJ:08x}")
        cpu.regs[EAX] = DI_OK
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("dinput.dll",  "DirectInputCreateA", _direct_input_create_a)
    stubs.register_handler("dinput8.dll", "DirectInputCreateA", _direct_input_create_a)

    logger.info("handlers", "DirectInput handlers registered — IDirectInput2A + IDirectInputDevice2A stubs wired")
