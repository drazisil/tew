"""D3D8 memory layout constants and error codes.

IMPORTANT: All COM object addresses MUST be above the stub trampoline region.
MAX_HANDLERS=4096 × HANDLER_SIZE=32 = 128 KB → stubs end at 0x00220000.
"""

# ── COM object addresses ──────────────────────────────────────────────────────
D3D8_VTABLE   = 0x00220000  # IDirect3D8 vtable     (16 × 4 =  64 bytes → 0x00220040)
D3D8_OBJ      = 0x00220040  # IDirect3D8 object     (vtable ptr, 4 bytes)
D3DDEV_VTABLE = 0x00220050  # IDirect3DDevice8 vtable (97 × 4 = 388 bytes → 0x002201D8)
D3DDEV_OBJ    = 0x00220200  # IDirect3DDevice8 object (vtable ptr, 4 bytes)
D3DRES_VTABLE  = 0x00220210  # IDirect3DResource8/Buffer vtable (14 × 4 = 56 bytes → 0x00220248)
D3DSURF_VTABLE = 0x00220260  # IDirect3DSurface8 vtable        (11 × 4 = 44 bytes → 0x0022028C)
D3DTEX_VTABLE  = 0x00220290  # IDirect3DTexture8 vtable        (18 × 4 = 72 bytes → 0x002202D8)
# Resource/surface/texture objects allocated from the bump heap at 0x09000000-0x10000000
# (D3D8_HEAP_BASE/D3D8_HEAP_LIMIT in tew/api/d3d8/_helpers.py).

# ── D3D / COM error codes ─────────────────────────────────────────────────────
S_OK             = 0x00000000
D3DERR_NOTAVAIL  = 0x8876086A
