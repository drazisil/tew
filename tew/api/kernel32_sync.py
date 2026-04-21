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
        lock_count = (memory.read32(ptr + 0x04) + 1) & 0xFFFFFFFF
        memory.write32(ptr + 0x04, lock_count)
        if lock_count == 0:
            # Acquired (LockCount was -1 → 0): first entry.
            memory.write32(ptr + 0x08, 1)
            memory.write32(ptr + 0x0C, tid)
        else:
            owner = memory.read32(ptr + 0x0C)
            if owner == tid:
                # Recursive entry by the same thread.
                memory.write32(ptr + 0x08, (memory.read32(ptr + 0x08) + 1) & 0xFFFFFFFF)
            else:
                logger.error("kernel32", f"[EnterCriticalSection] 0x{ptr:08x} contested: owner=0x{owner:08x} caller=0x{tid:08x} — blocking not implemented — halting")
                cpu.halted = True
                return
        cleanup_stdcall(cpu, memory, 4)

    def _leave_cs(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        rec = (memory.read32(ptr + 0x08) - 1) & 0xFFFFFFFF
        memory.write32(ptr + 0x08, rec)
        if rec == 0:
            memory.write32(ptr + 0x0C, 0)
            new_lock = (memory.read32(ptr + 0x04) - 1) & 0xFFFFFFFF
            memory.write32(ptr + 0x04, new_lock)
            if new_lock < 0x80000000:
                # LockCount >= 0 as signed means waiters exist; must signal LockSemaphore.
                logger.error("kernel32", f"[LeaveCriticalSection] 0x{ptr:08x} has waiters — LockSemaphore signal not implemented — halting")
                cpu.halted = True
                return
        cleanup_stdcall(cpu, memory, 4)

    def _delete_cs(cpu: "CPU") -> None:
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "InitializeCriticalSection",             _init_cs)
    stubs.register_handler("kernel32.dll", "InitializeCriticalSectionAndSpinCount", _init_cs_spin)
    stubs.register_handler("kernel32.dll", "EnterCriticalSection",                  _enter_cs)
    stubs.register_handler("kernel32.dll", "LeaveCriticalSection",                  _leave_cs)
    stubs.register_handler("kernel32.dll", "DeleteCriticalSection",                 _delete_cs)

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
