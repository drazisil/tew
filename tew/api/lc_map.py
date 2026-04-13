"""
Win32 LCMapStringW — locale-aware wide-string mapping.

Implements the string transformations used by MSVC CRT locale initialisation:
    LCMAP_LOWERCASE (0x00000100)  — map all characters to lowercase
    LCMAP_UPPERCASE (0x00000200)  — map all characters to uppercase

LCMAP_LINGUISTIC_CASING (0x01000000) is recognised and stripped before flag
checking.  Linguistic casing affects Turkish ı/I; MCO is English-only, so the
distinction does not apply to the ASCII range we support.

Any other active flag combination returns None to signal an unsupported
operation.  The handler is responsible for halting the emulator in that case.

Win32 reference:
    https://learn.microsoft.com/en-us/windows/win32/api/winnls/nf-winnls-lcmapstringw
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag
from typing import Final

from tew.api.char_type import WideMemory, wide_strlen


# ── Map flag constants ────────────────────────────────────────────────────────

class LCMapFlags(IntFlag):
    """
    dwMapFlags values for LCMapStringW.

    Names and values match winnls.h.  Only LCMAP_LOWERCASE and
    LCMAP_UPPERCASE are implemented; the rest are listed for documentation.
    """
    LCMAP_LOWERCASE         = 0x00000100  # Map to lowercase
    LCMAP_UPPERCASE         = 0x00000200  # Map to uppercase
    LCMAP_SORTKEY           = 0x00000400  # Sort key (not supported)
    LCMAP_BYTEREV           = 0x00000800  # Byte reversal (not supported)
    LCMAP_LINGUISTIC_CASING = 0x01000000  # Linguistic rules (ignored for ASCII)


# ── DTO ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LCMapStringArgs:
    """
    Parsed stdcall stack frame for LCMapStringW.

    Offsets relative to ESP at handler entry (before cleanup):
        ESP+ 4   Locale     LCID — ignored; we support only ASCII/invariant locale
        ESP+ 8   dwMapFlags
        ESP+12   lpSrcStr   pointer to source UTF-16LE string in guest memory
        ESP+16   cchSrc     char count, or 0xFFFFFFFF (−1) for null-terminated
        ESP+20   lpDestStr  pointer to output buffer (ignored when cch_dest == 0)
        ESP+24   cchDest    output buffer size in chars; 0 = return required size
    """
    locale:    int
    map_flags: int
    src_ptr:   int
    cch_src:   int
    dest_ptr:  int
    cch_dest:  int


# ── Internal helpers ──────────────────────────────────────────────────────────

# Flags that can safely be stripped before checking which operation to apply.
_IGNORABLE: Final[int] = int(LCMapFlags.LCMAP_LINGUISTIC_CASING)

# The two operations we support, as plain ints for fast masking.
_FLAG_LOWER: Final[int] = int(LCMapFlags.LCMAP_LOWERCASE)
_FLAG_UPPER: Final[int] = int(LCMapFlags.LCMAP_UPPERCASE)


def _to_lowercase(cp: int) -> int:
    """Lowercase *cp* for the ASCII range (A–Z → a–z).  Non-ASCII is unchanged."""
    if 0x41 <= cp <= 0x5A:   # A–Z
        return cp + 0x20
    return cp


def _to_uppercase(cp: int) -> int:
    """Uppercase *cp* for the ASCII range (a–z → A–Z).  Non-ASCII is unchanged."""
    if 0x61 <= cp <= 0x7A:   # a–z
        return cp - 0x20
    return cp


# ── Public API ────────────────────────────────────────────────────────────────

def lc_map_wide_string(mem: WideMemory, args: LCMapStringArgs) -> int | None:
    """
    Apply a locale string mapping to a UTF-16LE string in guest memory.

    Reads from *args.src_ptr*, writes transformed characters to *args.dest_ptr*.

    Returns:
        int   — characters written, or required buffer size when cch_dest == 0
        None  — unsupported dwMapFlags; caller must halt the emulator

    Win32 size-query semantics:
        cch_dest == 0  → return the required char count; write nothing
        cch_dest >  0  → write up to cch_dest chars; return chars written;
                         return 0 if the buffer is too small

    Null-terminated semantics (cch_src == 0xFFFFFFFF):
        The null terminator is included in the char count and copied to output,
        matching the Win32 behaviour where cchSrc == −1 processes the whole
        string including the terminating null.
    """
    # Strip ignorable modifier flags, then check which operation is requested.
    active = args.map_flags & ~_IGNORABLE

    if active == _FLAG_LOWER:
        transform = _to_lowercase
    elif active == _FLAG_UPPER:
        transform = _to_uppercase
    else:
        return None  # unknown or unsupported combination — caller must halt

    # Determine character count (include null terminator when cch_src == −1).
    if args.cch_src == 0xFFFFFFFF:
        count = wide_strlen(mem, args.src_ptr) + 1
    else:
        count = args.cch_src

    # Size query: return required size without writing.
    if args.cch_dest == 0:
        return count

    # Buffer-too-small: Win32 returns 0 to signal ERROR_INSUFFICIENT_BUFFER.
    if args.cch_dest < count:
        return 0

    # Write each transformed character.
    for i in range(count):
        cp = mem.read16( (args.src_ptr  + i * 2) & 0xFFFFFFFF)
        mem.write16(     (args.dest_ptr + i * 2) & 0xFFFFFFFF, transform(cp))

    return count
