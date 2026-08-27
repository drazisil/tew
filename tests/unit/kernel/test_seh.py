"""
Tests for tew.kernel.seh -- the 32-bit SEH (Structured Exception Handling)
dispatcher: FS:[0] chain walking, handler invocation via the return-sentinel
mechanism, disposition handling, and the real RtlUnwind implementation.

Exercised against hand-built SEH frames and hand-assembled tiny handler
routines rather than MCity_d.exe's real CRT internals -- reimplementing
_except_handler3's scope-table walking is explicitly NOT this module's job
(see tew/kernel/seh.py's docstring): real handlers are either genuine
compiled code (executed natively, like any other code this emulator runs)
or, where actually intercepted (RtlUnwind), given a real implementation.
These tests verify the DISPATCHER is correct on inputs we fully control.
"""

import pytest
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import ZigCPU, ESP, EAX, EBP
from tew.kernel.kernel_structures import KernelStructures
from tew.api.win32_handlers import Win32Handlers
from tew.logger import set_emit_hook
from tew.kernel.seh import (
    install as seh_install,
    register_seh_handlers,
    dispatch_exception,
)

MEM_SIZE = 0x00400000
STACK_TOP = 0x00040000
CODE_BASE = 0x00050000
FRAME_A = 0x00041000
FRAME_B = 0x00041020


def write_bytes(mem: Memory, addr: int, data: bytes) -> None:
    for i, b in enumerate(data):
        mem.write8(addr + i, b)


def push_seh_frame(mem: Memory, fs_base: int, frame_addr: int, handler_addr: int, next_frame: int) -> None:
    mem.write32(frame_addr + 0x00, next_frame)
    mem.write32(frame_addr + 0x04, handler_addr)
    mem.write32(fs_base + 0x00, frame_addr)


@pytest.fixture
def cpu_env():
    """A CPU with kernel structures + SEH installed, ready to dispatch."""
    mem = Memory(size_bytes=MEM_SIZE)
    cpu = ZigCPU(mem)
    ks = KernelStructures(mem)
    ks.initialize_kernel_structures(stack_base=STACK_TOP, stack_limit=STACK_TOP - 0x10000)
    cpu.kernel_structures = ks
    cpu.regs[ESP] = STACK_TOP - 0x100
    stubs = Win32Handlers(mem)
    seh_install(stubs, mem)
    register_seh_handlers(stubs, mem)
    stubs.install(cpu)
    return cpu, mem, ks, stubs


def test_single_continue_search_handler_is_unhandled(cpu_env):
    cpu, mem, ks, stubs = cpu_env
    handler = CODE_BASE
    write_bytes(mem, handler, bytes([0xB8, 0x01, 0x00, 0x00, 0x00, 0xC3]))  # mov eax,1 (ContinueSearch); ret
    push_seh_frame(mem, ks.get_fs_base(), FRAME_A, handler, 0xFFFFFFFF)

    handled = dispatch_exception(cpu, mem, 0xC0000005, 0x12345678)

    assert handled is False
    assert not cpu.halted
    assert not cpu.faulted


def test_single_continue_execution_handler_is_handled(cpu_env):
    cpu, mem, ks, stubs = cpu_env
    handler = CODE_BASE
    write_bytes(mem, handler, bytes([0xB8, 0x00, 0x00, 0x00, 0x00, 0xC3]))  # mov eax,0 (ContinueExecution); ret
    push_seh_frame(mem, ks.get_fs_base(), FRAME_A, handler, 0xFFFFFFFF)

    handled = dispatch_exception(cpu, mem, 0xC0000005, 0x12345678)

    assert handled is True


def test_chain_walks_past_continue_search_to_continue_execution(cpu_env):
    cpu, mem, ks, stubs = cpu_env
    search_handler = CODE_BASE
    write_bytes(mem, search_handler, bytes([0xB8, 0x01, 0x00, 0x00, 0x00, 0xC3]))
    exec_handler = CODE_BASE + 0x10
    write_bytes(mem, exec_handler, bytes([0xB8, 0x00, 0x00, 0x00, 0x00, 0xC3]))

    fs_base = ks.get_fs_base()
    push_seh_frame(mem, fs_base, FRAME_B, exec_handler, 0xFFFFFFFF)  # outer, installed first
    push_seh_frame(mem, fs_base, FRAME_A, search_handler, FRAME_B)   # inner, chains to outer

    handled = dispatch_exception(cpu, mem, 0xC0000005, 0x12345678)

    assert handled is True


def test_handler_escaping_via_jmp_is_detected_as_handled(cpu_env):
    """A handler that redirects execution (jmp) instead of returning via
    ret -- the shape any handler takes when it internally calls RtlUnwind
    and RtlUnwind takes over. dispatch_exception must recognize this as
    resolved and must NOT silently clear a real halt the redirected code
    produces (regression test for the bug where escape detection
    unconditionally cleared cpu.halted, erasing genuine hlt results)."""
    cpu, mem, ks, stubs = cpu_env
    escape_target = CODE_BASE + 0x100
    escape_handler = CODE_BASE
    write_bytes(mem, escape_handler, bytes([0xB9]) + escape_target.to_bytes(4, "little") + bytes([0xFF, 0xE1]))
    write_bytes(mem, escape_target, bytes([0xB8, 0x2A, 0x00, 0x00, 0x00, 0xF4]))  # mov eax,0x2a; hlt
    push_seh_frame(mem, ks.get_fs_base(), FRAME_A, escape_handler, 0xFFFFFFFF)

    handled = dispatch_exception(cpu, mem, 0xC0000005, 0x12345678)

    assert handled is True
    # The escape target's own code already ran to completion as part of the
    # escape itself -- dispatch_exception doesn't artificially stop at the
    # jump target.
    assert cpu.regs[EAX] == 0x2A
    assert cpu.halted


def test_handler_crashing_mid_execution_is_not_falsely_reported_as_handled(cpu_env):
    """A handler that redirects execution (jmp) into code that itself hits a
    genuine CPU fault -- NOT a clean RtlUnwind escape, even though it
    produces the identical shape (halted at some EIP other than the
    sentinel). Regression test for the bug where dispatch_exception assumed
    every escape was a clean unwind and reported "handled" for a handler
    that actually crashed, causing the caller to resume from corrupted
    state."""
    cpu, mem, ks, stubs = cpu_env
    crash_target = CODE_BASE + 0x100
    escape_handler = CODE_BASE
    write_bytes(mem, escape_handler, bytes([0xB9]) + crash_target.to_bytes(4, "little") + bytes([0xFF, 0xE1]))
    # mov al, [0x00500000] -- well past this fixture's MEM_SIZE (0x00400000),
    # so this genuinely faults via the ordinary out-of-bounds check.
    write_bytes(mem, crash_target, bytes([0xA0]) + (0x00500000).to_bytes(4, "little"))
    push_seh_frame(mem, ks.get_fs_base(), FRAME_A, escape_handler, 0xFFFFFFFF)

    esp_before = cpu.regs[ESP]
    handled = dispatch_exception(cpu, mem, 0xC0000005, 0x12345678)

    assert handled is False
    assert not cpu.faulted
    assert cpu.regs[ESP] == esp_before


def test_chain_continues_past_a_crashed_handler_to_a_working_outer_one(cpu_env):
    """The inner handler crashes the same way as the test above; the outer
    (next) frame's handler cleanly returns ContinueExecution. The crash must
    be treated like a decline (ContinueSearch), not a dead end -- the chain
    walk should still reach and honor the outer handler."""
    cpu, mem, ks, stubs = cpu_env
    fs_base = ks.get_fs_base()

    exec_handler = CODE_BASE + 0x10
    write_bytes(mem, exec_handler, bytes([0xB8, 0x00, 0x00, 0x00, 0x00, 0xC3]))  # mov eax,0 (ContinueExecution); ret
    push_seh_frame(mem, fs_base, FRAME_B, exec_handler, 0xFFFFFFFF)  # outer, installed first

    crash_target = CODE_BASE + 0x100
    crash_handler = CODE_BASE
    write_bytes(mem, crash_handler, bytes([0xB9]) + crash_target.to_bytes(4, "little") + bytes([0xFF, 0xE1]))
    write_bytes(mem, crash_target, bytes([0xA0]) + (0x00500000).to_bytes(4, "little"))
    push_seh_frame(mem, fs_base, FRAME_A, crash_handler, FRAME_B)  # inner, chains to outer

    handled = dispatch_exception(cpu, mem, 0xC0000005, 0x12345678)

    assert handled is True


def test_rtlunwind_restores_ebp_when_target_frame_matches_the_original_exception_frame(cpu_env):
    """Regression test for the anti-debug-self-test crash: a handler that
    reassigns EBP mid-execution (as __except_handler3's own scope-table-walk
    code does live, e.g. `lea ebp,[ebx+0x10]`), then calls RtlUnwind
    targeting the SAME frame the exception originated in -- the resumed
    __except-block code needs the ORIGINAL EBP back, not whatever the
    handler's own internal computation left it as."""
    cpu, mem, ks, stubs = cpu_env
    fs_base = ks.get_fs_base()

    original_ebp = 0x11223344
    cpu.regs[EBP] = original_ebp

    rtlunwind_addr = stubs.get_handler_address("kernel32.dll", "RtlUnwind")
    recovered_label = CODE_BASE + 0x200
    write_bytes(mem, recovered_label, bytes([0xB8, 0x77, 0x00, 0x00, 0x00, 0xF4]))  # mov eax,0x77; hlt

    handler = CODE_BASE
    code = b""
    code += bytes([0xBD]) + (0xDEADBEEF).to_bytes(4, "little")   # mov ebp, 0xDEADBEEF (garbage)
    code += bytes([0x68]) + (0x99).to_bytes(4, "little")          # push 0x99 (ReturnValue)
    code += bytes([0x6A, 0x00])                                    # push 0 (ExceptionRecord=NULL)
    code += bytes([0x68]) + recovered_label.to_bytes(4, "little")  # push recovered_label (TargetIp)
    code += bytes([0x68]) + FRAME_A.to_bytes(4, "little")          # push FRAME_A (TargetFrame == SAME frame)
    code += bytes([0xB9]) + rtlunwind_addr.to_bytes(4, "little")   # mov ecx, rtlunwind_addr
    code += bytes([0xFF, 0xD1])                                    # call ecx
    code += bytes([0xB8, 0x01, 0x00, 0x00, 0x00, 0xC3])            # (unreached)
    write_bytes(mem, handler, code)
    push_seh_frame(mem, fs_base, FRAME_A, handler, 0xFFFFFFFF)

    handled = dispatch_exception(cpu, mem, 0xC0000005, 0x12345678)

    assert handled is True
    assert (cpu.regs[EBP] & 0xFFFFFFFF) == original_ebp  # restored, not 0xDEADBEEF


def test_rtlunwind_leaves_ebp_alone_and_warns_when_target_frame_is_a_different_frame(cpu_env):
    """The unsupported (not-yet-implemented) case: RtlUnwind's target is a
    DIFFERENT frame than the one the exception originated in (a genuine
    multi-level unwind). Must not guess a value for EBP -- leave it exactly
    as the handler's own execution left it, and log a clear warning rather
    than silently doing nothing."""
    cpu, mem, ks, stubs = cpu_env
    fs_base = ks.get_fs_base()

    outer_handler = CODE_BASE + 0x10
    write_bytes(mem, outer_handler, bytes([0xB8, 0x01, 0x00, 0x00, 0x00, 0xC3]))
    push_seh_frame(mem, fs_base, FRAME_B, outer_handler, 0xFFFFFFFF)

    rtlunwind_addr = stubs.get_handler_address("kernel32.dll", "RtlUnwind")
    recovered_label = CODE_BASE + 0x200
    write_bytes(mem, recovered_label, bytes([0xB8, 0x77, 0x00, 0x00, 0x00, 0xF4]))  # mov eax,0x77; hlt

    handler = CODE_BASE
    code = b""
    code += bytes([0xBD]) + (0xDEADBEEF).to_bytes(4, "little")   # mov ebp, 0xDEADBEEF (garbage)
    code += bytes([0x68]) + (0x99).to_bytes(4, "little")          # push 0x99 (ReturnValue)
    code += bytes([0x6A, 0x00])                                    # push 0 (ExceptionRecord=NULL)
    code += bytes([0x68]) + recovered_label.to_bytes(4, "little")  # push recovered_label (TargetIp)
    code += bytes([0x68]) + FRAME_B.to_bytes(4, "little")          # push FRAME_B (TargetFrame != original frame FRAME_A)
    code += bytes([0xB9]) + rtlunwind_addr.to_bytes(4, "little")   # mov ecx, rtlunwind_addr
    code += bytes([0xFF, 0xD1])                                    # call ecx
    code += bytes([0xB8, 0x01, 0x00, 0x00, 0x00, 0xC3])            # (unreached)
    write_bytes(mem, handler, code)
    push_seh_frame(mem, fs_base, FRAME_A, handler, FRAME_B)

    lines: list[tuple[int, str]] = []
    set_emit_hook(lambda level, line: lines.append((level, line)))
    try:
        handled = dispatch_exception(cpu, mem, 0xC0000005, 0x12345678)
    finally:
        set_emit_hook(None)

    assert handled is True
    assert (cpu.regs[EBP] & 0xFFFFFFFF) == 0xDEADBEEF  # left untouched, not guessed
    assert any("EBP not restored" in line for _, line in lines)


def test_rtlunwind_pops_current_frame_without_reinvoking_it(cpu_env):
    """Regression test for the infinite-recursion bug: RtlUnwind's
    intervening-handler walk must not re-invoke the frame whose handler is
    currently calling RtlUnwind (the standard shape) -- doing so recurses
    into that same handler forever."""
    cpu, mem, ks, stubs = cpu_env
    fs_base = ks.get_fs_base()
    initial_esp = cpu.regs[ESP] & 0xFFFFFFFF

    outer_handler = CODE_BASE + 0x10
    write_bytes(mem, outer_handler, bytes([0xB8, 0x01, 0x00, 0x00, 0x00, 0xC3]))
    push_seh_frame(mem, fs_base, FRAME_B, outer_handler, 0xFFFFFFFF)

    rtlunwind_addr = stubs.get_handler_address("kernel32.dll", "RtlUnwind")
    recovered_label = CODE_BASE + 0x200
    write_bytes(mem, recovered_label, bytes([0xB8, 0x77, 0x00, 0x00, 0x00, 0xF4]))  # mov eax,0x77; hlt

    inner_handler = CODE_BASE
    code = b""
    code += bytes([0x68]) + (0x99).to_bytes(4, "little")           # push 0x99 (ReturnValue)
    code += bytes([0x6A, 0x00])                                     # push 0 (ExceptionRecord=NULL)
    code += bytes([0x68]) + recovered_label.to_bytes(4, "little")   # push recovered_label (TargetIp)
    code += bytes([0x68]) + FRAME_B.to_bytes(4, "little")           # push FRAME_B (TargetFrame)
    code += bytes([0xB9]) + rtlunwind_addr.to_bytes(4, "little")    # mov ecx, rtlunwind_addr
    code += bytes([0xFF, 0xD1])                                     # call ecx
    code += bytes([0xB8, 0x01, 0x00, 0x00, 0x00, 0xC3])             # (unreached)
    write_bytes(mem, inner_handler, code)
    push_seh_frame(mem, fs_base, FRAME_A, inner_handler, FRAME_B)

    assert mem.read32(fs_base) == FRAME_A

    handled = dispatch_exception(cpu, mem, 0xC0000005, 0x12345678)

    assert handled is True
    assert mem.read32(fs_base) == FRAME_B  # chain correctly popped past FRAME_A
    # ESP resumes at the caller's own stack depth (as if RtlUnwind had just
    # done an ordinary `RET 16`), not at FRAME_B's address -- see
    # test_rtlunwind_resumes_at_callers_stack_depth_when_target_ip_is_its_own_return_address
    # for why target_frame's address is the wrong value here. -20 accounts
    # for _invoke_handler's own 4-arg+sentinel setup (20 bytes) followed by
    # inner_handler's own 4 pushes + `call ecx`'s return-address push (20
    # bytes) minus the fix's +20 restoration -- net -20 from initial_esp.
    assert (cpu.regs[ESP] & 0xFFFFFFFF) == (initial_esp - 20) & 0xFFFFFFFF
    # recovered_label's own code ran to completion as part of the redirect.
    assert cpu.regs[EAX] == 0x77
    assert cpu.halted


def test_rtlunwind_resumes_at_callers_stack_depth_when_target_ip_is_its_own_return_address(cpu_env):
    """Regression test for the __global_unwind2 self-return trick: real
    MSVC CRT code calls RtlUnwind(TargetFrame, TargetIp=<its own return
    address>, ...) purely to simulate a normal function return, so its own
    ordinary epilogue can run afterward. ESP must resume at the CALLER's
    real stack depth (as if RtlUnwind had done `RET 16`), not at
    TargetFrame's address -- TargetFrame is just the SEH registration
    record's location, unrelated to the caller's actual stack depth.
    Confirmed live 2026-08-24 against MCity_d.exe's real, compiled
    __global_unwind2: using ESP=target_frame there made its epilogue pop the
    registration record's own next/handler/scopetable fields as if they
    were its saved EDI/ESI/EBX, eventually RETurning through unrelated
    leftover stack content back into __except_handler3 a second, bogus
    time -- the actual root cause of a fault that looked, from the outside,
    like unrelated EBP/stack corruption."""
    cpu, mem, ks, stubs = cpu_env
    fs_base = ks.get_fs_base()
    initial_esp = cpu.regs[ESP] & 0xFFFFFFFF

    rtlunwind_addr = stubs.get_handler_address("kernel32.dll", "RtlUnwind")

    handler = CODE_BASE
    # TargetIp = the address right after `call ecx` -- i.e. handler's own
    # return address, exactly the __global_unwind2 self-return shape.
    prefix_len = 5 + 2 + 5 + 5 + 5 + 2  # push,push,push,push,mov ecx,call ecx
    target_ip = handler + prefix_len

    code = b""
    code += bytes([0x68]) + (0x99).to_bytes(4, "little")          # push 0x99 (ReturnValue)
    code += bytes([0x6A, 0x00])                                    # push 0 (ExceptionRecord=NULL)
    code += bytes([0x68]) + target_ip.to_bytes(4, "little")        # push target_ip (TargetIp = own return address)
    code += bytes([0x68]) + FRAME_A.to_bytes(4, "little")          # push FRAME_A (TargetFrame == originating frame)
    code += bytes([0xB9]) + rtlunwind_addr.to_bytes(4, "little")   # mov ecx, rtlunwind_addr
    code += bytes([0xFF, 0xD1])                                    # call ecx
    assert len(code) == prefix_len
    code += bytes([0xB8, 0x77, 0x00, 0x00, 0x00, 0xF4])            # mov eax,0x77; hlt -- lands here on resume
    write_bytes(mem, handler, code)
    push_seh_frame(mem, fs_base, FRAME_A, handler, 0xFFFFFFFF)

    handled = dispatch_exception(cpu, mem, 0xC0000005, 0x12345678)

    assert handled is True
    assert cpu.regs[EAX] == 0x77  # the marker right after `call ecx` actually ran
    assert cpu.halted
    # ESP lands where an ordinary `RET 16` back to the caller would leave
    # it -- NOT at FRAME_A's address (the old, wrong behavior this fix
    # removes). -20 for the same reason as
    # test_rtlunwind_pops_current_frame_without_reinvoking_it above.
    final_esp = cpu.regs[ESP] & 0xFFFFFFFF
    assert final_esp == (initial_esp - 20) & 0xFFFFFFFF
    assert final_esp != FRAME_A


def test_dispatch_exception_returns_immediately_when_handler_resumes_far_from_itself(cpu_env):
    """Regression test for the anti-debug-self-test hang that survived the
    RtlUnwind resume-ESP fix: once a handler's RtlUnwind-driven redirect
    resumes execution millions of bytes away from the handler's own code
    (the real __except block, confirmed live 2026-08-24 against
    MCity_d.exe's own __except_handler3 -> real __except body jump -- a
    plain JMP baked into the compiler's scope table, not a second RtlUnwind
    call), that's ordinary, effectively unbounded program continuation -- it
    was never going to return to our sentinel or hit a halt on its own (real
    MSVC __except blocks fall straight through into the rest of the
    enclosing function). An earlier version of this fix tried detecting this
    via ESP reaching the original exception's own SEH frame address; live
    tracing proved that assumption wrong (ESP never got anywhere near it,
    even mid-escape) -- EIP distance from handler_addr is what actually
    distinguishes a handler's own bounded logic (clustered within ~0x10000
    bytes, confirmed live) from a genuine escape into unrelated code.
    Without detecting this proactively, _invoke_handler holds the CPU
    hostage for the full 2,000,000-step _STEP_LIMIT even though nothing is
    actually stuck, which then starves run_exe.py's own outer-loop timer/
    thread-scheduling machinery (already proven to work correctly on its
    own) for that whole duration. This test proves detection happens
    immediately (well within one _STEP_BATCH), not just eventually via the
    step-limit fallback: the resumed code jumps to itself forever (`jmp $`)
    and would otherwise burn through the entire 2,000,000-step budget before
    dispatch_exception ever returned."""
    cpu, mem, ks, stubs = cpu_env
    fs_base = ks.get_fs_base()

    rtlunwind_addr = stubs.get_handler_address("kernel32.dll", "RtlUnwind")

    # target_ip: comfortably more than _ESCAPE_EIP_DISTANCE (2MB) away from
    # `handler` below, well within this fixture's MEM_SIZE (4MB) -- then
    # spins forever. If _invoke_handler doesn't detect this proactively, it
    # never returns.
    handler = CODE_BASE
    resume_target = handler + 0x300000
    resume_code = bytes([0xEB, 0xFE])  # jmp $ (infinite self-loop)
    write_bytes(mem, resume_target, resume_code)

    code = b""
    code += bytes([0x68]) + (0x99).to_bytes(4, "little")          # push 0x99 (ReturnValue)
    code += bytes([0x6A, 0x00])                                    # push 0 (ExceptionRecord=NULL)
    code += bytes([0x68]) + resume_target.to_bytes(4, "little")    # push resume_target (TargetIp)
    code += bytes([0x68]) + FRAME_A.to_bytes(4, "little")          # push FRAME_A (TargetFrame == originating frame)
    code += bytes([0xB9]) + rtlunwind_addr.to_bytes(4, "little")   # mov ecx, rtlunwind_addr
    code += bytes([0xFF, 0xD1])                                    # call ecx
    code += bytes([0xB8, 0x01, 0x00, 0x00, 0x00, 0xC3])            # (unreached)
    write_bytes(mem, handler, code)
    push_seh_frame(mem, fs_base, FRAME_A, handler, 0xFFFFFFFF)

    handled = dispatch_exception(cpu, mem, 0xC0000005, 0x12345678)

    # Only reachable quickly if the proactive EIP-distance check fired --
    # without it, this would run the full _STEP_LIMIT (raising
    # SehHandlerTimeout internally, caught by dispatch_exception as
    # handled=False) before ever getting here.
    assert handled is True
    # Execution is genuinely still sitting in the infinite loop -- proof
    # dispatch_exception returned control without waiting for it to halt
    # or return, exactly like a real __except block that never comes back.
    assert (cpu.eip & 0xFFFFFFFF) == resume_target  # the `jmp $` instruction itself
    assert not cpu.halted
