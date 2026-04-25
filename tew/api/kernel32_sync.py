"""kernel32.dll synchronization handlers — critical sections and TLS."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers
    from tew.api._state import CRTState

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import cleanup_stdcall
from tew.api._state import TEB_BASE
from tew.logger import logger


def register_kernel32_sync_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register critical section and TLS handlers."""

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
        ptr        = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
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
        owner = memory.read32((ptr + 0x0C) & 0xFFFFFFFF)
        if owner == tid:
            # Recursive entry — same thread, deepen RecursionCount only.
            memory.write32((ptr + 0x08) & 0xFFFFFFFF,
                           (memory.read32((ptr + 0x08) & 0xFFFFFFFF) + 1) & 0xFFFFFFFF)
        else:
            lock_count = (memory.read32((ptr + 0x04) & 0xFFFFFFFF) + 1) & 0xFFFFFFFF
            memory.write32((ptr + 0x04) & 0xFFFFFFFF, lock_count)
            if lock_count == 0:
                # Acquired (LockCount was -1 → 0): first entry.
                memory.write32((ptr + 0x08) & 0xFFFFFFFF, 1)
                memory.write32((ptr + 0x0C) & 0xFFFFFFFF, tid)
            else:
                # CS is held by another thread — undo increment and block.
                memory.write32((ptr + 0x04) & 0xFFFFFFFF, (lock_count - 1) & 0xFFFFFFFF)
                retry_eip = (cpu.eip - 2) & 0xFFFFFFFF
                logger.debug("kernel32",
                    f"[EnterCriticalSection] 0x{ptr:08x} contested: "
                    f"owner=0x{owner:08x} tid=0x{tid:08x} — blocking")
                state.scheduler.block_current_on_cs(cpu, memory, ptr, retry_eip)
                return  # no cleanup_stdcall: EIP set to retry_eip by scheduler
        cleanup_stdcall(cpu, memory, 4)

    def _leave_cs(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        rec = (memory.read32((ptr + 0x08) & 0xFFFFFFFF) - 1) & 0xFFFFFFFF
        memory.write32((ptr + 0x08) & 0xFFFFFFFF, rec)
        if rec == 0:
            # Full release: reset to free state and wake any blocked threads.
            memory.write32((ptr + 0x0C) & 0xFFFFFFFF, 0x00000000)  # OwningThread = 0
            memory.write32((ptr + 0x04) & 0xFFFFFFFF, 0xFFFFFFFF)  # LockCount = -1 (free)
            state.scheduler.unblock_cs(ptr)
        cleanup_stdcall(cpu, memory, 4)

    def _delete_cs(cpu: "CPU") -> None:
        cleanup_stdcall(cpu, memory, 4)

    # TryEnterCriticalSection(LPCRITICAL_SECTION) -> BOOL
    # Acquires if free or already owned by this thread; returns FALSE without
    # blocking if held by another thread.
    def _try_enter_cs(cpu: "CPU") -> None:
        ptr   = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        tid   = state.tls_current_thread_id()
        owner = memory.read32((ptr + 0x0C) & 0xFFFFFFFF)
        if owner == tid:
            # Recursive entry — owning thread deepens RecursionCount.
            rec = (memory.read32((ptr + 0x08) & 0xFFFFFFFF) + 1) & 0xFFFFFFFF
            memory.write32((ptr + 0x08) & 0xFFFFFFFF, rec)
            cpu.regs[EAX] = 1  # TRUE
        elif memory.read32((ptr + 0x04) & 0xFFFFFFFF) == 0xFFFFFFFF:
            # CS is free (LockCount == -1): acquire it.
            memory.write32((ptr + 0x04) & 0xFFFFFFFF, 0)    # LockCount = 0
            memory.write32((ptr + 0x08) & 0xFFFFFFFF, 1)    # RecursionCount = 1
            memory.write32((ptr + 0x0C) & 0xFFFFFFFF, tid)  # OwningThread = tid
            cpu.regs[EAX] = 1  # TRUE
        else:
            # Held by another thread — return FALSE without blocking.
            cpu.regs[EAX] = 0  # FALSE
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "InitializeCriticalSection",             _init_cs)
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
        memory.write32(TEB_BASE + 0xE0 + idx * 4, val)
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
        cpu.regs[EAX] = memory.read32(TEB_BASE + 0xE0 + idx * 4)
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
        memory.write32(TEB_BASE + 0xE0 + idx * 4, 0)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "TlsAlloc",    _tls_alloc)
    stubs.register_handler("kernel32.dll", "TlsSetValue", _tls_set_value)
    stubs.register_handler("kernel32.dll", "TlsGetValue", _tls_get_value)
    stubs.register_handler("kernel32.dll", "TlsFree",     _tls_free)
