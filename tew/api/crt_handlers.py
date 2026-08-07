"""register_crt_handlers — main entry point for all Win32/CRT stub registration.

Orchestrates registration of all per-DLL handler modules and writes the
fixed data region (command line, environment strings, thread sentinel) into
emulator memory before any game code executes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory
    from tew.loader.dll_loader import DLLLoader
    from tew.api._state import EmulatorConfig

from tew.api.win32_handlers import Win32Handlers
from tew.api._state import CRTState, THREAD_SENTINEL
from tew.logger import logger


def register_crt_handlers(
    stubs: Win32Handlers,
    memory: "Memory",
    dll_loader: Optional["DLLLoader"] = None,
    config: Optional["EmulatorConfig"] = None,
    registry_dir: Optional[str] = None,
) -> CRTState:
    """Register all default Win32 API stubs needed for MSVC CRT startup.

    Writes fixed-address data (command line, env strings, thread sentinel) into
    emulator memory, creates a CRTState instance, then delegates to per-DLL
    registration functions.

    Returns the CRTState so callers can pass it to patch_crt_internals.
    """
    state = CRTState(config=config, registry_dir=registry_dir)

    # ── Fixed data region writes ──────────────────────────────────────────────
    # Generously-sized slots (not exact-length packed -- the previous
    # zero-slack layout couldn't grow the command line without overflowing
    # into the next field) starting at 0x00210024, matching
    # tew/api/kernel32_system.py's GetCommandLineA/W and
    # tew/api/msvcrt_handlers.py's __getmainargs address constants, which
    # must stay in sync with these.

    # ANSI command line string at 0x00210024 (64 bytes reserved).
    # -nomovie: MCity_d.exe's own command-line switch to skip the opening
    # movie -- confirmed in the binary (the string "nomovie" sits right
    # after "Skip the opening movie" in a switch-description table, also
    # shown by the in-game "Motor City Command Line Switches" dialog,
    # resource 104). Passed by default since the MAD movie/audio decoder
    # is the source of the known EIP=0x00a6bfcb fault documented in
    # tew/kernel/seh.py, and isn't needed for anything else.
    #
    # VERIFIED 2026-07-11 (merged temporarily with the dialog-click branch's
    # unattended-boot hooks for this live check, not otherwise related):
    # with -nomovie, that fault no longer occurs at all. The run gets much
    # further -- 192.7M steps, through DirectSound audio setup and real
    # thread/d3d8 activity in the main GetMessageA loop -- before halting
    # at a different, later, unrelated point: `abortmessage: mono.c:260`,
    # right after a failed `CreateFile("trace000.txt")` (looks like a
    # missing trace-log directory in this sandbox, not a MAD/movie issue,
    # and not investigated further here -- out of scope for this change).
    # -CaptureStdout: confirmed via Ghidra against MCity_d.exe's own
    # NFSArgs_ProcessArgs switch table (DAT_0126e060, row index 13) --
    # redirects WinMain's stdout (normally sent to the NUL device, see
    # DAT_0163d834's fopen("stdout.txt" | "NUL", "wt") branch) to a real
    # STDOUT.TXT file, so real puts()/printf() output the game makes is
    # observable instead of silently discarded.
    cmd_line_addr = 0x00210024
    cmd_line_str  = b"MCity_d.exe -nomovie -dbEnableLog -CaptureStdout\x00"
    for i, b in enumerate(cmd_line_str):
        memory.write8(cmd_line_addr + i, b)

    # Wide (UTF-16LE) command line string at 0x00210070 (128 bytes reserved)
    cmd_line_w_addr = 0x00210070
    cmd_line_w      = "MCity_d.exe -nomovie -dbEnableLog -CaptureStdout"
    for i, ch in enumerate(cmd_line_w):
        memory.write16(cmd_line_w_addr + i * 2, ord(ch))
    memory.write16(cmd_line_w_addr + len(cmd_line_w) * 2, 0)  # null terminator

    # Wide empty environment string at 0x002100F0 (double-null = empty env block)
    env_str_addr = 0x002100F0
    memory.write16(env_str_addr,     0)
    memory.write16(env_str_addr + 2, 0)

    # ANSI empty environment string at 0x002100F8
    env_str_a_addr = 0x002100F8
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
        _make_thread_return_handler(state, memory),
    )

    # ── SEH return sentinel ────────────────────────────────────────────────────
    # Lets tew.kernel.seh's dispatcher know precisely when an invoked
    # exception handler returns normally -- see that module's docstring.
    from tew.kernel.seh import install as _install_seh
    _install_seh(stubs, memory)

    # ── Per-DLL handler registration ──────────────────────────────────────────
    # Import here to keep top-level imports free of circular dependencies and
    # to allow individual modules to be loaded/tested in isolation.
    from tew.api.kernel32_handlers import register_kernel32_handlers
    from tew.api.kernel32_io import register_winmm_handlers
    from tew.api.msvcrt_handlers import register_msvcrt_handlers
    from tew.api.user32_handlers import register_user32_gdi32_handlers
    from tew.api.oleaut32_handlers import register_oleaut32_ole32_handlers
    from tew.api.advapi32_handlers import register_advapi32_handlers
    from tew.api.d3d8 import register_d3d8_handlers
    from tew.api.version_handlers import register_version_handlers
    from tew.api.wininet_handlers import register_wininet_handlers
    from tew.api.wsock32_handlers import register_wsock32_handlers
    from tew.api.dinput_handlers import register_dinput_handlers
    from tew.api.ifc22_handlers import register_ifc22_handlers
    from tew.api.dsound_handlers import register_dsound_handlers

    register_kernel32_handlers(stubs, memory, state, dll_loader)
    register_winmm_handlers(stubs, memory, state)
    register_msvcrt_handlers(stubs, memory, state)
    register_user32_gdi32_handlers(stubs, memory, state, dll_loader)
    register_oleaut32_ole32_handlers(stubs, memory, state, dll_loader)
    register_advapi32_handlers(stubs, memory, state)
    register_d3d8_handlers(stubs, memory, state)
    register_version_handlers(stubs, memory, state)
    register_wininet_handlers(stubs, memory, state)
    register_wsock32_handlers(stubs, memory, state)
    register_dinput_handlers(stubs, memory)
    register_ifc22_handlers(stubs, memory)
    register_dsound_handlers(stubs, memory, state)

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

# TEMP diagnostic, tid=1012 investigation (see memory/status.md): DAO's
# DllMain-calling worker thread reaches THREAD_SENTINEL ("returns normally")
# without ever hitting the nested-call sentinel _invoke_emulated_proc pushed
# for its call into DllMain -- i.e. it skips past that pushed return address
# entirely. tid numbering is deterministic run-to-run, so hardcoding the tid
# here is enough to isolate this thread's dump from the many other threads
# that complete normally every run. Discard once root-caused.
_THREAD_END_STACK_DUMP_TIDS: set[int] = {1012}


def _make_thread_return_handler(state: CRTState, memory: "Memory"):
    """Build the handler called when a spawned thread returns to THREAD_SENTINEL."""
    def _handler(cpu: "CPU") -> None:
        thread = state.scheduler.current_thread()
        logger.debug("thread", f"Thread {thread.thread_id} returned normally")
        if thread.thread_id in _THREAD_END_STACK_DUMP_TIDS:
            from tew.kernel.exception_diagnostics import diagnose_thread_end
            diagnose_thread_end(cpu, None, thread.thread_id)
        state.scheduler.mark_current_dead(cpu, memory)

    return _handler
