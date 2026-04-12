"""PE Debug Directory parser."""

from __future__ import annotations
import struct

from tew.helpers import hex_val, read_null_terminated

DEBUG_TYPE_NAMES: dict[int, str] = {
    0: "UNKNOWN", 1: "COFF", 2: "CODEVIEW", 3: "FPO", 4: "MISC",
    5: "EXCEPTION", 6: "FIXUP", 7: "OMAP_TO_SRC", 8: "OMAP_FROM_SRC",
    9: "BORLAND", 10: "RESERVED10", 11: "CLSID", 12: "VC_FEATURE",
    13: "POGO", 14: "ILTCG", 15: "MPX", 16: "REPRO", 20: "EX_DLLCHARACTERISTICS",
}


class DebugDirectoryEntry:
    SIZE_OF = 28

    def __init__(self, data: bytes | bytearray, file_image: bytes | bytearray) -> None:
        self._characteristics = struct.unpack_from("<I", data, 0)[0]
        self._time_date_stamp = struct.unpack_from("<I", data, 4)[0]
        self._major_version = struct.unpack_from("<H", data, 8)[0]
        self._minor_version = struct.unpack_from("<H", data, 10)[0]
        self._type = struct.unpack_from("<I", data, 12)[0]
        self._size_of_data = struct.unpack_from("<I", data, 16)[0]
        self._address_of_raw_data = struct.unpack_from("<I", data, 20)[0]
        self._pointer_to_raw_data = struct.unpack_from("<I", data, 24)[0]
        self._pdb_path: str | None = None
        self._pdb_guid: str | None = None
        self._pdb_age: int | None = None

        if self._type == 2 and self._pointer_to_raw_data > 0 and self._size_of_data >= 24:
            cv_offset = self._pointer_to_raw_data
            if cv_offset + self._size_of_data <= len(file_image):
                sig = struct.unpack_from("<I", file_image, cv_offset)[0]
                if sig == 0x53445352:  # RSDS = PDB 7.0
                    guid_bytes = file_image[cv_offset + 4 : cv_offset + 20]
                    self._pdb_guid = self._format_guid(guid_bytes)
                    self._pdb_age = struct.unpack_from("<I", file_image, cv_offset + 20)[0]
                    self._pdb_path = read_null_terminated(file_image, cv_offset + 24)
                elif sig == 0x3031424E:  # NB10 = PDB 2.0
                    self._pdb_age = struct.unpack_from("<I", file_image, cv_offset + 8)[0]
                    self._pdb_path = read_null_terminated(file_image, cv_offset + 16)

    @staticmethod
    def _format_guid(b: bytes | bytearray) -> str:
        d1 = struct.unpack_from("<I", b, 0)[0]
        d2 = struct.unpack_from("<H", b, 4)[0]
        d3 = struct.unpack_from("<H", b, 6)[0]
        d4 = b[8:10].hex()
        d5 = b[10:16].hex()
        return f"{{{d1:08X}-{d2:04X}-{d3:04X}-{d4.upper()}-{d5.upper()}}}"

    @property
    def characteristics(self) -> int: return self._characteristics
    @property
    def time_date_stamp(self) -> int: return self._time_date_stamp
    @property
    def major_version(self) -> int: return self._major_version
    @property
    def minor_version(self) -> int: return self._minor_version
    @property
    def type(self) -> int: return self._type
    @property
    def type_name(self) -> str:
        return DEBUG_TYPE_NAMES.get(self._type, f"UNKNOWN({self._type})")
    @property
    def size_of_data(self) -> int: return self._size_of_data
    @property
    def address_of_raw_data(self) -> int: return self._address_of_raw_data
    @property
    def pointer_to_raw_data(self) -> int: return self._pointer_to_raw_data
    @property
    def pdb_path(self) -> str | None: return self._pdb_path
    @property
    def pdb_guid(self) -> str | None: return self._pdb_guid
    @property
    def pdb_age(self) -> int | None: return self._pdb_age

    def __str__(self) -> str:
        s = (
            f"{self.type_name}: RVA={hex_val(self._address_of_raw_data)} "
            f"FilePtr={hex_val(self._pointer_to_raw_data)} Size={hex_val(self._size_of_data)}"
        )
        if self._pdb_path:
            s += f"\n  PDB: {self._pdb_path}"
            if self._pdb_guid:
                s += f"\n  GUID: {self._pdb_guid}"
            if self._pdb_age is not None:
                s += f"  Age: {self._pdb_age}"
        return s


class DebugDirectory:
    def __init__(self, data: bytes | bytearray, file_image: bytes | bytearray) -> None:
        self._entries: list[DebugDirectoryEntry] = []
        if not data:
            return
        count = len(data) // DebugDirectoryEntry.SIZE_OF
        for i in range(count):
            offset = i * DebugDirectoryEntry.SIZE_OF
            self._entries.append(
                DebugDirectoryEntry(
                    data[offset : offset + DebugDirectoryEntry.SIZE_OF], file_image
                )
            )

    @property
    def entries(self) -> list[DebugDirectoryEntry]: return self._entries

    def __str__(self) -> str:
        if not self._entries:
            return "Debug Directory: empty"
        return (
            f"Debug Directory ({len(self._entries)} entries):\n"
            + "\n".join(f"  [{i}] {e}" for i, e in enumerate(self._entries))
        )
