"""Win32 API stub handler infrastructure.

Instead of executing real DLL code, intercepts IAT calls and dispatches them
to Python callbacks via an INT 0xFE trampoline mechanism.

Architecture:
  1. Reserve memory at HANDLER_BASE (0x00200000) for stub trampolines
  2. Each stub writes: INT 0xFE; RET; <INT3 padding>
  3. cpu.on_interrupt dispatches INT 0xFE to _handle_api_int
  4. ImportResolver writes stub addresses into the IAT
"""

from __future__ import annotations

import collections
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory

from tew.api.nt_syscall import NtSyscallDispatcher
from tew.hardware.cpu_zig import ESP, ZigCPU
from tew.hardware.cpu_zig import _lib as _cpu_lib
from tew.logger import logger, set_current_handler

# ── Constants ────────────────────────────────────────────────────────────────

# Stub region: 0x00200000 – 0x002FFFFF (1 MB reserved)
HANDLER_BASE: int = 0x00200000
HANDLER_SIZE: int = 32          # bytes per stub (generous, most need ~7)
MAX_HANDLERS: int = 4096

# INT number used for stubs that need Python logic
STUB_INT: int = 0xFE

# Stub names suppressed from trace-level call logging (too noisy to be useful)
_TRACE_SUPPRESS: frozenset[str] = frozenset({"EnterCriticalSection", "LeaveCriticalSection"})

# Per-handler wall-clock accounting (2026-09-14, temporary): answers "is the
# DB thread's time inside Python handler code (incl. ctypes/Zig crossings)
# or in native CPU execution between calls" -- Molly's boundary-crossing
# question after the thread-time-probe showed tid=1011 dominating. Keyed by
# func_name only (no scheduler access at this dispatch point) -- since the
# DB thread already dominates overall, fileio-handler totals here are a
# reasonable proxy for its own I/O-handler cost even without per-thread
# breakdown.
_HANDLER_TIME_TOTALS: dict = collections.defaultdict(float)
_HANDLER_CALL_COUNTS: dict = collections.defaultdict(int)
_HANDLER_TIME_SAMPLE_COUNT = [0]

# Trampolines used by dialog / DllMain bootstrap sequences
DIALOG_TRAMPOLINE: int = 0x00210000
DLLMAIN_TRAMPOLINE: int = 0x00210010
DLLMAIN_HANDLE_STORE: int = 0x00210018

# ── Types ────────────────────────────────────────────────────────────────────

ApiHandler = Callable[["CPU"], None]


@dataclass
class HandlerEntry:
    name: str        # e.g. "kernel32.dll!GetVersion"
    dll_name: str    # e.g. "kernel32.dll"
    func_name: str   # e.g. "GetVersion"
    address: int     # address of the stub trampoline in memory
    handler_id: int  # index for INT 0xFE dispatch
    # None for an export implemented as guest code (register_guest_code):
    # its address is the start of real x86 code, not an INT 0xFE trampoline.
    handler: ApiHandler | None
    # Precomputed once at registration so the per-call dispatch path does no
    # string formatting or substring scanning (INT 0xFE is the hottest path).
    log_entry: str = field(init=False)
    trace_suppressed: bool = field(init=False)

    def __post_init__(self) -> None:
        self.log_entry = f"{self.name} @ 0x{self.address:x}"
        self.trace_suppressed = any(s in self.name for s in _TRACE_SUPPRESS)


# ── Timer type ───────────────────────────────────────────────────────────────

_TIME_CALLBACK_EVENT_SET   = 0x10
_TIME_CALLBACK_EVENT_PULSE = 0x20


@dataclass
class PendingTimer:
    id: int
    due_at: float    # absolute virtual-ms timestamp when callback fires
    period_ms: int   # 0 = one-shot, >0 = periodic interval
    cb_addr: int     # TIMECALLBACK address OR Win32 event handle (see fu_event)
    dw_user: int     # passed through as arg 3
    fu_event: int = 0  # fuEvent flags from timeSetEvent


# Module-level timer table: id → PendingTimer
pending_timers: dict[int, PendingTimer] = {}

# ── Helper ───────────────────────────────────────────────────────────────────


def cleanup_stdcall(cpu: CPU, memory: Memory, arg_bytes: int) -> None:
    """For stdcall: move return address past args so the RET skips them.

    Runs on nearly every API call, so a real ZigCPU does it in one libcpu
    call (cpu_stdcall_cleanup) instead of six ctypes crossings. The Python
    path below is the same operation for test CPU fakes.
    """
    if type(cpu) is ZigCPU and memory is cpu.memory:
        if not _cpu_lib.cpu_stdcall_cleanup(cpu._state, arg_bytes):
            esp = cpu.regs[ESP]
            raise RuntimeError(
                f"cleanup_stdcall: stack slot out of bounds (ESP=0x{esp:08x}, "
                f"arg_bytes={arg_bytes}, memory size=0x{memory.size:x})"
            )
        return
    ret_addr = memory.read32(cpu.regs[ESP] & 0xFFFFFFFF)
    cpu.regs[ESP] = (cpu.regs[ESP] + arg_bytes) & 0xFFFFFFFF
    memory.write32(cpu.regs[ESP], ret_addr)


def unimplemented_halt(name: str) -> Callable[[CPU], None]:
    """Return a handler that halts loudly with an UNIMPLEMENTED log.

    Use for a real Win32 API this project has deliberately not implemented
    yet, so calling it fails loudly instead of silently returning garbage.
    """
    def _h(cpu: CPU) -> None:
        logger.error("handlers", f"[UNIMPLEMENTED] {name} — halting")
        log_register_dump(cpu)
        cpu.halted = True
        cpu.fatal_halt = True
    return _h


def log_register_dump(cpu: CPU, category: str = "cpu") -> None:
    """Log all 8 GP registers plus the stdcall/cdecl return address and a run
    of stack slots below ESP.

    Shared by every "unimplemented API/method, halt loudly" handler (the
    dll_loader.py IAT auto-stub and the D3D8 COM-vtable `_halt` closures) so
    a halt always carries enough live state to diagnose without a re-run --
    most Win32/COM calls take 0-6 stdcall args sitting right above ESP, and
    printing them here beats adding a one-off logpoint and re-running.
    """
    esp = cpu.regs[ESP] & 0xFFFFFFFF
    eip = getattr(cpu, "eip", None)
    eip_str = f"0x{eip & 0xFFFFFFFF:08x}" if eip is not None else "?"
    logger.error(
        category,
        f"  EIP={eip_str}  "
        f"EAX=0x{cpu.regs[0] & 0xFFFFFFFF:08x}  "
        f"ECX=0x{cpu.regs[1] & 0xFFFFFFFF:08x}  "
        f"EDX=0x{cpu.regs[2] & 0xFFFFFFFF:08x}  "
        f"EBX=0x{cpu.regs[3] & 0xFFFFFFFF:08x}",
    )
    logger.error(
        category,
        f"  ESP=0x{esp:08x}  "
        f"EBP=0x{cpu.regs[5] & 0xFFFFFFFF:08x}  "
        f"ESI=0x{cpu.regs[6] & 0xFFFFFFFF:08x}  "
        f"EDI=0x{cpu.regs[7] & 0xFFFFFFFF:08x}",
    )
    try:
        mem = cpu.memory
        ret_addr = mem.read32(esp)
        args = [mem.read32((esp + 4 + i * 4) & 0xFFFFFFFF) for i in range(6)]
        logger.error(
            category,
            f"  [ESP]=ret 0x{ret_addr:08x}  args="
            + " ".join(f"0x{a:08x}" for a in args),
        )
    except Exception as err:
        logger.error(category, f"  (failed to read stack args: {err})")


# ── Win32Handlers ─────────────────────────────────────────────────────────────


class Win32Handlers:
    """Manages Win32 API stub trampolines and INT 0xFE dispatch."""

    def __init__(self, memory: Memory) -> None:
        self._handlers: dict[str, HandlerEntry] = {}          # "dllname!funcName" → entry
        self._handlers_by_id: list[HandlerEntry] = []
        self._handlers_by_addr: dict[int, HandlerEntry] = {}  # trampoline/patched code address → entry
        self._next_handler_addr: int = HANDLER_BASE
        self._memory: Memory = memory
        self._installed: bool = False
        # Recent stub calls as [entry, repeat_count]; consecutive calls to the
        # same entry bump the count instead of appending. Formatted to strings
        # only when read (get_call_log), never on the dispatch path.
        self._call_log_size: int = 2000
        self._call_log: collections.deque[list] = collections.deque(maxlen=self._call_log_size)
        self._nt_dispatcher: NtSyscallDispatcher = NtSyscallDispatcher(memory)

    @property
    def nt_dispatcher(self) -> NtSyscallDispatcher:
        return self._nt_dispatcher

    # ── Registration ─────────────────────────────────────────────────────────

    def register_handler(self, dll_name: str, func_name: str, handler: ApiHandler) -> None:
        """Register a stub for a Win32 API function.

        The handler receives the CPU and should set EAX (and optionally write
        to memory via pointers in registers/stack) then return. The stub
        trampoline handles the RET.
        """
        key = f"{dll_name.lower()}!{func_name}"
        if key in self._handlers:
            return  # already registered

        handler_id = len(self._handlers_by_id)
        address = self._next_handler_addr
        self._next_handler_addr += HANDLER_SIZE

        if handler_id >= MAX_HANDLERS:
            raise RuntimeError(f"Too many Win32 stubs (max {MAX_HANDLERS})")

        entry = HandlerEntry(
            name=key,
            dll_name=dll_name.lower(),
            func_name=func_name,
            address=address,
            handler_id=handler_id,
            handler=handler,
        )

        self._handlers[key] = entry
        self._handlers_by_id.append(entry)
        self._handlers_by_addr[address] = entry

        # Write stub machine code into memory:
        #   INT 0xFE  → CD FE   (triggers Python handler via on_interrupt)
        #   RET       → C3      (return to caller)
        #   INT3 (CC) padding for safety
        offset = address
        self._memory.write8(offset, 0xCD)       # INT
        offset += 1
        self._memory.write8(offset, STUB_INT)   # 0xFE
        offset += 1
        self._memory.write8(offset, 0xC3)       # RET
        offset += 1
        while offset < address + HANDLER_SIZE:
            self._memory.write8(offset, 0xCC)   # INT3
            offset += 1

    def register_guest_code(
        self,
        dll_name: str,
        code: bytes,
        exports: dict[str, int],
        hooks: dict[int, tuple[str, ApiHandler]],
    ) -> int:
        """Register exports implemented as guest x86 code instead of a Python handler.

        For hot APIs whose common path never needs Python (e.g. an
        uncontended EnterCriticalSection): the guest runs ``code`` natively
        and only traps to Python at its hook points. ``code`` is placed in
        consecutive stub slots; ``exports`` maps each exported name to its
        entry offset in ``code``; ``hooks`` maps the offset of each
        ``INT 0xFE`` (CD FE) in ``code`` to a (name, handler) pair. When a
        hook's handler returns normally, execution continues after the
        ``INT 0xFE`` -- the handler does NOT get the stub's implicit RET.
        Returns the address the code was placed at.
        """
        dll = dll_name.lower()
        for offset in hooks:
            if code[offset:offset + 2] != bytes([0xCD, STUB_INT]):
                raise ValueError(
                    f"register_guest_code({dll}): hook offset 0x{offset:x} is not an INT 0x{STUB_INT:02X}"
                )
        for func_name, offset in exports.items():
            if not 0 <= offset < len(code):
                raise ValueError(
                    f"register_guest_code({dll}): export {func_name} offset 0x{offset:x} outside the code"
                )
            if f"{dll}!{func_name}" in self._handlers:
                raise ValueError(f"register_guest_code: {dll}!{func_name} is already registered")

        slots = -(-len(code) // HANDLER_SIZE)
        base = self._next_handler_addr
        self._next_handler_addr += slots * HANDLER_SIZE
        self._memory.load(base, code)
        tail = slots * HANDLER_SIZE - len(code)
        if tail:
            self._memory.load(base + len(code), b"\xCC" * tail)  # INT3 padding

        for func_name, offset in exports.items():
            key = f"{dll}!{func_name}"
            entry = HandlerEntry(
                name=key, dll_name=dll, func_name=func_name,
                address=base + offset, handler_id=len(self._handlers_by_id), handler=None,
            )
            self._handlers[key] = entry
            self._handlers_by_id.append(entry)
        for offset, (hook_name, handler) in hooks.items():
            entry = HandlerEntry(
                name=f"{dll}!{hook_name}", dll_name=dll, func_name=hook_name,
                address=base + offset, handler_id=len(self._handlers_by_id), handler=handler,
            )
            self._handlers_by_id.append(entry)
            self._handlers_by_addr[base + offset] = entry
        if len(self._handlers_by_id) > MAX_HANDLERS:
            raise RuntimeError(f"Too many Win32 stubs (max {MAX_HANDLERS})")
        return base

    def patch_address_to_guest_code(self, addr: int, name: str, target: int) -> None:
        """Patch loaded code at ``addr`` with ``JMP target`` (5 bytes), for a
        real DLL export whose implementation is registered guest code."""
        rel = (target - (addr + 5)) & 0xFFFFFFFF
        self._memory.load(addr, bytes([0xE9]) + rel.to_bytes(4, "little"))
        logger.debug("handlers", f"[Win32Handlers] Patched 0x{addr:x} => JMP 0x{target:x} ({name})")

    def patch_address(self, addr: int, name: str, handler: ApiHandler) -> None:
        """Patch a specific address in loaded code to redirect to a Python handler.

        Overwrites the first 3 bytes at ``addr`` with INT 0xFE; RET.
        Use this for internal functions not called through the IAT
        (e.g. CRT internal functions like _sbh_heap_init).
        Must be called AFTER sections are loaded into memory.
        """
        handler_id = len(self._handlers_by_id)
        entry = HandlerEntry(
            name=f"patch:{name}",
            dll_name="patch",
            func_name=name,
            address=addr,
            handler_id=handler_id,
            handler=handler,
        )

        self._handlers_by_id.append(entry)
        self._handlers_by_addr[addr] = entry

        # Overwrite code at addr with: INT 0xFE; RET
        self._memory.write8(addr, 0xCD)           # INT
        self._memory.write8(addr + 1, STUB_INT)   # 0xFE
        self._memory.write8(addr + 2, 0xC3)       # RET

        from tew.logger import logger
        logger.debug("handlers", f"[Win32Handlers] Patched 0x{addr:x} => {name}")

    # ── Lookup helpers ────────────────────────────────────────────────────────

    def get_handler_address(self, dll_name: str, func_name: str) -> int | None:
        """Return the stub address for a function, or None if not stubbed."""
        key = f"{dll_name.lower()}!{func_name}"
        entry = self._handlers.get(key)
        return entry.address if entry is not None else None

    def lookup_handler_address(self, dll_name: str, func_name: str) -> int:
        """Return the trampoline address or 0 if not registered."""
        key = f"{dll_name.lower()}!{func_name}"
        entry = self._handlers.get(key)
        return entry.address if entry is not None else 0

    def has_handler(self, dll_name: str, func_name: str) -> bool:
        """Return True if the function is stubbed."""
        return f"{dll_name.lower()}!{func_name}" in self._handlers

    def find_handler_by_func_name(self, func_name: str) -> HandlerEntry | None:
        """Find a stub entry by function name across all DLL registrations."""
        for entry in self._handlers_by_id:
            if entry.func_name == func_name:
                return entry
        return None

    def get_stub_dll_handle(self, dll_name: str) -> int | None:
        """Return a stable handle for a stub-only DLL, or None if not registered.

        For DLLs implemented entirely by handler stubs (kernel32, user32, etc.)
        there is no real LoadedDLL entry in the DLL loader.  The address of the
        first registered handler for the DLL is a stable non-NULL value in our
        memory space, so it works as a module handle that satisfies pointer
        comparisons and NULL checks in the game.
        """
        norm = dll_name.lower()
        if not norm.endswith(".dll"):
            norm += ".dll"
        for entry in self._handlers_by_id:
            if entry.dll_name == norm:
                return entry.address
        return None

    def get_dll_name_for_stub_handle(self, handle: int) -> str | None:
        """Reverse of get_stub_dll_handle: given a stub-region address, return the DLL name.

        GetModuleHandleA returns the first stub address for a DLL (i.e. get_stub_dll_handle).
        GetProcAddress then passes that address back as hModule.  This method resolves it.
        """
        for entry in self._handlers_by_id:
            if entry.address == handle:
                return entry.dll_name
        return None

    def get_registered_handlers(self) -> list[dict]:
        """Return all registered stubs (for diagnostics)."""
        return [
            {"dll_name": e.dll_name, "func_name": e.func_name, "address": e.address}
            for e in self._handlers_by_id
        ]

    def get_call_log(self) -> list[str]:
        """Return the recent stub call log, oldest first, as display strings.

        Consecutive repeats of the same stub collapse to one line with an
        `` xN`` suffix.
        """
        return [
            entry.log_entry if count == 1 else f"{entry.log_entry} x{count}"
            for entry, count in self._call_log
        ]

    @property
    def count(self) -> int:
        """Total number of registered stubs."""
        return len(self._handlers_by_id)

    # ── Installation ──────────────────────────────────────────────────────────

    def install(self, cpu: CPU) -> None:
        """Install the INT 0xFE handler on the CPU.

        Must be called after all stubs are registered.
        """
        if self._installed:
            return
        self._installed = True

        # Capture the existing interrupt handler (if any) so we can delegate
        # unrecognised interrupt numbers to it.
        existing_handler = cpu._int_handler

        stubs = self

        nt_dispatcher = self._nt_dispatcher

        def _dispatch(int_num: int, c: CPU) -> None:
            if int_num == STUB_INT:
                stubs._handle_api_int(c)
                return
            if int_num == 0x2E:
                nt_dispatcher.dispatch(c)
                return
            if int_num == 3:
                # INT3 debug breakpoint. Real Windows raises STATUS_BREAKPOINT
                # as a normal, dispatchable structured exception, not an
                # automatic crash -- and this game relies on exactly that:
                # `_Nfs_DebuggerIsPresent` is hardcoded to 1 in this debug
                # build (set unconditionally in WinMain, not from an
                # IsDebuggerPresent() check), so every one of its ~1,780
                # assertion call sites throughout the binary executes a real
                # INT3 on purpose. The game installs its own SEH frame
                # (_CLayer_CatchSEH) specifically to catch STATUS_BREAKPOINT,
                # log, and continue -- exactly what real Windows does here
                # with no debugger attached. Route through the same SEH-chain
                # dispatch already used for access violations instead of
                # treating every one of those sites as instant-fatal.
                from tew.kernel.seh import STATUS_BREAKPOINT, dispatch_exception
                # EIP has already advanced past the 1-byte opcode by the time
                # this handler runs, but ExceptionAddress/CONTEXT.Eip must
                # point AT the INT3 itself, matching real Windows.
                fault_eip = (c.eip - 1) & 0xFFFFFFFF
                c.eip = fault_eip
                handled = dispatch_exception(c, stubs._memory, STATUS_BREAKPOINT, fault_eip)
                if handled:
                    logger.debug(
                        "seh",
                        f"INT3 at EIP=0x{fault_eip:08x} handled by game's own SEH chain -- resuming",
                    )
                    return
                # Genuinely unhandled: same permanent, unclearable halt as
                # before. A plain halted=True gets silently cleared by the
                # very next scheduler thread-switch or nested
                # _invoke_emulated_proc call (same class of gap fixed
                # elsewhere for unhandled access-violation faults), so this
                # must still be a fatal_halt -- now reached only after really
                # giving the game's own SEH chain a chance, not on sight.
                logger.error(
                    "handlers",
                    f"INT3 breakpoint at EIP=0x{fault_eip:08x} unhandled by SEH chain -- halting",
                )
                c.halted = True
                c.fatal_halt = True
                return
            # Delegate to whatever was installed before us
            if existing_handler is not None:
                existing_handler(int_num, c)
            else:
                from tew.logger import logger as _lg
                eip = c.eip & 0xFFFFFFFF
                ecx = c.regs[1] & 0xFFFFFFFF  # ECX
                esi = c.regs[6] & 0xFFFFFFFF  # ESI
                esp = c.regs[4] & 0xFFFFFFFF  # ESP
                # Dump vtable slots around the bad call
                try:
                    vtable_dump = ' '.join(
                        f'[{i}]=0x{stubs._memory.read32((ecx + i*4) & 0xFFFFFFFF):08x}'
                        for i in range(12))
                    _lg.error("exception", f"vtable at ECX=0x{ecx:08x}: {vtable_dump}")
                except Exception:
                    pass
                try:
                    _lg.error("exception",
                        f"ESI=0x{esi:08x} → *ESI=0x{stubs._memory.read32(esi):08x}")
                    _lg.error("exception",
                        f"[ESP+0]=0x{stubs._memory.read32(esp):08x} "
                        f"call-site ← 0x{stubs._memory.read32(esp):08x} - 5 = 0x{(stubs._memory.read32(esp) - 5) & 0xFFFFFFFF:08x}")
                except Exception:
                    pass
                raise RuntimeError(
                    f"Unhandled interrupt INT 0x{int_num:02x} at EIP=0x{eip:08x}"
                )

        cpu.on_interrupt(_dispatch)

    # ── INT 0xFE dispatch ─────────────────────────────────────────────────────

    def _handle_api_int(self, cpu: CPU) -> None:
        """Handle INT 0xFE — find which stub was called and execute its handler.

        EIP is now pointing past the INT 0xFE instruction (at the RET).
        The stub address is EIP - 2 (INT 0xFE is 2 bytes).
        """
        handler_addr = (cpu.eip - 2) & 0xFFFFFFFF

        entry = self._handlers_by_addr.get(handler_addr)
        if entry is None:
            raise RuntimeError(f"Unknown Win32 stub at 0x{handler_addr:08x}")


        # set_current_handler lets LOG_CATEGORIES target this specific
        # function (e.g. "handlers.CompareStringA" or "calls.CompareStringA")
        # for the full duration of this call, including the trace line
        # below -- save/restore rather than a plain set/clear so a handler
        # that itself triggers a nested dispatched call doesn't leave the
        # wrong name active once the inner call returns.
        previous_handler = set_current_handler(entry.func_name)
        try:
            # Log the stub call; deduplicate consecutive identical calls with a counter
            if not entry.trace_suppressed:
                logger.trace("calls", entry.log_entry)
            call_log = self._call_log
            if call_log and call_log[-1][0] is entry:
                call_log[-1][1] += 1
            else:
                call_log.append([entry, 1])

            # Execute the Python handler
            # EIP already points at RET, so the CPU will execute RET next.
            entry.handler(cpu)
            # Per-handler wall-clock probe -- 2026-09-14: fix verified (see
            # kernel32_sync.py's CriticalSectionEntry change) and committed,
            # freeing this for the next investigation. Re-enable by
            # restoring the perf_counter wrap + probe print below.
            # _t0 = time.perf_counter()
            # entry.handler(cpu)
            # _HANDLER_TIME_TOTALS[entry.func_name] += time.perf_counter() - _t0
            # _HANDLER_CALL_COUNTS[entry.func_name] += 1
            # _HANDLER_TIME_SAMPLE_COUNT[0] += 1
            # if _HANDLER_TIME_SAMPLE_COUNT[0] % 500 == 0:
            #     _grand_total = sum(_HANDLER_TIME_TOTALS.values()) or 1.0
            #     _top = sorted(_HANDLER_TIME_TOTALS.items(), key=lambda kv: -kv[1])[:10]
            #     _breakdown = ", ".join(
            #         f"{name}={t:.3f}s(n={_HANDLER_CALL_COUNTS[name]})"
            #         for name, t in _top
            #     )
            #     logger.error(
            #         "cpu",
            #         f"[handler-time-probe] total={_grand_total:.3f}s calls={_HANDLER_TIME_SAMPLE_COUNT[0]} {_breakdown}",
            #     )
        finally:
            set_current_handler(previous_handler)
