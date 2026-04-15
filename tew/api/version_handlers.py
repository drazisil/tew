"""version.dll handler registrations.

Win32 file-version API — GetFileVersionInfoSizeA, GetFileVersionInfoA,
and VerQueryValueA.

Parsing binary PE version resources (RT_VERSION) is not implemented; all
three functions report that no version information is available.  This is
a truthful response: the emulator does not have access to the version
resources embedded in the guest binaries, and the game must handle the
"not found" path gracefully.

Win32 reference:
    https://learn.microsoft.com/en-us/windows/win32/api/winver/
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall
from tew.api._state import CRTState, read_cstring
from tew.logger import logger


def register_version_handlers(
    stubs: Win32Handlers,
    memory: "Memory",
    state: CRTState,
) -> None:
    """Register all version.dll API handlers."""

    def _get_file_version_info_size_a(cpu: "CPU") -> None:
        lp_filename = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lpdw_handle = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        filename = read_cstring(lp_filename, memory) if lp_filename else ""
        logger.debug("handlers", f"GetFileVersionInfoSizeA({filename!r}) -> 0 (RT_VERSION parsing not implemented)")
        if lpdw_handle:
            memory.write32(lpdw_handle, 0)
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    def _get_file_version_info_a(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] GetFileVersionInfoA — RT_VERSION parsing not implemented, halting")
        cpu.halted = True

    def _ver_query_value_a(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] VerQueryValueA — RT_VERSION parsing not implemented, halting")
        cpu.halted = True

    stubs.register_handler("version.dll", "GetFileVersionInfoSizeA", _get_file_version_info_size_a)
    stubs.register_handler("version.dll", "GetFileVersionInfoA",     _get_file_version_info_a)
    stubs.register_handler("version.dll", "VerQueryValueA",          _ver_query_value_a)
