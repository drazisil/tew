"""kernel32.dll system handlers — version, time, process info, env, Sleep scheduler."""

from __future__ import annotations

import time as _time_module
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers

from tew.hardware.cpu_zig import EAX, EBX, ECX, EDX, ESP, EBP, ESI, EDI
from tew.api.win32_handlers import cleanup_stdcall
from tew.api._state import CRTState, FileHandleEntry, TEB_BASE
from tew.api.win32_errors import Win32Error
from tew.logger import logger

# This emulator only ever has one fake machine -- an arbitrary but stable
# Windows XP-era NetBIOS name, matching the "fake but plausible" convention
# already used for the fake PID etc. Real DAO/Jet locking uses the computer
# name (combined with username) to build the .ldb lock-owner identity, but
# any consistent, well-formed name works for that purpose.
_COMPUTER_NAME = "MCITY-PC"

# QPC reports 1 MHz so counter values stay in easy integer range.
_QPC_FREQ: int = 1_000_000


def _fire_due_timers(cpu: "CPU", memory: "Memory", state: CRTState) -> None:
    """Invoke any timer callbacks whose due_at <= virtual_ticks_ms."""
    from tew.api.win32_handlers import pending_timers, _TIME_CALLBACK_EVENT_SET
    if not pending_timers:
        return
    due = [t for t in list(pending_timers.values()) if t.due_at <= state.virtual_ticks_ms]
    if not due:
        return
    from tew.api.user32_handlers import _invoke_emulated_proc, _get_dialog_sentinel
    from tew.api._state import EventHandle
    sentinel = _get_dialog_sentinel(state, memory)
    for timer in due:
        if timer.fu_event & _TIME_CALLBACK_EVENT_SET:
            obj = state.kernel_handle_map.get(timer.cb_addr)
            if isinstance(obj, EventHandle):
                obj.signaled = True
                state.scheduler.unblock_handle(timer.cb_addr)
        elif timer.cb_addr != 0:
            _invoke_emulated_proc(cpu, memory, timer.cb_addr, [timer.id, 0, timer.dw_user, 0, 0], sentinel)
        if timer.period_ms > 0:
            timer.due_at += timer.period_ms
        else:
            pending_timers.pop(timer.id, None)



def register_kernel32_system_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: CRTState,
) -> None:
    """Register version, time, process info, environment, and Sleep handlers."""

    # ── Version ──────────────────────────────────────────────────────────────

    def _get_version(cpu: "CPU") -> None:
        cpu.regs[EAX] = (2600 << 16) | (1 << 8) | 5  # WinXP 5.1.2600

    def _get_version_ex_a(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        memory.write32(lp + 4,  5)
        memory.write32(lp + 8,  1)
        memory.write32(lp + 12, 2600)
        memory.write32(lp + 16, 2)
        sp2 = b"Service Pack 2"
        for i, b in enumerate(sp2):
            memory.write8(lp + 20 + i, b)
        memory.write8(lp + 20 + len(sp2), 0)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    def _get_version_ex_w(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        memory.write32(lp + 4,  5)
        memory.write32(lp + 8,  1)
        memory.write32(lp + 12, 2600)
        memory.write32(lp + 16, 2)
        sp2 = "Service Pack 2"
        for i, ch in enumerate(sp2):
            memory.write16(lp + 20 + i * 2, ord(ch))
        memory.write16(lp + 20 + len(sp2) * 2, 0)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GetVersion",    _get_version)
    stubs.register_handler("kernel32.dll", "GetVersionExA", _get_version_ex_a)
    stubs.register_handler("kernel32.dll", "GetVersionExW", _get_version_ex_w)

    # ── Command line / startup ────────────────────────────────────────────────

    def _get_cmd_a(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x00210024

    def _get_cmd_w(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x00210070

    def _get_startup_a(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        for i in range(0, 68, 4):
            memory.write32(lp + i, 0)
        memory.write32(lp, 68)
        cleanup_stdcall(cpu, memory, 4)

    def _get_startup_w(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        for i in range(0, 68, 4):
            memory.write32(lp + i, 0)
        memory.write32(lp, 68)
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GetCommandLineA", _get_cmd_a)
    stubs.register_handler("kernel32.dll", "GetCommandLineW", _get_cmd_w)
    stubs.register_handler("kernel32.dll", "GetStartupInfoA", _get_startup_a)
    stubs.register_handler("kernel32.dll", "GetStartupInfoW", _get_startup_w)

    # ── Process / thread identity ─────────────────────────────────────────────

    def _get_current_process(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xFFFFFFFF

    def _get_current_process_id(cpu: "CPU") -> None:
        # Must match GetWindowThreadProcessId's hardcoded fake PID
        # (user32_handlers.py, "our fake PID") -- this emulator only ever
        # has one fake process, so both need to agree on its ID or any
        # code comparing them (e.g. "is this window mine?") never matches.
        cpu.regs[EAX] = 1

    def _get_current_thread_id(cpu: "CPU") -> None:
        cpu.regs[EAX] = state.tls_current_thread_id()

    def _get_current_thread(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xFFFFFFFE

    def _get_computer_name_a(cpu: "CPU") -> None:
        lp_buffer = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_size   = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        buf_capacity = memory.read32(lp_size) if lp_size else 0
        # Real GetComputerNameA: on success, *lpnSize is set to the char
        # count copied (NOT including the null terminator); on failure
        # (buffer too small), it's set to the required size (including it).
        if lp_buffer and lp_size and buf_capacity >= len(_COMPUTER_NAME) + 1:
            for i, ch in enumerate(_COMPUTER_NAME):
                memory.write8(lp_buffer + i, ord(ch))
            memory.write8(lp_buffer + len(_COMPUTER_NAME), 0)
            memory.write32(lp_size, len(_COMPUTER_NAME))
            cpu.regs[EAX] = 1
            logger.debug("handlers", f'GetComputerNameA() -> "{_COMPUTER_NAME}"')
        else:
            if lp_size:
                memory.write32(lp_size, len(_COMPUTER_NAME) + 1)
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_BUFFER_OVERFLOW))
            cpu.regs[EAX] = 0
            logger.warn("handlers",
                f'GetComputerNameA() -> FALSE (buffer too small: have {buf_capacity}, '
                f'need {len(_COMPUTER_NAME) + 1})')
        cleanup_stdcall(cpu, memory, 8)

    def _get_computer_name_w(cpu: "CPU") -> None:
        lp_buffer = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_size   = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        buf_capacity = memory.read32(lp_size) if lp_size else 0
        if lp_buffer and lp_size and buf_capacity >= len(_COMPUTER_NAME) + 1:
            for i, ch in enumerate(_COMPUTER_NAME):
                memory.write16(lp_buffer + i * 2, ord(ch))
            memory.write16(lp_buffer + len(_COMPUTER_NAME) * 2, 0)
            memory.write32(lp_size, len(_COMPUTER_NAME))
            cpu.regs[EAX] = 1
            logger.debug("handlers", f'GetComputerNameW() -> "{_COMPUTER_NAME}"')
        else:
            if lp_size:
                memory.write32(lp_size, len(_COMPUTER_NAME) + 1)
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_BUFFER_OVERFLOW))
            cpu.regs[EAX] = 0
            logger.warn("handlers",
                f'GetComputerNameW() -> FALSE (buffer too small: have {buf_capacity}, '
                f'need {len(_COMPUTER_NAME) + 1})')
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("kernel32.dll", "GetCurrentProcess",   _get_current_process)
    stubs.register_handler("kernel32.dll", "GetCurrentProcessId", _get_current_process_id)
    stubs.register_handler("kernel32.dll", "GetCurrentThreadId",  _get_current_thread_id)
    stubs.register_handler("kernel32.dll", "GetCurrentThread",    _get_current_thread)
    stubs.register_handler("kernel32.dll", "GetComputerNameA",    _get_computer_name_a)
    stubs.register_handler("kernel32.dll", "GetComputerNameW",    _get_computer_name_w)

    # ── Error / tick / time ───────────────────────────────────────────────────

    def _get_last_error(cpu: "CPU") -> None:
        cpu.regs[EAX] = memory.read32(TEB_BASE + 0x34)
        cleanup_stdcall(cpu, memory, 0)

    def _set_last_error(cpu: "CPU") -> None:
        err = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if err == 6:  # ERROR_INVALID_HANDLE
            ret = memory.read32(cpu.regs[ESP] & 0xFFFFFFFF)
            logger.debug("handlers", f"SetLastError(INVALID_HANDLE) ret=0x{ret:x}")
        memory.write32(TEB_BASE + 0x34, err)
        cleanup_stdcall(cpu, memory, 4)

    # Monotonic start time captured at registration so tick counts are relative.
    _start_time = _time_module.monotonic()

    def _get_tick_count(cpu: "CPU") -> None:
        """GetTickCount() -> DWORD  (milliseconds since emulator start).

        Returns the virtual tick clock, which advances by dwMilliseconds per
        Sleep/SleepEx call rather than by real wall time.  This matches the
        emulated binary's expectation: GetTickCount should advance in step
        with emulated execution, not with Python wall time.
        The return value wraps after ~49.7 days.
        """
        cpu.regs[EAX] = state.virtual_ticks_ms & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 0)

    def _query_performance_counter(cpu: "CPU") -> None:
        """QueryPerformanceCounter(LARGE_INTEGER* lpPerformanceCount) -> BOOL."""
        p = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if p:
            us = int((_time_module.monotonic() - _start_time) * _QPC_FREQ)
            memory.write32(p,     us & 0xFFFFFFFF)
            memory.write32(p + 4, (us >> 32) & 0xFFFFFFFF)
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 4)

    def _query_performance_frequency(cpu: "CPU") -> None:
        """QueryPerformanceFrequency(LARGE_INTEGER* lpFrequency) -> BOOL."""
        p = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if p:
            memory.write32(p,     _QPC_FREQ & 0xFFFFFFFF)
            memory.write32(p + 4, 0)
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GetLastError",              _get_last_error)
    stubs.register_handler("kernel32.dll", "SetLastError",              _set_last_error)
    stubs.register_handler("kernel32.dll", "GetTickCount",              _get_tick_count)
    stubs.register_handler("kernel32.dll", "QueryPerformanceCounter",   _query_performance_counter)
    stubs.register_handler("kernel32.dll", "QueryPerformanceFrequency", _query_performance_frequency)

    # ── System info ───────────────────────────────────────────────────────────

    def _get_system_info(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        for i in range(0, 36, 4):
            memory.write32(ptr + i, 0)
        memory.write16(ptr + 0,  0)           # PROCESSOR_ARCHITECTURE_INTEL
        memory.write32(ptr + 4,  4096)
        memory.write32(ptr + 8,  0x00010000)
        memory.write32(ptr + 12, 0x7FFEFFFF)
        memory.write32(ptr + 16, 1)
        memory.write32(ptr + 20, 1)
        memory.write32(ptr + 24, 586)         # Pentium
        memory.write32(ptr + 28, 0x00010000)  # 64KB granularity
        memory.write16(ptr + 32, 6)
        memory.write16(ptr + 34, 0)
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GetSystemInfo", _get_system_info)

    # ── Exit / debug ──────────────────────────────────────────────────────────

    def _exit_process(cpu: "CPU") -> None:
        code = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        logger.info("handlers", f"ExitProcess({code})")
        cpu.halted = True
        cpu.fatal_halt = True

    def _is_debugger_present(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0

    def _is_processor_feature_present(cpu: "CPU") -> None:
        feature = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        supported = feature in (2, 3, 8)  # CMPXCHG8B, MMX, RDTSC
        cpu.regs[EAX] = 1 if supported else 0
        cleanup_stdcall(cpu, memory, 4)

    def _set_unhandled_ex(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    def _unhandled_ex(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "ExitProcess",                  _exit_process)
    stubs.register_handler("kernel32.dll", "IsDebuggerPresent",            _is_debugger_present)
    stubs.register_handler("kernel32.dll", "IsProcessorFeaturePresent",    _is_processor_feature_present)
    stubs.register_handler("kernel32.dll", "SetUnhandledExceptionFilter",  _set_unhandled_ex)
    stubs.register_handler("kernel32.dll", "UnhandledExceptionFilter",     _unhandled_ex)

    # ── Environment strings ───────────────────────────────────────────────────

    def _get_env_strings_w(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x002100F0

    def _free_env_strings_w(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    def _get_env_strings(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x002100F8

    def _free_env_strings_a(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GetEnvironmentStringsW",  _get_env_strings_w)
    stubs.register_handler("kernel32.dll", "FreeEnvironmentStringsW", _free_env_strings_w)
    stubs.register_handler("kernel32.dll", "GetEnvironmentStrings",   _get_env_strings)
    stubs.register_handler("kernel32.dll", "FreeEnvironmentStringsA", _free_env_strings_a)

    # ── Standard handles / file type ──────────────────────────────────────────

    def _get_std_handle(cpu: "CPU") -> None:
        n = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        handle = (0x00000100 + (n & 0xFF)) & 0xFFFFFFFF
        if handle not in state.file_handle_map:
            if n == 0xFFFFFFF6:  # STD_INPUT
                state.file_handle_map[handle] = FileHandleEntry(
                    path='<stdin>', data=b'', position=0, writable=False, fd=0)
            elif n == 0xFFFFFFF5:  # STD_OUTPUT
                state.file_handle_map[handle] = FileHandleEntry(
                    path='<stdout>', data=b'', position=0, writable=True, fd=1)
            elif n == 0xFFFFFFF4:  # STD_ERROR
                state.file_handle_map[handle] = FileHandleEntry(
                    path='<stderr>', data=b'', position=0, writable=True, fd=2)
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, 4)

    def _get_file_type(cpu: "CPU") -> None:
        hf = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(hf)
        if entry is None:
            logger.error("handlers",
                f"[UNIMPLEMENTED] GetFileType: unknown handle 0x{hf:x} — halting")
            cpu.halted = True
            cpu.fatal_halt = True
            return
        # FILE_TYPE_CHAR(2) for std handles/NUL device, FILE_TYPE_DISK(1) for
        # real files. CORRECTED 2026-08-07: this used to key off `entry.fd is
        # not None` -- exactly backwards, since every real disk file (read-
        # write, or write-only) also has a live fd, so it always reported
        # FILE_TYPE_CHAR for real files and FILE_TYPE_DISK only for the
        # read-only-with-cached-data case (entry.fd is None there). Confirmed
        # live as the true root cause of msjet35.dll's "unrecognized database
        # format" on a byte-perfect, correctly-signed Tmp.MDB: real Jet calls
        # GetFileType(handle) right after CreateFile and fails immediately
        # with error -0x404 if it isn't exactly FILE_TYPE_DISK -- before ever
        # calling ReadFile or checking the "Standard Jet DB" signature. Std
        # handles use sentinel paths ('<stdin>' etc, see _get_std_handle
        # above); the NUL device uses the real path "/dev/null" -- both are
        # genuinely FILE_TYPE_CHAR on real Windows too.
        is_char_device = entry.path.startswith("<") or entry.path == "/dev/null"
        cpu.regs[EAX] = 2 if is_char_device else 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GetStdHandle", _get_std_handle)
    stubs.register_handler("kernel32.dll", "GetFileType",  _get_file_type)

    # ── Pointer encode/decode (identity) ─────────────────────────────────────

    def _encode_ptr(cpu: "CPU") -> None:
        cpu.regs[EAX] = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cleanup_stdcall(cpu, memory, 4)

    def _decode_ptr(cpu: "CPU") -> None:
        cpu.regs[EAX] = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "EncodePointer", _encode_ptr)
    stubs.register_handler("kernel32.dll", "DecodePointer", _decode_ptr)

    # ── InterlockedCompareExchange ────────────────────────────────────────────

    def _interlocked_cmpxchg(cpu: "CPU") -> None:
        dest      = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        exchange  = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        comparand = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        current   = memory.read32(dest)
        if current == comparand:
            memory.write32(dest, exchange)
        cpu.regs[EAX] = current
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("kernel32.dll", "InterlockedCompareExchange", _interlocked_cmpxchg)

    # ── Sleep ─────────────────────────────────────────────────────────────────

    def _sleep(cpu: "CPU") -> None:
        dw_ms = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        return_eip = memory.read32(cpu.regs[ESP] & 0xFFFFFFFF)
        cpu.regs[ESP] = (cpu.regs[ESP] + 8) & 0xFFFFFFFF  # stdcall: pop ret addr + 4-byte arg
        state.scheduler.tick(dw_ms, memory)
        _fire_due_timers(cpu, memory, state)
        state.scheduler.sleep_current(cpu, memory, return_eip, 0, dw_ms)

    stubs.register_handler("kernel32.dll", "Sleep", _sleep)
