"""PE Load Configuration Directory parser."""

from __future__ import annotations
import struct

from tew.helpers import hex_val


def _read32(data: bytes | bytearray, offset: int, required_len: int) -> int:
    return struct.unpack_from("<I", data, offset)[0] if len(data) >= required_len else 0


def _read16(data: bytes | bytearray, offset: int, required_len: int) -> int:
    return struct.unpack_from("<H", data, offset)[0] if len(data) >= required_len else 0


def _read64(data: bytes | bytearray, offset: int, required_len: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0] if len(data) >= required_len else 0


class LoadConfigDirectory:
    def __init__(self, data: bytes | bytearray, is_pe32plus: bool) -> None:
        self._size = _read32(data, 0, 4)
        self._time_date_stamp = _read32(data, 4, 8)
        self._major_version = _read16(data, 8, 10)
        self._minor_version = _read16(data, 10, 12)
        self._global_flags_clear = _read32(data, 12, 16)
        self._global_flags_set = _read32(data, 16, 20)
        self._critical_section_default_timeout = _read32(data, 20, 24)

        if is_pe32plus:
            self._de_commit_free_block_threshold = _read64(data, 24, 32)
            self._de_commit_total_free_threshold = _read64(data, 32, 40)
            self._lock_prefix_table = _read64(data, 40, 48)
            self._maximum_allocation_size = _read64(data, 48, 56)
            self._virtual_memory_threshold = _read64(data, 56, 64)
            self._process_affinity_mask = _read64(data, 64, 72)
            self._process_heap_flags = _read32(data, 72, 76)
            self._csd_version = _read16(data, 76, 78)
            self._dependent_load_flags = _read16(data, 78, 80)
            self._edit_list = _read64(data, 80, 88)
            self._security_cookie = _read64(data, 88, 96)
            self._se_handler_table = _read64(data, 96, 104)
            self._se_handler_count = _read64(data, 104, 112)
            self._guard_cf_check_function_pointer = _read64(data, 112, 120)
            self._guard_cf_dispatch_function_pointer = _read64(data, 120, 128)
            self._guard_cf_function_table = _read64(data, 128, 136)
            self._guard_cf_function_count = _read64(data, 136, 144)
            self._guard_flags = _read32(data, 144, 148)
        else:
            self._de_commit_free_block_threshold = _read32(data, 24, 28)
            self._de_commit_total_free_threshold = _read32(data, 28, 32)
            self._lock_prefix_table = _read32(data, 32, 36)
            self._maximum_allocation_size = _read32(data, 36, 40)
            self._virtual_memory_threshold = _read32(data, 40, 44)
            self._process_affinity_mask = _read32(data, 44, 48)
            self._process_heap_flags = _read32(data, 48, 52)
            self._csd_version = _read16(data, 52, 54)
            self._dependent_load_flags = _read16(data, 54, 56)
            self._edit_list = _read32(data, 56, 60)
            self._security_cookie = _read32(data, 60, 64)
            self._se_handler_table = _read32(data, 64, 68)
            self._se_handler_count = _read32(data, 68, 72)
            self._guard_cf_check_function_pointer = _read32(data, 72, 76)
            self._guard_cf_dispatch_function_pointer = _read32(data, 76, 80)
            self._guard_cf_function_table = _read32(data, 80, 84)
            self._guard_cf_function_count = _read32(data, 84, 88)
            self._guard_flags = _read32(data, 88, 92)

    @property
    def size(self) -> int: return self._size
    @property
    def time_date_stamp(self) -> int: return self._time_date_stamp
    @property
    def major_version(self) -> int: return self._major_version
    @property
    def minor_version(self) -> int: return self._minor_version
    @property
    def global_flags_clear(self) -> int: return self._global_flags_clear
    @property
    def global_flags_set(self) -> int: return self._global_flags_set
    @property
    def critical_section_default_timeout(self) -> int: return self._critical_section_default_timeout
    @property
    def security_cookie(self) -> int: return self._security_cookie
    @property
    def se_handler_table(self) -> int: return self._se_handler_table
    @property
    def se_handler_count(self) -> int: return self._se_handler_count
    @property
    def guard_cf_check_function_pointer(self) -> int: return self._guard_cf_check_function_pointer
    @property
    def guard_cf_function_table(self) -> int: return self._guard_cf_function_table
    @property
    def guard_cf_function_count(self) -> int: return self._guard_cf_function_count
    @property
    def guard_flags(self) -> int: return self._guard_flags

    def __str__(self) -> str:
        rows = [
            f"Size:                      {hex_val(self._size)}",
            f"TimeDateStamp:             {hex_val(self._time_date_stamp)}",
            f"Version:                   {self._major_version}.{self._minor_version}",
            f"GlobalFlagsClear:          {hex_val(self._global_flags_clear)}",
            f"GlobalFlagsSet:            {hex_val(self._global_flags_set)}",
            f"CriticalSectionTimeout:    {self._critical_section_default_timeout}",
            f"SecurityCookie:            {hex_val(self._security_cookie)}",
        ]
        if self._se_handler_table != 0:
            rows.append(f"SEHandlerTable:            {hex_val(self._se_handler_table)}")
            rows.append(f"SEHandlerCount:            {self._se_handler_count}")
        if self._guard_cf_check_function_pointer != 0:
            rows.append(f"GuardCFCheckFunction:      {hex_val(self._guard_cf_check_function_pointer)}")
            rows.append(f"GuardCFFunctionTable:      {hex_val(self._guard_cf_function_table)}")
            rows.append(f"GuardCFFunctionCount:      {self._guard_cf_function_count}")
            rows.append(f"GuardFlags:                {hex_val(self._guard_flags)}")
        return "\n".join(rows)
