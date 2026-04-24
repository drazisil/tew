"""kernel32.dll system handlers — version, time, process info, env, Sleep scheduler."""

from __future__ import annotations

import time as _time_module
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers

from tew.hardware.cpu import EAX, EBX, ECX, EDX, ESP, EBP, ESI, EDI
from tew.api.win32_handlers import cleanup_stdcall
from tew.api._state import CRTState, FileHandleEntry, TEB_BASE
from tew.logger import logger

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
        cpu.regs[EAX] = 0x00210030

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
        cpu.regs[EAX] = 1234

    def _get_current_thread_id(cpu: "CPU") -> None:
        cpu.regs[EAX] = state.tls_current_thread_id()

    def _get_current_thread(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xFFFFFFFE

    stubs.register_handler("kernel32.dll", "GetCurrentProcess",   _get_current_process)
    stubs.register_handler("kernel32.dll", "GetCurrentProcessId", _get_current_process_id)
    stubs.register_handler("kernel32.dll", "GetCurrentThreadId",  _get_current_thread_id)
    stubs.register_handler("kernel32.dll", "GetCurrentThread",    _get_current_thread)

    # ── Error / tick / time ───────────────────────────────────────────────────

    def _get_last_error(cpu: "CPU") -> None:
        cpu.regs[EAX] = memory.read32(TEB_BASE + 0x34)
        cleanup_stdcall(cpu, memory, 0)

    def _set_last_error(cpu: "CPU") -> None:
        memory.write32(TEB_BASE + 0x34, memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF))
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
        cpu.regs[EAX] = 0x00210048

    def _free_env_strings_w(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    def _get_env_strings(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x0021004C

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
            return
        # FILE_TYPE_CHAR(2) for std handles (have fd), FILE_TYPE_DISK(1) for files
        cpu.regs[EAX] = 2 if entry.fd is not None else 1
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
