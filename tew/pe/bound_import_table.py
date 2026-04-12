"""PE Bound Import Table parser."""

from __future__ import annotations
import struct
from datetime import datetime, timezone

from tew.helpers import hex_val, read_null_terminated


class BoundForwarderRef:
    def __init__(self, time_date_stamp: int, module_name: str) -> None:
        self._time_date_stamp = time_date_stamp
        self._module_name = module_name

    @property
    def time_date_stamp(self) -> int: return self._time_date_stamp
    @property
    def module_name(self) -> str: return self._module_name

    def __str__(self) -> str:
        return f"-> {self._module_name} ({hex_val(self._time_date_stamp)})"


class BoundImportDescriptor:
    def __init__(
        self,
        time_date_stamp: int,
        module_name: str,
        forwarder_refs: list[BoundForwarderRef],
    ) -> None:
        self._time_date_stamp = time_date_stamp
        self._module_name = module_name
        self._forwarder_refs = forwarder_refs

    @property
    def time_date_stamp(self) -> int: return self._time_date_stamp
    @property
    def module_name(self) -> str: return self._module_name
    @property
    def forwarder_refs(self) -> list[BoundForwarderRef]: return self._forwarder_refs

    def __str__(self) -> str:
        dt = datetime.fromtimestamp(self._time_date_stamp, tz=timezone.utc).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
        s = f"{self._module_name} ({hex_val(self._time_date_stamp)} - {dt})"
        if self._forwarder_refs:
            s += "\n" + "\n".join(f"    {f}" for f in self._forwarder_refs)
        return s


class BoundImportTable:
    def __init__(self, data: bytes | bytearray) -> None:
        self._descriptors: list[BoundImportDescriptor] = []
        if not data:
            return

        offset = 0
        while offset + 8 <= len(data):
            time_date_stamp = struct.unpack_from("<I", data, offset)[0]
            offset_module_name = struct.unpack_from("<H", data, offset + 4)[0]
            number_of_forwarder_refs = struct.unpack_from("<H", data, offset + 6)[0]

            if time_date_stamp == 0 and offset_module_name == 0:
                break

            module_name = read_null_terminated(data, offset_module_name)

            forwarder_refs: list[BoundForwarderRef] = []
            for i in range(number_of_forwarder_refs):
                fwd_offset = offset + 8 + i * 8
                if fwd_offset + 8 > len(data):
                    break
                fwd_ts = struct.unpack_from("<I", data, fwd_offset)[0]
                fwd_name_offset = struct.unpack_from("<H", data, fwd_offset + 4)[0]
                fwd_name = read_null_terminated(data, fwd_name_offset)
                forwarder_refs.append(BoundForwarderRef(fwd_ts, fwd_name))

            self._descriptors.append(BoundImportDescriptor(time_date_stamp, module_name, forwarder_refs))
            offset += 8 + number_of_forwarder_refs * 8

    @property
    def descriptors(self) -> list[BoundImportDescriptor]: return self._descriptors

    def __str__(self) -> str:
        if not self._descriptors:
            return "Bound Import Table: empty"
        return (
            f"Bound Import Table ({len(self._descriptors)} entries):\n"
            + "\n".join(f"  [{i}] {d}" for i, d in enumerate(self._descriptors))
        )
