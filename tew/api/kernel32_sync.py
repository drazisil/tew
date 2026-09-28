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
from tew.api._state import TEB_BASE, CriticalSectionEntry
from tew.logger import logger


def register_kernel32_sync_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register critical section and TLS handlers."""

    # ── Critical sections ─────────────────────────────────────────────────────

    # 2026-09-14: critical-section state (LockCount/RecursionCount/
    # OwningThread) now lives in state.critical_sections, keyed by the guest
    # pointer, instead of being read/written through guest memory on every
    # call. Guest code only ever touches a CS through this documented Win32
    # API -- MSVC-compiled code treats CRITICAL_SECTION as opaque, nothing
    # reads its raw fields except our own handlers (exception_diagnostics.py
    # was the one exception and was switched to read this dict too) -- so
    # there's no correctness reason to keep it in guest memory at all.
    # Measured live first: batching the same reads/writes into fewer bulk
    # ctypes crossings (read_bytes/load instead of several read32/write32)
    # made no measurable difference (~27us/call either way) -- dropping the
    # crossings entirely, not just their count, is what actually helps.
    def _cs_entry(ptr: int) -> CriticalSectionEntry:
        entry = state.critical_sections.get(ptr)
        if entry is None:
            # Guest used the CS without calling Initialize -- real Windows
            # behavior is undefined here; degrade gracefully as free rather
            # than raising, matching this handler's past leniency.
            entry = state.critical_sections[ptr] = CriticalSectionEntry()
        return entry

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
        state.critical_sections[ptr] = CriticalSectionEntry()

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

    def _enter_cs(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        tid = state.tls_current_thread_id()
        cs = _cs_entry(ptr)
        if cs.owner_tid == tid:
            # Recursive entry — same thread, deepen RecursionCount only.
            cs.recursion_count += 1
        else:
            new_lock_count = (cs.lock_count + 1) & 0xFFFFFFFF
            if new_lock_count == 0:
                # Acquired (LockCount was -1 → 0): first entry.
                cs.lock_count = 0
                cs.recursion_count = 1
                cs.owner_tid = tid
            else:
                # CS is held by another thread — LockCount is left
                # untouched and we block.
                retry_eip = (cpu.eip - 2) & 0xFFFFFFFF
                logger.debug("kernel32",
                    f"[EnterCriticalSection] 0x{ptr:08x} contested: "
                    f"owner=0x{cs.owner_tid:08x} tid=0x{tid:08x} — blocking")
                state.scheduler.block_current_on_cs(cpu, memory, ptr, retry_eip)
                return  # no cleanup_stdcall: EIP set to retry_eip by scheduler
        cleanup_stdcall(cpu, memory, 4)

    def _leave_cs(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cs = _cs_entry(ptr)
        cs.recursion_count = (cs.recursion_count - 1) & 0xFFFFFFFF
        if cs.recursion_count == 0:
            # Full release: reset to free state and wake any blocked threads.
            cs.lock_count = 0xFFFFFFFF  # LockCount = -1 (free)
            cs.owner_tid = 0
            state.scheduler.unblock_cs(ptr)
        cleanup_stdcall(cpu, memory, 4)

    # XP's RtlDeleteCriticalSection: close LockSemaphore if one was created,
    # unlink DebugInfo from RtlCriticalSectionList, zero and free it, then
    # zero the whole 24-byte struct. A DebugInfo of 0 (already deleted, or
    # never initialized) skips the debug-block part, as on XP.
    def _delete_cs(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        semaphore = memory.read32((ptr + 0x10) & 0xFFFFFFFF)
        if semaphore != 0:
            # tew never creates a LockSemaphore yet, so a non-zero one here
            # isn't a handle tew can close.
            raise RuntimeError(
                f"DeleteCriticalSection(0x{ptr:08x}): LockSemaphore is "
                f"0x{semaphore:08x}, but tew never creates one -- not a "
                f"handle it can close")
        debug = memory.read32(ptr)
        if debug != 0:
            flink = memory.read32(debug + 0x08)
            blink = memory.read32(debug + 0x0C)
            memory.write32(blink, flink)       # Blink->Flink = Flink
            memory.write32(flink + 4, blink)   # Flink->Blink = Blink
            memory.load(debug, bytes(_CS_DEBUG_SIZE))
            state.simple_free(debug)
        memory.load(ptr, bytes(_CS_SIZE))
        state.critical_sections.pop(ptr, None)
        cleanup_stdcall(cpu, memory, 4)

    # TryEnterCriticalSection(LPCRITICAL_SECTION) -> BOOL
    # Acquires if free or already owned by this thread; returns FALSE without
    # blocking if held by another thread.
    def _try_enter_cs(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        tid = state.tls_current_thread_id()
        cs = _cs_entry(ptr)
        if cs.owner_tid == tid:
            # Recursive entry — owning thread deepens RecursionCount.
            cs.recursion_count += 1
            cpu.regs[EAX] = 1  # TRUE
        elif cs.lock_count == 0xFFFFFFFF:
            # CS is free (LockCount == -1): acquire it.
            cs.lock_count = 0
            cs.recursion_count = 1
            cs.owner_tid = tid
            cpu.regs[EAX] = 1  # TRUE
        else:
            # Held by another thread — return FALSE without blocking.
            cpu.regs[EAX] = 0  # FALSE
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
    stubs.register_handler("kernel32.dll", "EnterCriticalSection",                  _enter_cs)
    stubs.register_handler("kernel32.dll", "LeaveCriticalSection",                  _leave_cs)
    stubs.register_handler("kernel32.dll", "DeleteCriticalSection",                 _delete_cs)
    stubs.register_handler("kernel32.dll", "TryEnterCriticalSection",               _try_enter_cs)

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
