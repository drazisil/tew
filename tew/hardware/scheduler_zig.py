"""ZigScheduler — drop-in replacement for tew.kernel.scheduler.Scheduler,
backed by cpu/src/scheduler.zig (see ~/.claude/plans/vast-drifting-pike.md).

Mirrors cpu_zig.py's structure: _bind_lib() registers argtypes/restype
against the shared _lib handle, then a thin class wraps the calls. The
Python-facing method names/signatures are kept identical to the old
Scheduler so the ~15 hot-path call sites across the Win32 handler files
need zero changes -- only direct ThreadState field pokes (now handle-keyed
accessor calls) and construction/wiring in _state.py change.
"""

from __future__ import annotations

import ctypes
import enum
from typing import TYPE_CHECKING, Optional

from tew.hardware._kernel_lib import _lib

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory

# ── Constants (mirror tew/kernel/scheduler.py / cpu/src/scheduler.zig) ──────
THREAD_STACK_BASE = 0x08000000
THREAD_STACK_SIZE = 256 * 1024
THREAD_SENTINEL = 0x001FE000


class ThreadStatus(enum.IntEnum):
    """Values match cpu/src/scheduler.zig's `ThreadStatus = enum(u8) { ready,
    blocked_cs, blocked_handles, sleeping, dead }` exactly -- see the
    "RegKey alignment"-style guard in that file's own tests for the native
    side of this contract."""
    READY = 0
    BLOCKED_CS = 1
    BLOCKED_HANDLES = 2
    SLEEPING = 3
    DEAD = 4


# Sentinels returned by the native accessors for "handle/index not found" --
# see scheduler.zig's "Public: handle-keyed thread accessors" section.
_STATUS_NOT_FOUND = 0xFF
_THREAD_ID_NOT_FOUND = -1
_HANDLE_AT_IDX_NOT_FOUND = -1


# ── Bind libcpu.so ────────────────────────────────────────────────────────────

def _bind_lib() -> ctypes.CDLL:
    lib = _lib

    _u8 = ctypes.c_uint8
    _u32 = ctypes.c_uint32
    _i8 = ctypes.c_int8
    _i32 = ctypes.c_int32
    _i64 = ctypes.c_int64
    _vp = ctypes.c_void_p
    _b = ctypes.c_bool
    _u32p = ctypes.POINTER(_u32)

    lib.scheduler_create.argtypes = []
    lib.scheduler_create.restype = _vp
    lib.scheduler_destroy.argtypes = [_vp]
    lib.scheduler_destroy.restype = None

    lib.scheduler_create_main_thread.argtypes = [_vp, _u32, _u32]
    lib.scheduler_create_main_thread.restype = None
    lib.scheduler_create_thread.argtypes = [_vp, _u32, _u32, _u32, _u32, _b]
    lib.scheduler_create_thread.restype = _u32

    lib.scheduler_switch_to.argtypes = [_vp, _vp, _u32]
    lib.scheduler_switch_to.restype = _b
    lib.scheduler_preempt_slice.argtypes = [_vp, _vp]
    lib.scheduler_preempt_slice.restype = _b
    lib.scheduler_pick_next_ready.argtypes = [_vp, _vp]
    lib.scheduler_pick_next_ready.restype = _i32

    lib.scheduler_enter_reentrant_call.argtypes = [_vp]
    lib.scheduler_enter_reentrant_call.restype = None
    lib.scheduler_exit_reentrant_call.argtypes = [_vp]
    lib.scheduler_exit_reentrant_call.restype = None
    lib.scheduler_reentrant_depth.argtypes = [_vp]
    lib.scheduler_reentrant_depth.restype = _u32
    lib.scheduler_current_idx.argtypes = [_vp]
    lib.scheduler_current_idx.restype = _i32
    lib.scheduler_thread_count.argtypes = [_vp]
    lib.scheduler_thread_count.restype = _u32
    lib.scheduler_get_virtual_ticks_ms.argtypes = [_vp]
    lib.scheduler_get_virtual_ticks_ms.restype = _u32
    lib.scheduler_set_virtual_ticks_ms.argtypes = [_vp, _u32]
    lib.scheduler_set_virtual_ticks_ms.restype = None

    lib.scheduler_complete_block_on_cs.argtypes = [_vp, _vp, _u32, _u32, _i32]
    lib.scheduler_complete_block_on_cs.restype = _b
    lib.scheduler_complete_block_on_handles.argtypes = [
        _vp, _vp, _u32p, _u32, _u32, _b, _u32, _i32]
    lib.scheduler_complete_block_on_handles.restype = _b
    lib.scheduler_complete_sleep_current.argtypes = [_vp, _vp, _u32, _u32, _u32, _i32]
    lib.scheduler_complete_sleep_current.restype = _b
    lib.scheduler_complete_mark_current_dead.argtypes = [_vp, _vp, _i32]
    lib.scheduler_complete_mark_current_dead.restype = _b
    lib.scheduler_terminate_thread.argtypes = [_vp, _vp, _u32, _i32]
    lib.scheduler_terminate_thread.restype = _i8
    lib.scheduler_unblock_cs.argtypes = [_vp, _u32]
    lib.scheduler_unblock_cs.restype = None
    lib.scheduler_unblock_handle.argtypes = [_vp, _u32]
    lib.scheduler_unblock_handle.restype = _u32
    lib.scheduler_tick.argtypes = [_vp, _u32]
    lib.scheduler_tick.restype = None

    lib.scheduler_tls_alloc_slot.argtypes = [_vp, _u8]
    lib.scheduler_tls_alloc_slot.restype = None
    lib.scheduler_tls_free_slot.argtypes = [_vp, _u8]
    lib.scheduler_tls_free_slot.restype = None
    lib.scheduler_tls_slot_allocated.argtypes = [_vp, _u8]
    lib.scheduler_tls_slot_allocated.restype = _b

    lib.scheduler_get_suspended.argtypes = [_vp, _u32]
    lib.scheduler_get_suspended.restype = _b
    lib.scheduler_set_suspended.argtypes = [_vp, _u32, _b]
    lib.scheduler_set_suspended.restype = None
    lib.scheduler_get_completed.argtypes = [_vp, _u32]
    lib.scheduler_get_completed.restype = _b
    lib.scheduler_get_wait_timed_out.argtypes = [_vp, _u32]
    lib.scheduler_get_wait_timed_out.restype = _b
    lib.scheduler_set_wait_timed_out.argtypes = [_vp, _u32, _b]
    lib.scheduler_set_wait_timed_out.restype = None
    lib.scheduler_get_status.argtypes = [_vp, _u32]
    lib.scheduler_get_status.restype = _u8
    lib.scheduler_get_thread_id.argtypes = [_vp, _u32]
    lib.scheduler_get_thread_id.restype = _i64
    lib.scheduler_handle_at_idx.argtypes = [_vp, _u32]
    lib.scheduler_handle_at_idx.restype = _i64
    lib.scheduler_current_handle.argtypes = [_vp]
    lib.scheduler_current_handle.restype = _u32

    return lib


_bind_lib()


# ── current_thread() proxy ────────────────────────────────────────────────────

class _CurrentThreadProxy:
    """Returned by ZigScheduler.current_thread(). Exposes only the 3 real
    external readers of the old ThreadState object (.thread_id read-only,
    .wait_timed_out read/write) -- not a full ThreadState-shaped object,
    since nothing else is ever read off the result (see the plan's
    call-site inventory)."""
    __slots__ = ("_sched", "handle")

    def __init__(self, sched: "ZigScheduler", handle: int) -> None:
        self._sched = sched
        self.handle = handle

    @property
    def thread_id(self) -> int:
        tid = _lib.scheduler_get_thread_id(self._sched._sched, self.handle)
        return tid if tid != _THREAD_ID_NOT_FOUND else 0

    @property
    def wait_timed_out(self) -> bool:
        return bool(_lib.scheduler_get_wait_timed_out(self._sched._sched, self.handle))

    @wait_timed_out.setter
    def wait_timed_out(self, val: bool) -> None:
        _lib.scheduler_set_wait_timed_out(self._sched._sched, self.handle, val)


# ── ZigScheduler ──────────────────────────────────────────────────────────────

class ZigScheduler:
    """Cooperative thread scheduler backed by cpu/src/scheduler.zig."""

    def __init__(self) -> None:
        self._sched: int = _lib.scheduler_create()
        if not self._sched:
            raise RuntimeError("scheduler_create returned NULL")
        # Set to Kernel by CRTState after construction, same as the old
        # Scheduler -- the one place this class calls back into a
        # non-cpu/non-memory Python collaborator (Kernel.tick() does real
        # socket select()/window-manager posting), see the plan's Design
        # Decision 2 for why that can't move into Zig as-is.
        self._kernel: Optional[object] = None

    def __del__(self) -> None:
        if getattr(self, "_sched", 0):
            _lib.scheduler_destroy(self._sched)
            self._sched = 0

    # ── Thread registration ───────────────────────────────────────────────────

    def create_main_thread(self, thread_id: int, handle: int) -> None:
        _lib.scheduler_create_main_thread(self._sched, thread_id & 0xFFFFFFFF, handle & 0xFFFFFFFF)

    def create_thread(self, thread_id: int, handle: int, start_address: int,
                       parameter: int, suspended: bool = False) -> None:
        """Registers a new background thread. Unlike the old Scheduler,
        returns nothing -- callers already have `handle` in hand (it's
        their own argument), so there's no ThreadState object to hand
        back; see kernel32_io.py's _create_thread, which now appends
        `handle` directly to state.pending_threads instead of a return
        value."""
        _lib.scheduler_create_thread(
            self._sched, thread_id & 0xFFFFFFFF, handle & 0xFFFFFFFF,
            start_address & 0xFFFFFFFF, parameter & 0xFFFFFFFF, suspended)

    # ── Queries ────────────────────────────────────────────────────────────────

    @property
    def current_idx(self) -> int:
        return _lib.scheduler_current_idx(self._sched)

    @property
    def thread_count(self) -> int:
        return _lib.scheduler_thread_count(self._sched)

    @property
    def reentrant_depth(self) -> int:
        return _lib.scheduler_reentrant_depth(self._sched)

    @property
    def virtual_ticks_ms(self) -> int:
        return _lib.scheduler_get_virtual_ticks_ms(self._sched)

    @virtual_ticks_ms.setter
    def virtual_ticks_ms(self, val: int) -> None:
        _lib.scheduler_set_virtual_ticks_ms(self._sched, val & 0xFFFFFFFF)

    def current_thread(self) -> _CurrentThreadProxy:
        idx = _lib.scheduler_current_idx(self._sched)
        if idx < 0:
            raise RuntimeError(f"No current thread (current_idx={idx})")
        handle = _lib.scheduler_current_handle(self._sched)
        return _CurrentThreadProxy(self, handle)

    def status_at_idx(self, idx: int) -> Optional[ThreadStatus]:
        """Translator for user32_handlers.py's index-based DEAD check
        (`scheduler.threads[idx].status` in the old Scheduler) -- looks up
        the handle at `idx` first (scheduler_handle_at_idx), then the
        handle-keyed status accessor. Returns None if idx is out of range."""
        handle = _lib.scheduler_handle_at_idx(self._sched, idx)
        if handle == _HANDLE_AT_IDX_NOT_FOUND:
            return None
        status = _lib.scheduler_get_status(self._sched, handle & 0xFFFFFFFF)
        if status == _STATUS_NOT_FOUND:
            return None
        return ThreadStatus(status)

    def any_runnable(self) -> bool:
        """True if any thread is ready to run (not dead, sleeping, blocked,
        or suspended). No production call sites (kept for API-shape parity
        -- see changelog); composes the handle-keyed accessors rather than
        a dedicated native export since it's never on a hot path."""
        for idx in range(self.thread_count):
            handle = _lib.scheduler_handle_at_idx(self._sched, idx)
            if handle == _HANDLE_AT_IDX_NOT_FOUND:
                continue
            h = handle & 0xFFFFFFFF
            status = _lib.scheduler_get_status(self._sched, h)
            if status == ThreadStatus.READY and not _lib.scheduler_get_suspended(self._sched, h):
                return True
        return False

    # ── Direct-field accessors (handle-keyed) ─────────────────────────────────
    # Migrated call sites: kernel32_io.py's _resume_thread/_suspend_thread/
    # _get_exit_code_thread now go through these instead of poking a
    # ThreadState object directly (there is no such object anymore).

    def get_suspended(self, handle: int) -> bool:
        return bool(_lib.scheduler_get_suspended(self._sched, handle & 0xFFFFFFFF))

    def set_suspended(self, handle: int, val: bool) -> None:
        _lib.scheduler_set_suspended(self._sched, handle & 0xFFFFFFFF, val)

    def get_thread_id(self, handle: int) -> Optional[int]:
        """None if handle doesn't match any known thread (matches the other
        accessors' benign-sentinel convention -- see scheduler.zig)."""
        tid = _lib.scheduler_get_thread_id(self._sched, handle & 0xFFFFFFFF)
        return None if tid == _THREAD_ID_NOT_FOUND else tid

    def get_completed(self, handle: int) -> bool:
        return bool(_lib.scheduler_get_completed(self._sched, handle & 0xFFFFFFFF))

    # ── TLS bitset ─────────────────────────────────────────────────────────────
    # Migrated call sites: kernel32_sync.py's TlsAlloc/TlsSetValue/
    # TlsGetValue/TlsFree handlers -- replaces the old CRTState.tls_slots
    # set[int] (shared by reference into the old Scheduler) entirely.

    def tls_alloc_slot(self, slot: int) -> None:
        _lib.scheduler_tls_alloc_slot(self._sched, slot & 0xFF)

    def tls_free_slot(self, slot: int) -> None:
        _lib.scheduler_tls_free_slot(self._sched, slot & 0xFF)

    def tls_slot_allocated(self, slot: int) -> bool:
        return bool(_lib.scheduler_tls_slot_allocated(self._sched, slot & 0xFF))

    # ── Public: context switch ────────────────────────────────────────────────

    def switch_to(self, cpu: "CPU", memory: "Memory", idx: int) -> bool:
        return bool(_lib.scheduler_switch_to(self._sched, cpu.native_handle, idx & 0xFFFFFFFF))

    def preempt_slice(self, cpu: "CPU", memory: "Memory") -> bool:
        return bool(_lib.scheduler_preempt_slice(self._sched, cpu.native_handle))

    # ── Reentrancy guard ───────────────────────────────────────────────────────

    def enter_reentrant_call(self) -> None:
        _lib.scheduler_enter_reentrant_call(self._sched)

    def exit_reentrant_call(self) -> None:
        _lib.scheduler_exit_reentrant_call(self._sched)

    # ── Internal: kernel-tick fallback (Design Decision 2) ────────────────────
    # `scheduler_pick_next_ready` stops short of the kernel-tick branch; this
    # resolves next_idx the same way every one of the 4 public blocking/
    # dead-marking methods below needs to: try once, and if nothing is
    # ready, give Kernel.tick() a chance to signal something (real socket
    # I/O, window messages) before trying once more.

    def _resolve_next_idx(self, cpu: "CPU") -> int:
        next_idx = _lib.scheduler_pick_next_ready(self._sched, cpu.native_handle)
        if next_idx < 0 and self._kernel is not None:
            self._kernel.tick()
            next_idx = _lib.scheduler_pick_next_ready(self._sched, cpu.native_handle)
        return next_idx

    # ── Public: blocking operations ───────────────────────────────────────────
    # Each checks cpu.fatal_halt / reentrant_depth BEFORE calling
    # scheduler_pick_next_ready -- see scheduler.zig's "Public: blocking
    # operations" header comment for why this ordering matters (pick_next_
    # ready has real wake side effects that the original Python never
    # triggers on a refused/fatally-halted call).

    def block_current_on_cs(self, cpu: "CPU", memory: "Memory",
                             cs_ptr: int, retry_eip: int) -> None:
        if cpu.fatal_halt:
            return
        cs_ptr &= 0xFFFFFFFF
        retry_eip &= 0xFFFFFFFF
        if self.reentrant_depth > 0:
            _lib.scheduler_complete_block_on_cs(self._sched, cpu.native_handle, cs_ptr, retry_eip, -1)
            return
        next_idx = self._resolve_next_idx(cpu)
        _lib.scheduler_complete_block_on_cs(self._sched, cpu.native_handle, cs_ptr, retry_eip, next_idx)

    def block_current_on_handles(self, cpu: "CPU", memory: "Memory",
                                  handles: frozenset, retry_eip: int,
                                  deadline_ms: Optional[int] = None) -> None:
        if cpu.fatal_halt:
            return
        retry_eip &= 0xFFFFFFFF
        handle_list = [h & 0xFFFFFFFF for h in handles]
        arr = (ctypes.c_uint32 * len(handle_list))(*handle_list)
        has_deadline = deadline_ms is not None
        deadline_val = (deadline_ms & 0xFFFFFFFF) if has_deadline else 0
        if self.reentrant_depth > 0:
            _lib.scheduler_complete_block_on_handles(
                self._sched, cpu.native_handle, arr, len(handle_list),
                retry_eip, has_deadline, deadline_val, -1)
            return
        next_idx = self._resolve_next_idx(cpu)
        _lib.scheduler_complete_block_on_handles(
            self._sched, cpu.native_handle, arr, len(handle_list),
            retry_eip, has_deadline, deadline_val, next_idx)

    def sleep_current(self, cpu: "CPU", memory: "Memory",
                       return_eip: int, eax_val: int, sleep_ms: int) -> None:
        if cpu.fatal_halt:
            return
        return_eip &= 0xFFFFFFFF
        eax_val &= 0xFFFFFFFF
        sleep_ms &= 0xFFFFFFFF
        if self.reentrant_depth > 0:
            _lib.scheduler_complete_sleep_current(
                self._sched, cpu.native_handle, return_eip, eax_val, sleep_ms, -1)
            return
        next_idx = self._resolve_next_idx(cpu)
        _lib.scheduler_complete_sleep_current(
            self._sched, cpu.native_handle, return_eip, eax_val, sleep_ms, next_idx)

    def mark_current_dead(self, cpu: "CPU", memory: "Memory") -> None:
        """Deliberately does NOT check reentrant_depth -- a thread dying
        mid-nested-call must still hand off the CPU; see scheduler.zig's
        completeMarkCurrentDead docstring."""
        if cpu.fatal_halt:
            return
        next_idx = self._resolve_next_idx(cpu)
        _lib.scheduler_complete_mark_current_dead(self._sched, cpu.native_handle, next_idx)

    def terminate_thread(self, cpu: "CPU", memory: "Memory", handle: int) -> Optional[bool]:
        """Returns None if handle doesn't match any known thread, True if a
        *different* thread was terminated, False if the *current* thread
        terminated itself (matches the old tri-state Optional[bool]
        exactly). Only resolves next_idx (which calls scheduler_pick_next_
        ready, with its wake side effects) when `handle` actually is the
        current thread's -- the common "kill some other thread" path never
        touches pick_next_ready, matching the original Python, which only
        ever reaches _pick_next_ready via mark_current_dead's own call."""
        handle &= 0xFFFFFFFF
        current_idx = _lib.scheduler_current_idx(self._sched)
        is_self = current_idx >= 0 and handle == _lib.scheduler_current_handle(self._sched)
        if is_self:
            next_idx = -1 if cpu.fatal_halt else self._resolve_next_idx(cpu)
        else:
            next_idx = -1  # unused by the Zig side for the different-thread/not-found branches
        result = _lib.scheduler_terminate_thread(self._sched, cpu.native_handle, handle, next_idx)
        if result == -1:
            return None
        return result == 1

    # ── Public: unblocking ────────────────────────────────────────────────────

    def unblock_cs(self, cs_ptr: int) -> None:
        _lib.scheduler_unblock_cs(self._sched, cs_ptr & 0xFFFFFFFF)

    def unblock_handle(self, handle: int) -> int:
        return _lib.scheduler_unblock_handle(self._sched, handle & 0xFFFFFFFF)

    # ── Public: clock ──────────────────────────────────────────────────────────

    def tick(self, ms: int, memory: "Memory") -> None:
        _lib.scheduler_tick(self._sched, ms & 0xFFFFFFFF)
