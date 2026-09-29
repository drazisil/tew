"""kernel32.dll synchronization handlers — critical sections and TLS."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers
    from tew.api._state import CRTState

from tew.hardware.cpu_zig import EAX, ESP
from tew.api.win32_handlers import cleanup_stdcall
from tew.api._state import TEB_BASE, EventHandle
from tew.logger import logger


def register_kernel32_sync_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register critical section and TLS handlers."""

    # ── Critical sections ─────────────────────────────────────────────────────

    # ── Guest-side RTL_CRITICAL_SECTION, the way XP's ntdll lays it out ───────
    # Disassembled from XP ntdll (RtlInitializeCriticalSectionAndSpinCount
    # 7c9114fa, RtlDeleteCriticalSection 7c91135a). The 24-byte struct:
    # +0 DebugInfo, +4 LockCount, +8 RecursionCount, +0xC OwningThread,
    # +0x10 LockSemaphore, +0x14 SpinCount. DebugInfo points to a separate
    # 32-byte RTL_CRITICAL_SECTION_DEBUG: +0 WORD Type (0 = critical
    # section), +2 WORD CreatorBackTraceIndex, +4 CriticalSection
    # (back-pointer), +8 ProcessLocksList (LIST_ENTRY, linked at the tail of
    # the process-wide RtlCriticalSectionList), +0x10 EntryCount,
    # +0x14 ContentionCount, +0x18 Spare[2] (never written by XP).
    _CS_SIZE = 0x18
    _CS_DEBUG_SIZE = 0x20

    # RtlCriticalSectionList: the process-wide list head (a LIST_ENTRY that
    # lives in ntdll's .data on XP). Allocated on first use, empty list =
    # Flink and Blink both pointing at the head itself.
    cs_list_head = 0

    def _cs_list_head() -> int:
        nonlocal cs_list_head
        if cs_list_head == 0:
            cs_list_head = state.simple_alloc(8, fill=0)
            memory.write32(cs_list_head, cs_list_head)
            memory.write32(cs_list_head + 4, cs_list_head)
        return cs_list_head

    def _write_initialized_cs(ptr: int) -> None:
        """RtlInitializeCriticalSectionAndSpinCount's effect on the guest
        struct. SpinCount is stored as 0: XP only keeps the caller's spin
        count when the PEB reports more than one processor, and tew reports
        one (GetSystemInfo)."""
        debug = state.simple_alloc(_CS_DEBUG_SIZE, fill=0)
        # Type=0 and CreatorBackTraceIndex=0 (XP's RtlLogStackBackTrace
        # returns 0 without a stack-trace database), EntryCount and
        # ContentionCount=0 all come from the zero fill.
        memory.write32(debug + 0x04, ptr)
        head = _cs_list_head()
        tail = memory.read32(head + 4)
        memory.write32(debug + 0x08, head)       # Flink -> list head
        memory.write32(debug + 0x0C, tail)       # Blink -> old tail
        memory.write32(tail, debug + 0x08)       # old tail's Flink
        memory.write32(head + 4, debug + 0x08)   # head's Blink
        memory.write32(ptr + 0x00, debug)        # DebugInfo
        memory.write32(ptr + 0x04, 0xFFFFFFFF)   # LockCount = -1 (free)
        memory.write32(ptr + 0x08, 0)            # RecursionCount
        memory.write32(ptr + 0x0C, 0)            # OwningThread
        memory.write32(ptr + 0x10, 0)            # LockSemaphore (created lazily)
        memory.write32(ptr + 0x14, 0)            # SpinCount

    def _init_cs(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        _write_initialized_cs(ptr)
        # kernel32's InitializeCriticalSection is void; ntdll's own
        # RtlInitializeCriticalSection (same struct layout, same effect --
        # real kernel32 just forwards to it) returns NTSTATUS STATUS_SUCCESS.
        # Setting EAX=0 here is correct for both and harmless for the void case.
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    def _init_cs_spin(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        # spin_count (arg at ESP+8) is dropped: see _write_initialized_cs.
        _write_initialized_cs(ptr)
        cpu.regs[EAX] = 1  # BOOL TRUE
        cleanup_stdcall(cpu, memory, 8)

    # ntdll's own RtlInitializeCriticalSectionAndSpinCount -- same struct/
    # effect as kernel32's version above (which forwards to it on real
    # Windows), but returns NTSTATUS (0 = STATUS_SUCCESS) instead of BOOL.
    def _rtl_init_cs_spin(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        _write_initialized_cs(ptr)
        cpu.regs[EAX] = 0  # STATUS_SUCCESS
        cleanup_stdcall(cpu, memory, 8)

    # ── Enter/Leave/TryEnter: guest code, the way XP's ntdll does it ─────────
    # Same logic as XP's RtlEnterCriticalSection (7c901000, SpinCount==0
    # path), RtlLeaveCriticalSection (7c9010e0) and RtlTryEnterCriticalSection,
    # run as real x86 in the stub region so the uncontended paths never leave
    # the emulator. LockCount counts the owner's entries (recursions included)
    # plus every waiter, so -1 = free. Only two paths trap to Python:
    #   Enter, held by another thread: this thread already counted itself in
    #     LockCount, so it waits on LockSemaphore and, once woken, OWNS the CS
    #     (handoff -- it falls into the acquire path, no retry).
    #   Leave, LockCount still >= 0 after the release: someone is waiting, so
    #     signal LockSemaphore to hand the CS to exactly one waiter.
    # Assembled with GNU as (--32, intel syntax).
    _CS_GUEST_CODE = bytes.fromhex(
        # EnterCriticalSection (+0x00)
        "8b4c2404"          # mov  ecx, [esp+4]         ; cs
        "648b1524000000"    # mov  edx, fs:[0x24]       ; ClientId.UniqueThread
        "f0ff4104"          # lock inc dword [ecx+4]    ; LockCount
        "750f"              # jnz  .busy (+0x20)
        # .own (+0x11)
        "89510c"            # mov  [ecx+0xC], edx       ; OwningThread
        "c7410801000000"    # mov  dword [ecx+8], 1     ; RecursionCount
        "31c0"              # xor  eax, eax
        "c20400"            # ret  4
        # .busy (+0x20)
        "39510c"            # cmp  [ecx+0xC], edx
        "7508"              # jne  .wait (+0x2d)
        "ff4108"            # inc  dword [ecx+8]        ; recursive entry
        "31c0"              # xor  eax, eax
        "c20400"            # ret  4
        # .wait (+0x2d)
        "cdfe"              # int  0xFE                 ; -> _cs_enter_wait
        "ebe0"              # jmp  .own (+0x11)
        # LeaveCriticalSection (+0x31)
        "8b4c2404"          # mov  ecx, [esp+4]
        "ff4908"            # dec  dword [ecx+8]        ; RecursionCount
        "7512"              # jnz  .nested (+0x4c)
        "c7410c00000000"    # mov  dword [ecx+0xC], 0   ; OwningThread
        "f0ff4904"          # lock dec dword [ecx+4]
        "7d0e"              # jge  .wake (+0x55)
        "31c0"              # xor  eax, eax
        "c20400"            # ret  4
        # .nested (+0x4c)
        "f0ff4904"          # lock dec dword [ecx+4]
        "31c0"              # xor  eax, eax
        "c20400"            # ret  4
        # .wake (+0x55)
        "cdfe"              # int  0xFE                 ; -> _cs_leave_wake
        "31c0"              # xor  eax, eax
        "c20400"            # ret  4
        # TryEnterCriticalSection (+0x5c)
        "8b4c2404"          # mov  ecx, [esp+4]
        "b8ffffffff"        # mov  eax, -1
        "31d2"              # xor  edx, edx
        "f00fb15104"        # lock cmpxchg [ecx+4], edx ; free (-1) -> 0
        "648b1524000000"    # mov  edx, fs:[0x24]
        "7512"              # jne  .tbusy (+0x87)
        "89510c"            # mov  [ecx+0xC], edx
        "c7410801000000"    # mov  dword [ecx+8], 1
        "b801000000"        # mov  eax, 1
        "c20400"            # ret  4
        # .tbusy (+0x87)
        "39510c"            # cmp  [ecx+0xC], edx
        "750f"              # jne  .tfail (+0x9b)
        "f0ff4104"          # lock inc dword [ecx+4]
        "ff4108"            # inc  dword [ecx+8]
        "b801000000"        # mov  eax, 1
        "c20400"            # ret  4
        # .tfail (+0x9b)
        "31c0"              # xor  eax, eax
        "c20400"            # ret  4
    )
    _CS_ENTER, _CS_ENTER_WAIT, _CS_LEAVE, _CS_LEAVE_WAKE, _CS_TRY_ENTER = 0x00, 0x2D, 0x31, 0x55, 0x5C

    def _lock_semaphore(ptr: int) -> tuple[int, EventHandle]:
        """The CS's LockSemaphore: an auto-reset event, created on first need
        (XP's RtlpCreateCriticalSectionSem) and stored at +0x10."""
        h = memory.read32((ptr + 0x10) & 0xFFFFFFFF)
        if h == 0:
            h = state.next_kernel_handle
            state.next_kernel_handle += 1
            event = EventHandle(signaled=False, manual_reset=False)
            state.kernel_handle_map[h] = event
            memory.write32(ptr + 0x10, h)
            return h, event
        event = state.kernel_handle_map.get(h)
        if not isinstance(event, EventHandle):
            raise RuntimeError(
                f"critical section 0x{ptr:08x}: LockSemaphore 0x{h:08x} is not an event tew created")
        return h, event

    # Contended Enter. Returning normally continues into the acquire path
    # with this thread as owner; blocking re-runs this INT 0xFE when woken.
    def _cs_enter_wait(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        h, event = _lock_semaphore(ptr)
        if event.signaled:
            event.signaled = False  # auto-reset: this waiter takes the handoff
            return
        state.scheduler.block_current_on_handles(
            cpu, memory, frozenset([h]), (cpu.eip - 2) & 0xFFFFFFFF)

    # Leave with waiters: wake one (XP's RtlpUnWaitCriticalSection).
    def _cs_leave_wake(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        h, event = _lock_semaphore(ptr)
        event.signaled = True
        state.scheduler.unblock_handle(h)

    cs_exports = {
        "EnterCriticalSection": _CS_ENTER,
        "LeaveCriticalSection": _CS_LEAVE,
        "TryEnterCriticalSection": _CS_TRY_ENTER,
    }
    cs_hooks = {
        _CS_ENTER_WAIT: ("EnterCriticalSection:wait", _cs_enter_wait),
        _CS_LEAVE_WAKE: ("LeaveCriticalSection:wake", _cs_leave_wake),
    }

    # XP's RtlDeleteCriticalSection: close LockSemaphore if one was created,
    # unlink DebugInfo from RtlCriticalSectionList, zero and free it, then
    # zero the whole 24-byte struct. A DebugInfo of 0 (already deleted, or
    # never initialized) skips the debug-block part, as on XP.
    def _delete_cs(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if memory.read32((ptr + 0x10) & 0xFFFFFFFF) != 0:
            h, _event = _lock_semaphore(ptr)
            del state.kernel_handle_map[h]
        debug = memory.read32(ptr)
        if debug != 0:
            flink = memory.read32(debug + 0x08)
            blink = memory.read32(debug + 0x0C)
            memory.write32(blink, flink)       # Blink->Flink = Flink
            memory.write32(flink + 4, blink)   # Flink->Blink = Blink
            memory.load(debug, bytes(_CS_DEBUG_SIZE))
            state.simple_free(debug)
        memory.load(ptr, bytes(_CS_SIZE))
        cleanup_stdcall(cpu, memory, 4)

    # InitializeSListHead(PSLIST_HEADER) -> void
    # SLIST_HEADER is an 8-byte (32-bit) aligned union (Depth/Sequence/Next);
    # zeroing it is the real implementation's own effect (empty, depth 0).
    def _init_slist_head(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        memory.write32(ptr,     0)
        memory.write32(ptr + 4, 0)
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "InitializeSListHead",                    _init_slist_head)
    # RtlInitializeResource(PRTL_RESOURCE) -> void
    # RTL_RESOURCE = embedded RTL_CRITICAL_SECTION (24 bytes, same layout/
    # init as _init_cs above) + 8 ULONG/HANDLE fields (semaphores, waiter
    # counts, NumberOfActive, owner thread, flags, debug info), all zeroed
    # by the real init -- acquire/exclusive/shared semantics aren't modeled
    # since nothing has needed them yet; add RtlAcquireResourceShared/
    # Exclusive/RtlReleaseResource for real if that ever halts.
    def _rtl_init_resource(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        memory.write32(ptr + 0x00, 0)
        memory.write32(ptr + 0x04, 0xFFFFFFFF)
        memory.write32(ptr + 0x08, 0)
        memory.write32(ptr + 0x0C, 0)
        memory.write32(ptr + 0x10, 0)
        memory.write32(ptr + 0x14, 0)
        for _off in (0x18, 0x1C, 0x20, 0x24, 0x28, 0x2C, 0x30, 0x34):
            memory.write32(ptr + _off, 0)
        cleanup_stdcall(cpu, memory, 4)

    # RtlAcquireResourceExclusive(PRTL_RESOURCE, BOOLEAN Wait) -> BOOLEAN
    # RtlReleaseResource(PRTL_RESOURCE) -> void
    # No real blocking is modeled -- the cooperative scheduler can still
    # swap to a different thread mid-critical-section (e.g. on a Sleep or a
    # contested CS elsewhere) while this resource is held, so a naive
    # unconditional-success acquire would let that other thread "acquire"
    # the same exclusive resource too, clobbering NumberOfActive/
    # ExclusiveOwnerThread and corrupting the resource's bookkeeping.
    # Recursive acquisition by the SAME thread is real, documented Windows
    # behavior and is allowed here without changing state; a genuinely
    # contested acquire (different thread, already held) returns FALSE --
    # true blocking isn't implemented, so Wait=TRUE can't actually wait.
    def _rtl_acquire_resource_exclusive(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        tid = state.tls_current_thread_id()
        number_active = memory.read32((ptr + 0x28) & 0xFFFFFFFF)
        owner = memory.read32((ptr + 0x2C) & 0xFFFFFFFF)
        if number_active != 0 and owner != tid:
            logger.debug("kernel32",
                f"[RtlAcquireResourceExclusive] 0x{ptr:08x} contested: "
                f"owner=0x{owner:08x} tid=0x{tid:08x} -- returning FALSE")
            cpu.regs[EAX] = 0  # FALSE
        else:
            memory.write32(ptr + 0x28, 0xFFFFFFFF)  # NumberOfActive = -1 (exclusive)
            memory.write32(ptr + 0x2C, tid)          # ExclusiveOwnerThread
            cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 8)

    def _rtl_release_resource(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        memory.write32(ptr + 0x28, 0)  # NumberOfActive = 0
        memory.write32(ptr + 0x2C, 0)  # ExclusiveOwnerThread = NULL
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "InitializeCriticalSection",             _init_cs)
    stubs.register_handler("ntdll.dll",    "RtlInitializeCriticalSection",         _init_cs)
    stubs.register_handler("ntdll.dll",    "RtlInitializeResource",                _rtl_init_resource)
    stubs.register_handler("ntdll.dll",    "RtlAcquireResourceExclusive",          _rtl_acquire_resource_exclusive)
    stubs.register_handler("ntdll.dll",    "RtlReleaseResource",                   _rtl_release_resource)
    stubs.register_handler("ntdll.dll",    "RtlInitializeCriticalSectionAndSpinCount", _rtl_init_cs_spin)
    stubs.register_handler("kernel32.dll", "InitializeCriticalSectionAndSpinCount", _init_cs_spin)
    stubs.register_guest_code("kernel32.dll", _CS_GUEST_CODE, cs_exports, cs_hooks)
    stubs.register_handler("kernel32.dll", "DeleteCriticalSection",                 _delete_cs)

    # ── TLS ───────────────────────────────────────────────────────────────────

    TLS_OUT_OF_INDEXES = 0xFFFFFFFF

    def _tls_alloc(cpu: "CPU") -> None:
        if state.next_tls_slot >= state.tls_max_slots:
            cpu.regs[EAX] = TLS_OUT_OF_INDEXES
            return
        slot = state.next_tls_slot
        state.next_tls_slot += 1
        state.scheduler.tls_alloc_slot(slot)
        cpu.regs[EAX] = slot

    def _tls_set_value(cpu: "CPU") -> None:
        idx = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        val = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if not state.scheduler.tls_slot_allocated(idx):
            # Win32: returns FALSE for an invalid index; never halts.
            logger.warn("handlers", f"[TlsSetValue] invalid slot {idx} — returning FALSE")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 8)
            return
        memory.write32(TEB_BASE + 0xE0 + idx * 4, val)
        state.tls_thread_store(state.tls_current_thread_id())[idx] = val
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _tls_get_value(cpu: "CPU") -> None:
        idx = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if not state.scheduler.tls_slot_allocated(idx):
            # Win32: returns 0 (NULL) for an invalid index; never halts.
            logger.warn("handlers", f"[TlsGetValue] invalid slot {idx} — returning 0")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 4)
            return
        tid = state.tls_current_thread_id()
        cpu.regs[EAX] = state.tls_thread_store(tid).get(idx, 0)
        cleanup_stdcall(cpu, memory, 4)

    def _tls_free(cpu: "CPU") -> None:
        idx = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if not state.scheduler.tls_slot_allocated(idx):
            # Win32: returns FALSE for an unallocated index; never halts.
            logger.warn("handlers", f"[TlsFree] invalid slot {idx} — returning FALSE")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 4)
            return
        state.scheduler.tls_free_slot(idx)
        for store in state.tls_store.values():
            store.pop(idx, None)
        memory.write32(TEB_BASE + 0xE0 + idx * 4, 0)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "TlsAlloc",    _tls_alloc)
    stubs.register_handler("kernel32.dll", "TlsSetValue", _tls_set_value)
    stubs.register_handler("kernel32.dll", "TlsGetValue", _tls_get_value)
    stubs.register_handler("kernel32.dll", "TlsFree",     _tls_free)
