"""kernel32.dll locale and string conversion handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers
    from tew.api._state import CRTState

from tew.hardware.cpu_zig import EAX, ESP
from tew.api.win32_handlers import cleanup_stdcall, unimplemented_halt as _halt
from tew.api.char_type import GetStringTypeArgs, classify_wide_string
from tew.api.lc_map import LCMapStringArgs, lc_map_wide_string
from tew.logger import logger

# ── Codepage identity ─────────────────────────────────────────────────────────
# Single source of truth for "what ANSI codepage does this process report" --
# GetACP/GetCPInfo/IsDBCSLeadByte must all agree, since real code (DAO, Jet,
# or the game itself) can reasonably expect these three APIs to describe the
# same codepage rather than being independently, disconnectedly stubbed.
ANSI_CODEPAGE = 1252  # Western European (Windows-1252) -- no lead bytes

# DBCS lead-byte ranges (inclusive) for codepages that have them. Windows-1252
# has no entry -- it's single-byte, so every value is a standalone character
# and IsDBCSLeadByte/GetCPInfo's LeadByte table are both correctly empty.
_DBCS_LEAD_BYTE_RANGES: dict[int, list[tuple[int, int]]] = {
    932: [(0x81, 0x9F), (0xE0, 0xFC)],   # Shift-JIS (Japanese)
    936: [(0x81, 0xFE)],                 # GBK (Simplified Chinese)
    949: [(0x81, 0xFE)],                 # Unified Hangul Code (Korean)
    950: [(0x81, 0xFE)],                 # Big5 (Traditional Chinese)
}


def _is_dbcs_lead_byte_value(codepage: int, byte_val: int) -> bool:
    return any(lo <= byte_val <= hi for lo, hi in _DBCS_LEAD_BYTE_RANGES.get(codepage, ()))


def register_kernel32_locale_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register code page, locale, and string conversion handlers."""

    # ── Code pages ────────────────────────────────────────────────────────────

    def _get_acp(cpu: "CPU") -> None:
        cpu.regs[EAX] = ANSI_CODEPAGE

    def _get_cp_info(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        memory.write32(lp, 1)
        memory.write8(lp + 4, 0x3F)  # '?'
        memory.write8(lp + 5, 0)
        # LeadByte[]: up to 5 (lo, hi) range pairs + a terminating (0, 0) pair
        # -- real CPINFO format. Empty for a single-byte codepage like 1252.
        ranges = _DBCS_LEAD_BYTE_RANGES.get(ANSI_CODEPAGE, [])
        for i in range(6):
            lo, hi = ranges[i] if i < len(ranges) else (0, 0)
            memory.write8(lp + 6 + i * 2, lo)
            memory.write8(lp + 7 + i * 2, hi)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    def _is_valid_code_page(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    def _is_dbcs_lead_byte(cpu: "CPU") -> None:
        test_char = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF) & 0xFF
        cpu.regs[EAX] = 1 if _is_dbcs_lead_byte_value(ANSI_CODEPAGE, test_char) else 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GetACP",           _get_acp)
    stubs.register_handler("kernel32.dll", "GetCPInfo",        _get_cp_info)
    stubs.register_handler("kernel32.dll", "IsValidCodePage",  _is_valid_code_page)
    stubs.register_handler("kernel32.dll", "IsDBCSLeadByte",   _is_dbcs_lead_byte)

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
            cpu.fatal_halt = True
            return
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 16)

    def _get_string_type_ex_w(cpu: "CPU") -> None:
        # GetStringTypeExW(Locale, dwInfoType, lpSrcStr, cchSrc, lpCharType) --
        # same 4 trailing args as GetStringTypeW plus a leading Locale that
        # real Windows ignores for character-type classification (Unicode
        # char typing isn't locale-dependent), so this delegates to the same
        # classify_wide_string logic GetStringTypeW uses.
        args = GetStringTypeArgs(
            info_type = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF),
            src_ptr   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF),
            cch_src   = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF),
            out_ptr   = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF),
        )
        if not classify_wide_string(memory, args):
            logger.error("handlers",
                f"GetStringTypeExW: unsupported dwInfoType {args.info_type:#010x} — halting")
            cpu.halted = True
            cpu.fatal_halt = True
            return
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 20)

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
            cpu.fatal_halt = True
            return
        cpu.regs[EAX] = result
        cleanup_stdcall(cpu, memory, 24)

    # Real en-US (0x0409) locale data. This emulator only ever models one
    # locale everywhere else (_locale_is_valid/_get_user_default_lcid/
    # _get_system_default_lang_id, kernel32_io.py, all hardcoded to 0x0409) --
    # GetLocaleInfoA unconditionally returning 0/failure for every LCTYPE
    # was inconsistent with that. Confirmed live 2026-08-28: real Jet's
    # locale-aware date-literal tokenizer (compiling a WHERE-clause date
    # literal like #1/1/2010#) depends on this to determine the system's
    # date format/separator, and a hard failure here is a real, plausible
    # cause of its "Syntax error in date" (DAO error 2421) result.
    _RESOLVABLE_LOCALES_INFO = {0x0400: 0x0409, 0x0800: 0x0409}  # USER/SYSTEM_DEFAULT

    LOCALE_RETURN_NUMBER = 0x20000000
    # 2026-08-28: LOCALE_NOUSEROVERRIDE is a SEPARATE modifier flag from
    # LOCALE_RETURN_NUMBER -- real callers OR it into LCType to mean "use
    # system default, not any user override" (e.g. FUN_771454bc's retry
    # path in oleaut32.dll ORs this in unconditionally). It must also be
    # masked off before the table lookup, same as LOCALE_RETURN_NUMBER;
    # tew only ever serves one hardcoded en-US dataset regardless, so
    # stripping it is harmless and correct. Found live: lctype_raw=
    # 0x80000021 (LOCALE_IDATE|NOUSEROVERRIDE) was falling through to
    # "unimplemented LCTYPE" because only the RETURN_NUMBER bit was
    # stripped, leaving 0x80000021 instead of the real 0x21.
    LOCALE_NOUSEROVERRIDE = 0x80000000

    # String-valued LCTYPEs, real Windows XP en-US defaults. Values and
    # constant numbers cross-checked against real winnls.h LCTYPE
    # definitions -- an earlier pass here had several wrong (a constant
    # labeled as one LCTYPE but numbered as a different one); this table
    # replaces it with verified values only.
    _LOCALE_STRINGS: dict[int, str] = {
        0x0002: "English",                    # LOCALE_SLANGUAGE
        0x0003: "ENU",                        # LOCALE_SABBREVLANGNAME
        0x0006: "United States",              # LOCALE_SCOUNTRY
        0x0007: "USA",                        # LOCALE_SABBREVCTRYNAME
        0x000E: ".",                          # LOCALE_SDECIMAL
        0x000F: ",",                          # LOCALE_STHOUSAND
        0x0010: "3;0",                        # LOCALE_SGROUPING
        0x0013: "0123456789",                 # LOCALE_SNATIVEDIGITS
        0x0014: "$",                          # LOCALE_SCURRENCY
        0x0016: ".",                          # LOCALE_SMONDECIMALSEP
        0x0017: ",",                          # LOCALE_SMONTHOUSANDSEP
        0x0018: "3;0",                        # LOCALE_SMONGROUPING
        0x001D: "/",                          # LOCALE_SDATE (date separator)
        0x001E: ":",                          # LOCALE_STIME (time separator)
        0x001F: "M/d/yyyy",                   # LOCALE_SSHORTDATE
        0x0020: "dddd, MMMM dd, yyyy",        # LOCALE_SLONGDATE
        0x0028: "AM",                         # LOCALE_S1159
        0x0029: "PM",                         # LOCALE_S2359
        0x0050: "",                           # LOCALE_SPOSITIVESIGN
        0x0051: "-",                          # LOCALE_SNEGATIVESIGN
        0x1001: "English",                    # LOCALE_SENGLANGUAGE
        0x1002: "United States",              # LOCALE_SENGCOUNTRY
        0x1003: "h:mm:ss tt",                 # LOCALE_STIMEFORMAT
    }

    # Numeric-valued LCTYPEs (returned via LOCALE_RETURN_NUMBER as a raw
    # DWORD, not a string) -- real Windows XP en-US defaults.
    _LOCALE_NUMBERS: dict[int, int] = {
        0x0005: 1,        # LOCALE_ICOUNTRY (1 = USA calling code)
        0x000A: 1,        # LOCALE_IDEFAULTCOUNTRY
        0x000B: 437,      # LOCALE_IDEFAULTCODEPAGE (US OEM codepage)
        0x000D: 1,        # LOCALE_IMEASURE (1 = US/imperial)
        0x0011: 2,        # LOCALE_IDIGITS
        0x0012: 1,        # LOCALE_ILZERO
        0x0019: 2,        # LOCALE_ICURRDIGITS
        0x001A: 2,        # LOCALE_IINTLCURRDIGITS
        0x001B: 0,        # LOCALE_ICURRENCY
        0x001C: 0,        # LOCALE_INEGCURR
        0x0021: 0,        # LOCALE_IDATE (0 = month-day-year, e.g. "1/1/2010")
        0x0022: 0,        # LOCALE_ILDATE
        0x0023: 0,        # LOCALE_ITIME (0 = 12-hour clock)
        0x0024: 0,        # LOCALE_ICENTURY (0 = 2-digit year in short date)
        0x0025: 0,        # LOCALE_ITLZERO
        0x0026: 0,        # LOCALE_IDAYLZERO (no leading zero: "1", not "01")
        0x0027: 0,        # LOCALE_IMONLZERO (no leading zero: "1", not "01")
        0x1004: 1252,     # LOCALE_IDEFAULTANSICODEPAGE (matches ANSI_CODEPAGE above)
        0x1009: 1,        # LOCALE_ICALENDARTYPE (CAL_GREGORIAN = 1)
        0x100C: 6,        # LOCALE_IFIRSTDAYOFWEEK (US: week starts Sunday -> 6)
        0x100D: 0,        # LOCALE_IFIRSTWEEKOFYEAR
    }

    def _locale_string_for(lctype: int) -> str | None:
        # 2026-08-28: real Windows returns many "numeric" LCTYPEs (e.g.
        # LOCALE_IDATE, LOCALE_ICALENDARTYPE) as a plain decimal-digit
        # STRING by default -- callers only get the raw DWORD if they
        # explicitly OR in LOCALE_RETURN_NUMBER. Found live: msjet35.dll/
        # oleaut32.dll query LOCALE_IDATE without that flag and parse the
        # returned string themselves (checking `*param3==L'0'` directly,
        # or via _wtoi). Falling back to the numeric table's string form
        # instead of duplicating every entry into _LOCALE_STRINGS too.
        text = _LOCALE_STRINGS.get(lctype)
        if text is not None:
            return text
        value = _LOCALE_NUMBERS.get(lctype)
        if value is not None:
            return str(value)
        return None

    def _get_locale_info_a(cpu: "CPU") -> None:
        locale = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lctype_raw = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        lp_data = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cch_data = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)

        resolved = _RESOLVABLE_LOCALES_INFO.get(locale, locale)
        want_number = (lctype_raw & LOCALE_RETURN_NUMBER) != 0
        lctype = lctype_raw & ~(LOCALE_RETURN_NUMBER | LOCALE_NOUSEROVERRIDE)

        if resolved != 0x0409:
            logger.warn("handlers",
                f"GetLocaleInfoA(locale=0x{locale:x}, lctype=0x{lctype_raw:x}) -> 0 (unsupported locale)")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 16)
            return

        if want_number:
            value = _LOCALE_NUMBERS.get(lctype)
            if value is None:
                logger.warn("handlers",
                    f"GetLocaleInfoA(lctype=0x{lctype:x}|NUMBER) -> 0 (unimplemented numeric LCTYPE)")
                cpu.regs[EAX] = 0
                cleanup_stdcall(cpu, memory, 16)
                return
            if cch_data != 0:
                if cch_data < 4:
                    cpu.regs[EAX] = 0
                    cleanup_stdcall(cpu, memory, 16)
                    return
                memory.write32(lp_data, value & 0xFFFFFFFF)
            logger.debug("handlers", f"GetLocaleInfoA(lctype=0x{lctype:x}|NUMBER) -> {value}")
            cpu.regs[EAX] = 4
            cleanup_stdcall(cpu, memory, 16)
            return

        text = _locale_string_for(lctype)
        if text is None:
            logger.warn("handlers",
                f"GetLocaleInfoA(lctype=0x{lctype:x}) -> 0 (unimplemented LCTYPE)")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 16)
            return

        needed = len(text) + 1
        if cch_data == 0:
            cpu.regs[EAX] = needed
        elif needed > cch_data:
            cpu.regs[EAX] = 0
        else:
            for i, ch in enumerate(text):
                memory.write8(lp_data + i, ord(ch))
            memory.write8(lp_data + len(text), 0)
            cpu.regs[EAX] = needed
        logger.debug("handlers", f"GetLocaleInfoA(lctype=0x{lctype:x}) -> {text!r}")
        cleanup_stdcall(cpu, memory, 16)

    def _get_locale_info_w(cpu: "CPU") -> None:
        # 2026-08-28: was a bare "always fail" stub (EAX=0, no logging at
        # all) -- real oleaut32.dll code (FUN_7713cee1, reached from
        # VarDateFromStr's whole locale-struct-population chain) calls
        # this directly and unconditionally on the wide path, and its
        # silent, untraceable failure was the actual root cause of the
        # global locale-info-struct cache getting permanently poisoned
        # with a near-empty struct (LCID 0, calendar type 0, empty date
        # separator) on the very first locale query the process ever
        # makes -- confirmed live via cpu_add_logpoint tracing through
        # FUN_77145656/FUN_77145563/FUN_771454bc/FUN_7713cee1. Mirrors
        # _get_locale_info_a's tables exactly, writing UTF-16 instead of
        # ANSI bytes and counting cch_data in WCHARs (real TCHAR
        # convention for the W version) instead of bytes.
        locale = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lctype_raw = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        lp_data = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cch_data = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)

        resolved = _RESOLVABLE_LOCALES_INFO.get(locale, locale)
        want_number = (lctype_raw & LOCALE_RETURN_NUMBER) != 0
        lctype = lctype_raw & ~(LOCALE_RETURN_NUMBER | LOCALE_NOUSEROVERRIDE)

        if resolved != 0x0409:
            logger.warn("handlers",
                f"GetLocaleInfoW(locale=0x{locale:x}, lctype=0x{lctype_raw:x}) -> 0 (unsupported locale)")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 16)
            return

        if want_number:
            value = _LOCALE_NUMBERS.get(lctype)
            if value is None:
                logger.warn("handlers",
                    f"GetLocaleInfoW(lctype=0x{lctype:x}|NUMBER) -> 0 (unimplemented numeric LCTYPE)")
                cpu.regs[EAX] = 0
                cleanup_stdcall(cpu, memory, 16)
                return
            if cch_data != 0:
                # Real GetLocaleInfoW still writes a raw 4-byte DWORD for
                # LOCALE_RETURN_NUMBER regardless of A/W -- only the
                # documented *return value* convention differs (2 for W,
                # 4 for A), not the buffer contents.
                if cch_data < 2:
                    cpu.regs[EAX] = 0
                    cleanup_stdcall(cpu, memory, 16)
                    return
                memory.write32(lp_data, value & 0xFFFFFFFF)
            logger.debug("handlers", f"GetLocaleInfoW(lctype=0x{lctype:x}|NUMBER) -> {value}")
            cpu.regs[EAX] = 2
            cleanup_stdcall(cpu, memory, 16)
            return

        text = _locale_string_for(lctype)
        if text is None:
            logger.warn("handlers",
                f"GetLocaleInfoW(lctype=0x{lctype:x}) -> 0 (unimplemented LCTYPE)")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 16)
            return

        needed = len(text) + 1
        if cch_data == 0:
            cpu.regs[EAX] = needed
        elif needed > cch_data:
            cpu.regs[EAX] = 0
        else:
            for i, ch in enumerate(text):
                memory.write16(lp_data + i * 2, ord(ch))
            memory.write16(lp_data + len(text) * 2, 0)
            cpu.regs[EAX] = needed
        logger.debug("handlers", f"GetLocaleInfoW(lctype=0x{lctype:x}) -> {text!r}")
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("kernel32.dll", "MultiByteToWideChar",  _multi_byte_to_wide)
    stubs.register_handler("kernel32.dll", "WideCharToMultiByte",  _wide_to_multi_byte)
    stubs.register_handler("kernel32.dll", "GetStringTypeW",       _get_string_type_w)
    stubs.register_handler("kernel32.dll", "GetStringTypeExW",     _get_string_type_ex_w)
    stubs.register_handler("kernel32.dll", "LCMapStringW",         _lc_map_string_w)
    stubs.register_handler("kernel32.dll", "GetLocaleInfoA",       _get_locale_info_a)
    stubs.register_handler("kernel32.dll", "GetLocaleInfoW",       _get_locale_info_w)

    # ── Fiber local storage (unimplemented — halt loudly) ─────────────────────

    stubs.register_handler("kernel32.dll", "FlsAlloc",    _halt("FlsAlloc"))
    stubs.register_handler("kernel32.dll", "FlsSetValue", _halt("FlsSetValue"))
    stubs.register_handler("kernel32.dll", "FlsGetValue", _halt("FlsGetValue"))
    stubs.register_handler("kernel32.dll", "FlsFree",     _halt("FlsFree"))
