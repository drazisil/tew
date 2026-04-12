"""PE Export Table parser."""

from __future__ import annotations
import struct
from typing import TYPE_CHECKING

from tew.helpers import hex_val, rva_to_offset, read_null_terminated

if TYPE_CHECKING:
    from tew.pe.section_header import SectionHeader


class ExportEntry:
    def __init__(self, ordinal: int, rva: int, name: str | None, forwarder: str | None) -> None:
        self._ordinal = ordinal
        self._rva = rva
        self._name = name
        self._forwarder = forwarder

    @property
    def ordinal(self) -> int: return self._ordinal
    @property
    def rva(self) -> int: return self._rva
    @property
    def name(self) -> str | None: return self._name
    @property
    def forwarder(self) -> str | None: return self._forwarder

    def __str__(self) -> str:
        name = self._name if self._name is not None else "(ordinal only)"
        target = f"-> {self._forwarder}" if self._forwarder else hex_val(self._rva)
        return f"[{self._ordinal}] {name} {target}"


class ExportTable:
    def __init__(
        self,
        data: bytes | bytearray,
        file_image: bytes | bytearray,
        sections: list["SectionHeader"],
        export_dir_rva: int,
        export_dir_size: int,
    ) -> None:
        self._dll_name = ""
        self._ordinal_base = 0
        self._time_date_stamp = 0
        self._entries: list[ExportEntry] = []

        if len(data) < 40:
            return

        self._time_date_stamp = struct.unpack_from("<I", data, 4)[0]
        name_rva = struct.unpack_from("<I", data, 12)[0]
        self._ordinal_base = struct.unpack_from("<I", data, 16)[0]
        number_of_functions = struct.unpack_from("<I", data, 20)[0]
        number_of_names = struct.unpack_from("<I", data, 24)[0]
        address_of_functions = struct.unpack_from("<I", data, 28)[0]
        address_of_names = struct.unpack_from("<I", data, 32)[0]
        address_of_name_ordinals = struct.unpack_from("<I", data, 36)[0]

        name_offset = rva_to_offset(name_rva, sections)
        if name_offset != -1:
            self._dll_name = read_null_terminated(file_image, name_offset)

        eat_offset = rva_to_offset(address_of_functions, sections)
        if eat_offset == -1:
            return

        npt_offset = rva_to_offset(address_of_names, sections) if number_of_names > 0 else -1
        ot_offset = rva_to_offset(address_of_name_ordinals, sections) if number_of_names > 0 else -1

        ordinal_to_name: dict[int, str] = {}
        if npt_offset != -1 and ot_offset != -1:
            for i in range(number_of_names):
                func_name_rva = struct.unpack_from("<I", file_image, npt_offset + i * 4)[0]
                ordinal_index = struct.unpack_from("<H", file_image, ot_offset + i * 2)[0]
                fn_offset = rva_to_offset(func_name_rva, sections)
                if fn_offset != -1:
                    ordinal_to_name[ordinal_index] = read_null_terminated(file_image, fn_offset)

        for i in range(number_of_functions):
            func_rva = struct.unpack_from("<I", file_image, eat_offset + i * 4)[0]
            if func_rva == 0:
                continue

            ordinal = self._ordinal_base + i
            name = ordinal_to_name.get(i)

            forwarder: str | None = None
            if export_dir_rva <= func_rva < export_dir_rva + export_dir_size:
                fwd_offset = rva_to_offset(func_rva, sections)
                if fwd_offset != -1:
                    forwarder = read_null_terminated(file_image, fwd_offset)

            self._entries.append(ExportEntry(ordinal, func_rva, name, forwarder))

    @property
    def dll_name(self) -> str: return self._dll_name
    @property
    def ordinal_base(self) -> int: return self._ordinal_base
    @property
    def time_date_stamp(self) -> int: return self._time_date_stamp
    @property
    def entries(self) -> list[ExportEntry]: return self._entries

    def __str__(self) -> str:
        if not self._entries:
            return "Export Table: empty"
        return (
            f"Export Table: {self._dll_name} ({len(self._entries)} exports, base {self._ordinal_base}):\n"
            + "\n".join(f"  {e}" for e in self._entries)
        )
