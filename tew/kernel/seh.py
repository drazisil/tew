"""32-bit Structured Exception Handling (SEH) dispatch.

Real Windows SEH on x86 works via a singly-linked list of
EXCEPTION_REGISTRATION_RECORD structures, chained through FS:[0]
(TEB.ExceptionList). Each record is pushed onto the stack by a function's
own compiler-generated prologue -- that chain maintenance already happens
for free as ordinary memory writes during normal execution (confirmed:
KernelStructures already models TEB.ExceptionList at FS:[0x00]). This
module's job is purely to be a correct DISPATCHER: on a fault or an
explicit RaiseException, build the real EXCEPTION_RECORD/CONTEXT
structures, walk the chain, invoke each handler with the documented ABI,
and interpret its return value.

Deliberately NOT reimplemented here: MSVC's _except_handler3 scope-table
walking, filter evaluation, or C++ destructor unwinding logic. Where a
handler is real compiled code already present in the target binary (the
common case for a statically-linked CRT), it just runs natively -- exactly
like any other code this emulator executes. Where a handler is itself an
intercepted Win32/CRT-internal API (RtlUnwind), it's given a real
implementation here rather than the previous `_halt` placeholder.

Known, documented simplifications (see individual functions):
  - ExceptionAddress is read from the CPU's EIP at the moment a fault is
    detected, which for some opcodes may already point past the actual
    faulting instruction (EIP advances during instruction fetch, before
    the memory access that triggers the fault) -- an honest approximation,
    not exact hardware fidelity.
  - RtlUnwind resumes at TargetIp with ESP set to what it would be after an
    ordinary `RET <argbytes>` back to RtlUnwind's OWN caller (i.e. the
    caller's real stack depth at the moment it called RtlUnwind) -- not
    TargetFrame's address, which is just the SEH registration record's own
    location and generally unrelated to the caller's actual stack depth.
    Confirmed live 2026-08-24: MSVC's `__global_unwind2` calls RtlUnwind
    with TargetIp = its own return address purely as a "fake return" trick
    (so its own ordinary epilogue can run afterward) -- using ESP=TargetFrame
    there made that epilogue pop the SEH registration record's own fields as
    if they were its saved registers, landing back inside __except_handler3
    via unrelated leftover stack content instead of a real return address.
    EBP is left as whatever the unwind-triggering handler's own execution
    left it as (see the separate EBP-restoration logic below for the common
    case).
  - ExceptionContinueExecution (retry the faulting instruction) is
    accepted and clears the fault, but since EIP has typically already
    advanced past the faulting instruction by the time the fault is
    detected (see above), "retry" in practice resumes at the *next*
    instruction, not a true retry. This case is rare in real handlers
    (almost everything uses ContinueSearch + unwind instead).

VERIFIED 2026-07-10 against the real MCity_d.exe (needs the mco-server
auth server running first): two real faults occurred during a run that
reached actual gameplay rendering (~196M steps). EIP=0xccccccce (MSVC's
debug-build uninitialized-stack poison pattern -- a genuine jump through a
garbage function pointer) was caught and handled by the game's own SEH
chain; execution correctly continued afterward into the game's own real
_CrtDbgReport/CRT-assertion code path (patch_internals.py, pre-existing,
not part of this module) -- confirming the full dispatch pipeline against
real compiled code, not just the synthetic test suite. A second fault at
EIP=0x00a6bfcb (inside the MAD audio decoder) matched a handler at
0x00c76920 that ran past _STEP_LIMIT without returning or escaping --
logged honestly as unhandled rather than pretending success. Not yet
investigated whether that handler is doing legitimate heavy work (e.g.
audio resync/error-recovery) or is genuinely stuck -- worth checking in
Ghidra before assuming either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu_zig import EAX, ESP, EBP
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall
from tew.logger import logger

# ── EXCEPTION_DISPOSITION values (winnt.h) ────────────────────────────────────
EXCEPTION_CONTINUE_EXECUTION = 0
EXCEPTION_CONTINUE_SEARCH    = 1
EXCEPTION_NESTED_EXCEPTION   = 2
EXCEPTION_COLLIDED_UNWIND    = 3

# ── ExceptionFlags bits ────────────────────────────────────────────────────────
EXCEPTION_NONCONTINUABLE = 0x01
EH_UNWINDING             = 0x02

# ── Common ExceptionCode values (ntstatus.h) ──────────────────────────────────
STATUS_ACCESS_VIOLATION         = 0xC0000005
STATUS_INTEGER_DIVIDE_BY_ZERO   = 0xC0000094
STATUS_ILLEGAL_INSTRUCTION      = 0xC000001D
STATUS_BREAKPOINT               = 0x80000003

# ── Structure sizes ────────────────────────────────────────────────────────────
EXCEPTION_MAXIMUM_PARAMETERS = 15
EXCEPTION_RECORD_SIZE = 4 + 4 + 4 + 4 + 4 + EXCEPTION_MAXIMUM_PARAMETERS * 4  # 0x50
CONTEXT_SIZE = 0x2CC  # x86 CONTEXT (winnt.h) -- stable, public structure

# ── Fixed sentinel address ─────────────────────────────────────────────────────
# Mirrors THREAD_SENTINEL's pattern (tew/api/_state.py) exactly -- a fixed,
# manually-written "INT 0xFE; RET" landing pad used as the pushed return
# address whenever this module calls into guest code (a handler function)
# and needs to know precisely when it returns. Deliberately separate from
# THREAD_SENTINEL: that one carries thread-lifecycle side effects
# (_make_thread_return_handler) this module must not trigger.
SEH_RETURN_SENTINEL = 0x001FE010

_STEP_BATCH = 10_000
_STEP_LIMIT = 2_000_000  # safety net against a genuinely broken/looping handler
# Observed live 2026-07-10: a real handler at 0x00c76920 (matched for a
# fault at 0x00a6bfcb, inside the MAD audio decoder) hit this limit against
# the real MCity_d.exe. Not yet determined whether it's legitimate heavy
# work (audio resync/error-recovery is plausible) or genuinely stuck --
# see seh.py's module docstring "VERIFIED" section. If raising this value
# is ever considered, check that address in Ghidra first rather than
# guessing a bigger number.


class SehHandlerTimeout(Exception):
    """A handler ran past _STEP_LIMIT without returning or escaping."""
    def __init__(self, handler_addr: int) -> None:
        self.handler_addr = handler_addr
        super().__init__(f"SEH handler at 0x{handler_addr:08x} exceeded step limit")


class SehHandlerEscaped(Exception):
    """A handler didn't return via the sentinel -- it redirected execution
    itself. Two genuinely different things produce this identical shape
    (halted at some EIP other than the sentinel):
      - The expected case: the handler called RtlUnwind, which jumps
        directly to the __except block and never returns normally to
        whoever invoked the handler.
      - A real bug case: the handler's OWN execution (or the real code
        RtlUnwind jumped into) hit a genuine CPU fault -- core.zig's
        memRead8/memWrite8 set halted=True the same way a clean escape
        does. `faulted` distinguishes them; callers must check it rather
        than assume every escape is a clean one."""
    def __init__(self, handler_addr: int, eip: int, *, faulted: bool, esp_before: int) -> None:
        self.handler_addr = handler_addr
        self.eip = eip
        self.faulted = faulted
        self.esp_before = esp_before
        super().__init__(f"SEH handler at 0x{handler_addr:08x} escaped to 0x{eip:08x}")


def install(stubs: Win32Handlers, memory: "Memory") -> None:
    """Writes the SEH return sentinel's trampoline bytes and registers its
    (side-effect-free) handler. Call once during handler registration,
    mirroring THREAD_SENTINEL's own installation in crt_handlers.py."""
    memory.write8(SEH_RETURN_SENTINEL,     0xCD)  # INT
    memory.write8(SEH_RETURN_SENTINEL + 1, 0xFE)  # vector 0xFE
    memory.write8(SEH_RETURN_SENTINEL + 2, 0xC3)  # RET (never actually reached --
                                                    # the handler below halts first)

    def _sentinel_handler(cpu: "CPU") -> None:
        cpu.halted = True

    stubs.patch_address(SEH_RETURN_SENTINEL, "_sehReturnSentinel", _sentinel_handler)


def _invoke_handler(cpu: "CPU", memory: "Memory", handler_addr: int, args: list[int]) -> int:
    """Calls handler_addr(*args) [PEXCEPTION_ROUTINE ABI: 4 args, pushed
    left-to-right so arg1 ends up at [ESP+4]] and returns its EAX
    (disposition). Restores ESP to its exact pre-call value on a normal
    return -- correct regardless of whether the handler itself cleans up
    args, since we captured the real pre-call ESP directly rather than
    computing an offset.

    Raises SehHandlerEscaped if the handler redirects execution instead of
    returning via the sentinel (the RtlUnwind-called-internally case) --
    callers must not touch ESP/EIP themselves in that case, since the
    handler already did.
    """
    esp_before = cpu.regs[ESP] & 0xFFFFFFFF
    esp = esp_before
    for arg in reversed(args):
        esp = (esp - 4) & 0xFFFFFFFF
        memory.write32(esp, arg & 0xFFFFFFFF)
    esp = (esp - 4) & 0xFFFFFFFF
    memory.write32(esp, SEH_RETURN_SENTINEL)
    cpu.regs[ESP] = esp
    cpu.eip = handler_addr & 0xFFFFFFFF
    if not cpu.fatal_halt:
        cpu.halted = False

    steps_left = _STEP_LIMIT
    while not cpu.halted and steps_left > 0:
        batch = min(_STEP_BATCH, steps_left)
        cpu.run(batch)
        steps_left -= batch

    if not cpu.halted:
        cpu.halted = True
        cpu.fatal_halt = True
        cpu.regs[ESP] = esp_before
        raise SehHandlerTimeout(handler_addr)

    landed_eip = cpu.eip & 0xFFFFFFFF

    if landed_eip != (SEH_RETURN_SENTINEL + 2) & 0xFFFFFFFF:
        # Escaped -- the halt we just observed is NOT our own sentinel's
        # bookkeeping, it's whatever real state the redirected execution
        # left behind (which may itself be a genuine `hlt`, a real fault,
        # or anything else). Leave it exactly as-is: clearing it here
        # would silently erase a real halt/fault that actually happened.
        # cpu.faulted distinguishes a real fault from a clean RtlUnwind
        # escape -- callers must not assume every escape is clean.
        raise SehHandlerEscaped(handler_addr, landed_eip, faulted=cpu.faulted, esp_before=esp_before)

    # Only our own sentinel's halt gets cleared -- that one is purely
    # internal bookkeeping for this call, not a real CPU event.
    if not cpu.fatal_halt:
        cpu.halted = False
    disposition = cpu.regs[EAX] & 0xFFFFFFFF
    cpu.regs[ESP] = esp_before
    return disposition


def _write_exception_record(
    memory: "Memory", addr: int, code: int, address: int,
    parameters: tuple[int, ...], noncontinuable: bool,
) -> None:
    flags = EXCEPTION_NONCONTINUABLE if noncontinuable else 0
    memory.write32(addr + 0x00, code & 0xFFFFFFFF)
    memory.write32(addr + 0x04, flags)
    memory.write32(addr + 0x08, 0)               # ExceptionRecord (chained records) -- unused here
    memory.write32(addr + 0x0C, address & 0xFFFFFFFF)
    n = min(len(parameters), EXCEPTION_MAXIMUM_PARAMETERS)
    memory.write32(addr + 0x10, n)
    for i in range(EXCEPTION_MAXIMUM_PARAMETERS):
        val = parameters[i] if i < n else 0
        memory.write32(addr + 0x14 + i * 4, val & 0xFFFFFFFF)


def _write_context(memory: "Memory", addr: int, cpu: "CPU") -> None:
    """Captures the CPU's current integer register state into a real x86
    CONTEXT structure (winnt.h layout -- public, stable). FloatSave/
    ExtendedRegisters are zeroed: this emulator's FPU/MMX state isn't
    threaded through exception CONTEXTs, an existing limitation elsewhere
    too, not something new introduced here."""
    for i in range(addr, addr + CONTEXT_SIZE, 4):
        memory.write32(i, 0)
    memory.write32(addr + 0x00, 0x10007)  # ContextFlags = CONTEXT_FULL
    memory.write32(addr + 0x9C, cpu.regs[7])       # Edi
    memory.write32(addr + 0xA0, cpu.regs[6])       # Esi
    memory.write32(addr + 0xA4, cpu.regs[3])       # Ebx
    memory.write32(addr + 0xA8, cpu.regs[2])       # Edx
    memory.write32(addr + 0xAC, cpu.regs[1])       # Ecx
    memory.write32(addr + 0xB0, cpu.regs[0])       # Eax
    memory.write32(addr + 0xB4, cpu.regs[5])       # Ebp
    memory.write32(addr + 0xB8, cpu.eip & 0xFFFFFFFF)
    memory.write32(addr + 0xC0, cpu.eflags & 0xFFFFFFFF)
    memory.write32(addr + 0xC4, cpu.regs[4])       # Esp


def _apply_context(memory: "Memory", addr: int, cpu: "CPU") -> None:
    """Reverse of _write_context -- applies a (possibly handler-modified)
    CONTEXT back onto the CPU. Only used for ExceptionContinueExecution,
    which is rare (see module docstring)."""
    cpu.regs[7] = memory.read32(addr + 0x9C)
    cpu.regs[6] = memory.read32(addr + 0xA0)
    cpu.regs[3] = memory.read32(addr + 0xA4)
    cpu.regs[2] = memory.read32(addr + 0xA8)
    cpu.regs[1] = memory.read32(addr + 0xAC)
    cpu.regs[0] = memory.read32(addr + 0xB0)
    cpu.regs[5] = memory.read32(addr + 0xB4)
    cpu.eip     = memory.read32(addr + 0xB8)
    cpu.eflags  = memory.read32(addr + 0xC0)
    cpu.regs[4] = memory.read32(addr + 0xC4)


def dispatch_exception(
    cpu: "CPU", memory: "Memory", exception_code: int, exception_address: int,
    parameters: tuple[int, ...] = (), noncontinuable: bool = False,
) -> bool:
    """Dispatches a hardware or software exception through the real SEH
    chain (FS:[0]). Returns True if some handler resolved it (either by
    ExceptionContinueExecution, or by escaping via RtlUnwind -- both mean
    "the caller should resume the CPU normally, something already fixed
    EIP/ESP appropriately"); False if the chain was exhausted unhandled,
    in which case the caller should fall back to today's report-and-halt
    behavior -- this function never fakes a recovery it didn't actually
    perform.
    """
    fs_base = cpu.kernel_structures.get_fs_base()

    # Scratch space for EXCEPTION_RECORD + CONTEXT lives below the current
    # stack, mirroring where real KiUserExceptionDispatcher places them --
    # not a fixed global buffer, so nested/reentrant dispatch (an exception
    # raised from inside a handler) doesn't corrupt an in-flight one.
    esp = cpu.regs[ESP] & 0xFFFFFFFF
    scratch = (esp - (EXCEPTION_RECORD_SIZE + CONTEXT_SIZE) - 0x200) & 0xFFFFFFFF
    record_addr = scratch
    context_addr = scratch + EXCEPTION_RECORD_SIZE

    _write_exception_record(memory, record_addr, exception_code, exception_address, parameters, noncontinuable)
    _write_context(memory, context_addr, cpu)

    frame = memory.read32(fs_base + 0x00)
    visited: set[int] = set()

    # Stashed so RtlUnwind can restore EBP for the common case (unwinding
    # back to the same frame/function the exception originated in) --
    # see _rtl_unwind below. Captured here, once, before the chain walk
    # reassigns `frame`: this is the original frame and the CPU's real EBP
    # at the moment of the fault, nothing has changed yet.
    cpu._seh_original_frame = frame
    cpu._seh_original_ebp = cpu.regs[EBP] & 0xFFFFFFFF

    while frame not in (0, 0xFFFFFFFF):
        if frame in visited:
            logger.error("seh", f"SEH chain cycle detected at 0x{frame:08x} -- aborting dispatch")
            break
        visited.add(frame)

        next_frame = memory.read32(frame + 0x00)
        handler = memory.read32(frame + 0x04)

        logger.debug("seh", f"dispatch: frame=0x{frame:08x} handler=0x{handler:08x} code=0x{exception_code:08x}")

        try:
            disposition = _invoke_handler(cpu, memory, handler, [record_addr, frame, context_addr, 0])
        except SehHandlerEscaped as e:
            if e.faulted:
                # Not a clean escape -- the handler's own execution (or
                # whatever RtlUnwind jumped into) hit a genuine second
                # fault. Reporting this as "handled" would silently resume
                # execution from the crashed state. Treat it like the
                # handler declined (ContinueSearch): restore a sane CPU
                # state and keep walking the chain.
                logger.error(
                    "seh",
                    f"handler at 0x{handler:08x} crashed mid-execution (fault at 0x{e.eip:08x}) "
                    "instead of escaping via RtlUnwind -- treating as declined",
                )
                cpu.faulted = False
                cpu.regs[ESP] = e.esp_before
                frame = next_frame
                continue
            # The expected shape of "handled": the handler called RtlUnwind
            # internally, which already redirected EIP/ESP to the __except
            # block. Nothing left for us to do.
            logger.debug("seh", f"handler at 0x{handler:08x} escaped to 0x{e.eip:08x} (handled via unwind)")
            return True
        except SehHandlerTimeout:
            logger.error("seh", f"handler at 0x{handler:08x} timed out -- treating chain as exhausted")
            return False

        if disposition == EXCEPTION_CONTINUE_EXECUTION:
            _apply_context(memory, context_addr, cpu)
            return True
        if disposition != EXCEPTION_CONTINUE_SEARCH:
            logger.warn("seh", f"handler at 0x{handler:08x} returned unusual disposition {disposition} -- treating as ContinueSearch")

        frame = next_frame

    return False


def register_seh_handlers(stubs: Win32Handlers, memory: "Memory") -> None:
    """Registers real RtlUnwind and RaiseException implementations,
    replacing the previous `_halt` placeholders in kernel32_io.py."""

    def _raise_exception(cpu: "CPU") -> None:
        # RaiseException(DWORD dwExceptionCode, DWORD dwExceptionFlags,
        #                 DWORD nNumberOfArguments, const ULONG_PTR* lpArguments) [stdcall]
        esp = cpu.regs[ESP] & 0xFFFFFFFF
        code       = memory.read32((esp + 4) & 0xFFFFFFFF)
        flags      = memory.read32((esp + 8) & 0xFFFFFFFF)
        n_args     = memory.read32((esp + 12) & 0xFFFFFFFF)
        args_ptr   = memory.read32((esp + 16) & 0xFFFFFFFF)

        n = min(n_args, EXCEPTION_MAXIMUM_PARAMETERS)
        params = tuple(memory.read32((args_ptr + i * 4) & 0xFFFFFFFF) for i in range(n)) if args_ptr else ()

        logger.info("seh", f"RaiseException(code=0x{code:08x}, flags=0x{flags:08x}, nargs={n_args})")

        handled = dispatch_exception(
            cpu, memory, code, cpu.eip & 0xFFFFFFFF,
            parameters=params, noncontinuable=bool(flags & EXCEPTION_NONCONTINUABLE),
        )
        if not handled:
            logger.error("seh", f"RaiseException(0x{code:08x}) unhandled -- halting")
            cpu.halted = True
            cpu.fatal_halt = True
            return
        # A handler resumed execution (ContinueExecution or an unwind
        # escape) -- both already set EIP/ESP correctly; RaiseException
        # itself never "returns" to its stdcall call site in that case,
        # matching real semantics (control resumes wherever the handler
        # sent it, not after the RaiseException call).

    def _rtl_unwind(cpu: "CPU") -> None:
        # RtlUnwind(PVOID TargetFrame, PVOID TargetIp,
        #           PEXCEPTION_RECORD ExceptionRecord, PVOID ReturnValue) [stdcall]
        esp = cpu.regs[ESP] & 0xFFFFFFFF
        target_frame = memory.read32((esp + 4) & 0xFFFFFFFF)
        target_ip    = memory.read32((esp + 8) & 0xFFFFFFFF)
        exc_record   = memory.read32((esp + 12) & 0xFFFFFFFF)
        return_value = memory.read32((esp + 16) & 0xFFFFFFFF)

        logger.debug("seh", f"RtlUnwind(target_frame=0x{target_frame:08x}, target_ip=0x{target_ip:08x})")

        fs_base = cpu.kernel_structures.get_fs_base()
        frame = memory.read32(fs_base + 0x00)
        visited: set[int] = set()

        # The chain head is always the frame whose handler is CURRENTLY
        # EXECUTING and is the one calling RtlUnwind right now (the
        # standard shape: _except_handler3 decides to handle, calls
        # RtlUnwind on its own establisher frame) -- it must be popped
        # like any other unwound frame, but must NOT be invoked again
        # (it's mid-call, not a separate intervening __finally). Confirmed
        # live: without this, a handler calling RtlUnwind on itself
        # recurses into itself infinitely via _invoke_handler.
        if frame not in (0, 0xFFFFFFFF, target_frame):
            head_next = memory.read32(frame + 0x00)
            memory.write32(fs_base + 0x00, head_next)
            frame = head_next

        while frame not in (0, 0xFFFFFFFF, target_frame):
            if frame in visited:
                logger.error("seh", f"RtlUnwind: chain cycle at 0x{frame:08x} -- aborting walk")
                break
            visited.add(frame)

            next_frame = memory.read32(frame + 0x00)
            handler = memory.read32(frame + 0x04)

            if exc_record:
                old_flags = memory.read32(exc_record + 0x04)
                memory.write32(exc_record + 0x04, old_flags | EH_UNWINDING)
            try:
                _invoke_handler(cpu, memory, handler, [exc_record, frame, 0, 0])
            except SehHandlerEscaped as e:
                logger.warn("seh", f"RtlUnwind: handler at 0x{handler:08x} escaped to 0x{e.eip:08x} during unwind (unexpected)")
                # Unlike the main dispatch loop, any escape here is already
                # "unexpected" regardless of e.faulted -- these intervening
                # handlers are only supposed to run __finally-style cleanup
                # and return normally. Restore ESP either way so a crashed
                # (or otherwise redirected) handler doesn't corrupt the rest
                # of this unwind walk.
                cpu.faulted = False
                cpu.regs[ESP] = e.esp_before
            except SehHandlerTimeout:
                logger.error("seh", f"RtlUnwind: handler at 0x{handler:08x} timed out during unwind")
            if exc_record:
                memory.write32(exc_record + 0x04, old_flags)

            memory.write32(fs_base + 0x00, next_frame)  # pop this frame from the chain
            frame = next_frame

        if target_ip:
            # Real RtlUnwind never returns to its caller when TargetIp is
            # set -- it resumes execution there directly, with ESP set as if
            # RtlUnwind had just done an ordinary `RET 16` back to whoever
            # called it (4 stdcall DWORD args). This is NOT target_frame's
            # address -- confirmed live 2026-08-24 via MSVC's
            # __global_unwind2, which calls RtlUnwind with TargetIp = its
            # own return address as a "fake return" trick; using
            # ESP=target_frame there corrupts its epilogue (see module
            # docstring). `esp` here is RtlUnwind's own entry ESP, captured
            # before its args were read, so esp+20 (retaddr + 4 args) is
            # exactly that caller-return depth.
            cpu.regs[EAX] = return_value & 0xFFFFFFFF
            cpu.regs[ESP] = (esp + 20) & 0xFFFFFFFF
            # EBP restoration: only handled for the common case of
            # unwinding back to the same frame the exception originated in
            # (dispatch_exception stashes that (frame, EBP) pairing --
            # see there). A true multi-level unwind (target several call
            # frames further out) would need an EBP-chain walk this module
            # doesn't implement -- log clearly and leave EBP untouched
            # rather than fabricate a value.
            original_frame = getattr(cpu, "_seh_original_frame", None)
            if target_frame == original_frame:
                cpu.regs[EBP] = cpu._seh_original_ebp
            else:
                logger.warn(
                    "seh",
                    f"RtlUnwind: target_frame=0x{target_frame:08x} doesn't match the original "
                    f"exception frame (0x{(original_frame or 0):08x}) -- EBP not restored "
                    "(multi-level unwind recovery not implemented), __except-block code using "
                    "EBP-relative addressing may misbehave",
                )
            cpu.eip = target_ip & 0xFFFFFFFF
        else:
            cpu.regs[EAX] = return_value & 0xFFFFFFFF
            cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("kernel32.dll", "RaiseException", _raise_exception)
    stubs.register_handler("kernel32.dll", "RtlUnwind", _rtl_unwind)
