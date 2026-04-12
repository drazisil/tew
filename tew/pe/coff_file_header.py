"""COFF File Header parser."""

from __future__ import annotations
import struct
from datetime import datetime, timezone

from tew.helpers import hex_val

MACHINE_TYPES: dict[int, str] = {
    0x14C: "IMAGE_FILE_MACHINE_I386",
}


class COFFFileHeader:
    SIZE_OF = 20

    def __init__(self, data: bytes | bytearray) -> None:
        machine_id = struct.unpack_from("<H", data, 0)[0]
        self._machine = MACHINE_TYPES.get(machine_id, f"UNKNOWN(0x{machine_id:04x})")
        self._number_of_sections = struct.unpack_from("<H", data, 2)[0]
        self._time_date_stamp = struct.unpack_from("<I", data, 4)[0]
        self._pointer_to_symbol_table = struct.unpack_from("<I", data, 8)[0]
        self._number_of_symbols = struct.unpack_from("<I", data, 12)[0]
        self._size_of_optional_header = struct.unpack_from("<H", data, 16)[0]
        self._characteristics = struct.unpack_from("<H", data, 18)[0]

    @property
    def machine(self) -> str:
        return self._machine

    @property
    def number_of_sections(self) -> int:
        return self._number_of_sections

    @property
    def time_date_stamp(self) -> int:
        return self._time_date_stamp

    @property
    def pointer_to_symbol_table(self) -> int:
        return self._pointer_to_symbol_table

    @property
    def number_of_symbols(self) -> int:
        return self._number_of_symbols

    @property
    def size_of_optional_header(self) -> int:
        return self._size_of_optional_header

    @property
    def characteristics(self) -> int:
        return self._characteristics

    def __str__(self) -> str:
        dt = datetime.fromtimestamp(self._time_date_stamp, tz=timezone.utc).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
        return "\n".join([
            f"Machine:              {self._machine}",
            f"NumberOfSections:     {self._number_of_sections}",
            f"TimeDateStamp:        {hex_val(self._time_date_stamp)} ({dt})",
            f"PointerToSymbolTable: {hex_val(self._pointer_to_symbol_table)}",
            f"NumberOfSymbols:      {self._number_of_symbols}",
            f"SizeOfOptionalHeader: {hex_val(self._size_of_optional_header, 4)}",
            f"Characteristics:      {hex_val(self._characteristics, 4)}",
        ])
