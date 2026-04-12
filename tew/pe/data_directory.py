"""PE Data Directory parser."""

from __future__ import annotations
import struct
from typing import TYPE_CHECKING

from tew.helpers import hex_val, rva_to_offset

if TYPE_CHECKING:
    from tew.pe.section_header import SectionHeader

DATA_DIRECTORY_NAMES = [
    "Export Table",
    "Import Table",
    "Resource Table",
    "Exception Table",
    "Certificate Table",
    "Base Relocation Table",
    "Debug",
    "Architecture",
    "Global Ptr",
    "TLS Table",
    "Load Config Table",
    "Bound Import",
    "IAT",
    "Delay Import Descriptor",
    "CLR Runtime Header",
    "Reserved",
]


class DataDirectory:
    SIZE_OF = 8

    def __init__(self, data: bytes | bytearray, index: int) -> None:
        self._virtual_address = struct.unpack_from("<I", data, 0)[0]
        self._size = struct.unpack_from("<I", data, 4)[0]
        self._index = index
        self._name = (
            DATA_DIRECTORY_NAMES[index]
            if index < len(DATA_DIRECTORY_NAMES)
            else f"Unknown ({index})"
        )
        self._data: bytes = b""

    def resolve(self, file_image: bytes | bytearray, sections: list["SectionHeader"]) -> None:
        if self._virtual_address == 0 or self._size == 0:
            return

        if self._index == 4:
            # Certificate Table uses a file pointer, not an RVA
            file_offset = self._virtual_address
        else:
            file_offset = rva_to_offset(self._virtual_address, sections)
            if file_offset == -1:
                return

        self._data = bytes(file_image[file_offset : file_offset + self._size])

    @property
    def virtual_address(self) -> int:
        return self._virtual_address

    @property
    def size(self) -> int:
        return self._size

    @property
    def name(self) -> str:
        return self._name

    @property
    def data(self) -> bytes:
        return self._data

    def __str__(self) -> str:
        s = f"{self._name}: {hex_val(self._virtual_address)} ({hex_val(self._size)} bytes)"
        if self._data:
            rows = []
            for i in range(0, len(self._data), 16):
                chunk = self._data[i:i + 16]
                hex_bytes = " ".join(f"{b:02X}" for b in chunk)
                ascii_part = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in chunk)
                rows.append(f"    {hex_val(i, 8)}  {hex_bytes:<47}  {ascii_part}")
            s += "\n" + "\n".join(rows)
        return s
