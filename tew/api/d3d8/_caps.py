"""D3DCAPS8 and D3DADAPTER_IDENTIFIER8 struct population.

D3DCAPS8 field offsets (DirectX 8 SDK, 53 DWORDs = 212 bytes):
   0  DeviceType         4  AdapterOrdinal      8  Caps
  12  Caps2             16  Caps3              20  PresentationIntervals
  24  CursorCaps        28  DevCaps            32  PrimitiveMiscCaps
  36  RasterCaps        40  ZCmpCaps           44  SrcBlendCaps
  48  DestBlendCaps     52  AlphaCmpCaps       56  ShadeCaps
  60  TextureCaps       64  TextureFilterCaps  68  CubeTextureFilterCaps
  72  VolumeTextureFilterCaps  76 TextureAddressCaps  80 VolumeTextureAddressCaps
  84  LineCaps          88  MaxTextureWidth    92  MaxTextureHeight
  96  MaxVolumeExtent  100  MaxTextureRepeat  104  MaxTextureAspectRatio
 108  MaxAnisotropy    112  MaxVertexW (f32)  116  GuardBandLeft (f32)
 120  GuardBandTop     124  GuardBandRight    128  GuardBandBottom
 132  ExtentsAdjust    136  StencilCaps       140  FVFCaps
 144  TextureOpCaps    148  MaxTextureBlendStages  152  MaxSimultaneousTextures
 156  VertexProcessingCaps  160  MaxActiveLights  164  MaxUserClipPlanes
 168  MaxVertexBlendMatrices  172  MaxVertexBlendMatrixIndex  176  MaxPointSize (f32)
 180  MaxPrimitiveCount  184  MaxVertexIndex    188  MaxStreams
 192  MaxStreamStride   196  VertexShaderVersion  200  MaxVertexShaderConst
 204  PixelShaderVersion  208  MaxPixelShaderValue (f32)   [sizeof=212]
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.memory import Memory


def _fill_d3d_caps8(p_caps: int, memory: "Memory") -> None:
    """Fill D3DCAPS8 for a virtual HAL device (GeForce4-class, Vulkan-backed).

    All values are per the D3D8 SDK spec for a D3DDEVTYPE_HAL device.
    Fields that depend on Vulkan implementation details use conservative
    but spec-valid values for a discrete GPU of this era.
    """
    if not p_caps:
        return
    # Zero the whole struct first (212 bytes = 53 DWORDs, offsets 0..208)
    for i in range(0, 212, 4):
        memory.write32(p_caps + i, 0)

    # ── Identity ──────────────────────────────────────────────────────────────
    memory.write32(p_caps + 0,  1)  # DeviceType = D3DDEVTYPE_HAL
    memory.write32(p_caps + 4,  0)  # AdapterOrdinal = 0

    # ── Caps / Caps2 / Caps3 ─────────────────────────────────────────────────
    # D3DCAPS_READ_SCANLINE
    memory.write32(p_caps + 8,  0x00000040)
    # D3DCAPS2_FULLSCREENGAMMA | D3DCAPS2_DYNAMICTEXTURES | D3DCAPS2_CANAUTOGENMIPMAP
    memory.write32(p_caps + 12, 0x00020000 | 0x20000000 | 0x40000000)
    # D3DCAPS3_ALPHA_FULLSCREEN_FLIP_OR_DISCARD | D3DCAPS3_LINEAR_TO_SRGB_PRESENTATION
    memory.write32(p_caps + 16, 0x00000020 | 0x00000080)
    # D3DPRESENT_INTERVAL_ONE | D3DPRESENT_INTERVAL_IMMEDIATE
    memory.write32(p_caps + 20, 0x00000002 | 0x80000000)
    # CursorCaps: D3DCURSORCAPS_COLOR | D3DCURSORCAPS_LOWRES
    memory.write32(p_caps + 24, 0x00000001 | 0x00000002)

    # ── DevCaps — defines this as a real HAL device ───────────────────────────
    # D3DDEVCAPS_EXECUTESYSTEMMEMORY | D3DDEVCAPS_EXECUTEVIDEOMEMORY
    # | D3DDEVCAPS_TLVERTEXSYSTEMMEMORY | D3DDEVCAPS_TLVERTEXVIDEOMEMORY
    # | D3DDEVCAPS_TEXTURESYSTEMMEMORY | D3DDEVCAPS_TEXTUREVIDEOMEMORY
    # | D3DDEVCAPS_DRAWPRIMITIVES2 | D3DDEVCAPS_DRAWPRIMITIVES2EX
    # | D3DDEVCAPS_HWTRANSFORMANDLIGHT | D3DDEVCAPS_HWRASTERIZATION
    # | D3DDEVCAPS_PUREDEVICE
    memory.write32(p_caps + 28,
        0x00000010 | 0x00000020 | 0x00000040 | 0x00000080 |
        0x00000100 | 0x00000200 | 0x00002000 | 0x00010000 |
        0x00040000 | 0x00080000 | 0x00100000)  # = 0x001D23F0

    # ── Primitive / Raster caps ───────────────────────────────────────────────
    # PrimitiveMiscCaps: MASKZ | CULLNONE | CULLCW | CULLCCW | COLORWRITEENABLE
    #   | CLIPPLANESCALEDPOINTS | CLIPTLVERTS | TRIFAN | SEPARATEALPHABLEND
    memory.write32(p_caps + 32,
        0x00000002 | 0x00000010 | 0x00000020 | 0x00000040 |
        0x00000080 | 0x00000100 | 0x00000200 | 0x00000400 | 0x00010000)
    # RasterCaps: DITHER | ZTEST | FOGVERTEX | FOGTABLE | MIPMAPLODBIAS
    #   | ZBIAS | FOGRANGE | ANISOTROPY | WFOG | ZFOG | COLORPERSPECTIVE
    memory.write32(p_caps + 36,
        0x00000001 | 0x00000010 | 0x00000080 | 0x00000200 | 0x00002000 |
        0x00010000 | 0x00020000 | 0x00040000 | 0x00100000 | 0x00400000 | 0x00800000)

    # ── Compare / Blend caps ─────────────────────────────────────────────────
    # ZCmpCaps: all eight compare functions supported
    memory.write32(p_caps + 40, 0x000000FF)
    # SrcBlendCaps / DestBlendCaps: all blend modes supported
    memory.write32(p_caps + 44, 0x00001FFF)
    memory.write32(p_caps + 48, 0x00001FFF)
    # AlphaCmpCaps: all eight compare functions
    memory.write32(p_caps + 52, 0x000000FF)

    # ── Shade / Texture caps ─────────────────────────────────────────────────
    # ShadeCaps: COLORGOURAUDRGB | COLORGOURAUDALPHA | SPECULARGOURAUDRGB | FOGGOURAUD
    memory.write32(p_caps + 56,
        0x00000002 | 0x00000040 | 0x00000200 | 0x00004000)
    # TextureCaps: PERSPECTIVE | ALPHA | MIPMAP | MIPVOLUMEMAP | MIPCUBEMAP
    #   | CUBEMAP | VOLUMEMAP
    memory.write32(p_caps + 60,
        0x00000001 | 0x00000004 | 0x00000040 | 0x00000400 |
        0x00002000 | 0x00004000 | 0x00008000 | 0x00010000)
    # TextureFilterCaps: MIN/MAG/MIP POINT + LINEAR + ANISOTROPIC
    memory.write32(p_caps + 64,
        0x00000100 | 0x00000200 | 0x00000400 |   # MIN point/linear/aniso
        0x00010000 | 0x00020000 | 0x00040000 |   # MAG point/linear/aniso
        0x01000000 | 0x02000000 | 0x04000000)    # MIP point/linear
    # CubeTextureFilterCaps: same set minus aniso MIP
    memory.write32(p_caps + 68,
        0x00000100 | 0x00000200 | 0x00010000 | 0x00020000 |
        0x01000000 | 0x02000000)
    # VolumeTextureFilterCaps: same as cube
    memory.write32(p_caps + 72,
        0x00000100 | 0x00000200 | 0x00010000 | 0x00020000 |
        0x01000000 | 0x02000000)
    # TextureAddressCaps: WRAP | MIRROR | CLAMP | BORDER | INDEPENDENTUV | MIRRORONCE
    memory.write32(p_caps + 76,
        0x00000001 | 0x00000002 | 0x00000004 | 0x00000008 |
        0x00000010 | 0x00000080)
    # VolumeTextureAddressCaps: same
    memory.write32(p_caps + 80,
        0x00000001 | 0x00000002 | 0x00000004 | 0x00000008 |
        0x00000010 | 0x00000080)

    # ── Line caps ─────────────────────────────────────────────────────────────
    # D3DLINECAPS_ANTIALIAS | D3DLINECAPS_TEXTURE | D3DLINECAPS_ZTEST
    # | D3DLINECAPS_BLEND | D3DLINECAPS_ALPHACMP | D3DLINECAPS_FOG
    memory.write32(p_caps + 84,
        0x00000001 | 0x00000002 | 0x00000004 |
        0x00000008 | 0x00000010 | 0x00000020)

    # ── Texture size limits ───────────────────────────────────────────────────
    memory.write32(p_caps + 88,  2048)   # MaxTextureWidth
    memory.write32(p_caps + 92,  2048)   # MaxTextureHeight
    memory.write32(p_caps + 96,  256)    # MaxVolumeExtent
    memory.write32(p_caps + 100, 32768)  # MaxTextureRepeat
    memory.write32(p_caps + 104, 2048)   # MaxTextureAspectRatio (0 = any)
    memory.write32(p_caps + 108, 16)     # MaxAnisotropy

    # ── Guard band / stencil ─────────────────────────────────────────────────
    # MaxVertexW, GuardBand{Left,Top,Right,Bottom}, ExtentsAdjust: leave 0.0
    # StencilCaps: full set (all 8 stencil ops)
    memory.write32(p_caps + 136,
        0x00000001 | 0x00000002 | 0x00000004 | 0x00000008 |
        0x00000010 | 0x00000020 | 0x00000040 | 0x00000080)

    # ── FVF / texture stage ops ───────────────────────────────────────────────
    # FVFCaps: 8 tex coord sets | D3DFVFCAPS_DONOTSTRIPELEMENTS | D3DFVFCAPS_PSIZE
    memory.write32(p_caps + 140, 0x00080008)
    # TextureOpCaps: all ops
    memory.write32(p_caps + 144, 0x03FFFFFF)
    memory.write32(p_caps + 148, 8)   # MaxTextureBlendStages
    memory.write32(p_caps + 152, 8)   # MaxSimultaneousTextures

    # ── Vertex processing ─────────────────────────────────────────────────────
    # VertexProcessingCaps: TEXGEN | MATERIALSOURCE7 | DIRECTIONALLIGHTS
    #   | POSITIONALLIGHTS | LOCALVIEWER | TWEENING
    memory.write32(p_caps + 156,
        0x00000001 | 0x00000002 | 0x00000004 |
        0x00000008 | 0x00000010 | 0x00000040)
    memory.write32(p_caps + 160, 8)    # MaxActiveLights
    memory.write32(p_caps + 164, 6)    # MaxUserClipPlanes
    memory.write32(p_caps + 168, 4)    # MaxVertexBlendMatrices
    memory.write32(p_caps + 172, 255)  # MaxVertexBlendMatrixIndex

    # ── Primitive / stream limits ─────────────────────────────────────────────
    memory.write32(p_caps + 180, 0x1FFFFF)  # MaxPrimitiveCount
    memory.write32(p_caps + 184, 0xFFFF)    # MaxVertexIndex
    memory.write32(p_caps + 188, 8)          # MaxStreams
    memory.write32(p_caps + 192, 256)        # MaxStreamStride

    # ── Shader versions ───────────────────────────────────────────────────────
    memory.write32(p_caps + 196, 0xFFFE0101)  # VertexShaderVersion = VS 1.1
    memory.write32(p_caps + 200, 256)          # MaxVertexShaderConst
    memory.write32(p_caps + 204, 0xFFFF0104)  # PixelShaderVersion = PS 1.4
    # MaxPixelShaderValue (float 1.0 at offset 208)
    memory.write32(p_caps + 208, 0x3F800000)


def _fill_adapter_identifier(p_ident: int, memory: "Memory") -> None:
    """Write D3DADAPTER_IDENTIFIER8 struct (1068 bytes) into emulator memory.

    Layout (D3D8 SDK):
      [   0] Driver[512]          — display driver filename
      [ 512] Description[512]     — human-readable adapter name
      [1024] DriverVersion        — LARGE_INTEGER (8 bytes), leave 0
      [1032] VendorId             — PCI vendor ID
      [1036] DeviceId             — PCI device ID
      [1040] SubSysId             — PCI subsystem ID
      [1044] Revision             — PCI revision
      [1048] DeviceIdentifier     — GUID (16 bytes), leave 0
      [1064] WHQLLevel            — WHQL certification level
    """
    if not p_ident:
        return

    def _write_str(offset: int, s: str) -> None:
        for i, ch in enumerate(s[:511]):
            memory.write8(p_ident + offset + i, ord(ch))
        memory.write8(p_ident + offset + min(len(s), 511), 0)

    _write_str(0,   "nv4_disp.dll")            # Driver  — XP-era NVIDIA driver filename
    _write_str(512, "NVIDIA GeForce4 Ti 4200")  # Description
    memory.write32(p_ident + 1032, 0x10DE)      # VendorId  — NVIDIA
    memory.write32(p_ident + 1036, 0x0253)      # DeviceId  — GeForce4 Ti 4200 (NV28)
    memory.write32(p_ident + 1040, 0)           # SubSysId
    memory.write32(p_ident + 1044, 0)           # Revision
    # DeviceIdentifier GUID at offset 1048: leave zero
    memory.write32(p_ident + 1064, 1)           # WHQLLevel — WHQL certified
