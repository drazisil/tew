"""Shared helper utilities for PE parsing."""

from __future__ import annotations
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.pe.section_header import SectionHeader


def get16(buffer: bytes | bytearray, offset: int) -> int:
    """Read a little-endian 16-bit signed integer."""
    return struct.unpack_from("<h", buffer, offset)[0]


def hex_val(value: int, pad: int = 8) -> str:
    """Format an integer as '0x' prefixed zero-padded uppercase hex."""
    return "0x" + format(value & ((1 << (pad * 4)) - 1), f"0{pad}X")


def rva_to_offset(rva: int, sections: list["SectionHeader"]) -> int:
    """
    Convert a Relative Virtual Address to a file offset using section headers.
    Returns -1 if no section contains the RVA.
    """
    for section in sections:
        effective_size = max(section.virtual_size, section.size_of_raw_data)
        if section.virtual_address <= rva < section.virtual_address + effective_size:
            return section.pointer_to_raw_data + (rva - section.virtual_address)
    return -1


def read_null_terminated(data: bytes | bytearray, offset: int, max_len: int = 256) -> str:
    """Read a null-terminated ASCII/UTF-8 string from a buffer."""
    end = data.find(b"\x00", offset)
    if end == -1:
        end = min(offset + max_len, len(data))
    return data[offset:end].decode("utf-8", errors="replace")
