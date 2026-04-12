"""PE Delay Import Table parser."""

from __future__ import annotations
import struct
from typing import TYPE_CHECKING

from tew.helpers import rva_to_offset, read_null_terminated
from tew.pe.import_table import ImportEntry, _read_hint_name

if TYPE_CHECKING:
    from tew.pe.section_header import SectionHeader


class DelayImportDescriptor:
    def __init__(
        self,
        dll_name: str,
        attributes: int,
        module_handle: int,
        iat: int,
        int_rva: int,
        bound_iat: int,
        unload_iat: int,
        time_date_stamp: int,
        entries: list[ImportEntry],
    ) -> None:
        self._dll_name = dll_name
        self._attributes = attributes
        self._module_handle = module_handle
        self._iat = iat
        self._int = int_rva
        self._bound_iat = bound_iat
        self._unload_iat = unload_iat
        self._time_date_stamp = time_date_stamp
        self._entries = entries

    @property
    def attributes(self) -> int: return self._attributes
    @property
    def dll_name(self) -> str: return self._dll_name
    @property
    def entries(self) -> list[ImportEntry]: return self._entries

    def __str__(self) -> str:
        header = f"{self._dll_name} ({len(self._entries)} imports)"
        entries = "\n".join(f"    [{i}] {e}" for i, e in enumerate(self._entries))
        return f"{header}\n{entries}"


def _parse_delay_thunks(
    file_image: bytes | bytearray,
    sections: list["SectionHeader"],
    int_rva: int,
    first_thunk_rva: int,
    is_pe32plus: bool,
) -> list[ImportEntry]:
    entries: list[ImportEntry] = []
    int_offset = rva_to_offset(int_rva, sections)
    if int_offset == -1:
        return entries

    iat_base_offset = rva_to_offset(first_thunk_rva, sections)
    thunk_size = 8 if is_pe32plus else 4

    i = 0
    while True:
        offset = int_offset + i * thunk_size
        if offset + thunk_size > len(file_image):
            break
        iat_rva = first_thunk_rva + i * thunk_size
        iat_file_offset = iat_base_offset + i * thunk_size if iat_base_offset != -1 else -1

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


class DelayImportTable:
    def __init__(
        self,
        data: bytes | bytearray,
        file_image: bytes | bytearray,
        sections: list["SectionHeader"],
        is_pe32plus: bool,
    ) -> None:
        self._descriptors: list[DelayImportDescriptor] = []
        if not data:
            return

        descriptor_size = 32
        i = 0
        while True:
            offset = i * descriptor_size
            if offset + descriptor_size > len(data):
                break

            attributes = struct.unpack_from("<I", data, offset)[0]
            dll_name_rva = struct.unpack_from("<I", data, offset + 4)[0]
            module_handle = struct.unpack_from("<I", data, offset + 8)[0]
            iat_rva = struct.unpack_from("<I", data, offset + 12)[0]
            int_rva = struct.unpack_from("<I", data, offset + 16)[0]
            bound_iat_rva = struct.unpack_from("<I", data, offset + 20)[0]
            unload_iat_rva = struct.unpack_from("<I", data, offset + 24)[0]
            time_date_stamp = struct.unpack_from("<I", data, offset + 28)[0]

            if dll_name_rva == 0 and int_rva == 0 and iat_rva == 0:
                break

            name_offset = rva_to_offset(dll_name_rva, sections)
            dll_name = "<unknown>"
            if name_offset != -1:
                dll_name = read_null_terminated(file_image, name_offset)

            entries = _parse_delay_thunks(file_image, sections, int_rva, iat_rva, is_pe32plus)
            self._descriptors.append(
                DelayImportDescriptor(
                    dll_name, attributes, module_handle, iat_rva, int_rva,
                    bound_iat_rva, unload_iat_rva, time_date_stamp, entries,
                )
            )
            i += 1

    @property
    def descriptors(self) -> list[DelayImportDescriptor]: return self._descriptors

    def __str__(self) -> str:
        if not self._descriptors:
            return "Delay Import Table: empty"
        parts = [f"Delay Import Table ({len(self._descriptors)} DLLs):"]
        for i, d in enumerate(self._descriptors):
            parts.append(f"  [{i}] {d}")
        return "\n\n".join(parts)
