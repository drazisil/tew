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
from tew.api._state import (
    CRTState, FileHandleEntry, PendingThreadInfo,
    EventHandle, MutexHandle,
    THREAD_STACK_SIZE, THREAD_SENTINEL, TEB_BASE,
)
from tew.logger import logger

# QPC reports 1 MHz so counter values stay in easy integer range.
_QPC_FREQ: int = 1_000_000


def _run_thread_slice(cpu: "CPU", memory: "Memory",
                      thread: PendingThreadInfo, state: CRTState) -> None:
    """Run thread for up to 1M steps; save/restore state around it."""
    step_limit = 1_000_000
    cpu.halted = False
    steps = 0
    thread_error = None
    if thread.calls_seen is None:
        thread.calls_seen = set()
    log_first = thread.saved_state is None

    if log_first:
        logger.trace("thread", f"Starting thread at EIP=0x{cpu.eip:x}, ESP=0x{cpu.regs[ESP]:x}")
        param = thread.parameter
        logger.trace("thread", f"Parameter at 0x{param:x}:")
        for off in range(0, 0x54, 4):
            v = memory.read32(param + off)
            if v:
                logger.trace("thread", f"  [+0x{off:x}] = 0x{v:x}")

    try:
        last_valid = cpu.eip
        while not cpu.halted and steps < step_limit:
            eip = cpu.eip & 0xFFFFFFFF
            in_stubs    = 0x00200000 <= eip < 0x00220000
            in_exe      = 0x00400000 <= eip < 0x02000000
            in_dlls     = 0x10000000 <= eip < 0x40000000
            in_sentinel = 0x001FE000 <= eip < 0x001FE004
            if not (in_stubs or in_exe or in_dlls or in_sentinel) and steps > 10:
                logger.warn("thread", f"RUNAWAY step={steps}: EIP=0x{eip:x} last=0x{last_valid:x}")
                thread.completed = True
                break
            if in_stubs or in_exe or in_dlls:
                last_valid = eip
            if steps % 250_000 == 0 and steps:
                logger.warn("thread",
                    f"[tid={thread.thread_id} step={steps}] "
                    f"EIP=0x{eip:x} ESP=0x{cpu.regs[ESP]:x}")
            cpu.step()
            steps += 1
    except Exception as exc:
        thread_error = exc
        logger.warn("thread", f"Thread {thread.thread_id} error after {steps} steps: {exc}")

    if thread.completed:
        logger.debug("scheduler", f"Thread {thread.thread_id} completed ({steps} steps)")
    elif thread_error:
        thread.completed = True
    elif cpu.halted:
        if state.thread_yield_requested:
            state.thread_yield_requested = False
            thread.saved_state = cpu.save_state()
            logger.debug("scheduler",
                f"Thread {thread.thread_id} blocked (waiting on handles) after {steps} steps "
                f"(EIP=0x{cpu.eip & 0xFFFFFFFF:08x})")
        else:
            detail = f": {cpu.last_error}" if cpu.last_error else ""
            logger.error(
                "thread",
                f"Thread {thread.thread_id} crashed: unexpected halt at "
                f"EIP=0x{cpu.eip & 0xFFFFFFFF:08x} after {steps} steps{detail} — marking dead",
            )
            thread.completed = True
    else:
        thread.saved_state = cpu.save_state()
        logger.debug("scheduler",
            f"Thread {thread.thread_id} yielded after {steps} steps "
            f"(EIP=0x{cpu.eip:x})")


def _cooperative_sleep(
    cpu: "CPU", memory: "Memory", state: CRTState, arg_bytes: int
) -> bool:
    """Try to run a pending thread for one time slice.
    Returns True if a thread ran (caller should cleanup and return),
    False if no thread was available.

    Guards against reentrance: if a background thread calls Sleep() during
    its own slice, we do not recurse into another slice (that would blow
    Python's call stack).  Just return False so the Sleep stub returns 0.
    """
    if state.is_running_thread:
        return False
    num = len(state.pending_threads)
    runnable: Optional[PendingThreadInfo] = None
    tidx = -1
    for i in range(1, num + 1):
        idx = (state.last_scheduled_idx + i) % num
        t = state.pending_threads[idx]
        if t.suspended or t.completed:
            continue
        if t.waiting_on_handles:
            unblocked = False
            for wh in t.waiting_on_handles:
                obj = state.kernel_handle_map.get(wh)
                if obj is None or (isinstance(obj, EventHandle) and obj.signaled):
                    unblocked = True
                    break
                if isinstance(obj, MutexHandle) and not obj.locked:
                    unblocked = True
                    break
            if not unblocked and t.wait_deadline_ms is not None:
                if state.virtual_ticks_ms >= t.wait_deadline_ms:
                    t.wait_timed_out = True
                    t.wait_deadline_ms = None
                    unblocked = True
            if not unblocked:
                continue
            t.waiting_on_handles = None
        runnable = t
        tidx = idx
        break
    if runnable is None or tidx < 0:
        return False

    state.last_scheduled_idx = tidx
    logger.debug("scheduler",
        f"Main thread Sleep #{state.sleep_count} - thread {runnable.thread_id} "
        f"(startAddr=0x{runnable.start_address:x})")

    main_state = cpu.save_state()
    main_tid = state.tls_current_thread_id()   # capture before context switch
    state.is_running_thread = True
    state.current_thread_idx = tidx

    # Save main thread TLS from TEB to store; load background thread TLS into TEB.
    _tls_teb = TEB_BASE + 0xE0
    if state.tls_slots:
        main_tls = state.tls_thread_store(main_tid)
        for slot in state.tls_slots:
            main_tls[slot] = memory.read32(_tls_teb + slot * 4)
        bg_tls = state.tls_thread_store(runnable.thread_id)
        for slot in state.tls_slots:
            memory.write32(_tls_teb + slot * 4, bg_tls.get(slot, 0))

    if runnable.saved_state:
        cpu.restore_state(runnable.saved_state)
    else:
        stack_top = state.thread_stack_next + THREAD_STACK_SIZE - 16
        state.thread_stack_next += THREAD_STACK_SIZE
        esp = stack_top - 4
        memory.write32(esp, runnable.parameter)
        esp -= 4
        memory.write32(esp, THREAD_SENTINEL)
        cpu.regs[ESP] = esp & 0xFFFFFFFF
        cpu.regs[EBP] = 0
        cpu.regs[EAX] = 0
        cpu.regs[ECX] = 0
        cpu.regs[EDX] = 0
        cpu.regs[EBX] = 0
        cpu.regs[ESI] = 0
        cpu.regs[EDI] = 0
        cpu.eip = runnable.start_address
        cpu.eflags = 0x202

    _run_thread_slice(cpu, memory, runnable, state)

    # Advance the virtual clock by 1ms per background slice so finite-timeout
    # waits can expire even when the main thread spins on Sleep(0).
    state.virtual_ticks_ms = (state.virtual_ticks_ms + 1) & 0xFFFFFFFF

    # Save background thread TLS from TEB to store; restore main thread TLS into TEB.
    if state.tls_slots:
        bg_tls = state.tls_thread_store(runnable.thread_id)
        for slot in state.tls_slots:
            bg_tls[slot] = memory.read32(_tls_teb + slot * 4)
        main_tls = state.tls_thread_store(main_tid)
        for slot in state.tls_slots:
            memory.write32(_tls_teb + slot * 4, main_tls.get(slot, 0))

    cpu.restore_state(main_state)
    cpu.halted = False
    state.is_running_thread = False
    state.current_thread_idx = -1

    cleanup_stdcall(cpu, memory, arg_bytes)
    return True


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

    # ── Cooperative Sleep scheduler ───────────────────────────────────────────

    def _sleep(cpu: "CPU") -> None:
        if state.is_running_thread:
            # Background thread: can't yield to another thread from within a
            # thread slice.  Treat sleep as instant so the thread keeps running.
            cleanup_stdcall(cpu, memory, 4)
            return
        # Advance virtual clock by the requested sleep duration before yielding.
        # GetTickCount/timeGetTime return this virtual counter so that the
        # emulated binary sees time advance in proportion to emulated execution,
        # not Python wall time.
        dw_ms = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        state.virtual_ticks_ms = (state.virtual_ticks_ms + dw_ms) & 0xFFFFFFFF
        # Fire timer callbacks whose due_at has elapsed.
        from tew.api.win32_handlers import pending_timers
        due = [t for t in pending_timers.values() if t.due_at <= state.virtual_ticks_ms]
        if due:
            from tew.api.user32_handlers import _invoke_emulated_proc, _get_dialog_sentinel
            sentinel = _get_dialog_sentinel(state, memory)
            for timer in due:
                _invoke_emulated_proc(
                    cpu, memory, timer.cb_addr,
                    [timer.id, 0, timer.dw_user, 0, 0],
                    sentinel,
                )
                if timer.period_ms > 0:
                    timer.due_at += timer.period_ms
                else:
                    pending_timers.pop(timer.id, None)
        state.sleep_count += 1
        if _cooperative_sleep(cpu, memory, state, 4):
            state.sleep_count = 0
            return
        # Main thread, no runnable threads.  Warn periodically but do not halt —
        # the game may legitimately be waiting on a network response.
        if state.sleep_count % 50 == 0:
            logger.warn("handlers",
                f"[Win32] Sleep() called {state.sleep_count} times with no runnable threads")
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "Sleep", _sleep)
