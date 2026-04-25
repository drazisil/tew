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
    from tew.api.window_manager import WindowManager

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


def make_vtable(stubs: "Win32Handlers", memory: "Memory", window_manager: "WindowManager") -> list[int]:
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
    # Stack (this at ESP+4, args at ESP+8..):
    #   ESP+4:  this
    #   ESP+8:  Adapter
    #   ESP+12: DevType
    #   ESP+16: hFocusWindow
    #   ESP+20: BehaviorFlags
    #   ESP+24: pPresentationParameters
    #   ESP+28: ppReturnedDeviceInterface
    def _create_device(cpu: "CPU", mem: "Memory") -> None:
        import os
        import ctypes
        import vulkan as vk
        from vulkan import ffi
        from sdl2 import SDL_DestroyRenderer
        from sdl2.syswm import SDL_SysWMinfo, SDL_GetWindowWMInfo

        pp_device  = mem.read32((cpu.regs[ESP] + 28) & 0xFFFFFFFF)
        hwnd       = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        pp_params  = mem.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        back_w = mem.read32(pp_params & 0xFFFFFFFF)       if pp_params else 0
        back_h = mem.read32((pp_params + 4) & 0xFFFFFFFF) if pp_params else 0

        logger.info("d3d8",
            f"IDirect3D8::CreateDevice hwnd=0x{hwnd:x} "
            f"back={back_w}x{back_h} ppdev=0x{pp_device:08x}")

        # ── Locate SDL window ──────────────────────────────────────────────
        entry = window_manager.get_window(hwnd)
        if entry is None or entry.sdl_window is None:
            # Fall back to first top-level window that has an SDL window
            for e in window_manager._windows.values():
                if e.sdl_window is not None:
                    entry = e
                    break
        if entry is None or entry.sdl_window is None:
            logger.error("d3d8", "CreateDevice: no SDL window found — halting")
            cpu.halted = True
            return

        sdl_window = entry.sdl_window

        # Destroy the SDL renderer — Vulkan presentation takes over this window
        if entry.sdl_renderer is not None:
            SDL_DestroyRenderer(entry.sdl_renderer)
            entry.sdl_renderer = None

        # ── Load instance-level KHR extension functions ────────────────────
        try:
            vkGetSurfaceSupport = vk.vkGetInstanceProcAddr(
                _state._vk_instance, 'vkGetPhysicalDeviceSurfaceSupportKHR')
            vkGetSurfaceCaps = vk.vkGetInstanceProcAddr(
                _state._vk_instance, 'vkGetPhysicalDeviceSurfaceCapabilitiesKHR')
            vkGetSurfaceFormats = vk.vkGetInstanceProcAddr(
                _state._vk_instance, 'vkGetPhysicalDeviceSurfaceFormatsKHR')
            vkGetPresentModes = vk.vkGetInstanceProcAddr(
                _state._vk_instance, 'vkGetPhysicalDeviceSurfacePresentModesKHR')
        except Exception as exc:
            logger.error("d3d8",
                f"CreateDevice: failed to load surface extension functions: {exc} — halting")
            cpu.halted = True
            return

        # ── Create VkSurfaceKHR ────────────────────────────────────────────
        try:
            wm_info = SDL_SysWMinfo()
            wm_info.version.major = 2
            wm_info.version.minor = 0
            wm_info.version.patch = 0
            SDL_GetWindowWMInfo(sdl_window, ctypes.byref(wm_info))

            if os.environ.get("WAYLAND_DISPLAY"):
                vkCreateSurface = vk.vkGetInstanceProcAddr(
                    _state._vk_instance, 'vkCreateWaylandSurfaceKHR')
                wl_display_ptr = wm_info.info.wl.display or 0
                disp = ffi.cast('struct wl_display *', wl_display_ptr)
                surf = ffi.cast('struct wl_surface *',
                                wm_info.info.wl.surface or 0)
                surface_ci = vk.VkWaylandSurfaceCreateInfoKHR(
                    sType=vk.VK_STRUCTURE_TYPE_WAYLAND_SURFACE_CREATE_INFO_KHR,
                    display=disp,
                    surface=surf,
                )
            else:
                wl_display_ptr = 0
                vkCreateSurface = vk.vkGetInstanceProcAddr(
                    _state._vk_instance, 'vkCreateXlibSurfaceKHR')
                disp = ffi.cast('Display *', wm_info.info.x11.display or 0)
                surface_ci = vk.VkXlibSurfaceCreateInfoKHR(
                    sType=vk.VK_STRUCTURE_TYPE_XLIB_SURFACE_CREATE_INFO_KHR,
                    dpy=disp,
                    window=int(wm_info.info.x11.window),
                )
            _state._vk_surface = vkCreateSurface(
                _state._vk_instance, surface_ci, None)

            # On Wayland the compositor hasn't acknowledged the surface yet,
            # so surface-property queries (support, caps, formats) deadlock
            # unless we flush all pending Wayland events first.
            if wl_display_ptr:
                import ctypes as _ct
                _libwl = _ct.CDLL('libwayland-client.so.0')
                _libwl.wl_display_roundtrip.restype  = _ct.c_int
                _libwl.wl_display_roundtrip.argtypes = [_ct.c_void_p]
                _libwl.wl_display_roundtrip(_ct.c_void_p(wl_display_ptr))
        except Exception as exc:
            logger.error("d3d8",
                f"CreateDevice: VkSurface creation failed: {exc} — halting")
            cpu.halted = True
            return

        logger.info("d3d8", "CreateDevice: VkSurfaceKHR created")

        # ── Find queue family with graphics + present support ──────────────
        phys_dev = _state._vk_physical_devices[0]
        queue_families = vk.vkGetPhysicalDeviceQueueFamilyProperties(phys_dev)
        gfx_family = -1
        for i, qf in enumerate(queue_families):
            if qf.queueFlags & vk.VK_QUEUE_GRAPHICS_BIT:
                try:
                    supported = vkGetSurfaceSupport(
                        phys_dev, i, _state._vk_surface)
                    if supported:
                        gfx_family = i
                        break
                except Exception:
                    continue
        if gfx_family < 0:
            logger.error("d3d8",
                "CreateDevice: no GRAPHICS queue family found — halting")
            cpu.halted = True
            return
        _state._vk_graphics_queue_family = gfx_family
        _state._vk_present_queue_family  = gfx_family

        # ── Create logical VkDevice ────────────────────────────────────────
        try:
            q_priority = ffi.new('float[1]', [1.0])
            queue_ci = vk.VkDeviceQueueCreateInfo(
                sType=vk.VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
                queueFamilyIndex=gfx_family,
                queueCount=1,
                pQueuePriorities=q_priority,
            )
            device_ci = vk.VkDeviceCreateInfo(
                sType=vk.VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
                queueCreateInfoCount=1,
                pQueueCreateInfos=[queue_ci],
                enabledExtensionCount=1,
                ppEnabledExtensionNames=["VK_KHR_swapchain"],
                enabledLayerCount=0,
                ppEnabledLayerNames=[],
            )
            _state._vk_device = vk.vkCreateDevice(phys_dev, device_ci, None)
        except Exception as exc:
            logger.error("d3d8",
                f"CreateDevice: vkCreateDevice failed: {exc} — halting")
            cpu.halted = True
            return

        _state._vk_graphics_queue = vk.vkGetDeviceQueue(
            _state._vk_device, gfx_family, 0)
        _state._vk_present_queue = _state._vk_graphics_queue
        logger.info("d3d8",
            f"CreateDevice: VkDevice ready, graphics queue family={gfx_family}")

        # ── Load device-level KHR extension functions ──────────────────────
        try:
            _state._vk_fn_create_swapchain = vk.vkGetDeviceProcAddr(
                _state._vk_device, 'vkCreateSwapchainKHR')
            _state._vk_fn_get_swapchain_images = vk.vkGetDeviceProcAddr(
                _state._vk_device, 'vkGetSwapchainImagesKHR')
            _state._vk_fn_acquire_next_image = vk.vkGetDeviceProcAddr(
                _state._vk_device, 'vkAcquireNextImageKHR')
            _state._vk_fn_queue_present = vk.vkGetDeviceProcAddr(
                _state._vk_device, 'vkQueuePresentKHR')
        except Exception as exc:
            logger.error("d3d8",
                f"CreateDevice: failed to load swapchain extension functions: {exc} — halting")
            cpu.halted = True
            return

        # ── Create swapchain ───────────────────────────────────────────────
        try:
            caps = vkGetSurfaceCaps(phys_dev, _state._vk_surface)

            # Choose format: prefer VK_FORMAT_B8G8R8A8_UNORM (44)
            formats = vkGetSurfaceFormats(phys_dev, _state._vk_surface)
            chosen_fmt   = int(formats[0].format)
            chosen_space = int(formats[0].colorSpace)
            for f in formats:
                if int(f.format) == vk.VK_FORMAT_B8G8R8A8_UNORM:
                    chosen_fmt   = int(f.format)
                    chosen_space = int(f.colorSpace)
                    break
            _state._vk_swapchain_format = chosen_fmt

            # Extent: use D3DPRESENT_PARAMETERS values when provided
            if back_w > 0 and back_h > 0:
                w, h = back_w, back_h
            elif caps.currentExtent.width != 0xFFFFFFFF:
                w = int(caps.currentExtent.width)
                h = int(caps.currentExtent.height)
            else:
                w = max(int(caps.minImageExtent.width),
                        min(int(caps.maxImageExtent.width),  800))
                h = max(int(caps.minImageExtent.height),
                        min(int(caps.maxImageExtent.height), 600))
            _state._vk_swapchain_width  = w
            _state._vk_swapchain_height = h

            img_count = int(caps.minImageCount) + 1
            if int(caps.maxImageCount) > 0:
                img_count = min(img_count, int(caps.maxImageCount))

            swapchain_ci = vk.VkSwapchainCreateInfoKHR(
                sType=vk.VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR,
                surface=_state._vk_surface,
                minImageCount=img_count,
                imageFormat=chosen_fmt,
                imageColorSpace=chosen_space,
                imageExtent=vk.VkExtent2D(width=w, height=h),
                imageArrayLayers=1,
                imageUsage=(vk.VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT |
                            vk.VK_IMAGE_USAGE_TRANSFER_DST_BIT),
                imageSharingMode=vk.VK_SHARING_MODE_EXCLUSIVE,
                queueFamilyIndexCount=0,
                pQueueFamilyIndices=None,
                preTransform=caps.currentTransform,
                compositeAlpha=vk.VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR,
                presentMode=vk.VK_PRESENT_MODE_FIFO_KHR,
                clipped=vk.VK_TRUE,
                oldSwapchain=None,
            )
            _state._vk_swapchain = _state._vk_fn_create_swapchain(
                _state._vk_device, swapchain_ci, None)

            raw_imgs = _state._vk_fn_get_swapchain_images(
                _state._vk_device, _state._vk_swapchain)
            _state._vk_swapchain_images = list(raw_imgs)
        except Exception as exc:
            logger.error("d3d8",
                f"CreateDevice: swapchain creation failed: {exc} — halting")
            cpu.halted = True
            return

        logger.info("d3d8",
            f"CreateDevice: swapchain {w}x{h} fmt={chosen_fmt} "
            f"images={len(_state._vk_swapchain_images)}")

        # ── Command pool + command buffer ──────────────────────────────────
        try:
            pool_ci = vk.VkCommandPoolCreateInfo(
                sType=vk.VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
                flags=vk.VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT,
                queueFamilyIndex=gfx_family,
            )
            _state._vk_command_pool = vk.vkCreateCommandPool(
                _state._vk_device, pool_ci, None)

            alloc_info = vk.VkCommandBufferAllocateInfo(
                sType=vk.VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
                commandPool=_state._vk_command_pool,
                level=vk.VK_COMMAND_BUFFER_LEVEL_PRIMARY,
                commandBufferCount=1,
            )
            cmd_bufs = vk.vkAllocateCommandBuffers(
                _state._vk_device, alloc_info)
            _state._vk_cmd_buf = cmd_bufs[0]
        except Exception as exc:
            logger.error("d3d8",
                f"CreateDevice: command pool/buffer creation failed: {exc} — halting")
            cpu.halted = True
            return

        # ── Sync primitives ────────────────────────────────────────────────
        try:
            sem_ci = vk.VkSemaphoreCreateInfo(
                sType=vk.VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO)
            # Fence starts signalled so the first BeginScene doesn't block
            fence_ci = vk.VkFenceCreateInfo(
                sType=vk.VK_STRUCTURE_TYPE_FENCE_CREATE_INFO,
                flags=vk.VK_FENCE_CREATE_SIGNALED_BIT)
            _state._vk_image_available = vk.vkCreateSemaphore(
                _state._vk_device, sem_ci, None)
            _state._vk_render_done = vk.vkCreateSemaphore(
                _state._vk_device, sem_ci, None)
            _state._vk_in_flight = vk.vkCreateFence(
                _state._vk_device, fence_ci, None)
        except Exception as exc:
            logger.error("d3d8",
                f"CreateDevice: sync primitive creation failed: {exc} — halting")
            cpu.halted = True
            return

        if pp_device:
            mem.write32(pp_device, D3DDEV_OBJ)
        logger.info("d3d8",
            f"IDirect3D8::CreateDevice complete -> D3DDEV_OBJ=0x{D3DDEV_OBJ:08x}")
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
