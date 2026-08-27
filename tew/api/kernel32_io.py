"""kernel32.dll stub handlers — I/O, threading, sync objects, time, and misc.

Registered by register_kernel32_io_handlers(), called from kernel32_handlers.py.
Covers handlers from CloseHandle through GetWindowsDirectoryA.
"""

from __future__ import annotations

import fnmatch
import os
import stat
import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory
    from tew.loader.dll_loader import DLLLoader

from tew.hardware.cpu_zig import EAX, EBX, ECX, EDX, ESP, EBP, ESI, EDI
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall, unimplemented_halt as _halt
from tew.api.win32_errors import Win32Error
from tew.api.ini_file import (
    GetPrivateProfileStringArgs,
    GetPrivateProfileIntArgs,
    parse_ini,
    read_profile_string,
    read_profile_int,
    write_profile_string,
    write_profile_section,
)
from tew.api._state import (
    CRTState,
    MutexHandle,
    EventHandle,
    find_file_ci,
    read_cstring,
    read_wide_string,
    TEB_BASE,
)
from tew.api.kernel32_system import _fire_due_timers
from tew.logger import logger

# ── Environment variable store ────────────────────────────────────────────────
# Shared by Set/GetEnvironmentVariable{A,W} handlers.
_env_vars: dict[str, str] = {}

# ── Win32 handle constants ─────────────────────────────────────────────────────

_CURRENT_PROCESS_HANDLE = 0xFFFFFFFF  # GetCurrentProcess() pseudo-handle
_CURRENT_THREAD_HANDLE = 0xFFFFFFFE  # GetCurrentThread() pseudo-handle
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
        state.kernel_handle_map[new_handle] = EventHandle(
            signaled=True, manual_reset=True
        )

    elif h_source in state.file_handle_map:
        entry = state.file_handle_map[h_source]
        new_handle = state.next_file_handle
        state.next_file_handle += 1
        # shared ref → shared file position
        state.file_handle_map[new_handle] = entry

    elif h_source in state.kernel_handle_map:
        obj = state.kernel_handle_map[h_source]
        new_handle = state.next_kernel_handle
        state.next_kernel_handle += 1
        state.kernel_handle_map[new_handle] = obj  # shared ref

    else:
        # Thread handle or other value not tracked in a lookup table.
        # Register a dummy so CloseHandle on the result does not warn.
        logger.debug(
            "handlers",
            f"DuplicateHandle: untracked src=0x{h_source:08x} — registering dummy",
        )
        new_handle = state.next_kernel_handle
        state.next_kernel_handle += 1
        state.kernel_handle_map[new_handle] = EventHandle(
            signaled=True, manual_reset=True
        )

    if close_source:
        state.file_handle_map.pop(h_source, None)
        state.kernel_handle_map.pop(h_source, None)

    return new_handle


def register_kernel32_io_handlers(
    stubs: Win32Handlers,
    memory: "Memory",
    state: CRTState,
    dll_loader: Optional["DLLLoader"] = None,
) -> None:
    """Register kernel32.dll handlers for I/O, threading, sync, time, and misc."""

    # ── Handle management ─────────────────────────────────────────────────────

    def _close_handle(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(h)
        if entry is not None and entry.fd is not None and entry.fd >= 3:
            try:
                os.close(entry.fd)
            except OSError as e:
                logger.warn(
                    "fileio", f"CloseHandle: os.close(fd={entry.fd}) failed: {e}"
                )
        if entry is not None:
            # Real Windows releases every byte-range lock a handle holds the
            # moment it's closed, whether or not UnlockFile was called first.
            locks = state.file_locks.get(entry.path)
            if locks:
                remaining = [l for l in locks if l[2] != h]
                if len(remaining) != len(locks):
                    state.file_locks[entry.path] = remaining
        state.file_handle_map.pop(h, None)
        state.kernel_handle_map.pop(h, None)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "CloseHandle", _close_handle)

    # ── File I/O ──────────────────────────────────────────────────────────────

    def _write_file(cpu: "CPU") -> None:
        h_file = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_buf = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        n_bytes = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_written = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_overlapped = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        # See _read_file's comment: a real positioned write via OVERLAPPED
        # must not disturb the handle's own sequential cursor either.
        overlapped_pos = None
        if lp_overlapped:
            off_low = memory.read32((lp_overlapped + 8) & 0xFFFFFFFF)
            off_high = memory.read32((lp_overlapped + 0xC) & 0xFFFFFFFF)
            overlapped_pos = off_low | (off_high << 32)
        entry = state.file_handle_map.get(h_file)
        if not entry or not entry.writable or entry.fd is None:
            if lp_written:
                memory.write32(lp_written, 0)
            if not entry:
                logger.warn(
                    "fileio",
                    f"[Win32] WriteFile(handle=0x{h_file:x}) -> FALSE (unknown handle)",
                )
            elif not entry.writable:
                logger.warn(
                    "fileio",
                    f"[Win32] WriteFile(handle=0x{h_file:x}) -> FALSE (read-only)",
                )
            else:
                logger.warn(
                    "fileio",
                    f'[Win32] WriteFile(handle=0x{h_file:x}, "{entry.path}") -> FALSE (no fd)',
                )
            cpu.regs[EAX] = 0
        else:
            data = memory.read_bytes(lp_buf & 0xFFFFFFFF, n_bytes)
            if overlapped_pos is not None:
                os.pwrite(entry.fd, data, overlapped_pos)
            else:
                os.write(entry.fd, data)
                entry.position += n_bytes
            if lp_written:
                memory.write32(lp_written, n_bytes)
            logger.debug(
                "fileio",
                f"[Win32] WriteFile(handle=0x{h_file:x}, nBytes={n_bytes}) -> TRUE"
                + (
                    f" [overlapped offset={overlapped_pos}]"
                    if overlapped_pos is not None
                    else ""
                ),
            )
            cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 20)

    def _set_handle_count(cpu: "CPU") -> None:
        u = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = u
        cleanup_stdcall(cpu, memory, 4)

    def _set_std_handle(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("kernel32.dll", "WriteFile", _write_file)
    stubs.register_handler("kernel32.dll", "SetHandleCount", _set_handle_count)
    stubs.register_handler("kernel32.dll", "SetStdHandle", _set_std_handle)

    def _get_module_file_name_a(cpu: "CPU") -> None:
        h_module = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_filename = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        n_size = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)

        if h_module == 0:
            # NULL → return the path of the running executable.
            if not state.exe_path:
                logger.error(
                    "handlers",
                    "GetModuleFileNameA: exe_path not set in CRTState — halting",
                )
                cpu.halted = True
                cpu.fatal_halt = True
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
                cpu.fatal_halt = True
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

    stubs.register_handler(
        "kernel32.dll", "GetModuleFileNameA", _get_module_file_name_a
    )

    def _get_module_file_name_w(cpu: "CPU") -> None:
        # Same as GetModuleFileNameA above, except nSize is a WCHAR count
        # (not bytes) and the path is written as null-terminated UTF-16LE.
        h_module = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_filename = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        n_size = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)

        if h_module == 0:
            if not state.exe_path:
                logger.error(
                    "handlers",
                    "GetModuleFileNameW: exe_path not set in CRTState — halting",
                )
                cpu.halted = True
                cpu.fatal_halt = True
                return
            win_path = state.reverse_translate_path(state.exe_path)
        else:
            mod = state.dynamic_modules.get(h_module)
            if mod is None:
                logger.error(
                    "handlers",
                    f"GetModuleFileNameW: unknown hModule 0x{h_module:x} — halting",
                )
                cpu.halted = True
                cpu.fatal_halt = True
                return
            win_path = mod.dll_path if mod.dll_path else mod.dll_name

        chars_to_copy = min(len(win_path), max(n_size - 1, 0))
        for i in range(chars_to_copy):
            cp = ord(win_path[i])
            memory.write8((lp_filename + i * 2) & 0xFFFFFFFF, cp & 0xFF)
            memory.write8((lp_filename + i * 2 + 1) & 0xFFFFFFFF, (cp >> 8) & 0xFF)
        memory.write8((lp_filename + chars_to_copy * 2) & 0xFFFFFFFF, 0)
        memory.write8((lp_filename + chars_to_copy * 2 + 1) & 0xFFFFFFFF, 0)

        # Return chars copied (not including null), or n_size when truncated.
        cpu.regs[EAX] = n_size if len(win_path) >= n_size else len(win_path)
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler(
        "kernel32.dll", "GetModuleFileNameW", _get_module_file_name_w
    )

    # ── Pointer validation ────────────────────────────────────────────────────

    def _is_bad_read_ptr(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        ucb = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        mem_size = memory.size
        cpu.regs[EAX] = 1 if (lp == 0 or lp + ucb > mem_size) else 0
        cleanup_stdcall(cpu, memory, 8)

    def _is_bad_write_ptr(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        ucb = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        mem_size = memory.size
        cpu.regs[EAX] = 1 if (lp == 0 or lp + ucb > mem_size) else 0
        cleanup_stdcall(cpu, memory, 8)

    def _is_bad_code_ptr(cpu: "CPU") -> None:
        lpfn = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        mem_size = memory.size
        cpu.regs[EAX] = 1 if (lpfn == 0 or lpfn >= mem_size) else 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "IsBadReadPtr", _is_bad_read_ptr)
    stubs.register_handler("kernel32.dll", "IsBadWritePtr", _is_bad_write_ptr)
    stubs.register_handler("kernel32.dll", "IsBadCodePtr", _is_bad_code_ptr)

    # ── Process termination ───────────────────────────────────────────────────

    def _terminate_process(cpu: "CPU") -> None:
        code = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        logger.info("handlers", f"[Win32] TerminateProcess(exitCode={code})")
        cpu.halted = True
        cpu.fatal_halt = True

    def _fatal_app_exit(cpu: "CPU") -> None:
        logger.error("handlers", "[Win32] FatalAppExitA called")
        cpu.halted = True
        cpu.fatal_halt = True

    stubs.register_handler("kernel32.dll", "TerminateProcess", _terminate_process)
    stubs.register_handler("kernel32.dll", "FatalAppExitA", _fatal_app_exit)
    # RtlUnwind/RaiseException: real implementations, not _halt placeholders
    # -- see tew/kernel/seh.py. register_handler dedupes by key, so this
    # must run before anything else tries to register the same names.
    from tew.kernel.seh import register_seh_handlers

    register_seh_handlers(stubs, memory)

    # ── Thread creation and management ────────────────────────────────────────

    def _create_thread(cpu: "CPU") -> None:
        lp_start = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_param = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        lp_tid = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        CREATE_SUSPENDED = 0x4
        is_susp = bool(dw_flags & CREATE_SUSPENDED)
        tid = state.next_thread_id
        state.next_thread_id += 1
        handle = state.next_thread_handle
        state.next_thread_handle += 1
        ret_addr = memory.read32(cpu.regs[ESP] & 0xFFFFFFFF)
        logger.debug(
            "thread",
            f"CreateThread(start=0x{lp_start:x}, param=0x{lp_param:x}, "
            f"flags=0x{dw_flags:x}) -> handle=0x{handle:x}, tid={tid}  "
            f"called_from=0x{ret_addr:x}",
        )
        idx = state.scheduler.thread_count
        state.scheduler.create_thread(
            thread_id=tid,
            handle=handle,
            start_address=lp_start,
            parameter=lp_param,
            suspended=is_susp,
        )
        logger.debug("thread", f"  tid={tid} assigned scheduler idx={idx}")
        state.pending_threads.append(handle)
        if lp_tid:
            memory.write32(lp_tid, tid)
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, 24)

    def _resume_thread(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if h in state.pending_threads and state.scheduler.get_suspended(h):
            logger.debug(
                "thread",
                f"ResumeThread(0x{h:x}) - unsuspending thread {state.scheduler.get_thread_id(h)}",
            )
            state.scheduler.set_suspended(h, False)
        cpu.regs[EAX] = 1  # previous suspend count
        cleanup_stdcall(cpu, memory, 4)

    def _exit_thread(cpu: "CPU") -> None:
        code = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        logger.debug("thread", f"ExitThread({code})")
        state.scheduler.mark_current_dead(cpu, memory)

    def _terminate_thread(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        exit_code = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        logger.info(
            "thread", f"TerminateThread(handle=0x{h:x}, exitCode=0x{exit_code:x})"
        )
        result = state.scheduler.terminate_thread(cpu, memory, h)
        if result is None:
            cpu.regs[EAX] = 0  # invalid handle
            cleanup_stdcall(cpu, memory, 8)
        elif result is True:
            cpu.regs[EAX] = 1
            cleanup_stdcall(cpu, memory, 8)
        # result is False: current thread terminated itself -- scheduler
        # already switched the live CPU away, must not touch it here.

    def _get_exit_code_thread(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_code = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        is_pending = h in state.pending_threads
        if lp_code:
            memory.write32(
                lp_code, 0 if (is_pending and state.scheduler.get_completed(h)) else 259
            )
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _suspend_thread(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if h in state.pending_threads:
            logger.debug(
                "thread",
                f"SuspendThread(0x{h:x}) - suspending thread {state.scheduler.get_thread_id(h)}",
            )
            state.scheduler.set_suspended(h, True)
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    def _set_thread_priority(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _get_thread_priority(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # THREAD_PRIORITY_NORMAL
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "CreateThread", _create_thread)
    stubs.register_handler("kernel32.dll", "ResumeThread", _resume_thread)
    stubs.register_handler("kernel32.dll", "ExitThread", _exit_thread)
    stubs.register_handler("kernel32.dll", "TerminateThread", _terminate_thread)
    stubs.register_handler("kernel32.dll", "GetExitCodeThread", _get_exit_code_thread)
    stubs.register_handler("kernel32.dll", "OpenThread", _halt("OpenThread"))
    stubs.register_handler("kernel32.dll", "CreateProcessA", _halt("CreateProcessA"))
    stubs.register_handler("kernel32.dll", "CreateProcessW", _halt("CreateProcessW"))
    stubs.register_handler("kernel32.dll", "OpenProcess", _halt("OpenProcess"))
    stubs.register_handler("kernel32.dll", "SuspendThread", _suspend_thread)
    stubs.register_handler("kernel32.dll", "SetThreadPriority", _set_thread_priority)
    stubs.register_handler("kernel32.dll", "GetThreadPriority", _get_thread_priority)

    # ── SleepEx ───────────────────────────────────────────────────────────────

    def _sleep_ex(cpu: "CPU") -> None:
        dw_ms = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        return_eip = memory.read32(cpu.regs[ESP] & 0xFFFFFFFF)
        logger.debug("scheduler", f"SleepEx(ms={dw_ms}) ret=0x{return_eip:x}")
        cpu.regs[ESP] = (
            cpu.regs[ESP] + 12
        ) & 0xFFFFFFFF  # stdcall: pop ret addr + 8-byte args
        state.scheduler.tick(dw_ms, memory)
        _fire_due_timers(cpu, memory, state)
        state.scheduler.sleep_current(cpu, memory, return_eip, 0, dw_ms)

    stubs.register_handler("kernel32.dll", "SleepEx", _sleep_ex)

    # ── Wait functions ────────────────────────────────────────────────────────

    _WAIT_INFINITE = 0xFFFFFFFF

    def _wait_for_single(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        timeout_ms = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        obj = state.kernel_handle_map.get(h)
        if obj is not None:
            tid = state.tls_current_thread_id()
            # Recursive mutex acquisition by the owning thread always succeeds immediately.
            if isinstance(obj, MutexHandle) and obj.owner_tid == tid:
                obj.recursion_count += 1
                cpu.regs[EAX] = 0
                cleanup_stdcall(cpu, memory, 8)
                return
            ready = (isinstance(obj, MutexHandle) and obj.owner_tid is None) or (
                isinstance(obj, EventHandle) and obj.signaled
            )
            if ready:
                if isinstance(obj, MutexHandle):
                    obj.owner_tid = tid
                    obj.recursion_count = 1
                    obj.locked = True
                elif isinstance(obj, EventHandle) and not obj.manual_reset:
                    obj.signaled = False
                cpu.regs[EAX] = 0
                cleanup_stdcall(cpu, memory, 8)
                return
            # Not ready — check if we timed out on a retry, then block.
            current = state.scheduler.current_thread()
            if current.wait_timed_out:
                current.wait_timed_out = False
                cpu.regs[EAX] = 0x102  # WAIT_TIMEOUT
                cleanup_stdcall(cpu, memory, 8)
                return
            retry_eip = (cpu.eip - 2) & 0xFFFFFFFF
            deadline_ms = (
                None
                if timeout_ms == _WAIT_INFINITE
                else (state.virtual_ticks_ms + timeout_ms) & 0xFFFFFFFF
            )
            state.scheduler.block_current_on_handles(
                cpu, memory, frozenset([h]), retry_eip, deadline_ms
            )
            return
        # Unknown handle (thread handle etc.) — treat as signaled.
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    def _wait_for_multiple_ex(cpu: "CPU") -> None:
        base = cpu.regs[ESP]
        n_count = memory.read32((base + 4) & 0xFFFFFFFF)
        lp_handles = memory.read32((base + 8) & 0xFFFFFFFF)
        b_wait_all = memory.read32((base + 12) & 0xFFFFFFFF) != 0
        timeout_ms = memory.read32((base + 16) & 0xFFFFFFFF)
        tid = state.tls_current_thread_id()
        all_ready = True
        for i in range(n_count):
            h = memory.read32((lp_handles + i * 4) & 0xFFFFFFFF)
            obj = state.kernel_handle_map.get(h)
            if obj is None:
                # Unknown handle (e.g. thread handle) — treat as always-ready.
                if not b_wait_all:
                    cpu.regs[EAX] = i & 0xFFFFFFFF
                    cleanup_stdcall(cpu, memory, 20)
                    return
                continue
            ready = (isinstance(obj, MutexHandle) and obj.owner_tid is None) or (
                isinstance(obj, EventHandle) and obj.signaled
            )
            if ready:
                if not b_wait_all:
                    if isinstance(obj, EventHandle) and not obj.manual_reset:
                        obj.signaled = False
                    if isinstance(obj, MutexHandle):
                        obj.owner_tid = tid
                        obj.recursion_count = 1
                        obj.locked = True
                    logger.debug(
                        "scheduler", f"WaitForMultipleEx: satisfied h=0x{h:x} idx={i}"
                    )
                    cpu.regs[EAX] = i & 0xFFFFFFFF
                    cleanup_stdcall(cpu, memory, 20)
                    return
            else:
                all_ready = False
                if b_wait_all:
                    break
        if b_wait_all and all_ready:
            for i in range(n_count):
                h = memory.read32((lp_handles + i * 4) & 0xFFFFFFFF)
                obj = state.kernel_handle_map.get(h)
                if obj is not None:
                    if isinstance(obj, EventHandle) and not obj.manual_reset:
                        obj.signaled = False
                    if isinstance(obj, MutexHandle):
                        obj.owner_tid = tid
                        obj.recursion_count = 1
                        obj.locked = True
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 20)
            return
        # Not yet ready — check timeout then block.
        current = state.scheduler.current_thread()
        if current.wait_timed_out:
            current.wait_timed_out = False
            cpu.regs[EAX] = 0x102  # WAIT_TIMEOUT
            cleanup_stdcall(cpu, memory, 20)
            return
        retry_eip = (cpu.eip - 2) & 0xFFFFFFFF
        handles_set: set[int] = set()
        for i in range(n_count):
            handles_set.add(memory.read32((lp_handles + i * 4) & 0xFFFFFFFF))
        deadline_ms = (
            None
            if timeout_ms == _WAIT_INFINITE
            else (state.virtual_ticks_ms + timeout_ms) & 0xFFFFFFFF
        )
        state.scheduler.block_current_on_handles(
            cpu, memory, frozenset(handles_set), retry_eip, deadline_ms
        )

    stubs.register_handler("kernel32.dll", "WaitForSingleObject", _wait_for_single)
    stubs.register_handler(
        "kernel32.dll", "WaitForMultipleObjects", _halt("WaitForMultipleObjects")
    )
    stubs.register_handler(
        "kernel32.dll", "WaitForMultipleObjectsEx", _wait_for_multiple_ex
    )

    # ── Mutex / Event ─────────────────────────────────────────────────────────

    def _create_mutex_a(cpu: "CPU") -> None:
        b_owner = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        name_ptr = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory) if name_ptr else ""
        if name:
            for h_existing, obj in state.kernel_handle_map.items():
                if isinstance(obj, MutexHandle) and obj.name == name:
                    memory.write32(
                        TEB_BASE + 0x34, int(Win32Error.ERROR_ALREADY_EXISTS)
                    )
                    logger.debug(
                        "handlers",
                        f'[Win32] CreateMutexA("{name}") -> 0x{h_existing:x} (already exists)',
                    )
                    cpu.regs[EAX] = h_existing
                    cleanup_stdcall(cpu, memory, 12)
                    return
        h = state.next_kernel_handle
        state.next_kernel_handle += 1
        owner_tid = state.tls_current_thread_id() if b_owner != 0 else None
        state.kernel_handle_map[h] = MutexHandle(
            locked=b_owner != 0,
            name=name,
            owner_tid=owner_tid,
            recursion_count=1 if b_owner != 0 else 0,
        )
        logger.debug(
            "handlers", f'[Win32] CreateMutexA("{name or "(unnamed)"}") -> 0x{h:x}'
        )
        cpu.regs[EAX] = h
        cleanup_stdcall(cpu, memory, 12)

    def _open_mutex_a(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory) if name_ptr else ""
        if name:
            for h_existing, obj in state.kernel_handle_map.items():
                if isinstance(obj, MutexHandle) and obj.name == name:
                    logger.debug(
                        "handlers", f'[Win32] OpenMutexA("{name}") -> 0x{h_existing:x}'
                    )
                    cpu.regs[EAX] = h_existing
                    cleanup_stdcall(cpu, memory, 12)
                    return
        memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
        logger.warn(
            "handlers",
            f'[Win32] OpenMutexA("{name or "(unnamed)"}") -> NULL (not found)',
        )
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 12)

    def _release_mutex(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        obj = state.kernel_handle_map.get(h)
        if isinstance(obj, MutexHandle):
            obj.recursion_count -= 1
            if obj.recursion_count <= 0:
                obj.owner_tid = None
                obj.locked = False
                obj.recursion_count = 0
                state.scheduler.unblock_handle(h)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    def _create_event_a(cpu: "CPU") -> None:
        b_manual = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        b_initial = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        name_ptr = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory) if name_ptr else "(unnamed)"
        h = state.next_kernel_handle
        state.next_kernel_handle += 1
        state.kernel_handle_map[h] = EventHandle(
            signaled=b_initial != 0,
            manual_reset=b_manual != 0,
        )
        logger.debug(
            "handlers",
            f'[Win32] CreateEventA("{name}", manual={bool(b_manual)}, '
            f"signaled={bool(b_initial)}) -> 0x{h:x}",
        )
        cpu.regs[EAX] = h
        cleanup_stdcall(cpu, memory, 16)

    def _create_event_w(cpu: "CPU") -> None:
        b_manual = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        b_initial = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        name_ptr = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        name = read_wide_string(name_ptr, memory) if name_ptr else "(unnamed)"
        h = state.next_kernel_handle
        state.next_kernel_handle += 1
        state.kernel_handle_map[h] = EventHandle(
            signaled=b_initial != 0,
            manual_reset=b_manual != 0,
        )
        logger.debug(
            "handlers",
            f'[Win32] CreateEventW("{name}", manual={bool(b_manual)}, '
            f"signaled={bool(b_initial)}) -> 0x{h:x}",
        )
        cpu.regs[EAX] = h
        cleanup_stdcall(cpu, memory, 16)

    def _set_event(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        obj = state.kernel_handle_map.get(h)
        if isinstance(obj, EventHandle):
            obj.signaled = True
            n = state.scheduler.unblock_handle(h)
            logger.debug(
                "scheduler", f"SetEvent(0x{h:x}) signaled={obj.signaled} unblocked={n}"
            )
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    def _reset_event(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        obj = state.kernel_handle_map.get(h)
        if isinstance(obj, EventHandle):
            obj.signaled = False
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "CreateMutexA", _create_mutex_a)
    stubs.register_handler("kernel32.dll", "CreateMutexW", _halt("CreateMutexW"))
    stubs.register_handler("kernel32.dll", "OpenMutexA", _open_mutex_a)
    stubs.register_handler("kernel32.dll", "OpenMutexW", _halt("OpenMutexW"))
    stubs.register_handler("kernel32.dll", "ReleaseMutex", _release_mutex)
    stubs.register_handler("kernel32.dll", "CreateEventA", _create_event_a)
    stubs.register_handler("kernel32.dll", "CreateEventW", _create_event_w)
    stubs.register_handler("kernel32.dll", "OpenEventA", _halt("OpenEventA"))
    stubs.register_handler("kernel32.dll", "OpenEventW", _halt("OpenEventW"))
    stubs.register_handler("kernel32.dll", "SetEvent", _set_event)
    stubs.register_handler("kernel32.dll", "ResetEvent", _reset_event)
    stubs.register_handler(
        "kernel32.dll", "CreateFileMappingA", _halt("CreateFileMappingA")
    )
    stubs.register_handler(
        "kernel32.dll", "CreateFileMappingW", _halt("CreateFileMappingW")
    )
    stubs.register_handler(
        "kernel32.dll", "OpenFileMappingA", _halt("OpenFileMappingA")
    )
    stubs.register_handler(
        "kernel32.dll", "OpenFileMappingW", _halt("OpenFileMappingW")
    )
    stubs.register_handler(
        "kernel32.dll", "CreateNamedPipeA", _halt("CreateNamedPipeA")
    )
    stubs.register_handler(
        "kernel32.dll", "ConnectNamedPipe", _halt("ConnectNamedPipe")
    )
    stubs.register_handler("kernel32.dll", "WaitNamedPipeA", _halt("WaitNamedPipeA"))

    # ── CreateFile / ReadFile ─────────────────────────────────────────────────

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000

    _CF_CREATE_NEW = 1
    _CF_CREATE_ALWAYS = 2
    _CF_OPEN_EXISTING = 3
    _CF_OPEN_ALWAYS = 4
    _CF_TRUNCATE_EXISTING = 5

    def _create_file_a(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        access = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        disposition = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory)
        if not name:
            logger.debug(
                "fileio",
                f"CreateFileA: name_ptr=0x{name_ptr:08x} (ESP=0x{cpu.regs[ESP]:08x})",
            )
        writable = bool(access & GENERIC_WRITE) or disposition in (
            _CF_CREATE_NEW,
            _CF_CREATE_ALWAYS,
            _CF_OPEN_ALWAYS,
            _CF_TRUNCATE_EXISTING,
        )
        also_readable = bool(access & GENERIC_READ)
        no_prompt = disposition in (_CF_OPEN_EXISTING, _CF_TRUNCATE_EXISTING)
        cpu.regs[EAX] = state.open_file_handle(
            name,
            writable,
            memory,
            no_create_prompt=no_prompt,
            disposition=disposition,
            also_readable=also_readable,
        )
        cleanup_stdcall(cpu, memory, 28)

    def _create_file_w(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        access = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        disposition = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        name = read_wide_string(name_ptr, memory)
        writable = bool(access & GENERIC_WRITE) or disposition in (
            _CF_CREATE_NEW,
            _CF_CREATE_ALWAYS,
            _CF_OPEN_ALWAYS,
            _CF_TRUNCATE_EXISTING,
        )
        also_readable = bool(access & GENERIC_READ)
        no_prompt = disposition in (_CF_OPEN_EXISTING, _CF_TRUNCATE_EXISTING)
        cpu.regs[EAX] = state.open_file_handle(
            name,
            writable,
            memory,
            no_create_prompt=no_prompt,
            disposition=disposition,
            also_readable=also_readable,
        )
        cleanup_stdcall(cpu, memory, 28)

    def _read_file(cpu: "CPU") -> None:
        h_file = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_buf = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        n_to_read = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_read = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_overlapped = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        # OVERLAPPED.Offset/.OffsetHigh sit at +8/+0xC (after Internal/
        # InternalHigh) -- a real positioned read: use this position instead
        # of the handle's own sequential cursor, and do NOT advance that
        # cursor afterward (matches real Win32: a non-NULL lpOverlapped reads
        # at the given offset without disturbing the file pointer, even on a
        # handle not opened with FILE_FLAG_OVERLAPPED).
        overlapped_pos = None
        if lp_overlapped:
            off_low = memory.read32((lp_overlapped + 8) & 0xFFFFFFFF)
            off_high = memory.read32((lp_overlapped + 0xC) & 0xFFFFFFFF)
            overlapped_pos = off_low | (off_high << 32)
        entry = state.file_handle_map.get(h_file)
        if not entry or (entry.writable and not entry.readable):
            logger.warn(
                "fileio",
                f"[Win32] ReadFile(handle=0x{h_file:x}) -> FALSE"
                + (" (write-only handle)" if entry else " (unknown handle)"),
            )
            if lp_read:
                memory.write32(lp_read, 0)
            cpu.regs[EAX] = 0
        elif entry.writable and entry.readable:
            # Opened GENERIC_READ|GENERIC_WRITE (or fopen "r+"/"w+"/"a+") --
            # real read via the live fd, positioned at entry.position (the
            # tracked logical offset; see FileHandleEntry.readable's own
            # docstring for why this path exists at all), unless a real
            # OVERLAPPED offset was given.
            pos = overlapped_pos if overlapped_pos is not None else entry.position
            data = os.pread(entry.fd, n_to_read, pos) if entry.fd is not None else b""
            if data:
                memory.load(lp_buf & 0xFFFFFFFF, data)
            if overlapped_pos is None:
                entry.position += len(data)
            if lp_read:
                memory.write32(lp_read, len(data))
            cpu.regs[EAX] = 1
            name_short = entry.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            logger.debug(
                "fileio",
                f"ReadFile({name_short} h=0x{h_file:x}) "
                f"offset={pos} req={n_to_read} got={len(data)} "
                f"pos_after={entry.position} buf=0x{lp_buf:x} [read+write handle]"
                + (" [overlapped]" if overlapped_pos is not None else ""),
            )
        else:
            pos = overlapped_pos if overlapped_pos is not None else entry.position
            available = len(entry.data) - pos
            to_read = min(n_to_read, max(available, 0))
            if to_read > 0:
                memory.load(lp_buf & 0xFFFFFFFF, entry.data[pos : pos + to_read])
            if overlapped_pos is None:
                entry.position += to_read
            if lp_read:
                memory.write32(lp_read, to_read)
            cpu.regs[EAX] = 1
            name_short = entry.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            logger.debug(
                "fileio",
                f"ReadFile({name_short} h=0x{h_file:x}) "
                f"offset={pos} req={n_to_read} got={to_read} "
                f"pos_after={entry.position} buf=0x{lp_buf:x} eof={len(entry.data)}"
                + (" [overlapped]" if overlapped_pos is not None else ""),
            )
        cleanup_stdcall(cpu, memory, 20)

    def _lock_file(cpu: "CPU") -> None:
        h_file = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        off_low = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        off_high = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        len_low = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        len_high = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(h_file)
        if entry is None:
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_INVALID_HANDLE))
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 20)
            return
        start = off_low | (off_high << 32)
        end = start + (len_low | (len_high << 32))
        locks = state.file_locks.setdefault(entry.path, [])
        for l_start, l_end, l_handle in locks:
            if l_handle != h_file and start < l_end and l_start < end:
                logger.warn(
                    "fileio",
                    f"[Win32] LockFile(handle=0x{h_file:x}, range=[{start},{end})) -> FALSE "
                    f"(conflicts with [{l_start},{l_end}) held by 0x{l_handle:x})",
                )
                memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_LOCK_VIOLATION))
                cpu.regs[EAX] = 0
                cleanup_stdcall(cpu, memory, 20)
                return
        locks.append((start, end, h_file))
        logger.debug(
            "fileio",
            f"[Win32] LockFile(handle=0x{h_file:x}, range=[{start},{end})) -> TRUE",
        )
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 20)

    def _unlock_file(cpu: "CPU") -> None:
        h_file = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        off_low = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        off_high = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        len_low = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        len_high = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(h_file)
        if entry is None:
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_INVALID_HANDLE))
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 20)
            return
        start = off_low | (off_high << 32)
        end = start + (len_low | (len_high << 32))
        locks = state.file_locks.get(entry.path, [])
        for i, (l_start, l_end, l_handle) in enumerate(locks):
            if l_handle == h_file and l_start == start and l_end == end:
                locks.pop(i)
                logger.debug(
                    "fileio",
                    f"[Win32] UnlockFile(handle=0x{h_file:x}, range=[{start},{end})) -> TRUE",
                )
                cpu.regs[EAX] = 1
                cleanup_stdcall(cpu, memory, 20)
                return
        logger.warn(
            "fileio",
            f"[Win32] UnlockFile(handle=0x{h_file:x}, range=[{start},{end})) -> FALSE (not locked)",
        )
        memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_NOT_LOCKED))
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("kernel32.dll", "LockFile", _lock_file)
    stubs.register_handler("kernel32.dll", "UnlockFile", _unlock_file)

    def _delete_file_a(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory)
        real_path = state.translate_windows_path(name)
        try:
            os.unlink(real_path)
            success = True
        except FileNotFoundError:
            success = False
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
        except PermissionError:
            success = False
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_ACCESS_DENIED))
        except OSError:
            success = False
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
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
        except FileNotFoundError:
            success = False
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
        except PermissionError:
            success = False
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_ACCESS_DENIED))
        except OSError:
            success = False
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
        logger.debug("fileio", f'[Win32] DeleteFileW("{name}") -> {success}')
        cpu.regs[EAX] = 1 if success else 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "CreateFileA", _create_file_a)
    stubs.register_handler("kernel32.dll", "CreateFileW", _create_file_w)
    stubs.register_handler("kernel32.dll", "ReadFile", _read_file)
    stubs.register_handler("kernel32.dll", "DeleteFileA", _delete_file_a)
    stubs.register_handler("kernel32.dll", "DeleteFileW", _delete_file_w)

    # ── Find file / attributes ────────────────────────────────────────────────

    # WIN32_FIND_DATAA field offsets
    _FIND_OFF_ATTRS = 0  # DWORD dwFileAttributes
    _FIND_OFF_FNAME = 44  # CHAR  cFileName[260]
    _FIND_OFF_ALTNAME = 304  # CHAR  cAlternateFileName[14]
    FILE_ATTRIBUTE_DIRECTORY = 0x10
    FILE_ATTRIBUTE_ARCHIVE = 0x20

    def _write_find_data(addr: int, name: str, attrs: int) -> None:
        memory.write32(addr + _FIND_OFF_ATTRS, attrs)
        for i in range(4, 44):  # timestamps + sizes: zero
            memory.write8(addr + i, 0)
        name_b = name.encode("ascii", errors="replace")[:259]
        for i, b in enumerate(name_b):
            memory.write8(addr + _FIND_OFF_FNAME + i, b)
        memory.write8(addr + _FIND_OFF_FNAME + len(name_b), 0)
        for i in range(14):  # cAlternateFileName: empty
            memory.write8(addr + _FIND_OFF_ALTNAME + i, 0)

    def _find_first_file_a(cpu: "CPU") -> None:
        INVALID = 0xFFFFFFFF
        lp_name = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_find_data = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        pattern_win = read_cstring(lp_name, memory, 260)
        linux_pat = state.translate_windows_path(pattern_win)
        dir_path = os.path.dirname(linux_pat)
        pat = os.path.basename(linux_pat)
        if pat in ("*.*", ""):  # Windows *.*  matches everything
            pat = "*"
        logger.debug("handlers", f"FindFirstFileA({pattern_win!r})")
        real_dir = find_file_ci(dir_path) if dir_path else None
        if not real_dir or not os.path.isdir(real_dir):
            cpu.regs[EAX] = INVALID
            cleanup_stdcall(cpu, memory, 8)
            return
        try:
            entries: list[tuple[str, int]] = []
            for name in sorted(os.listdir(real_dir), key=str.lower):
                if fnmatch.fnmatch(name.lower(), pat.lower()):
                    full = os.path.join(real_dir, name)
                    attrs = (
                        FILE_ATTRIBUTE_DIRECTORY
                        if os.path.isdir(full)
                        else FILE_ATTRIBUTE_ARCHIVE
                    )
                    entries.append((name, attrs))
        except OSError:
            cpu.regs[EAX] = INVALID
            cleanup_stdcall(cpu, memory, 8)
            return
        if not entries:
            cpu.regs[EAX] = INVALID
            cleanup_stdcall(cpu, memory, 8)
            return
        handle = state.next_find_handle
        state.next_find_handle += 1
        state.find_handle_map[handle] = entries
        state.find_handle_idx[handle] = 0
        _write_find_data(lp_find_data, entries[0][0], entries[0][1])
        logger.debug(
            "handlers",
            f"  -> handle=0x{handle:x} first={entries[0][0]!r} ({len(entries)} total)",
        )
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, 8)

    def _find_next_file_a(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_find_data = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        entries = state.find_handle_map.get(h)
        if entries is None:
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 8)
            return
        idx = state.find_handle_idx[h] + 1
        state.find_handle_idx[h] = idx
        if idx >= len(entries):
            logger.debug("handlers", f"FindNextFileA(0x{h:x}) -> no more files")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 8)
            return
        _write_find_data(lp_find_data, entries[idx][0], entries[idx][1])
        logger.debug("handlers", f"FindNextFileA(0x{h:x}) -> {entries[idx][0]!r}")
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _find_close(cpu: "CPU") -> None:
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        state.find_handle_map.pop(h, None)
        state.find_handle_idx.pop(h, None)
        logger.debug("handlers", f"FindClose(0x{h:x})")
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
                FILE_ATTRIBUTE_NORMAL = 0x80
                cpu.regs[EAX] = (
                    FILE_ATTRIBUTE_DIRECTORY
                    if stat.S_ISDIR(s.st_mode)
                    else FILE_ATTRIBUTE_NORMAL
                )
            except OSError as e:
                logger.warn(
                    "fileio", f'GetFileAttributesA: stat failed for "{real_path}": {e}'
                )
                cpu.regs[EAX] = 0xFFFFFFFF
        else:
            cpu.regs[EAX] = 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "FindFirstFileA", _find_first_file_a)
    stubs.register_handler("kernel32.dll", "FindFirstFileW", _halt("FindFirstFileW"))
    stubs.register_handler("kernel32.dll", "FindNextFileA", _find_next_file_a)
    stubs.register_handler("kernel32.dll", "FindNextFileW", _halt("FindNextFileW"))
    stubs.register_handler("kernel32.dll", "FindClose", _find_close)
    stubs.register_handler("kernel32.dll", "CompareFileTime", _halt("CompareFileTime"))
    stubs.register_handler("kernel32.dll", "GetFileAttributesA", _get_file_attributes_a)

    def _get_full_path_name_a(cpu: "CPU") -> None:
        lp_file = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        n_buf = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        lp_buf = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_part = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)

        raw = read_cstring(lp_file, memory, 260) if lp_file else ""

        CWD = state.current_directory
        if raw and len(raw) >= 2 and raw[1] == ":":
            full_win = raw
        elif raw.startswith("\\\\"):
            full_win = raw
        else:
            full_win = CWD + "\\" + raw.lstrip("\\")

        parts: list = []
        for seg in full_win.replace("/", "\\").split("\\"):
            if seg == "..":
                if len(parts) > 1:
                    parts.pop()
            elif seg == "." or seg == "":
                pass
            else:
                parts.append(seg)
        result = "\\".join(parts) or "C:\\"

        needed = len(result) + 1
        if lp_buf and n_buf >= needed:
            for i, ch in enumerate(result):
                memory.write8((lp_buf + i) & 0xFFFFFFFF, ord(ch) & 0xFF)
            memory.write8((lp_buf + len(result)) & 0xFFFFFFFF, 0)
            if lp_part:
                last_sep = result.rfind("\\")
                if 0 <= last_sep < len(result) - 1:
                    memory.write32(lp_part, (lp_buf + last_sep + 1) & 0xFFFFFFFF)
                else:
                    memory.write32(lp_part, 0)
            cpu.regs[EAX] = len(result)
            logger.trace("handlers", f"GetFullPathNameA({raw!r}) -> {result!r}")
        else:
            cpu.regs[EAX] = needed
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("kernel32.dll", "GetFullPathNameA", _get_full_path_name_a)

    def _get_short_path_name_a(cpu: "CPU") -> None:
        lp_long = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_short = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        cch_buf = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)

        long_path = read_cstring(lp_long, memory) if lp_long else ""
        linux_path = state.translate_windows_path(long_path)
        real_path = find_file_ci(linux_path)
        if real_path is None:
            logger.warn("fileio", f'GetShortPathNameA("{long_path}") — not found')
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 12)
            return

        # This emulator doesn't implement real 8.3 short-name generation
        # (NTFS tilde-truncated names) -- the same real behavior a genuine
        # Windows install has when 8.3 name creation is disabled (fsutil
        # 8dot3name): the long path is returned unchanged since there's no
        # shorter form to give, not a fabricated result.
        result = long_path
        needed = len(result) + 1
        if lp_short and cch_buf >= needed:
            for i, ch in enumerate(result):
                memory.write8((lp_short + i) & 0xFFFFFFFF, ord(ch) & 0xFF)
            memory.write8((lp_short + len(result)) & 0xFFFFFFFF, 0)
            cpu.regs[EAX] = len(result)
            logger.trace("handlers", f"GetShortPathNameA({long_path!r}) -> {result!r}")
        else:
            cpu.regs[EAX] = needed
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("kernel32.dll", "GetShortPathNameA", _get_short_path_name_a)

    def _search_path_find(
        path_override: Optional[str],
        filename: str,
        extension: Optional[str],
    ) -> Optional[str]:
        if not filename:
            return None

        # If extension provided and filename has no extension in base name, append it
        base = filename.replace("/", "\\").split("\\")[-1]
        effective_filename = filename
        if extension and "." not in base:
            ext = extension if extension.startswith(".") else "." + extension
            effective_filename = filename + ext

        # If effective_filename contains directory separators or drive letter, search only that path
        if (
            "\\" in effective_filename
            or "/" in effective_filename
            or (
                len(effective_filename) >= 2
                and effective_filename[1] == ":"
                and effective_filename[0].isalpha()
            )
        ):
            linux_path = state.translate_windows_path(effective_filename)
            resolved = find_file_ci(linux_path)
            if resolved and os.path.isfile(resolved):
                return state.reverse_translate_path(resolved)
            return None

        # Otherwise, search list of directories
        search_dirs: list[str] = []
        if path_override is not None:
            # Per MSDN: a non-NULL lpPath (even an empty string) restricts
            # the search to exactly the directories it lists -- it must NOT
            # fall through to the default exe-dir/cwd/System32/PATH sequence
            # below, which is reserved for lpPath == NULL only.
            for d in path_override.split(";"):
                d = d.strip()
                if d:
                    search_dirs.append(d)
        else:
            # Standard search sequence:
            # 1. Directory of the executable
            if state.exe_path:
                exe_dir = os.path.dirname(state.exe_path)
                win_exe_dir = state.reverse_translate_path(exe_dir)
                if win_exe_dir:
                    search_dirs.append(win_exe_dir)
            # 2. Current working directory
            search_dirs.append(state.current_directory)
            # 3. System32 directory
            search_dirs.append("C:\\WINDOWS\\SYSTEM32")
            # 4. Windows directory
            search_dirs.append("C:\\WINDOWS")
            # 5. PATH environment variable
            path_env = _env_vars.get("PATH", "")
            if path_env:
                for d in path_env.split(";"):
                    d = d.strip()
                    if d:
                        search_dirs.append(d)

        for d in search_dirs:
            cand_win = f"{d.rstrip('\\')}\\{effective_filename}"
            cand_linux = state.translate_windows_path(cand_win)
            resolved = find_file_ci(cand_linux)
            if resolved and os.path.isfile(resolved):
                return state.reverse_translate_path(resolved)

        return None

    def _search_path_a(cpu: "CPU") -> None:
        lp_path = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_file = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        lp_ext = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        n_buf = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_buf = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        lp_part = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)

        if not lp_file:
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_INVALID_PARAMETER))
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 24)
            return

        path_override = read_cstring(lp_path, memory) if lp_path else None
        filename = read_cstring(lp_file, memory)
        ext = read_cstring(lp_ext, memory) if lp_ext else None

        found_path = _search_path_find(path_override, filename, ext)
        if found_path is None:
            logger.debug(
                "fileio", f'SearchPathA(file="{filename}", ext={ext!r}) — not found'
            )
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 24)
            return

        needed = len(found_path) + 1
        if lp_buf and n_buf >= needed:
            for i, ch in enumerate(found_path):
                memory.write8((lp_buf + i) & 0xFFFFFFFF, ord(ch) & 0xFF)
            memory.write8((lp_buf + len(found_path)) & 0xFFFFFFFF, 0)
            if lp_part:
                last_sep = found_path.rfind("\\")
                if 0 <= last_sep < len(found_path) - 1:
                    memory.write32(lp_part, (lp_buf + last_sep + 1) & 0xFFFFFFFF)
                else:
                    memory.write32(lp_part, 0)
            cpu.regs[EAX] = len(found_path)
            logger.debug(
                "fileio",
                f'SearchPathA(file="{filename}", ext={ext!r}) -> "{found_path}"',
            )
        else:
            cpu.regs[EAX] = needed
        cleanup_stdcall(cpu, memory, 24)

    def _search_path_w(cpu: "CPU") -> None:
        lp_path = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_file = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        lp_ext = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        n_buf = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_buf = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        lp_part = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)

        if not lp_file:
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_INVALID_PARAMETER))
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 24)
            return

        path_override = read_wide_string(lp_path, memory) if lp_path else None
        filename = read_wide_string(lp_file, memory)
        ext = read_wide_string(lp_ext, memory) if lp_ext else None

        found_path = _search_path_find(path_override, filename, ext)
        if found_path is None:
            logger.debug(
                "fileio", f'SearchPathW(file="{filename}", ext={ext!r}) — not found'
            )
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 24)
            return

        needed = len(found_path) + 1
        if lp_buf and n_buf >= needed:
            for i, ch in enumerate(found_path):
                memory.write16((lp_buf + i * 2) & 0xFFFFFFFF, ord(ch) & 0xFFFF)
            memory.write16((lp_buf + len(found_path) * 2) & 0xFFFFFFFF, 0)
            if lp_part:
                last_sep = found_path.rfind("\\")
                if 0 <= last_sep < len(found_path) - 1:
                    memory.write32(lp_part, (lp_buf + (last_sep + 1) * 2) & 0xFFFFFFFF)
                else:
                    memory.write32(lp_part, 0)
            cpu.regs[EAX] = len(found_path)
            logger.debug(
                "fileio",
                f'SearchPathW(file="{filename}", ext={ext!r}) -> "{found_path}"',
            )
        else:
            cpu.regs[EAX] = needed
        cleanup_stdcall(cpu, memory, 24)

    stubs.register_handler("kernel32.dll", "SearchPathA", _search_path_a)
    stubs.register_handler("kernel32.dll", "SearchPathW", _search_path_w)

    # ── SetFilePointer / GetFileSize ──────────────────────────────────────────

    def _set_file_pointer(cpu: "CPU") -> None:
        h_file = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        dist_raw = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        distance = dist_raw if dist_raw < 0x80000000 else dist_raw - 0x100000000
        method = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        FILE_BEGIN = 0
        FILE_CURRENT = 1
        FILE_END = 2
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
        h_file = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_high = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(h_file)
        size = None
        if entry is not None:
            # Query the real host file directly (same approach
            # GetFileInformationByHandle already uses) instead of
            # entry.data's cached length -- entry.data is only populated
            # for read-mode entries (empty bytes for write-only, per its own
            # field comment in _state.py), which made GetFileSize wrongly
            # fail on every writable handle -- confirmed live: right after
            # creating and writing its own scratch temp file, msjet35.dll
            # calls GetFileSize on that exact handle as a completely normal
            # operation, and GetFileInformationByHandle succeeds on the same
            # handle moments earlier in the same log, proving it's valid.
            try:
                st = os.fstat(entry.fd) if entry.fd is not None else os.stat(entry.path)
                size = st.st_size
            except OSError:
                size = None
        if size is not None:
            if lp_high:
                memory.write32(lp_high, 0)
            cpu.regs[EAX] = size & 0xFFFFFFFF
        else:
            # h_file is genuinely unknown, or the real host file is no
            # longer accessible. Real GetFileSize always calls SetLastError
            # on this 0xFFFFFFFF failure return -- callers (confirmed live:
            # msjet35.dll's own error-code translator) read GetLastError()
            # right after and need a real value, not whatever was already
            # there.
            cpu.regs[EAX] = 0xFFFFFFFF
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_INVALID_HANDLE))
        cleanup_stdcall(cpu, memory, 8)

    def _get_file_size_ex(cpu: "CPU") -> None:
        h_file = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_size = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
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
            memory.write32(lp_size, size & 0xFFFFFFFF)
            memory.write32(lp_size + 4, 0)
            cpu.regs[EAX] = 1
        else:
            cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    # FILETIME epoch (1601-01-01) to Unix epoch (1970-01-01) offset, in 100ns units
    _FILETIME_EPOCH_DIFF = 116444736000000000

    def _unix_to_filetime(unix_seconds: float) -> int:
        return int(unix_seconds * 10_000_000) + _FILETIME_EPOCH_DIFF

    def _write_filetime(addr: int, ft: int) -> None:
        memory.write32(addr, ft & 0xFFFFFFFF)
        memory.write32(addr + 4, (ft >> 32) & 0xFFFFFFFF)

    def _get_file_information_by_handle(cpu: "CPU") -> None:
        h_file = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_info = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(h_file)
        if not entry or not lp_info:
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 8)
            return
        try:
            st = os.fstat(entry.fd) if entry.fd is not None else os.stat(entry.path)
        except OSError:
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 8)
            return

        attrs = (
            FILE_ATTRIBUTE_DIRECTORY
            if stat.S_ISDIR(st.st_mode)
            else FILE_ATTRIBUTE_ARCHIVE
        )
        memory.write32(lp_info + 0x00, attrs)  # dwFileAttributes
        _write_filetime(
            lp_info + 0x04, _unix_to_filetime(st.st_ctime)
        )  # ftCreationTime
        _write_filetime(
            lp_info + 0x0C, _unix_to_filetime(st.st_atime)
        )  # ftLastAccessTime
        _write_filetime(
            lp_info + 0x14, _unix_to_filetime(st.st_mtime)
        )  # ftLastWriteTime
        memory.write32(lp_info + 0x1C, 0x12345678)  # dwVolumeSerialNumber
        memory.write32(lp_info + 0x20, 0)  # nFileSizeHigh
        memory.write32(lp_info + 0x24, st.st_size & 0xFFFFFFFF)  # nFileSizeLow
        memory.write32(lp_info + 0x28, 1)  # nNumberOfLinks
        memory.write32(lp_info + 0x2C, 0)  # nFileIndexHigh
        memory.write32(lp_info + 0x30, st.st_ino & 0xFFFFFFFF)  # nFileIndexLow
        logger.debug(
            "fileio", f"GetFileInformationByHandle(0x{h_file:x}) -> size={st.st_size}"
        )
        cpu.regs[EAX] = 1
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

    stubs.register_handler("kernel32.dll", "SetFilePointer", _set_file_pointer)
    stubs.register_handler("kernel32.dll", "GetFileSize", _get_file_size)
    stubs.register_handler("kernel32.dll", "GetFileSizeEx", _get_file_size_ex)
    stubs.register_handler(
        "kernel32.dll", "GetFileInformationByHandle", _get_file_information_by_handle
    )
    stubs.register_handler("kernel32.dll", "FlushFileBuffers", _flush_file_buffers)
    stubs.register_handler("kernel32.dll", "SetEndOfFile", _set_end_of_file)

    # ── Directory / drives ────────────────────────────────────────────────────

    def _get_current_dir_a(cpu: "CPU") -> None:
        n_buf = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_buf = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        d = state.current_directory
        if lp_buf and n_buf > len(d):
            for i, ch in enumerate(d):
                memory.write8(lp_buf + i, ord(ch))
            memory.write8(lp_buf + len(d), 0)
            cpu.regs[EAX] = len(d)
        else:
            cpu.regs[EAX] = len(d) + 1  # required size
        cleanup_stdcall(cpu, memory, 8)

    def _set_current_dir_a(cpu: "CPU") -> None:
        lp_path = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        path = read_cstring(lp_path, memory, 260) if lp_path else ""
        if path:
            # Preserve root "C:\" as-is; strip trailing backslash from subdirs
            state.current_directory = (
                path if path.endswith(":\\") else path.rstrip("\\")
            )
            logger.debug("handlers", f"SetCurrentDirectoryA({path!r})")
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

    def _get_system_dir_a(cpu: "CPU") -> None:
        lp_buf = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        u_size = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        d = "C:\\WINDOWS\\SYSTEM32"
        if lp_buf and u_size > len(d):
            for i, ch in enumerate(d):
                memory.write8(lp_buf + i, ord(ch))
            memory.write8(lp_buf + len(d), 0)
        cpu.regs[EAX] = len(d)
        cleanup_stdcall(cpu, memory, 8)

    def _get_temp_path_a(cpu: "CPU") -> None:
        n_buf = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_buf = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        d = "C:\\WINDOWS\\TEMP\\"
        if lp_buf and n_buf > len(d):
            for i, ch in enumerate(d):
                memory.write8(lp_buf + i, ord(ch))
            memory.write8(lp_buf + len(d), 0)
            cpu.regs[EAX] = len(d)
        else:
            # required size, including null terminator
            cpu.regs[EAX] = len(d) + 1
        logger.debug("handlers", f'GetTempPathA({n_buf}) -> "{d}"')
        cleanup_stdcall(cpu, memory, 8)

    _temp_file_unique = [0xA000]

    def _get_temp_file_name_a(cpu: "CPU") -> None:
        lp_path_name = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_prefix = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        u_unique = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_temp_file = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)

        path = read_cstring(lp_path_name, memory, 260) if lp_path_name else ""
        prefix = (read_cstring(lp_prefix, memory, 260) if lp_prefix else "")[:3]
        if path and not path.endswith("\\"):
            path += "\\"

        if u_unique:
            unique = u_unique & 0xFFFF
        else:
            unique = _temp_file_unique[0] & 0xFFFF
            _temp_file_unique[0] += 1

        full_name = f"{path}{prefix}{unique:04X}.TMP"

        # uUnique == 0 means the caller wants the name *and* the file reserved
        # (created, 0 bytes) — same real-file-creation path CreateFileA's
        # writable branch uses, so a later real CreateFileA/ReadFile against
        # this name (e.g. Jet's own scratch-file use) sees a real file.
        if u_unique == 0:
            real_path = state.translate_windows_path(full_name)
            try:
                dirname = os.path.dirname(real_path)
                if dirname:
                    os.makedirs(dirname, exist_ok=True)
                os.close(
                    os.open(real_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                )
            except OSError as e:
                logger.warn(
                    "fileio", f'GetTempFileNameA: failed to reserve "{full_name}": {e}'
                )
                cpu.regs[EAX] = 0
                cleanup_stdcall(cpu, memory, 16)
                return

        if lp_temp_file:
            for i, ch in enumerate(full_name):
                memory.write8(lp_temp_file + i, ord(ch))
            memory.write8(lp_temp_file + len(full_name), 0)

        logger.debug(
            "fileio",
            f'GetTempFileNameA(path="{path}", prefix="{prefix}", unique={u_unique}) -> "{full_name}"',
        )
        cpu.regs[EAX] = unique
        cleanup_stdcall(cpu, memory, 16)

    def _get_disk_free_space_a(cpu: "CPU") -> None:
        lp_spc = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        lp_bps = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_fc = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_tc = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        if lp_spc:
            memory.write32(lp_spc, 8)
        if lp_bps:
            memory.write32(lp_bps, 512)
        if lp_fc:
            memory.write32(lp_fc, 1000000)
        if lp_tc:
            memory.write32(lp_tc, 2000000)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 20)

    _drive_type_traced = [False]

    def _get_drive_type_a(cpu: "CPU") -> None:
        # GetDriveTypeA(lpRootPathName) -> UINT
        # DRIVE_UNKNOWN=0, DRIVE_NO_ROOT_DIR=1, DRIVE_REMOVABLE=2,
        # DRIVE_FIXED=3, DRIVE_REMOTE=4, DRIVE_CDROM=5, DRIVE_RAMDISK=6
        DRIVE_NO_ROOT_DIR = 1
        DRIVE_FIXED = 3
        _NAMES = {
            0: "UNKNOWN",
            1: "NO_ROOT_DIR",
            2: "REMOVABLE",
            3: "FIXED",
            4: "REMOTE",
            5: "CDROM",
            6: "RAMDISK",
        }
        lp_root = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if lp_root == 0:
            cpu.regs[EAX] = DRIVE_FIXED
            logger.debug("handlers", f"GetDriveTypeA(NULL) -> FIXED")
        else:
            root_path = read_cstring(lp_root, memory, 16)
            linux_path = state.translate_windows_path(root_path)
            if os.path.isdir(linux_path.rstrip("/") or "/"):
                cpu.regs[EAX] = DRIVE_FIXED
            else:
                cpu.regs[EAX] = DRIVE_NO_ROOT_DIR
            logger.debug(
                "handlers",
                f"GetDriveTypeA({root_path!r}) -> {_NAMES.get(cpu.regs[EAX], cpu.regs[EAX])}",
            )
        if not _drive_type_traced[0]:
            _drive_type_traced[0] = True
            ret_addr = memory.read32(cpu.regs[ESP] & 0xFFFFFFFF)
            logger.info("handlers", f"GetDriveTypeA first call from 0x{ret_addr:08x}")
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GetCurrentDirectoryA", _get_current_dir_a)
    stubs.register_handler("kernel32.dll", "SetCurrentDirectoryA", _set_current_dir_a)
    stubs.register_handler("kernel32.dll", "GetWindowsDirectoryA", _get_windows_dir_a)
    stubs.register_handler("kernel32.dll", "GetSystemDirectoryA", _get_system_dir_a)
    stubs.register_handler("kernel32.dll", "GetTempPathA", _get_temp_path_a)
    stubs.register_handler("kernel32.dll", "GetTempFileNameA", _get_temp_file_name_a)
    stubs.register_handler("kernel32.dll", "GetDiskFreeSpaceA", _get_disk_free_space_a)
    stubs.register_handler("kernel32.dll", "GetDriveTypeA", _get_drive_type_a)

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
            memory.write32(lp + 0, 32)  # dwLength
            memory.write32(lp + 4, 50)  # dwMemoryLoad (50%)
            memory.write32(lp + 8, 256 * MB)  # dwTotalPhys
            memory.write32(lp + 12, 128 * MB)  # dwAvailPhys
            memory.write32(lp + 16, 512 * MB)  # dwTotalPageFile
            memory.write32(lp + 20, 384 * MB)  # dwAvailPageFile
            memory.write32(lp + 24, 0x7FFF0000)  # dwTotalVirtual
            memory.write32(lp + 28, 0x7FF00000)  # dwAvailVirtual
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GlobalMemoryStatus", _global_memory_status)

    # ── Time ──────────────────────────────────────────────────────────────────

    def _write_systemtime(lp: int, dt: datetime.datetime, *, utc: bool) -> None:
        memory.write16(lp, dt.year)
        memory.write16(lp + 2, dt.month)
        memory.write16(lp + 4, dt.weekday() + 1 if not utc else dt.isoweekday() % 7)
        memory.write16(lp + 6, dt.day)
        memory.write16(lp + 8, dt.hour)
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

    def _get_system_time_as_filetime(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        now_utc = datetime.datetime.now(datetime.timezone.utc).timestamp()
        _write_filetime(lp, _unix_to_filetime(now_utc))
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
        cur_off = -int(
            datetime.datetime.now().astimezone().utcoffset().total_seconds() // 60
        )
        is_dst = (cur_off == dst_offset) and (std_offset != dst_offset)
        memory.write32(lp, std_offset & 0xFFFFFFFF)  # Bias
        memory.write32(lp + 84, 0)  # StandardBias
        memory.write32(lp + 168, (dst_offset - std_offset) & 0xFFFFFFFF)
        cpu.regs[EAX] = 2 if is_dst else 1
        cleanup_stdcall(cpu, memory, 4)

    def _file_time_to_local(cpu: "CPU") -> None:
        lp_in = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_out = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if lp_in == 0 or lp_out == 0:
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 8)
            return
        lo = memory.read32(lp_in)
        hi = memory.read32(lp_in + 4)
        utc = (hi << 32) | lo
        bias_min = -int(
            datetime.datetime.now().astimezone().utcoffset().total_seconds() // 60
        )
        bias_100ns = bias_min * 60 * 10_000_000
        local = (utc - bias_100ns) & 0xFFFFFFFFFFFFFFFF
        memory.write32(lp_out, local & 0xFFFFFFFF)
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

    stubs.register_handler("kernel32.dll", "GetLocalTime", _get_local_time)
    stubs.register_handler("kernel32.dll", "GetSystemTime", _get_system_time)
    stubs.register_handler(
        "kernel32.dll", "GetSystemTimeAsFileTime", _get_system_time_as_filetime
    )
    stubs.register_handler("kernel32.dll", "GetTimeZoneInformation", _get_tz_info)
    stubs.register_handler(
        "kernel32.dll", "FileTimeToLocalFileTime", _file_time_to_local
    )
    stubs.register_handler("kernel32.dll", "FileTimeToSystemTime", _file_time_to_system)

    # ── Misc ──────────────────────────────────────────────────────────────────

    # FormatMessageA(dwFlags, lpSource, dwMessageId, dwLanguageId,
    #                lpBuffer, nSize, Arguments) -> DWORD (chars written)
    #
    # Only FORMAT_MESSAGE_FROM_SYSTEM (dwMessageId -> canned message text) and
    # FORMAT_MESSAGE_FROM_STRING (lpSource is the format string itself) are
    # handled; %1/%2-style insert substitution is not implemented (every
    # caller seen so far -- e.g. formatting a COM HRESULT for a log/error
    # dialog -- uses FORMAT_MESSAGE_IGNORE_INSERTS). Message text below is a
    # best-effort match to real Windows' system message table for the codes
    # this emulator's own handlers actually produce (mostly COM HRESULTs) --
    # exact wording isn't load-bearing, callers just display/log the string.
    _FORMAT_MESSAGE_ALLOCATE_BUFFER = 0x00000100
    _FORMAT_MESSAGE_FROM_STRING = 0x00000400
    _FORMAT_MESSAGE_FROM_SYSTEM = 0x00001000

    _SYSTEM_MESSAGES: dict[int, str] = {
        0x00000000: "The operation completed successfully.",
        0x80004001: "Not implemented",
        0x80004002: "No such interface supported",
        0x80004003: "Invalid pointer",
        0x80004004: "Operation aborted",
        0x80004005: "Unspecified error",
        0x8000FFFF: "Catastrophic failure",
        0x80040110: "Class does not support aggregation (or class object is remote)",
        0x80040111: "Class not registered for this server -- the object is not available",
        0x80040112: "Class is not licensed for use",
        0x80040154: "Class not registered",
        0x80070005: "General access denied error",
    }

    def _format_message_a(cpu: "CPU") -> None:
        sp = cpu.regs[ESP]
        dw_flags = memory.read32((sp + 4) & 0xFFFFFFFF)
        lp_source = memory.read32((sp + 8) & 0xFFFFFFFF)
        dw_message_id = memory.read32((sp + 12) & 0xFFFFFFFF)
        lp_buffer = memory.read32((sp + 20) & 0xFFFFFFFF)
        n_size = memory.read32((sp + 24) & 0xFFFFFFFF)

        if dw_flags & _FORMAT_MESSAGE_FROM_STRING:
            text = read_cstring(lp_source, memory) if lp_source else ""
        elif dw_flags & _FORMAT_MESSAGE_FROM_SYSTEM:
            text = _SYSTEM_MESSAGES.get(
                dw_message_id, f"Unknown error (0x{dw_message_id:08x})"
            )
        else:
            text = f"Unknown error (0x{dw_message_id:08x})"

        allocate = bool(dw_flags & _FORMAT_MESSAGE_ALLOCATE_BUFFER)
        if allocate:
            # lpBuffer is actually LPSTR* here -- we allocate the real
            # buffer ourselves and write its address into *lpBuffer.
            out_addr = state.simple_alloc(len(text) + 1)
            if lp_buffer:
                memory.write32(lp_buffer, out_addr)
        else:
            out_addr = lp_buffer
            if n_size > 0 and len(text) > n_size - 1:
                text = text[: n_size - 1]

        if out_addr:
            for i, ch in enumerate(text):
                memory.write8(out_addr + i, ord(ch) & 0xFF)
            memory.write8(out_addr + len(text), 0)

        logger.debug(
            "handlers",
            f'FormatMessageA(flags=0x{dw_flags:x}, id=0x{dw_message_id:x}) -> "{text}"',
        )
        cpu.regs[EAX] = len(text)
        cleanup_stdcall(cpu, memory, 28)

    stubs.register_handler("kernel32.dll", "FormatMessageA", _format_message_a)
    stubs.register_handler("kernel32.dll", "GlobalAddAtom", _halt("GlobalAddAtom"))
    stubs.register_handler("kernel32.dll", "GlobalFindAtom", _halt("GlobalFindAtom"))
    stubs.register_handler(
        "kernel32.dll", "GlobalGetAtomNameA", _halt("GlobalGetAtomNameA")
    )
    stubs.register_handler(
        "kernel32.dll", "GlobalDeleteAtom", _halt("GlobalDeleteAtom")
    )
    stubs.register_handler("kernel32.dll", "AddAtom", _halt("AddAtom"))
    stubs.register_handler("kernel32.dll", "FindAtom", _halt("FindAtom"))
    stubs.register_handler("kernel32.dll", "GetAtomName", _halt("GetAtomName"))
    stubs.register_handler("kernel32.dll", "DeleteAtom", _halt("DeleteAtom"))

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
    stubs.register_handler("kernel32.dll", "WinExec", _win_exec)
    stubs.register_handler("kernel32.dll", "_lopen", _lopen)
    stubs.register_handler("kernel32.dll", "_lclose", _lclose)

    # ── Private profile (INI file) ────────────────────────────────────────────

    def _get_private_profile_string_a(cpu: "CPU") -> None:
        esp = cpu.regs[ESP]
        lp_app_name = memory.read32((esp + 4) & 0xFFFFFFFF)
        lp_key_name = memory.read32((esp + 8) & 0xFFFFFFFF)
        lp_default = memory.read32((esp + 12) & 0xFFFFFFFF)
        lp_returned = memory.read32((esp + 16) & 0xFFFFFFFF)
        n_size = memory.read32((esp + 20) & 0xFFFFFFFF)
        lp_file_name = memory.read32((esp + 24) & 0xFFFFFFFF)

        app_name = read_cstring(lp_app_name, memory) if lp_app_name else None
        key_name = read_cstring(lp_key_name, memory) if lp_key_name else None
        default = read_cstring(lp_default, memory) if lp_default else ""
        file_name = read_cstring(lp_file_name, memory) if lp_file_name else ""

        args = GetPrivateProfileStringArgs(
            app_name=app_name,
            key_name=key_name,
            default=default,
            out_ptr=lp_returned,
            n_size=n_size,
            file_name=file_name,
        )

        # Load and parse the INI file from the translated Linux path.
        ini: dict = {}
        if file_name:
            linux_path = state.translate_windows_path(file_name)
            real_path = find_file_ci(linux_path)
            if real_path:
                # find_file_ci confirmed the path exists — any OSError here is not ENOENT.
                try:
                    with open(real_path, "r", encoding="latin-1") as fh:
                        ini = parse_ini(fh.read())
                except OSError as e:
                    logger.error(
                        "fileio",
                        f"GetPrivateProfileStringA: file exists but cannot be read {real_path!r}: {e}",
                    )

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
            encoded = encoded[: n_size - 1]
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
        lp_app_name = memory.read32((esp + 4) & 0xFFFFFFFF)
        lp_key_name = memory.read32((esp + 8) & 0xFFFFFFFF)
        n_default = memory.read32((esp + 12) & 0xFFFFFFFF)
        lp_file_name = memory.read32((esp + 16) & 0xFFFFFFFF)

        app_name = read_cstring(lp_app_name, memory) if lp_app_name else ""
        key_name = read_cstring(lp_key_name, memory) if lp_key_name else ""
        file_name = read_cstring(lp_file_name, memory) if lp_file_name else ""
        default = n_default if n_default < 0x80000000 else n_default - 0x100000000

        args = GetPrivateProfileIntArgs(
            app_name=app_name,
            key_name=key_name,
            default=default,
            file_name=file_name,
        )

        ini: dict = {}
        if file_name:
            linux_path = state.translate_windows_path(file_name)
            real_path = find_file_ci(linux_path)
            if real_path:
                # find_file_ci confirmed the path exists — any OSError here is not ENOENT.
                try:
                    with open(real_path, "r", encoding="latin-1") as fh:
                        ini = parse_ini(fh.read())
                except OSError as e:
                    logger.error(
                        "fileio",
                        f"GetPrivateProfileIntA: file exists but cannot be read {real_path!r}: {e}",
                    )

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
        lp_app_name = memory.read32((esp + 4) & 0xFFFFFFFF)
        lp_key_name = memory.read32((esp + 8) & 0xFFFFFFFF)
        lp_string = memory.read32((esp + 12) & 0xFFFFFFFF)
        lp_file_name = memory.read32((esp + 16) & 0xFFFFFFFF)

        app_name = read_cstring(lp_app_name, memory) if lp_app_name else None
        key_name = read_cstring(lp_key_name, memory) if lp_key_name else None
        value = read_cstring(lp_string, memory) if lp_string else None
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
        lp_app_name = memory.read32((esp + 4) & 0xFFFFFFFF)
        lp_string = memory.read32((esp + 8) & 0xFFFFFFFF)
        lp_file_name = memory.read32((esp + 12) & 0xFFFFFFFF)

        app_name = read_cstring(lp_app_name, memory) if lp_app_name else None
        file_name = read_cstring(lp_file_name, memory) if lp_file_name else ""

        # lpString is a double-null-terminated list of "key=value" entries.
        pairs: dict[str, str] = {}
        if lp_string and app_name:
            ptr = lp_string
            while True:
                entry = read_cstring(ptr, memory)
                if not entry:
                    break  # hit the double-null terminator
                if "=" in entry:
                    k, _, v = entry.partition("=")
                    pairs[k.strip().lower()] = v.strip()
                ptr += len(entry.encode("latin-1")) + 1  # advance past the null

        linux_path = state.translate_windows_path(file_name) if file_name else ""
        ok = write_profile_section(linux_path, app_name, pairs)
        logger.debug(
            "handlers",
            f"WritePrivateProfileSectionA({app_name!r}, file={file_name!r}) -> {ok}",
        )
        cpu.regs[EAX] = 1 if ok else 0
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler(
        "kernel32.dll", "GetPrivateProfileStringA", _get_private_profile_string_a
    )
    stubs.register_handler(
        "kernel32.dll", "GetPrivateProfileIntA", _get_private_profile_int_a
    )
    stubs.register_handler(
        "kernel32.dll", "WritePrivateProfileStringA", _write_private_profile_string_a
    )
    stubs.register_handler(
        "kernel32.dll", "WritePrivateProfileSectionA", _write_private_profile_section_a
    )

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
        p = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        val = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        orig = memory.read32(p)
        memory.write32(p, val)
        cpu.regs[EAX] = orig
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("kernel32.dll", "InterlockedIncrement", _interlocked_inc)
    stubs.register_handler("kernel32.dll", "InterlockedDecrement", _interlocked_dec)
    stubs.register_handler("kernel32.dll", "InterlockedExchange", _interlocked_exch)

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
            text = "".join(s)
            logger.info("handlers", f"[OutputDebugString] {text}")
            # Also lands in the real stdout.txt stream (guest_stdout_handle,
            # same sink Channel_SystemPrint uses -- Molly's request
            # 2026-08-07) rather than only tew's own /tmp/emu.log, since
            # OutputDebugString is real diagnostic text a debugger would
            # normally show and is worth having on disk the same way.
            state.write_guest_stdout(text if text.endswith("\n") else text + "\n")
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "OutputDebugStringA", _output_debug_string_a)
    stubs.register_handler("kernel32.dll", "DebugBreak", _halt("DebugBreak"))

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

    def _lstrcat_a(cpu: "CPU") -> None:
        dst = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        src = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        dst_len = 0
        while dst_len < 65535 and memory.read8(dst + dst_len) != 0:
            dst_len += 1
        i = 0
        while dst_len + i < 65535:
            ch = memory.read8(src + i)
            memory.write8(dst + dst_len + i, ch)
            if ch == 0:
                break
            i += 1
        cpu.regs[EAX] = dst
        cleanup_stdcall(cpu, memory, 8)

    def _lstrcpyn_a(cpu: "CPU") -> None:
        dst = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        src = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        max_len = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        max_len = min(max_len, 65535)
        if dst and max_len > 0:
            i = 0
            while i < max_len - 1:
                ch = memory.read8(src + i) if src else 0
                if ch == 0:
                    break
                memory.write8(dst + i, ch)
                i += 1
            memory.write8(dst + i, 0)
        cpu.regs[EAX] = dst
        cleanup_stdcall(cpu, memory, 12)

    def _lstrcmp_w(cpu: "CPU") -> None:
        p1 = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        p2 = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        result = 0
        i = 0
        while True:
            c1 = memory.read16((p1 + i * 2) & 0xFFFFFFFF) if p1 else 0
            c2 = memory.read16((p2 + i * 2) & 0xFFFFFFFF) if p2 else 0
            if c1 != c2:
                result = -1 if c1 < c2 else 1
                break
            if c1 == 0:
                break
            i += 1
        cpu.regs[EAX] = result & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 8)

    def _lstrcmpi_a(cpu: "CPU") -> None:
        # Real Windows is locale-aware for case-folding; plain ASCII
        # upper-casing is correct for every string this game actually
        # compares (matches the IsCharAlphaA/IsCharAlphaNumericA reasoning).
        p1 = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        p2 = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        result = 0
        i = 0
        while True:
            c1 = memory.read8((p1 + i) & 0xFFFFFFFF) if p1 else 0
            c2 = memory.read8((p2 + i) & 0xFFFFFFFF) if p2 else 0
            u1 = c1 - 0x20 if 0x61 <= c1 <= 0x7A else c1
            u2 = c2 - 0x20 if 0x61 <= c2 <= 0x7A else c2
            if u1 != u2:
                result = -1 if u1 < u2 else 1
                break
            if c1 == 0:
                break
            i += 1
        cpu.regs[EAX] = result & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 8)

    def _local_alloc(cpu: "CPU") -> None:
        flags = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
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
        flags = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
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

    def _global_lock(cpu: "CPU") -> None:
        # _global_alloc above always hands out fixed (non-moveable) memory --
        # a direct pointer, not a real HGLOBAL needing indirection -- so
        # locking it is the real-Windows-documented no-op for fixed memory:
        # GlobalLock just returns the same pointer back.
        h_mem = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = h_mem
        cleanup_stdcall(cpu, memory, 4)

    def _global_unlock(cpu: "CPU") -> None:
        # Same rationale as _global_lock: fixed memory has no real lock
        # count to decrement. Real Windows returns FALSE here for fixed
        # memory (GetLastError == NO_ERROR), which real callers already
        # treat as "not an error", not "still locked".
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "SetErrorMode", _set_error_mode)
    stubs.register_handler("kernel32.dll", "lstrlenA", _lstrlen_a)
    stubs.register_handler("kernel32.dll", "lstrcpyA", _lstrcpy_a)
    stubs.register_handler("kernel32.dll", "lstrcpynA", _lstrcpyn_a)
    stubs.register_handler("kernel32.dll", "lstrcatA", _lstrcat_a)
    stubs.register_handler("kernel32.dll", "lstrcmpW", _lstrcmp_w)
    stubs.register_handler("kernel32.dll", "lstrcmpiA", _lstrcmpi_a)
    stubs.register_handler("kernel32.dll", "LocalAlloc", _local_alloc)
    stubs.register_handler("kernel32.dll", "LocalFree", _local_free)
    stubs.register_handler("kernel32.dll", "GlobalAlloc", _global_alloc)
    stubs.register_handler("kernel32.dll", "GlobalFree", _global_free)
    stubs.register_handler("kernel32.dll", "GlobalLock", _global_lock)
    stubs.register_handler("kernel32.dll", "GlobalUnlock", _global_unlock)

    # ── Heap / handle ops ─────────────────────────────────────────────────────

    stubs.register_handler("kernel32.dll", "HeapValidate", _halt("HeapValidate"))
    stubs.register_handler("kernel32.dll", "HeapDestroy", _halt("HeapDestroy"))

    def _duplicate_handle(cpu: "CPU") -> None:
        """DuplicateHandle(hSourceProcess, hSource, hTargetProcess, lpTarget, access, inherit, options)

        stdcall, 7 args (28 bytes).  hSourceProcess and hTargetProcessHandle are
        ignored — the emulator is single-process and both are always the
        current-process pseudo-handle.
        """
        h_source = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        lp_target = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        dw_options = memory.read32((cpu.regs[ESP] + 28) & 0xFFFFFFFF)

        close_source = bool(dw_options & _DUPLICATE_CLOSE_SOURCE)
        new_handle = _duplicate_handle_entry(state, h_source, close_source)

        if lp_target:
            memory.write32(lp_target & 0xFFFFFFFF, new_handle)

        logger.debug(
            "handlers", f"DuplicateHandle(src=0x{h_source:08x}) -> 0x{new_handle:08x}"
        )
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 28)

    stubs.register_handler("kernel32.dll", "DuplicateHandle", _duplicate_handle)

    # ── Locale / string type ──────────────────────────────────────────────────

    stubs.register_handler("kernel32.dll", "LCMapStringA", _halt("LCMapStringA"))

    # Real CompareStringA/W validate the locale identifier (IsValidLocale)
    # and fail (return 0, GetLastError()==ERROR_INVALID_PARAMETER) for one
    # that isn't -- this emulator only ever models the single real locale
    # 0x0409 (see _is_valid_locale/_get_user_default_lcid/
    # _get_system_default_lang_id below, all hardcoded to it, "no separate
    # system-vs-user locale concept anywhere else"). Neither comparison
    # handler used to read the locale argument at all, so ANY value
    # (including 0, "no locale specified") silently "succeeded" -- found
    # live: msjet35.dll's own default-collating-order fallback logic
    # (FUN_7a84c830) deliberately probes CompareStringA with an
    # unspecified locale specifically to detect this failure and substitute
    # a safe default; tew's always-succeeds behavior meant that probe
    # never failed, so the fallback path never triggered and a database
    # opened with no explicit locale (msjet35.dll's CreateDatabase Locale=
    # "") ended up with no valid collating-order id at all -- surfacing
    # much later as a null per-session collation-interface pointer and an
    # unrelated-looking crash deep in query evaluation.
    #
    # 2026-08-25: that fix over-corrected -- LOCALE_USER_DEFAULT (0x0400)
    # and LOCALE_SYSTEM_DEFAULT (0x0800) are real Windows sentinels meaning
    # "resolve to whatever the caller's actual locale is", not "no locale
    # specified"; real CompareStringA resolves them and succeeds. dao350.dll's
    # own internal name-comparison helper (FUN_044c6284, reached from the
    # column-collection dedup check in FUN_044d1d53/FUN_044da868) calls
    # CompareStringA(LOCALE_USER_DEFAULT, ...) for every single field-name
    # comparison -- confirmed live, 371 calls in one run, all rejected as
    # "invalid locale" (EAX=0). dao350's own switch on the result maps
    # failure (0) to the SAME code as CSTR_EQUAL (2) -- real Windows
    # essentially never returns 0 for a well-formed call, so this collapsed
    # error handling is harmless there, but tew's rejection turned every
    # single name comparison through this path into an unconditional
    # "equal", regardless of actual string content. Root cause of Fields.Count
    # reading 1 instead of 10 for 3-table-join QueryDefs: every column after
    # the first got misidentified as a duplicate of it and silently skipped.
    _RESOLVABLE_LOCALES = {
        0x0400: 0x0409,
        0x0800: 0x0409,
    }  # LOCALE_USER_DEFAULT, LOCALE_SYSTEM_DEFAULT -> en-US

    def _resolve_locale(locale: int) -> int:
        return _RESOLVABLE_LOCALES.get(locale, locale)

    def _locale_is_valid(locale: int) -> bool:
        return _resolve_locale(locale) == 0x0409

    def _compare_string_a(cpu: "CPU") -> None:
        locale = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        lp1 = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cch1 = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp2 = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        cch2 = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        if not _locale_is_valid(locale):
            logger.debug(
                "handlers",
                f"CompareStringA(locale=0x{locale:08x}) -> 0 (invalid locale)",
            )
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_INVALID_PARAMETER))
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 24)
            return
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
            s1 = s1.upper()
            s2 = s2.upper()
        cpu.regs[EAX] = 1 if s1 < s2 else (3 if s1 > s2 else 2)
        cleanup_stdcall(cpu, memory, 24)

    def _compare_string_w(cpu: "CPU") -> None:
        locale = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        lp1 = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cch1 = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp2 = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        cch2 = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        if not _locale_is_valid(locale):
            logger.debug(
                "handlers",
                f"CompareStringW(locale=0x{locale:08x}) -> 0 (invalid locale)",
            )
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_INVALID_PARAMETER))
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 24)
            return
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
            s1 = s1.upper()
            s2 = s2.upper()
        cpu.regs[EAX] = 1 if s1 < s2 else (3 if s1 > s2 else 2)
        cleanup_stdcall(cpu, memory, 24)

    def _get_oemc_p(cpu: "CPU") -> None:
        cpu.regs[EAX] = 437

    def _get_user_default_lcid(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x0409

    def _get_user_default_lang_id(cpu: "CPU") -> None:
        # LANGIDFROMLCID(lcid) == lcid & 0xFFFF; for the en-US LCID above
        # (0x0409, SORT_DEFAULT already 0 in the high word) that's the same
        # value, not a coincidence to hardcode separately.
        cpu.regs[EAX] = 0x0409

    def _get_system_default_lang_id(cpu: "CPU") -> None:
        # Same value as GetUserDefaultLangID: this emulator has no separate
        # system-vs-user locale concept anywhere else (IsValidLocale below
        # hardcodes the one locale it knows about, 0x0409, the same way).
        cpu.regs[EAX] = 0x0409

    def _is_valid_locale(cpu: "CPU") -> None:
        locale = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = 1 if locale == 0x0409 else 0
        cleanup_stdcall(cpu, memory, 8)

    def _get_locale_info_w(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("kernel32.dll", "CompareStringA", _compare_string_a)
    stubs.register_handler("kernel32.dll", "CompareStringW", _compare_string_w)
    stubs.register_handler("kernel32.dll", "GetStringTypeA", _halt("GetStringTypeA"))
    stubs.register_handler("kernel32.dll", "GetOEMCP", _get_oemc_p)
    stubs.register_handler("kernel32.dll", "GetUserDefaultLCID", _get_user_default_lcid)
    stubs.register_handler(
        "kernel32.dll", "GetUserDefaultLangID", _get_user_default_lang_id
    )
    stubs.register_handler(
        "kernel32.dll", "GetSystemDefaultLangID", _get_system_default_lang_id
    )
    stubs.register_handler("kernel32.dll", "IsValidLocale", _is_valid_locale)
    stubs.register_handler(
        "kernel32.dll", "EnumSystemLocalesA", _halt("EnumSystemLocalesA")
    )
    stubs.register_handler("kernel32.dll", "GetLocaleInfoW", _get_locale_info_w)
    stubs.register_handler(
        "kernel32.dll", "SetConsoleCtrlHandler", _halt("SetConsoleCtrlHandler")
    )

    def _set_environment_variable_a(cpu: "CPU") -> None:
        """BOOL SetEnvironmentVariableA(LPCSTR lpName, LPCSTR lpValue)"""
        esp = cpu.regs[ESP]
        lp_name = memory.read32((esp + 4) & 0xFFFFFFFF)
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
        esp = cpu.regs[ESP]
        lp_name = memory.read32((esp + 4) & 0xFFFFFFFF)
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
        esp = cpu.regs[ESP]
        lp_name = memory.read32((esp + 4) & 0xFFFFFFFF)
        lp_buf = memory.read32((esp + 8) & 0xFFFFFFFF)
        n_size = memory.read32((esp + 12) & 0xFFFFFFFF)
        name = read_cstring(lp_name, memory).upper() if lp_name else ""
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
        esp = cpu.regs[ESP]
        lp_name = memory.read32((esp + 4) & 0xFFFFFFFF)
        lp_buf = memory.read32((esp + 8) & 0xFFFFFFFF)
        n_size = memory.read32((esp + 12) & 0xFFFFFFFF)
        name = read_wide_string(lp_name, memory).upper() if lp_name else ""
        value = _env_vars.get(name, "")
        if n_size > len(value):
            for i, ch in enumerate(value):
                memory.write16(lp_buf + i * 2, ord(ch))
            memory.write16(lp_buf + len(value) * 2, 0)
            cpu.regs[EAX] = len(value)
        else:
            cpu.regs[EAX] = len(value) + 1
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler(
        "kernel32.dll", "SetEnvironmentVariableA", _set_environment_variable_a
    )
    stubs.register_handler(
        "kernel32.dll", "SetEnvironmentVariableW", _set_environment_variable_w
    )
    stubs.register_handler(
        "kernel32.dll", "GetEnvironmentVariableA", _get_environment_variable_a
    )
    stubs.register_handler(
        "kernel32.dll", "GetEnvironmentVariableW", _get_environment_variable_w
    )

    def _virtual_protect(cpu: "CPU") -> None:
        """
        BOOL VirtualProtect(LPVOID lpAddress, SIZE_T dwSize,
                            DWORD flNewProtect, PDWORD lpflOldProtect)

        The emulator uses a flat, unprotected memory model — all pages are
        always read/write/execute.  We record the "old" protection as
        PAGE_EXECUTE_READ_WRITE (0x40) so callers that save and restore it
        get a consistent value, and return TRUE.
        """
        esp = cpu.regs[ESP]
        lp_old_protect = memory.read32((esp + 16) & 0xFFFFFFFF)
        if lp_old_protect:
            memory.write32(lp_old_protect, 0x40)  # PAGE_EXECUTE_READWRITE
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
        ptc = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cbtc = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if ptc and cbtc >= 8:
            memory.write32(ptc, 1)  # wPeriodMin = 1 ms
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
    stubs.register_handler("winmm.dll", "timeEndPeriod", _time_end_period)

    # ── timeSetEvent ──────────────────────────────────────────────────────────
    # MMRESULT timeSetEvent(UINT uDelay, UINT uResolution,
    #                       LPTIMECALLBACK lpTimeProc, DWORD_PTR dwUser, UINT fuEvent)
    # fuEvent: TIME_ONESHOT=0x0000, TIME_PERIODIC=0x0001
    _TIME_PERIODIC = 0x0001

    def _time_set_event(cpu: "CPU") -> None:
        sp = cpu.regs[ESP]
        u_delay = memory.read32((sp + 4) & 0xFFFFFFFF)
        lp_time_proc = memory.read32((sp + 12) & 0xFFFFFFFF)
        dw_user = memory.read32((sp + 16) & 0xFFFFFFFF)
        fu_event = memory.read32((sp + 20) & 0xFFFFFFFF)

        if u_delay == 0:
            u_delay = 1
        period_ms = u_delay if (fu_event & _TIME_PERIODIC) else 0
        due_at = state.virtual_ticks_ms + u_delay

        tid = _next_timer_id[0]
        _next_timer_id[0] += 1
        pending_timers[tid] = PendingTimer(
            id=tid,
            due_at=due_at,
            period_ms=period_ms,
            cb_addr=lp_time_proc,
            dw_user=dw_user,
            fu_event=fu_event,
        )
        logger.trace(
            "handlers",
            f"timeSetEvent(delay={u_delay}ms, proc=0x{lp_time_proc:08x}, fuEvent=0x{fu_event:x}) -> id={tid}",
        )
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
