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
from tew.api._state import (
    CRTState, FileHandleEntry, MutexHandle, EventHandle,
    PendingThreadInfo, SavedCPUState,
    find_file_ci, read_cstring, read_wide_string,
    THREAD_STACK_BASE, THREAD_STACK_SIZE,
)
from tew.logger import logger


# ── Thread scheduling helpers (imported from kernel32_handlers) ───────────────

def _save_cpu_state(cpu: "CPU") -> SavedCPUState:
    return SavedCPUState(
        regs=list(cpu.regs),
        eip=cpu.eip,
        eflags=cpu.eflags,
        fpu_stack=list(cpu.fpu_stack),
        fpu_top=cpu.fpu_top,
        fpu_status_word=cpu.fpu_status_word,
        fpu_control_word=cpu.fpu_control_word,
        fpu_tag_word=cpu.fpu_tag_word,
    )


def _restore_cpu_state(cpu: "CPU", s: SavedCPUState) -> None:
    for i, v in enumerate(s.regs):
        cpu.regs[i] = v
    cpu.eip = s.eip
    cpu.eflags = s.eflags
    for i, v in enumerate(s.fpu_stack):
        cpu.fpu_stack[i] = v
    cpu.fpu_top = s.fpu_top
    cpu.fpu_status_word = s.fpu_status_word
    cpu.fpu_control_word = s.fpu_control_word
    cpu.fpu_tag_word = s.fpu_tag_word


def _run_thread_slice(
    cpu: "CPU", memory: "Memory", thread: PendingThreadInfo, state: CRTState
) -> None:
    step_limit = 1_000_000
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
    else:
        thread.saved_state = _save_cpu_state(cpu)
        logger.debug("scheduler",
            f"Thread {thread.thread_id} yielded after {steps} steps "
            f"(EIP=0x{cpu.eip:x})")


def _cooperative_sleep_ex(
    cpu: "CPU", memory: "Memory", state: CRTState, arg_bytes: int, eax_val: int
) -> bool:
    """Try to schedule a thread (SleepEx variant). Returns True if handled."""
    num = len(state.pending_threads)
    runnable: Optional[PendingThreadInfo] = None
    tidx = -1
    for i in range(1, num + 1):
        idx = (state.last_scheduled_idx + i) % num
        t = state.pending_threads[idx]
        if not t.suspended and not t.completed:
            runnable = t
            tidx = idx
            break
    if runnable is None or tidx < 0:
        return False

    state.last_scheduled_idx = tidx
    logger.debug("scheduler",
        f"Main thread SleepEx #{state.sleep_count} - thread {runnable.thread_id}")

    main_state = _save_cpu_state(cpu)
    state.is_running_thread = True
    state.current_thread_idx = tidx

    if runnable.saved_state:
        _restore_cpu_state(cpu, runnable.saved_state)
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

    _restore_cpu_state(cpu, main_state)
    cpu.halted = False
    state.is_running_thread = False
    state.current_thread_idx = -1

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
            except OSError:
                pass
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
    stubs.register_handler("kernel32.dll", "GetModuleFileNameA", _halt("GetModuleFileNameA"))
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
        state.sleep_count += 1
        if _cooperative_sleep_ex(cpu, memory, state, 8, 0):
            return
        if state.sleep_count >= 50:
            logger.warn("handlers",
                f"[Win32] SleepEx() called {state.sleep_count} times — halting")
            cpu.halted = True
            return
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("kernel32.dll", "SleepEx", _sleep_ex)

    # ── Wait functions ────────────────────────────────────────────────────────

    def _wait_for_single(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        obj = state.kernel_handle_map.get(h)
        if obj is not None:
            if isinstance(obj, MutexHandle):
                obj.locked = True
                cpu.regs[EAX] = 0
            else:  # EventHandle
                if obj.signaled:
                    if not obj.manual_reset:
                        obj.signaled = False
                    cpu.regs[EAX] = 0
                else:
                    cpu.regs[EAX] = 0x102  # WAIT_TIMEOUT
        else:
            cpu.regs[EAX] = 0  # thread handle or unknown — assume signaled
        cleanup_stdcall(cpu, memory, 8)

    def _wait_for_multiple_ex(cpu: "CPU") -> None:
        base      = cpu.regs[ESP]
        n_count   = memory.read32((base +  4) & 0xFFFFFFFF)
        lp_handles = memory.read32((base +  8) & 0xFFFFFFFF)
        b_wait_all = memory.read32((base + 12) & 0xFFFFFFFF) != 0
        for i in range(n_count):
            h   = memory.read32((lp_handles + i * 4) & 0xFFFFFFFF)
            obj = state.kernel_handle_map.get(h)
            if obj is None:
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
            elif b_wait_all:
                cpu.regs[EAX] = 0x102
                cleanup_stdcall(cpu, memory, 20)
                return
        if b_wait_all:
            for i in range(n_count):
                h   = memory.read32((lp_handles + i * 4) & 0xFFFFFFFF)
                obj = state.kernel_handle_map.get(h)
                if obj is not None:
                    if isinstance(obj, EventHandle) and not obj.manual_reset:
                        obj.signaled = False
                    if isinstance(obj, MutexHandle):
                        obj.locked = True
            cpu.regs[EAX] = 0
        else:
            cpu.regs[EAX] = 0x102
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
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

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
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

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
        cpu.regs[EAX] = 3  # DRIVE_FIXED
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GetCurrentDirectoryA", _get_current_dir_a)
    stubs.register_handler("kernel32.dll", "SetCurrentDirectoryA", _set_current_dir_a)
    stubs.register_handler("kernel32.dll", "GetWindowsDirectoryA", _get_windows_dir_a)
    stubs.register_handler("kernel32.dll", "GetDiskFreeSpaceA",    _get_disk_free_space_a)
    stubs.register_handler("kernel32.dll", "GetDriveTypeA",        _get_drive_type_a)
    stubs.register_handler("kernel32.dll", "GlobalMemoryStatus",   _halt("GlobalMemoryStatus"))

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
        import time as _time
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
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "DeviceIoControl", _device_io_control)
    stubs.register_handler("kernel32.dll", "WinExec",         _win_exec)
    stubs.register_handler("kernel32.dll", "_lopen",          _lopen)
    stubs.register_handler("kernel32.dll", "_lclose",         _lclose)

    stubs.register_handler("kernel32.dll", "GetPrivateProfileStringA",  _halt("GetPrivateProfileStringA"))
    stubs.register_handler("kernel32.dll", "GetPrivateProfileIntA",     _halt("GetPrivateProfileIntA"))
    stubs.register_handler("kernel32.dll", "WritePrivateProfileStringA", _halt("WritePrivateProfileStringA"))
    stubs.register_handler("kernel32.dll", "WritePrivateProfileSectionA", _halt("WritePrivateProfileSectionA"))

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

    # ── Unimplemented heap/handle ops ─────────────────────────────────────────

    stubs.register_handler("kernel32.dll", "HeapValidate",     _halt("HeapValidate"))
    stubs.register_handler("kernel32.dll", "HeapDestroy",      _halt("HeapDestroy"))
    stubs.register_handler("kernel32.dll", "DuplicateHandle",  _halt("DuplicateHandle"))

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
    stubs.register_handler("kernel32.dll", "SetEnvironmentVariableA", _halt("SetEnvironmentVariableA"))
    stubs.register_handler("kernel32.dll", "SetEnvironmentVariableW", _halt("SetEnvironmentVariableW"))
    stubs.register_handler("kernel32.dll", "VirtualProtect",       _halt("VirtualProtect"))
