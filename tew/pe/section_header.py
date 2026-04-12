"""PE Section Header parser."""

from __future__ import annotations
import struct

from tew.helpers import hex_val


class SectionHeader:
    SIZE_OF = 40

    def __init__(self, data: bytes | bytearray) -> None:
        self._name = data[0:8].rstrip(b"\x00").decode("utf-8", errors="replace")
        self._virtual_size = struct.unpack_from("<I", data, 8)[0]
        self._virtual_address = struct.unpack_from("<I", data, 12)[0]
        self._size_of_raw_data = struct.unpack_from("<I", data, 16)[0]
        self._pointer_to_raw_data = struct.unpack_from("<I", data, 20)[0]
        self._pointer_to_relocations = struct.unpack_from("<I", data, 24)[0]
        self._pointer_to_linenumbers = struct.unpack_from("<I", data, 28)[0]
        self._number_of_relocations = struct.unpack_from("<H", data, 32)[0]
        self._number_of_linenumbers = struct.unpack_from("<H", data, 34)[0]
        self._characteristics = struct.unpack_from("<I", data, 36)[0]
        self._data: bytes = b""

    def resolve(self, file_image: bytes | bytearray) -> None:
        if self._pointer_to_raw_data == 0 or self._size_of_raw_data == 0:
            return
        start = self._pointer_to_raw_data
        end = start + self._size_of_raw_data
        self._data = bytes(file_image[start:end])

    @property
    def name(self) -> str:
        return self._name

    @property
    def virtual_size(self) -> int:
        return self._virtual_size

    @property
    def virtual_address(self) -> int:
        return self._virtual_address

    @property
    def size_of_raw_data(self) -> int:
        return self._size_of_raw_data

    @property
    def pointer_to_raw_data(self) -> int:
        return self._pointer_to_raw_data

    @property
    def pointer_to_relocations(self) -> int:
        return self._pointer_to_relocations

    @property
    def pointer_to_linenumbers(self) -> int:
        return self._pointer_to_linenumbers

    @property
    def number_of_relocations(self) -> int:
        return self._number_of_relocations

    @property
    def number_of_linenumbers(self) -> int:
        return self._number_of_linenumbers

    @property
    def characteristics(self) -> int:
        return self._characteristics

    @property
    def data(self) -> bytes:
        return self._data

    def __str__(self) -> str:
        lines = [
            f"Name:                 {self._name}",
            f"VirtualSize:          {hex_val(self._virtual_size)}",
            f"VirtualAddress:       {hex_val(self._virtual_address)}",
            f"SizeOfRawData:        {hex_val(self._size_of_raw_data)}",
            f"PointerToRawData:     {hex_val(self._pointer_to_raw_data)}",
            f"PointerToRelocations: {hex_val(self._pointer_to_relocations)}",
            f"PointerToLinenumbers: {hex_val(self._pointer_to_linenumbers)}",
            f"NumberOfRelocations:  {self._number_of_relocations}",
            f"NumberOfLinenumbers:  {self._number_of_linenumbers}",
            f"Characteristics:      {hex_val(self._characteristics)}",
        ]
        if self._data:
            lines.append("")
            for i in range(0, len(self._data), 16):
                chunk = self._data[i:i + 16]
                hex_bytes = " ".join(f"{b:02X}" for b in chunk)
                ascii_part = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in chunk)
                lines.append(f"    {hex_val(i, 8)}  {hex_bytes:<47}  {ascii_part}")
        return "\n".join(lines)
