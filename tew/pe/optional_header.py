"""PE Optional Header parser (PE32 and PE32+)."""

from __future__ import annotations
import struct

from tew.helpers import hex_val
from tew.pe.data_directory import DataDirectory


class OptionalHeader:
    def __init__(self, data: bytes | bytearray) -> None:
        self._magic = struct.unpack_from("<H", data, 0)[0]
        self._major_linker_version = data[2]
        self._minor_linker_version = data[3]
        self._size_of_code = struct.unpack_from("<I", data, 4)[0]
        self._size_of_initialized_data = struct.unpack_from("<I", data, 8)[0]
        self._size_of_uninitialized_data = struct.unpack_from("<I", data, 12)[0]
        self._address_of_entry_point = struct.unpack_from("<I", data, 16)[0]
        self._base_of_code = struct.unpack_from("<I", data, 20)[0]

        if self.is_pe32_plus:
            self._base_of_data = 0
            self._image_base = struct.unpack_from("<Q", data, 24)[0]
        else:
            self._base_of_data = struct.unpack_from("<I", data, 24)[0]
            self._image_base = struct.unpack_from("<I", data, 28)[0]

        self._section_alignment = struct.unpack_from("<I", data, 32)[0]
        self._file_alignment = struct.unpack_from("<I", data, 36)[0]
        self._major_os_version = struct.unpack_from("<H", data, 40)[0]
        self._minor_os_version = struct.unpack_from("<H", data, 42)[0]
        self._major_image_version = struct.unpack_from("<H", data, 44)[0]
        self._minor_image_version = struct.unpack_from("<H", data, 46)[0]
        self._major_subsystem_version = struct.unpack_from("<H", data, 48)[0]
        self._minor_subsystem_version = struct.unpack_from("<H", data, 50)[0]
        self._win32_version_value = struct.unpack_from("<I", data, 52)[0]
        self._size_of_image = struct.unpack_from("<I", data, 56)[0]
        self._size_of_headers = struct.unpack_from("<I", data, 60)[0]
        self._check_sum = struct.unpack_from("<I", data, 64)[0]
        self._subsystem = struct.unpack_from("<H", data, 68)[0]
        self._dll_characteristics = struct.unpack_from("<H", data, 70)[0]

        if self.is_pe32_plus:
            self._size_of_stack_reserve = struct.unpack_from("<Q", data, 72)[0]
            self._size_of_stack_commit = struct.unpack_from("<Q", data, 80)[0]
            self._size_of_heap_reserve = struct.unpack_from("<Q", data, 88)[0]
            self._size_of_heap_commit = struct.unpack_from("<Q", data, 96)[0]
            self._loader_flags = struct.unpack_from("<I", data, 104)[0]
            self._number_of_rva_and_sizes = struct.unpack_from("<I", data, 108)[0]
        else:
            self._size_of_stack_reserve = struct.unpack_from("<I", data, 72)[0]
            self._size_of_stack_commit = struct.unpack_from("<I", data, 76)[0]
            self._size_of_heap_reserve = struct.unpack_from("<I", data, 80)[0]
            self._size_of_heap_commit = struct.unpack_from("<I", data, 84)[0]
            self._loader_flags = struct.unpack_from("<I", data, 88)[0]
            self._number_of_rva_and_sizes = struct.unpack_from("<I", data, 92)[0]

        dd_offset = 112 if self.is_pe32_plus else 96
        self._data_directories: list[DataDirectory] = []
        for i in range(self._number_of_rva_and_sizes):
            offset = dd_offset + i * DataDirectory.SIZE_OF
            self._data_directories.append(
                DataDirectory(data[offset : offset + DataDirectory.SIZE_OF], i)
            )

    @property
    def is_pe32_plus(self) -> bool:
        return self._magic == 0x20B

    @property
    def size_of(self) -> int:
        base = 112 if self.is_pe32_plus else 96
        return base + self._number_of_rva_and_sizes * DataDirectory.SIZE_OF

    @property
    def magic(self) -> int: return self._magic
    @property
    def major_linker_version(self) -> int: return self._major_linker_version
    @property
    def minor_linker_version(self) -> int: return self._minor_linker_version
    @property
    def size_of_code(self) -> int: return self._size_of_code
    @property
    def size_of_initialized_data(self) -> int: return self._size_of_initialized_data
    @property
    def size_of_uninitialized_data(self) -> int: return self._size_of_uninitialized_data
    @property
    def address_of_entry_point(self) -> int: return self._address_of_entry_point
    @property
    def base_of_code(self) -> int: return self._base_of_code
    @property
    def base_of_data(self) -> int: return self._base_of_data
    @property
    def image_base(self) -> int: return self._image_base
    @property
    def section_alignment(self) -> int: return self._section_alignment
    @property
    def file_alignment(self) -> int: return self._file_alignment
    @property
    def major_os_version(self) -> int: return self._major_os_version
    @property
    def minor_os_version(self) -> int: return self._minor_os_version
    @property
    def major_image_version(self) -> int: return self._major_image_version
    @property
    def minor_image_version(self) -> int: return self._minor_image_version
    @property
    def major_subsystem_version(self) -> int: return self._major_subsystem_version
    @property
    def minor_subsystem_version(self) -> int: return self._minor_subsystem_version
    @property
    def win32_version_value(self) -> int: return self._win32_version_value
    @property
    def size_of_image(self) -> int: return self._size_of_image
    @property
    def size_of_headers(self) -> int: return self._size_of_headers
    @property
    def check_sum(self) -> int: return self._check_sum
    @property
    def subsystem(self) -> int: return self._subsystem
    @property
    def dll_characteristics(self) -> int: return self._dll_characteristics
    @property
    def size_of_stack_reserve(self) -> int: return self._size_of_stack_reserve
    @property
    def size_of_stack_commit(self) -> int: return self._size_of_stack_commit
    @property
    def size_of_heap_reserve(self) -> int: return self._size_of_heap_reserve
    @property
    def size_of_heap_commit(self) -> int: return self._size_of_heap_commit
    @property
    def loader_flags(self) -> int: return self._loader_flags
    @property
    def number_of_rva_and_sizes(self) -> int: return self._number_of_rva_and_sizes
    @property
    def data_directories(self) -> list[DataDirectory]: return self._data_directories

    def __str__(self) -> str:
        fmt = "PE32+" if self.is_pe32_plus else "PE32"
        lines = [
            f"Magic:                        {hex_val(self._magic, 4)} ({fmt})",
            f"LinkerVersion:                {self._major_linker_version}.{self._minor_linker_version}",
            f"SizeOfCode:                   {hex_val(self._size_of_code)}",
            f"SizeOfInitializedData:        {hex_val(self._size_of_initialized_data)}",
            f"SizeOfUninitializedData:      {hex_val(self._size_of_uninitialized_data)}",
            f"AddressOfEntryPoint:          {hex_val(self._address_of_entry_point)}",
            f"BaseOfCode:                   {hex_val(self._base_of_code)}",
        ]
        if not self.is_pe32_plus:
            lines.append(f"BaseOfData:                   {hex_val(self._base_of_data)}")
        lines.extend([
            f"ImageBase:                    {hex_val(self._image_base)}",
            f"SectionAlignment:             {hex_val(self._section_alignment)}",
            f"FileAlignment:                {hex_val(self._file_alignment)}",
            f"OperatingSystemVersion:       {self._major_os_version}.{self._minor_os_version}",
            f"ImageVersion:                 {self._major_image_version}.{self._minor_image_version}",
            f"SubsystemVersion:             {self._major_subsystem_version}.{self._minor_subsystem_version}",
            f"Win32VersionValue:            {self._win32_version_value}",
            f"SizeOfImage:                  {hex_val(self._size_of_image)}",
            f"SizeOfHeaders:                {hex_val(self._size_of_headers)}",
            f"CheckSum:                     {hex_val(self._check_sum)}",
            f"Subsystem:                    {hex_val(self._subsystem, 4)}",
            f"DllCharacteristics:           {hex_val(self._dll_characteristics, 4)}",
            f"SizeOfStackReserve:           {hex_val(self._size_of_stack_reserve)}",
            f"SizeOfStackCommit:            {hex_val(self._size_of_stack_commit)}",
            f"SizeOfHeapReserve:            {hex_val(self._size_of_heap_reserve)}",
            f"SizeOfHeapCommit:             {hex_val(self._size_of_heap_commit)}",
            f"LoaderFlags:                  {hex_val(self._loader_flags)}",
            f"NumberOfRvaAndSizes:          {self._number_of_rva_and_sizes}",
            "",
            "Data Directories:",
            *[f"  [{i:2}] {dd}" for i, dd in enumerate(self._data_directories)],
        ])
        return "\n".join(lines)
