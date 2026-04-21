"""kernel32.dll locale and string conversion handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers
    from tew.api._state import CRTState

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import cleanup_stdcall
from tew.api.char_type import GetStringTypeArgs, classify_wide_string
from tew.api.lc_map import LCMapStringArgs, lc_map_wide_string
from tew.logger import logger


def register_kernel32_locale_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register code page, locale, and string conversion handlers."""

    def _halt(name: str):
        def _h(cpu: "CPU") -> None:
            logger.error("handlers", f"[UNIMPLEMENTED] {name} — halting")
            cpu.halted = True
        return _h

    # ── Code pages ────────────────────────────────────────────────────────────

    def _get_acp(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1252

    def _get_cp_info(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        memory.write32(lp, 1)
        memory.write8(lp + 4, 0x3F)  # '?'
        memory.write8(lp + 5, 0)
        for i in range(12):
            memory.write8(lp + 6 + i, 0)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _is_valid_code_page(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GetACP",           _get_acp)
    stubs.register_handler("kernel32.dll", "GetCPInfo",        _get_cp_info)
    stubs.register_handler("kernel32.dll", "IsValidCodePage",  _is_valid_code_page)

    # ── String conversion ─────────────────────────────────────────────────────

    def _multi_byte_to_wide(cpu: "CPU") -> None:
        lp_mb  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cb_mb  = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_wc  = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        cch_wc = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        if cch_wc == 0:
            cpu.regs[EAX] = cb_mb
        else:
            count = min(cb_mb, cch_wc)
            for i in range(count):
                memory.write16(lp_wc + i * 2, memory.read8(lp_mb + i))
            cpu.regs[EAX] = count
        cleanup_stdcall(cpu, memory, 24)

    def _wide_to_multi_byte(cpu: "CPU") -> None:
        lp_wc  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cch_wc = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_mb  = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        cb_mb  = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        if cb_mb == 0:
            cpu.regs[EAX] = cch_wc
        else:
            count = min(cch_wc, cb_mb)
            for i in range(count):
                wc = memory.read16(lp_wc + i * 2)
                memory.write8(lp_mb + i, wc if wc <= 255 else 0x3F)
            cpu.regs[EAX] = count
        cleanup_stdcall(cpu, memory, 32)

    def _get_string_type_w(cpu: "CPU") -> None:
        args = GetStringTypeArgs(
            info_type = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF),
            src_ptr   = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF),
            cch_src   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF),
            out_ptr   = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF),
        )
        if not classify_wide_string(memory, args):
            logger.error("handlers",
                f"GetStringTypeW: unsupported dwInfoType {args.info_type:#010x} — halting")
            cpu.halted = True
            return
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 16)

    def _lc_map_string_w(cpu: "CPU") -> None:
        args = LCMapStringArgs(
            locale    = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF),
            map_flags = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF),
            src_ptr   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF),
            cch_src   = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF),
            dest_ptr  = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF),
            cch_dest  = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF),
        )
        result = lc_map_wide_string(memory, args)
        if result is None:
            logger.error("handlers",
                f"LCMapStringW: unsupported dwMapFlags {args.map_flags:#010x} — halting")
            cpu.halted = True
            return
        cpu.regs[EAX] = result
        cleanup_stdcall(cpu, memory, 24)

    def _get_locale_info_a(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("kernel32.dll", "MultiByteToWideChar",  _multi_byte_to_wide)
    stubs.register_handler("kernel32.dll", "WideCharToMultiByte",  _wide_to_multi_byte)
    stubs.register_handler("kernel32.dll", "GetStringTypeW",       _get_string_type_w)
    stubs.register_handler("kernel32.dll", "LCMapStringW",         _lc_map_string_w)
    stubs.register_handler("kernel32.dll", "GetLocaleInfoA",       _get_locale_info_a)

    # ── Fiber local storage (unimplemented — halt loudly) ─────────────────────

    stubs.register_handler("kernel32.dll", "FlsAlloc",    _halt("FlsAlloc"))
    stubs.register_handler("kernel32.dll", "FlsSetValue", _halt("FlsSetValue"))
    stubs.register_handler("kernel32.dll", "FlsGetValue", _halt("FlsGetValue"))
    stubs.register_handler("kernel32.dll", "FlsFree",     _halt("FlsFree"))
