"""PE Exception Table parser."""

from __future__ import annotations
import struct

from tew.helpers import hex_val


class RuntimeFunction:
    SIZE_OF = 12

    def __init__(self, data: bytes | bytearray) -> None:
        self._begin_address = struct.unpack_from("<I", data, 0)[0]
        self._end_address = struct.unpack_from("<I", data, 4)[0]
        self._unwind_info_address = struct.unpack_from("<I", data, 8)[0]

    @property
    def begin_address(self) -> int: return self._begin_address
    @property
    def end_address(self) -> int: return self._end_address
    @property
    def unwind_info_address(self) -> int: return self._unwind_info_address
    @property
    def code_size(self) -> int: return self._end_address - self._begin_address

    def __str__(self) -> str:
        return (
            f"{hex_val(self._begin_address)}-{hex_val(self._end_address)} "
            f"({self.code_size} bytes) Unwind: {hex_val(self._unwind_info_address)}"
        )


class ExceptionTable:
    def __init__(self, data: bytes | bytearray) -> None:
        self._entries: list[RuntimeFunction] = []
        if not data:
            return
        count = len(data) // RuntimeFunction.SIZE_OF
        for i in range(count):
            offset = i * RuntimeFunction.SIZE_OF
            self._entries.append(
                RuntimeFunction(data[offset : offset + RuntimeFunction.SIZE_OF])
            )

    @property
    def entries(self) -> list[RuntimeFunction]: return self._entries

    def __str__(self) -> str:
        if not self._entries:
            return "Exception Table: empty"
        return (
            f"Exception Table ({len(self._entries)} entries):\n"
            + "\n".join(f"  [{i}] {e}" for i, e in enumerate(self._entries))
        )
