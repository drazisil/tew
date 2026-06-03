"""IDirect3DDevice8 COM vtable — 97 slots.

Vtable slot order (matches d3d8.h):
  [0]   QueryInterface(REFIID, void**)
  [1]   AddRef()
  [2]   Release()
  [3]   TestCooperativeLevel()
  [4]   GetAvailableTextureMem() -> UINT
  [5]   ResourceManagerDiscardBytes(Bytes)
  [6]   GetDirect3D(IDirect3D8**)
  [7]   GetDeviceCaps(D3DCAPS8*)
  [8]   GetDisplayMode(D3DDISPLAYMODE*)
  [9]   GetCreationParameters(D3DDEVICE_CREATION_PARAMETERS*)
  [10]  SetCursorProperties(XHotSpot, YHotSpot, IDirect3DSurface8*)
  [11]  SetCursorPosition(X, Y, Flags)
  [12]  ShowCursor(bShow) -> BOOL
  [13]  CreateAdditionalSwapChain(D3DPRESENT_PARAMETERS*, IDirect3DSwapChain8**)
  [14]  Reset(D3DPRESENT_PARAMETERS*)
  [15]  Present(pSrc, pDest, hWnd, pRegion)
  [16]  GetBackBuffer(UINT, D3DBACKBUFFER_TYPE, IDirect3DSurface8**)
  [17]  GetRasterStatus(D3DRASTER_STATUS*)
  [18]  SetGammaRamp(Flags, D3DGAMMARAMP*)
  [19]  GetGammaRamp(D3DGAMMARAMP*)
  [20]  CreateTexture(W, H, Levels, Usage, Fmt, Pool, IDirect3DTexture8**)
  [21]  CreateVolumeTexture(W, H, D, Levels, Usage, Fmt, Pool, IDirect3DVolumeTexture8**)
  [22]  CreateCubeTexture(EdgeLength, Levels, Usage, Fmt, Pool, IDirect3DCubeTexture8**)
  [23]  CreateVertexBuffer(Length, Usage, FVF, Pool, IDirect3DVertexBuffer8**)
  [24]  CreateIndexBuffer(Length, Usage, Fmt, Pool, IDirect3DIndexBuffer8**)
  [25]  CreateRenderTarget(W, H, Fmt, MultiSample, Lockable, IDirect3DSurface8**)
  [26]  CreateDepthStencilSurface(W, H, Fmt, MultiSample, IDirect3DSurface8**)
  [27]  CreateImageSurface(W, H, Fmt, IDirect3DSurface8**)
  [28]  CopyRects(...)
  [29]  UpdateTexture(pSrc, pDest)
  [30]  GetFrontBuffer(IDirect3DSurface8*)
  [31]  SetRenderTarget(pRT, pNewZStencil)
  [32]  GetRenderTarget(IDirect3DSurface8**)
  [33]  GetDepthStencilSurface(IDirect3DSurface8**)
  [34]  BeginScene()
  [35]  EndScene()
  [36]  Clear(Count, pRects, Flags, Color, Z, Stencil)
  [37]  SetTransform(State, pMatrix)
  [38]  GetTransform(State, pMatrix)
  [39]  MultiplyTransform(State, pMatrix)
  [40]  SetViewport(D3DVIEWPORT8*)
  [41]  GetViewport(D3DVIEWPORT8*)
  [42]  SetMaterial(D3DMATERIAL8*)
  [43]  GetMaterial(D3DMATERIAL8*)
  [44]  SetLight(Index, D3DLIGHT8*)
  [45]  GetLight(Index, D3DLIGHT8*)
  [46]  LightEnable(Index, Enable)
  [47]  GetLightEnable(Index, pEnable)
  [48]  SetClipPlane(Index, pPlane)
  [49]  GetClipPlane(Index, pPlane)
  [50]  SetRenderState(State, Value)
  [51]  GetRenderState(State, DWORD* pValue)
  [52]  BeginStateBlock()
  [53]  EndStateBlock(DWORD* pToken)
  [54]  ApplyStateBlock(Token)
  [55]  CaptureStateBlock(Token)
  [56]  DeleteStateBlock(Token)
  [57]  CreateStateBlock(Type, DWORD* pToken)
  [58]  SetClipStatus(D3DCLIPSTATUS8*)
  [59]  GetClipStatus(D3DCLIPSTATUS8*)
  [60]  GetTexture(Stage, IDirect3DBaseTexture8**)
  [61]  SetTexture(Stage, IDirect3DBaseTexture8*)
  [62]  GetTextureStageState(Stage, Type, DWORD* pValue)
  [63]  SetTextureStageState(Stage, Type, Value)
  [64]  ValidateDevice(DWORD* pNumPasses)
  [65]  GetInfo(DevInfoID, pDevInfoStruct, DevInfoStructSize)
  [66]  SetPaletteEntries(PaletteNumber, pEntries)
  [67]  GetPaletteEntries(PaletteNumber, pEntries)
  [68]  SetCurrentTexturePalette(PaletteNumber)
  [69]  GetCurrentTexturePalette(pPaletteNumber)
  [70]  DrawPrimitive(PrimType, StartVertex, PrimCount)
  [71]  DrawIndexedPrimitive(PrimType, minIndex, NumVerts, startIndex, primCount)
  [72]  DrawPrimitiveUP(PrimType, PrimCount, pVertexStreamZeroData, VertexStreamZeroStride)
  [73]  DrawIndexedPrimitiveUP(8 args)
  [74]  ProcessVertices(SrcStartIndex, DestIndex, VertexCount, pDestBuffer, Flags)
  [75]  CreateVertexShader(pDecl, pFunction, DWORD* pHandle, Usage)
  [76]  SetVertexShader(Handle)
  [77]  GetVertexShader(DWORD* pHandle)
  [78]  DeleteVertexShader(Handle)
  [79]  SetVertexShaderConstant(Register, pConstantData, ConstantCount)
  [80]  GetVertexShaderConstant(Register, pConstantData, ConstantCount)
  [81]  GetVertexShaderDeclaration(Handle, pData, pSizeOfData)
  [82]  GetVertexShaderFunction(Handle, pData, pSizeOfData)
  [83]  SetStreamSource(StreamNumber, pStreamData, Stride)
  [84]  GetStreamSource(StreamNumber, IDirect3DVertexBuffer8**, UINT* pStride)
  [85]  SetIndices(pIndexData, BaseVertexIndex)
  [86]  GetIndices(IDirect3DIndexBuffer8**, UINT* pBaseVertexIndex)
  [87]  CreatePixelShader(pFunction, DWORD* pHandle)
  [88]  SetPixelShader(Handle)
  [89]  GetPixelShader(DWORD* pHandle)
  [90]  DeletePixelShader(Handle)
  [91]  SetPixelShaderConstant(Register, pConstantData, ConstantCount)
  [92]  GetPixelShaderConstant(Register, pConstantData, ConstantCount)
  [93]  GetPixelShaderFunction(Handle, pData, pSizeOfData)
  [94]  DrawRectPatch(Handle, pNumSegs, pTriPatchInfo)
  [95]  DrawTriPatch(Handle, pNumSegs, pTriPatchInfo)
  [96]  DeletePatch(Handle)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers

import struct as _struct

from tew.hardware.cpu import EAX, ECX, ESP
from tew.logger import logger
from tew.api.d3d8._layout import D3D8_OBJ, D3DDEV_OBJ, S_OK
from tew.api.d3d8._helpers import _alloc_resource_obj, _alloc_surface_obj, _com_stub, _set_eax, vk_pump
from tew.api.d3d8._caps import _fill_d3d_caps8
import tew.api.d3d8._state as _state


def make_vtable(stubs: "Win32Handlers", memory: "Memory") -> list[int]:
    """Return the 97 trampoline addresses for the IDirect3DDevice8 vtable."""

    def _ok(name: str, arg_bytes: int) -> int:
        return _com_stub(stubs, "d3d8dev", name,
            lambda cpu, mem: _set_eax(cpu, S_OK), arg_bytes, memory, D3DDEV_OBJ)

    def _void(name: str, arg_bytes: int) -> int:
        return _com_stub(stubs, "d3d8dev", name,
            lambda cpu, mem: None, arg_bytes, memory, D3DDEV_OBJ)

    def _uint(name: str, arg_bytes: int, val: int) -> int:
        return _com_stub(stubs, "d3d8dev", name,
            lambda cpu, mem: _set_eax(cpu, val), arg_bytes, memory, D3DDEV_OBJ)

    def _halt(name: str, arg_bytes: int) -> int:
        def _handler(cpu: "CPU", mem: "Memory") -> None:
            logger.error("d3d8", f"UNIMPLEMENTED: {name} — halting")
            cpu.halted = True
        return _com_stub(stubs, "d3d8dev", name, _handler, arg_bytes, memory, D3DDEV_OBJ)

    # [6] GetDirect3D(IDirect3D8**)
    def _get_direct3d(cpu: "CPU", mem: "Memory") -> None:
        pp = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if pp:
            mem.write32(pp, D3D8_OBJ)
        cpu.regs[EAX] = S_OK

    # [7] GetDeviceCaps(D3DCAPS8*)
    def _get_device_caps(cpu: "CPU", mem: "Memory") -> None:
        p_caps = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        _fill_d3d_caps8(p_caps, mem)
        cpu.regs[EAX] = S_OK

    # [8] GetDisplayMode(D3DDISPLAYMODE*)
    def _get_display_mode(cpu: "CPU", mem: "Memory") -> None:
        p_mode = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_mode:
            mem.write32(p_mode,      800)
            mem.write32(p_mode + 4,  600)
            mem.write32(p_mode + 8,  60)
            mem.write32(p_mode + 12, 0x16)
        cpu.regs[EAX] = S_OK

    # [9] GetCreationParameters(D3DDEVICE_CREATION_PARAMETERS*)
    def _get_creation_params(cpu: "CPU", mem: "Memory") -> None:
        p = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p:
            mem.write32(p,      0)       # AdapterOrdinal
            mem.write32(p + 4,  1)       # DeviceType = D3DDEVTYPE_HAL
            mem.write32(p + 8,  0xABCD)  # hFocusWindow (fake HWND)
            mem.write32(p + 12, 0x40)    # BehaviorFlags = D3DCREATE_HARDWARE_VERTEXPROCESSING
        cpu.regs[EAX] = S_OK

    # [14] Reset(D3DPRESENT_PARAMETERS*)
    def _reset(cpu: "CPU", mem: "Memory") -> None:
        logger.info("d3d8", "IDirect3DDevice8::Reset")
        cpu.regs[EAX] = S_OK

    # [16] GetBackBuffer(UINT, D3DBACKBUFFER_TYPE, IDirect3DSurface8**)
    def _get_back_buffer(cpu: "CPU", mem: "Memory") -> None:
        pp_surface = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        # Use swapchain dimensions if known, else fall back to 800×600
        w = _state._vk_swapchain_width  or 800
        h = _state._vk_swapchain_height or 600
        surf = _alloc_surface_obj(w, h, 0x16, mem)  # D3DFMT_X8R8G8B8 = 0x16
        if pp_surface:
            mem.write32(pp_surface, surf)
        cpu.regs[EAX] = S_OK

    # [20] CreateTexture(W, H, Levels, Usage, Fmt, Pool, IDirect3DTexture8**)
    def _create_texture(cpu: "CPU", mem: "Memory") -> None:
        w          = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        h          = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        fmt        = mem.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        pp_texture = mem.read32((cpu.regs[ESP] + 32) & 0xFFFFFFFF)
        tex = _alloc_surface_obj(w or 1, h or 1, fmt, mem)
        if pp_texture:
            mem.write32(pp_texture, tex)
        cpu.regs[EAX] = S_OK

    # [21] CreateVolumeTexture(W, H, D, Levels, Usage, Fmt, Pool, IDirect3DVolumeTexture8**)
    def _create_volume_texture(cpu: "CPU", mem: "Memory") -> None:
        w          = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        h          = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        fmt        = mem.read32((cpu.regs[ESP] + 28) & 0xFFFFFFFF)
        pp_texture = mem.read32((cpu.regs[ESP] + 36) & 0xFFFFFFFF)
        if pp_texture:
            mem.write32(pp_texture, _alloc_surface_obj(w or 1, h or 1, fmt, mem))
        cpu.regs[EAX] = S_OK

    # [22] CreateCubeTexture(EdgeLength, Levels, Usage, Fmt, Pool, IDirect3DCubeTexture8**)
    def _create_cube_texture(cpu: "CPU", mem: "Memory") -> None:
        edge       = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        fmt        = mem.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        pp_texture = mem.read32((cpu.regs[ESP] + 28) & 0xFFFFFFFF)
        if pp_texture:
            mem.write32(pp_texture, _alloc_surface_obj(edge or 1, edge or 1, fmt, mem))
        cpu.regs[EAX] = S_OK

    # [23] CreateVertexBuffer(Length, Usage, FVF, Pool, IDirect3DVertexBuffer8**)
    def _create_vertex_buffer(cpu: "CPU", mem: "Memory") -> None:
        length = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        pp_vb  = mem.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        if pp_vb:
            mem.write32(pp_vb, _alloc_resource_obj(length or 4, mem))
        cpu.regs[EAX] = S_OK

    # [24] CreateIndexBuffer(Length, Usage, Fmt, Pool, IDirect3DIndexBuffer8**)
    def _create_index_buffer(cpu: "CPU", mem: "Memory") -> None:
        length = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        pp_ib  = mem.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        if pp_ib:
            mem.write32(pp_ib, _alloc_resource_obj(length or 4, mem))
        cpu.regs[EAX] = S_OK

    # [25] CreateRenderTarget(W, H, Fmt, MultiSample, Lockable, IDirect3DSurface8**)
    def _create_render_target(cpu: "CPU", mem: "Memory") -> None:
        w       = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        h       = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        fmt     = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        pp_surf = mem.read32((cpu.regs[ESP] + 28) & 0xFFFFFFFF)
        if pp_surf:
            mem.write32(pp_surf, _alloc_surface_obj(w or 1, h or 1, fmt, mem))
        cpu.regs[EAX] = S_OK

    # [26] CreateDepthStencilSurface(W, H, Fmt, MultiSample, IDirect3DSurface8**)
    def _create_depth_stencil(cpu: "CPU", mem: "Memory") -> None:
        w       = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        h       = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        fmt     = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        pp_surf = mem.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        if pp_surf:
            mem.write32(pp_surf, _alloc_surface_obj(w or 1, h or 1, fmt, mem))
        cpu.regs[EAX] = S_OK

    # [27] CreateImageSurface(W, H, Fmt, IDirect3DSurface8**)
    def _create_image_surface(cpu: "CPU", mem: "Memory") -> None:
        w       = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        h       = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        fmt     = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        pp_surf = mem.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        if pp_surf:
            mem.write32(pp_surf, _alloc_surface_obj(w or 1, h or 1, fmt, mem))
        cpu.regs[EAX] = S_OK

    # [32] GetRenderTarget(IDirect3DSurface8**)
    def _get_render_target(cpu: "CPU", mem: "Memory") -> None:
        pp_surf = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        w = _state._vk_swapchain_width  or 800
        h = _state._vk_swapchain_height or 600
        if pp_surf:
            mem.write32(pp_surf, _alloc_surface_obj(w, h, 0x16, mem))
        cpu.regs[EAX] = S_OK

    # [33] GetDepthStencilSurface(IDirect3DSurface8**)
    def _get_depth_stencil(cpu: "CPU", mem: "Memory") -> None:
        pp_surf = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        w = _state._vk_swapchain_width  or 800
        h = _state._vk_swapchain_height or 600
        if pp_surf:
            mem.write32(pp_surf, _alloc_surface_obj(w, h, 0x4F, mem))  # D3DFMT_D24S8 = 0x4F
        cpu.regs[EAX] = S_OK

    def _rebuild_swapchain() -> None:
        """Recreate the swapchain using the current surface extent."""
        import vulkan as vk

        phys_dev = _state._vk_physical_devices[0]
        caps = _state._vk_fn_get_surface_caps(phys_dev, _state._vk_surface)

        w = caps.currentExtent.width
        h = caps.currentExtent.height
        # 0xFFFFFFFF means driver wants us to pick; fall back to last known size
        if w == 0xFFFFFFFF:
            w = _state._vk_swapchain_width
            h = _state._vk_swapchain_height

        old_swapchain = _state._vk_swapchain
        swapchain_ci = vk.VkSwapchainCreateInfoKHR(
            sType=vk.VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR,
            surface=_state._vk_surface,
            minImageCount=2,
            imageFormat=_state._vk_swapchain_format,
            imageColorSpace=0,   # VK_COLOR_SPACE_SRGB_NONLINEAR_KHR
            imageExtent=vk.VkExtent2D(width=w, height=h),
            imageArrayLayers=1,
            imageUsage=(vk.VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT |
                        vk.VK_IMAGE_USAGE_TRANSFER_DST_BIT),
            imageSharingMode=vk.VK_SHARING_MODE_EXCLUSIVE,
            queueFamilyIndexCount=0,
            pQueueFamilyIndices=None,
            preTransform=0x00000001,  # VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR
            compositeAlpha=vk.VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR,
            presentMode=vk.VK_PRESENT_MODE_FIFO_KHR,
            clipped=vk.VK_TRUE,
            oldSwapchain=old_swapchain,
        )
        new_swapchain = vk_pump(
            lambda: _state._vk_fn_create_swapchain(
                _state._vk_device, swapchain_ci, None))

        _state._vk_fn_destroy_swapchain(_state._vk_device, old_swapchain, None)

        raw_imgs = _state._vk_fn_get_swapchain_images(
            _state._vk_device, new_swapchain)
        _state._vk_swapchain        = new_swapchain
        _state._vk_swapchain_images = list(raw_imgs)
        _state._vk_swapchain_width  = w
        _state._vk_swapchain_height = h
        logger.info("d3d8",
            f"_rebuild_swapchain: {w}x{h} images={len(_state._vk_swapchain_images)}")

    # [34] BeginScene()
    # Waits for the previous frame fence, acquires the next swapchain image, and
    # opens the command buffer.  Transitions the image to TRANSFER_DST_OPTIMAL so
    # Clear can vkCmdClearColorImage without an additional barrier.
    def _begin_scene(cpu: "CPU", mem: "Memory") -> None:
        import vulkan as vk

        if _state._vk_device is None:
            logger.error("d3d8", "BeginScene: device not initialised — halting")
            cpu.halted = True
            return

        logger.info("d3d8", "BeginScene: ENTER")

        def _wait_and_acquire():
            # vkWaitForFences can call wl_display_roundtrip on Mesa/Wayland WSI;
            # run on background thread so the main thread keeps pumping SDL events.
            # Only wait if a frame was actually submitted (Present called); D3D8
            # allows BeginScene/EndScene without Present, which leaves the fence
            # unsignaled — waiting on it would deadlock.
            if _state._vk_frame_submitted:
                vk.vkWaitForFences(
                    _state._vk_device, 1, [_state._vk_in_flight],
                    vk.VK_TRUE, 0xFFFFFFFFFFFFFFFF)
                vk.vkResetFences(_state._vk_device, 1, [_state._vk_in_flight])
                _state._vk_frame_submitted = False
            # Re-use existing acquired image if Present was never called; calling
            # AcquireNextImage again would leave _vk_image_available signaled twice
            # and miscount against the swapchain's available image count.
            if _state._vk_image_acquired:
                return _state._vk_current_image_idx
            idx = _state._vk_fn_acquire_next_image(
                _state._vk_device, _state._vk_swapchain,
                0xFFFFFFFFFFFFFFFF, _state._vk_image_available, None)
            _state._vk_image_acquired = True
            return idx

        try:
            try:
                idx = vk_pump(_wait_and_acquire)
            except Exception as acq_exc:
                if "OutOfDate" in type(acq_exc).__name__:
                    logger.info("d3d8", "BeginScene: swapchain out-of-date, rebuilding")
                    _rebuild_swapchain()
                    idx = vk_pump(_wait_and_acquire)
                else:
                    raise
            _state._vk_current_image_idx = int(idx)

            vk.vkResetCommandBuffer(_state._vk_cmd_buf, 0)
            begin_info = vk.VkCommandBufferBeginInfo(
                sType=vk.VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
                flags=vk.VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT,
            )
            vk.vkBeginCommandBuffer(_state._vk_cmd_buf, begin_info)

            # Transition swapchain image UNDEFINED → TRANSFER_DST_OPTIMAL
            image = _state._vk_swapchain_images[_state._vk_current_image_idx]
            subresource = vk.VkImageSubresourceRange(
                aspectMask=vk.VK_IMAGE_ASPECT_COLOR_BIT,
                baseMipLevel=0, levelCount=1,
                baseArrayLayer=0, layerCount=1,
            )
            barrier = vk.VkImageMemoryBarrier(
                sType=vk.VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
                srcAccessMask=0,
                dstAccessMask=vk.VK_ACCESS_TRANSFER_WRITE_BIT,
                oldLayout=vk.VK_IMAGE_LAYOUT_UNDEFINED,
                newLayout=vk.VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                srcQueueFamilyIndex=vk.VK_QUEUE_FAMILY_IGNORED,
                dstQueueFamilyIndex=vk.VK_QUEUE_FAMILY_IGNORED,
                image=image,
                subresourceRange=subresource,
            )
            vk.vkCmdPipelineBarrier(
                _state._vk_cmd_buf,
                vk.VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                vk.VK_PIPELINE_STAGE_TRANSFER_BIT,
                0, 0, None, 0, None, 1, [barrier],
            )
        except Exception as exc:
            logger.error("d3d8",
                f"BeginScene failed: {type(exc).__name__}: {exc!r} — halting")
            cpu.halted = True
            return

        # Transition TRANSFER_DST_OPTIMAL → COLOR_ATTACHMENT_OPTIMAL so the
        # render pass (loadOp=LOAD, initialLayout=COLOR_ATTACHMENT_OPTIMAL) can
        # draw on top of the cleared background.
        try:
            image = _state._vk_swapchain_images[_state._vk_current_image_idx]
            subresource = vk.VkImageSubresourceRange(
                aspectMask=vk.VK_IMAGE_ASPECT_COLOR_BIT,
                baseMipLevel=0, levelCount=1,
                baseArrayLayer=0, layerCount=1,
            )
            to_ca_barrier = vk.VkImageMemoryBarrier(
                sType=vk.VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
                srcAccessMask=vk.VK_ACCESS_TRANSFER_WRITE_BIT,
                dstAccessMask=vk.VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,
                oldLayout=vk.VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                newLayout=vk.VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
                srcQueueFamilyIndex=vk.VK_QUEUE_FAMILY_IGNORED,
                dstQueueFamilyIndex=vk.VK_QUEUE_FAMILY_IGNORED,
                image=image,
                subresourceRange=subresource,
            )
            vk.vkCmdPipelineBarrier(
                _state._vk_cmd_buf,
                vk.VK_PIPELINE_STAGE_TRANSFER_BIT,
                vk.VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                0, 0, None, 0, None, 1, [to_ca_barrier],
            )

            # Begin render pass so Draw* commands can record into the framebuffer
            if _state._vk_render_pass is not None:
                fb = _state._vk_framebuffers[_state._vk_current_image_idx]
                rp_begin = vk.VkRenderPassBeginInfo(
                    sType=vk.VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO,
                    renderPass=_state._vk_render_pass,
                    framebuffer=fb,
                    renderArea=vk.VkRect2D(
                        vk.VkOffset2D(0, 0),
                        vk.VkExtent2D(_state._vk_swapchain_width,
                                      _state._vk_swapchain_height)),
                    clearValueCount=0,
                )
                vk.vkCmdBeginRenderPass(
                    _state._vk_cmd_buf, rp_begin,
                    vk.VK_SUBPASS_CONTENTS_INLINE)
                _state._vk_in_render_pass = True
        except Exception as exc:
            logger.error("d3d8",
                f"BeginScene: render pass setup failed: {exc} — halting")
            cpu.halted = True
            return

        logger.info("d3d8",
            f"BeginScene: OK image_idx={_state._vk_current_image_idx}")
        cpu.regs[EAX] = S_OK

    # [35] EndScene()
    # Ends the render pass and command buffer; the final image barrier (to
    # PRESENT_SRC_KHR) is now handled by the render pass finalLayout.
    def _end_scene(cpu: "CPU", mem: "Memory") -> None:
        import vulkan as vk

        if _state._vk_device is None:
            logger.error("d3d8", "EndScene: device not initialised — halting")
            cpu.halted = True
            return

        try:
            if _state._vk_in_render_pass:
                vk.vkCmdEndRenderPass(_state._vk_cmd_buf)
                _state._vk_in_render_pass = False
            else:
                # Fallback: manual barrier if render pass not yet ready
                image = _state._vk_swapchain_images[_state._vk_current_image_idx]
                subresource = vk.VkImageSubresourceRange(
                    aspectMask=vk.VK_IMAGE_ASPECT_COLOR_BIT,
                    baseMipLevel=0, levelCount=1,
                    baseArrayLayer=0, layerCount=1,
                )
                barrier = vk.VkImageMemoryBarrier(
                    sType=vk.VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
                    srcAccessMask=vk.VK_ACCESS_TRANSFER_WRITE_BIT,
                    dstAccessMask=0,
                    oldLayout=vk.VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                    newLayout=vk.VK_IMAGE_LAYOUT_PRESENT_SRC_KHR,
                    srcQueueFamilyIndex=vk.VK_QUEUE_FAMILY_IGNORED,
                    dstQueueFamilyIndex=vk.VK_QUEUE_FAMILY_IGNORED,
                    image=image,
                    subresourceRange=subresource,
                )
                vk.vkCmdPipelineBarrier(
                    _state._vk_cmd_buf,
                    vk.VK_PIPELINE_STAGE_TRANSFER_BIT,
                    vk.VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
                    0, 0, None, 0, None, 1, [barrier],
                )
            vk.vkEndCommandBuffer(_state._vk_cmd_buf)
        except Exception as exc:
            logger.error("d3d8", f"EndScene failed: {exc} — halting")
            cpu.halted = True
            return

        logger.info("d3d8", "EndScene: OK")
        cpu.regs[EAX] = S_OK

    # [36] Clear(Count, pRects, Flags, Color, Z, Stencil)
    # Stack (this at ESP+4):
    #   ESP+8:  Count
    #   ESP+12: pRects
    #   ESP+16: Flags
    #   ESP+20: Color (D3DCOLOR = 0xAARRGGBB)
    #   ESP+24: Z
    #   ESP+28: Stencil
    def _clear(cpu: "CPU", mem: "Memory") -> None:
        import vulkan as vk

        if _state._vk_device is None:
            logger.error("d3d8", "Clear: device not initialised — halting")
            cpu.halted = True
            return

        flags = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        D3DCLEAR_TARGET = 0x00000001
        if not (flags & D3DCLEAR_TARGET):
            cpu.regs[EAX] = S_OK
            return

        argb  = mem.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        a = ((argb >> 24) & 0xFF) / 255.0
        r = ((argb >> 16) & 0xFF) / 255.0
        g = ((argb >>  8) & 0xFF) / 255.0
        b = ( argb        & 0xFF) / 255.0

        try:
            clear_color = vk.VkClearColorValue(float32=[r, g, b, a])
            if _state._vk_in_render_pass:
                # Inside a render pass: use vkCmdClearAttachments
                clear_attach = vk.VkClearAttachment(
                    aspectMask=vk.VK_IMAGE_ASPECT_COLOR_BIT,
                    colorAttachment=0,
                    clearValue=vk.VkClearValue(color=clear_color),
                )
                clear_rect = vk.VkClearRect(
                    rect=vk.VkRect2D(
                        vk.VkOffset2D(0, 0),
                        vk.VkExtent2D(_state._vk_swapchain_width,
                                      _state._vk_swapchain_height)),
                    baseArrayLayer=0,
                    layerCount=1,
                )
                vk.vkCmdClearAttachments(_state._vk_cmd_buf,
                                         1, [clear_attach], 1, [clear_rect])
            else:
                image = _state._vk_swapchain_images[_state._vk_current_image_idx]
                subresource = vk.VkImageSubresourceRange(
                    aspectMask=vk.VK_IMAGE_ASPECT_COLOR_BIT,
                    baseMipLevel=0, levelCount=1,
                    baseArrayLayer=0, layerCount=1,
                )
                vk.vkCmdClearColorImage(
                    _state._vk_cmd_buf,
                    image,
                    vk.VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                    clear_color,
                    1, [subresource],
                )
        except Exception as exc:
            logger.error("d3d8", f"Clear failed: {exc} — halting")
            cpu.halted = True
            return

        logger.debug("d3d8",
            f"Clear: ARGB=0x{argb:08x} rgba=({r:.2f},{g:.2f},{b:.2f},{a:.2f})")
        cpu.regs[EAX] = S_OK

    # [15] Present(pSrc, pDest, hWnd, pRegion)
    # Submits the command buffer and presents the current swapchain image.
    def _present(cpu: "CPU", mem: "Memory") -> None:
        import vulkan as vk

        if _state._vk_device is None:
            logger.error("d3d8", "Present: device not initialised — halting")
            cpu.halted = True
            return

        logger.info("d3d8", "Present: ENTER")
        try:
            submit_info = vk.VkSubmitInfo(
                sType=vk.VK_STRUCTURE_TYPE_SUBMIT_INFO,
                waitSemaphoreCount=1,
                pWaitSemaphores=[_state._vk_image_available],
                pWaitDstStageMask=[
                    vk.VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT],
                commandBufferCount=1,
                pCommandBuffers=[_state._vk_cmd_buf],
                signalSemaphoreCount=1,
                pSignalSemaphores=[_state._vk_render_done],
            )
            vk.vkQueueSubmit(
                _state._vk_graphics_queue, 1, [submit_info],
                _state._vk_in_flight)
            _state._vk_frame_submitted = True

            present_info = vk.VkPresentInfoKHR(
                sType=vk.VK_STRUCTURE_TYPE_PRESENT_INFO_KHR,
                waitSemaphoreCount=1,
                pWaitSemaphores=[_state._vk_render_done],
                swapchainCount=1,
                pSwapchains=[_state._vk_swapchain],
                pImageIndices=[_state._vk_current_image_idx],
            )
            vk_pump(lambda: _state._vk_fn_queue_present(
                _state._vk_present_queue, present_info))
            _state._vk_image_acquired = False
        except Exception as exc:
            if "OutOfDate" in type(exc).__name__ or "Suboptimal" in type(exc).__name__:
                logger.info("d3d8", "Present: swapchain out-of-date, rebuilding")
                _rebuild_swapchain()
                _state._vk_image_acquired = False
                cpu.regs[EAX] = S_OK
                return
            logger.error("d3d8", f"Present failed: {type(exc).__name__} — halting")
            cpu.halted = True
            return

        logger.info("d3d8",
            f"Present: OK image_idx={_state._vk_current_image_idx}")
        cpu.regs[EAX] = S_OK


    # [51] GetRenderState(State, DWORD* pValue)
    def _get_render_state(cpu: "CPU", mem: "Memory") -> None:
        p_val = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if p_val:
            mem.write32(p_val, 0)
        cpu.regs[EAX] = S_OK

    # [53] EndStateBlock(DWORD* pToken)
    def _end_state_block(cpu: "CPU", mem: "Memory") -> None:
        p_token = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_token:
            mem.write32(p_token, 0xD3D50001)
        cpu.regs[EAX] = S_OK

    # [57] CreateStateBlock(Type, DWORD* pToken)
    def _create_state_block(cpu: "CPU", mem: "Memory") -> None:
        p_token = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if p_token:
            mem.write32(p_token, 0xD3D50002)
        cpu.regs[EAX] = S_OK

    # [60] GetTexture(Stage, IDirect3DBaseTexture8**)
    def _get_texture(cpu: "CPU", mem: "Memory") -> None:
        pp_tex = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if pp_tex:
            mem.write32(pp_tex, 0)
        cpu.regs[EAX] = S_OK

    # [62] GetTextureStageState(Stage, Type, DWORD* pValue)
    def _get_texture_stage_state(cpu: "CPU", mem: "Memory") -> None:
        p_val = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        if p_val:
            mem.write32(p_val, 0)
        cpu.regs[EAX] = S_OK

    # [64] ValidateDevice(DWORD* pNumPasses)
    def _validate_device(cpu: "CPU", mem: "Memory") -> None:
        p_passes = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_passes:
            mem.write32(p_passes, 1)
        cpu.regs[EAX] = S_OK


    # [75] CreateVertexShader(pDecl, pFunction, DWORD* pHandle, Usage)
    def _create_vertex_shader(cpu: "CPU", mem: "Memory") -> None:
        p_handle = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        if p_handle:
            mem.write32(p_handle, 0xD3D30001)
        cpu.regs[EAX] = S_OK

    # [77] GetVertexShader(DWORD* pHandle)
    def _get_vertex_shader(cpu: "CPU", mem: "Memory") -> None:
        p_handle = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_handle:
            mem.write32(p_handle, 0xD3D30001)
        cpu.regs[EAX] = S_OK

    # [84] GetStreamSource(StreamNum, IDirect3DVertexBuffer8**, UINT* pStride)
    def _get_stream_source(cpu: "CPU", mem: "Memory") -> None:
        pp_vb = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if pp_vb:
            mem.write32(pp_vb, 0)
        cpu.regs[EAX] = S_OK

    # [87] CreatePixelShader(pFunction, DWORD* pHandle)
    def _create_pixel_shader(cpu: "CPU", mem: "Memory") -> None:
        p_handle = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if p_handle:
            mem.write32(p_handle, 0xD3D40001)
        cpu.regs[EAX] = S_OK

    # [89] GetPixelShader(DWORD* pHandle)
    def _get_pixel_shader(cpu: "CPU", mem: "Memory") -> None:
        p_handle = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if p_handle:
            mem.write32(p_handle, 0xD3D40001)
        cpu.regs[EAX] = S_OK

    dev = [0] * 97

    dev[0]  = _com_stub(stubs, "d3d8dev", "Dev::QueryInterface",
                lambda cpu, mem: _set_eax(cpu, 0x80004002), 8, memory, D3DDEV_OBJ)
    dev[1]  = _uint("Dev::AddRef",  0, 1)
    dev[2]  = _uint("Dev::Release", 0, 0)
    dev[3]  = _ok  ("Dev::TestCooperativeLevel",        0)
    dev[4]  = _uint("Dev::GetAvailableTextureMem",      0, 128 * 1024 * 1024)
    dev[5]  = _ok  ("Dev::ResourceManagerDiscardBytes", 4)
    dev[6]  = _com_stub(stubs, "d3d8dev", "Dev::GetDirect3D",
                _get_direct3d, 4, memory, D3DDEV_OBJ)
    dev[7]  = _com_stub(stubs, "d3d8dev", "Dev::GetDeviceCaps",
                _get_device_caps, 4, memory, D3DDEV_OBJ)
    dev[8]  = _com_stub(stubs, "d3d8dev", "Dev::GetDisplayMode",
                _get_display_mode, 4, memory, D3DDEV_OBJ)
    dev[9]  = _com_stub(stubs, "d3d8dev", "Dev::GetCreationParameters",
                _get_creation_params, 4, memory, D3DDEV_OBJ)
    dev[10] = _ok  ("Dev::SetCursorProperties",       12)
    dev[11] = _void("Dev::SetCursorPosition",         12)
    dev[12] = _uint("Dev::ShowCursor",                 4, 0)
    dev[13] = _ok  ("Dev::CreateAdditionalSwapChain",  8)
    dev[14] = _com_stub(stubs, "d3d8dev", "Dev::Reset",   _reset,   4, memory, D3DDEV_OBJ)
    dev[15] = _com_stub(stubs, "d3d8dev", "Dev::Present",
                _present, 16, memory, D3DDEV_OBJ)
    dev[16] = _com_stub(stubs, "d3d8dev", "Dev::GetBackBuffer",
                _get_back_buffer, 12, memory, D3DDEV_OBJ)
    dev[17] = _ok  ("Dev::GetRasterStatus",   4)
    dev[18] = _void("Dev::SetGammaRamp",      8)
    dev[19] = _void("Dev::GetGammaRamp",      4)
    dev[20] = _com_stub(stubs, "d3d8dev", "Dev::CreateTexture",
                _create_texture, 28, memory, D3DDEV_OBJ)
    dev[21] = _com_stub(stubs, "d3d8dev", "Dev::CreateVolumeTexture",
                _create_volume_texture, 32, memory, D3DDEV_OBJ)
    dev[22] = _com_stub(stubs, "d3d8dev", "Dev::CreateCubeTexture",
                _create_cube_texture, 24, memory, D3DDEV_OBJ)
    dev[23] = _com_stub(stubs, "d3d8dev", "Dev::CreateVertexBuffer",
                _create_vertex_buffer, 20, memory, D3DDEV_OBJ)
    dev[24] = _com_stub(stubs, "d3d8dev", "Dev::CreateIndexBuffer",
                _create_index_buffer, 20, memory, D3DDEV_OBJ)
    dev[25] = _com_stub(stubs, "d3d8dev", "Dev::CreateRenderTarget",
                _create_render_target, 24, memory, D3DDEV_OBJ)
    dev[26] = _com_stub(stubs, "d3d8dev", "Dev::CreateDepthStencilSurface",
                _create_depth_stencil, 20, memory, D3DDEV_OBJ)
    dev[27] = _com_stub(stubs, "d3d8dev", "Dev::CreateImageSurface",
                _create_image_surface, 16, memory, D3DDEV_OBJ)
    dev[28] = _ok  ("Dev::CopyRects",     20)
    dev[29] = _ok  ("Dev::UpdateTexture",  8)
    dev[30] = _com_stub(stubs, "d3d8dev", "Dev::GetFrontBuffer",
                lambda cpu, mem: _set_eax(cpu, S_OK), 4, memory, D3DDEV_OBJ)
    dev[31] = _ok  ("Dev::SetRenderTarget", 8)
    dev[32] = _com_stub(stubs, "d3d8dev", "Dev::GetRenderTarget",
                _get_render_target, 4, memory, D3DDEV_OBJ)
    dev[33] = _com_stub(stubs, "d3d8dev", "Dev::GetDepthStencilSurface",
                _get_depth_stencil, 4, memory, D3DDEV_OBJ)
    dev[34] = _com_stub(stubs, "d3d8dev", "Dev::BeginScene",
                _begin_scene, 0, memory, D3DDEV_OBJ)
    dev[35] = _com_stub(stubs, "d3d8dev", "Dev::EndScene",
                _end_scene,   0, memory, D3DDEV_OBJ)
    dev[36] = _com_stub(stubs, "d3d8dev", "Dev::Clear",
                _clear,      24, memory, D3DDEV_OBJ)
    dev[37] = _ok  ("Dev::SetTransform",      8)
    dev[38] = _ok  ("Dev::GetTransform",      8)
    dev[39] = _ok  ("Dev::MultiplyTransform",  8)
    dev[40] = _ok  ("Dev::SetViewport",        4)
    dev[41] = _ok  ("Dev::GetViewport",        4)
    dev[42] = _ok  ("Dev::SetMaterial",        4)
    dev[43] = _ok  ("Dev::GetMaterial",        4)
    dev[44] = _ok  ("Dev::SetLight",           8)
    dev[45] = _ok  ("Dev::GetLight",           8)
    dev[46] = _ok  ("Dev::LightEnable",        8)
    dev[47] = _ok  ("Dev::GetLightEnable",     8)
    dev[48] = _ok  ("Dev::SetClipPlane",       8)
    dev[49] = _ok  ("Dev::GetClipPlane",       8)
    dev[50] = _ok  ("Dev::SetRenderState",     8)
    dev[51] = _com_stub(stubs, "d3d8dev", "Dev::GetRenderState",
                _get_render_state, 8, memory, D3DDEV_OBJ)
    dev[52] = _ok  ("Dev::BeginStateBlock", 0)
    dev[53] = _com_stub(stubs, "d3d8dev", "Dev::EndStateBlock",
                _end_state_block, 4, memory, D3DDEV_OBJ)
    dev[54] = _ok  ("Dev::ApplyStateBlock",   4)
    dev[55] = _ok  ("Dev::CaptureStateBlock", 4)
    dev[56] = _ok  ("Dev::DeleteStateBlock",  4)
    dev[57] = _com_stub(stubs, "d3d8dev", "Dev::CreateStateBlock",
                _create_state_block, 8, memory, D3DDEV_OBJ)
    dev[58] = _ok  ("Dev::SetClipStatus", 4)
    dev[59] = _ok  ("Dev::GetClipStatus", 4)
    dev[60] = _com_stub(stubs, "d3d8dev", "Dev::GetTexture",
                _get_texture, 8, memory, D3DDEV_OBJ)
    dev[61] = _ok  ("Dev::SetTexture", 8)
    dev[62] = _com_stub(stubs, "d3d8dev", "Dev::GetTextureStageState",
                _get_texture_stage_state, 12, memory, D3DDEV_OBJ)
    dev[63] = _ok  ("Dev::SetTextureStageState", 12)
    dev[64] = _com_stub(stubs, "d3d8dev", "Dev::ValidateDevice",
                _validate_device, 4, memory, D3DDEV_OBJ)
    dev[65] = _ok  ("Dev::GetInfo",                   12)
    dev[66] = _ok  ("Dev::SetPaletteEntries",          8)
    dev[67] = _ok  ("Dev::GetPaletteEntries",          8)
    dev[68] = _ok  ("Dev::SetCurrentTexturePalette",   4)
    dev[69] = _ok  ("Dev::GetCurrentTexturePalette",   4)
    # [70] DrawPrimitive(PrimitiveType, StartVertex, PrimitiveCount)
    def _draw_primitive(cpu: "CPU", mem: "Memory") -> None:
        prim_type  = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        start_vert = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        prim_count = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)

        if _state._vk_pipeline is None or _state._vk_vertex_mapped_ptr is None:
            logger.error("d3d8", "DrawPrimitive: pipeline not ready — halting")
            cpu.halted = True
            return

        D3DPT_TRIANGLELIST = 4
        if prim_type != D3DPT_TRIANGLELIST:
            logger.warn("d3d8",
                f"DrawPrimitive: unsupported PrimType={prim_type}, skipping")
            cpu.regs[EAX] = S_OK
            return

        if _state._draw_stream_stride == 0 or _state._draw_stream_ptr == 0:
            logger.warn("d3d8", "DrawPrimitive: no stream source set, skipping")
            cpu.regs[EAX] = S_OK
            return

        import vulkan as vk

        stride  = _state._draw_stream_stride
        n_verts = prim_count * 3
        src_off = _state._draw_stream_ptr + start_vert * stride
        vp_w    = float(_state._vk_swapchain_width  or 1)
        vp_h    = float(_state._vk_swapchain_height or 1)

        out_verts = bytearray(n_verts * 32)
        flat = mem._buffer   # raw bytearray for fast access
        for i in range(n_verts):
            base = src_off + i * stride
            x,   = _struct.unpack_from('<f', flat, base)
            y,   = _struct.unpack_from('<f', flat, base + 4)
            z,   = _struct.unpack_from('<f', flat, base + 8)
            dif, = _struct.unpack_from('<I', flat, base + 16)
            xn = (x / vp_w) * 2.0 - 1.0
            yn = 1.0 - (y / vp_h) * 2.0
            b = ((dif >>  0) & 0xFF) / 255.0
            g = ((dif >>  8) & 0xFF) / 255.0
            r = ((dif >> 16) & 0xFF) / 255.0
            a = ((dif >> 24) & 0xFF) / 255.0
            _struct.pack_into('<ffffffff', out_verts, i * 32,
                              xn, yn, z, 1.0, b, g, r, a)

        size = len(out_verts)
        import cffi as _cffi
        _cffi.FFI().memmove(_state._vk_vertex_mapped_ptr, bytes(out_verts), size)

        cmd = _state._vk_cmd_buf
        vk.vkCmdBindPipeline(cmd, vk.VK_PIPELINE_BIND_POINT_GRAPHICS,
                             _state._vk_pipeline)
        vp = vk.VkViewport(x=0, y=0,
                           width=_state._vk_swapchain_width,
                           height=_state._vk_swapchain_height,
                           minDepth=0.0, maxDepth=1.0)
        sc = vk.VkRect2D(vk.VkOffset2D(0, 0),
                         vk.VkExtent2D(_state._vk_swapchain_width,
                                       _state._vk_swapchain_height))
        vk.vkCmdSetViewport(cmd, 0, 1, [vp])
        vk.vkCmdSetScissor(cmd, 0, 1, [sc])
        vk.vkCmdBindVertexBuffers(cmd, 0, 1, [_state._vk_vertex_buffer], [0])
        vk.vkCmdDraw(cmd, n_verts, 1, 0, 0)
        logger.debug("d3d8",
            f"DrawPrimitive: TRIANGLELIST prim_count={prim_count}")
        cpu.regs[EAX] = S_OK

    dev[70] = _com_stub(stubs, "d3d8dev", "Dev::DrawPrimitive",
                _draw_primitive, 12, memory, D3DDEV_OBJ)
    dev[71] = _halt("Dev::DrawIndexedPrimitive",   20)
    dev[72] = _halt("Dev::DrawPrimitiveUP",        16)
    dev[73] = _halt("Dev::DrawIndexedPrimitiveUP", 32)
    dev[74] = _ok  ("Dev::ProcessVertices", 20)
    dev[75] = _com_stub(stubs, "d3d8dev", "Dev::CreateVertexShader",
                _create_vertex_shader, 16, memory, D3DDEV_OBJ)
    def _set_vertex_shader(cpu: "CPU", mem: "Memory") -> None:
        handle = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        _state._draw_vertex_fvf = handle
        cpu.regs[EAX] = S_OK
    dev[76] = _com_stub(stubs, "d3d8dev", "Dev::SetVertexShader",
                _set_vertex_shader, 4, memory, D3DDEV_OBJ)
    dev[77] = _com_stub(stubs, "d3d8dev", "Dev::GetVertexShader",
                _get_vertex_shader, 4, memory, D3DDEV_OBJ)
    dev[78] = _ok  ("Dev::DeleteVertexShader",         4)
    dev[79] = _ok  ("Dev::SetVertexShaderConstant",   12)
    dev[80] = _ok  ("Dev::GetVertexShaderConstant",   12)
    dev[81] = _ok  ("Dev::GetVertexShaderDeclaration", 12)
    dev[82] = _ok  ("Dev::GetVertexShaderFunction",    12)
    def _set_stream_source(cpu: "CPU", mem: "Memory") -> None:
        # StreamNumber=ESP+8, pStreamData=ESP+12, Stride=ESP+16
        p_buf  = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        stride = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        if p_buf:
            data_ptr = mem.read32((p_buf + 4) & 0xFFFFFFFF)  # [vtable][data_ptr][size]
            _state._draw_stream_ptr    = data_ptr
            _state._draw_stream_stride = stride
        cpu.regs[EAX] = S_OK
    dev[83] = _com_stub(stubs, "d3d8dev", "Dev::SetStreamSource",
                _set_stream_source, 12, memory, D3DDEV_OBJ)
    dev[84] = _com_stub(stubs, "d3d8dev", "Dev::GetStreamSource",
                _get_stream_source, 12, memory, D3DDEV_OBJ)
    dev[85] = _ok  ("Dev::SetIndices", 8)
    dev[86] = _ok  ("Dev::GetIndices", 8)
    dev[87] = _com_stub(stubs, "d3d8dev", "Dev::CreatePixelShader",
                _create_pixel_shader, 8, memory, D3DDEV_OBJ)
    dev[88] = _ok  ("Dev::SetPixelShader", 4)
    dev[89] = _com_stub(stubs, "d3d8dev", "Dev::GetPixelShader",
                _get_pixel_shader, 4, memory, D3DDEV_OBJ)
    dev[90] = _ok  ("Dev::DeletePixelShader",          4)
    dev[91] = _ok  ("Dev::SetPixelShaderConstant",    12)
    dev[92] = _ok  ("Dev::GetPixelShaderConstant",    12)
    dev[93] = _ok  ("Dev::GetPixelShaderFunction",    12)
    dev[94] = _ok  ("Dev::DrawRectPatch",             12)
    dev[95] = _ok  ("Dev::DrawTriPatch",              12)
    dev[96] = _ok  ("Dev::DeletePatch",                4)

    return dev
