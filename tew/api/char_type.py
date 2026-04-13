"""
Win32 character-type classification for GetStringTypeW.

Implements CT_CTYPE1 for the ASCII range (U+0000–U+007F), which covers all
characters used by Motor City Online.

Only CT_CTYPE1 is implemented.  The handler must halt the emulator if the
game ever calls GetStringTypeW with CT_CTYPE2 or CT_CTYPE3 — we will add
those only when we have evidence they are needed.

Win32 reference:
    https://learn.microsoft.com/en-us/windows/win32/api/stringapiset/nf-stringapiset-getstringtypew
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag
from typing import Final, Protocol


# ── dwInfoType constants ─────────────────────────────────────────────────────

CT_CTYPE1: Final[int] = 1  # Character-type (alpha / digit / space / …)
CT_CTYPE2: Final[int] = 2  # Bidirectional layout
CT_CTYPE3: Final[int] = 4  # Text-processing


# ── CT_CTYPE1 bitmask ────────────────────────────────────────────────────────

class Ctype1(IntFlag):
    """
    CT_CTYPE1 character-classification bits.

    Each wide character maps to a WORD whose bits are OR-ed from these values.
    Names and values match winnls.h (C1_UPPER, C1_LOWER, …).
    """
    NONE   = 0x0000
    UPPER  = 0x0001  # Uppercase letter
    LOWER  = 0x0002  # Lowercase letter
    DIGIT  = 0x0004  # Decimal digit
    SPACE  = 0x0008  # Space character
    PUNCT  = 0x0010  # Punctuation
    CNTRL  = 0x0020  # Control character
    BLANK  = 0x0040  # Blank (space U+0020 or horizontal tab U+0009)
    XDIGIT = 0x0080  # Hexadecimal digit (0–9, A–F, a–f)
    ALPHA  = 0x0100  # Letter (any linguistic character)


# ── DTOs ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GetStringTypeArgs:
    """
    Parsed stdcall stack frame for GetStringTypeW.

    Offsets are relative to ESP at handler entry (before cleanup):
        ESP+ 4   dwInfoType
        ESP+ 8   lpSrcStr      pointer to UTF-16LE string in guest memory
        ESP+12   cchSrc        character count, or 0xFFFFFFFF (−1) if null-terminated
        ESP+16   lpCharType    pointer to output WORD array
    """
    info_type: int
    src_ptr:   int
    cch_src:   int
    out_ptr:   int


# ── Memory protocol ──────────────────────────────────────────────────────────

class WideMemory(Protocol):
    """
    Minimal interface for reading and writing 16-bit wide-character buffers.

    ``tew.hardware.memory.Memory`` satisfies this protocol.  Tests may pass
    any object that implements these two methods.
    """
    def read16(self, addr: int) -> int: ...
    def write16(self, addr: int, value: int) -> None: ...


# ── CT_CTYPE1 lookup table ───────────────────────────────────────────────────

def _build_ctype1_table() -> list[int]:
    """
    Build the CT_CTYPE1 classification table for U+0000–U+007F.

    Returns a 128-element list where index == codepoint and value is
    the Ctype1 bitmask stored as a plain int (ready for memory.write16).

    ASCII layout reference:
        0x00–0x08  NUL … BS      control
        0x09       HT (tab)      control + space + blank
        0x0A–0x0D  LF VT FF CR   control + space
        0x0E–0x1F  SO … US       control
        0x20       SPACE         space + blank
        0x21–0x2F  ! … /         punctuation
        0x30–0x39  0 … 9         digit + xdigit
        0x3A–0x40  : … @         punctuation
        0x41–0x46  A … F         upper + alpha + xdigit
        0x47–0x5A  G … Z         upper + alpha
        0x5B–0x60  [ … `         punctuation
        0x61–0x66  a … f         lower + alpha + xdigit
        0x67–0x7A  g … z         lower + alpha
        0x7B–0x7E  { … ~         punctuation
        0x7F       DEL           control
    """
    F = Ctype1
    table: list[int] = [int(F.NONE)] * 128

    # Control: 0x00–0x08, 0x0E–0x1F, 0x7F
    for cp in [*range(0x00, 0x09), *range(0x0E, 0x20), 0x7F]:
        table[cp] = int(F.CNTRL)

    # Tab 0x09: control + space + blank
    table[0x09] = int(F.CNTRL | F.SPACE | F.BLANK)

    # LF 0x0A, VT 0x0B, FF 0x0C, CR 0x0D: control + space (not blank)
    for cp in range(0x0A, 0x0E):
        table[cp] = int(F.CNTRL | F.SPACE)

    # Space 0x20: space + blank (not control, not alpha, not digit)
    table[0x20] = int(F.SPACE | F.BLANK)

    # Punctuation: !"#$%&'()*+,-./  :;<=>?@  [\]^_`  {|}~
    for cp in [*range(0x21, 0x30), *range(0x3A, 0x41),
               *range(0x5B, 0x61), *range(0x7B, 0x7F)]:
        table[cp] = int(F.PUNCT)

    # Digits 0–9: digit + xdigit
    for cp in range(0x30, 0x3A):
        table[cp] = int(F.DIGIT | F.XDIGIT)

    # A–F: upper + alpha + xdigit
    for cp in range(0x41, 0x47):
        table[cp] = int(F.UPPER | F.ALPHA | F.XDIGIT)

    # G–Z: upper + alpha
    for cp in range(0x47, 0x5B):
        table[cp] = int(F.UPPER | F.ALPHA)

    # a–f: lower + alpha + xdigit
    for cp in range(0x61, 0x67):
        table[cp] = int(F.LOWER | F.ALPHA | F.XDIGIT)

    # g–z: lower + alpha
    for cp in range(0x67, 0x7B):
        table[cp] = int(F.LOWER | F.ALPHA)

    return table


_CTYPE1_TABLE: Final[list[int]] = _build_ctype1_table()


# ── Public API ───────────────────────────────────────────────────────────────

def classify_ctype1(codepoint: int) -> int:
    """
    Return the CT_CTYPE1 classification WORD for *codepoint*.

    Covers U+0000–U+007F.  Codepoints outside that range return 0 — MCO
    does not use non-ASCII wide characters, so returning zero is explicit
    rather than a silent approximation.
    """
    if 0 <= codepoint < len(_CTYPE1_TABLE):
        return _CTYPE1_TABLE[codepoint]
    return 0


def wide_strlen(mem: WideMemory, ptr: int) -> int:
    """Count UTF-16LE code units from *ptr* until U+0000 (not including the null)."""
    count = 0
    while mem.read16((ptr + count * 2) & 0xFFFFFFFF) != 0:
        count += 1
    return count


def classify_wide_string(mem: WideMemory, args: GetStringTypeArgs) -> bool:
    """
    Classify each wide character in a guest-memory string using CT_CTYPE1.

    Reads UTF-16LE code units from *args.src_ptr* — either *args.cch_src*
    characters, or until a null terminator when ``cch_src == 0xFFFFFFFF``.
    Writes a WORD CT_CTYPE1 bitmask for each character to *args.out_ptr*.

    Returns True on success, False if *args.info_type* is not CT_CTYPE1.
    The caller is responsible for halting when False is returned.
    """
    if args.info_type != CT_CTYPE1:
        return False

    count = (
        wide_strlen(mem, args.src_ptr)
        if args.cch_src == 0xFFFFFFFF
        else args.cch_src
    )

    for i in range(count):
        cp = mem.read16((args.src_ptr + i * 2) & 0xFFFFFFFF)
        flags = classify_ctype1(cp)
        mem.write16((args.out_ptr + i * 2) & 0xFFFFFFFF, flags)

    return True
