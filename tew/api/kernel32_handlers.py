"""kernel32.dll stub handlers — process, heap, virtual memory, TLS, library loading, Sleep scheduler.

Covers handlers from GetVersion through the cooperative Sleep scheduler.
File I/O, threading creation, synchronization, and remaining misc handlers
are in kernel32_io.py, called from register_kernel32_handlers.
"""

from __future__ import annotations

import os
import time as _time_module
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.loader.dll_loader import DLLLoader

from tew.hardware.cpu import EAX, EBX, ECX, EDX, ESP, EBP, ESI, EDI
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall, DLLMAIN_TRAMPOLINE, DLLMAIN_HANDLE_STORE
from tew.api.char_type import CT_CTYPE1, GetStringTypeArgs, classify_wide_string
from tew.api.lc_map import LCMapStringArgs, lc_map_wide_string
from tew.api.win32_errors import Win32Error
from tew.api._state import (
    CRTState, FileHandleEntry, DynamicModule, PendingThreadInfo,
    EventHandle, MutexHandle,
    find_file_ci, read_cstring, read_wide_string,
    THREAD_STACK_SIZE,
)
from tew.logger import logger


# ── Heap flag constants ───────────────────────────────────────────────────────

_HEAP_NO_SERIALIZE          = 0x00000001
_HEAP_ZERO_MEMORY           = 0x00000008
_HEAP_REALLOC_IN_PLACE_ONLY = 0x00000010
_HEAP_KNOWN_ALLOC_FLAGS     = _HEAP_NO_SERIALIZE | _HEAP_ZERO_MEMORY
_HEAP_KNOWN_REALLOC_FLAGS   = _HEAP_NO_SERIALIZE | _HEAP_ZERO_MEMORY | _HEAP_REALLOC_IN_PLACE_ONLY
_HEAP_KNOWN_CREATE_FLAGS    = _HEAP_NO_SERIALIZE

# ── VirtualAlloc flag constants ───────────────────────────────────────────────

_PAGE_SIZE               = 4096
_MEM_COMMIT              = 0x00001000
_MEM_RESERVE             = 0x00002000
_PAGE_READWRITE          = 0x04
_PAGE_EXECUTE_READWRITE  = 0x40
_KNOWN_PROTECT_FLAGS     = _PAGE_READWRITE | _PAGE_EXECUTE_READWRITE
_KNOWN_ALLOC_TYPES       = _MEM_COMMIT | _MEM_RESERVE


def _fake_dll_handle(name: str) -> int:
    """Compute a fake module handle from the DLL name (hash-based)."""
    h = 0
    for ch in name.lower():
        h = ((h << 5) - h + ord(ch)) & 0xFFFFFFFF
    return (h & 0x7FFFFFFF) | 0x10000000


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
            in_stubs   = 0x00200000 <= eip < 0x00220000
            in_exe     = 0x00400000 <= eip < 0x02000000
            in_dlls    = 0x10000000 <= eip < 0x40000000
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
            # Halted unexpectedly — INT3, a halt-stub inside the thread, memory
            # fault, or an explicit cpu.halted set by a handler.
            # cpu.last_error carries the Python exception if step() caught one.
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
            # Thread is blocked until one of its handles is signaled.
            unblocked = False
            for wh in t.waiting_on_handles:
                obj = state.kernel_handle_map.get(wh)
                if obj is None or (isinstance(obj, EventHandle) and obj.signaled):
                    unblocked = True
                    break
                if isinstance(obj, MutexHandle) and not obj.locked:
                    unblocked = True
                    break
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
    state.is_running_thread = True
    state.current_thread_idx = tidx

    if runnable.saved_state:
        cpu.restore_state(runnable.saved_state)
    else:
        stack_top = state.thread_stack_next + THREAD_STACK_SIZE - 16
        state.thread_stack_next += THREAD_STACK_SIZE
        esp = stack_top - 4
        memory.write32(esp, runnable.parameter)
        esp -= 4
        from tew.api._state import THREAD_SENTINEL
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

    cpu.restore_state(main_state)
    cpu.halted = False
    state.is_running_thread = False
    state.current_thread_idx = -1

    cleanup_stdcall(cpu, memory, arg_bytes)
    return True


def _load_dll_with_dllmain(
    cpu: "CPU", memory: "Memory", stubs: Win32Handlers,
    state: CRTState, dll_loader, loaded, handle: int, arg_bytes: int,
) -> None:
    """If DLL has an entry point, invoke DllMain via stack trick; otherwise just return handle."""
    if loaded.entry_point != 0:
        logger.debug("handlers", f"LoadLibraryA: invoking DllMain @ 0x{loaded.entry_point:x}")
        memory.write32(DLLMAIN_HANDLE_STORE, handle)
        cleanup_stdcall(cpu, memory, arg_bytes)
        cpu.regs[ESP] = (cpu.regs[ESP] - 20) & 0xFFFFFFFF
        memory.write32(cpu.regs[ESP] + 0,  loaded.entry_point)
        memory.write32(cpu.regs[ESP] + 4,  DLLMAIN_TRAMPOLINE)
        memory.write32(cpu.regs[ESP] + 8,  handle)
        memory.write32(cpu.regs[ESP] + 12, 1)   # DLL_PROCESS_ATTACH
        memory.write32(cpu.regs[ESP] + 16, 0)   # lpReserved
    else:
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, arg_bytes)


def register_kernel32_handlers(
    stubs: Win32Handlers,
    memory: "Memory",
    state: CRTState,
    dll_loader: Optional["DLLLoader"] = None,
) -> None:
    """Register all kernel32.dll stub handlers."""

    # ── Version ──────────────────────────────────────────────────────────────

    def _get_version(cpu: "CPU") -> None:
        cpu.regs[EAX] = (2600 << 16) | (1 << 8) | 5  # WinXP 5.1.2600

    stubs.register_handler("kernel32.dll", "GetVersion", _get_version)

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

    stubs.register_handler("kernel32.dll", "GetVersionExA", _get_version_ex_a)

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

    stubs.register_handler("kernel32.dll", "GetVersionExW", _get_version_ex_w)

    # ── Command line / startup ────────────────────────────────────────────────

    def _get_cmd_a(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x00210024

    def _get_cmd_w(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x00210030

    stubs.register_handler("kernel32.dll", "GetCommandLineA", _get_cmd_a)
    stubs.register_handler("kernel32.dll", "GetCommandLineW", _get_cmd_w)

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

    stubs.register_handler("kernel32.dll", "GetStartupInfoA", _get_startup_a)
    stubs.register_handler("kernel32.dll", "GetStartupInfoW", _get_startup_w)

    # ── Module handles ────────────────────────────────────────────────────────

    def _resolve_module_handle(name: str) -> int:
        """Resolve a module name to a handle (base address).

        Resolution order:
          1. NULL name → main exe image base.
          2. Name ends in .exe → main exe image base.
          3. Loaded real DLL (from disk) → its base_address.
          4. Stub-only system DLL (kernel32, user32, etc.) → first handler address.
          5. Unresolvable → 0 (caller will warn).
        """
        if not name:
            return 0x00400000
        lower = name.lower()
        if lower.endswith(".exe"):
            return 0x00400000
        if dll_loader:
            # Normalise: strip .dll suffix then re-add so "KERNEL32" → "kernel32.dll"
            canonical = lower.rstrip(".dll").rstrip(".") + ".dll"
            dll = dll_loader.get_dll(name) or dll_loader.get_dll(canonical)
            if dll:
                return dll.base_address
        # Stub-only DLLs (kernel32, user32, gdi32, etc.) have handler trampolines
        # but no LoadedDLL entry.  Return the address of the first registered handler
        # for that DLL — stable, non-NULL, and lives in our mapped address space.
        stub_handle = stubs.get_stub_dll_handle(name)
        if stub_handle is not None:
            return stub_handle
        return 0

    def _get_module_handle_a(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        name = read_cstring(lp, memory) if lp != 0 else ""
        handle = _resolve_module_handle(name)
        if handle:
            logger.debug("handlers", f'GetModuleHandleA("{name}") -> 0x{handle:08x}')
        else:
            logger.warn("handlers", f'GetModuleHandleA("{name}") -> NULL (not loaded)')
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, 4)

    def _get_module_handle_w(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        name = read_wide_string(lp, memory) if lp != 0 else ""
        handle = _resolve_module_handle(name)
        if handle:
            logger.debug("handlers", f'GetModuleHandleW("{name}") -> 0x{handle:08x}')
        else:
            logger.warn("handlers", f'GetModuleHandleW("{name}") -> NULL (not loaded)')
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GetModuleHandleA", _get_module_handle_a)
    stubs.register_handler("kernel32.dll", "GetModuleHandleW", _get_module_handle_w)

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

    # ── Heap management ───────────────────────────────────────────────────────

    def _heap_create(cpu: "CPU") -> None:
        fl = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        unsupported = fl & ~_HEAP_KNOWN_CREATE_FLAGS
        if unsupported:
            logger.error("handlers",
                f"[UNIMPLEMENTED] HeapCreate — unsupported flag(s) 0x{unsupported:x} — halting")
            cpu.halted = True
            return
        h = state.next_heap_handle
        state.next_heap_handle += 1
        state.heap_handles.add(h)
        cpu.regs[EAX] = h
        cleanup_stdcall(cpu, memory, 12)

    def _get_process_heap(cpu: "CPU") -> None:
        cpu.regs[EAX] = state.process_heap

    def _heap_alloc(cpu: "CPU") -> None:
        h_heap   = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        dw_bytes = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if h_heap not in state.heap_handles:
            logger.error("handlers", f"[HeapAlloc] invalid heap 0x{h_heap:x} — halting")
            cpu.halted = True
            return
        unsupported = dw_flags & ~_HEAP_KNOWN_ALLOC_FLAGS
        if unsupported:
            logger.error("handlers",
                f"[UNIMPLEMENTED] HeapAlloc — unsupported flags 0x{unsupported:x} — halting")
            cpu.halted = True
            return
        size = dw_bytes or 1
        addr = state.simple_alloc(size)
        state.heap_alloc_owner[addr] = h_heap
        if dw_flags & _HEAP_ZERO_MEMORY:
            for i in range(size):
                memory.write8(addr + i, 0)
        cpu.regs[EAX] = addr
        cleanup_stdcall(cpu, memory, 12)

    def _heap_free(cpu: "CPU") -> None:
        h_heap   = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        lp_mem   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if h_heap not in state.heap_handles:
            logger.error("handlers", f"[HeapFree] invalid heap 0x{h_heap:x} — halting")
            cpu.halted = True
            return
        unsupported = dw_flags & ~_HEAP_NO_SERIALIZE
        if unsupported:
            logger.error("handlers",
                f"[UNIMPLEMENTED] HeapFree — unsupported flags 0x{unsupported:x} — halting")
            cpu.halted = True
            return
        if lp_mem == 0:
            cpu.regs[EAX] = 1
            cleanup_stdcall(cpu, memory, 12)
            return
        if lp_mem not in state.heap_alloc_sizes:
            logger.error("handlers", f"[HeapFree] untracked pointer 0x{lp_mem:x} — halting")
            cpu.halted = True
            return
        del state.heap_alloc_sizes[lp_mem]
        state.heap_alloc_owner.pop(lp_mem, None)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 12)

    def _heap_realloc(cpu: "CPU") -> None:
        h_heap   = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        lp_mem   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        dw_bytes = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        if h_heap not in state.heap_handles:
            logger.error("handlers", f"[HeapReAlloc] invalid heap 0x{h_heap:x} — halting")
            cpu.halted = True
            return
        unsupported = dw_flags & ~_HEAP_KNOWN_REALLOC_FLAGS
        if unsupported:
            logger.error("handlers",
                f"[UNIMPLEMENTED] HeapReAlloc — unsupported flags 0x{unsupported:x} — halting")
            cpu.halted = True
            return
        if dw_flags & _HEAP_REALLOC_IN_PLACE_ONLY:
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 16)
            return
        old_size = state.heap_alloc_sizes.get(lp_mem, 0) if lp_mem != 0 else 0
        if lp_mem != 0 and lp_mem not in state.heap_alloc_sizes:
            logger.error("handlers", f"[HeapReAlloc] untracked pointer 0x{lp_mem:x} — halting")
            cpu.halted = True
            return
        new_size = dw_bytes or 1
        new_addr = state.simple_alloc(new_size)
        state.heap_alloc_owner[new_addr] = h_heap
        copy_len = min(old_size, new_size)
        for i in range(copy_len):
            memory.write8(new_addr + i, memory.read8(lp_mem + i))
        if (dw_flags & _HEAP_ZERO_MEMORY) and new_size > old_size:
            for i in range(old_size, new_size):
                memory.write8(new_addr + i, 0)
        if lp_mem != 0:
            state.heap_alloc_sizes.pop(lp_mem, None)
            state.heap_alloc_owner.pop(lp_mem, None)
        cpu.regs[EAX] = new_addr
        cleanup_stdcall(cpu, memory, 16)

    def _heap_size(cpu: "CPU") -> None:
        h_heap   = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        lp_mem   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if h_heap not in state.heap_handles:
            logger.error("handlers", f"[HeapSize] invalid heap 0x{h_heap:x} — halting")
            cpu.halted = True
            return
        unsupported = dw_flags & ~_HEAP_NO_SERIALIZE
        if unsupported:
            logger.error("handlers",
                f"[UNIMPLEMENTED] HeapSize — unsupported flags 0x{unsupported:x} — halting")
            cpu.halted = True
            return
        sz = state.heap_alloc_sizes.get(lp_mem)
        cpu.regs[EAX] = sz if sz is not None else 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 12)

    def _heap_validate(cpu: "CPU") -> None:
        h_heap  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        # dw_flags (ESP+8) and lp_mem (ESP+12) are intentionally unused:
        # our bump allocator has no fragmentation or corruption to check.
        if h_heap not in state.heap_handles:
            logger.error("handlers", f"[HeapValidate] invalid heap 0x{h_heap:x} — halting")
            cpu.halted = True
            return
        cpu.regs[EAX] = 1  # TRUE — heap is always valid
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("kernel32.dll", "HeapCreate",    _heap_create)
    stubs.register_handler("kernel32.dll", "GetProcessHeap", _get_process_heap)
    stubs.register_handler("kernel32.dll", "HeapAlloc",     _heap_alloc)
    stubs.register_handler("kernel32.dll", "HeapFree",      _heap_free)
    stubs.register_handler("kernel32.dll", "HeapReAlloc",   _heap_realloc)
    stubs.register_handler("kernel32.dll", "HeapSize",      _heap_size)
    stubs.register_handler("kernel32.dll", "HeapValidate",  _heap_validate)

    # ── VirtualAlloc / VirtualFree ────────────────────────────────────────────

    def _virtual_alloc(cpu: "CPU") -> None:
        lp_addr  = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_size  = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        fl_type  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        fl_prot  = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        unk_type = fl_type & ~_KNOWN_ALLOC_TYPES
        if unk_type:
            logger.error("handlers",
                f"[UNIMPLEMENTED] VirtualAlloc — unsupported flAllocationType 0x{unk_type:x} — halting")
            cpu.halted = True
            return
        unk_prot = fl_prot & ~_KNOWN_PROTECT_FLAGS
        if unk_prot:
            logger.error("handlers",
                f"[UNIMPLEMENTED] VirtualAlloc — unsupported flProtect 0x{unk_prot:x} — halting")
            cpu.halted = True
            return
        page_size = ((dw_size + _PAGE_SIZE - 1) & ~(_PAGE_SIZE - 1)) & 0xFFFFFFFF
        if (fl_type & _MEM_COMMIT) and not (fl_type & _MEM_RESERVE) and lp_addr != 0:
            if lp_addr in state.virtual_reserved:
                state.virtual_committed[lp_addr] = page_size
                cpu.regs[EAX] = lp_addr
                cleanup_stdcall(cpu, memory, 16)
                return
            logger.error("handlers",
                f"[VirtualAlloc] MEM_COMMIT on unreserved 0x{lp_addr:x} — halting")
            cpu.halted = True
            return
        addr = state.next_virtual_alloc
        state.next_virtual_alloc = (
            (state.next_virtual_alloc + page_size + _PAGE_SIZE - 1) & ~(_PAGE_SIZE - 1)
        ) & 0xFFFFFFFF
        if fl_type & _MEM_RESERVE:
            state.virtual_reserved[addr] = page_size
        if fl_type & _MEM_COMMIT:
            state.virtual_committed[addr] = page_size
        cpu.regs[EAX] = addr
        cleanup_stdcall(cpu, memory, 16)

    def _virtual_free(cpu: "CPU") -> None:
        lp_addr   = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_size   = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        dw_type   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        MEM_DECOMMIT = 0x4000
        MEM_RELEASE  = 0x8000
        if dw_type == MEM_RELEASE:
            if dw_size != 0:
                logger.error("handlers", "[VirtualFree] MEM_RELEASE requires dwSize=0 — halting")
                cpu.halted = True
                return
            if lp_addr not in state.virtual_reserved:
                logger.error("handlers",
                    f"[VirtualFree] MEM_RELEASE on unreserved 0x{lp_addr:x} — halting")
                cpu.halted = True
                return
            del state.virtual_reserved[lp_addr]
            state.virtual_committed.pop(lp_addr, None)
        elif dw_type == MEM_DECOMMIT:
            state.virtual_committed.pop(lp_addr, None)
        else:
            logger.error("handlers",
                f"[UNIMPLEMENTED] VirtualFree — unsupported type 0x{dw_type:x} — halting")
            cpu.halted = True
            return
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("kernel32.dll", "VirtualAlloc", _virtual_alloc)
    stubs.register_handler("kernel32.dll", "VirtualFree",  _virtual_free)

    # ── UNIMPLEMENTED halts ───────────────────────────────────────────────────

    def _halt(name: str):
        def _h(cpu: "CPU") -> None:
            logger.error("handlers", f"[UNIMPLEMENTED] {name} — halting")
            cpu.halted = True
        return _h

    def _get_last_error(cpu: "CPU") -> None:
        cpu.regs[EAX] = state.last_error & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 0)

    def _set_last_error(cpu: "CPU") -> None:
        state.last_error = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cleanup_stdcall(cpu, memory, 4)

    # ── Time / tick functions ─────────────────────────────────────────────────

    # Monotonic start time captured once so tick counts are relative.
    _start_time = _time_module.monotonic()

    def _get_tick_count(cpu: "CPU") -> None:
        """GetTickCount() -> DWORD  (milliseconds since emulator start).

        The return value wraps after ~49.7 days.  Game code uses it for
        timeouts and animation deltas, not absolute timestamps.
        """
        elapsed_ms = int((_time_module.monotonic() - _start_time) * 1000) & 0xFFFFFFFF
        cpu.regs[EAX] = elapsed_ms
        cleanup_stdcall(cpu, memory, 0)

    # QPC frequency: we report 1 000 000 (1 MHz) so the counter value in
    # microseconds stays within easy integer range.
    _QPC_FREQ: int = 1_000_000

    def _query_performance_counter(cpu: "CPU") -> None:
        """QueryPerformanceCounter(LARGE_INTEGER* lpPerformanceCount) -> BOOL."""
        p = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if p:
            us = int((_time_module.monotonic() - _start_time) * _QPC_FREQ)
            memory.write32(p,     us & 0xFFFFFFFF)         # low DWORD
            memory.write32(p + 4, (us >> 32) & 0xFFFFFFFF) # high DWORD
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

    stubs.register_handler("kernel32.dll", "GetLastError",               _get_last_error)
    stubs.register_handler("kernel32.dll", "SetLastError",               _set_last_error)
    stubs.register_handler("kernel32.dll", "GetTickCount",               _get_tick_count)
    stubs.register_handler("kernel32.dll", "QueryPerformanceCounter",    _query_performance_counter)
    stubs.register_handler("kernel32.dll", "QueryPerformanceFrequency",  _query_performance_frequency)

    # ── GetSystemInfo ─────────────────────────────────────────────────────────

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

    # ── Critical sections ─────────────────────────────────────────────────────

    def _init_cs(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        memory.write32(ptr + 0x00, 0)
        memory.write32(ptr + 0x04, 0xFFFFFFFF)
        memory.write32(ptr + 0x08, 0)
        memory.write32(ptr + 0x0C, 0)
        memory.write32(ptr + 0x10, 0)
        memory.write32(ptr + 0x14, 0)
        cleanup_stdcall(cpu, memory, 4)

    def _init_cs_spin(cpu: "CPU") -> None:
        ptr       = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        spin_count = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        memory.write32(ptr + 0x00, 0)
        memory.write32(ptr + 0x04, 0xFFFFFFFF)
        memory.write32(ptr + 0x08, 0)
        memory.write32(ptr + 0x0C, 0)
        memory.write32(ptr + 0x10, 0)
        memory.write32(ptr + 0x14, spin_count)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _enter_cs(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        tid = state.tls_current_thread_id()
        memory.write32(ptr + 0x04, (memory.read32(ptr + 0x04) + 1) & 0xFFFFFFFF)
        memory.write32(ptr + 0x08, (memory.read32(ptr + 0x08) + 1) & 0xFFFFFFFF)
        memory.write32(ptr + 0x0C, tid)
        cleanup_stdcall(cpu, memory, 4)

    def _leave_cs(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        rec = (memory.read32(ptr + 0x08) - 1) & 0xFFFFFFFF
        memory.write32(ptr + 0x08, rec)
        memory.write32(ptr + 0x04, (memory.read32(ptr + 0x04) - 1) & 0xFFFFFFFF)
        if rec == 0:
            memory.write32(ptr + 0x0C, 0)
        cleanup_stdcall(cpu, memory, 4)

    def _delete_cs(cpu: "CPU") -> None:
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "InitializeCriticalSection",               _init_cs)
    stubs.register_handler("kernel32.dll", "InitializeCriticalSectionAndSpinCount",   _init_cs_spin)
    stubs.register_handler("kernel32.dll", "EnterCriticalSection",                    _enter_cs)
    stubs.register_handler("kernel32.dll", "LeaveCriticalSection",                    _leave_cs)
    stubs.register_handler("kernel32.dll", "DeleteCriticalSection",                   _delete_cs)

    # ── TLS ───────────────────────────────────────────────────────────────────

    TLS_OUT_OF_INDEXES = 0xFFFFFFFF

    def _tls_alloc(cpu: "CPU") -> None:
        if state.next_tls_slot >= state.tls_max_slots:
            cpu.regs[EAX] = TLS_OUT_OF_INDEXES
            return
        slot = state.next_tls_slot
        state.next_tls_slot += 1
        state.tls_slots.add(slot)
        cpu.regs[EAX] = slot

    def _tls_set_value(cpu: "CPU") -> None:
        idx = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        val = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if idx not in state.tls_slots:
            # Win32: returns FALSE for an invalid index; never halts.
            logger.warn("handlers", f"[TlsSetValue] invalid slot {idx} — returning FALSE")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 8)
            return
        state.tls_thread_store(state.tls_current_thread_id())[idx] = val
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _tls_get_value(cpu: "CPU") -> None:
        idx = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if idx not in state.tls_slots:
            # Win32: returns 0 (NULL) for an invalid index; never halts.
            logger.warn("handlers", f"[TlsGetValue] invalid slot {idx} — returning 0")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 4)
            return
        val = state.tls_thread_store(state.tls_current_thread_id()).get(idx, 0)
        cpu.regs[EAX] = val
        cleanup_stdcall(cpu, memory, 4)

    def _tls_free(cpu: "CPU") -> None:
        idx = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if idx not in state.tls_slots:
            # Win32: returns FALSE for an unallocated index; never halts.
            logger.warn("handlers", f"[TlsFree] invalid slot {idx} — returning FALSE")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 4)
            return
        state.tls_slots.discard(idx)
        for store in state.tls_store.values():
            store.pop(idx, None)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "TlsAlloc",    _tls_alloc)
    stubs.register_handler("kernel32.dll", "TlsSetValue", _tls_set_value)
    stubs.register_handler("kernel32.dll", "TlsGetValue", _tls_get_value)
    stubs.register_handler("kernel32.dll", "TlsFree",     _tls_free)

    # ── GetProcAddress ────────────────────────────────────────────────────────

    def _get_proc_address(cpu: "CPU") -> None:
        h_module  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        name_ptr  = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        proc_name: str
        if (name_ptr & 0xFFFF0000) == 0:
            proc_name = f"ordinal#{name_ptr}"
        else:
            proc_name = read_cstring(name_ptr, memory)

        dll_name: Optional[str] = None
        if h_module == 0x00400000:
            dll_name = None
        else:
            dyn = state.dynamic_modules.get(h_module)
            if dyn:
                dll_name = dyn.dll_name
            elif dll_loader:
                loaded = dll_loader.find_dll_for_address(h_module)
                if loaded:
                    dll_name = loaded.name

        if dll_name is None:
            logger.warn("handlers",
                f'GetProcAddress(0x{h_module:x}, "{proc_name}") -> NULL (unknown module)')
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 8)
            return

        handler_addr = stubs.lookup_handler_address(dll_name, proc_name)
        if handler_addr:
            logger.debug("handlers",
                f'GetProcAddress("{dll_name}", "{proc_name}") -> 0x{handler_addr:x} [handler]')
            cpu.regs[EAX] = handler_addr
            cleanup_stdcall(cpu, memory, 8)
            return

        if dll_loader:
            export_addr = dll_loader.get_export_address(dll_name, proc_name)
            if export_addr:
                logger.debug("handlers",
                    f'GetProcAddress("{dll_name}", "{proc_name}") -> 0x{export_addr:x} [export]')
                cpu.regs[EAX] = export_addr
                cleanup_stdcall(cpu, memory, 8)
                return

        logger.warn("handlers",
            f'GetProcAddress("{dll_name}", "{proc_name}") -> NULL (not found)')
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("kernel32.dll", "GetProcAddress", _get_proc_address)

    # ── LoadLibraryA ─────────────────────────────────────────────────────────

    def _load_dll_by_path(name: str, arg_bytes: int,
                          cpu: "CPU", memory: "Memory") -> bool:
        """Try to load a path-based DLL. Returns True if handled (caller should return)."""
        linux_path = state.translate_windows_path(name)
        while True:
            real_path = find_file_ci(linux_path)
            if real_path is not None:
                if dll_loader:
                    basename = os.path.basename(real_path)
                    dll_loader.add_search_path(os.path.dirname(real_path))
                    was_loaded = dll_loader.get_dll(basename) is not None
                    loaded = dll_loader.load_dll(basename, memory)
                    if loaded:
                        dll_loader.patch_dll_iats(memory, stubs)
                        handle = loaded.base_address & 0xFFFFFFFF
                        state.dynamic_modules[handle] = DynamicModule(
                            dll_name=basename.lower(),
                            base_address=loaded.base_address,
                        )
                        logger.info("handlers",
                            f'LoadLibraryA("{name}") -> 0x{handle:x} '
                            f'(loaded at 0x{loaded.base_address:x})')
                        if not was_loaded and basename.lower() == "authlogin.dll":
                            # authlogin.dll ships its own MSVC SBH (small-block heap).
                            # The allocator at offset 0xca1e cannot run because our
                            # HeapCreate/HeapAlloc stubs do not set up the SBH metadata
                            # it expects.  Replace it with simple_alloc so the rest of
                            # the DLL (TLS init, critical sections, etc.) can run
                            # normally without any other patches.
                            base = loaded.base_address
                            def _authlogin_alloc(cpu: "CPU") -> None:
                                sz = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
                                cpu.regs[EAX] = state.simple_alloc(sz or 1)
                                # __cdecl: caller cleans the stack — no cleanup_stdcall
                            stubs.patch_address(base + 0xca1e, "authlogin_heapAlloc",
                                                _authlogin_alloc)
                        if not was_loaded and loaded.entry_point != 0:
                            _load_dll_with_dllmain(cpu, memory, stubs, state,
                                                   dll_loader, loaded, handle, arg_bytes)
                            return True
                        cpu.regs[EAX] = handle
                        cleanup_stdcall(cpu, memory, arg_bytes)
                        return True
                # No dll_loader or load failed — fake handle
                fh = _fake_dll_handle(os.path.basename(name))
                state.dynamic_modules[fh] = DynamicModule(
                    dll_name=os.path.basename(name).lower(),
                    base_address=fh,
                )
                logger.debug("handlers",
                    f'LoadLibraryA("{name}") -> 0x{fh:x} (stub-only, path)')
                cpu.regs[EAX] = fh
                cleanup_stdcall(cpu, memory, arg_bytes)
                return True
            if not state.config.interactive_on_missing_file:
                logger.warn("handlers",
                    f'LoadLibraryA("{name}") -> NULL (not found: {linux_path})')
                cpu.regs[EAX] = 0
                cleanup_stdcall(cpu, memory, arg_bytes)
                return True
            print(f"\n[LoadLibrary] DLL not found: {linux_path}")
            print("  Add the file then press Enter to retry, or 'c' to skip.")
            ans = input("  > ").strip().lower()
            if ans != "c":
                linux_path = state.translate_windows_path(name)
                continue
            logger.debug("handlers", f'LoadLibraryA("{name}") -> NULL (user skipped)')
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, arg_bytes)
            return True

    def _load_dll_by_name(name: str, arg_bytes: int,
                          cpu: "CPU", memory: "Memory") -> None:
        """Try to load a name-only DLL (no path separator)."""
        if dll_loader:
            was_loaded = dll_loader.get_dll(name) is not None
            loaded = dll_loader.load_dll(name, memory)
            if loaded:
                dll_loader.patch_dll_iats(memory, stubs)
                handle = loaded.base_address & 0xFFFFFFFF
                state.dynamic_modules[handle] = DynamicModule(
                    dll_name=name.lower(), base_address=loaded.base_address)
                logger.info("handlers",
                    f'LoadLibraryA("{name}") -> 0x{handle:x} '
                    f'(loaded at 0x{loaded.base_address:x})')
                if not was_loaded and loaded.entry_point != 0:
                    _load_dll_with_dllmain(cpu, memory, stubs, state,
                                           dll_loader, loaded, handle, arg_bytes)
                    return
                cpu.regs[EAX] = handle
                cleanup_stdcall(cpu, memory, arg_bytes)
                return
        fh = _fake_dll_handle(name)
        state.dynamic_modules[fh] = DynamicModule(
            dll_name=name.lower(), base_address=fh)
        logger.debug("handlers", f'LoadLibraryA("{name}") -> 0x{fh:x} (stub-only)')
        cpu.regs[EAX] = fh
        cleanup_stdcall(cpu, memory, arg_bytes)

    def _load_library_a(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory)
        if (name.startswith("\\") or name.startswith("/")) and \
                not (len(name) > 1 and name[1] == ':'):
            name = "C:" + name
        has_sep = "\\" in name or "/" in name
        if has_sep:
            _load_dll_by_path(name, 4, cpu, memory)
        else:
            _load_dll_by_name(name, 4, cpu, memory)

    def _load_library_ex_a(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if dw_flags != 0:
            logger.error("handlers",
                f"[UNIMPLEMENTED] LoadLibraryExA dwFlags=0x{dw_flags:x} — halting")
            cpu.halted = True
            return
        name = read_cstring(name_ptr, memory) if name_ptr else ""
        has_sep = "\\" in name or "/" in name
        if has_sep:
            _load_dll_by_path(name, 12, cpu, memory)
        else:
            _load_dll_by_name(name, 12, cpu, memory)

    stubs.register_handler("kernel32.dll", "LoadLibraryA",   _load_library_a)
    stubs.register_handler("kernel32.dll", "LoadLibraryExA", _load_library_ex_a)

    def _free_library(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    def _disable_thread_lib(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "FreeLibrary",               _free_library)
    stubs.register_handler("kernel32.dll", "DisableThreadLibraryCalls", _disable_thread_lib)

    # ── Exit / debug ──────────────────────────────────────────────────────────

    def _exit_process(cpu: "CPU") -> None:
        code = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        logger.info("handlers", f"ExitProcess({code})")
        cpu.halted = True

    stubs.register_handler("kernel32.dll", "ExitProcess", _exit_process)

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

    # ── Code pages / locale ───────────────────────────────────────────────────

    def _get_acp(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1252

    def _get_cp_info(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        memory.write32(lp, 1)
        memory.write8(lp + 4, 0x3F)  # '?'
        memory.write8(lp + 5, 0)
        for i in range(12):
            memory.write8(lp + 6 + i, 0)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _is_valid_code_page(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    def _multi_byte_to_wide(cpu: "CPU") -> None:
        lp_mb  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cb_mb  = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_wc  = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        cch_wc = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        if cch_wc == 0:
            cpu.regs[EAX] = cb_mb
        else:
            count = min(cb_mb, cch_wc)
            for i in range(count):
                memory.write16(lp_wc + i * 2, memory.read8(lp_mb + i))
            cpu.regs[EAX] = count
        cleanup_stdcall(cpu, memory, 24)

    def _wide_to_multi_byte(cpu: "CPU") -> None:
        lp_wc  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cch_wc = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_mb  = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        cb_mb  = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        if cb_mb == 0:
            cpu.regs[EAX] = cch_wc
        else:
            count = min(cch_wc, cb_mb)
            for i in range(count):
                wc = memory.read16(lp_wc + i * 2)
                memory.write8(lp_mb + i, wc if wc <= 255 else 0x3F)
            cpu.regs[EAX] = count
        cleanup_stdcall(cpu, memory, 32)

    def _get_locale_info_a(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("kernel32.dll", "GetACP",                _get_acp)
    stubs.register_handler("kernel32.dll", "GetCPInfo",             _get_cp_info)
    stubs.register_handler("kernel32.dll", "IsValidCodePage",       _is_valid_code_page)
    def _get_string_type_w(cpu: "CPU") -> None:
        args = GetStringTypeArgs(
            info_type = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF),
            src_ptr   = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF),
            cch_src   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF),
            out_ptr   = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF),
        )
        if not classify_wide_string(memory, args):
            logger.error(
                "handlers",
                f"GetStringTypeW: unsupported dwInfoType {args.info_type:#010x} — halting",
            )
            cpu.halted = True
            return
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("kernel32.dll", "GetStringTypeW",        _get_string_type_w)
    stubs.register_handler("kernel32.dll", "MultiByteToWideChar",   _multi_byte_to_wide)
    stubs.register_handler("kernel32.dll", "WideCharToMultiByte",   _wide_to_multi_byte)
    def _lc_map_string_w(cpu: "CPU") -> None:
        args = LCMapStringArgs(
            locale    = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF),
            map_flags = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF),
            src_ptr   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF),
            cch_src   = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF),
            dest_ptr  = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF),
            cch_dest  = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF),
        )
        result = lc_map_wide_string(memory, args)
        if result is None:
            logger.error(
                "handlers",
                f"LCMapStringW: unsupported dwMapFlags {args.map_flags:#010x} — halting",
            )
            cpu.halted = True
            return
        cpu.regs[EAX] = result
        cleanup_stdcall(cpu, memory, 24)

    stubs.register_handler("kernel32.dll", "LCMapStringW",          _lc_map_string_w)
    stubs.register_handler("kernel32.dll", "GetLocaleInfoA",        _get_locale_info_a)
    stubs.register_handler("kernel32.dll", "FlsAlloc",              _halt("FlsAlloc"))
    stubs.register_handler("kernel32.dll", "FlsSetValue",           _halt("FlsSetValue"))
    stubs.register_handler("kernel32.dll", "FlsGetValue",           _halt("FlsGetValue"))
    stubs.register_handler("kernel32.dll", "FlsFree",               _halt("FlsFree"))

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
        # Fire all pending timer callbacks before cooperative sleep.
        # The game uses timeSetEvent+Sleep as a yield primitive; 9ms delay is a
        # Windows minimum that means nothing in an emulator — fire on any Sleep.
        from tew.api.win32_handlers import pending_timers
        due = list(pending_timers.values())
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
        # Fall through: yield to background threads so the timer thread can run.
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

    # ── Delegate remaining handlers to kernel32_io ────────────────────────────

    from tew.api.kernel32_io import register_kernel32_io_handlers
    register_kernel32_io_handlers(stubs, memory, state, dll_loader)
