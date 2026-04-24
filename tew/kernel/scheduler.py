"""Cooperative thread scheduler.

The CPU is just a CPU — cpu.halted means the CPU stopped.
Thread context switches happen inside stub handlers: save current thread
registers, load next thread registers, return to Zig, which continues
on the new thread's EIP with no knowledge a switch occurred.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu import EAX, EBX, ECX, EDX, ESI, EDI, ESP, EBP
from tew.logger import logger

# ── Constants (mirror tew/api/_state.py) ─────────────────────────────────────
TEB_BASE          = 0x00320000
_TLS_TEB_OFFSET   = 0xE0          # TLS slots start at TEB + 0xE0
THREAD_STACK_BASE = 0x08000000
THREAD_STACK_SIZE = 256 * 1024    # 256 KB per thread
THREAD_SENTINEL   = 0x001FE000   # return address that marks thread exit


class ThreadStatus(enum.Enum):
    READY           = "ready"
    BLOCKED_CS      = "blocked_cs"
    BLOCKED_HANDLES = "blocked_handles"
    SLEEPING        = "sleeping"
    DEAD            = "dead"


@dataclass
class ThreadState:
    thread_id:    int
    handle:       int
    start_address: int
    parameter:    int
    status:       ThreadStatus = ThreadStatus.READY
    suspended:    bool = False
    saved_state:  Any  = None        # opaque SavedCPUState from cpu.save_state()
    tls_slots:    dict = field(default_factory=dict)   # slot_index -> value
    calls_seen:   Optional[set] = None

    # Blocking conditions
    waiting_on_cs:      Optional[int]       = None
    waiting_on_handles: Optional[frozenset] = None
    wait_deadline_ms:   Optional[int]       = None
    wait_timed_out:     bool                = False
    sleep_until_ms:     int                 = 0

    @property
    def completed(self) -> bool:
        return self.status == ThreadStatus.DEAD

    @completed.setter
    def completed(self, val: bool) -> None:
        if val:
            self.status = ThreadStatus.DEAD


class Scheduler:
    """Cooperative thread scheduler.

    Thread management is entirely here. The CPU registers are the source of
    truth for the currently running thread; all other threads have their
    full state in ThreadState.saved_state.

    Context switching:
      1. Caller sets cpu.eip to desired resume point (stub addr or return addr).
      2. Caller marks the current thread's status.
      3. Caller calls block_current_* / sleep_current / mark_current_dead.
      4. Those methods save current state and load the next thread.
      5. On return from the stub handler, Zig runs from the new cpu.eip.
    """

    def __init__(self, tls_slots: set, thread_stack_next: int = THREAD_STACK_BASE) -> None:
        self.threads: list[ThreadState] = []
        self.current_idx: int = -1
        self.virtual_ticks_ms: int = 0
        self._tls_slots = tls_slots           # shared reference — sees TlsAlloc additions
        self._thread_stack_next = thread_stack_next
        self._last_scheduled_idx: int = 0
        self._kernel: Optional[object] = None  # set to Kernel by CRTState after construction

    # ── Thread registration ───────────────────────────────────────────────────

    def create_main_thread(self, thread_id: int, handle: int) -> ThreadState:
        """Register the already-running main thread at index 0.

        Must be called before any background threads are created.
        The main thread has no saved_state — its state lives in the CPU.
        """
        assert len(self.threads) == 0, "create_main_thread must be called first"
        t = ThreadState(
            thread_id=thread_id,
            handle=handle,
            start_address=0,  # already running; not used for stack init
            parameter=0,
        )
        self.threads.append(t)
        self.current_idx = 0
        self._last_scheduled_idx = 0
        return t

    def create_thread(self, thread_id: int, handle: int, start_address: int,
                      parameter: int, suspended: bool = False) -> ThreadState:
        """Register a new background thread. Returns the new ThreadState."""
        t = ThreadState(
            thread_id=thread_id,
            handle=handle,
            start_address=start_address,
            parameter=parameter,
            status=ThreadStatus.READY,
            suspended=suspended,
        )
        self.threads.append(t)
        return t

    # ── Internal: TLS ────────────────────────────────────────────────────────

    def _tls_base(self) -> int:
        return TEB_BASE + _TLS_TEB_OFFSET

    def _save_tls(self, memory: "Memory", thread: ThreadState) -> None:
        base = self._tls_base()
        for slot in self._tls_slots:
            thread.tls_slots[slot] = memory.read32((base + slot * 4) & 0xFFFFFFFF)

    def _load_tls(self, memory: "Memory", thread: ThreadState) -> None:
        base = self._tls_base()
        for slot in self._tls_slots:
            memory.write32((base + slot * 4) & 0xFFFFFFFF, thread.tls_slots.get(slot, 0))

    # ── Internal: CPU state ───────────────────────────────────────────────────

    def _save_current(self, cpu: "CPU", memory: "Memory") -> None:
        """Snapshot current CPU state (including EIP) to the current thread."""
        thread = self.threads[self.current_idx]
        thread.saved_state = cpu.save_state()
        self._save_tls(memory, thread)

    def _load_thread(self, idx: int, cpu: "CPU", memory: "Memory") -> None:
        """Restore a thread's saved state into the CPU. Sets current_idx."""
        thread = self.threads[idx]
        self._load_tls(memory, thread)
        cpu.restore_state(thread.saved_state)
        cpu.halted = False   # restore_state does not touch halted; clear explicitly
        self.current_idx = idx

    def _init_thread_stack(self, cpu: "CPU", memory: "Memory",
                            thread: ThreadState) -> None:
        """Set up the initial stack for a thread running for the first time."""
        stack_top = self._thread_stack_next + THREAD_STACK_SIZE - 16
        self._thread_stack_next += THREAD_STACK_SIZE
        esp = stack_top - 4
        memory.write32(esp & 0xFFFFFFFF, thread.parameter)
        esp -= 4
        memory.write32(esp & 0xFFFFFFFF, THREAD_SENTINEL)
        cpu.regs[EAX] = 0
        cpu.regs[EBX] = 0
        cpu.regs[ECX] = 0
        cpu.regs[EDX] = 0
        cpu.regs[ESI] = 0
        cpu.regs[EDI] = 0
        cpu.regs[ESP] = esp & 0xFFFFFFFF
        cpu.regs[EBP] = 0
        cpu.eip    = thread.start_address
        cpu.eflags = 0x202

    def _load_next(self, idx: int, cpu: "CPU", memory: "Memory") -> None:
        """Load thread idx into the CPU. Current thread must already be saved."""
        target = self.threads[idx]
        self._last_scheduled_idx = idx
        if target.saved_state is None:
            self._init_thread_stack(cpu, memory, target)
            self.current_idx = idx
            cpu.halted = False
        else:
            self._load_thread(idx, cpu, memory)

    # ── Internal: scheduling ──────────────────────────────────────────────────

    def _pick_next_ready(self, memory: "Memory") -> Optional[int]:
        """Round-robin scan for the next runnable thread, excluding current."""
        n = len(self.threads)
        if n == 0:
            return None
        start = (self._last_scheduled_idx + 1) % n
        for i in range(n):
            idx = (start + i) % n
            if idx == self.current_idx:
                continue
            t = self.threads[idx]
            if t.suspended:
                continue
            if t.status == ThreadStatus.DEAD:
                continue
            if t.status == ThreadStatus.SLEEPING:
                if self.virtual_ticks_ms < t.sleep_until_ms:
                    continue
                t.status = ThreadStatus.READY
            if t.status == ThreadStatus.BLOCKED_CS:
                if t.waiting_on_cs is not None:
                    owner = memory.read32((t.waiting_on_cs + 0x0C) & 0xFFFFFFFF)
                    if owner != 0:
                        continue
                t.waiting_on_cs = None
                t.status = ThreadStatus.READY
            if t.status == ThreadStatus.BLOCKED_HANDLES:
                if (t.wait_deadline_ms is not None
                        and self.virtual_ticks_ms >= t.wait_deadline_ms):
                    t.wait_timed_out = True
                    t.wait_deadline_ms = None
                    t.waiting_on_handles = None
                    t.status = ThreadStatus.READY
                else:
                    continue
            if t.status == ThreadStatus.READY:
                return idx

        # Fallback: no READY thread found.
        # Pass 1: wake the SLEEPING thread with the earliest deadline so a
        #   blocking background thread doesn't starve a sleeping main thread.
        # Pass 2: if no sleeping thread, wake a BLOCKED_HANDLES thread so it
        #   can retry its wait — handles may be signaled by the heartbeat between
        #   batches; cpu.halted is never used for this case.
        earliest_sleep_idx: Optional[int] = None
        earliest_sleep_ms: Optional[int] = None
        blocked_fallback_idx: Optional[int] = None
        for i in range(n):
            idx = (start + i) % n
            if idx == self.current_idx:
                continue
            t = self.threads[idx]
            if t.suspended or t.status == ThreadStatus.DEAD:
                continue
            if t.status == ThreadStatus.SLEEPING:
                if earliest_sleep_ms is None or t.sleep_until_ms < earliest_sleep_ms:
                    earliest_sleep_ms = t.sleep_until_ms
                    earliest_sleep_idx = idx
            elif t.status == ThreadStatus.BLOCKED_HANDLES and blocked_fallback_idx is None:
                blocked_fallback_idx = idx
        if earliest_sleep_idx is not None:
            self.threads[earliest_sleep_idx].status = ThreadStatus.READY
            return earliest_sleep_idx
        if blocked_fallback_idx is not None:
            self.threads[blocked_fallback_idx].status = ThreadStatus.READY
            return blocked_fallback_idx

        # Kernel tick: poll pending I/O completions.  If any socket is ready,
        # kernel.tick() signals event handles and calls unblock_handle(), which
        # sets BLOCKED_HANDLES threads to READY.  Re-scan to pick one up.
        if self._kernel is not None:
            self._kernel.tick()
            for i in range(n):
                idx = (start + i) % n
                if idx == self.current_idx:
                    continue
                t = self.threads[idx]
                if not t.suspended and t.status == ThreadStatus.READY:
                    return idx

        return None

    # ── Public: context switch ────────────────────────────────────────────────

    def switch_to(self, cpu: "CPU", memory: "Memory", idx: int) -> None:
        """Save current thread and load thread at idx. For external callers."""
        if 0 <= self.current_idx < len(self.threads):
            if self.threads[self.current_idx].status != ThreadStatus.DEAD:
                self._save_current(cpu, memory)
        self._load_next(idx, cpu, memory)

    def preempt_slice(self, cpu: "CPU", memory: "Memory") -> bool:
        """Round-robin preemption: yield the current slice to the next READY thread.

        Called after each cpu.run(batch) so that a thread which never calls a
        blocking Win32 stub (e.g. a timer dispatch loop that re-signals its own
        wait event) cannot starve other threads indefinitely.

        Returns True if a context switch occurred.
        """
        current = self.threads[self.current_idx]
        if current.status != ThreadStatus.READY:
            return False  # Thread blocked mid-batch; switch already happened.
        n = len(self.threads)
        for i in range(1, n):
            idx = (self.current_idx + i) % n
            t = self.threads[idx]
            if not t.suspended and t.status == ThreadStatus.READY:
                self.switch_to(cpu, memory, idx)
                return True
        return False

    # ── Public: blocking operations ───────────────────────────────────────────

    def block_current_on_cs(self, cpu: "CPU", memory: "Memory",
                              cs_ptr: int, retry_eip: int) -> None:
        """Suspend current thread waiting on a contested critical section.

        retry_eip should be the address of the INT 0xFE stub (cpu.eip - 2)
        so the EnterCriticalSection call is retried when this thread resumes.
        """
        thread = self.threads[self.current_idx]
        thread.waiting_on_cs = cs_ptr
        thread.status = ThreadStatus.BLOCKED_CS
        cpu.eip = retry_eip
        self._save_current(cpu, memory)

        next_idx = self._pick_next_ready(memory)
        if next_idx is None:
            # No other thread is runnable. Reload the current thread so it
            # retries the CS wait from retry_eip; the heartbeat will advance
            # virtual time and may unblock threads between batches.
            thread.status = ThreadStatus.READY
            self._load_next(self.current_idx, cpu, memory)
            return
        self._load_next(next_idx, cpu, memory)

    def block_current_on_handles(self, cpu: "CPU", memory: "Memory",
                                   handles: frozenset, retry_eip: int,
                                   deadline_ms: Optional[int] = None) -> None:
        """Suspend current thread waiting on kernel handles (event/mutex/etc.).

        retry_eip should be the stub address (cpu.eip - 2) so the Wait call
        is retried when this thread is unblocked.
        """
        thread = self.threads[self.current_idx]
        thread.waiting_on_handles = handles
        thread.wait_deadline_ms = deadline_ms
        thread.wait_timed_out = False
        thread.status = ThreadStatus.BLOCKED_HANDLES
        cpu.eip = retry_eip
        self._save_current(cpu, memory)

        next_idx = self._pick_next_ready(memory)
        if next_idx is None:
            # No other thread is runnable. Reload the current thread so it
            # retries the wait from retry_eip; the heartbeat will advance
            # virtual time and may signal handles between batches.
            thread.status = ThreadStatus.READY
            thread.waiting_on_handles = None
            thread.wait_deadline_ms = None
            self._load_next(self.current_idx, cpu, memory)
            return
        self._load_next(next_idx, cpu, memory)

    def sleep_current(self, cpu: "CPU", memory: "Memory",
                       return_eip: int, eax_val: int, sleep_ms: int) -> None:
        """Suspend current thread for sleep_ms virtual milliseconds.

        return_eip is the caller's return address (past the Sleep stub) so the
        thread resumes as if Sleep just returned.  eax_val is Sleep's return value.
        """
        thread = self.threads[self.current_idx]
        cpu.eip = return_eip
        cpu.regs[EAX] = eax_val
        thread.sleep_until_ms = self.virtual_ticks_ms + sleep_ms
        thread.status = ThreadStatus.SLEEPING
        self._save_current(cpu, memory)

        next_idx = self._pick_next_ready(memory)
        if next_idx is None:
            # No other thread ready — wake immediately and stay on this thread.
            thread.status = ThreadStatus.READY
            cpu.restore_state(thread.saved_state)
            cpu.halted = False
            return
        self._load_next(next_idx, cpu, memory)

    def mark_current_dead(self, cpu: "CPU", memory: "Memory") -> None:
        """Mark current thread as dead and switch to the next ready thread.

        If no threads remain, sets cpu.halted = True (process exit).
        """
        thread = self.threads[self.current_idx]
        thread.status = ThreadStatus.DEAD
        thread.saved_state = None
        logger.debug("scheduler",
            f"Thread tid={thread.thread_id} (handle=0x{thread.handle:x}) exited")

        next_idx = self._pick_next_ready(memory)
        if next_idx is None:
            logger.info("scheduler", "No runnable threads remain — halting CPU")
            cpu.halted = True
            return
        self._load_next(next_idx, cpu, memory)

    # ── Public: unblocking ────────────────────────────────────────────────────

    def unblock_cs(self, cs_ptr: int) -> None:
        """Mark all threads blocked on cs_ptr as READY when the CS is released."""
        for t in self.threads:
            if t.status == ThreadStatus.BLOCKED_CS and t.waiting_on_cs == cs_ptr:
                t.waiting_on_cs = None
                t.status = ThreadStatus.READY
                logger.debug("scheduler",
                    f"unblock_cs: tid={t.thread_id} ready "
                    f"(CS 0x{cs_ptr:08x} released)")

    def unblock_handle(self, handle: int) -> None:
        """Mark threads blocked on handle as READY when the object is signaled.

        Does NOT consume the signal — the thread will do that when it retries
        the Wait stub.
        """
        for t in self.threads:
            if (t.status == ThreadStatus.BLOCKED_HANDLES
                    and t.waiting_on_handles is not None
                    and handle in t.waiting_on_handles):
                t.waiting_on_handles = None
                t.status = ThreadStatus.READY
                logger.debug("scheduler",
                    f"unblock_handle: tid={t.thread_id} ready "
                    f"(handle 0x{handle:x} signaled)")

    # ── Public: clock ─────────────────────────────────────────────────────────

    def tick(self, ms: int, memory: "Memory") -> None:
        """Advance the virtual clock and wake any sleeping or deadline-expired threads."""
        self.virtual_ticks_ms = (self.virtual_ticks_ms + ms) & 0xFFFFFFFF
        for t in self.threads:
            if t.status == ThreadStatus.SLEEPING:
                if self.virtual_ticks_ms >= t.sleep_until_ms:
                    t.status = ThreadStatus.READY
                    logger.debug("scheduler",
                        f"tick: tid={t.thread_id} woke from sleep "
                        f"(vtime={self.virtual_ticks_ms}ms)")
            elif t.status == ThreadStatus.BLOCKED_HANDLES:
                if (t.wait_deadline_ms is not None
                        and self.virtual_ticks_ms >= t.wait_deadline_ms):
                    t.wait_timed_out = True
                    t.wait_deadline_ms = None
                    t.waiting_on_handles = None
                    t.status = ThreadStatus.READY
                    logger.debug("scheduler",
                        f"tick: tid={t.thread_id} wait deadline expired")

    # ── Public: queries ───────────────────────────────────────────────────────

    def current_thread(self) -> ThreadState:
        if not (0 <= self.current_idx < len(self.threads)):
            raise RuntimeError(
                f"No current thread (current_idx={self.current_idx}, "
                f"n={len(self.threads)})")
        return self.threads[self.current_idx]

    def any_runnable(self) -> bool:
        """True if any thread is ready to run (not dead, sleeping, blocked, or suspended)."""
        return any(
            t.status == ThreadStatus.READY and not t.suspended
            for t in self.threads
        )

    def get_thread_tls(self, thread_id: int) -> dict:
        """Return the TLS slot dict for thread_id, or a fresh dict if not found."""
        for t in self.threads:
            if t.thread_id == thread_id:
                return t.tls_slots
        return {}
