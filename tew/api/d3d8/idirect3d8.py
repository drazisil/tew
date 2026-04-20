"""IDirect3D8 COM vtable (16 slots) and Direct3DCreate8 factory.

Vtable slot order (matches d3d8.h):
  [0]  QueryInterface(REFIID, void**)
  [1]  AddRef()
  [2]  Release()
  [3]  RegisterSoftwareDevice(void*)
  [4]  GetAdapterCount() -> UINT
  [5]  GetAdapterIdentifier(Adapter, Flags, D3DADAPTER_IDENTIFIER8*)
  [6]  GetAdapterModeCount(Adapter) -> UINT
  [7]  EnumAdapterModes(Adapter, Mode, D3DDISPLAYMODE*)
  [8]  GetAdapterDisplayMode(Adapter, D3DDISPLAYMODE*)
  [9]  CheckDeviceType(Adapter, CheckType, DisplayFmt, BackFmt, Windowed)
  [10] CheckDeviceFormat(Adapter, DevType, AdapterFmt, Usage, RType, CheckFmt)
  [11] CheckDeviceMultiSampleType(Adapter, DevType, SurfaceFmt, Windowed, MultiSampleType)
  [12] CheckDepthStencilMatch(Adapter, DevType, AdapterFmt, RTFmt, DSFmt)
  [13] GetDeviceCaps(Adapter, DevType, D3DCAPS8*)
  [14] GetAdapterMonitor(Adapter) -> HMONITOR
  [15] CreateDevice(Adapter, DevType, hFocusWindow, BehaviorFlags, D3DPRESENT_PARAMETERS*, IDirect3DDevice8**)

Direct3DCreate8 initialises a VkInstance and enumerates physical devices.
Returns D3D8_OBJ on success; halts loudly on any Vulkan failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import cleanup_stdcall
from tew.logger import logger
from tew.api.d3d8._layout import D3D8_OBJ, D3DDEV_OBJ, D3DERR_NOTAVAIL, S_OK
from tew.api.d3d8._helpers import _com_stub, _set_eax
from tew.api.d3d8._caps import _fill_adapter_identifier, _fill_d3d_caps8
import tew.api.d3d8._state as _state

# Offset of DAT_6001c080 from dx8z.dll's preferred base (0x60000000).
# This flag controls whether setvideomode takes the CreateDevice (1) or Reset (0) path.
# BSS default is 0 (Reset), but no device exists on first init — must be 1 for CreateDevice.
_DX8Z_PREFERRED_BASE   = 0x60000000
_DAT_6001C080_OFFSET   = 0x6001C080 - _DX8Z_PREFERRED_BASE  # 0x1C080


def make_vtable(stubs: "Win32Handlers", memory: "Memory") -> list[int]:
    """Return the 16 trampoline addresses for the IDirect3D8 vtable."""

    # [5] GetAdapterIdentifier(Adapter, Flags, D3DADAPTER_IDENTIFIER8*)
    # Stack (past ret + this): Adapter, Flags, pIdent
    def _get_adapter_identifier(cpu: "CPU", mem: "Memory") -> None:
        p_ident = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        logger.info("d3d8", f"GetAdapterIdentifier pIdent=0x{p_ident:08x}")
        _fill_adapter_identifier(p_ident, mem)
        cpu.regs[EAX] = S_OK

    # [7] EnumAdapterModes(Adapter, Mode, D3DDISPLAYMODE*)
    def _enum_adapter_modes(cpu: "CPU", mem: "Memory") -> None:
        p_mode = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        if p_mode:
            mem.write32(p_mode,      800)   # Width
            mem.write32(p_mode + 4,  600)   # Height
            mem.write32(p_mode + 8,  60)    # RefreshRate
            mem.write32(p_mode + 12, 0x16)  # Format = D3DFMT_X8R8G8B8
        cpu.regs[EAX] = S_OK

    # [8] GetAdapterDisplayMode(Adapter, D3DDISPLAYMODE*)
    def _get_adapter_display_mode(cpu: "CPU", mem: "Memory") -> None:
        p_mode = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if p_mode:
            mem.write32(p_mode,      800)
            mem.write32(p_mode + 4,  600)
            mem.write32(p_mode + 8,  60)
            mem.write32(p_mode + 12, 0x16)
        cpu.regs[EAX] = S_OK

    # [13] GetDeviceCaps(Adapter, DevType, D3DCAPS8*)
    def _get_device_caps(cpu: "CPU", mem: "Memory") -> None:
        p_caps = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        logger.info("d3d8", f"GetDeviceCaps pCaps=0x{p_caps:08x}")
        _fill_d3d_caps8(p_caps, mem)
        cpu.regs[EAX] = S_OK

    # [15] CreateDevice(Adapter, DevType, hFocusWindow, BehaviorFlags,
    #                   D3DPRESENT_PARAMETERS*, IDirect3DDevice8**)
    def _create_device(cpu: "CPU", mem: "Memory") -> None:
        pp_device = mem.read32((cpu.regs[ESP] + 28) & 0xFFFFFFFF)
        if pp_device:
            mem.write32(pp_device, D3DDEV_OBJ)
        logger.info("d3d8", f"IDirect3D8::CreateDevice -> 0x{D3DDEV_OBJ:08x}")
        cpu.regs[EAX] = S_OK

    return [
        # [0]  QueryInterface
        _com_stub(stubs, "d3d8", "IDirect3D8::QueryInterface",
            lambda cpu, mem: _set_eax(cpu, 0x80004002), 8, memory),
        # [1]  AddRef
        _com_stub(stubs, "d3d8", "IDirect3D8::AddRef",
            lambda cpu, mem: _set_eax(cpu, 1), 0, memory),
        # [2]  Release
        _com_stub(stubs, "d3d8", "IDirect3D8::Release",
            lambda cpu, mem: _set_eax(cpu, 0), 0, memory),
        # [3]  RegisterSoftwareDevice
        _com_stub(stubs, "d3d8", "IDirect3D8::RegisterSoftwareDevice",
            lambda cpu, mem: _set_eax(cpu, D3DERR_NOTAVAIL), 4, memory),
        # [4]  GetAdapterCount
        _com_stub(stubs, "d3d8", "IDirect3D8::GetAdapterCount",
            lambda cpu, mem: _set_eax(cpu, 1), 0, memory),
        # [5]  GetAdapterIdentifier
        _com_stub(stubs, "d3d8", "IDirect3D8::GetAdapterIdentifier",
            _get_adapter_identifier, 12, memory),
        # [6]  GetAdapterModeCount
        _com_stub(stubs, "d3d8", "IDirect3D8::GetAdapterModeCount",
            lambda cpu, mem: _set_eax(cpu, 1), 4, memory),
        # [7]  EnumAdapterModes
        _com_stub(stubs, "d3d8", "IDirect3D8::EnumAdapterModes",
            _enum_adapter_modes, 12, memory),
        # [8]  GetAdapterDisplayMode
        _com_stub(stubs, "d3d8", "IDirect3D8::GetAdapterDisplayMode",
            _get_adapter_display_mode, 8, memory),
        # [9]  CheckDeviceType
        _com_stub(stubs, "d3d8", "IDirect3D8::CheckDeviceType",
            lambda cpu, mem: _set_eax(cpu, S_OK), 20, memory),
        # [10] CheckDeviceFormat
        _com_stub(stubs, "d3d8", "IDirect3D8::CheckDeviceFormat",
            lambda cpu, mem: _set_eax(cpu, S_OK), 24, memory),
        # [11] CheckDeviceMultiSampleType
        _com_stub(stubs, "d3d8", "IDirect3D8::CheckDeviceMultiSampleType",
            lambda cpu, mem: _set_eax(cpu, D3DERR_NOTAVAIL), 20, memory),
        # [12] CheckDepthStencilMatch
        _com_stub(stubs, "d3d8", "IDirect3D8::CheckDepthStencilMatch",
            lambda cpu, mem: _set_eax(cpu, S_OK), 20, memory),
        # [13] GetDeviceCaps
        _com_stub(stubs, "d3d8", "IDirect3D8::GetDeviceCaps",
            _get_device_caps, 12, memory),
        # [14] GetAdapterMonitor
        _com_stub(stubs, "d3d8", "IDirect3D8::GetAdapterMonitor",
            lambda cpu, mem: _set_eax(cpu, 0x0D3D0001), 4, memory),
        # [15] CreateDevice
        _com_stub(stubs, "d3d8", "IDirect3D8::CreateDevice",
            _create_device, 24, memory),
    ]


def _platform_vulkan_extensions() -> list[str]:
    """Return the Vulkan instance extensions required for surface creation on this host.

    VK_KHR_surface is always required. The platform-specific surface extension
    is selected by platform and, on Linux, by display server environment variables.
    This mirrors what a real D3D runtime does: it knows its platform at build time
    and doesn't ask SDL which extensions to request.
    """
    import os
    from sdl2.platform import SDL_GetPlatform

    extensions = ["VK_KHR_surface"]
    platform = SDL_GetPlatform().decode()

    if platform == "Linux":
        if os.environ.get("WAYLAND_DISPLAY"):
            extensions.append("VK_KHR_wayland_surface")
        else:
            # DISPLAY set or unset — default to xlib, most widely supported
            extensions.append("VK_KHR_xlib_surface")
    elif platform == "Windows":
        extensions.append("VK_KHR_win32_surface")
    elif platform == "Mac OS X":
        extensions.append("VK_EXT_metal_surface")
    else:
        logger.warn("d3d8", f"[Direct3DCreate8] Unknown platform '{platform}' — defaulting to VK_KHR_xlib_surface")
        extensions.append("VK_KHR_xlib_surface")

    return extensions


def make_create8(memory: "Memory") -> Callable:
    """Return the Direct3DCreate8 handler function.

    Direct3DCreate8(SDKVersion: UINT) -> IDirect3D8*   [stdcall, 1 arg]

    Initialises a VkInstance with the platform-appropriate surface extensions,
    enumerates physical devices, then returns D3D8_OBJ.
    Halts loudly on any Vulkan failure.
    """
    def _direct3d_create8(cpu: "CPU") -> None:
        import vulkan as vk

        extensions = _platform_vulkan_extensions()
        logger.info("d3d8", f"[Direct3DCreate8] Vulkan surface extensions: {extensions}")

        app_info = vk.VkApplicationInfo(
            sType=vk.VK_STRUCTURE_TYPE_APPLICATION_INFO,
            pApplicationName="tew",
            applicationVersion=vk.VK_MAKE_VERSION(1, 0, 0),
            pEngineName="tew",
            engineVersion=vk.VK_MAKE_VERSION(1, 0, 0),
            apiVersion=vk.VK_API_VERSION_1_0,
        )
        create_info = vk.VkInstanceCreateInfo(
            sType=vk.VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
            pApplicationInfo=app_info,
            enabledExtensionCount=len(extensions),
            ppEnabledExtensionNames=extensions,
            enabledLayerCount=0,
            ppEnabledLayerNames=[],
        )
        try:
            _state._vk_instance = vk.vkCreateInstance(create_info, None)
        except Exception as e:
            logger.error("d3d8", f"[Direct3DCreate8] vkCreateInstance failed: {e} — halting")
            cpu.halted = True
            return

        _state._vk_physical_devices = vk.vkEnumeratePhysicalDevices(_state._vk_instance)
        if not _state._vk_physical_devices:
            logger.error("d3d8", "[Direct3DCreate8] No Vulkan physical devices found — halting")
            cpu.halted = True
            return

        logger.info("d3d8", f"[Direct3DCreate8] VkInstance created, {len(_state._vk_physical_devices)} physical device(s) found")
        cpu.regs[EAX] = D3D8_OBJ
        cleanup_stdcall(cpu, memory, 4)

    return _direct3d_create8
