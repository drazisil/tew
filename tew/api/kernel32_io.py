"""kernel32.dll stub handlers — I/O, threading, sync objects, time, and misc.

Registered by register_kernel32_io_handlers(), called from kernel32_handlers.py.
Covers handlers from CloseHandle through GetWindowsDirectoryA.
"""

from __future__ import annotations

import os
import stat
import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.loader.dll_loader import DLLLoader

from tew.hardware.cpu import EAX, EBX, ECX, EDX, ESP, EBP, ESI, EDI
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall
from tew.api.win32_errors import Win32Error
from tew.api.ini_file import (
    GetPrivateProfileStringArgs, GetPrivateProfileIntArgs,
    parse_ini, read_profile_string, read_profile_int,
    write_profile_string, write_profile_section,
)
from tew.api._state import (
    CRTState, MutexHandle, EventHandle,
    PendingThreadInfo,
    find_file_ci, read_cstring, read_wide_string,
    THREAD_STACK_SIZE,
)
from tew.logger import logger

# ── Environment variable store ────────────────────────────────────────────────
# Shared by Set/GetEnvironmentVariable{A,W} handlers.
_env_vars: dict[str, str] = {}

# ── Win32 handle constants ─────────────────────────────────────────────────────

_CURRENT_PROCESS_HANDLE = 0xFFFFFFFF  # GetCurrentProcess() pseudo-handle
_CURRENT_THREAD_HANDLE  = 0xFFFFFFFE  # GetCurrentThread() pseudo-handle
_DUPLICATE_CLOSE_SOURCE = 0x00000001


def _duplicate_handle_entry(state: CRTState, h_source: int, close_source: bool) -> int:
    """Find h_source in the handle tables and register a duplicate entry.

    Returns the new handle value.  All four source categories are handled:

    * Pseudo-handles (0xFFFFFFFF / 0xFFFFFFFE) — converted to a real kernel
      handle so the caller can later pass it to CloseHandle without errors.
    * File handles  — new entry shares the same FileHandleEntry object so that
      both handles advance the same file position (correct Win32 semantics).
    * Kernel handles (mutex / event) — new entry shares the same object.
    * Unknown handles (thread handles, module handles) — a dummy EventHandle is
      registered under the new value so that CloseHandle succeeds silently.

    When close_source is True the source handle is removed from both maps
    (DUPLICATE_CLOSE_SOURCE semantics).  The caller is responsible for the
    stack-cleanup and EAX=TRUE writeback.
    """
    new_handle: int

    if h_source in (_CURRENT_PROCESS_HANDLE, _CURRENT_THREAD_HANDLE):
        new_handle = state.next_kernel_handle
        state.next_kernel_handle += 1
        state.kernel_handle_map[new_handle] = EventHandle(signaled=True, manual_reset=True)

    elif h_source in state.file_handle_map:
        entry = state.file_handle_map[h_source]
        new_handle = state.next_file_handle
        state.next_file_handle += 1
        state.file_handle_map[new_handle] = entry   # shared ref → shared file position

    elif h_source in state.kernel_handle_map:
        obj = state.kernel_handle_map[h_source]
        new_handle = state.next_kernel_handle
        state.next_kernel_handle += 1
        state.kernel_handle_map[new_handle] = obj   # shared ref

    else:
        # Thread handle or other value not tracked in a lookup table.
        # Register a dummy so CloseHandle on the result does not warn.
        logger.debug("handlers",
            f"DuplicateHandle: untracked src=0x{h_source:08x} — registering dummy")
        new_handle = state.next_kernel_handle
        state.next_kernel_handle += 1
        state.kernel_handle_map[new_handle] = EventHandle(signaled=True, manual_reset=True)

    if close_source:
        state.file_handle_map.pop(h_source, None)
        state.kernel_handle_map.pop(h_source, None)

    return new_handle


def _run_thread_slice(
    cpu: "CPU", memory: "Memory", thread: PendingThreadInfo, state: CRTState
) -> None:
    step_limit = 10_000
    cpu.halted = False
    steps = 0
    thread_error = None
    if thread.calls_seen is None:
        thread.calls_seen = set()

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
            # A wait handler asked to yield the slice without killing the
            # thread (e.g. blocking WaitForSingleObject/WaitForMultipleObjectsEx).
            # Save CPU state so the thread resumes from the same INT 0xFE on
            # the next schedule.
            state.thread_yield_requested = False
            thread.saved_state = cpu.save_state()
            logger.debug("scheduler",
                f"Thread {thread.thread_id} blocked (waiting on handles) after {steps} steps "
                f"(EIP=0x{cpu.eip & 0xFFFFFFFF:08x})")
        else:
            # Halted unexpectedly — INT3, a halt-stub, memory fault, or explicit
            # cpu.halted set by a handler.  cpu.last_error carries the Python
            # exception if step() caught one internally.
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


def _run_background_slice(
    cpu: "CPU", memory: "Memory", state: CRTState
) -> bool:
    """Find a runnable background thread, execute one slice, restore main state.

    Returns True if a thread ran, False if all threads are suspended/blocked/complete.
    Does NOT set EAX or call cleanup_stdcall — the caller owns its own stack frame.
    Guards against reentrance: returns False if already inside a thread slice.
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
    main_state = cpu.save_state()
    state.is_running_thread = True
    state.current_thread_idx = tidx

    if runnable.saved_state:
        cpu.restore_state(runnable.saved_state)
    else:
        from tew.api._state import THREAD_SENTINEL
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
    state.virtual_ticks_ms = (state.virtual_ticks_ms + 1) & 0xFFFFFFFF
    cpu.restore_state(main_state)
    cpu.halted = False
    state.is_running_thread = False
    state.current_thread_idx = -1
    return True


def _cooperative_sleep_ex(
    cpu: "CPU", memory: "Memory", state: CRTState, arg_bytes: int, eax_val: int
) -> bool:
    """Try to schedule a thread (SleepEx variant). Returns True if handled.

    Guards against reentrance: if a background thread calls SleepEx during
    its own slice, do not recurse.  Return False so the stub returns 0.
    """
    if not _run_background_slice(cpu, memory, state):
        return False
    last_thread = state.pending_threads[state.last_scheduled_idx]
    logger.debug("scheduler",
        f"Main thread SleepEx #{state.sleep_count} - thread {last_thread.thread_id}")
    cpu.regs[EAX] = eax_val
    cleanup_stdcall(cpu, memory, arg_bytes)
    return True


def register_kernel32_io_handlers(
    stubs: Win32Handlers,
    memory: "Memory",
    state: CRTState,
    dll_loader: Optional["DLLLoader"] = None,
) -> None:
    """Register kernel32.dll handlers for I/O, threading, sync, time, and misc."""

    def _halt(name: str):
        def _h(cpu: "CPU") -> None:
            logger.error("handlers", f"[UNIMPLEMENTED] {name} — halting")
            cpu.halted = True
        return _h

    # ── Handle management ─────────────────────────────────────────────────────

    def _close_handle(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(h)
        if entry is not None and entry.fd is not None and entry.fd >= 3:
            try:
                os.close(entry.fd)
            except OSError as e:
                logger.warn("fileio", f"CloseHandle: os.close(fd={entry.fd}) failed: {e}")
        state.file_handle_map.pop(h, None)
        state.kernel_handle_map.pop(h, None)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "CloseHandle", _close_handle)

    # ── File I/O ──────────────────────────────────────────────────────────────

    def _write_file(cpu: "CPU") -> None:
        h_file      = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        lp_buf      = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        n_bytes     = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_written  = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(h_file)
        if not entry or not entry.writable or entry.fd is None:
            if lp_written:
                memory.write32(lp_written, 0)
            if not entry:
                logger.warn("fileio",
                    f'[Win32] WriteFile(handle=0x{h_file:x}) -> FALSE (unknown handle)')
            elif not entry.writable:
                logger.warn("fileio",
                    f'[Win32] WriteFile(handle=0x{h_file:x}) -> FALSE (read-only)')
            else:
                logger.warn("fileio",
                    f'[Win32] WriteFile(handle=0x{h_file:x}, "{entry.path}") -> FALSE (no fd)')
            cpu.regs[EAX] = 0
        else:
            buf = bytearray(n_bytes)
            for i in range(n_bytes):
                buf[i] = memory.read8((lp_buf + i) & 0xFFFFFFFF)
            os.write(entry.fd, bytes(buf))
            entry.position += n_bytes
            if lp_written:
                memory.write32(lp_written, n_bytes)
            logger.debug("fileio",
                f'[Win32] WriteFile(handle=0x{h_file:x}, nBytes={n_bytes}) -> TRUE')
            cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 20)

    def _set_handle_count(cpu: "CPU") -> None:
        u = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = u
        cleanup_stdcall(cpu, memory, 4)

    def _set_std_handle(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("kernel32.dll", "WriteFile",       _write_file)
    stubs.register_handler("kernel32.dll", "SetHandleCount",  _set_handle_count)
    stubs.register_handler("kernel32.dll", "SetStdHandle",    _set_std_handle)
    def _get_module_file_name_a(cpu: "CPU") -> None:
        h_module    = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        lp_filename = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        n_size      = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)

        if h_module == 0:
            # NULL → return the path of the running executable.
            if not state.exe_path:
                logger.error("handlers", "GetModuleFileNameA: exe_path not set in CRTState — halting")
                cpu.halted = True
                return
            win_path = state.reverse_translate_path(state.exe_path)
        else:
            mod = state.dynamic_modules.get(h_module)
            if mod is None:
                logger.error(
                    "handlers",
                    f"GetModuleFileNameA: unknown hModule 0x{h_module:x} — halting",
                )
                cpu.halted = True
                return
            # Use the stored full path when available; fall back to the bare DLL name.
            win_path = mod.dll_path if mod.dll_path else mod.dll_name

        # Encode as ANSI and write to the guest buffer.
        encoded = win_path.encode("latin-1", errors="replace")
        chars_to_copy = min(len(encoded), max(n_size - 1, 0))

        for i in range(chars_to_copy):
            memory.write8((lp_filename + i) & 0xFFFFFFFF, encoded[i])
        memory.write8((lp_filename + chars_to_copy) & 0xFFFFFFFF, 0)  # null terminator

        # Return chars copied (not including null), or n_size when truncated.
        cpu.regs[EAX] = n_size if len(encoded) >= n_size else len(encoded)
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("kernel32.dll", "GetModuleFileNameA", _get_module_file_name_a)
    stubs.register_handler("kernel32.dll", "GetModuleFileNameW", _halt("GetModuleFileNameW"))

    # ── Pointer validation ────────────────────────────────────────────────────

    def _is_bad_read_ptr(cpu: "CPU") -> None:
        lp  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        ucb = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        mem_size = memory.size
        cpu.regs[EAX] = 1 if (lp == 0 or lp + ucb > mem_size) else 0
        cleanup_stdcall(cpu, memory, 8)

    def _is_bad_write_ptr(cpu: "CPU") -> None:
        lp  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        ucb = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        mem_size = memory.size
        cpu.regs[EAX] = 1 if (lp == 0 or lp + ucb > mem_size) else 0
        cleanup_stdcall(cpu, memory, 8)

    def _is_bad_code_ptr(cpu: "CPU") -> None:
        lpfn = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        mem_size = memory.size
        cpu.regs[EAX] = 1 if (lpfn == 0 or lpfn >= mem_size) else 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "IsBadReadPtr",  _is_bad_read_ptr)
    stubs.register_handler("kernel32.dll", "IsBadWritePtr", _is_bad_write_ptr)
    stubs.register_handler("kernel32.dll", "IsBadCodePtr",  _is_bad_code_ptr)

    # ── Process termination ───────────────────────────────────────────────────

    def _terminate_process(cpu: "CPU") -> None:
        code = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        logger.info("handlers", f"[Win32] TerminateProcess(exitCode={code})")
        cpu.halted = True

    def _fatal_app_exit(cpu: "CPU") -> None:
        logger.error("handlers", "[Win32] FatalAppExitA called")
        cpu.halted = True

    stubs.register_handler("kernel32.dll", "TerminateProcess", _terminate_process)
    stubs.register_handler("kernel32.dll", "FatalAppExitA",    _fatal_app_exit)
    stubs.register_handler("kernel32.dll", "RtlUnwind",        _halt("RtlUnwind"))
    stubs.register_handler("kernel32.dll", "RaiseException",   _halt("RaiseException"))

    # ── Thread creation and management ────────────────────────────────────────

    def _create_thread(cpu: "CPU") -> None:
        lp_start  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_param  = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        dw_flags  = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        lp_tid    = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        CREATE_SUSPENDED = 0x4
        is_susp = bool(dw_flags & CREATE_SUSPENDED)
        tid    = state.next_thread_id
        state.next_thread_id += 1
        handle = state.next_thread_handle
        state.next_thread_handle += 1
        logger.info("thread",
            f"CreateThread(start=0x{lp_start:x}, param=0x{lp_param:x}, "
            f"flags=0x{dw_flags:x}) -> handle=0x{handle:x}, tid={tid}")
        state.pending_threads.append(PendingThreadInfo(
            start_address=lp_start,
            parameter=lp_param,
            handle=handle,
            thread_id=tid,
            suspended=is_susp,
            completed=False,
        ))
        if lp_tid:
            memory.write32(lp_tid, tid)
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, 24)

    def _resume_thread(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        for t in state.pending_threads:
            if t.handle == h and t.suspended:
                logger.debug("thread",
                    f"ResumeThread(0x{h:x}) - unsuspending thread {t.thread_id}")
                t.suspended = False
                break
        cpu.regs[EAX] = 1  # previous suspend count
        cleanup_stdcall(cpu, memory, 4)

    def _exit_thread(cpu: "CPU") -> None:
        code = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        logger.debug("thread", f"ExitThread({code})")
        if 0 <= state.current_thread_idx < len(state.pending_threads):
            state.pending_threads[state.current_thread_idx].completed = True
        cpu.halted = True

    def _get_exit_code_thread(cpu: "CPU") -> None:
        h         = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_code   = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        thread = next((t for t in state.pending_threads if t.handle == h), None)
        if lp_code:
            memory.write32(lp_code, 0 if (thread and thread.completed) else 259)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _suspend_thread(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        for t in state.pending_threads:
            if t.handle == h:
                logger.debug("thread",
                    f"SuspendThread(0x{h:x}) - suspending thread {t.thread_id}")
                t.suspended = True
                break
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    def _set_thread_priority(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _get_thread_priority(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # THREAD_PRIORITY_NORMAL
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "CreateThread",        _create_thread)
    stubs.register_handler("kernel32.dll", "ResumeThread",        _resume_thread)
    stubs.register_handler("kernel32.dll", "ExitThread",          _exit_thread)
    stubs.register_handler("kernel32.dll", "GetExitCodeThread",   _get_exit_code_thread)
    stubs.register_handler("kernel32.dll", "SuspendThread",       _suspend_thread)
    stubs.register_handler("kernel32.dll", "SetThreadPriority",   _set_thread_priority)
    stubs.register_handler("kernel32.dll", "GetThreadPriority",   _get_thread_priority)

    # ── SleepEx ───────────────────────────────────────────────────────────────

    def _sleep_ex(cpu: "CPU") -> None:
        if state.is_running_thread:
            # We are inside a cooperative thread slice.  We cannot schedule
            # another cooperative slice (that would corrupt the main-thread
            # save state), but we CAN fire due timer callbacks in-place via
            # _invoke_emulated_proc (which saves/restores the current CPU
            # state around the call).
            #
            # We ALWAYS yield after this, even when no callbacks fired.
            # On real Windows, SleepEx(n) suspends the calling thread and
            # gives other threads a chance to run.  In our cooperative
            # scheduler the timer thread body (FUN_00a30ea0) must run at
            # least once before mmtimer_callback fires so that
            # _SIGNAL_alloc() has been called and DAT_020d84cc holds a
            # valid event handle.  If we return immediately when no
            # callbacks are due, the timer thread never gets a slice and
            # SetEvent(0) is a no-op forever.
            #
            # cleanup_stdcall is called before yielding so the saved EIP
            # points to the return address (after the SleepEx call site)
            # rather than back at INT 0xFE, which would cause an infinite
            # re-entry loop on the next schedule.
            #
            # Note: we do NOT advance virtual_ticks_ms from the background
            # thread path — only the main thread drives the virtual clock.
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
            # Always yield: return EAX=0 from SleepEx, then suspend this
            # thread so the scheduler can run newly-unblocked threads.
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 8)
            state.thread_yield_requested = True
            cpu.halted = True
            return
        # Advance virtual clock by the requested sleep duration before yielding.
        # GetTickCount/timeGetTime return this virtual counter so that the
        # emulated binary sees time advance in proportion to emulated execution,
        # not Python wall time.  This prevents GetTickCount-based timeouts from
        # expiring during cooperative thread slices (each of which takes ~1-2s
        # of Python wall time but represents only dwMilliseconds of emulated time).
        dw_ms = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        state.virtual_ticks_ms = (state.virtual_ticks_ms + dw_ms) & 0xFFFFFFFF
        # Fire timer callbacks whose due_at has elapsed.  The due_at guard
        # ensures mmtimer_callback does not fire before the timer thread has
        # run its first slice and set DAT_020d84cc via _SIGNAL_alloc().
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
        if _cooperative_sleep_ex(cpu, memory, state, 8, 0):
            state.sleep_count = 0
            return
        if state.sleep_count % 50 == 0:
            logger.warn("handlers",
                f"[Win32] SleepEx() called {state.sleep_count} times with no runnable threads")
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("kernel32.dll", "SleepEx", _sleep_ex)

    # ── Wait functions ────────────────────────────────────────────────────────

    def _wait_for_single(cpu: "CPU") -> None:
        h          = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        timeout_ms = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        obj = state.kernel_handle_map.get(h)
        if obj is not None:
            if isinstance(obj, MutexHandle):
                obj.locked = True
                cpu.regs[EAX] = 0
                cleanup_stdcall(cpu, memory, 8)
                return
            else:  # EventHandle
                if obj.signaled:
                    if not obj.manual_reset:
                        obj.signaled = False
                    cpu.regs[EAX] = 0
                    cleanup_stdcall(cpu, memory, 8)
                    return
                # Not signaled.
                if state.is_running_thread:
                    tidx = state.current_thread_idx
                    if 0 <= tidx < len(state.pending_threads):
                        t = state.pending_threads[tidx]
                        # Woken by timeout expiry (scheduler set wait_timed_out).
                        if t.wait_timed_out:
                            t.wait_timed_out = False
                            cpu.regs[EAX] = 0x102  # WAIT_TIMEOUT
                            cleanup_stdcall(cpu, memory, 8)
                            return
                    # Block this thread: rewind EIP so the wait is retried on the
                    # next schedule, record the handle, yield the slice.
                    cpu.eip = (cpu.eip - 2) & 0xFFFFFFFF
                    if 0 <= tidx < len(state.pending_threads):
                        t = state.pending_threads[tidx]
                        t.waiting_on_handles = frozenset([h])
                        if timeout_ms != _WAIT_INFINITE:
                            t.wait_deadline_ms = state.virtual_ticks_ms + timeout_ms
                            t.wait_timed_out = False
                    state.thread_yield_requested = True
                    cpu.halted = True
                    return
                # Main thread — drive background threads until the handle is
                # signaled (process-zero pattern).  Without this, the main
                # thread would halt and nothing could ever call SetEvent.
                if timeout_ms == _WAIT_INFINITE:
                    _wfso_slice_count = 0
                    while True:
                        if not _run_background_slice(cpu, memory, state):
                            logger.warn("scheduler",
                                f"WaitForSingleObject(0x{h:x}) INFINITE: "
                                f"deadlock — no runnable threads, returning TIMEOUT")
                            cpu.regs[EAX] = 0x102
                            cleanup_stdcall(cpu, memory, 8)
                            return
                        _wfso_slice_count += 1
                        if _wfso_slice_count % 10 == 0:
                            # Advance virtual clock so deadline-based waits and
                            # timer threads can make progress.  Mirrors the
                            # main-loop heartbeat rate (1ms per 100K steps at
                            # 10K steps/slice × 10 slices = 100K steps/ms).
                            state.virtual_ticks_ms = (state.virtual_ticks_ms + 1) & 0xFFFFFFFF
                        obj = state.kernel_handle_map.get(h)
                        if obj is None:
                            break
                        if isinstance(obj, EventHandle) and obj.signaled:
                            break
                        if isinstance(obj, MutexHandle) and not obj.locked:
                            break
                    # Handle is now ready — acquire and return WAIT_OBJECT_0.
                    obj = state.kernel_handle_map.get(h)
                    if isinstance(obj, EventHandle) and not obj.manual_reset:
                        obj.signaled = False
                    elif isinstance(obj, MutexHandle):
                        obj.locked = True
                    cpu.regs[EAX] = 0
                    cleanup_stdcall(cpu, memory, 8)
                    return
                cpu.regs[EAX] = 0x102  # WAIT_TIMEOUT
                cleanup_stdcall(cpu, memory, 8)
                return
        # Unknown handle (thread handle etc.) — treat as signaled.
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    _WAIT_INFINITE = 0xFFFFFFFF

    def _wait_for_multiple_ex(cpu: "CPU") -> None:
        base       = cpu.regs[ESP]
        n_count    = memory.read32((base +  4) & 0xFFFFFFFF)
        lp_handles = memory.read32((base +  8) & 0xFFFFFFFF)
        b_wait_all = memory.read32((base + 12) & 0xFFFFFFFF) != 0
        timeout_ms = memory.read32((base + 16) & 0xFFFFFFFF)
        all_ready  = True
        for i in range(n_count):
            h   = memory.read32((lp_handles + i * 4) & 0xFFFFFFFF)
            obj = state.kernel_handle_map.get(h)
            if obj is None:
                # Unknown handle (e.g. thread handle) — treat as always-ready.
                if not b_wait_all:
                    cpu.regs[EAX] = i & 0xFFFFFFFF
                    cleanup_stdcall(cpu, memory, 20)
                    return
                continue
            ready = isinstance(obj, MutexHandle) or (
                isinstance(obj, EventHandle) and obj.signaled)
            if ready:
                if not b_wait_all:
                    if isinstance(obj, EventHandle) and not obj.manual_reset:
                        obj.signaled = False
                    if isinstance(obj, MutexHandle):
                        obj.locked = True
                    cpu.regs[EAX] = i & 0xFFFFFFFF
                    cleanup_stdcall(cpu, memory, 20)
                    return
            else:
                all_ready = False
                if b_wait_all:
                    break
        if b_wait_all and all_ready:
            for i in range(n_count):
                h   = memory.read32((lp_handles + i * 4) & 0xFFFFFFFF)
                obj = state.kernel_handle_map.get(h)
                if obj is not None:
                    if isinstance(obj, EventHandle) and not obj.manual_reset:
                        obj.signaled = False
                    if isinstance(obj, MutexHandle):
                        obj.locked = True
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 20)
            return
        # Not yet ready.
        if state.is_running_thread and timeout_ms == _WAIT_INFINITE:
            # Block this thread: rewind EIP so the wait is retried on
            # the next schedule, record all handles being waited on, and
            # yield the slice without killing the thread.
            cpu.eip = (cpu.eip - 2) & 0xFFFFFFFF  # rewind past INT 0xFE
            handles_set: set[int] = set()
            for i in range(n_count):
                handles_set.add(memory.read32((lp_handles + i * 4) & 0xFFFFFFFF))
            tidx = state.current_thread_idx
            if 0 <= tidx < len(state.pending_threads):
                state.pending_threads[tidx].waiting_on_handles = frozenset(handles_set)
            state.thread_yield_requested = True
            cpu.halted = True
            return  # no cleanup — stack stays for retry
        cpu.regs[EAX] = 0x102  # WAIT_TIMEOUT
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("kernel32.dll", "WaitForSingleObject",     _wait_for_single)
    stubs.register_handler("kernel32.dll", "WaitForMultipleObjects",  _halt("WaitForMultipleObjects"))
    stubs.register_handler("kernel32.dll", "WaitForMultipleObjectsEx", _wait_for_multiple_ex)

    # ── Mutex / Event ─────────────────────────────────────────────────────────

    def _create_mutex_a(cpu: "CPU") -> None:
        b_owner  = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        name_ptr = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory) if name_ptr else "(unnamed)"
        h = state.next_kernel_handle
        state.next_kernel_handle += 1
        state.kernel_handle_map[h] = MutexHandle(locked=b_owner != 0)
        logger.debug("handlers", f'[Win32] CreateMutexA("{name}") -> 0x{h:x}')
        cpu.regs[EAX] = h
        cleanup_stdcall(cpu, memory, 12)

    def _open_mutex_a(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory) if name_ptr else "(unnamed)"
        # Named mutexes are process-local in this emulator; a named mutex opened
        # before CreateMutexA creates it does not exist.  Signal this the same
        # way Win32 does: return NULL and set ERROR_FILE_NOT_FOUND.
        state.last_error = int(Win32Error.ERROR_FILE_NOT_FOUND)
        logger.warn("handlers",
            f'[Win32] OpenMutexA("{name}") -> NULL (no shared named mutexes)')
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 12)

    def _release_mutex(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        obj = state.kernel_handle_map.get(h)
        if isinstance(obj, MutexHandle):
            obj.locked = False
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    def _create_event_a(cpu: "CPU") -> None:
        b_manual   = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        b_initial  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        name_ptr   = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory) if name_ptr else "(unnamed)"
        h = state.next_kernel_handle
        state.next_kernel_handle += 1
        state.kernel_handle_map[h] = EventHandle(
            signaled=b_initial != 0,
            manual_reset=b_manual != 0,
        )
        logger.debug("handlers",
            f'[Win32] CreateEventA("{name}", manual={bool(b_manual)}, '
            f'signaled={bool(b_initial)}) -> 0x{h:x}')
        cpu.regs[EAX] = h
        cleanup_stdcall(cpu, memory, 16)

    def _set_event(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        obj = state.kernel_handle_map.get(h)
        if isinstance(obj, EventHandle):
            obj.signaled = True
            # Unblock any threads waiting on this handle so the scheduler
            # will pick them up on the next SleepEx/cooperative yield.
            for t in state.pending_threads:
                if t.waiting_on_handles and h in t.waiting_on_handles:
                    t.waiting_on_handles = None
                    logger.debug("scheduler",
                        f"SetEvent(0x{h:x}) unblocked thread {t.thread_id}")
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    def _reset_event(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        obj = state.kernel_handle_map.get(h)
        if isinstance(obj, EventHandle):
            obj.signaled = False
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "CreateMutexA",   _create_mutex_a)
    stubs.register_handler("kernel32.dll", "OpenMutexA",     _open_mutex_a)
    stubs.register_handler("kernel32.dll", "ReleaseMutex",   _release_mutex)
    stubs.register_handler("kernel32.dll", "CreateEventA",   _create_event_a)
    stubs.register_handler("kernel32.dll", "SetEvent",       _set_event)
    stubs.register_handler("kernel32.dll", "ResetEvent",     _reset_event)

    # ── CreateFile / ReadFile ─────────────────────────────────────────────────

    GENERIC_WRITE = 0x40000000

    def _create_file_a(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        access   = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory)
        cpu.regs[EAX] = state.open_file_handle(name, bool(access & GENERIC_WRITE))
        cleanup_stdcall(cpu, memory, 28)

    def _create_file_w(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        access   = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        name = read_wide_string(name_ptr, memory)
        cpu.regs[EAX] = state.open_file_handle(name, bool(access & GENERIC_WRITE))
        cleanup_stdcall(cpu, memory, 28)

    def _read_file(cpu: "CPU") -> None:
        h_file      = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        lp_buf      = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        n_to_read   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_read     = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(h_file)
        if not entry or entry.writable:
            logger.warn("fileio",
                f'[Win32] ReadFile(handle=0x{h_file:x}) -> FALSE')
            if lp_read:
                memory.write32(lp_read, 0)
            cpu.regs[EAX] = 0
        else:
            available = len(entry.data) - entry.position
            to_read = min(n_to_read, available)
            for i in range(to_read):
                memory.write8(lp_buf + i, entry.data[entry.position + i])
            entry.position += to_read
            if lp_read:
                memory.write32(lp_read, to_read)
            cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 20)

    def _delete_file_a(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory)
        real_path = state.translate_windows_path(name)
        try:
            os.unlink(real_path)
            success = True
        except OSError:
            success = False
        logger.debug("fileio", f'[Win32] DeleteFileA("{name}") -> {success}')
        cpu.regs[EAX] = 1 if success else 0
        cleanup_stdcall(cpu, memory, 4)

    def _delete_file_w(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        name = read_wide_string(name_ptr, memory)
        real_path = state.translate_windows_path(name)
        try:
            os.unlink(real_path)
            success = True
        except OSError:
            success = False
        logger.debug("fileio", f'[Win32] DeleteFileW("{name}") -> {success}')
        cpu.regs[EAX] = 1 if success else 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "CreateFileA", _create_file_a)
    stubs.register_handler("kernel32.dll", "CreateFileW", _create_file_w)
    stubs.register_handler("kernel32.dll", "ReadFile",    _read_file)
    stubs.register_handler("kernel32.dll", "DeleteFileA", _delete_file_a)
    stubs.register_handler("kernel32.dll", "DeleteFileW", _delete_file_w)

    # ── Find file / attributes ────────────────────────────────────────────────

    def _find_close(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] FindClose — find handle tracking not implemented, halting")
        cpu.halted = True

    def _get_file_attributes_a(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory)
        linux_path = state.translate_windows_path(name)
        real_path = find_file_ci(linux_path)
        if real_path is not None:
            try:
                s = os.stat(real_path)
                FILE_ATTRIBUTE_DIRECTORY = 0x10
                FILE_ATTRIBUTE_NORMAL    = 0x80
                cpu.regs[EAX] = (FILE_ATTRIBUTE_DIRECTORY
                                  if stat.S_ISDIR(s.st_mode)
                                  else FILE_ATTRIBUTE_NORMAL)
            except OSError as e:
                logger.warn("fileio",
                    f'GetFileAttributesA: stat failed for "{real_path}": {e}')
                cpu.regs[EAX] = 0xFFFFFFFF
        else:
            cpu.regs[EAX] = 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "FindFirstFileA", _halt("FindFirstFileA"))
    stubs.register_handler("kernel32.dll", "FindFirstFileW", _halt("FindFirstFileW"))
    stubs.register_handler("kernel32.dll", "FindNextFileA",  _halt("FindNextFileA"))
    stubs.register_handler("kernel32.dll", "FindNextFileW",  _halt("FindNextFileW"))
    stubs.register_handler("kernel32.dll", "FindClose",      _find_close)
    stubs.register_handler("kernel32.dll", "CompareFileTime", _halt("CompareFileTime"))
    stubs.register_handler("kernel32.dll", "GetFileAttributesA", _get_file_attributes_a)
    stubs.register_handler("kernel32.dll", "GetFullPathNameA",   _halt("GetFullPathNameA"))

    # ── SetFilePointer / GetFileSize ──────────────────────────────────────────

    def _set_file_pointer(cpu: "CPU") -> None:
        h_file   = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dist_raw = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        distance = dist_raw if dist_raw < 0x80000000 else dist_raw - 0x100000000
        method   = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        FILE_BEGIN = 0; FILE_CURRENT = 1; FILE_END = 2
        entry = state.file_handle_map.get(h_file)
        if entry and not entry.writable:
            if method == FILE_BEGIN:
                new_pos = distance
            elif method == FILE_CURRENT:
                new_pos = entry.position + distance
            else:
                new_pos = len(entry.data) + distance
            new_pos = max(0, min(new_pos, len(entry.data)))
            entry.position = new_pos
            cpu.regs[EAX] = new_pos & 0xFFFFFFFF
        elif entry and entry.writable and entry.fd is not None:
            file_size = 0
            if method == FILE_END:
                file_size = os.fstat(entry.fd).st_size
            if method == FILE_BEGIN:
                new_pos = distance
            elif method == FILE_CURRENT:
                new_pos = entry.position + distance
            else:
                new_pos = file_size + distance
            new_pos = max(0, new_pos)
            entry.position = new_pos
            cpu.regs[EAX] = new_pos & 0xFFFFFFFF
        else:
            cpu.regs[EAX] = 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 16)

    def _get_file_size(cpu: "CPU") -> None:
        h_file    = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_high   = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(h_file)
        if entry and not entry.writable:
            if lp_high:
                memory.write32(lp_high, 0)
            cpu.regs[EAX] = len(entry.data) & 0xFFFFFFFF
        else:
            cpu.regs[EAX] = 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 8)

    def _get_file_size_ex(cpu: "CPU") -> None:
        h_file    = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_size   = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(h_file)
        if entry and lp_size:
            if not entry.writable:
                size = len(entry.data)
            elif entry.fd is not None:
                size = os.fstat(entry.fd).st_size
            else:
                cpu.regs[EAX] = 0
                cleanup_stdcall(cpu, memory, 8)
                return
            memory.write32(lp_size,     size & 0xFFFFFFFF)
            memory.write32(lp_size + 4, 0)
            cpu.regs[EAX] = 1
        else:
            cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    def _flush_file_buffers(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    def _set_end_of_file(cpu: "CPU") -> None:
        h_file = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(h_file)
        if entry and entry.writable and entry.fd is not None:
            os.ftruncate(entry.fd, entry.position)
            cpu.regs[EAX] = 1
        else:
            cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "SetFilePointer",   _set_file_pointer)
    stubs.register_handler("kernel32.dll", "GetFileSize",      _get_file_size)
    stubs.register_handler("kernel32.dll", "GetFileSizeEx",    _get_file_size_ex)
    stubs.register_handler("kernel32.dll", "FlushFileBuffers", _flush_file_buffers)
    stubs.register_handler("kernel32.dll", "SetEndOfFile",     _set_end_of_file)

    # ── Directory / drives ────────────────────────────────────────────────────

    def _get_current_dir_a(cpu: "CPU") -> None:
        n_buf  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_buf = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        d = "C:\\MCity"
        if lp_buf and n_buf > len(d):
            for i, ch in enumerate(d):
                memory.write8(lp_buf + i, ord(ch))
            memory.write8(lp_buf + len(d), 0)
            cpu.regs[EAX] = len(d)
        else:
            cpu.regs[EAX] = len(d) + 1  # required size
        cleanup_stdcall(cpu, memory, 8)

    def _set_current_dir_a(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] SetCurrentDirectoryA — halting")
        cpu.halted = True

    def _get_windows_dir_a(cpu: "CPU") -> None:
        lp_buf = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        u_size = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        d = "C:\\WINDOWS"
        if lp_buf and u_size > len(d):
            for i, ch in enumerate(d):
                memory.write8(lp_buf + i, ord(ch))
            memory.write8(lp_buf + len(d), 0)
        cpu.regs[EAX] = len(d)
        cleanup_stdcall(cpu, memory, 8)

    def _get_disk_free_space_a(cpu: "CPU") -> None:
        lp_spc = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        lp_bps = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_fc  = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_tc  = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        if lp_spc: memory.write32(lp_spc, 8)
        if lp_bps: memory.write32(lp_bps, 512)
        if lp_fc:  memory.write32(lp_fc, 1000000)
        if lp_tc:  memory.write32(lp_tc, 2000000)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 20)

    def _get_drive_type_a(cpu: "CPU") -> None:
        # GetDriveTypeA(lpRootPathName) -> UINT
        # DRIVE_UNKNOWN=0, DRIVE_NO_ROOT_DIR=1, DRIVE_REMOVABLE=2,
        # DRIVE_FIXED=3, DRIVE_REMOTE=4, DRIVE_CDROM=5, DRIVE_RAMDISK=6
        DRIVE_NO_ROOT_DIR = 1
        DRIVE_FIXED       = 3
        lp_root = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if lp_root == 0:
            # NULL → drive type of current directory; we report fixed
            cpu.regs[EAX] = DRIVE_FIXED
        else:
            root_path = read_cstring(lp_root, memory, 16)
            linux_path = state.translate_windows_path(root_path)
            if os.path.isdir(linux_path.rstrip("/") or "/"):
                cpu.regs[EAX] = DRIVE_FIXED
            else:
                cpu.regs[EAX] = DRIVE_NO_ROOT_DIR
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GetCurrentDirectoryA", _get_current_dir_a)
    stubs.register_handler("kernel32.dll", "SetCurrentDirectoryA", _set_current_dir_a)
    stubs.register_handler("kernel32.dll", "GetWindowsDirectoryA", _get_windows_dir_a)
    stubs.register_handler("kernel32.dll", "GetDiskFreeSpaceA",    _get_disk_free_space_a)
    stubs.register_handler("kernel32.dll", "GetDriveTypeA",        _get_drive_type_a)
    def _global_memory_status(cpu: "CPU") -> None:
        """
        void GlobalMemoryStatus(LPMEMORYSTATUS lpBuffer)

        Fills a MEMORYSTATUS structure (32 bytes) with plausible values.
        The emulator reports 256 MB physical RAM, half available.

        MEMORYSTATUS layout:
            +0  dwLength          DWORD  (must be set to sizeof(MEMORYSTATUS) = 32)
            +4  dwMemoryLoad      DWORD  percentage of memory in use
            +8  dwTotalPhys       DWORD  total physical bytes
            +12 dwAvailPhys       DWORD  available physical bytes
            +16 dwTotalPageFile   DWORD  total paging file bytes
            +20 dwAvailPageFile   DWORD  available paging file bytes
            +24 dwTotalVirtual    DWORD  total virtual address space
            +28 dwAvailVirtual    DWORD  available virtual address space
        """
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if lp:
            MB = 1024 * 1024
            memory.write32(lp +  0, 32)          # dwLength
            memory.write32(lp +  4, 50)          # dwMemoryLoad (50%)
            memory.write32(lp +  8, 256 * MB)    # dwTotalPhys
            memory.write32(lp + 12, 128 * MB)    # dwAvailPhys
            memory.write32(lp + 16, 512 * MB)    # dwTotalPageFile
            memory.write32(lp + 20, 384 * MB)    # dwAvailPageFile
            memory.write32(lp + 24, 0x7FFF0000)  # dwTotalVirtual
            memory.write32(lp + 28, 0x7FF00000)  # dwAvailVirtual
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GlobalMemoryStatus", _global_memory_status)

    # ── Time ──────────────────────────────────────────────────────────────────

    def _write_systemtime(lp: int, dt: datetime.datetime, *, utc: bool) -> None:
        memory.write16(lp,      dt.year)
        memory.write16(lp +  2, dt.month)
        memory.write16(lp +  4, dt.weekday() + 1 if not utc else dt.isoweekday() % 7)
        memory.write16(lp +  6, dt.day)
        memory.write16(lp +  8, dt.hour)
        memory.write16(lp + 10, dt.minute)
        memory.write16(lp + 12, dt.second)
        memory.write16(lp + 14, dt.microsecond // 1000)

    def _get_local_time(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        now = datetime.datetime.now()
        _write_systemtime(lp, now, utc=False)
        cleanup_stdcall(cpu, memory, 4)

    def _get_system_time(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        now = datetime.datetime.utcnow()
        _write_systemtime(lp, now, utc=True)
        cleanup_stdcall(cpu, memory, 4)

    def _get_tz_info(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        for i in range(172):
            memory.write8(lp + i, 0)
        # Standard offset is the larger of jan/jul (further from UTC)
        now = datetime.datetime.now()
        jan = datetime.datetime(now.year, 1, 1).astimezone()
        jul = datetime.datetime(now.year, 7, 1).astimezone()
        jan_off = -int(jan.utcoffset().total_seconds() // 60)
        jul_off = -int(jul.utcoffset().total_seconds() // 60)
        std_offset = max(jan_off, jul_off)
        dst_offset = min(jan_off, jul_off)
        cur_off = -int(datetime.datetime.now().astimezone().utcoffset().total_seconds() // 60)
        is_dst = (cur_off == dst_offset) and (std_offset != dst_offset)
        memory.write32(lp,      std_offset & 0xFFFFFFFF)  # Bias
        memory.write32(lp + 84, 0)                        # StandardBias
        memory.write32(lp + 168, (dst_offset - std_offset) & 0xFFFFFFFF)
        cpu.regs[EAX] = 2 if is_dst else 1
        cleanup_stdcall(cpu, memory, 4)

    def _file_time_to_local(cpu: "CPU") -> None:
        lp_in  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_out = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if lp_in == 0 or lp_out == 0:
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 8)
            return
        lo = memory.read32(lp_in)
        hi = memory.read32(lp_in + 4)
        utc = (hi << 32) | lo
        bias_min = -int(datetime.datetime.now().astimezone().utcoffset().total_seconds() // 60)
        bias_100ns = bias_min * 60 * 10_000_000
        local = (utc - bias_100ns) & 0xFFFFFFFFFFFFFFFF
        memory.write32(lp_out,     local & 0xFFFFFFFF)
        memory.write32(lp_out + 4, (local >> 32) & 0xFFFFFFFF)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _file_time_to_system(cpu: "CPU") -> None:
        lp_ft = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_st = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if lp_ft == 0 or lp_st == 0:
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 8)
            return
        lo = memory.read32(lp_ft)
        hi = memory.read32(lp_ft + 4)
        ft = (hi << 32) | lo
        # FILETIME epoch = 1601-01-01; Unix epoch = 1970-01-01
        # difference = 11644473600 s = 116444736000000000 × 100ns
        unix_ms = (ft - 116444736000000000) // 10000
        try:
            d = datetime.datetime.utcfromtimestamp(unix_ms / 1000.0)
        except (OSError, OverflowError, ValueError):
            d = datetime.datetime(1970, 1, 1)
        _write_systemtime(lp_st, d, utc=True)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("kernel32.dll", "GetLocalTime",            _get_local_time)
    stubs.register_handler("kernel32.dll", "GetSystemTime",           _get_system_time)
    stubs.register_handler("kernel32.dll", "GetTimeZoneInformation",  _get_tz_info)
    stubs.register_handler("kernel32.dll", "FileTimeToLocalFileTime", _file_time_to_local)
    stubs.register_handler("kernel32.dll", "FileTimeToSystemTime",    _file_time_to_system)

    # ── Misc ──────────────────────────────────────────────────────────────────

    stubs.register_handler("kernel32.dll", "FormatMessageA",        _halt("FormatMessageA"))
    stubs.register_handler("kernel32.dll", "GlobalGetAtomNameA",    _halt("GlobalGetAtomNameA"))
    stubs.register_handler("kernel32.dll", "GlobalDeleteAtom",      _halt("GlobalDeleteAtom"))

    def _device_io_control(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 32)

    def _win_exec(cpu: "CPU") -> None:
        cpu.regs[EAX] = 31  # ERROR_FILE_NOT_FOUND
        cleanup_stdcall(cpu, memory, 8)

    def _lopen(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 8)

    def _lclose(cpu: "CPU") -> None:
        hfile = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        entry = state.file_handle_map.pop(hfile, None)
        if entry is not None:
            if entry.fd is not None:
                os.close(entry.fd)
            cpu.regs[EAX] = hfile
        else:
            cpu.regs[EAX] = 0xFFFFFFFF  # HFILE_ERROR
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "DeviceIoControl", _device_io_control)
    stubs.register_handler("kernel32.dll", "WinExec",         _win_exec)
    stubs.register_handler("kernel32.dll", "_lopen",          _lopen)
    stubs.register_handler("kernel32.dll", "_lclose",         _lclose)

    # ── Private profile (INI file) ────────────────────────────────────────────

    def _get_private_profile_string_a(cpu: "CPU") -> None:
        esp = cpu.regs[ESP]
        lp_app_name  = memory.read32((esp +  4) & 0xFFFFFFFF)
        lp_key_name  = memory.read32((esp +  8) & 0xFFFFFFFF)
        lp_default   = memory.read32((esp + 12) & 0xFFFFFFFF)
        lp_returned  = memory.read32((esp + 16) & 0xFFFFFFFF)
        n_size       = memory.read32((esp + 20) & 0xFFFFFFFF)
        lp_file_name = memory.read32((esp + 24) & 0xFFFFFFFF)

        app_name  = read_cstring(lp_app_name,  memory) if lp_app_name  else None
        key_name  = read_cstring(lp_key_name,  memory) if lp_key_name  else None
        default   = read_cstring(lp_default,   memory) if lp_default   else ""
        file_name = read_cstring(lp_file_name, memory) if lp_file_name else ""

        args = GetPrivateProfileStringArgs(
            app_name=app_name, key_name=key_name, default=default,
            out_ptr=lp_returned, n_size=n_size, file_name=file_name,
        )

        # Load and parse the INI file from the translated Linux path.
        ini: dict = {}
        if file_name:
            linux_path = state.translate_windows_path(file_name)
            real_path  = find_file_ci(linux_path)
            if real_path:
                # find_file_ci confirmed the path exists — any OSError here is not ENOENT.
                try:
                    with open(real_path, "r", encoding="latin-1") as fh:
                        ini = parse_ini(fh.read())
                except OSError as e:
                    logger.error("fileio", f"GetPrivateProfileStringA: file exists but cannot be read {real_path!r}: {e}")

        value = read_profile_string(ini, args.app_name, args.key_name, args.default)

        # Enumeration modes return null-separated names; Win32 callers expect
        # double-null termination, so append one extra null byte.
        is_enum = args.app_name is None or args.key_name is None
        if is_enum:
            value = value + "\0"

        encoded = value.encode("latin-1", errors="replace")

        if n_size == 0:
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 24)
            return

        # Clamp to buffer; always null-terminate.
        if len(encoded) >= n_size:
            encoded = encoded[:n_size - 1]
        for i, b in enumerate(encoded):
            memory.write8(lp_returned + i, b)
        memory.write8(lp_returned + len(encoded), 0)

        logger.debug(
            "handlers",
            f"GetPrivateProfileStringA({app_name!r}, {key_name!r}, "
            f"file={file_name!r}) -> {value!r}",
        )
        cpu.regs[EAX] = len(encoded)
        cleanup_stdcall(cpu, memory, 24)

    def _get_private_profile_int_a(cpu: "CPU") -> None:
        esp = cpu.regs[ESP]
        lp_app_name  = memory.read32((esp +  4) & 0xFFFFFFFF)
        lp_key_name  = memory.read32((esp +  8) & 0xFFFFFFFF)
        n_default    = memory.read32((esp + 12) & 0xFFFFFFFF)
        lp_file_name = memory.read32((esp + 16) & 0xFFFFFFFF)

        app_name  = read_cstring(lp_app_name,  memory) if lp_app_name  else ""
        key_name  = read_cstring(lp_key_name,  memory) if lp_key_name  else ""
        file_name = read_cstring(lp_file_name, memory) if lp_file_name else ""
        default   = n_default if n_default < 0x80000000 else n_default - 0x100000000

        args = GetPrivateProfileIntArgs(
            app_name=app_name, key_name=key_name,
            default=default, file_name=file_name,
        )

        ini: dict = {}
        if file_name:
            linux_path = state.translate_windows_path(file_name)
            real_path  = find_file_ci(linux_path)
            if real_path:
                # find_file_ci confirmed the path exists — any OSError here is not ENOENT.
                try:
                    with open(real_path, "r", encoding="latin-1") as fh:
                        ini = parse_ini(fh.read())
                except OSError as e:
                    logger.error("fileio", f"GetPrivateProfileIntA: file exists but cannot be read {real_path!r}: {e}")

        result = read_profile_int(ini, args.app_name, args.key_name, args.default)
        logger.debug(
            "handlers",
            f"GetPrivateProfileIntA({app_name!r}, {key_name!r}, "
            f"file={file_name!r}) -> {result}",
        )
        cpu.regs[EAX] = result & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 16)

    def _write_private_profile_string_a(cpu: "CPU") -> None:
        esp = cpu.regs[ESP]
        lp_app_name  = memory.read32((esp +  4) & 0xFFFFFFFF)
        lp_key_name  = memory.read32((esp +  8) & 0xFFFFFFFF)
        lp_string    = memory.read32((esp + 12) & 0xFFFFFFFF)
        lp_file_name = memory.read32((esp + 16) & 0xFFFFFFFF)

        app_name  = read_cstring(lp_app_name,  memory) if lp_app_name  else None
        key_name  = read_cstring(lp_key_name,  memory) if lp_key_name  else None
        value     = read_cstring(lp_string,    memory) if lp_string    else None
        file_name = read_cstring(lp_file_name, memory) if lp_file_name else ""

        linux_path = state.translate_windows_path(file_name) if file_name else ""
        ok = write_profile_string(linux_path, app_name, key_name, value)
        logger.debug(
            "handlers",
            f"WritePrivateProfileStringA({app_name!r}, {key_name!r}, "
            f"{value!r}, file={file_name!r}) -> {ok}",
        )
        cpu.regs[EAX] = 1 if ok else 0
        cleanup_stdcall(cpu, memory, 16)

    def _write_private_profile_section_a(cpu: "CPU") -> None:
        esp = cpu.regs[ESP]
        lp_app_name  = memory.read32((esp +  4) & 0xFFFFFFFF)
        lp_string    = memory.read32((esp +  8) & 0xFFFFFFFF)
        lp_file_name = memory.read32((esp + 12) & 0xFFFFFFFF)

        app_name  = read_cstring(lp_app_name,  memory) if lp_app_name  else None
        file_name = read_cstring(lp_file_name, memory) if lp_file_name else ""

        # lpString is a double-null-terminated list of "key=value" entries.
        pairs: dict[str, str] = {}
        if lp_string and app_name:
            ptr = lp_string
            while True:
                entry = read_cstring(ptr, memory)
                if not entry:
                    break                   # hit the double-null terminator
                if "=" in entry:
                    k, _, v = entry.partition("=")
                    pairs[k.strip().lower()] = v.strip()
                ptr += len(entry.encode("latin-1")) + 1   # advance past the null

        linux_path = state.translate_windows_path(file_name) if file_name else ""
        ok = write_profile_section(linux_path, app_name, pairs)
        logger.debug(
            "handlers",
            f"WritePrivateProfileSectionA({app_name!r}, file={file_name!r}) -> {ok}",
        )
        cpu.regs[EAX] = 1 if ok else 0
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("kernel32.dll", "GetPrivateProfileStringA",   _get_private_profile_string_a)
    stubs.register_handler("kernel32.dll", "GetPrivateProfileIntA",      _get_private_profile_int_a)
    stubs.register_handler("kernel32.dll", "WritePrivateProfileStringA",  _write_private_profile_string_a)
    stubs.register_handler("kernel32.dll", "WritePrivateProfileSectionA", _write_private_profile_section_a)

    # ── Interlocked operations ────────────────────────────────────────────────

    def _interlocked_inc(cpu: "CPU") -> None:
        p = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        v = (memory.read32(p) + 1) & 0xFFFFFFFF
        memory.write32(p, v)
        cpu.regs[EAX] = v
        cleanup_stdcall(cpu, memory, 4)

    def _interlocked_dec(cpu: "CPU") -> None:
        p = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        v = (memory.read32(p) - 1) & 0xFFFFFFFF
        memory.write32(p, v)
        cpu.regs[EAX] = v
        cleanup_stdcall(cpu, memory, 4)

    def _interlocked_exch(cpu: "CPU") -> None:
        p    = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        val  = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        orig = memory.read32(p)
        memory.write32(p, val)
        cpu.regs[EAX] = orig
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("kernel32.dll", "InterlockedIncrement", _interlocked_inc)
    stubs.register_handler("kernel32.dll", "InterlockedDecrement", _interlocked_dec)
    stubs.register_handler("kernel32.dll", "InterlockedExchange",  _interlocked_exch)

    # ── Debug output ──────────────────────────────────────────────────────────

    def _output_debug_string_a(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if lp:
            s = []
            for i in range(256):
                ch = memory.read8(lp + i)
                if ch == 0:
                    break
                s.append(chr(ch))
            logger.info("handlers", f"[OutputDebugString] {''.join(s)}")
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "OutputDebugStringA", _output_debug_string_a)
    stubs.register_handler("kernel32.dll", "DebugBreak",         _halt("DebugBreak"))

    # ── Error mode / string utils / memory alloc ──────────────────────────────

    def _set_error_mode(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    def _lstrlen_a(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        n = 0
        if lp:
            while n < 65535 and memory.read8(lp + n) != 0:
                n += 1
        cpu.regs[EAX] = n
        cleanup_stdcall(cpu, memory, 4)

    def _lstrcpy_a(cpu: "CPU") -> None:
        dst = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        src = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        i = 0
        while i < 65535:
            ch = memory.read8(src + i)
            memory.write8(dst + i, ch)
            if ch == 0:
                break
            i += 1
        if i == 65535:
            memory.write8(dst + i, 0)
        cpu.regs[EAX] = dst
        cleanup_stdcall(cpu, memory, 8)

    def _local_alloc(cpu: "CPU") -> None:
        flags  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        n_bytes = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        LMEM_ZEROINIT = 0x0040
        addr = state.simple_alloc(n_bytes)
        state.local_alloc_map[addr] = n_bytes
        if flags & LMEM_ZEROINIT:
            for i in range(n_bytes):
                memory.write8(addr + i, 0)
        cpu.regs[EAX] = addr
        cleanup_stdcall(cpu, memory, 8)

    def _local_free(cpu: "CPU") -> None:
        addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        state.local_alloc_map.pop(addr, None)
        cpu.regs[EAX] = 0  # NULL = success
        cleanup_stdcall(cpu, memory, 4)

    def _global_alloc(cpu: "CPU") -> None:
        flags  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        n_bytes = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        GMEM_ZEROINIT = 0x0040
        addr = state.simple_alloc(n_bytes)
        if flags & GMEM_ZEROINIT:
            for i in range(n_bytes):
                memory.write8(addr + i, 0)
        cpu.regs[EAX] = addr
        cleanup_stdcall(cpu, memory, 8)

    def _global_free(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "SetErrorMode", _set_error_mode)
    stubs.register_handler("kernel32.dll", "lstrlenA",     _lstrlen_a)
    stubs.register_handler("kernel32.dll", "lstrcpyA",     _lstrcpy_a)
    stubs.register_handler("kernel32.dll", "LocalAlloc",   _local_alloc)
    stubs.register_handler("kernel32.dll", "LocalFree",    _local_free)
    stubs.register_handler("kernel32.dll", "GlobalAlloc",  _global_alloc)
    stubs.register_handler("kernel32.dll", "GlobalFree",   _global_free)

    # ── Heap / handle ops ─────────────────────────────────────────────────────

    stubs.register_handler("kernel32.dll", "HeapValidate",     _halt("HeapValidate"))
    stubs.register_handler("kernel32.dll", "HeapDestroy",      _halt("HeapDestroy"))

    def _duplicate_handle(cpu: "CPU") -> None:
        """DuplicateHandle(hSourceProcess, hSource, hTargetProcess, lpTarget, access, inherit, options)

        stdcall, 7 args (28 bytes).  hSourceProcess and hTargetProcessHandle are
        ignored — the emulator is single-process and both are always the
        current-process pseudo-handle.
        """
        h_source   = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        lp_target  = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        dw_options = memory.read32((cpu.regs[ESP] + 28) & 0xFFFFFFFF)

        close_source = bool(dw_options & _DUPLICATE_CLOSE_SOURCE)
        new_handle   = _duplicate_handle_entry(state, h_source, close_source)

        if lp_target:
            memory.write32(lp_target & 0xFFFFFFFF, new_handle)

        logger.debug("handlers",
            f"DuplicateHandle(src=0x{h_source:08x}) -> 0x{new_handle:08x}")
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 28)

    stubs.register_handler("kernel32.dll", "DuplicateHandle", _duplicate_handle)

    # ── Locale / string type ──────────────────────────────────────────────────

    stubs.register_handler("kernel32.dll", "LCMapStringA",    _halt("LCMapStringA"))

    def _compare_string_a(cpu: "CPU") -> None:
        dw_flags = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        lp1      = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cch1     = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp2      = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        cch2     = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        NORM_IGNORECASE = 0x00000001
        LINGUISTIC_IGNORECASE = 0x00000010
        ignore_case = bool(dw_flags & (NORM_IGNORECASE | LINGUISTIC_IGNORECASE))
        def read_ansi(ptr: int, count: int) -> str:
            s = []
            mx = 4096 if count == 0xFFFFFFFF else count
            for i in range(mx):
                ch = memory.read8(ptr + i)
                if count == 0xFFFFFFFF and ch == 0:
                    break
                s.append(chr(ch))
            return "".join(s)
        s1 = read_ansi(lp1, cch1)
        s2 = read_ansi(lp2, cch2)
        if ignore_case:
            s1 = s1.upper(); s2 = s2.upper()
        cpu.regs[EAX] = 1 if s1 < s2 else (3 if s1 > s2 else 2)
        cleanup_stdcall(cpu, memory, 24)

    def _compare_string_w(cpu: "CPU") -> None:
        dw_flags = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        lp1      = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cch1     = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp2      = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        cch2     = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        NORM_IGNORECASE = 0x00000001
        LINGUISTIC_IGNORECASE = 0x00000010
        ignore_case = bool(dw_flags & (NORM_IGNORECASE | LINGUISTIC_IGNORECASE))
        def read_wide(ptr: int, count: int) -> str:
            s = []
            mx = 4096 if count == 0xFFFFFFFF else count
            for i in range(mx):
                ch = memory.read16(ptr + i * 2)
                if count == 0xFFFFFFFF and ch == 0:
                    break
                s.append(chr(ch))
            return "".join(s)
        s1 = read_wide(lp1, cch1)
        s2 = read_wide(lp2, cch2)
        if ignore_case:
            s1 = s1.upper(); s2 = s2.upper()
        cpu.regs[EAX] = 1 if s1 < s2 else (3 if s1 > s2 else 2)
        cleanup_stdcall(cpu, memory, 24)

    def _get_oemc_p(cpu: "CPU") -> None:
        cpu.regs[EAX] = 437

    def _get_user_default_lcid(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x0409

    def _is_valid_locale(cpu: "CPU") -> None:
        locale = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = 1 if locale == 0x0409 else 0
        cleanup_stdcall(cpu, memory, 8)

    def _get_locale_info_w(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("kernel32.dll", "CompareStringA",       _compare_string_a)
    stubs.register_handler("kernel32.dll", "CompareStringW",       _compare_string_w)
    stubs.register_handler("kernel32.dll", "GetStringTypeA",       _halt("GetStringTypeA"))
    stubs.register_handler("kernel32.dll", "GetOEMCP",             _get_oemc_p)
    stubs.register_handler("kernel32.dll", "GetUserDefaultLCID",   _get_user_default_lcid)
    stubs.register_handler("kernel32.dll", "IsValidLocale",        _is_valid_locale)
    stubs.register_handler("kernel32.dll", "EnumSystemLocalesA",   _halt("EnumSystemLocalesA"))
    stubs.register_handler("kernel32.dll", "GetLocaleInfoW",       _get_locale_info_w)
    stubs.register_handler("kernel32.dll", "SetConsoleCtrlHandler", _halt("SetConsoleCtrlHandler"))
    def _set_environment_variable_a(cpu: "CPU") -> None:
        """BOOL SetEnvironmentVariableA(LPCSTR lpName, LPCSTR lpValue)"""
        esp      = cpu.regs[ESP]
        lp_name  = memory.read32((esp + 4) & 0xFFFFFFFF)
        lp_value = memory.read32((esp + 8) & 0xFFFFFFFF)
        name = read_cstring(lp_name, memory) if lp_name else ""
        if name:
            if lp_value:
                _env_vars[name.upper()] = read_cstring(lp_value, memory)
            else:
                _env_vars.pop(name.upper(), None)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _set_environment_variable_w(cpu: "CPU") -> None:
        """BOOL SetEnvironmentVariableW(LPCWSTR lpName, LPCWSTR lpValue)"""
        esp      = cpu.regs[ESP]
        lp_name  = memory.read32((esp + 4) & 0xFFFFFFFF)
        lp_value = memory.read32((esp + 8) & 0xFFFFFFFF)
        name = read_wide_string(lp_name, memory) if lp_name else ""
        if name:
            if lp_value:
                _env_vars[name.upper()] = read_wide_string(lp_value, memory)
            else:
                _env_vars.pop(name.upper(), None)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _get_environment_variable_a(cpu: "CPU") -> None:
        """DWORD GetEnvironmentVariableA(LPCSTR lpName, LPSTR lpBuffer, DWORD nSize)"""
        esp     = cpu.regs[ESP]
        lp_name = memory.read32((esp +  4) & 0xFFFFFFFF)
        lp_buf  = memory.read32((esp +  8) & 0xFFFFFFFF)
        n_size  = memory.read32((esp + 12) & 0xFFFFFFFF)
        name  = read_cstring(lp_name, memory).upper() if lp_name else ""
        value = _env_vars.get(name, "")
        encoded = value.encode("latin-1", errors="replace")
        if n_size > len(encoded):
            for i, b in enumerate(encoded):
                memory.write8(lp_buf + i, b)
            memory.write8(lp_buf + len(encoded), 0)
            cpu.regs[EAX] = len(encoded)
        else:
            # Buffer too small: return required size (including null).
            cpu.regs[EAX] = len(encoded) + 1
        cleanup_stdcall(cpu, memory, 12)

    def _get_environment_variable_w(cpu: "CPU") -> None:
        """DWORD GetEnvironmentVariableW(LPCWSTR lpName, LPWSTR lpBuffer, DWORD nSize)"""
        esp     = cpu.regs[ESP]
        lp_name = memory.read32((esp +  4) & 0xFFFFFFFF)
        lp_buf  = memory.read32((esp +  8) & 0xFFFFFFFF)
        n_size  = memory.read32((esp + 12) & 0xFFFFFFFF)
        name  = read_wide_string(lp_name, memory).upper() if lp_name else ""
        value = _env_vars.get(name, "")
        if n_size > len(value):
            for i, ch in enumerate(value):
                memory.write16(lp_buf + i * 2, ord(ch))
            memory.write16(lp_buf + len(value) * 2, 0)
            cpu.regs[EAX] = len(value)
        else:
            cpu.regs[EAX] = len(value) + 1
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("kernel32.dll", "SetEnvironmentVariableA",  _set_environment_variable_a)
    stubs.register_handler("kernel32.dll", "SetEnvironmentVariableW",  _set_environment_variable_w)
    stubs.register_handler("kernel32.dll", "GetEnvironmentVariableA",  _get_environment_variable_a)
    stubs.register_handler("kernel32.dll", "GetEnvironmentVariableW",  _get_environment_variable_w)
    def _virtual_protect(cpu: "CPU") -> None:
        """
        BOOL VirtualProtect(LPVOID lpAddress, SIZE_T dwSize,
                            DWORD flNewProtect, PDWORD lpflOldProtect)

        The emulator uses a flat, unprotected memory model — all pages are
        always read/write/execute.  We record the "old" protection as
        PAGE_EXECUTE_READ_WRITE (0x40) so callers that save and restore it
        get a consistent value, and return TRUE.
        """
        esp            = cpu.regs[ESP]
        lp_old_protect = memory.read32((esp + 16) & 0xFFFFFFFF)
        if lp_old_protect:
            memory.write32(lp_old_protect, 0x40)   # PAGE_EXECUTE_READWRITE
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("kernel32.dll", "VirtualProtect", _virtual_protect)


def register_winmm_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register WINMM.DLL multimedia timer stubs.

    The game uses a self-rescheduling one-shot timer pattern:
      mmtimer_callback → timeSetEvent(delay, 1, mmtimer_callback, 0, TIME_ONESHOT)
    Each callback fires once, signals the timer thread, then reschedules itself.
    _sleep_ex fires due PendingTimers cooperatively during SleepEx calls.
    """
    from tew.api.win32_handlers import pending_timers, PendingTimer

    _next_timer_id = [1]

    # ── timeGetDevCaps ────────────────────────────────────────────────────────
    # MMRESULT timeGetDevCaps(LPTIMECAPS ptc, UINT cbtc)
    # TIMECAPS: {UINT wPeriodMin, UINT wPeriodMax} = 8 bytes
    def _time_get_dev_caps(cpu: "CPU") -> None:
        ptc  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cbtc = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if ptc and cbtc >= 8:
            memory.write32(ptc,     1)       # wPeriodMin = 1 ms
            memory.write32(ptc + 4, 0x7FFF)  # wPeriodMax = 32767 ms
        cpu.regs[EAX] = 0  # TIMERR_NOERROR
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("winmm.dll", "timeGetDevCaps", _time_get_dev_caps)

    # ── timeBeginPeriod / timeEndPeriod ───────────────────────────────────────
    def _time_begin_period(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # TIMERR_NOERROR
        cleanup_stdcall(cpu, memory, 4)

    def _time_end_period(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # TIMERR_NOERROR
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("winmm.dll", "timeBeginPeriod", _time_begin_period)
    stubs.register_handler("winmm.dll", "timeEndPeriod",   _time_end_period)

    # ── timeSetEvent ──────────────────────────────────────────────────────────
    # MMRESULT timeSetEvent(UINT uDelay, UINT uResolution,
    #                       LPTIMECALLBACK lpTimeProc, DWORD_PTR dwUser, UINT fuEvent)
    # fuEvent: TIME_ONESHOT=0x0000, TIME_PERIODIC=0x0001
    _TIME_PERIODIC = 0x0001

    def _time_set_event(cpu: "CPU") -> None:
        sp           = cpu.regs[ESP]
        u_delay      = memory.read32((sp +  4) & 0xFFFFFFFF)
        lp_time_proc = memory.read32((sp + 12) & 0xFFFFFFFF)
        dw_user      = memory.read32((sp + 16) & 0xFFFFFFFF)
        fu_event     = memory.read32((sp + 20) & 0xFFFFFFFF)

        if u_delay == 0:
            u_delay = 1
        period_ms = u_delay if (fu_event & _TIME_PERIODIC) else 0
        due_at    = state.virtual_ticks_ms + u_delay

        tid = _next_timer_id[0]
        _next_timer_id[0] += 1
        pending_timers[tid] = PendingTimer(
            id=tid, due_at=due_at, period_ms=period_ms,
            cb_addr=lp_time_proc, dw_user=dw_user,
        )
        logger.debug("handlers",
            f"timeSetEvent(delay={u_delay}ms, proc=0x{lp_time_proc:08x}) -> id={tid}")
        cpu.regs[EAX] = tid
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("winmm.dll", "timeSetEvent", _time_set_event)

    # ── timeKillEvent ─────────────────────────────────────────────────────────
    def _time_kill_event(cpu: "CPU") -> None:
        tid = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        pending_timers.pop(tid, None)
        cpu.regs[EAX] = 0  # TIMERR_NOERROR
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("winmm.dll", "timeKillEvent", _time_kill_event)

    # ── timeGetTime ───────────────────────────────────────────────────────────
    # DWORD timeGetTime(void) — milliseconds since system start
    def _time_get_time(cpu: "CPU") -> None:
        cpu.regs[EAX] = state.virtual_ticks_ms & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 0)

    stubs.register_handler("winmm.dll", "timeGetTime", _time_get_time)
