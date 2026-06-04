"""Patch CRT internal functions at hardcoded game addresses.

Ported from Win32Handlers.ts patchCRTInternals (lines 6641–6773).

Must be called AFTER sections are loaded into memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu import EAX, ESP, EBP, ZF_BIT
from tew.api.win32_handlers import (
    Win32Handlers,
    DIALOG_TRAMPOLINE,
    DLLMAIN_TRAMPOLINE,
    DLLMAIN_HANDLE_STORE,
)
from tew.api._state import CRTState, read_cstring
from tew.logger import logger


def patch_crt_internals(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Patch CRT internal functions at hardcoded game addresses."""

    # DIALOG_TRAMPOLINE: used by DialogBoxParamA login dialog invocation.
    # After the dialog proc returns (RET 16 pops 4 args), EIP lands here.
    # We set EAX=1 (IDOK) and RET — which pops the original DialogBoxParamA
    # return address (placed there by our stack manipulation) — returning to
    # the game's call site with the login result.
    def _dialog_finish_idok(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1  # IDOK — dialog proc ran, credentials were read
        # Dialog proc uses RET (cdecl, no arg cleanup), so the 4 args (hwnd, msg,
        # wParam, lParam) remain on the stack. Skip them so [ESP] = retAddr.
        cpu.regs[ESP] = (cpu.regs[ESP] + 16) & 0xFFFFFFFF
        # Now [ESP] = original retAddr of DialogBoxParamA call.
        # The RET at stub+2 pops it and returns to the game.

    stubs.patch_address(DIALOG_TRAMPOLINE, "_dialogFinishIdok", _dialog_finish_idok)

    # DLLMAIN_TRAMPOLINE: return address placed on stack when LoadLibraryA invokes
    # DllMain(hModule, DLL_PROCESS_ATTACH, 0) via the stack trick.
    # After DllMain does RET 12, EIP lands here. We restore EAX = hModule
    # (the correct LoadLibraryA return value) then RET back to the original caller.
    def _dll_main_finish(cpu: "CPU") -> None:
        cpu.regs[EAX] = memory.read32(DLLMAIN_HANDLE_STORE)
        # Stack at this point: [ESP] = original retAddr of LoadLibraryA call site.
        # The RET at stub+2 pops it and returns to the game.

    stubs.patch_address(DLLMAIN_TRAMPOLINE, "_dllMainFinish", _dll_main_finish)

    # WinMain check 1 (0x68a402 CALL 0x40d1d4; 0x68a407 TEST EAX,EAX; JNZ pass)
    # No-arg cdecl function; must return non-zero (any non-zero = pass).
    def _winmain_check1(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x12345678  # non-zero — cdecl no args, caller has no ADD ESP

    stubs.patch_address(0x0040D1D4, "_winmain_check1", _winmain_check1)

    # WinMain check 2 (0x68a432 CALL 0x40159b(buf, len); 0x68a43a TEST EAX,EAX; JNZ pass)
    # cdecl 2 args: [ESP+4]=buf_ptr, [ESP+8]=max_len (31).
    # Must return non-zero AND write a parseable version string to the buffer so that
    # the following _sscanf(buf, "%u, %u, %u", ...) returns 3.
    def _winmain_check2(cpu: "CPU") -> None:
        buf_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        s = "1, 2, 3"
        for i, ch in enumerate(s):
            memory.write8((buf_ptr + i) & 0xFFFFFFFF, ord(ch))
        memory.write8((buf_ptr + len(s)) & 0xFFFFFFFF, 0)  # null terminator
        cpu.regs[EAX] = 1  # non-zero = success; cdecl, caller does ADD ESP, 8

    stubs.patch_address(0x0040159B, "_winmain_check2_GetVersionString", _winmain_check2)

    # WinMain check 3: unnamed init fn at 0x8ed560 (via thunk 0x409cfa, called from 0x68a536).
    # No args, cdecl, returns 0 = failure. Patch to return 1 so WinMain proceeds.
    def _winmain_check3(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1  # non-zero = success; cdecl no-args, caller has no ADD ESP

    stubs.patch_address(0x008ED560, "_winmain_check3_init", _winmain_check3)

    # __chkesp (0x009f1bc0): called after every function call in MSVC debug builds to
    # verify ESP was properly restored. If ESP is wrong it calls _CrtDbgReport then INT3.
    # Caller emits: CMP EBP, ESP; CALL __chkesp at end of each function epilog.
    # ZF=1 if EBP==ESP (frame balanced) → return transparently.
    # ZF=0 if mismatch → halt with diagnostic.
    def _chkesp(cpu: "CPU") -> None:
        if not cpu.get_flag(ZF_BIT):
            ret_addr    = memory.read32(cpu.regs[ESP])
            ebp         = cpu.regs[EBP] & 0xFFFFFFFF
            esp_at_cmp  = (cpu.regs[ESP] + 4) & 0xFFFFFFFF  # CALL pushed retAddr, so add 4
            logger.error(
                "exception",
                f"__chkesp FAILED at return to 0x{(ret_addr & 0xFFFFFFFF):08x}"
                f" — EBP=0x{ebp:08x}"
                f" ESP=0x{esp_at_cmp:08x}"
                f" delta={esp_at_cmp - ebp}",
            )
            cpu.halted = True
        # On pass: cdecl no args, EAX preserved (caller continues using it), plain RET

    stubs.patch_address(0x009F1BC0, "__chkesp", _chkesp)

    # _CrtDbgReport (0x009f9300): called by MSVC debug CRT assertions (_ASSERTE, _ASSERT etc.)
    # Signature: __cdecl _CrtDbgReport(int reportType, const char *filename, int linenumber,
    #            const char *moduleName, const char *format, ...)
    # reportType: 0=_CRT_WARN, 1=_CRT_ERROR, 2=_CRT_ASSERT
    # We halt loudly so assertions are never silently swallowed.
    def _crt_dbg_report(cpu: "CPU") -> None:
        sp          = cpu.regs[ESP]
        report_type = memory.read32((sp + 4)  & 0xFFFFFFFF)
        filename_ptr = memory.read32((sp + 8)  & 0xFFFFFFFF)
        line_number  = memory.read32((sp + 12) & 0xFFFFFFFF)
        # moduleName at sp+16, format at sp+20
        format_ptr   = memory.read32((sp + 20) & 0xFFFFFFFF)

        type_names = ["_CRT_WARN", "_CRT_ERROR", "_CRT_ASSERT"]
        type_name  = type_names[report_type] if report_type < len(type_names) else f"type={report_type}"

        filename = "(null)"
        if filename_ptr > 0x1000:
            try:
                filename = read_cstring(filename_ptr, memory)
            except Exception as e:
                logger.debug("exception", f"_CrtDbgReport: read_cstring(filename) failed: {e}")

        fmt = "(null)"
        if format_ptr > 0x1000:
            try:
                fmt = read_cstring(format_ptr, memory)
            except Exception as e:
                logger.debug("exception", f"_CrtDbgReport: read_cstring(fmt) failed: {e}")

        logger.error(
            "exception",
            f"_CrtDbgReport [{type_name}] {filename}:{line_number} — {fmt}",
        )
        cpu.halted = True
        cpu.regs[EAX] = 1  # retry = __debugbreak (moot since we halted)
        # cdecl variadic — no stack cleanup by callee

    stubs.patch_address(0x009F9300, "_CrtDbgReport", _crt_dbg_report)

    # abortmessage (0x00a30140): game's own assert/abort handler.
    # Called with _REALabortfilename/_REALabortlinenum already set by the caller.
    # Signature: __cdecl abortmessage(const char *fmt, ...)
    # Without patching this reaches MessageBoxA then INT3 — log and halt instead.
    def _abort_message(cpu: "CPU") -> None:
        sp      = cpu.regs[ESP]
        fmt_ptr = memory.read32((sp + 4) & 0xFFFFFFFF)

        fmt = "(null)"
        if fmt_ptr > 0x1000:
            try:
                fmt = read_cstring(fmt_ptr, memory)
            except Exception as e:
                logger.debug("exception", f"abortmessage: read_cstring(fmt) failed: {e}")

        # _REALabortfilename (0x020d84b4) and _REALabortlinenum (0x020d84b8) set by caller
        filename_ptr = memory.read32(0x020D84B4)
        line_num     = memory.read32(0x020D84B8)
        filename = "(none)"
        if filename_ptr > 0x1000:
            try:
                filename = read_cstring(filename_ptr, memory)
            except Exception as e:
                logger.debug("exception", f"abortmessage: read_cstring(filename) failed: {e}")

        # Format variadic args: handle %s (pointer) and %d/%i (int) substitutions
        if '%' in fmt:
            args_base = (sp + 8) & 0xFFFFFFFF  # first variadic arg after fmt_ptr
            parts = fmt.split('%')
            out = parts[0]
            for i, part in enumerate(parts[1:]):
                arg_ptr = memory.read32((args_base + i * 4) & 0xFFFFFFFF)
                if part.startswith('s'):
                    val = "(null)"
                    if arg_ptr > 0x1000:
                        try:
                            val = read_cstring(arg_ptr, memory)
                        except Exception:
                            val = f"<bad ptr {arg_ptr:#010x}>"
                    out += val + part[1:]
                elif part.startswith(('d', 'i')):
                    out += str(arg_ptr) + part[1:]  # arg_ptr holds the int value
                else:
                    out += '%' + part  # unknown spec, pass through
            fmt = out

        logger.error("exception", f"abortmessage: {filename}:{line_num} — {fmt}")
        cpu.halted = True
        # cdecl variadic — no stack cleanup by callee

    stubs.patch_address(0x00A30140, "abortmessage", _abort_message)

    # __free_dbg (0x009f6e20): internal MSVC debug CRT free, called by __freeptd and
    # other CRT internals. Validates an MSVC debug block header (_BLOCK_TYPE_IS_VALID)
    # before the pointer — our bump allocator never writes those headers, so any call
    # would assert. No-op matches our existing free() IAT handler behavior.
    # __cdecl (void*, int) — caller cleans args.
    def _free_dbg_noop(cpu: "CPU") -> None:
        pass

    stubs.patch_address(0x009F6E20, "__free_dbg", _free_dbg_noop)

    # SNDMEMI_init (FUN_00a5422a) — zero-fill pool + log
    #
    # The game's pool allocator (FUN_00a70610) does not guarantee the returned
    # block is zero-filled.  SNDMEMI_init never writes struct[5] (entry count),
    # and the sentinel-validator FUN_00a54107 dereferences struct[1][0] (the
    # first block-entry's start offset) as an offset into pool_base.  If that
    # offset is garbage (uninitialised pool memory), the read faults.
    #
    # Fix: intercept SNDMEMI_init, zero-fill the pool buffer, then replay the
    # six field writes that the original asm does.  __cdecl, caller cleans args.
    #
    # Original (FUN_00a5422a at 0xa5422a):
    #   DAT_020def78    = param_1
    #   param_1[2]      = param_2
    #   param_1[1]      = param_1 + param_2 - 0x18   (block-list ptr)
    #   param_1[0]      = (param_1 + 40) aligned-4   (pool base)
    #   param_1[3]      = param_2 - 0x43             (available size)
    #   param_1[4]      = param_2 - 3                (min alloc size)
    #   param_1[5]      = NOT SET  ← we now set it to 0 explicitly
    SNDMEMI_STRUCT_PTR = 0x020def78  # DAT_020def78
    SNDMEMI_INIT_ADDR  = 0x00a5422a

    def _sndmemi_init(cpu: "CPU") -> None:
        param_1 = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        param_2 = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        # Zero-fill the entire pool struct so all fields (including [5]) start clean
        for off in range(0, param_2, 4):
            memory.write32((param_1 + off) & 0xFFFFFFFF, 0)
        # Replay the six field assignments from the original asm
        memory.write32(SNDMEMI_STRUCT_PTR, param_1)            # DAT_020def78 = param_1
        pool_base = ((param_1 + 40) + 3) & ~3                  # align to 4
        memory.write32(param_1,           pool_base)           # param_1[0] = pool_base
        memory.write32(param_1 + 4,       (param_1 + param_2 - 0x18) & 0xFFFFFFFF)  # [1]
        memory.write32(param_1 + 8,       param_2)             # param_1[2] = size
        memory.write32(param_1 + 12,      (param_2 - 0x43) & 0xFFFFFFFF)  # [3] available
        memory.write32(param_1 + 16,      (param_2 - 3) & 0xFFFFFFFF)     # [4] min
        memory.write32(param_1 + 20,      0)                   # [5] entry count = 0
        blist = (param_1 + param_2 - 0x18) & 0xFFFFFFFF
        watch = blist + 7  # MSB of size field — any write here is corrupt (pool < 0x64000)
        logger.info("handlers",
            f"[SNDMEMI_init] pool={param_1:#010x} size={param_2:#x} "
            f"base={pool_base:#010x} blist={blist:#010x} watching {watch:#010x} (size MSB)")

        # Install a Zig-level watchpoint: halt the CPU and log when anything
        # writes the first byte of the size field of block-entry 0.
        cpu.set_watchpoint(watch)

        # cdecl: caller cleans args; just return (RET pops saved ret addr)

    stubs.patch_address(SNDMEMI_INIT_ADDR, "SNDMEMI_init", _sndmemi_init)

    # SNDMEMI_validator intercept — log sentinel check state
    #
    # FUN_00a54107 (0xa54107): iterates existing allocs and checks sentinel
    # values (0xDEADDEAD) at each end.  If it reads from a bad address, a
    # prior alloc recorded a garbage block size in the block table.
    # We replace the entire function with a Python version that logs each
    # check so we can find which block entry is corrupt.
    #
    # Original: __cdecl, no args, no return value.
    from tew.hardware.cpu_zig import EBP as _EBP

    def _sndmemi_validate(cpu: "CPU") -> None:
        pool_ptr = memory.read32(SNDMEMI_STRUCT_PTR & 0xFFFFFFFF)
        if not pool_ptr:
            return
        pool_base   = memory.read32(pool_ptr & 0xFFFFFFFF)
        blist_ptr   = memory.read32((pool_ptr + 4) & 0xFFFFFFFF)
        entry_count = memory.read32((pool_ptr + 20) & 0xFFFFFFFF)  # [5], negative
        n = (-entry_count) & 0x7FFFFFFF
        if n > 1024:
            logger.warn("handlers", f"[SNDMEMI_validate] insane count={entry_count:#x}")
            return
        # Log count=0 allocs too — these are the "first alloc" path and may be
        # the corrupt writer (count=0 writes entry 0 at blist_ptr with whatever uVar2).
        if n == 0:
            caller_ebp = cpu.regs[_EBP] & 0xFFFFFFFF
            try:
                param1 = memory.read32((caller_ebp + 8) & 0xFFFFFFFF) & 0xFFFFFFFF
            except Exception:
                param1 = 0
            logger.info("handlers",
                f"[SNDMEMI_validate] [count=0] first alloc, param1={param1:#010x}")
        any_bad = False
        for idx in range(n):
            entry = blist_ptr - idx * 0x18
            start  = memory.read32(entry & 0xFFFFFFFF)
            size   = memory.read32((entry + 4) & 0xFFFFFFFF)
            lo_addr = (pool_base + start) & 0xFFFFFFFF
            hi_addr = (pool_base + start + size - 4) & 0xFFFFFFFF
            lo_val = memory.read32(lo_addr) if lo_addr < 0x7FFFFFFF else 0
            hi_val = memory.read32(hi_addr) if hi_addr < 0x7FFFFFFF else 0
            ok = "✓" if (lo_val == 0xDEADDEAD and hi_val == 0xDEADDEAD) else "✗"
            logger.info("handlers",
                f"[SNDMEMI_validate] [{ok}] idx={idx} entry={entry:#010x} "
                f"start={start:#x} size={size:#x} "
                f"lo={lo_addr:#010x}={lo_val:#010x} hi={hi_addr:#010x}={hi_val:#010x}")
            if ok == "✗":
                any_bad = True
        # When corruption is detected, the validator is called BY the SNDMEMI_alloc
        # that made the bad write.  Walk the EBP chain to find who passed the giant size.
        if any_bad:
            logger.warn("handlers", "[SNDMEMI_validate] CORRUPTION detected — call stack:")
            ebp = cpu.regs[_EBP] & 0xFFFFFFFF
            for depth in range(16):
                if not (0x07000000 <= ebp <= 0x7FFFFFFF):
                    break
                try:
                    saved_ebp = memory.read32(ebp) & 0xFFFFFFFF
                    ret_addr  = memory.read32(ebp + 4) & 0xFFFFFFFF
                    param1    = memory.read32(ebp + 8) & 0xFFFFFFFF  # first arg
                except Exception:
                    break
                logger.warn("handlers",
                    f"  [{depth}] EBP={ebp:#010x}  ret={ret_addr:#010x}  arg0={param1:#010x}")
                ebp = saved_ebp

    stubs.patch_address(0x00a54107, "SNDMEMI_validate", _sndmemi_validate)


