"""ZigMemory — drop-in replacement for Memory backed by libcpu.so (mem_* C ABI)."""

from __future__ import annotations

import ctypes
from pathlib import Path

_LIB_PATH = Path(__file__).parent.parent.parent / "cpu" / "zig-out" / "lib" / "libcpu.so"


def _load_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL(str(_LIB_PATH))

    _u8  = ctypes.c_uint8
    _i8  = ctypes.c_int8
    _u16 = ctypes.c_uint16
    _u32 = ctypes.c_uint32
    _i32 = ctypes.c_int32
    _sz  = ctypes.c_size_t
    _b   = ctypes.c_bool
    _p8  = ctypes.POINTER(_u8)

    lib.mem_read8.argtypes  = [_p8, _sz, _u32, ctypes.POINTER(_u8)]
    lib.mem_read8.restype   = _b
    lib.mem_read_signed8.argtypes = [_p8, _sz, _u32, ctypes.POINTER(_i8)]
    lib.mem_read_signed8.restype  = _b
    lib.mem_write8.argtypes = [_p8, _sz, _u32, _u8]
    lib.mem_write8.restype  = _b

    lib.mem_read16.argtypes  = [_p8, _sz, _u32, ctypes.POINTER(_u16)]
    lib.mem_read16.restype   = _b
    lib.mem_write16.argtypes = [_p8, _sz, _u32, _u16]
    lib.mem_write16.restype  = _b

    lib.mem_read32.argtypes  = [_p8, _sz, _u32, ctypes.POINTER(_u32)]
    lib.mem_read32.restype   = _b
    lib.mem_read_signed32.argtypes = [_p8, _sz, _u32, ctypes.POINTER(_i32)]
    lib.mem_read_signed32.restype  = _b
    lib.mem_write32.argtypes = [_p8, _sz, _u32, _u32]
    lib.mem_write32.restype  = _b

    lib.mem_load.argtypes = [_p8, _sz, _u32, _p8, _sz]
    lib.mem_load.restype  = _b

    lib.mem_is_valid_address.argtypes = [_sz, _u32]
    lib.mem_is_valid_address.restype  = _b
    lib.mem_is_valid_range.argtypes   = [_sz, _u32, _sz]
    lib.mem_is_valid_range.restype    = _b

    return lib


_lib = _load_lib()


class ZigMemory:
    """
    Flat byte-addressable virtual memory backed by a bytearray, with bounds
    checking and reads/writes delegated to libcpu.so's mem_* C ABI.
    Drop-in for Memory (tew/hardware/memory.py): same constructor signature,
    same method names, same `_buffer`/`size`.
    """

    def __init__(self, size_bytes: int = 0x100000) -> None:
        self._buffer = bytearray(size_bytes)
        # Pin the bytearray in a ctypes array so Zig can access it directly.
        # from_buffer keeps the bytearray alive via this reference -- same
        # idiom ZigCPU uses (tew/hardware/cpu_zig.py) to hand the identical
        # buffer to cpu_create, so ZigMemory and ZigCPU share one buffer.
        self._ctypes_buf = (ctypes.c_uint8 * size_bytes).from_buffer(self._buffer)
        self._ptr = ctypes.cast(self._ctypes_buf, ctypes.POINTER(ctypes.c_uint8))

    @property
    def size(self) -> int:
        return len(self._buffer)

    def _bounds_error(self, op: str, addr: int) -> ValueError:
        return ValueError(
            f"{op}: address 0x{addr & 0xFFFFFFFF:08x} outside bounds "
            f"[0, 0x{len(self._buffer):08x})"
        )

    # ── 8-bit ──────────────────────────────────────────────────────────────

    def read8(self, addr: int) -> int:
        out = ctypes.c_uint8()
        if addr < 0 or not _lib.mem_read8(self._ptr, self.size, addr, ctypes.byref(out)):
            raise self._bounds_error("read8", addr)
        return out.value

    def read_signed8(self, addr: int) -> int:
        out = ctypes.c_int8()
        if addr < 0 or not _lib.mem_read_signed8(self._ptr, self.size, addr, ctypes.byref(out)):
            raise self._bounds_error("read_signed8", addr)
        return out.value

    def write8(self, addr: int, val: int) -> None:
        if addr < 0 or not _lib.mem_write8(self._ptr, self.size, addr, val & 0xFF):
            raise self._bounds_error("write8", addr)

    # ── 16-bit ─────────────────────────────────────────────────────────────

    def read16(self, addr: int) -> int:
        out = ctypes.c_uint16()
        if addr < 0 or not _lib.mem_read16(self._ptr, self.size, addr, ctypes.byref(out)):
            raise self._bounds_error("read16", addr)
        return out.value

    def write16(self, addr: int, val: int) -> None:
        if addr < 0 or not _lib.mem_write16(self._ptr, self.size, addr, val & 0xFFFF):
            raise self._bounds_error("write16", addr)

    # ── 32-bit ─────────────────────────────────────────────────────────────

    def read32(self, addr: int) -> int:
        out = ctypes.c_uint32()
        if addr < 0 or not _lib.mem_read32(self._ptr, self.size, addr, ctypes.byref(out)):
            raise self._bounds_error("read32", addr)
        return out.value

    def read_signed32(self, addr: int) -> int:
        out = ctypes.c_int32()
        if addr < 0 or not _lib.mem_read_signed32(self._ptr, self.size, addr, ctypes.byref(out)):
            raise self._bounds_error("read_signed32", addr)
        return out.value

    def write32(self, addr: int, val: int) -> None:
        if addr < 0 or not _lib.mem_write32(self._ptr, self.size, addr, val & 0xFFFFFFFF):
            raise self._bounds_error("write32", addr)

    # ── Bulk load ──────────────────────────────────────────────────────────

    def load(self, addr: int, data: bytes | bytearray) -> None:
        buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        data_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))
        if addr < 0 or not _lib.mem_load(self._ptr, self.size, addr, data_ptr, len(data)):
            raise ValueError(
                f"load: cannot fit {len(data)} bytes at 0x{addr & 0xFFFFFFFF:08x}, "
                f"would exceed bounds 0x{len(self._buffer):08x}"
            )

    # ── Validity checks ────────────────────────────────────────────────────

    def is_valid_address(self, addr: int) -> bool:
        return addr >= 0 and bool(_lib.mem_is_valid_address(self.size, addr))

    def is_valid_range(self, addr: int, size: int) -> bool:
        return addr >= 0 and bool(_lib.mem_is_valid_range(self.size, addr, size))

    def get_bounds(self) -> dict[str, int]:
        return {
            "start": 0,
            "end": len(self._buffer) - 1,
            "size": len(self._buffer),
        }
