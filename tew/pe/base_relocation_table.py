"""PE Base Relocation Table parser."""

from __future__ import annotations
import struct

from tew.helpers import hex_val

RELOC_TYPE_NAMES: dict[int, str] = {
    0: "ABS", 1: "HIGH", 2: "LOW", 3: "HIGHLOW", 4: "HIGHADJ",
    5: "MIPS_JMPADDR", 9: "MIPS_JMPADDR16", 10: "DIR64",
}


class RelocationEntry:
    def __init__(self, type_: int, offset: int) -> None:
        self._type = type_
        self._offset = offset

    @property
    def type(self) -> int: return self._type
    @property
    def offset(self) -> int: return self._offset
    @property
    def type_name(self) -> str:
        return RELOC_TYPE_NAMES.get(self._type, f"UNKNOWN({self._type})")

    def __str__(self) -> str:
        return f"{self.type_name} +{hex_val(self._offset, 3)}"


class RelocationBlock:
    def __init__(self, page_rva: int, entries: list[RelocationEntry]) -> None:
        self._page_rva = page_rva
        self._entries = entries

    @property
    def page_rva(self) -> int: return self._page_rva
    @property
    def entries(self) -> list[RelocationEntry]: return self._entries

    def __str__(self) -> str:
        return (
            f"Page {hex_val(self._page_rva)} ({len(self._entries)} entries):\n"
            + "\n".join(f"  {e}" for e in self._entries)
        )


class BaseRelocationTable:
    def __init__(self, data: bytes | bytearray) -> None:
        self._blocks: list[RelocationBlock] = []
        self._total_entries = 0

        if not data:
            return

        offset = 0
        while offset + 8 <= len(data):
            page_rva = struct.unpack_from("<I", data, offset)[0]
            block_size = struct.unpack_from("<I", data, offset + 4)[0]

            if block_size == 0 or block_size < 8:
                break

            entry_count = (block_size - 8) // 2
            entries: list[RelocationEntry] = []

            for i in range(entry_count):
                entry_offset = offset + 8 + i * 2
                if entry_offset + 2 > len(data):
                    break
                value = struct.unpack_from("<H", data, entry_offset)[0]
                type_ = (value >> 12) & 0xF
                page_offset = value & 0xFFF
                if type_ != 0:  # type 0 (ABS) is padding
                    entries.append(RelocationEntry(type_, page_offset))

            self._blocks.append(RelocationBlock(page_rva, entries))
            self._total_entries += len(entries)
            offset += block_size

    @property
    def blocks(self) -> list[RelocationBlock]: return self._blocks
    @property
    def total_entries(self) -> int: return self._total_entries

    def __str__(self) -> str:
        if not self._blocks:
            return "Base Relocation Table: empty"
        return (
            f"Base Relocation Table ({len(self._blocks)} pages, {self._total_entries} relocations):\n"
            + "\n\n".join(f"  {b}" for b in self._blocks)
        )
