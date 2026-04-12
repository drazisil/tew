"""PE TLS (Thread-Local Storage) Directory parser."""

from __future__ import annotations
import struct
from typing import TYPE_CHECKING

from tew.helpers import hex_val, rva_to_offset

if TYPE_CHECKING:
    from tew.pe.section_header import SectionHeader


class TLSDirectory:
    def __init__(
        self,
        data: bytes | bytearray,
        file_image: bytes | bytearray,
        sections: list["SectionHeader"],
        is_pe32plus: bool,
        image_base: int,
    ) -> None:
        self._callbacks: list[int] = []

        if is_pe32plus:
            if len(data) < 40:
                return
            self._start_address_of_raw_data = struct.unpack_from("<Q", data, 0)[0]
            self._end_address_of_raw_data = struct.unpack_from("<Q", data, 8)[0]
            self._address_of_index = struct.unpack_from("<Q", data, 16)[0]
            self._address_of_callbacks = struct.unpack_from("<Q", data, 24)[0]
            self._size_of_zero_fill = struct.unpack_from("<I", data, 32)[0]
            self._characteristics = struct.unpack_from("<I", data, 36)[0]
        else:
            if len(data) < 24:
                return
            self._start_address_of_raw_data = struct.unpack_from("<I", data, 0)[0]
            self._end_address_of_raw_data = struct.unpack_from("<I", data, 4)[0]
            self._address_of_index = struct.unpack_from("<I", data, 8)[0]
            self._address_of_callbacks = struct.unpack_from("<I", data, 12)[0]
            self._size_of_zero_fill = struct.unpack_from("<I", data, 16)[0]
            self._characteristics = struct.unpack_from("<I", data, 20)[0]

        if self._address_of_callbacks != 0:
            callbacks_rva = self._address_of_callbacks - image_base
            callbacks_offset = rva_to_offset(callbacks_rva, sections)
            if callbacks_offset != -1:
                ptr_size = 8 if is_pe32plus else 4
                i = 0
                while True:
                    off = callbacks_offset + i * ptr_size
                    if off + ptr_size > len(file_image):
                        break
                    if is_pe32plus:
                        cb = struct.unpack_from("<Q", file_image, off)[0]
                    else:
                        cb = struct.unpack_from("<I", file_image, off)[0]
                    if cb == 0:
                        break
                    self._callbacks.append(cb)
                    i += 1

    @property
    def start_address_of_raw_data(self) -> int: return self._start_address_of_raw_data
    @property
    def end_address_of_raw_data(self) -> int: return self._end_address_of_raw_data
    @property
    def address_of_index(self) -> int: return self._address_of_index
    @property
    def address_of_callbacks(self) -> int: return self._address_of_callbacks
    @property
    def size_of_zero_fill(self) -> int: return self._size_of_zero_fill
    @property
    def characteristics(self) -> int: return self._characteristics
    @property
    def callbacks(self) -> list[int]: return self._callbacks

    def __str__(self) -> str:
        lines = [
            f"StartAddressOfRawData:  {hex_val(self._start_address_of_raw_data)}",
            f"EndAddressOfRawData:    {hex_val(self._end_address_of_raw_data)}",
            f"AddressOfIndex:         {hex_val(self._address_of_index)}",
            f"AddressOfCallBacks:     {hex_val(self._address_of_callbacks)}",
            f"SizeOfZeroFill:         {self._size_of_zero_fill}",
            f"Characteristics:        {hex_val(self._characteristics)}",
        ]
        if self._callbacks:
            lines.append(f"Callbacks ({len(self._callbacks)}):")
            for i, cb in enumerate(self._callbacks):
                lines.append(f"  [{i}] {hex_val(cb)}")
        return "\n".join(lines)
