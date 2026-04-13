"""Win32 DIB parsing and SDL2 texture creation.

Win32 bitmap resources are stored as a BITMAPINFOHEADER followed by an optional
colour table and then raw pixel data.  They do NOT have a BITMAPFILEHEADER (that
only appears in .bmp files on disk).

Supported input formats: BI_RGB (uncompressed), 1, 4, 8, 24, or 32 bpp.
Output is always BGR24 (3 bytes per pixel, top-to-bottom row order) so the SDL2
caller only needs to handle one format.
"""

from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.api.pe_resources import PEResources

from tew.logger import logger


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BitmapInfo:
    """Decoded bitmap: always BGR24, top-to-bottom row order.

    ``pixels`` has exactly ``width * height * 3`` bytes.
    Each group of three bytes is [B, G, R] for one pixel.
    Rows are stored top-to-bottom regardless of the original DIB orientation.
    """
    width: int
    height: int     # always positive (bottom-up DIBs are flipped on load)
    pixels: bytes   # width * height * 3 bytes, BGR24, row-major, top-down


# ── DIB constants ─────────────────────────────────────────────────────────────

_BITMAPINFOHEADER_SIZE = 40
_BI_RGB = 0    # uncompressed, no palette compression


# ── DIB parser ────────────────────────────────────────────────────────────────

def parse_dib(raw: bytes) -> BitmapInfo | None:
    """Parse a raw Win32 DIB (no BITMAPFILEHEADER) into a BitmapInfo.

    Supports BI_RGB bitmaps at 1, 4, 8, 24, and 32 bits per pixel.
    Rows stored bottom-up (positive biHeight) are flipped to top-down.
    Returns None and logs a warning if the bitmap cannot be parsed.
    """
    if len(raw) < _BITMAPINFOHEADER_SIZE:
        logger.warn("window", f"[BitmapLoader] DIB too short ({len(raw)} bytes)")
        return None

    hdr_size = struct.unpack_from("<I", raw, 0)[0]
    if hdr_size < _BITMAPINFOHEADER_SIZE:
        logger.warn("window", f"[BitmapLoader] Unsupported DIB header size {hdr_size}")
        return None

    width, height = struct.unpack_from("<ii", raw, 4)
    bpp           = struct.unpack_from("<H",  raw, 14)[0]
    compression   = struct.unpack_from("<I",  raw, 16)[0]
    clr_used      = struct.unpack_from("<I",  raw, 32)[0]

    if compression != _BI_RGB:
        logger.warn("window", f"[BitmapLoader] Compressed DIB (type {compression}) not supported")
        return None

    if width <= 0:
        logger.warn("window", f"[BitmapLoader] Invalid bitmap width {width}")
        return None

    bottom_up  = height > 0
    abs_height = abs(height)

    if abs_height <= 0:
        logger.warn("window", f"[BitmapLoader] Invalid bitmap height {height}")
        return None

    # ── Colour palette (for ≤ 8 bpp) ─────────────────────────────────────────
    # Each RGBQUAD entry is 4 bytes: B, G, R, Reserved.
    # biClrUsed=0 means "use all colours for this depth".
    palette: list[tuple[int, int, int]] = []
    palette_offset = hdr_size

    if bpp <= 8:
        n_colors = clr_used if clr_used > 0 else (1 << bpp)
        for i in range(n_colors):
            off = palette_offset + i * 4
            if off + 4 > len(raw):
                break
            b, g, r, _ = struct.unpack_from("<BBBB", raw, off)
            palette.append((b, g, r))   # stored as (B, G, R) to match BGR24 output
        palette_offset += n_colors * 4

    # ── Pixel data ────────────────────────────────────────────────────────────
    # Row stride is padded to a 4-byte (DWORD) boundary.
    row_stride    = ((bpp * width + 31) // 32) * 4
    pixel_start   = palette_offset
    required_size = pixel_start + row_stride * abs_height

    if len(raw) < required_size:
        logger.warn("window",
            f"[BitmapLoader] Pixel data too short: need {required_size}, have {len(raw)}")
        return None

    # Decode each row to BGR24, reversing order for bottom-up DIBs.
    output_rows: list[bytes] = []

    for row_idx in range(abs_height):
        src_row   = (abs_height - 1 - row_idx) if bottom_up else row_idx
        row_start = pixel_start + src_row * row_stride
        row_bytes = raw[row_start : row_start + row_stride]

        bgr_row = _decode_row(row_bytes, width, bpp, palette)
        if bgr_row is None:
            logger.warn("window", f"[BitmapLoader] Failed to decode row {row_idx}")
            return None

        output_rows.append(bgr_row)

    return BitmapInfo(
        width=width,
        height=abs_height,
        pixels=b"".join(output_rows),
    )


def _decode_row(
    row_bytes: bytes,
    width: int,
    bpp: int,
    palette: list[tuple[int, int, int]],
) -> bytes | None:
    """Decode one DIB row to BGR24 (3 bytes per pixel).

    Returns exactly ``width * 3`` bytes, or None if the input is too short.
    """
    result = bytearray(width * 3)

    if bpp == 24:
        # DIB 24-bit stores pixels as B, G, R — already BGR24.
        src_len = width * 3
        if len(row_bytes) < src_len:
            return None
        result[:] = row_bytes[:src_len]

    elif bpp == 32:
        # DIB 32-bit stores pixels as B, G, R, X — drop the reserved byte.
        if len(row_bytes) < width * 4:
            return None
        for i in range(width):
            result[i * 3 : i * 3 + 3] = row_bytes[i * 4 : i * 4 + 3]

    elif bpp == 8:
        if len(row_bytes) < width:
            return None
        for i in range(width):
            idx = row_bytes[i]
            if idx < len(palette):
                b, g, r = palette[idx]
                result[i * 3]     = b
                result[i * 3 + 1] = g
                result[i * 3 + 2] = r
            # Indices beyond palette end render as black (zeroes already set).

    elif bpp == 4:
        for i in range(width):
            if (i // 2) >= len(row_bytes):
                return None
            byte   = row_bytes[i // 2]
            nibble = (byte >> 4) if (i % 2 == 0) else (byte & 0xF)
            if nibble < len(palette):
                b, g, r = palette[nibble]
                result[i * 3]     = b
                result[i * 3 + 1] = g
                result[i * 3 + 2] = r

    elif bpp == 1:
        for i in range(width):
            if (i // 8) >= len(row_bytes):
                return None
            byte = row_bytes[i // 8]
            bit  = (byte >> (7 - (i % 8))) & 1
            if bit < len(palette):
                b, g, r = palette[bit]
                result[i * 3]     = b
                result[i * 3 + 1] = g
                result[i * 3 + 2] = r

    else:
        logger.warn("window", f"[BitmapLoader] Unsupported bpp={bpp}")
        return None

    return bytes(result)


# ── SDL2 texture creation ─────────────────────────────────────────────────────

def create_sdl_texture(renderer: object, info: BitmapInfo) -> object | None:
    """Create an SDL2 texture from a BitmapInfo.

    Converts the BGR24 pixel data to an SDL2 surface, uploads it as a texture,
    then frees the surface.  The caller must call SDL_DestroyTexture on the
    returned texture when it is no longer needed.

    Returns None if texture creation fails.
    """
    from sdl2 import (
        SDL_CreateRGBSurfaceFrom,
        SDL_FreeSurface,
        SDL_CreateTextureFromSurface,
    )

    pitch    = info.width * 3   # BGR24: 3 bytes per pixel, no row padding
    # Keep pixel_buf alive until SDL_FreeSurface so the surface's pixel pointer
    # remains valid.  SDL_CreateTextureFromSurface copies data to the GPU, so
    # the buffer is safe to release after the surface is freed.
    pixel_buf = (ctypes.c_uint8 * len(info.pixels)).from_buffer_copy(info.pixels)

    # BGR24 bitmask interpretation (little-endian 24-bit integer, byte order B G R):
    #   B occupies bits  0– 7 → Bmask = 0x0000FF
    #   G occupies bits  8–15 → Gmask = 0x00FF00
    #   R occupies bits 16–23 → Rmask = 0xFF0000
    surface = SDL_CreateRGBSurfaceFrom(
        ctypes.cast(pixel_buf, ctypes.c_void_p),
        info.width,
        info.height,
        24,
        pitch,
        0xFF0000,   # Rmask
        0x00FF00,   # Gmask
        0x0000FF,   # Bmask
        0,          # Amask (no alpha)
    )
    if not surface:
        logger.warn("window",
            f"[BitmapLoader] SDL_CreateRGBSurfaceFrom failed "
            f"({info.width}x{info.height})")
        return None

    texture = SDL_CreateTextureFromSurface(renderer, surface)
    SDL_FreeSurface(surface)
    # pixel_buf goes out of scope after this point — safe because the surface
    # is already freed and the texture was uploaded to the GPU.

    if not texture:
        logger.warn("window", "[BitmapLoader] SDL_CreateTextureFromSurface failed")
        return None

    return texture


# ── High-level loader ─────────────────────────────────────────────────────────

def load_bitmap_texture(
    renderer: object,
    bitmap_id: int,
    pe_resources: "PEResources",
) -> object | None:
    """Load bitmap resource *bitmap_id* from PE resources and create an SDL2 texture.

    Returns the SDL_Texture, or None if the resource is missing or the bitmap
    cannot be decoded.  The caller is responsible for calling SDL_DestroyTexture
    when the texture is no longer needed.
    """
    raw = pe_resources.find_bitmap(bitmap_id)
    if raw is None:
        logger.warn("window", f"[BitmapLoader] Bitmap resource {bitmap_id} not found in PE")
        return None

    info = parse_dib(raw)
    if info is None:
        return None

    texture = create_sdl_texture(renderer, info)
    if texture is not None:
        logger.debug("window",
            f"[BitmapLoader] Loaded bitmap resource {bitmap_id}: "
            f"{info.width}x{info.height} px")
    return texture
