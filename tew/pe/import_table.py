"""PE Import Table parser."""

from __future__ import annotations
import struct
from typing import TYPE_CHECKING

from tew.helpers import hex_val, rva_to_offset, read_null_terminated

if TYPE_CHECKING:
    from tew.pe.section_header import SectionHeader


class ImportEntry:
    def __init__(
        self,
        ordinal: int | None,
        hint: int,
        name: str,
        iat_rva: int,
        iat_file_offset: int,
        iat_value: int,
    ) -> None:
        self._ordinal = ordinal
        self._hint = hint
        self._name = name
        self._iat_rva = iat_rva
        self._iat_file_offset = iat_file_offset
        self._iat_value = iat_value

    @property
    def ordinal(self) -> int | None: return self._ordinal
    @property
    def hint(self) -> int: return self._hint
    @property
    def name(self) -> str: return self._name
    @property
    def iat_rva(self) -> int: return self._iat_rva
    @property
    def iat_file_offset(self) -> int: return self._iat_file_offset
    @property
    def iat_value(self) -> int: return self._iat_value

    def __str__(self) -> str:
        if self._ordinal is not None:
            return f"{hex_val(self._iat_rva)}  {hex_val(self._iat_value)}  Ordinal #{self._ordinal}"
        return f"{hex_val(self._iat_rva)}  {hex_val(self._iat_value)}  {self._name} (hint: {self._hint})"


class ImportDescriptor:
    def __init__(
        self,
        dll_name: str,
        entries: list[ImportEntry],
        original_first_thunk: int,
        first_thunk: int,
    ) -> None:
        self._dll_name = dll_name
        self._entries = entries
        self._original_first_thunk = original_first_thunk
        self._first_thunk = first_thunk

    @property
    def dll_name(self) -> str: return self._dll_name
    @property
    def entries(self) -> list[ImportEntry]: return self._entries
    @property
    def original_first_thunk(self) -> int: return self._original_first_thunk
    @property
    def first_thunk(self) -> int: return self._first_thunk

    def __str__(self) -> str:
        header = f"{self._dll_name} ({len(self._entries)} imports)"
        entries = "\n".join(f"    [{i}] {e}" for i, e in enumerate(self._entries))
        return f"{header}\n{entries}"


def _read_hint_name(
    file_image: bytes | bytearray,
    sections: list["SectionHeader"],
    rva: int,
    iat_rva: int,
    iat_file_offset: int,
    iat_value: int,
    entries: list[ImportEntry],
) -> None:
    offset = rva_to_offset(rva, sections)
    if offset == -1:
        return
    hint = struct.unpack_from("<H", file_image, offset)[0]
    name = read_null_terminated(file_image, offset + 2)
    entries.append(ImportEntry(None, hint, name, iat_rva, iat_file_offset, iat_value))


def _parse_thunks(
    file_image: bytes | bytearray,
    sections: list["SectionHeader"],
    thunk_rva: int,
    first_thunk_rva: int,
    is_pe32plus: bool,
) -> list[ImportEntry]:
    entries: list[ImportEntry] = []
    thunk_file_offset = rva_to_offset(thunk_rva, sections)
    if thunk_file_offset == -1:
        return entries

    iat_base_file_offset = rva_to_offset(first_thunk_rva, sections)
    thunk_size = 8 if is_pe32plus else 4

    i = 0
    while True:
        offset = thunk_file_offset + i * thunk_size
        if offset + thunk_size > len(file_image):
            break
        iat_rva = first_thunk_rva + i * thunk_size
        iat_file_offset = iat_base_file_offset + i * thunk_size if iat_base_file_offset != -1 else -1

        if is_pe32plus:
            thunk = struct.unpack_from("<Q", file_image, offset)[0]
            if thunk == 0:
                break
            iat_value = struct.unpack_from("<Q", file_image, iat_file_offset)[0] if iat_file_offset != -1 else 0
            if thunk & 0x8000000000000000:
                ordinal = thunk & 0xFFFF
                entries.append(ImportEntry(ordinal, 0, f"Ordinal #{ordinal}", iat_rva, iat_file_offset, iat_value))
            else:
                _read_hint_name(file_image, sections, thunk, iat_rva, iat_file_offset, iat_value, entries)
        else:
            thunk = struct.unpack_from("<I", file_image, offset)[0]
            if thunk == 0:
                break
            iat_value = struct.unpack_from("<I", file_image, iat_file_offset)[0] if iat_file_offset != -1 else 0
            if thunk & 0x80000000:
                ordinal = thunk & 0xFFFF
                entries.append(ImportEntry(ordinal, 0, f"Ordinal #{ordinal}", iat_rva, iat_file_offset, iat_value))
            else:
                _read_hint_name(file_image, sections, thunk, iat_rva, iat_file_offset, iat_value, entries)
        i += 1

    return entries


class ImportTable:
    def __init__(
        self,
        data: bytes | bytearray,
        file_image: bytes | bytearray,
        sections: list["SectionHeader"],
        is_pe32plus: bool,
    ) -> None:
        self._descriptors: list[ImportDescriptor] = []
        if not data:
            return

        descriptor_size = 20
        i = 0
        while True:
            offset = i * descriptor_size
            if offset + descriptor_size > len(data):
                break

            original_first_thunk = struct.unpack_from("<I", data, offset)[0]
            name_rva = struct.unpack_from("<I", data, offset + 12)[0]
            first_thunk = struct.unpack_from("<I", data, offset + 16)[0]

            if original_first_thunk == 0 and name_rva == 0 and first_thunk == 0:
                break

            name_file_offset = rva_to_offset(name_rva, sections)
            dll_name = "<unknown>"
            if name_file_offset != -1:
                dll_name = read_null_terminated(file_image, name_file_offset)

            thunk_rva = original_first_thunk if original_first_thunk != 0 else first_thunk
            entries = _parse_thunks(file_image, sections, thunk_rva, first_thunk, is_pe32plus)

            self._descriptors.append(ImportDescriptor(dll_name, entries, original_first_thunk, first_thunk))
            i += 1

    @property
    def descriptors(self) -> list[ImportDescriptor]:
        return self._descriptors

    def __str__(self) -> str:
        if not self._descriptors:
            return "Import Table: empty"
        parts = [f"Import Table ({len(self._descriptors)} DLLs):"]
        for i, d in enumerate(self._descriptors):
            parts.append(f"  [{i}] {d}")
        return "\n\n".join(parts)
