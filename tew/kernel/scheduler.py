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
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu_zig import EAX, EBX, ECX, EDX, ESI, EDI, ESP, EBP
from tew.logger import logger

# ── Constants (mirror tew/api/_state.py) ─────────────────────────────────────
TEB_BASE          = 0x00320000
_TLS_TEB_OFFSET   = 0xE0          # TLS slots start at TEB + 0xE0
_LAST_ERROR_TEB_OFFSET = 0x34     # NT_TIB.LastErrorValue (mirrors tew/api/kernel32_system.py)
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
    last_error:   int  = 0           # this thread's GetLastError/SetLastError value
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
        # Reentrancy guard (see _swap_current) — depth, not a flag, so a
        # nested call made from inside another nested call is still safe.
        self.reentrant_depth: int = 0
        self.reentrancy_violations: list[str] = []

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

    # ── Internal: last-error (GetLastError/SetLastError) ─────────────────────
    # Real Windows keeps this per-thread (each thread has its own TEB), but
    # this emulator has only one fixed TEB address shared by every thread —
    # without this save/restore, one thread's SetLastError would silently
    # clobber every other thread's GetLastError result across a context
    # switch. Same shape as _save_tls/_load_tls above, one DWORD instead of
    # a slot table.

    def _save_last_error(self, memory: "Memory", thread: ThreadState) -> None:
        thread.last_error = memory.read32((TEB_BASE + _LAST_ERROR_TEB_OFFSET) & 0xFFFFFFFF)

    def _load_last_error(self, memory: "Memory", thread: ThreadState) -> None:
        memory.write32((TEB_BASE + _LAST_ERROR_TEB_OFFSET) & 0xFFFFFFFF, thread.last_error)

    # ── Internal: CPU state ───────────────────────────────────────────────────

    def _save_current(self, cpu: "CPU", memory: "Memory") -> None:
        """Snapshot current CPU state (including EIP) to the current thread."""
        thread = self.threads[self.current_idx]
        thread.saved_state = cpu.save_state()
        self._save_tls(memory, thread)
        self._save_last_error(memory, thread)

    def _load_thread(self, idx: int, cpu: "CPU", memory: "Memory") -> None:
        """Restore a thread's saved state into the CPU. Sets current_idx."""
        thread = self.threads[idx]
        self._load_tls(memory, thread)
        self._load_last_error(memory, thread)
        cpu.restore_state(thread.saved_state)
        # restore_state does not touch halted; clear explicitly -- but never
        # override a fatal_halt (e.g. an unimplemented Win32 API): that means
        # the whole emulator must stop, not just this one thread's slice.
        if not cpu.fatal_halt:
            cpu.halted = False
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
        self._load_last_error(memory, thread)  # fresh thread starts at last_error=0
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
        from_idx = self.current_idx
        from_tid = self.threads[from_idx].thread_id if 0 <= from_idx < len(self.threads) else None
        logger.debug("scheduler",
            f"switch: idx={from_idx} (tid={from_tid}) -> idx={idx} (tid={target.thread_id})")
        self._last_scheduled_idx = idx
        if target.saved_state is None:
            self._init_thread_stack(cpu, memory, target)
            self.current_idx = idx
            if not cpu.fatal_halt:
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

    # ── Reentrancy guard ───────────────────────────────────────────────────────
    # tew's CPU has exactly one register file (cpu.regs), shared by every
    # thread; a thread's "state" only exists as a ThreadState.saved_state
    # snapshot while it isn't the one running. A nested synchronous call
    # (e.g. _invoke_emulated_proc, used to invoke a DllMain from inside a
    # stub handler) runs cpu.run() again while the *outer* call is still on
    # the Python stack, still expecting cpu.regs to belong to the thread
    # that entered it. If a stub handler reached during that nested run
    # triggers a scheduler swap, it silently hands the shared registers to
    # a different thread's state -- the outer call resumes into someone
    # else's registers with no error, no exception, nothing until state
    # visibly stops making sense many steps later. enter/exit_reentrant_call
    # bracket every nested cpu.run(); _swap_current is the single chokepoint
    # every swap-capable public method routes through, so it's the one place
    # that has to know about this.

    def enter_reentrant_call(self) -> None:
        """Mark that a nested synchronous cpu.run() is starting.

        Call before invoking cpu.run() from inside a stub handler (i.e. any
        call that is not the main step loop). Depth-counted, not a flag, so
        a nested call made from inside another nested call stays safe.
        """
        self.reentrant_depth += 1

    def exit_reentrant_call(self) -> None:
        """Mark that a nested synchronous cpu.run() has returned.

        Must be paired 1:1 with enter_reentrant_call, normally via
        try/finally around the nested cpu.run().
        """
        assert self.reentrant_depth > 0, (
            "exit_reentrant_call called with no matching enter_reentrant_call "
            "(reentrant_depth was already 0)")
        self.reentrant_depth -= 1

    def _reentrancy_check(self, operation: str) -> bool:
        """True if `operation` may proceed; False if a nested synchronous call
        is in progress and swapping the shared CPU registers away from it
        would corrupt that call's mid-flight state.

        The sole place reentrant_depth is consulted. On refusal: logs (this
        IS the failure signal -- callers never raise across the stub-handler
        boundary for this) and records the violation in
        reentrancy_violations for tests/diagnostics, but never mutates
        scheduler or thread state.
        """
        if self.reentrant_depth <= 0:
            return True
        msg = (f"reentrancy violation: {operation} refused "
               f"(reentrant_depth={self.reentrant_depth}, "
               f"current_idx={self.current_idx})")
        logger.error("scheduler", msg)
        self.reentrancy_violations.append(msg)
        return False

    def _swap_current(self, cpu: "CPU", memory: "Memory", target_idx: int,
                       operation: str) -> bool:
        """Single chokepoint for handing the shared CPU registers to a
        different thread: save the outgoing thread's state (unless it just
        died) and load target_idx's. Every public method that can move
        control to a different thread routes through here -- this is the
        scheduler's one API border to everything else for that operation.

        mark_current_dead/terminate_thread deliberately do NOT route through
        here: a thread dying mid-nested-call is a separate, already-tested,
        already-correct case (see mark_current_dead's docstring) -- the
        nested call's own thread-death detection depends on that swap still
        happening even while reentrant_depth > 0.

        Returns True if the swap happened, False if refused by the
        reentrancy guard (no state mutated).
        """
        if not self._reentrancy_check(operation):
            return False
        if 0 <= self.current_idx < len(self.threads):
            if self.threads[self.current_idx].status != ThreadStatus.DEAD:
                self._save_current(cpu, memory)
        self._load_next(target_idx, cpu, memory)
        return True

    # ── Public: context switch ────────────────────────────────────────────────

    def switch_to(self, cpu: "CPU", memory: "Memory", idx: int) -> bool:
        """Save current thread and load thread at idx. For external callers.

        Returns True if the swap happened, False if refused by the
        reentrancy guard.
        """
        return self._swap_current(cpu, memory, idx, "switch_to")

    def preempt_slice(self, cpu: "CPU", memory: "Memory") -> bool:
        """Round-robin preemption: yield the current slice to the next READY thread.

        Called after each cpu.run(batch) so that a thread which never calls a
        blocking Win32 stub (e.g. a timer dispatch loop that re-signals its own
        wait event) cannot starve other threads indefinitely.

        Returns True if a context switch occurred.
        """
        if cpu.fatal_halt:
            return False  # single core, fatally locked up -- nothing to hand it to
        current = self.threads[self.current_idx]
        if current.status != ThreadStatus.READY:
            return False  # Thread blocked mid-batch; switch already happened.
        n = len(self.threads)
        for i in range(1, n):
            idx = (self.current_idx + i) % n
            t = self.threads[idx]
            if not t.suspended and t.status == ThreadStatus.READY:
                return self.switch_to(cpu, memory, idx)
        return False

    # ── Public: blocking operations ───────────────────────────────────────────

    def block_current_on_cs(self, cpu: "CPU", memory: "Memory",
                              cs_ptr: int, retry_eip: int) -> None:
        """Suspend current thread waiting on a contested critical section.

        retry_eip should be the address of the INT 0xFE stub (cpu.eip - 2)
        so the EnterCriticalSection call is retried when this thread resumes.
        """
        if cpu.fatal_halt:
            return  # single core, fatally locked up -- nothing left to block/resume
        if not self._reentrancy_check("block_current_on_cs"):
            # Can't actually give up the CPU inside a nested synchronous call
            # (see enter_reentrant_call's docstring) -- but the caller (e.g.
            # _enter_cs) already deliberately skipped its own cleanup_stdcall
            # on this path, trusting *us* to redirect eip. Leaving eip
            # untouched here would resume execution with a stack the caller
            # never actually returned from -- live-verified this corrupts
            # ESP/EBP and trips __chkesp a few instructions later. Retry the
            # acquisition immediately instead; harmless busy-poll bounded by
            # the nested call's own step budget, not a real blocking wait.
            cpu.eip = retry_eip
            return
        thread = self.threads[self.current_idx]
        thread.waiting_on_cs = cs_ptr
        thread.status = ThreadStatus.BLOCKED_CS
        cpu.eip = retry_eip
        logger.debug("scheduler",
            f"block_current_on_cs: idx={self.current_idx} tid={thread.thread_id} "
            f"blocking on CS 0x{cs_ptr:08x}, retry_eip=0x{retry_eip:08x}")

        next_idx = self._pick_next_ready(memory)
        if next_idx is None:
            # No other thread is runnable. Reload the current thread so it
            # retries the CS wait from retry_eip; the heartbeat will advance
            # virtual time and may unblock threads between batches.
            thread.status = ThreadStatus.READY
            self._swap_current(cpu, memory, self.current_idx, "block_current_on_cs")
            return
        self._swap_current(cpu, memory, next_idx, "block_current_on_cs")

    def block_current_on_handles(self, cpu: "CPU", memory: "Memory",
                                   handles: frozenset, retry_eip: int,
                                   deadline_ms: Optional[int] = None) -> None:
        """Suspend current thread waiting on kernel handles (event/mutex/etc.).

        retry_eip should be the stub address (cpu.eip - 2) so the Wait call
        is retried when this thread is unblocked.
        """
        if cpu.fatal_halt:
            return  # single core, fatally locked up -- nothing left to block/resume
        if not self._reentrancy_check("block_current_on_handles"):
            # See block_current_on_cs's matching comment: the caller (e.g.
            # _wait_for_single) skipped its own cleanup_stdcall on this path
            # and trusts us to redirect eip -- retry immediately rather than
            # resuming with a stack the caller never returned from.
            cpu.eip = retry_eip
            return
        thread = self.threads[self.current_idx]
        thread.waiting_on_handles = handles
        thread.wait_deadline_ms = deadline_ms
        thread.wait_timed_out = False
        thread.status = ThreadStatus.BLOCKED_HANDLES
        cpu.eip = retry_eip
        logger.debug("scheduler",
            f"block_current_on_handles: idx={self.current_idx} tid={thread.thread_id} "
            f"blocking on handles={sorted(hex(h) for h in handles)}, "
            f"deadline={deadline_ms}, retry_eip=0x{retry_eip:08x}")

        next_idx = self._pick_next_ready(memory)
        if next_idx is None:
            # No other thread is runnable. Reload the current thread so it
            # retries the wait from retry_eip; the heartbeat will advance
            # virtual time and may signal handles between batches.
            thread.status = ThreadStatus.READY
            thread.waiting_on_handles = None
            thread.wait_deadline_ms = None
            self._swap_current(cpu, memory, self.current_idx, "block_current_on_handles")
            return
        self._swap_current(cpu, memory, next_idx, "block_current_on_handles")

    def sleep_current(self, cpu: "CPU", memory: "Memory",
                       return_eip: int, eax_val: int, sleep_ms: int) -> None:
        """Suspend current thread for sleep_ms virtual milliseconds.

        return_eip is the caller's return address (past the Sleep stub) so the
        thread resumes as if Sleep just returned.  eax_val is Sleep's return value.
        """
        if cpu.fatal_halt:
            return  # single core, fatally locked up -- nothing left to sleep/resume
        if not self._reentrancy_check("sleep_current"):
            # Can't actually give up the CPU inside a nested synchronous call
            # -- but callers use return_eip two different ways (Sleep/SleepEx
            # already popped their own stack and expect a genuine "past the
            # call" resume address; GetMessageA's 1ms poll-retry passes its
            # own dispatch address and never popped anything, expecting a
            # retry). Either way the caller needs eip (and, for the Sleep
            # case, EAX) set before returning -- leaving it untouched
            # resumes execution against a stack/EAX the caller doesn't
            # expect. Live-verified this corrupts ESP/EBP and trips
            # __chkesp a few instructions later. Complete the call as if the
            # sleep/retry happened instantly instead.
            cpu.eip = return_eip
            cpu.regs[EAX] = eax_val
            return
        thread = self.threads[self.current_idx]
        cpu.eip = return_eip
        cpu.regs[EAX] = eax_val
        thread.sleep_until_ms = self.virtual_ticks_ms + sleep_ms
        thread.status = ThreadStatus.SLEEPING
        logger.debug("scheduler",
            f"sleep_current: idx={self.current_idx} tid={thread.thread_id} "
            f"sleeping {sleep_ms}ms (until vtime={thread.sleep_until_ms}ms)")

        next_idx = self._pick_next_ready(memory)
        if next_idx is None:
            # No other thread ready — wake immediately and stay on this thread.
            # Routed through _swap_current (target == current_idx) rather than
            # a bespoke save/restore pair: since saved_state is non-None after
            # that save, _load_next's "resume a real thread" branch performs
            # the exact same cpu.restore_state() + halted-clear this used to
            # do by hand, just via the shared chokepoint.
            thread.status = ThreadStatus.READY
            self._swap_current(cpu, memory, self.current_idx, "sleep_current")
            return
        self._swap_current(cpu, memory, next_idx, "sleep_current")

    def mark_current_dead(self, cpu: "CPU", memory: "Memory") -> None:
        """Mark current thread as dead and switch to the next ready thread.

        If no threads remain, sets cpu.halted = True (process exit).

        Deliberately does NOT route through _swap_current / the reentrancy
        guard -- unlike the other swap-triggering methods, a thread dying
        (even mid-nested-call, reentrant_depth > 0) must still be able to
        hand the CPU to the next thread; that's the mechanism
        _invoke_emulated_proc's own thread-death detection depends on.
        Calls _load_next directly.
        """
        if cpu.fatal_halt:
            return  # single core, fatally locked up -- no thread state to update
        thread = self.threads[self.current_idx]
        thread.status = ThreadStatus.DEAD
        thread.saved_state = None
        logger.debug("scheduler",
            f"Thread tid={thread.thread_id} (handle=0x{thread.handle:x}) exited")

        next_idx = self._pick_next_ready(memory)
        if next_idx is None:
            # NOT made fatal: this fires whenever the last schedulable thread
            # dies, which includes a single thread dying mid-nested-call
            # (e.g. ExitThread from inside _invoke_emulated_proc) -- a
            # legitimate, recoverable case callers are specifically built to
            # detect and handle (see test_invoke_emulated_proc_thread_death.py,
            # which asserts cpu.fatal_halt is False for exactly this
            # scenario), not just a genuine whole-process exit.
            logger.info("scheduler", "No runnable threads remain — halting CPU")
            cpu.halted = True
            return
        self._load_next(next_idx, cpu, memory)

    def terminate_thread(self, cpu: "CPU", memory: "Memory", handle: int) -> Optional[bool]:
        """Forcibly terminate a thread by handle (TerminateThread).

        Returns None if handle doesn't match any known thread. Returns True
        if a *different* thread was terminated -- the caller (still running)
        should do its own normal stdcall cleanup. Returns False if the
        *current* thread terminated itself -- mark_current_dead already
        switched the live CPU to a different thread's context (or halted if
        none remained), so the caller must NOT touch cpu/EAX/ESP afterward,
        exactly like ExitThread's own handler.
        """
        for idx, t in enumerate(self.threads):
            if t.handle == handle:
                if idx == self.current_idx:
                    self.mark_current_dead(cpu, memory)
                    return False
                t.status = ThreadStatus.DEAD
                t.saved_state = None
                logger.debug("scheduler",
                    f"TerminateThread: tid={t.thread_id} (handle=0x{handle:x}) terminated")
                return True
        return None

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

    def unblock_handle(self, handle: int) -> int:
        """Mark threads blocked on handle as READY when the object is signaled.

        Does NOT consume the signal — the thread will do that when it retries
        the Wait stub.  Returns the number of threads unblocked.
        """
        n = 0
        for t in self.threads:
            if (t.status == ThreadStatus.BLOCKED_HANDLES
                    and t.waiting_on_handles is not None
                    and handle in t.waiting_on_handles):
                t.waiting_on_handles = None
                t.status = ThreadStatus.READY
                n += 1
                logger.debug("scheduler",
                    f"unblock_handle: tid={t.thread_id} ready "
                    f"(handle 0x{handle:x} signaled)")
        return n

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
