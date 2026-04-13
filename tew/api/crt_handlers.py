"""register_crt_handlers — main entry point for all Win32/CRT stub registration.

Orchestrates registration of all per-DLL handler modules and writes the
fixed data region (command line, environment strings, thread sentinel) into
emulator memory before any game code executes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.loader.dll_loader import DLLLoader

from tew.api.win32_handlers import Win32Handlers
from tew.api._state import CRTState, THREAD_SENTINEL
from tew.logger import logger


def register_crt_handlers(
    stubs: Win32Handlers,
    memory: "Memory",
    dll_loader: Optional["DLLLoader"] = None,
) -> CRTState:
    """Register all default Win32 API stubs needed for MSVC CRT startup.

    Writes fixed-address data (command line, env strings, thread sentinel) into
    emulator memory, creates a CRTState instance, then delegates to per-DLL
    registration functions.

    Returns the CRTState so callers can pass it to patch_crt_internals.
    """
    state = CRTState()

    # ── Fixed data region writes ──────────────────────────────────────────────

    # ANSI command line string at 0x00210024
    cmd_line_addr = 0x00210024
    cmd_line_str  = b"MCity_d.exe\x00"
    for i, b in enumerate(cmd_line_str):
        memory.write8(cmd_line_addr + i, b)

    # Wide (UTF-16LE) command line string at 0x00210030
    cmd_line_w_addr = 0x00210030
    cmd_line_w      = "MCity_d.exe"
    for i, ch in enumerate(cmd_line_w):
        memory.write16(cmd_line_w_addr + i * 2, ord(ch))
    memory.write16(cmd_line_w_addr + len(cmd_line_w) * 2, 0)  # null terminator

    # Wide empty environment string at 0x00210048 (double-null = empty env block)
    env_str_addr = 0x00210048
    memory.write16(env_str_addr,     0)
    memory.write16(env_str_addr + 2, 0)

    # ANSI empty environment string at 0x0021004C
    env_str_a_addr = 0x0021004C
    memory.write8(env_str_a_addr,     0)
    memory.write8(env_str_a_addr + 1, 0)

    # ── Thread sentinel ───────────────────────────────────────────────────────
    # Written at THREAD_SENTINEL (0x001FE000): INT 0xFE; RET
    # so threads that return normally are caught and marked completed.
    memory.write8(THREAD_SENTINEL,     0xCD)  # INT opcode
    memory.write8(THREAD_SENTINEL + 1, 0xFE)  # interrupt vector 0xFE
    memory.write8(THREAD_SENTINEL + 2, 0xC3)  # RET
    stubs.patch_address(
        THREAD_SENTINEL,
        "_threadReturn",
        _make_thread_return_handler(state),
    )

    # ── Per-DLL handler registration ──────────────────────────────────────────
    # Import here to keep top-level imports free of circular dependencies and
    # to allow individual modules to be loaded/tested in isolation.
    from tew.api.kernel32_handlers import register_kernel32_handlers
    from tew.api.msvcrt_handlers import register_msvcrt_handlers
    from tew.api.user32_handlers import register_user32_gdi32_handlers
    from tew.api.oleaut32_handlers import register_oleaut32_ole32_handlers
    from tew.api.advapi32_handlers import register_advapi32_handlers
    from tew.api.d3d8_handlers import register_d3d8_handlers
    from tew.api.version_handlers import register_version_handlers
    from tew.api.wininet_handlers import register_wininet_handlers
    from tew.api.wsock32_handlers import register_wsock32_handlers

    register_kernel32_handlers(stubs, memory, state, dll_loader)
    register_msvcrt_handlers(stubs, memory, state)
    register_user32_gdi32_handlers(stubs, memory, state)
    register_oleaut32_ole32_handlers(stubs, memory, state)
    register_advapi32_handlers(stubs, memory, state)
    register_d3d8_handlers(stubs, memory)
    register_version_handlers(stubs, memory, state)
    register_wininet_handlers(stubs, memory, state)
    register_wsock32_handlers(stubs, memory, state)

    logger.info("handlers", f"Registered {stubs.count} Win32 stubs")

    return state


def patch_crt_internals(
    stubs: Win32Handlers,
    memory: "Memory",
    state: CRTState,
) -> None:
    """Patch CRT internal functions at hardcoded game addresses.

    Must be called AFTER sections are loaded into memory (the patch_address
    call overwrites real bytes in the loaded executable image).

    Requires the CRTState returned by register_crt_handlers so that patched
    handlers for allocators (e.g. __sbh_alloc_block) can use state.simple_alloc.
    """
    from tew.api.patch_internals import patch_crt_internals as _impl
    _impl(stubs, memory, state)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _make_thread_return_handler(state: CRTState):
    """Build the handler called when a spawned thread returns to THREAD_SENTINEL."""
    def _handler(cpu: "CPU") -> None:
        if 0 <= state.current_thread_idx < len(state.pending_threads):
            thread = state.pending_threads[state.current_thread_idx]
            logger.debug(
                "thread",
                f"Thread {thread.thread_id} returned normally",
            )
            thread.completed = True
        cpu.halted = True

    return _handler
