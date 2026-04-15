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

from tew.hardware.cpu import EAX, ECX, ESP
from tew.logger import logger
from tew.api.d3d8._layout import D3D8_OBJ, S_OK
from tew.api.d3d8._helpers import _alloc_resource_obj, _com_stub, _set_eax
from tew.api.d3d8._caps import _fill_d3d_caps8


def make_vtable(stubs: "Win32Handlers", memory: "Memory") -> list[int]:
    """Return the 97 trampoline addresses for the IDirect3DDevice8 vtable."""

    def _ok(name: str, arg_bytes: int) -> int:
        return _com_stub(stubs, "d3d8dev", name,
            lambda cpu, mem: _set_eax(cpu, S_OK), arg_bytes, memory)

    def _void(name: str, arg_bytes: int) -> int:
        return _com_stub(stubs, "d3d8dev", name,
            lambda cpu, mem: None, arg_bytes, memory)

    def _uint(name: str, arg_bytes: int, val: int) -> int:
        return _com_stub(stubs, "d3d8dev", name,
            lambda cpu, mem: _set_eax(cpu, val), arg_bytes, memory)

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
        logger.debug("d3d8", "IDirect3DDevice8::Reset")
        cpu.regs[EAX] = S_OK

    # [15] Present(pSrc, pDest, hWnd, pRegion)
    def _present(cpu: "CPU", mem: "Memory") -> None:
        logger.debug("d3d8", "IDirect3DDevice8::Present")
        cpu.regs[EAX] = S_OK

    # [16] GetBackBuffer(UINT, D3DBACKBUFFER_TYPE, IDirect3DSurface8**)
    def _get_back_buffer(cpu: "CPU", mem: "Memory") -> None:
        pp_surface = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        surf = _alloc_resource_obj(800 * 600 * 4, mem)
        if pp_surface:
            mem.write32(pp_surface, surf)
        cpu.regs[EAX] = S_OK

    # [20] CreateTexture(W, H, Levels, Usage, Fmt, Pool, IDirect3DTexture8**)
    def _create_texture(cpu: "CPU", mem: "Memory") -> None:
        w          = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        h          = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        pp_texture = mem.read32((cpu.regs[ESP] + 32) & 0xFFFFFFFF)
        tex = _alloc_resource_obj(w * h * 4 or 4, mem)
        if pp_texture:
            mem.write32(pp_texture, tex)
        cpu.regs[EAX] = S_OK

    # [21] CreateVolumeTexture(W, H, D, Levels, Usage, Fmt, Pool, IDirect3DVolumeTexture8**)
    def _create_volume_texture(cpu: "CPU", mem: "Memory") -> None:
        pp_texture = mem.read32((cpu.regs[ESP] + 36) & 0xFFFFFFFF)
        if pp_texture:
            mem.write32(pp_texture, _alloc_resource_obj(4096, mem))
        cpu.regs[EAX] = S_OK

    # [22] CreateCubeTexture(EdgeLength, Levels, Usage, Fmt, Pool, IDirect3DCubeTexture8**)
    def _create_cube_texture(cpu: "CPU", mem: "Memory") -> None:
        pp_texture = mem.read32((cpu.regs[ESP] + 28) & 0xFFFFFFFF)
        if pp_texture:
            mem.write32(pp_texture, _alloc_resource_obj(4096, mem))
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
        pp_surf = mem.read32((cpu.regs[ESP] + 28) & 0xFFFFFFFF)
        if pp_surf:
            mem.write32(pp_surf, _alloc_resource_obj(w * h * 4 or 4, mem))
        cpu.regs[EAX] = S_OK

    # [26] CreateDepthStencilSurface(W, H, Fmt, MultiSample, IDirect3DSurface8**)
    def _create_depth_stencil(cpu: "CPU", mem: "Memory") -> None:
        w       = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        h       = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        pp_surf = mem.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        if pp_surf:
            mem.write32(pp_surf, _alloc_resource_obj(w * h * 4 or 4, mem))
        cpu.regs[EAX] = S_OK

    # [27] CreateImageSurface(W, H, Fmt, IDirect3DSurface8**)
    def _create_image_surface(cpu: "CPU", mem: "Memory") -> None:
        w       = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        h       = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        pp_surf = mem.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        if pp_surf:
            mem.write32(pp_surf, _alloc_resource_obj(w * h * 4 or 4, mem))
        cpu.regs[EAX] = S_OK

    # [32] GetRenderTarget(IDirect3DSurface8**)
    def _get_render_target(cpu: "CPU", mem: "Memory") -> None:
        pp_surf = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if pp_surf:
            mem.write32(pp_surf, _alloc_resource_obj(800 * 600 * 4, mem))
        cpu.regs[EAX] = S_OK

    # [33] GetDepthStencilSurface(IDirect3DSurface8**)
    def _get_depth_stencil(cpu: "CPU", mem: "Memory") -> None:
        pp_surf = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if pp_surf:
            mem.write32(pp_surf, _alloc_resource_obj(800 * 600 * 4, mem))
        cpu.regs[EAX] = S_OK

    # [36] Clear(Count, pRects, Flags, Color, Z, Stencil)
    def _clear(cpu: "CPU", mem: "Memory") -> None:
        color = mem.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        logger.debug("d3d8", f"Clear color=0x{color & 0xFFFFFFFF:08x}")
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

    # [70] DrawPrimitive(PrimType, StartVertex, PrimCount)
    def _draw_primitive(cpu: "CPU", mem: "Memory") -> None:
        prim_type  = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        prim_count = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        logger.debug("d3d8", f"DrawPrimitive type={prim_type} count={prim_count}")
        cpu.regs[EAX] = S_OK

    # [71] DrawIndexedPrimitive(PrimType, minIndex, NumVerts, startIndex, primCount)
    def _draw_indexed_primitive(cpu: "CPU", mem: "Memory") -> None:
        prim_type  = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        prim_count = mem.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        logger.debug("d3d8", f"DrawIndexedPrimitive type={prim_type} count={prim_count}")
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
                lambda cpu, mem: _set_eax(cpu, 0x80004002), 8, memory)
    dev[1]  = _uint("Dev::AddRef",  0, 1)
    dev[2]  = _uint("Dev::Release", 0, 0)
    dev[3]  = _ok  ("Dev::TestCooperativeLevel",        0)
    dev[4]  = _uint("Dev::GetAvailableTextureMem",      0, 128 * 1024 * 1024)
    dev[5]  = _ok  ("Dev::ResourceManagerDiscardBytes", 4)
    dev[6]  = _com_stub(stubs, "d3d8dev", "Dev::GetDirect3D",
                _get_direct3d, 4, memory)
    dev[7]  = _com_stub(stubs, "d3d8dev", "Dev::GetDeviceCaps",
                _get_device_caps, 4, memory)
    dev[8]  = _com_stub(stubs, "d3d8dev", "Dev::GetDisplayMode",
                _get_display_mode, 4, memory)
    dev[9]  = _com_stub(stubs, "d3d8dev", "Dev::GetCreationParameters",
                _get_creation_params, 4, memory)
    dev[10] = _ok  ("Dev::SetCursorProperties",       12)
    dev[11] = _void("Dev::SetCursorPosition",         12)
    dev[12] = _uint("Dev::ShowCursor",                 4, 0)
    dev[13] = _ok  ("Dev::CreateAdditionalSwapChain",  8)
    dev[14] = _com_stub(stubs, "d3d8dev", "Dev::Reset",   _reset,   4, memory)
    dev[15] = _com_stub(stubs, "d3d8dev", "Dev::Present", _present, 16, memory)
    dev[16] = _com_stub(stubs, "d3d8dev", "Dev::GetBackBuffer",
                _get_back_buffer, 12, memory)
    dev[17] = _ok  ("Dev::GetRasterStatus",   4)
    dev[18] = _void("Dev::SetGammaRamp",      8)
    dev[19] = _void("Dev::GetGammaRamp",      4)
    dev[20] = _com_stub(stubs, "d3d8dev", "Dev::CreateTexture",
                _create_texture, 28, memory)
    dev[21] = _com_stub(stubs, "d3d8dev", "Dev::CreateVolumeTexture",
                _create_volume_texture, 32, memory)
    dev[22] = _com_stub(stubs, "d3d8dev", "Dev::CreateCubeTexture",
                _create_cube_texture, 24, memory)
    dev[23] = _com_stub(stubs, "d3d8dev", "Dev::CreateVertexBuffer",
                _create_vertex_buffer, 20, memory)
    dev[24] = _com_stub(stubs, "d3d8dev", "Dev::CreateIndexBuffer",
                _create_index_buffer, 20, memory)
    dev[25] = _com_stub(stubs, "d3d8dev", "Dev::CreateRenderTarget",
                _create_render_target, 24, memory)
    dev[26] = _com_stub(stubs, "d3d8dev", "Dev::CreateDepthStencilSurface",
                _create_depth_stencil, 20, memory)
    dev[27] = _com_stub(stubs, "d3d8dev", "Dev::CreateImageSurface",
                _create_image_surface, 16, memory)
    dev[28] = _ok  ("Dev::CopyRects",     20)
    dev[29] = _ok  ("Dev::UpdateTexture",  8)
    dev[30] = _com_stub(stubs, "d3d8dev", "Dev::GetFrontBuffer",
                lambda cpu, mem: _set_eax(cpu, S_OK), 4, memory)
    dev[31] = _ok  ("Dev::SetRenderTarget", 8)
    dev[32] = _com_stub(stubs, "d3d8dev", "Dev::GetRenderTarget",
                _get_render_target, 4, memory)
    dev[33] = _com_stub(stubs, "d3d8dev", "Dev::GetDepthStencilSurface",
                _get_depth_stencil, 4, memory)
    dev[34] = _ok  ("Dev::BeginScene", 0)
    dev[35] = _ok  ("Dev::EndScene",   0)
    dev[36] = _com_stub(stubs, "d3d8dev", "Dev::Clear",  _clear,  24, memory)
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
                _get_render_state, 8, memory)
    dev[52] = _ok  ("Dev::BeginStateBlock", 0)
    dev[53] = _com_stub(stubs, "d3d8dev", "Dev::EndStateBlock",
                _end_state_block, 4, memory)
    dev[54] = _ok  ("Dev::ApplyStateBlock",   4)
    dev[55] = _ok  ("Dev::CaptureStateBlock", 4)
    dev[56] = _ok  ("Dev::DeleteStateBlock",  4)
    dev[57] = _com_stub(stubs, "d3d8dev", "Dev::CreateStateBlock",
                _create_state_block, 8, memory)
    dev[58] = _ok  ("Dev::SetClipStatus", 4)
    dev[59] = _ok  ("Dev::GetClipStatus", 4)
    dev[60] = _com_stub(stubs, "d3d8dev", "Dev::GetTexture",
                _get_texture, 8, memory)
    dev[61] = _ok  ("Dev::SetTexture", 8)
    dev[62] = _com_stub(stubs, "d3d8dev", "Dev::GetTextureStageState",
                _get_texture_stage_state, 12, memory)
    dev[63] = _ok  ("Dev::SetTextureStageState", 12)
    dev[64] = _com_stub(stubs, "d3d8dev", "Dev::ValidateDevice",
                _validate_device, 4, memory)
    dev[65] = _ok  ("Dev::GetInfo",                   12)
    dev[66] = _ok  ("Dev::SetPaletteEntries",          8)
    dev[67] = _ok  ("Dev::GetPaletteEntries",          8)
    dev[68] = _ok  ("Dev::SetCurrentTexturePalette",   4)
    dev[69] = _ok  ("Dev::GetCurrentTexturePalette",   4)
    dev[70] = _com_stub(stubs, "d3d8dev", "Dev::DrawPrimitive",
                _draw_primitive, 12, memory)
    dev[71] = _com_stub(stubs, "d3d8dev", "Dev::DrawIndexedPrimitive",
                _draw_indexed_primitive, 20, memory)
    dev[72] = _com_stub(stubs, "d3d8dev", "Dev::DrawPrimitiveUP",
                lambda cpu, mem: _set_eax(cpu, S_OK), 16, memory)
    dev[73] = _com_stub(stubs, "d3d8dev", "Dev::DrawIndexedPrimitiveUP",
                lambda cpu, mem: _set_eax(cpu, S_OK), 32, memory)
    dev[74] = _ok  ("Dev::ProcessVertices", 20)
    dev[75] = _com_stub(stubs, "d3d8dev", "Dev::CreateVertexShader",
                _create_vertex_shader, 16, memory)
    dev[76] = _ok  ("Dev::SetVertexShader", 4)
    dev[77] = _com_stub(stubs, "d3d8dev", "Dev::GetVertexShader",
                _get_vertex_shader, 4, memory)
    dev[78] = _ok  ("Dev::DeleteVertexShader",         4)
    dev[79] = _ok  ("Dev::SetVertexShaderConstant",   12)
    dev[80] = _ok  ("Dev::GetVertexShaderConstant",   12)
    dev[81] = _ok  ("Dev::GetVertexShaderDeclaration", 12)
    dev[82] = _ok  ("Dev::GetVertexShaderFunction",    12)
    dev[83] = _ok  ("Dev::SetStreamSource",            12)
    dev[84] = _com_stub(stubs, "d3d8dev", "Dev::GetStreamSource",
                _get_stream_source, 12, memory)
    dev[85] = _ok  ("Dev::SetIndices", 8)
    dev[86] = _ok  ("Dev::GetIndices", 8)
    dev[87] = _com_stub(stubs, "d3d8dev", "Dev::CreatePixelShader",
                _create_pixel_shader, 8, memory)
    dev[88] = _ok  ("Dev::SetPixelShader", 4)
    dev[89] = _com_stub(stubs, "d3d8dev", "Dev::GetPixelShader",
                _get_pixel_shader, 4, memory)
    dev[90] = _ok  ("Dev::DeletePixelShader",          4)
    dev[91] = _ok  ("Dev::SetPixelShaderConstant",    12)
    dev[92] = _ok  ("Dev::GetPixelShaderConstant",    12)
    dev[93] = _ok  ("Dev::GetPixelShaderFunction",    12)
    dev[94] = _ok  ("Dev::DrawRectPatch",             12)
    dev[95] = _ok  ("Dev::DrawTriPatch",              12)
    dev[96] = _ok  ("Dev::DeletePatch",                4)

    return dev
