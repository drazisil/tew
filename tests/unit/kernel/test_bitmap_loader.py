"""Tests for tew.api.bitmap_loader — Win32 DIB parsing.

SDL2 texture creation (create_sdl_texture, load_bitmap_texture) requires an
active SDL2 renderer and is tested as an integration concern; it is not
covered here.  These tests cover only the pure-Python parse_dib path.
"""

from __future__ import annotations

import struct
import pytest

from tew.api.bitmap_loader import BitmapInfo, parse_dib


# ── Test helpers ──────────────────────────────────────────────────────────────

def _bitmapinfoheader(
    width: int,
    height: int,
    bpp: int,
    clr_used: int = 0,
    compression: int = 0,
) -> bytes:
    """Build a 40-byte BITMAPINFOHEADER."""
    return struct.pack(
        "<IiiHHIIiiII",
        40,           # biSize
        width,        # biWidth  (positive = left-to-right columns)
        height,       # biHeight (positive = bottom-up; negative = top-down)
        1,            # biPlanes
        bpp,          # biBitCount
        compression,  # biCompression (0 = BI_RGB)
        0,            # biSizeImage
        0,            # biXPelsPerMeter
        0,            # biYPelsPerMeter
        clr_used,     # biClrUsed
        0,            # biClrImportant
    )


def _palette(n: int) -> bytes:
    """Build an n-entry RGBQUAD palette where entry i has all channels = i."""
    result = bytearray()
    for i in range(n):
        v = min(i, 255)
        result.extend([v, v, v, 0])  # B, G, R, Reserved
    return bytes(result)


def _row_stride(bpp: int, width: int) -> int:
    """Return the DWORD-padded row stride in bytes."""
    return ((bpp * width + 31) // 32) * 4


# ── 24-bit tests ──────────────────────────────────────────────────────────────

class TestParseDib24Bit:
    def test_top_down_2x2(self) -> None:
        """Negative biHeight means top-down — rows should NOT be flipped."""
        # Row 0 (top): pixel (0,0) = pure blue (B=255,G=0,R=0),
        #              pixel (0,1) = pure green (B=0,G=255,R=0)
        # Row 1 (bot): pixel (1,0) = pure red (B=0,G=0,R=255),
        #              pixel (1,1) = white (B=255,G=255,R=255)
        # 24-bit stride for width=2: 2*3=6, padded to 8 (next multiple of 4 ≥ 6)
        row0 = bytes([255, 0, 0,   0, 255, 0,   0, 0])   # 6 data + 2 pad
        row1 = bytes([0, 0, 255,   255, 255, 255,   0, 0])
        pixels = row0 + row1
        raw = _bitmapinfoheader(2, -2, 24) + pixels

        info = parse_dib(raw)

        assert info is not None
        assert info.width == 2
        assert info.height == 2
        assert len(info.pixels) == 2 * 2 * 3
        # Output row 0: blue, green
        assert info.pixels[0:3]  == bytes([255, 0, 0])
        assert info.pixels[3:6]  == bytes([0, 255, 0])
        # Output row 1: red, white
        assert info.pixels[6:9]  == bytes([0, 0, 255])
        assert info.pixels[9:12] == bytes([255, 255, 255])

    def test_bottom_up_flip(self) -> None:
        """Positive biHeight means bottom-up — rows are reversed on output."""
        # Stored order (bottom-up): row 0 (image bottom), row 1 (image top)
        # We put a recognisable marker in each row.
        # Stored row 0 (image bottom): BGR [10,20,30] + pad
        # Stored row 1 (image top):    BGR [40,50,60] + pad
        row0_stored = bytes([10, 20, 30, 0])   # width=1 → stride=4
        row1_stored = bytes([40, 50, 60, 0])
        pixels = row0_stored + row1_stored
        raw = _bitmapinfoheader(1, 2, 24) + pixels   # positive height = bottom-up

        info = parse_dib(raw)

        assert info is not None
        assert info.height == 2
        # After flip: stored row 1 (image top) comes first
        assert info.pixels[0:3] == bytes([40, 50, 60])
        assert info.pixels[3:6] == bytes([10, 20, 30])

    def test_single_pixel(self) -> None:
        """1x1 24-bit top-down — simplest non-trivial case."""
        pixels = bytes([0xAB, 0xCD, 0xEF, 0])   # BGR + 1 pad (stride=4)
        raw = _bitmapinfoheader(1, -1, 24) + pixels

        info = parse_dib(raw)

        assert info is not None
        assert info.width == 1
        assert info.height == 1
        assert info.pixels == bytes([0xAB, 0xCD, 0xEF])


# ── 32-bit tests ──────────────────────────────────────────────────────────────

class TestParseDib32Bit:
    def test_alpha_channel_dropped(self) -> None:
        """32-bit DIB stores B, G, R, X — the X byte is discarded."""
        # Single pixel: B=10, G=20, R=30, X=255
        pixels = bytes([10, 20, 30, 255])
        raw = _bitmapinfoheader(1, -1, 32) + pixels

        info = parse_dib(raw)

        assert info is not None
        assert info.pixels == bytes([10, 20, 30])

    def test_two_pixels_top_down(self) -> None:
        """2x1 32-bit top-down."""
        pixels = bytes([1, 2, 3, 0,   4, 5, 6, 0])
        raw = _bitmapinfoheader(2, -1, 32) + pixels

        info = parse_dib(raw)

        assert info is not None
        assert info.width == 2
        assert info.pixels == bytes([1, 2, 3,   4, 5, 6])


# ── 8-bit indexed tests ───────────────────────────────────────────────────────

class TestParseDib8Bit:
    def test_basic_indexed(self) -> None:
        """2x1 8-bit: two pixels with palette lookup."""
        pal = _palette(4)   # entries 0–3; all channels = index value
        # clr_used=4 so the parser reads exactly 4 entries, not 256
        hdr = _bitmapinfoheader(2, -1, 8, clr_used=4)
        # Stride for 8-bit width=2 is 4 bytes (padded)
        pixels = bytes([0, 3, 0, 0])   # pixel 0 → palette[0], pixel 1 → palette[3]
        raw = hdr + pal + pixels

        info = parse_dib(raw)

        assert info is not None
        assert info.width == 2
        assert info.height == 1
        # palette[0] → (B=0,G=0,R=0), palette[3] → (B=3,G=3,R=3)
        assert info.pixels[0:3] == bytes([0, 0, 0])
        assert info.pixels[3:6] == bytes([3, 3, 3])

    def test_out_of_range_index_renders_black(self) -> None:
        """Pixel index beyond palette end is rendered as black (not an error)."""
        pal = _palette(2)   # only 2 entries
        hdr = _bitmapinfoheader(1, -1, 8, clr_used=2)
        pixels = bytes([5, 0, 0, 0])   # index 5 is beyond the palette
        raw = hdr + pal + pixels

        info = parse_dib(raw)

        assert info is not None
        assert info.pixels[0:3] == bytes([0, 0, 0])   # black


# ── 4-bit indexed tests ───────────────────────────────────────────────────────

class TestParseDib4Bit:
    def test_two_pixels_in_one_byte(self) -> None:
        """4-bit: two pixels packed into one byte (high nibble first)."""
        pal = _palette(16)   # 16-entry palette (all channels = index)
        hdr = _bitmapinfoheader(2, -1, 4)   # clr_used=0 → read 2^4=16 entries
        # Stride for 4-bit width=2: (4*2+31)//32*4 = 4 bytes
        pixels = bytes([0x3A, 0, 0, 0])   # pixel 0 → nibble 3, pixel 1 → nibble 10
        raw = hdr + pal + pixels

        info = parse_dib(raw)

        assert info is not None
        assert info.width == 2
        # palette[3] → (B=3,G=3,R=3), palette[10] → (B=10,G=10,R=10)
        assert info.pixels[0:3] == bytes([3, 3, 3])
        assert info.pixels[3:6] == bytes([10, 10, 10])

    def test_odd_width_padding(self) -> None:
        """4-bit with an odd width: the second nibble of the last byte is unused."""
        pal = _palette(16)
        hdr = _bitmapinfoheader(3, -1, 4)   # 3 pixels, clr_used=0 → 16 entries
        # Stride for 4-bit width=3: (4*3+31)//32*4 = 4 bytes
        # Byte 0: pixel 0=nibble 5, pixel 1=nibble 7 → 0x57
        # Byte 1: pixel 2=nibble 1, padding nibble → 0x10 (padding nibble irrelevant)
        pixels = bytes([0x57, 0x10, 0, 0])
        raw = hdr + pal + pixels

        info = parse_dib(raw)

        assert info is not None
        assert info.width == 3
        assert info.pixels[0:3]  == bytes([5, 5, 5])
        assert info.pixels[3:6]  == bytes([7, 7, 7])
        assert info.pixels[6:9]  == bytes([1, 1, 1])


# ── 1-bit indexed tests ───────────────────────────────────────────────────────

class TestParseDib1Bit:
    def test_monochrome_8pixels(self) -> None:
        """1-bit: 8 pixels in one byte using a two-entry palette."""
        # Palette: entry 0 = black (0,0,0), entry 1 = white (255,255,255)
        pal = bytes([0, 0, 0, 0,   255, 255, 255, 0])
        hdr = _bitmapinfoheader(8, -1, 1, clr_used=2)
        # Stride for 1-bit width=8: (1*8+31)//32*4 = 4 bytes
        # Byte 0 = 0b10110010: pixels 0,2,3,6 = white; others = black
        pixels = bytes([0b10110010, 0, 0, 0])
        raw = hdr + pal + pixels

        info = parse_dib(raw)

        assert info is not None
        assert info.width == 8
        expected_bits = [1, 0, 1, 1, 0, 0, 1, 0]
        for i, bit in enumerate(expected_bits):
            v = 255 if bit else 0
            assert info.pixels[i * 3 : i * 3 + 3] == bytes([v, v, v]), \
                f"pixel {i}: expected {v}"


# ── Error handling tests ──────────────────────────────────────────────────────

class TestParseDibErrors:
    def test_too_short_for_header(self) -> None:
        """Fewer than 40 bytes → None."""
        assert parse_dib(b"\x00" * 20) is None

    def test_header_size_too_small(self) -> None:
        """biSize < 40 → None."""
        raw = struct.pack("<I", 16) + b"\x00" * 36   # biSize=16 but we have 40 bytes
        assert parse_dib(raw) is None

    def test_compressed_unsupported(self) -> None:
        """BI_RLE8 (compression=1) → None."""
        raw = _bitmapinfoheader(4, -4, 8, compression=1)
        assert parse_dib(raw) is None

    def test_pixel_data_too_short(self) -> None:
        """Pixel buffer smaller than width*height*3 bytes → None."""
        # A 100x100 24-bit image needs 100*100*3 = 30000 bytes of pixel data.
        raw = _bitmapinfoheader(100, -100, 24) + bytes(100)
        assert parse_dib(raw) is None

    def test_zero_width(self) -> None:
        """biWidth=0 → None."""
        raw = _bitmapinfoheader(0, -1, 24) + bytes(100)
        assert parse_dib(raw) is None

    def test_zero_height(self) -> None:
        """biHeight=0 → None."""
        raw = _bitmapinfoheader(1, 0, 24) + bytes(100)
        assert parse_dib(raw) is None


# ── BitmapInfo property tests ─────────────────────────────────────────────────

class TestBitmapInfo:
    def test_pixel_buffer_size(self) -> None:
        """pixels length must equal width * height * 3."""
        raw = _bitmapinfoheader(3, -2, 24) + bytes(_row_stride(24, 3) * 2)
        info = parse_dib(raw)
        assert info is not None
        assert len(info.pixels) == 3 * 2 * 3

    def test_frozen_dataclass(self) -> None:
        """BitmapInfo is immutable."""
        info = BitmapInfo(width=1, height=1, pixels=bytes(3))
        with pytest.raises((AttributeError, TypeError)):
            info.width = 99  # type: ignore[misc]
