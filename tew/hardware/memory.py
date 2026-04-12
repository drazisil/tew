"""Virtual memory simulation for the x86-32 emulator."""

from __future__ import annotations
import struct


class Memory:
    """
    Flat byte-addressable virtual memory backed by a bytearray.
    Bounds-checked for 8/32-bit reads; raises on out-of-bounds access.
    """

    def __init__(self, size_bytes: int = 0x100000) -> None:
        self._buffer = bytearray(size_bytes)

    @property
    def size(self) -> int:
        return len(self._buffer)

    # ── 8-bit ──────────────────────────────────────────────────────────────

    def read8(self, addr: int) -> int:
        if addr < 0 or addr >= len(self._buffer):
            raise ValueError(
                f"read8: address 0x{addr & 0xFFFFFFFF:08x} outside bounds "
                f"[0, 0x{len(self._buffer):08x})"
            )
        return self._buffer[addr]

    def read_signed8(self, addr: int) -> int:
        val = self.read8(addr)
        return val if val < 0x80 else val - 0x100

    def write8(self, addr: int, val: int) -> None:
        self._buffer[addr] = val & 0xFF

    # ── 16-bit ─────────────────────────────────────────────────────────────

    def read16(self, addr: int) -> int:
        return struct.unpack_from("<H", self._buffer, addr)[0]

    def write16(self, addr: int, val: int) -> None:
        struct.pack_into("<H", self._buffer, addr, val & 0xFFFF)

    # ── 32-bit ─────────────────────────────────────────────────────────────

    def read32(self, addr: int) -> int:
        if addr < 0 or addr + 3 >= len(self._buffer):
            raise ValueError(
                f"read32: address 0x{addr & 0xFFFFFFFF:08x} outside bounds "
                f"[0, 0x{len(self._buffer):08x})"
            )
        return struct.unpack_from("<I", self._buffer, addr)[0]

    def read_signed32(self, addr: int) -> int:
        if addr < 0 or addr + 3 >= len(self._buffer):
            raise ValueError(
                f"read_signed32: address 0x{addr & 0xFFFFFFFF:08x} outside bounds "
                f"[0, 0x{len(self._buffer):08x})"
            )
        return struct.unpack_from("<i", self._buffer, addr)[0]

    def write32(self, addr: int, val: int) -> None:
        if addr < 0 or addr + 3 >= len(self._buffer):
            raise ValueError(
                f"write32: address 0x{addr & 0xFFFFFFFF:08x} outside bounds "
                f"[0, 0x{len(self._buffer):08x})"
            )
        struct.pack_into("<I", self._buffer, addr, val & 0xFFFFFFFF)

    # ── Bulk load ──────────────────────────────────────────────────────────

    def load(self, addr: int, data: bytes | bytearray) -> None:
        if addr + len(data) > len(self._buffer):
            raise ValueError(
                f"load: cannot fit {len(data)} bytes at 0x{addr & 0xFFFFFFFF:08x}, "
                f"would exceed bounds 0x{len(self._buffer):08x}"
            )
        self._buffer[addr : addr + len(data)] = data

    # ── Validity checks ────────────────────────────────────────────────────

    def is_valid_address(self, addr: int) -> bool:
        return 0 <= addr < len(self._buffer)

    def is_valid_range(self, addr: int, size: int) -> bool:
        return 0 <= addr and addr + size <= len(self._buffer)

    def get_bounds(self) -> dict[str, int]:
        return {
            "start": 0,
            "end": len(self._buffer) - 1,
            "size": len(self._buffer),
        }
