"""oleaut32.dll and ole32.dll handler registrations.

Implements BSTR heap, VARIANT lifecycle, SafeArray allocation, COM initialisation
stubs, and the ordinal-aliased exports from WinXP OLEAUT32.dll.
"""

from __future__ import annotations

import math
import re
import struct
from datetime import date, timedelta
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory
    from tew.loader.dll_loader import DLLLoader, LoadedDLL

from tew.hardware.cpu_zig import EAX, ESP
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall
from tew.api._state import CRTState, read_wide_string
from tew.logger import logger

S_OK = 0
E_NOINTERFACE = 0x80004002
E_INVALIDARG = 0x80070057
REGDB_E_CLASSNOTREG = 0x80040154
CO_E_CLASSSTRING = 0x800401F3
CO_S_NOTALLINTERFACES = 0x00080012

# Real, period-correct COM in-proc servers this emulator can actually load
# and execute (as opposed to Python-faked). Registry entries whose
# InprocServer32 names something not in this set are honestly reported as
# unregistered, same as a real, unmodified install missing that component.
#
# dao350.dll: extracted from the game's OWN real installer
# (~/.emu32/DBInst/DAO/data1.cab, "DAO registered\dao350.dll") -- the exact
# version this game's CLSID {00000010-...} actually belongs to (confirmed:
# a newer dao360.dll, also available on this host, does NOT contain that
# CLSID -- DAO 3.5 and 3.6 are different, non-interchangeable installed
# components, not just a version bump). Placed at
# C:\WINDOWS\System32\dao350.dll (~/.emu32/WINDOWS/System32/), i.e. exactly
# where the real installer would have put it -- kept out of the tew repo
# itself since these are Microsoft-copyrighted redistributable binaries,
# not project source.
_KNOWN_COM_SERVER_DIR = "/home/drazisil/.emu32/WINDOWS/System32"
_KNOWN_COM_SERVERS = {"dao350.dll"}


def register_oleaut32_ole32_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
    dll_loader: Optional["DLLLoader"] = None,
) -> None:
    """Register all oleaut32.dll and ole32.dll handlers."""

    if dll_loader is not None:
        dll_loader.add_search_path(_KNOWN_COM_SERVER_DIR)

    # A real, genuine oleaut32.dll loads for real in this emulator (confirmed
    # live 2026-08-26: it patches its own imports from advapi32/gdi32/
    # kernel32/msvcrt/ole32/rpcrt4/user32.dll, and msjet35.dll/expsrv.dll/
    # dao350.dll all correctly resolve their own oleaut32.dll imports against
    # it). dll_loader.py's patch_iat_entry tries a *registered handler*
    # before ever checking a real DLL's own export -- so every
    # "oleaut32.dll" handler this file registers unconditionally shadows the
    # real, correct Microsoft code, even though it's genuinely present and
    # loaded. This was the actual root cause of the whole LoadTypeLibEx/
    # ITypeComp::Bind investigation (see changelog.md 2026-08-26): real
    # oleaut32.dll would have parsed expsrv.dll's real embedded TYPELIB
    # resource (confirmed present, 42KB) and answered every query correctly,
    # but a hand-crafted trap object was answering first instead.
    # Neutralizing every "oleaut32.dll" registration this file makes --
    # real code should be handling all of it now.
    _real_stubs = stubs

    class _NoOleaut32Stubs:
        def __getattr__(self, name):
            return getattr(_real_stubs, name)

        def register_handler(self, dll_name, func_name, handler):
            if dll_name == "oleaut32.dll":
                return
            _real_stubs.register_handler(dll_name, func_name, handler)

    stubs = _NoOleaut32Stubs()

    # ── oleaut32.dll — BSTR / VARIANT / SafeArray ─────────────────────────────
    #
    # BSTR memory layout:
    #   [block + 0]  4-byte byte-length prefix
    #   [block + 4]  wide-char data (byte-length bytes)
    #   [block + 4 + byte-length]  2-byte null terminator
    #
    # The pointer returned to the caller points at [block + 4], i.e. the data.
    # SysStringLen reads the 4-byte prefix at ptr-4 and divides by 2.
    # SysStringByteLen reads the 4-byte prefix at ptr-4 directly.

    # SysAllocString(LPCOLESTR psz) -> BSTR
    def _SysAllocString(cpu: "CPU") -> None:
        psz = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if psz == 0:
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 4)
            return
        length = 0
        while memory.read16(psz + length * 2) != 0:
            length += 1
        byte_len = length * 2
        block = state.simple_alloc(4 + byte_len + 2)
        memory.write32(block, byte_len)
        for i in range(byte_len + 2):
            memory.write8(block + 4 + i, memory.read8(psz + i))
        cpu.regs[EAX] = (block + 4) & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("oleaut32.dll", "SysAllocString", _SysAllocString)

    # SysAllocStringLen(LPCOLESTR psz, UINT len) -> BSTR
    def _SysAllocStringLen(cpu: "CPU") -> None:
        psz    = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        length = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        byte_len = length * 2
        block = state.simple_alloc(4 + byte_len + 2)
        memory.write32(block, byte_len)
        if psz != 0:
            for i in range(byte_len):
                memory.write8(block + 4 + i, memory.read8(psz + i))
        memory.write16(block + 4 + byte_len, 0)
        cpu.regs[EAX] = (block + 4) & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("oleaut32.dll", "SysAllocStringLen", _SysAllocStringLen)

    # SysAllocStringByteLen(LPCSTR psz, UINT len) -> BSTR
    def _SysAllocStringByteLen(cpu: "CPU") -> None:
        psz    = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        length = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        block = state.simple_alloc(4 + length + 2)
        memory.write32(block, length)
        if psz != 0:
            for i in range(length):
                memory.write8(block + 4 + i, memory.read8(psz + i))
        memory.write16(block + 4 + length, 0)
        cpu.regs[EAX] = (block + 4) & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("oleaut32.dll", "SysAllocStringByteLen", _SysAllocStringByteLen)

    # SysReAllocString(BSTR* pbstr, LPCOLESTR psz) -> INT
    def _SysReAllocString(cpu: "CPU") -> None:
        pbstr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        psz   = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        length = 0
        if psz != 0:
            while memory.read16(psz + length * 2) != 0:
                length += 1
        byte_len = length * 2
        block = state.simple_alloc(4 + byte_len + 2)
        memory.write32(block, byte_len)
        if psz != 0:
            for i in range(byte_len + 2):
                memory.write8(block + 4 + i, memory.read8(psz + i))
        memory.write16(block + 4 + byte_len, 0)
        memory.write32(pbstr, (block + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("oleaut32.dll", "SysReAllocString", _SysReAllocString)

    # SysReAllocStringLen(BSTR* pbstr, LPCOLESTR psz, UINT len) -> INT
    def _SysReAllocStringLen(cpu: "CPU") -> None:
        pbstr  = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        psz    = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        length = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        byte_len = length * 2
        block = state.simple_alloc(4 + byte_len + 2)
        memory.write32(block, byte_len)
        if psz != 0:
            for i in range(byte_len):
                memory.write8(block + 4 + i, memory.read8(psz + i))
        memory.write16(block + 4 + byte_len, 0)
        memory.write32(pbstr, (block + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("oleaut32.dll", "SysReAllocStringLen", _SysReAllocStringLen)

    # SysFreeString(BSTR bstr) -> void
    # The bump-allocator cannot free; no-op is correct here.
    def _SysFreeString(cpu: "CPU") -> None:
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("oleaut32.dll", "SysFreeString", _SysFreeString)

    # SysStringLen(BSTR bstr) -> UINT (character count)
    def _SysStringLen(cpu: "CPU") -> None:
        bstr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if bstr == 0:
            cpu.regs[EAX] = 0
        else:
            byte_len = memory.read32((bstr - 4) & 0xFFFFFFFF)
            cpu.regs[EAX] = byte_len // 2
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("oleaut32.dll", "SysStringLen", _SysStringLen)

    # SysStringByteLen(BSTR bstr) -> UINT (byte count)
    def _SysStringByteLen(cpu: "CPU") -> None:
        bstr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if bstr == 0:
            cpu.regs[EAX] = 0
        else:
            cpu.regs[EAX] = memory.read32((bstr - 4) & 0xFFFFFFFF)
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("oleaut32.dll", "SysStringByteLen", _SysStringByteLen)

    # VariantInit(VARIANTARG *pvarg) -> void
    def _VariantInit(cpu: "CPU") -> None:
        pv = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if pv != 0:
            for i in range(16):
                memory.write8(pv + i, 0)
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("oleaut32.dll", "VariantInit", _VariantInit)

    # VariantClear(VARIANTARG *pvarg) -> HRESULT
    def _VariantClear(cpu: "CPU") -> None:
        pv = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if pv != 0:
            for i in range(16):
                memory.write8(pv + i, 0)
        cpu.regs[EAX] = 0  # S_OK
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("oleaut32.dll", "VariantClear", _VariantClear)

    # VariantChangeType(pvargDest, pvarSrc, wFlags, vt) -> HRESULT
    #
    # VARIANT layout (16 bytes): vt at +0 (2 bytes), 6 bytes reserved, value
    # union at +8. Real OLE Automation numeric coercion: only the
    # well-defined integer/bool subset confirmed live so far (VT_I2<->VT_I4
    # <->VT_BOOL, plus VT_INT treated as storage-identical to VT_I4 per
    # MSDN VARENUM) is implemented with real range-checked semantics; anything
    # else (VT_BSTR/VT_R4/VT_R8/VT_CY/VT_DATE/...) halts loudly rather than
    # guess at locale-aware string parsing or float formatting rules never
    # actually observed. pvargDest/pvarSrc may alias (real callers rely on
    # in-place conversion) -- the source value is read into a local before
    # any write to the destination, so aliasing is safe regardless.
    _VT_I2   = 2
    _VT_I4   = 3
    _VT_BOOL = 11
    _VT_INT  = 22  # MSDN VARENUM: storage-identical to VT_I4 (4-byte signed int at +8)
    _DISP_E_OVERFLOW = 0x8002000A

    def _variant_read_i2(addr: int) -> int:
        v = memory.read16(addr + 8)
        return v - 0x10000 if v >= 0x8000 else v

    def _variant_write_i2(addr: int, val: int) -> None:
        memory.write16(addr, _VT_I2)
        memory.write16(addr + 8, val & 0xFFFF)

    def _variant_read_i4(addr: int) -> int:
        return memory.read_signed32(addr + 8)

    def _variant_write_i4(addr: int, val: int) -> None:
        memory.write16(addr, _VT_I4)
        memory.write32(addr + 8, val & 0xFFFFFFFF)

    def _variant_write_int(addr: int, val: int) -> None:
        memory.write16(addr, _VT_INT)
        memory.write32(addr + 8, val & 0xFFFFFFFF)

    def _variant_write_bool(addr: int, val: bool) -> None:
        memory.write16(addr, _VT_BOOL)
        memory.write16(addr + 8, 0xFFFF if val else 0x0000)

    def _VariantChangeType(cpu: "CPU") -> None:
        pvarg_dest = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        pvar_src   = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        target_vt  = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF) & 0xFFFF
        src_vt = memory.read16(pvar_src)

        if src_vt == _VT_I2:
            val = _variant_read_i2(pvar_src)
        elif src_vt == _VT_I4 or src_vt == _VT_INT:
            val = _variant_read_i4(pvar_src)
        elif src_vt == _VT_BOOL:
            # VARIANT_BOOL's storage IS a signed 16-bit field (VARIANT_TRUE
            # = -1, VARIANT_FALSE = 0) -- read the real signed value
            # directly rather than remapping to 1/0, matching real
            # VariantChangeType(VT_BOOL -> numeric) semantics exactly.
            val = _variant_read_i2(pvar_src)
        else:
            logger.error("handlers",
                f"[UNIMPLEMENTED] VariantChangeType: unhandled source vt={src_vt} — halting")
            cpu.halted = True
            cpu.fatal_halt = True
            return

        if target_vt == _VT_I2:
            if not (-32768 <= val <= 32767):
                cpu.regs[EAX] = _DISP_E_OVERFLOW
                cleanup_stdcall(cpu, memory, 16)
                return
            _variant_write_i2(pvarg_dest, val)
        elif target_vt == _VT_I4:
            _variant_write_i4(pvarg_dest, val)
        elif target_vt == _VT_INT:
            _variant_write_int(pvarg_dest, val)
        elif target_vt == _VT_BOOL:
            _variant_write_bool(pvarg_dest, val != 0)
        else:
            logger.error("handlers",
                f"[UNIMPLEMENTED] VariantChangeType: unhandled target vt={target_vt} "
                f"(src vt={src_vt} val={val}) — halting")
            cpu.halted = True
            cpu.fatal_halt = True
            return

        cpu.regs[EAX] = S_OK
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("oleaut32.dll", "VariantChangeType", _VariantChangeType)

    # SafeArrayCreate(VARTYPE vt, UINT cDims, SAFEARRAYBOUND *rgsabound) -> SAFEARRAY*
    def _SafeArrayCreate(cpu: "CPU") -> None:
        _vt        = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        c_dims     = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        rgsabound  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        header_size = 16 + c_dims * 8
        # VT_BSTR (8) = 4-byte pointers; default 4 bytes per element
        elem_size = 4  # noqa: F841 — same for both branches in original
        total_elements = 1
        for d in range(c_dims):
            total_elements *= memory.read32(rgsabound + d * 8)
        data_size = total_elements * elem_size
        sa_block = state.simple_alloc(header_size + data_size)
        memory.write16(sa_block,      c_dims)
        memory.write16(sa_block + 2,  0)
        memory.write32(sa_block + 4,  elem_size)
        memory.write32(sa_block + 8,  0)
        memory.write32(sa_block + 12, (sa_block + header_size) & 0xFFFFFFFF)
        for d in range(c_dims):
            memory.write32(sa_block + 16 + d * 8,     memory.read32(rgsabound + d * 8))
            memory.write32(sa_block + 16 + d * 8 + 4, memory.read32(rgsabound + d * 8 + 4))
        for i in range(data_size):
            memory.write8(sa_block + header_size + i, 0)
        cpu.regs[EAX] = sa_block & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("oleaut32.dll", "SafeArrayCreate", _SafeArrayCreate)

    # SafeArrayGetDim(SAFEARRAY *psa) -> UINT
    def _SafeArrayGetDim(cpu: "CPU") -> None:
        psa = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = memory.read16(psa) if psa else 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("oleaut32.dll", "SafeArrayGetDim", _SafeArrayGetDim)

    # SafeArrayGetElemsize(SAFEARRAY *psa) -> UINT
    def _SafeArrayGetElemsize(cpu: "CPU") -> None:
        psa = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = memory.read32(psa + 4) if psa else 0
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("oleaut32.dll", "SafeArrayGetElemsize", _SafeArrayGetElemsize)

    # SafeArrayGetUBound(SAFEARRAY *psa, UINT nDim, LONG *plUbound) -> HRESULT
    def _SafeArrayGetUBound(cpu: "CPU") -> None:
        psa      = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        n_dim    = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        pl_ubound = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if psa and pl_ubound:
            off = 16 + (n_dim - 1) * 8
            c_elements = memory.read32(psa + off)
            l_lbound   = memory.read32(psa + off + 4)
            memory.write32(pl_ubound, (l_lbound + c_elements - 1) & 0xFFFFFFFF)
        cpu.regs[EAX] = 0  # S_OK
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("oleaut32.dll", "SafeArrayGetUBound", _SafeArrayGetUBound)

    # SafeArrayGetLBound(SAFEARRAY *psa, UINT nDim, LONG *plLbound) -> HRESULT
    def _SafeArrayGetLBound(cpu: "CPU") -> None:
        psa      = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        n_dim    = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        pl_lbound = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if psa and pl_lbound:
            memory.write32(pl_lbound, memory.read32(psa + 16 + (n_dim - 1) * 8 + 4))
        cpu.regs[EAX] = 0  # S_OK
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("oleaut32.dll", "SafeArrayGetLBound", _SafeArrayGetLBound)

    # SafeArrayAccessData(SAFEARRAY *psa, void **ppvData) -> HRESULT
    def _SafeArrayAccessData(cpu: "CPU") -> None:
        psa     = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        ppv_data = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if psa and ppv_data:
            memory.write32(ppv_data, memory.read32(psa + 12))
        cpu.regs[EAX] = 0  # S_OK
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("oleaut32.dll", "SafeArrayAccessData", _SafeArrayAccessData)

    # SafeArrayUnaccessData(SAFEARRAY *psa) -> HRESULT
    def _SafeArrayUnaccessData(cpu: "CPU") -> None:
        # Spec: decrements lock count. We don't track locks — no-op is harmless.
        cpu.regs[EAX] = 0  # S_OK
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("oleaut32.dll", "SafeArrayUnaccessData", _SafeArrayUnaccessData)

    # SafeArrayRedim(SAFEARRAY *psa, SAFEARRAYBOUND *psaboundNew) -> HRESULT
    def _SafeArrayRedim(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] SafeArrayRedim — halting")
        cpu.halted = True
        cpu.fatal_halt = True

    stubs.register_handler("oleaut32.dll", "SafeArrayRedim", _SafeArrayRedim)

    # SafeArrayPutElement(SAFEARRAY *psa, LONG *rgIndices, void *pv) -> HRESULT
    def _SafeArrayPutElement(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] SafeArrayPutElement — halting")
        cpu.halted = True
        cpu.fatal_halt = True

    stubs.register_handler("oleaut32.dll", "SafeArrayPutElement", _SafeArrayPutElement)

    # SafeArrayDestroy(SAFEARRAY *psa) -> HRESULT
    def _SafeArrayDestroy(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # S_OK; bump allocator cannot free
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("oleaut32.dll", "SafeArrayDestroy", _SafeArrayDestroy)

    # ── oleaut32.dll ordinal aliases ──────────────────────────────────────────
    # Ordinals verified against WinXP OLEAUT32.dll export table.
    # Game imports by ordinal only, so names must match exactly.

    def _ole_ord(n: int, fn: "type[CPU]") -> None:  # type: ignore[valid-type]
        stubs.register_handler("oleaut32.dll", f"Ordinal #{n}", fn)

    # Ordinal 2 — SysAllocString(psz) -> BSTR
    def _ord2(cpu: "CPU") -> None:
        psz = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if psz == 0:
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 4)
            return
        length = 0
        while memory.read16(psz + length * 2):
            length += 1
        byte_len = length * 2
        block = state.simple_alloc(byte_len + 6)
        memory.write32(block, byte_len)
        for i in range(byte_len):
            memory.write8(block + 4 + i, memory.read8(psz + i))
        memory.write16(block + 4 + byte_len, 0)
        cpu.regs[EAX] = block + 4
        cleanup_stdcall(cpu, memory, 4)

    _ole_ord(2, _ord2)

    # Ordinal 4 — SysAllocStringLen (same handler as the named export above;
    # DAO350.DLL imports OLEAUT32 by ordinal, not by name)
    _ole_ord(4, _SysAllocStringLen)

    # Ordinal 6 — SysFreeString(bstr) -> void
    def _ord6(cpu: "CPU") -> None:
        cleanup_stdcall(cpu, memory, 4)

    _ole_ord(6, _ord6)

    # Ordinal 7 — SysStringLen(bstr) -> UINT
    def _ord7(cpu: "CPU") -> None:
        bstr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = (memory.read32(bstr - 4) >> 1) if bstr else 0
        cleanup_stdcall(cpu, memory, 4)

    _ole_ord(7, _ord7)

    # Ordinal 8 — VariantInit(pvarg)
    def _ord8(cpu: "CPU") -> None:
        pv = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if pv:
            memory.write16(pv,     0)
            memory.write16(pv + 2, 0)
        cleanup_stdcall(cpu, memory, 4)

    _ole_ord(8, _ord8)

    # Ordinal 9 — VariantClear(pvarg) -> HRESULT
    # Real callers (msjet35.dll, dao350.dll) import this by ordinal, not by
    # name, so it must be kept behaviorally identical to the named
    # VariantClear handler above rather than reimplemented separately --
    # delegating directly makes that impossible to drift out of sync again.
    # The bug this replaces only zeroed the 4-byte vt/reserved header,
    # leaving the 8-byte value union (e.g. a BSTR pointer) untouched, so a
    # "cleared" VARIANT still held stale data for anything that read it
    # without checking vt first.
    _ole_ord(9, _VariantClear)

    # Ordinal 10 — VariantCopy(pvargDest, pvargSrc) -> HRESULT
    def _ord10(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] VariantCopy (Ordinal 10) — halting")
        cpu.halted = True
        cpu.fatal_halt = True

    _ole_ord(10, _ord10)

    # Ordinal 12 — same real function as the named export above; DAO350.DLL
    # imports OLEAUT32 by ordinal, not by name.
    _ole_ord(12, _VariantChangeType)

    # Ordinal 15 — SafeArrayCreate (same handler as the named export above;
    # DAO350.DLL imports OLEAUT32 by ordinal, not by name)
    _ole_ord(15, _SafeArrayCreate)

    # Ordinal 21 — SafeArrayLock(psa) -> HRESULT
    # Spec: increments the array's lock count to pin pvData in place. Like
    # SafeArrayUnaccessData above, we don't track locks -- nothing in this
    # emulator moves/frees a SAFEARRAY's data out from under a caller, so a
    # harmless no-op returning S_OK is sufficient.
    def _ord21(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # S_OK
        cleanup_stdcall(cpu, memory, 4)

    _ole_ord(21, _ord21)

    # Ordinal 82 — VarR8FromCy(cyIn, pdblOut) -> HRESULT
    def _ord82(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] VarR8FromCy (Ordinal 82) — halting")
        cpu.halted = True
        cpu.fatal_halt = True

    _ole_ord(82, _ord82)

    # Ordinal 104 — VarCyFromStr(strIn, lcid, dwFlags, pcyOut) -> HRESULT
    def _ord104(cpu: "CPU") -> None:
        logger.warn("com", "VarCyFromStr (Ordinal 104) called — returning E_NOTIMPL (currency conversion not implemented)")
        cpu.regs[EAX] = 0x80004001  # E_NOTIMPL
        cleanup_stdcall(cpu, memory, 20)

    _ole_ord(104, _ord104)

    # Ordinal 113 — VarBstrFromCy(cyIn, lcid, dwFlags, pbstrOut) -> HRESULT
    def _ord113(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] VarBstrFromCy (Ordinal 113) — halting")
        cpu.halted = True
        cpu.fatal_halt = True

    _ole_ord(113, _ord113)

    # Ordinal 149 — SysStringByteLen(bstr) -> UINT
    def _ord149(cpu: "CPU") -> None:
        bstr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        length = memory.read32(bstr - 4) if bstr else 0
        logger.debug("handlers", f"SysStringByteLen(bstr=0x{bstr:x}) -> {length} (0x{length:x})")
        cpu.regs[EAX] = length
        cleanup_stdcall(cpu, memory, 4)

    _ole_ord(149, _ord149)

    # Ordinal 150 — SysAllocStringByteLen(psz, len) -> BSTR
    def _ord150(cpu: "CPU") -> None:
        psz      = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        byte_len = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        # Layout: [4-byte byte-count][byte_len bytes][2-byte WCHAR null]
        block = state.simple_alloc(byte_len + 6)
        memory.write32(block, byte_len)
        for i in range(byte_len):
            b = memory.read8((psz + i) & 0xFFFFFFFF) if psz else 0
            memory.write8((block + 4 + i) & 0xFFFFFFFF, b)
        memory.write16((block + 4 + byte_len) & 0xFFFFFFFF, 0)
        cpu.regs[EAX] = (block + 4) & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 8)

    _ole_ord(150, _ord150)

    # Ordinal 64 — VarI4FromStr(LPCOLESTR strIn, LCID lcid, ULONG dwFlags,
    # LONG *plOut) -> HRESULT. Live-confirmed call: MSJET35.DLL's expression
    # parser (FUN_7a86756b) converting a plain decimal numeric literal
    # (e.g. "251658241") inside a WHERE-clause expression to VT_I4. Only
    # the well-defined case -- optional sign, ASCII digits, optional
    # surrounding whitespace, no locale-specific thousands/decimal
    # separators -- is implemented with real range-checked semantics
    # (matching this file's own stated philosophy elsewhere); a string
    # that doesn't match that shape returns the real HRESULT
    # (DISP_E_TYPEMISMATCH) real Windows would also give for genuinely
    # non-numeric input -- that's the real contract, not a guess.
    _INT_STR_RE = re.compile(r"^\s*[+-]?[0-9]+\s*$")
    _DISP_E_TYPEMISMATCH = 0x80020005

    def _VarI4FromStr(cpu: "CPU") -> None:
        str_in  = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        pl_out  = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        s = read_wide_string(str_in, memory) if str_in else ""
        if not _INT_STR_RE.match(s):
            cpu.regs[EAX] = _DISP_E_TYPEMISMATCH
            cleanup_stdcall(cpu, memory, 16)
            return
        val = int(s, 10)
        if not (-2147483648 <= val <= 2147483647):
            cpu.regs[EAX] = _DISP_E_OVERFLOW
            cleanup_stdcall(cpu, memory, 16)
            return
        memory.write32(pl_out, val & 0xFFFFFFFF)
        cpu.regs[EAX] = S_OK
        cleanup_stdcall(cpu, memory, 16)

    _ole_ord(64, _VarI4FromStr)
    stubs.register_handler("oleaut32.dll", "VarI4FromStr", _VarI4FromStr)

    # Ordinal 84 — VarR8FromStr(LPCOLESTR strIn, LCID lcid, ULONG dwFlags,
    # double *pdblOut) -> HRESULT. Same live call chain as VarI4FromStr
    # above (MSJET35.DLL's expression parser evaluating a numeric literal
    # -- Jet's expression evaluator tries the integer conversion first and
    # falls back to a real-number conversion). Same scope discipline: only
    # the well-defined, non-locale-specific case -- standard invariant-
    # culture decimal/scientific float literal, period as decimal point --
    # is implemented; anything else returns the real DISP_E_TYPEMISMATCH
    # HRESULT rather than guessing at locale-specific formats never
    # observed live. Python's float() accepts things like "inf"/"nan"
    # that aren't valid numeric string literals in this sense, so
    # validate the shape with a regex first rather than trust float()
    # directly.
    _FLOAT_STR_RE = re.compile(r"^\s*[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\s*$")

    def _write_f64(addr: int, val: float) -> None:
        # No 64-bit memory primitive exists (Memory only has 8/16/32-bit
        # read/write) -- pack as real IEEE-754 little-endian bytes and
        # write as two 32-bit words rather than guess at a wider API.
        raw = struct.pack("<d", val)
        memory.write32(addr, int.from_bytes(raw[0:4], "little"))
        memory.write32((addr + 4) & 0xFFFFFFFF, int.from_bytes(raw[4:8], "little"))

    def _VarR8FromStr(cpu: "CPU") -> None:
        str_in   = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        pdbl_out = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        s = read_wide_string(str_in, memory) if str_in else ""
        if not _FLOAT_STR_RE.match(s):
            cpu.regs[EAX] = _DISP_E_TYPEMISMATCH
            cleanup_stdcall(cpu, memory, 16)
            return
        val = float(s)
        if math.isinf(val):
            cpu.regs[EAX] = _DISP_E_OVERFLOW
            cleanup_stdcall(cpu, memory, 16)
            return
        _write_f64(pdbl_out, val)
        cpu.regs[EAX] = S_OK
        cleanup_stdcall(cpu, memory, 16)

    _ole_ord(84, _VarR8FromStr)
    stubs.register_handler("oleaut32.dll", "VarR8FromStr", _VarR8FromStr)

    # Ordinal 94 — VarDateFromStr(LPCOLESTR strIn, LCID lcid, ULONG
    # dwFlags, DATE *pdateOut) -> HRESULT. Live-confirmed call: MSJET35.DLL's
    # expression parser evaluating a WHERE-clause date-literal comparison
    # ("(BrandedPart.MfgDate)<>#1/1/2010#") -- Jet's own tokenizer strips
    # the '#' delimiters before this call, confirmed via a temporary
    # diagnostic probe (real strIn = '1/1/2010' exactly, no time component,
    # no 2-digit year). Only that well-defined M/D/YYYY numeric shape is
    # implemented (4-digit year, '/' separator, real calendar validation
    # via datetime.date rather than hand-rolled day-per-month tables);
    # anything else returns the real DISP_E_TYPEMISMATCH HRESULT rather
    # than guessing at locale-specific date formats never observed live.
    #
    # An OLE Automation DATE is a double: whole part = days since
    # 1899-12-30 (day 0), fractional part = time-of-day (always 0.0 here).
    # Real OLE dates carry a documented Lotus-1-2-3-compatibility quirk:
    # 1900 is (incorrectly) treated as a leap year, so a real Gregorian
    # day-count is off by one for any date on/after 1900-03-01 -- corrected
    # by adding 1 in that case. This isn't a guess, it's documented
    # Microsoft behavior, and it matters here since the query does a real
    # <> comparison against a stored database date value.
    _DATE_STR_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")
    _OLE_DATE_EPOCH = date(1899, 12, 30)
    _OLE_DATE_LOTUS_QUIRK_CUTOFF = date(1900, 3, 1)

    def _VarDateFromStr(cpu: "CPU") -> None:
        str_in    = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        pdate_out = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        s = read_wide_string(str_in, memory) if str_in else ""
        m = _DATE_STR_RE.match(s)
        if not m:
            cpu.regs[EAX] = _DISP_E_TYPEMISMATCH
            cleanup_stdcall(cpu, memory, 16)
            return
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            parsed = date(year, month, day)
        except ValueError:
            cpu.regs[EAX] = _DISP_E_TYPEMISMATCH
            cleanup_stdcall(cpu, memory, 16)
            return
        days = (parsed - _OLE_DATE_EPOCH).days
        if parsed >= _OLE_DATE_LOTUS_QUIRK_CUTOFF:
            days += 1
        _write_f64(pdate_out, float(days))
        cpu.regs[EAX] = S_OK
        cleanup_stdcall(cpu, memory, 16)

    _ole_ord(94, _VarDateFromStr)
    stubs.register_handler("oleaut32.dll", "VarDateFromStr", _VarDateFromStr)

    # VarDateFromUdate(UDATE *pudateIn, ULONG dwFlags, DATE *pdateOut) -> HRESULT.
    # UDATE = SYSTEMTIME (8 WORDs: wYear,wMonth,wDayOfWeek,wDay,wHour,wMinute,
    # wSecond,wMilliseconds) + a trailing `long wDayOfYear` we don't need to
    # read. Reuses the same OLE DATE epoch/Lotus-leap-year-quirk math as
    # _VarDateFromStr above -- same output representation, different input
    # shape (a struct instead of a parsed string).
    def _VarDateFromUdate(cpu: "CPU") -> None:
        pudate_in = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        pdate_out = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        year   = memory.read16(pudate_in & 0xFFFFFFFF)
        month  = memory.read16((pudate_in + 2) & 0xFFFFFFFF)
        day    = memory.read16((pudate_in + 6) & 0xFFFFFFFF)
        hour   = memory.read16((pudate_in + 8) & 0xFFFFFFFF)
        minute = memory.read16((pudate_in + 10) & 0xFFFFFFFF)
        second = memory.read16((pudate_in + 12) & 0xFFFFFFFF)
        millis = memory.read16((pudate_in + 14) & 0xFFFFFFFF)
        try:
            parsed = date(year, month, day)
        except ValueError:
            cpu.regs[EAX] = _DISP_E_TYPEMISMATCH
            cleanup_stdcall(cpu, memory, 12)
            return
        days = (parsed - _OLE_DATE_EPOCH).days
        if parsed >= _OLE_DATE_LOTUS_QUIRK_CUTOFF:
            days += 1
        frac = (hour * 3600 + minute * 60 + second + millis / 1000.0) / 86400.0
        _write_f64(pdate_out, float(days) + frac)
        cpu.regs[EAX] = S_OK
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("oleaut32.dll", "VarDateFromUdate", _VarDateFromUdate)

    # VarUdateFromDate(DATE dateIn, ULONG dwFlags, UDATE *pudateOut) -> HRESULT.
    # The inverse of VarDateFromUdate above -- dateIn is passed BY VALUE (8
    # bytes on the stack, not a pointer), everything else mirrors it: same
    # OLE DATE epoch, same Lotus leap-year quirk, inverted. UDATE output is a
    # SYSTEMTIME (8 WORDs) followed by a `long wDayOfYear`; wDayOfWeek uses
    # Win32's Sunday=0 convention (Python's date.weekday() is Monday=0).
    def _VarUdateFromDate(cpu: "CPU") -> None:
        esp = cpu.regs[ESP]
        raw = struct.pack("<II", memory.read32((esp + 4) & 0xFFFFFFFF), memory.read32((esp + 8) & 0xFFFFFFFF))
        date_val = struct.unpack("<d", raw)[0]
        pudate_out = memory.read32((esp + 16) & 0xFFFFFFFF)
        days_frac, days_int = math.modf(date_val)
        days_int = int(days_int)
        candidate = _OLE_DATE_EPOCH + timedelta(days=days_int)
        if candidate >= _OLE_DATE_LOTUS_QUIRK_CUTOFF:
            candidate = _OLE_DATE_EPOCH + timedelta(days=days_int - 1)
        total_millis = round(abs(days_frac) * 86400000)
        hour, rem = divmod(total_millis, 3600000)
        minute, rem = divmod(rem, 60000)
        second, millis = divmod(rem, 1000)
        win_day_of_week = (candidate.weekday() + 1) % 7  # Python Mon=0 -> Win32 Sun=0
        memory.write16(pudate_out & 0xFFFFFFFF, candidate.year)
        memory.write16((pudate_out + 2) & 0xFFFFFFFF, candidate.month)
        memory.write16((pudate_out + 4) & 0xFFFFFFFF, win_day_of_week)
        memory.write16((pudate_out + 6) & 0xFFFFFFFF, candidate.day)
        memory.write16((pudate_out + 8) & 0xFFFFFFFF, hour)
        memory.write16((pudate_out + 10) & 0xFFFFFFFF, minute)
        memory.write16((pudate_out + 12) & 0xFFFFFFFF, second)
        memory.write16((pudate_out + 14) & 0xFFFFFFFF, millis)
        memory.write32((pudate_out + 16) & 0xFFFFFFFF, candidate.timetuple().tm_yday)
        cpu.regs[EAX] = S_OK
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("oleaut32.dll", "VarUdateFromDate", _VarUdateFromDate)

    # Ordinal 154 — LoadTypeLibEx(...) -> HRESULT
    #
    # Live-confirmed root cause of a real crash: expsrv.dll's own init
    # resolves LoadTypeLibEx via GetProcAddress-by-NAME (as every real
    # caller does -- it's a documented export, not an ordinal-only one)
    # and caches the result unconditionally, never NULL-checking it
    # before calling through it later (real Windows guarantees this
    # export always resolves, so real code has no reason to check).
    # GetProcAddress does a strict string lookup against whatever key(s)
    # a handler was registered under -- this only had the ordinal key,
    # so the by-name lookup returned NULL, and the later unconditional
    # call through that cached NULL/garbage pointer jumped to invalid
    # memory. Same bug class as VariantClear's Ordinal #9 (see
    # test_oleaut32_variant_clear.py), just the opposite direction: fix
    # is to register the SAME handler under both keys, never duplicate
    # logic so the two can't drift apart again.
    # TEMP (2026-08-25 cont'd x24): scoping WHICH vtable slot(s) a real
    # caller actually invokes on the returned ITypeLib*, before writing a
    # real MSFT-format parser + COM object for it (see TODO.md -- confirmed
    # every real call asks for szFile="expsrv.dll", i.e. it wants that DLL's
    # own embedded TYPELIB resource, not an arbitrary external file). Builds
    # a 20-slot "trap" vtable (covers IUnknown's 3 + a generous ITypeLib
    # method count) where each slot is a real Win32-handler trampoline
    # (reusing register_handler/get_handler_address exactly as any other
    # stub -- no new mechanism needed) that just logs its own slot index
    # and halts, so whichever slot real code calls first is unambiguous.
    # Remove this whole trap once the real implementation lands.
    # Real, documented ITypeLib vtable (oaidl.h) -- (arg_bytes, benign_return, name).
    # arg_bytes is the real stdcall cleanup size (not a guess) so execution
    # can continue correctly through a whole sequence of calls in one run.
    # Real, documented ITypeComp vtable (oaidl.h) -- called on the object
    # ITypeLib::GetTypeComp hands back (found live: real code doesn't call
    # ITypeLib::FindName directly, it goes through GetTypeComp then
    # ITypeComp::Bind/BindType instead).
    _TYPECOMP_VTABLE_SPEC = [
        (8,  0, "QueryInterface"),   # 0: (REFIID, void**)
        (0,  1, "AddRef"),           # 1: ()
        (0,  1, "Release"),         # 2: ()
        None,                        # 3: Bind -- handled specially below (real 6-arg signature, not the generic trap)
        (16, 0, "BindType"),         # 4: (LPOLESTR, ULONG, ITypeInfo**, ITypeComp**) -- 4 args
    ]

    def _make_generic_trap_slot(label: str, slot_index: int, arg_bytes: int, benign_return: int, name: str):
        def _slot(cpu: "CPU") -> None:
            esp = cpu.regs[ESP]
            n_args = arg_bytes // 4
            # [ESP+0]=return addr, [ESP+4]=this, [ESP+8..]=real args
            args_preview = [memory.read32((esp + 8 + i * 4) & 0xFFFFFFFF) for i in range(n_args)]
            logger.error(
                "com",
                f"[typelib-trap] {label} slot {slot_index} ({name}) called, args={[hex(a) for a in args_preview]}",
            )
            cpu.regs[EAX] = benign_return
            cleanup_stdcall(cpu, memory, 4 + arg_bytes)  # +4 for the implicit `this`
        return _slot

    for _i, _spec in enumerate(_TYPECOMP_VTABLE_SPEC):
        if _spec is None:
            continue  # slot3 (Bind) registered separately below
        _argb, _ret, _name = _spec
        stubs.register_handler("__typecomp_trap__", f"slot{_i}", _make_generic_trap_slot("ITypeComp", _i, _argb, _ret, _name))

    # expsrv.dll's real .tlb is the standard, publicly-documented Microsoft
    # Jet/VBA "Expression Service" built-in function library (Abs, CStr,
    # DateAdd, ...) -- confirmed live: every ITypeComp::Bind name this
    # emulator has ever seen requested (base name, or "_b_var_"/"_b_str_"
    # boxed-result variants of it) is one of these ~40 real, well-known
    # functions, not arbitrary/game-specific data. Real Jet SQL expression
    # evaluation is VARIANT-based throughout, so every param and return type
    # below is VT_VARIANT -- accurate for this context, not a simplification
    # of real per-function signatures.
    #
    # (required_params, optional_params) -- from public VBA/Access function
    # reference documentation.
    _EXPR_FUNCTIONS: dict[str, tuple[int, int]] = {
        "ABS": (1, 0), "ASC": (1, 0), "ATN": (1, 0),
        "CCUR": (1, 0), "CDBL": (1, 0), "CHOOSE": (1, 8), "CHR": (1, 0),
        "CINT": (1, 0), "CLNG": (1, 0), "COS": (1, 0), "CSNG": (1, 0),
        "CSTR": (1, 0), "CVDATE": (1, 0),
        "DATE": (0, 0), "DATEADD": (3, 0), "DATEDIFF": (3, 2),
        "DATEPART": (2, 2), "DATESERIAL": (3, 0), "DATEVALUE": (1, 0),
        "DAY": (1, 0),
        "EVAL": (1, 0), "EXP": (1, 0),
        "FIX": (1, 0), "FORMAT": (1, 3),
        "HOUR": (1, 0),
        "INSTR": (2, 2), "INT": (1, 0),
        "LCASE": (1, 0), "LEFT": (2, 0), "LEN": (1, 0), "LOG": (1, 0),
        "LTRIM": (1, 0),
        "MID": (2, 1), "MINUTE": (1, 0), "MONTH": (1, 0),
        "NOW": (0, 0),
        "RIGHT": (2, 0), "RND": (0, 1), "RTRIM": (1, 0),
        "SECOND": (1, 0), "SGN": (1, 0), "SIN": (1, 0), "SPACE": (1, 0),
        "SQR": (1, 0), "STR": (1, 0), "STRING": (2, 0), "SWITCH": (2, 12),
        "TAN": (1, 0), "TIME": (0, 0), "TIMESERIAL": (3, 0),
        "TIMEVALUE": (1, 0), "TRIM": (1, 0),
        "UCASE": (1, 0), "USER": (0, 0),
        "WEEKDAY": (1, 1),
        "YEAR": (1, 0),
    }

    VT_VARIANT = 12
    DESCKIND_FUNCDESC = 1
    FUNC_DISPATCH = 4
    INVOKE_FUNC = 1
    CC_STDCALL = 4

    def _write_typedesc(addr: int, vt: int) -> None:
        memory.write32(addr & 0xFFFFFFFF, 0)             # union (unused for simple VT_*)
        memory.write16((addr + 4) & 0xFFFFFFFF, vt)       # VARTYPE
        memory.write16((addr + 6) & 0xFFFFFFFF, 0)        # padding

    def _write_elemdesc(addr: int, vt: int) -> None:
        _write_typedesc(addr, vt)                          # tdesc: 8 bytes
        memory.write32((addr + 8) & 0xFFFFFFFF, 0)          # paramdesc.pparamdescex = NULL
        memory.write16((addr + 12) & 0xFFFFFFFF, 0)         # paramdesc.wParamFlags
        memory.write16((addr + 14) & 0xFFFFFFFF, 0)         # padding

    _funcdesc_cache: dict[str, int] = {}  # base function name -> heap FUNCDESC address

    def _build_funcdesc(base_name: str, memid: int) -> int:
        """Allocate a real FUNCDESC (+ its param ELEMDESC array) for one of
        the ~40 known expression-service functions, matching real oaidl.h
        layout exactly. Cached per base name -- every caller asking for the
        same function gets the same descriptor, matching how a real
        ITypeInfo's member table would behave."""
        cached = _funcdesc_cache.get(base_name)
        if cached is not None:
            return cached
        required, optional = _EXPR_FUNCTIONS[base_name]
        total_params = required + optional
        params_addr = state.simple_alloc(16 * total_params) if total_params else 0
        for i in range(total_params):
            _write_elemdesc((params_addr + i * 16) & 0xFFFFFFFF, VT_VARIANT)
        funcdesc_addr = state.simple_alloc(52)
        memory.write32(funcdesc_addr & 0xFFFFFFFF, memid)                     # memid
        memory.write32((funcdesc_addr + 4) & 0xFFFFFFFF, 0)                    # lprgscode
        memory.write32((funcdesc_addr + 8) & 0xFFFFFFFF, params_addr)          # lprgelemdescParam
        memory.write32((funcdesc_addr + 12) & 0xFFFFFFFF, FUNC_DISPATCH)       # funckind
        memory.write32((funcdesc_addr + 16) & 0xFFFFFFFF, INVOKE_FUNC)         # invkind
        memory.write32((funcdesc_addr + 20) & 0xFFFFFFFF, CC_STDCALL)          # callconv
        memory.write16((funcdesc_addr + 24) & 0xFFFFFFFF, required + optional) # cParams
        memory.write16((funcdesc_addr + 26) & 0xFFFFFFFF, optional)            # cParamsOpt
        memory.write16((funcdesc_addr + 28) & 0xFFFFFFFF, 0)                   # oVft
        memory.write16((funcdesc_addr + 30) & 0xFFFFFFFF, 0)                   # cScodes
        _write_elemdesc((funcdesc_addr + 32) & 0xFFFFFFFF, VT_VARIANT)         # elemdescFunc (return type)
        memory.write16((funcdesc_addr + 48) & 0xFFFFFFFF, 0)                   # wFuncFlags
        _funcdesc_cache[base_name] = funcdesc_addr
        return funcdesc_addr

    _next_memid = [0x60000000]  # arbitrary but stable, distinct per base function

    def _base_expr_name(sz_name: str) -> str | None:
        upper = sz_name.upper()
        for prefix in ("_B_VAR_", "_B_STR_"):
            if upper.startswith(prefix):
                upper = upper[len(prefix):]
                break
        return upper if upper in _EXPR_FUNCTIONS else None

    # Real, documented ITypeInfo vtable (oaidl.h). Bind's ppTInfo out-param
    # needs *something* real -- a caller that got DESCKIND_FUNCDESC may still
    # AddRef/Release it, or call GetFuncDesc separately. GetFuncDesc genuinely
    # forwards to the same FUNCDESC Bind already returned (real behavior, not
    # a fabricated one).
    #
    # GetDllEntry is ALSO handled specially, not by the generic trap: live
    # tracing showed msjet35.dll calls it immediately after a successful
    # Bind, presumably to fast-path directly into expsrv.dll's own C export
    # for the function rather than going through IDispatch::Invoke. Real
    # semantics: GetDllEntry only applies to FUNC_STATIC members;
    # our FUNCDESCs declare FUNC_DISPATCH (accurate for a VARIANT-based
    # expression-service dispatch member without reverse-engineering
    # expsrv.dll's real internal export names/ordinals), so the honest,
    # correct answer is TYPE_E_BADMODULEKIND -- not a fabricated success with
    # unset output params. This should route the real caller into its own
    # real IDispatch::Invoke fallback. Real numeric/string execution of these
    # ~40 functions via Invoke is NOT implemented (still the generic trap,
    # benign no-op) -- a correctness gap for computed-expression *results*,
    # separate from the crash this fix targets (which was about Bind never
    # succeeding at all, not about executing what it finds).
    #
    # arg_bytes verified against each method's real oaidl.h signature
    # (param count * 4) -- a wrong count here silently under/over-pops the
    # stack in cleanup_stdcall, corrupting the caller's frame a few
    # instructions later rather than failing at the call site itself. Caught
    # live: GetDllEntry(MEMBERID,INVOKEKIND,BSTR*,BSTR*,WORD*) is 5 args (20
    # bytes) -- an earlier wrong guess of 3 args (12 bytes) corrupted EBP a
    # few calls later (`EBP=0x60000001`, a memid value shifted into the wrong
    # stack slot by the resulting 8-byte misalignment). Re-audited every slot
    # below against the real signature after finding that one.
    TYPE_E_BADMODULEKIND = 0x8002802A

    def _typeinfo_get_dll_entry(cpu: "CPU") -> None:
        cpu.regs[EAX] = TYPE_E_BADMODULEKIND
        cleanup_stdcall(cpu, memory, 4 + 20)  # this(4) + 5 args(20)

    _TYPEINFO_VTABLE_SPEC = [
        (8,  0, "QueryInterface"), (0, 1, "AddRef"), (0, 1, "Release"),
        (4,  0, "GetTypeAttr"), (4, 0, "GetTypeComp"), None,  # 5: GetFuncDesc, handled specially
        (8,  0, "GetVarDesc"), (16, 0, "GetNames"), (8, 0, "GetRefTypeOfImplType"),
        (8,  0, "GetImplTypeFlags"), (12, 0, "GetIDsOfNames"), (28, 0, "Invoke"),
        (20, 0, "GetDocumentation"), None, (8, 0, "GetRefTypeInfo"),  # 13: GetDllEntry, handled specially
        (12, 0, "AddressOfMember"), (12, 0, "CreateInstance"), (8, 0, "GetMops"),
        (8,  0, "GetContainingTypeLib"), (4, 0, "ReleaseTypeAttr"),
        (4,  0, "ReleaseFuncDesc"), (4, 0, "ReleaseVarDesc"),
    ]

    def _typeinfo_get_func_desc(cpu: "CPU") -> None:
        esp = cpu.regs[ESP]
        this_ptr = memory.read32((esp + 4) & 0xFFFFFFFF)
        pp_funcdesc = memory.read32((esp + 12) & 0xFFFFFFFF)
        # `index` ([ESP+8]) is the ordinal into the type's member table, not
        # something we track -- every trap ITypeInfo this session hands out
        # is already scoped to exactly one function (the one Bind resolved
        # it for), so index 0 is the only value a correct caller would ever
        # pass. Return whichever FUNCDESC this object's own Bind call built,
        # found via the object->funcdesc side table.
        funcdesc_addr = _typeinfo_obj_funcdesc.get(this_ptr, 0)
        if pp_funcdesc:
            memory.write32(pp_funcdesc & 0xFFFFFFFF, funcdesc_addr)
        cpu.regs[EAX] = 0  # S_OK
        cleanup_stdcall(cpu, memory, 8)

    for _i, _spec in enumerate(_TYPEINFO_VTABLE_SPEC):
        if _spec is None:
            continue  # slot5 (GetFuncDesc), slot13 (GetDllEntry) registered separately
        _argb, _ret, _name = _spec
        stubs.register_handler("__typeinfo_trap__", f"slot{_i}", _make_generic_trap_slot("ITypeInfo", _i, _argb, _ret, _name))
    stubs.register_handler("__typeinfo_trap__", "slot13", _typeinfo_get_dll_entry)
    stubs.register_handler("__typeinfo_trap__", "slot5", _typeinfo_get_func_desc)

    _typeinfo_obj_funcdesc: dict[int, int] = {}  # trap ITypeInfo object addr -> its FUNCDESC addr
    _typeinfo_trap_objs: dict[int, int] = {}      # FUNCDESC addr -> cached trap ITypeInfo object addr

    def _get_typeinfo_trap_obj(funcdesc_addr: int = 0) -> int:
        cached = _typeinfo_trap_objs.get(funcdesc_addr)
        if cached is not None:
            return cached
        vtable_addr = state.simple_alloc(4 * len(_TYPEINFO_VTABLE_SPEC))
        for i in range(len(_TYPEINFO_VTABLE_SPEC)):
            memory.write32((vtable_addr + i * 4) & 0xFFFFFFFF, stubs.get_handler_address("__typeinfo_trap__", f"slot{i}"))
        obj_addr = state.simple_alloc(4)
        memory.write32(obj_addr & 0xFFFFFFFF, vtable_addr)
        _typeinfo_trap_objs[funcdesc_addr] = obj_addr
        _typeinfo_obj_funcdesc[obj_addr] = funcdesc_addr
        return obj_addr

    # ITypeComp::Bind(LPOLESTR szName, ULONG lHashVal, WORD wFlags,
    #                 ITypeInfo** ppTInfo, DESCKIND* pDescKind, BINDPTR* pBindPtr)
    # expsrv.dll's real .tlb is the Jet/VBA expression-function library (see
    # _EXPR_FUNCTIONS above) -- real names resolve to a real FUNCDESC now,
    # not just an honest "not found". This matters: msjet35.dll's
    # FUN_7a8a4975 only fully/correctly initializes its per-record fields on
    # Bind's *success* path; "not found" (however honestly reported) still
    # left three sibling fields as uninitialized stack garbage, which is what
    # this whole investigation traced the expsrv.dll ESI=0xFFFFFFFF crash to
    # (see changelog.md 2026-08-25 cont'd x31). A name genuinely outside this
    # ~40-function set (should not happen for expsrv.dll callers, but the
    # honest path is kept for anything unexpected) still gets a clean,
    # always-initialized DESCKIND_NONE/S_OK answer.
    def _bind_slot(cpu: "CPU") -> None:
        esp = cpu.regs[ESP]
        sz_name_ptr, l_hash_val, w_flags, pp_tinfo, p_desc_kind, p_bind_ptr = (
            memory.read32((esp + 8 + i * 4) & 0xFFFFFFFF) for i in range(6)
        )
        try:
            sz_name = read_wide_string(sz_name_ptr, memory) if sz_name_ptr else "(null)"
        except Exception as exc:
            sz_name = f"(unreadable: {exc})"
        base_name = _base_expr_name(sz_name) if sz_name_ptr else None
        if base_name is not None:
            if base_name not in _funcdesc_cache:
                _next_memid[0] += 1
            funcdesc_addr = _build_funcdesc(base_name, _next_memid[0])
            logger.info("com", f"ITypeComp::Bind({sz_name!r}) -> expression function {base_name!r}, FUNCDESC at 0x{funcdesc_addr:x}")
            if pp_tinfo:
                memory.write32(pp_tinfo & 0xFFFFFFFF, _get_typeinfo_trap_obj(funcdesc_addr))
            if p_desc_kind:
                memory.write32(p_desc_kind & 0xFFFFFFFF, DESCKIND_FUNCDESC)
            if p_bind_ptr:
                memory.write32(p_bind_ptr & 0xFFFFFFFF, funcdesc_addr)  # BINDPTR union: lpfuncdesc is slot 0
            cpu.regs[EAX] = 0  # S_OK
        else:
            logger.warn("com", f"ITypeComp::Bind({sz_name!r}) -- not one of the known expression-service functions, honestly reporting DESCKIND_NONE (not found)")
            if pp_tinfo:
                memory.write32(pp_tinfo & 0xFFFFFFFF, 0)
            if p_desc_kind:
                memory.write32(p_desc_kind & 0xFFFFFFFF, 0)  # DESCKIND_NONE
            if p_bind_ptr:
                memory.write32(p_bind_ptr & 0xFFFFFFFF, 0)
            cpu.regs[EAX] = 0  # S_OK -- "not found" is success, not failure, for Bind
        cleanup_stdcall(cpu, memory, 4 + 24)  # this(4) + 6 args(24)
    stubs.register_handler("__typecomp_trap__", "slot3", _bind_slot)

    _typecomp_trap_obj: list[int] = []  # lazily built once, cached (mutable cell since Python closures can't rebind an outer int)

    def _get_typecomp_trap_obj() -> int:
        if not _typecomp_trap_obj:
            vtable_addr = state.simple_alloc(4 * len(_TYPECOMP_VTABLE_SPEC))
            for i in range(len(_TYPECOMP_VTABLE_SPEC)):
                memory.write32((vtable_addr + i * 4) & 0xFFFFFFFF, stubs.get_handler_address("__typecomp_trap__", f"slot{i}"))
            obj_addr = state.simple_alloc(4)
            memory.write32(obj_addr, vtable_addr)
            _typecomp_trap_obj.append(obj_addr)
        return _typecomp_trap_obj[0]

    def _get_type_comp_slot(cpu: "CPU") -> None:
        esp = cpu.regs[ESP]
        ppv = memory.read32((esp + 8) & 0xFFFFFFFF)  # [ESP+4]=this, [ESP+8]=ppTComp
        logger.error("com", f"[typelib-trap] ITypeLib slot 8 (GetTypeComp) called, ppv=0x{ppv:x}")
        if ppv:
            memory.write32(ppv & 0xFFFFFFFF, _get_typecomp_trap_obj())
        cpu.regs[EAX] = 0  # S_OK
        cleanup_stdcall(cpu, memory, 8)  # this(4) + ppTComp(4)
    stubs.register_handler("__typelib_trap__", "slot8", _get_type_comp_slot)

    _TYPELIB_VTABLE_SPEC = [
        (8,  0,  "QueryInterface"),       # 0: (REFIID, void**) -- 2 args
        (0,  1,  "AddRef"),               # 1: () -- refcount-ish return
        (0,  1,  "Release"),              # 2: ()
        (4,  0,  "GetTypeInfoCount"),     # 3: (UINT*)
        (8,  0,  "GetTypeInfo"),          # 4: (UINT, ITypeInfo**)
        (8,  0,  "GetTypeInfoType"),      # 5: (UINT, TYPEKIND*)
        (8,  0,  "GetTypeInfoOfGuid"),    # 6: (REFGUID, ITypeInfo**)
        (4,  0,  "GetLibAttr"),           # 7: (TLIBATTR**)
        None,                             # 8: GetTypeComp -- handled specially above
        (20, 0,  "GetDocumentation"),     # 9: (int, BSTR*, BSTR*, DWORD*, BSTR*) -- 5 args
        (12, 0,  "IsName"),               # 10: (LPOLESTR, ULONG, BOOL*) -- 3 args
        (20, 0,  "FindName"),             # 11: (LPOLESTR, ULONG, ITypeInfo**, MEMBERID*, USHORT*) -- 5 args
        (4,  0,  "ReleaseTLibAttr"),      # 12: (TLIBATTR*)
    ]
    _TYPELIB_TRAP_SLOTS = len(_TYPELIB_VTABLE_SPEC)

    for _i, _spec in enumerate(_TYPELIB_VTABLE_SPEC):
        if _spec is None:
            continue  # slot8 already registered above
        _argb, _ret, _name = _spec
        stubs.register_handler("__typelib_trap__", f"slot{_i}", _make_generic_trap_slot("ITypeLib", _i, _argb, _ret, _name))

    def _ord154(cpu: "CPU") -> None:
        ppv = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if ppv:
            vtable_addr = state.simple_alloc(4 * _TYPELIB_TRAP_SLOTS)
            for i in range(_TYPELIB_TRAP_SLOTS):
                slot_addr = stubs.get_handler_address("__typelib_trap__", f"slot{i}")
                memory.write32((vtable_addr + i * 4) & 0xFFFFFFFF, slot_addr)
            obj_addr = state.simple_alloc(4)
            memory.write32(obj_addr, vtable_addr)
            memory.write32(ppv & 0xFFFFFFFF, obj_addr)
            logger.error("com", f"[typelib-trap] LoadTypeLibEx returning trap object at 0x{obj_addr:x} (vtable 0x{vtable_addr:x}, {_TYPELIB_TRAP_SLOTS} slots)")
        cpu.regs[EAX] = 0  # S_OK
        cleanup_stdcall(cpu, memory, 12)

    _ole_ord(154, _ord154)
    stubs.register_handler("oleaut32.dll", "LoadTypeLibEx", _ord154)

    # Ordinal 155 — RegisterTypeLib(...) -> HRESULT
    def _ord155(cpu: "CPU") -> None:
        logger.warn("com", "RegisterTypeLib (Ordinal 155) called — returning E_NOTIMPL (type library registration not implemented)")
        cpu.regs[EAX] = 0x80004001  # E_NOTIMPL
        cleanup_stdcall(cpu, memory, 12)

    _ole_ord(155, _ord155)
    stubs.register_handler("oleaut32.dll", "RegisterTypeLib", _ord155)

    # UnRegisterTypeLib(REFGUID libID, WORD wVerMajor, WORD wVerMinor,
    # LCID lcid, SYSKIND syskind) -> HRESULT. Part of the same
    # GetProcAddress-by-name probe chain as LoadTypeLibEx above
    # (expsrv.dll's init resolves DispCallFunc/LoadTypeLibEx/
    # UnRegisterTypeLib/CreateTypeLib2 in sequence, bailing the whole
    # chain if any single lookup fails) -- didn't exist under any key at
    # all before this.
    def _UnRegisterTypeLib(cpu: "CPU") -> None:
        logger.warn("com", "UnRegisterTypeLib called — returning E_NOTIMPL (type library registration not implemented)")
        cpu.regs[EAX] = 0x80004001  # E_NOTIMPL
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("oleaut32.dll", "UnRegisterTypeLib", _UnRegisterTypeLib)

    # CreateTypeLib2(SYSKIND syskind, LPCOLESTR szFile,
    # ICreateTypeLib2** ppctlib) -> HRESULT. Same probe chain as above.
    def _CreateTypeLib2(cpu: "CPU") -> None:
        logger.warn("com", "CreateTypeLib2 called — returning E_NOTIMPL (type library creation not implemented)")
        cpu.regs[EAX] = 0x80004001  # E_NOTIMPL
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("oleaut32.dll", "CreateTypeLib2", _CreateTypeLib2)

    # ── ole32.dll — COM initialisation ────────────────────────────────────────

    # CoInitialize(pvReserved) -> HRESULT
    def _CoInitialize(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # S_OK
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("ole32.dll", "CoInitialize", _CoInitialize)

    # CoInitializeEx(pvReserved, dwCoInit) -> HRESULT
    def _CoInitializeEx(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # S_OK
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("ole32.dll", "CoInitializeEx", _CoInitializeEx)

    # CoUninitialize() -> void
    def _CoUninitialize(cpu: "CPU") -> None:
        cleanup_stdcall(cpu, memory, 0)

    stubs.register_handler("ole32.dll", "CoUninitialize", _CoUninitialize)

    # ── ole32.dll — CoGetMalloc / IMalloc ───────────────────────────────────────
    # The process-wide task allocator real COM code fetches via CoGetMalloc.
    # Previously unimplemented entirely -- dao350.dll's DllGetClassObject
    # helper-object init (FUN_044947fc) calls this as its very first real
    # dependency and hit an [UNIMPLEMENTED] fatal halt long before reaching
    # QueryInterface/*ppv (see memory/status.md, "Current status 2026-07-22").
    # A single lazily-allocated singleton object is returned on every call,
    # AddRef'd each time, matching real CoGetMalloc semantics (always the same
    # IMalloc*, refcounted like any other COM interface). Alloc/Realloc/Free
    # ride the same state.simple_alloc bump allocator + heap_alloc_sizes
    # bookkeeping that HeapAlloc/HeapFree/HeapSize already use (kernel32_memory.py)
    # so GetSize/DidAlloc report real, consistent answers.

    IID_IUNKNOWN = "00000000-0000-0000-c000-000000000046"
    IID_IMALLOC  = "00000002-0000-0000-c000-000000000046"

    _imalloc_box = {"obj_addr": 0, "refcount": 0}

    def _com_method(name: str, handler, stack_arg_bytes: int) -> int:
        """Register an IMalloc vtable method ('this' pushed on stack as an
        implicit first arg, standard COM __stdcall ABI) and return its
        trampoline address."""
        def _h(cpu: "CPU") -> None:
            handler(cpu)
            cleanup_stdcall(cpu, memory, 4 + stack_arg_bytes)
        stubs.register_handler("ole32", name, _h)
        return stubs.get_handler_address("ole32", name) or 0

    def _imalloc_query_interface(cpu: "CPU") -> None:
        riid = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        ppv  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        riid_str = _read_guid_str(riid)
        if riid_str in (IID_IUNKNOWN, IID_IMALLOC):
            if ppv:
                memory.write32(ppv, _imalloc_box["obj_addr"])
            _imalloc_box["refcount"] += 1
            logger.info("com", f"IMalloc::QueryInterface({{{riid_str}}}) -> S_OK")
            cpu.regs[EAX] = S_OK
        else:
            if ppv:
                memory.write32(ppv, 0)
            logger.info("com", f"IMalloc::QueryInterface({{{riid_str}}}) -> E_NOINTERFACE")
            cpu.regs[EAX] = E_NOINTERFACE

    def _imalloc_add_ref(cpu: "CPU") -> None:
        _imalloc_box["refcount"] += 1
        cpu.regs[EAX] = _imalloc_box["refcount"] & 0xFFFFFFFF

    def _imalloc_release(cpu: "CPU") -> None:
        _imalloc_box["refcount"] = max(0, _imalloc_box["refcount"] - 1)
        cpu.regs[EAX] = _imalloc_box["refcount"] & 0xFFFFFFFF

    def _imalloc_alloc(cpu: "CPU") -> None:
        cb = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        addr = state.simple_alloc(cb or 1)
        cpu.regs[EAX] = addr & 0xFFFFFFFF

    def _imalloc_realloc(cpu: "CPU") -> None:
        pv = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        cb = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if pv == 0:
            cpu.regs[EAX] = state.simple_alloc(cb or 1) & 0xFFFFFFFF if cb else 0
            return
        if cb == 0:
            state.heap_alloc_sizes.pop(pv, None)
            cpu.regs[EAX] = 0
            return
        old_size = state.heap_alloc_sizes.get(pv)
        if old_size is None:
            logger.error("com", f"IMalloc::Realloc — untracked pointer 0x{pv:08x} — halting")
            cpu.halted = True
            cpu.fatal_halt = True
            return
        new_addr = state.simple_alloc(cb)
        for i in range(min(old_size, cb)):
            memory.write8(new_addr + i, memory.read8(pv + i))
        state.heap_alloc_sizes.pop(pv, None)
        cpu.regs[EAX] = new_addr & 0xFFFFFFFF

    def _imalloc_free(cpu: "CPU") -> None:
        pv = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if pv == 0:
            return
        if pv not in state.heap_alloc_sizes:
            logger.error("com", f"IMalloc::Free — untracked pointer 0x{pv:08x} — halting")
            cpu.halted = True
            cpu.fatal_halt = True
            return
        del state.heap_alloc_sizes[pv]

    def _imalloc_get_size(cpu: "CPU") -> None:
        pv = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        sz = state.heap_alloc_sizes.get(pv)
        cpu.regs[EAX] = sz if sz is not None else 0xFFFFFFFF

    def _imalloc_did_alloc(cpu: "CPU") -> None:
        pv = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        cpu.regs[EAX] = 1 if pv in state.heap_alloc_sizes else 0

    def _imalloc_heap_minimize(cpu: "CPU") -> None:
        pass  # no-op, void return — bump allocator has nothing to minimize

    def _get_imalloc_obj() -> int:
        if _imalloc_box["obj_addr"] == 0:
            vtable_addr = state.simple_alloc(9 * 4)
            obj_addr = state.simple_alloc(4)
            memory.write32(obj_addr, vtable_addr)
            slots = [
                _com_method("IMalloc::QueryInterface", _imalloc_query_interface, 8),
                _com_method("IMalloc::AddRef",         _imalloc_add_ref,         0),
                _com_method("IMalloc::Release",        _imalloc_release,         0),
                _com_method("IMalloc::Alloc",           _imalloc_alloc,          4),
                _com_method("IMalloc::Realloc",         _imalloc_realloc,        8),
                _com_method("IMalloc::Free",            _imalloc_free,           4),
                _com_method("IMalloc::GetSize",         _imalloc_get_size,       4),
                _com_method("IMalloc::DidAlloc",        _imalloc_did_alloc,      4),
                _com_method("IMalloc::HeapMinimize",    _imalloc_heap_minimize,  0),
            ]
            for i, addr in enumerate(slots):
                memory.write32(vtable_addr + i * 4, addr)
            _imalloc_box["obj_addr"] = obj_addr
            logger.info("com", f"IMalloc singleton created @ 0x{obj_addr:08x} (vtable @ 0x{vtable_addr:08x})")
        return _imalloc_box["obj_addr"]

    # CoGetMalloc(dwMemContext, ppMalloc) -> HRESULT
    def _CoGetMalloc(cpu: "CPU") -> None:
        ppmalloc = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        obj_addr = _get_imalloc_obj()
        _imalloc_box["refcount"] += 1
        if ppmalloc:
            memory.write32(ppmalloc, obj_addr)
        logger.info("com", f"CoGetMalloc -> S_OK *ppMalloc=0x{obj_addr:08x}")
        cpu.regs[EAX] = S_OK
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("ole32.dll", "CoGetMalloc", _CoGetMalloc)

    # ── oleaut32.dll ordinal 202 — CreateErrorInfo ──────────────────────────────
    # CreateErrorInfo(ICreateErrorInfo **pperrinfo) -> HRESULT
    # Real OLEAUT32.dll's generic error object implements both
    # ICreateErrorInfo (Set* methods, filled in by the creator) and IErrorInfo
    # (Get* methods, read by SetErrorInfo/GetErrorInfo consumers) on one
    # object via two vtables at a fixed +4 offset -- the standard C++
    # multiple-inheritance "this-adjustor" layout, not an emulator-specific
    # simplification. A fresh object is allocated per call (unlike the
    # IMalloc singleton above); only the two vtables are built once and
    # shared across every instance, since they're stateless function tables.

    IID_ICREATEERRORINFO = "22f03340-547d-101b-8e65-08002b2bd119"
    IID_IERRORINFO        = "1cf2b120-547d-101b-8e65-08002b2bd119"

    # Object layout: +0x00 vtable_create, +0x04 vtable_errinfo, +0x08 refcount,
    # +0x0C guid(16), +0x1C source BSTR, +0x20 description BSTR,
    # +0x24 helpfile BSTR, +0x28 helpcontext DWORD. Size 0x2C.
    _ERRINFO_OBJ_SIZE = 0x2C

    def _errinfo_qi_core(obj_addr: int, riid_str: str, ppv: int) -> int:
        if riid_str in (IID_IUNKNOWN, IID_ICREATEERRORINFO):
            target = obj_addr
        elif riid_str == IID_IERRORINFO:
            target = obj_addr + 4
        else:
            target = None
        if ppv:
            memory.write32(ppv, target if target is not None else 0)
        if target is not None:
            memory.write32(obj_addr + 8, (memory.read32(obj_addr + 8) + 1) & 0xFFFFFFFF)
        return S_OK if target is not None else E_NOINTERFACE

    def _errinfo_addref_core(obj_addr: int) -> int:
        rc = (memory.read32(obj_addr + 8) + 1) & 0xFFFFFFFF
        memory.write32(obj_addr + 8, rc)
        return rc

    def _errinfo_release_core(obj_addr: int) -> int:
        rc = max(0, memory.read32(obj_addr + 8) - 1)
        memory.write32(obj_addr + 8, rc)
        return rc

    # ICreateErrorInfo face (this == obj_addr)
    def _errinfo_create_qi(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        riid = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        ppv  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        riid_str = _read_guid_str(riid)
        result = _errinfo_qi_core(this_addr, riid_str, ppv)
        logger.info("com", f"ICreateErrorInfo(0x{this_addr:x})::QueryInterface({{{riid_str}}}) -> {'S_OK' if result == S_OK else 'E_NOINTERFACE'}")
        cpu.regs[EAX] = result

    def _errinfo_create_addref(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = _errinfo_addref_core(this_addr)

    def _errinfo_create_release(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = _errinfo_release_core(this_addr)

    def _errinfo_set_guid(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        rguid = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        for i in range(16):
            memory.write8(this_addr + 0x0C + i, memory.read8(rguid + i) if rguid else 0)
        logger.info("com", f"ICreateErrorInfo(0x{this_addr:x})::SetGUID({{{_read_guid_str(rguid) if rguid else 'NULL'}}})")
        cpu.regs[EAX] = S_OK

    def _errinfo_set_source(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        sz = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        memory.write32(this_addr + 0x1C, sz)
        from tew.api._state import read_wide_string
        text = read_wide_string(sz, memory) if sz else "(null)"
        logger.info("com", f'ICreateErrorInfo(0x{this_addr:x})::SetSource("{text}")')
        cpu.regs[EAX] = S_OK

    def _errinfo_set_description(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        sz = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        memory.write32(this_addr + 0x20, sz)
        from tew.api._state import read_wide_string
        text = read_wide_string(sz, memory) if sz else "(null)"
        logger.info("com", f'ICreateErrorInfo(0x{this_addr:x})::SetDescription("{text}")')
        cpu.regs[EAX] = S_OK

    def _errinfo_set_help_file(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        sz = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        memory.write32(this_addr + 0x24, sz)
        logger.info("com", f"ICreateErrorInfo(0x{this_addr:x})::SetHelpFile(0x{sz:x})")
        cpu.regs[EAX] = S_OK

    def _errinfo_set_help_context(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        ctx = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        memory.write32(this_addr + 0x28, ctx)
        logger.info("com", f"ICreateErrorInfo(0x{this_addr:x})::SetHelpContext({ctx})")
        cpu.regs[EAX] = S_OK

    # IErrorInfo face (this == obj_addr + 4; adjust back to obj_addr)
    def _errinfo_face2_qi(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        riid = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        ppv  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        riid_str = _read_guid_str(riid)
        result = _errinfo_qi_core((this_addr - 4) & 0xFFFFFFFF, riid_str, ppv)
        logger.info("com", f"IErrorInfo(0x{this_addr:x})::QueryInterface({{{riid_str}}}) -> {'S_OK' if result == S_OK else 'E_NOINTERFACE'}")
        cpu.regs[EAX] = result

    def _errinfo_face2_addref(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = _errinfo_addref_core((this_addr - 4) & 0xFFFFFFFF)

    def _errinfo_face2_release(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = _errinfo_release_core((this_addr - 4) & 0xFFFFFFFF)

    def _errinfo_get_guid(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        pguid = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        obj_addr = (this_addr - 4) & 0xFFFFFFFF
        if pguid:
            for i in range(16):
                memory.write8(pguid + i, memory.read8(obj_addr + 0x0C + i))
        logger.info("com", f"IErrorInfo(0x{this_addr:x})::GetGUID() -> {{{_read_guid_str(obj_addr + 0x0C)}}}")
        cpu.regs[EAX] = S_OK

    def _errinfo_get_source(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        pbstr = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        obj_addr = (this_addr - 4) & 0xFFFFFFFF
        if pbstr:
            memory.write32(pbstr, memory.read32(obj_addr + 0x1C))
        logger.info("com", f"IErrorInfo(0x{this_addr:x})::GetSource() -> 0x{memory.read32(obj_addr + 0x1C):x}")
        cpu.regs[EAX] = S_OK

    def _errinfo_get_description(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        pbstr = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        obj_addr = (this_addr - 4) & 0xFFFFFFFF
        if pbstr:
            memory.write32(pbstr, memory.read32(obj_addr + 0x20))
        logger.info("com", f"IErrorInfo(0x{this_addr:x})::GetDescription() -> 0x{memory.read32(obj_addr + 0x20):x}")
        cpu.regs[EAX] = S_OK

    def _errinfo_get_help_file(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        pbstr = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        obj_addr = (this_addr - 4) & 0xFFFFFFFF
        if pbstr:
            memory.write32(pbstr, memory.read32(obj_addr + 0x24))
        cpu.regs[EAX] = S_OK

    def _errinfo_get_help_context(cpu: "CPU") -> None:
        this_addr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        pdw = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        obj_addr = (this_addr - 4) & 0xFFFFFFFF
        if pdw:
            memory.write32(pdw, memory.read32(obj_addr + 0x28))
        cpu.regs[EAX] = S_OK

    _errinfo_vtables_box = {"create": 0, "err": 0}

    def _ensure_errinfo_vtables() -> tuple[int, int]:
        if _errinfo_vtables_box["create"] == 0:
            create_vt = state.simple_alloc(8 * 4)
            err_vt    = state.simple_alloc(8 * 4)
            create_slots = [
                _com_method("ICreateErrorInfo::QueryInterface", _errinfo_create_qi,        8),
                _com_method("ICreateErrorInfo::AddRef",         _errinfo_create_addref,    0),
                _com_method("ICreateErrorInfo::Release",        _errinfo_create_release,   0),
                _com_method("ICreateErrorInfo::SetGUID",        _errinfo_set_guid,         4),
                _com_method("ICreateErrorInfo::SetSource",      _errinfo_set_source,       4),
                _com_method("ICreateErrorInfo::SetDescription", _errinfo_set_description,  4),
                _com_method("ICreateErrorInfo::SetHelpFile",    _errinfo_set_help_file,    4),
                _com_method("ICreateErrorInfo::SetHelpContext", _errinfo_set_help_context, 4),
            ]
            for i, addr in enumerate(create_slots):
                memory.write32(create_vt + i * 4, addr)

            err_slots = [
                _com_method("IErrorInfo::QueryInterface",  _errinfo_face2_qi,         8),
                _com_method("IErrorInfo::AddRef",          _errinfo_face2_addref,     0),
                _com_method("IErrorInfo::Release",         _errinfo_face2_release,    0),
                _com_method("IErrorInfo::GetGUID",         _errinfo_get_guid,         4),
                _com_method("IErrorInfo::GetSource",       _errinfo_get_source,       4),
                _com_method("IErrorInfo::GetDescription",  _errinfo_get_description,  4),
                _com_method("IErrorInfo::GetHelpFile",     _errinfo_get_help_file,    4),
                _com_method("IErrorInfo::GetHelpContext",  _errinfo_get_help_context, 4),
            ]
            for i, addr in enumerate(err_slots):
                memory.write32(err_vt + i * 4, addr)

            _errinfo_vtables_box["create"] = create_vt
            _errinfo_vtables_box["err"] = err_vt
        return _errinfo_vtables_box["create"], _errinfo_vtables_box["err"]

    def _CreateErrorInfo(cpu: "CPU") -> None:
        pperrinfo = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        create_vt, err_vt = _ensure_errinfo_vtables()
        obj_addr = state.simple_alloc(_ERRINFO_OBJ_SIZE)
        memory.write32(obj_addr, create_vt)
        memory.write32(obj_addr + 4, err_vt)
        memory.write32(obj_addr + 8, 1)   # initial refcount = 1 (caller's own ref)
        if pperrinfo:
            memory.write32(pperrinfo, obj_addr)
        logger.info("com", f"CreateErrorInfo -> S_OK *pperrinfo=0x{obj_addr:08x}")
        cpu.regs[EAX] = S_OK
        cleanup_stdcall(cpu, memory, 4)

    _ole_ord(202, _CreateErrorInfo)

    # ── oleaut32.dll ordinal 201 — SetErrorInfo ─────────────────────────────────
    # SetErrorInfo(ULONG dwReserved, IErrorInfo *perrinfo) -> HRESULT
    # Real OLE32's implementation (re-exported by OLEAUT32 by ordinal on WinXP)
    # stores perrinfo as the calling thread's current COM error object,
    # releasing whatever was set before and AddRef'ing the new one. Always
    # returns S_OK. Stored per-thread (matching this file's existing
    # tls_current_thread_id() pattern) since real SetErrorInfo/GetErrorInfo
    # state is per-thread, not process-global.
    def _SetErrorInfo(cpu: "CPU") -> None:
        perrinfo = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        tid = state.tls_current_thread_id()
        prev = state.error_info_store.get(tid, 0)
        if prev:
            _errinfo_release_core((prev - 4) & 0xFFFFFFFF)
        if perrinfo:
            _errinfo_addref_core((perrinfo - 4) & 0xFFFFFFFF)
        state.error_info_store[tid] = perrinfo
        logger.info("com", f"SetErrorInfo(perrinfo=0x{perrinfo:08x}) -> S_OK")
        cpu.regs[EAX] = S_OK
        cleanup_stdcall(cpu, memory, 8)

    _ole_ord(201, _SetErrorInfo)

    # ── ole32.dll — COM activation ─────────────────────────────────────────────
    # Registry-driven, like real Windows: CoGetClassObject/CoCreateInstance
    # look the CLSID up under hkcr\clsid\{...}\inprocserver32 (see
    # registry.json). A CLSID nobody registered fails honestly with
    # REGDB_E_CLASSNOTREG, exactly as an unmodified real install would. A
    # CLSID registered to a server this emulator doesn't actually have also
    # fails honestly, rather than faking success. For servers we do have
    # (_KNOWN_COM_SERVERS), the real DLL is loaded from disk and its real
    # DllGetClassObject is invoked via _invoke_emulated_proc — genuine COM
    # activation against real, period-correct compiled code, not a Python
    # stand-in.

    # Local import to avoid a circular dependency (user32_handlers.py does
    # not import oleaut32_handlers.py, but this keeps the same
    # import-inside-function convention crt_handlers.py/kernel32_system.py
    # already use for this exact same import).
    from tew.api.user32_handlers import _invoke_emulated_proc, _get_dialog_sentinel

    def _read_guid_str(addr: int) -> str:
        d1 = memory.read32(addr)
        d2 = memory.read16(addr + 4)
        d3 = memory.read16(addr + 6)
        d4 = bytes(memory.read8(addr + 8 + i) for i in range(8))
        return (f"{d1:08x}-{d2:04x}-{d3:04x}-"
                f"{d4[0]:02x}{d4[1]:02x}-"
                f"{d4[2]:02x}{d4[3]:02x}{d4[4]:02x}{d4[5]:02x}{d4[6]:02x}{d4[7]:02x}")

    def _write_guid(addr: int, guid: str) -> None:
        memory.write32(addr, int(guid[0:8], 16))
        memory.write16(addr + 4, int(guid[9:13], 16))
        memory.write16(addr + 6, int(guid[14:18], 16))
        for i, b in enumerate(bytes.fromhex(guid[19:23] + guid[24:36])):
            memory.write8(addr + 8 + i, b)

    # Scratch IID_IClassFactory, needed for the internal
    # DllGetClassObject(..., IID_IClassFactory, ...) call every real
    # CoCreateInstance makes before dispatching to CreateInstance. Allocated
    # lazily on first use (like user32_handlers._get_dialog_sentinel) so
    # registration doesn't touch the heap for emulator setups/tests that
    # never exercise COM activation at all.
    _iid_iclassfactory_addr_box = [0]

    def _get_iid_iclassfactory_addr() -> int:
        if _iid_iclassfactory_addr_box[0] == 0:
            addr = state.simple_alloc(16)
            _write_guid(addr, "00000001-0000-0000-C000-000000000046")
            _iid_iclassfactory_addr_box[0] = addr
        return _iid_iclassfactory_addr_box[0]

    # Scratch IID_IUnknown, needed by CoCreateInstanceEx's initial
    # DllGetClassObject(..., IID_IClassFactory, ...) -> CreateInstance(...,
    # IID_IUnknown, ...) step before it QueryInterfaces out each of the
    # caller's actually-requested interfaces. Same lazy-allocation pattern
    # as _get_iid_iclassfactory_addr above.
    _iid_iunknown_addr_box = [0]

    def _get_iid_iunknown_addr() -> int:
        if _iid_iunknown_addr_box[0] == 0:
            addr = state.simple_alloc(16)
            _write_guid(addr, "00000000-0000-0000-C000-000000000046")
            _iid_iunknown_addr_box[0] = addr
        return _iid_iunknown_addr_box[0]

    def _resolve_com_server(clsid_addr: int) -> "str | None":
        key = f"hkcr\\clsid\\{{{_read_guid_str(clsid_addr)}}}\\inprocserver32"
        entry = state.registry_values.get(key, {}).get("")
        return str(entry.value) if entry is not None else None

    def _resolve_progid_clsid(progid: str) -> "str | None":
        """ProgID -> CLSID string (no braces), per HKCR\\<ProgID>\\CLSID's
        default value. Registry-driven, same honest-failure philosophy as
        _resolve_com_server: an unregistered ProgID returns None rather
        than fabricating a CLSID, matching a real unmodified install."""
        key = f"hkcr\\{progid.lower()}\\clsid"
        entry = state.registry_values.get(key, {}).get("")
        if entry is None:
            return None
        raw = str(entry.value)
        return raw.strip("{}")

    def _clsid_has_server(clsid_str: str) -> bool:
        """True if HKCR\\CLSID\\{clsid}\\ has a real in-proc or local
        server registration -- the extra check CLSIDFromProgIDEx makes
        beyond CLSIDFromProgID's plain ProgID->CLSID string lookup."""
        base = f"hkcr\\clsid\\{{{clsid_str}}}\\"
        return any(
            key.startswith(base) and key.endswith(("inprocserver32", "localserver32"))
            for key in state.registry_values
        )

    def _ensure_dll_ready(dll_filename: str, cpu: "CPU") -> "LoadedDLL | None":
        if dll_loader is None:
            return None
        was_loaded = dll_loader.get_dll(dll_filename) is not None
        loaded = dll_loader.load_dll(dll_filename, memory)
        if loaded is None:
            return None
        if not was_loaded:
            # patch_dll_iats is itself incremental now (only processes
            # entries added since its own last call), but there's still no
            # reason to call it at all when this particular DLL was already
            # loaded -- an already-loaded DLL adds no new IAT entries, so
            # the call would just be a guaranteed-empty no-op every time.
            dll_loader.patch_dll_iats(memory, stubs)
        if not was_loaded and loaded.entry_point != 0:
            sentinel = _get_dialog_sentinel(state, memory)
            handle = loaded.base_address & 0xFFFFFFFF
            result = _invoke_emulated_proc(
                cpu, memory, loaded.entry_point, [handle, 1, 0], sentinel,
                # Default max_steps=5_000_000 was too small here: with real
                # cooperative threads (timers etc.) running while this
                # thread is swapped out, the whole budget was routinely
                # exhausted before this thread ever got back to finish its
                # own call -- see _invoke_emulated_proc's "max_steps
                # exhausted" diagnostic for the fix that stopped this from
                # silently returning garbage; this raises the budget so the
                # call actually gets a real chance to complete instead of
                # relying on that fallback every time.
                max_steps=50_000_000,
                scheduler=state.scheduler)
            logger.info("com", f"{dll_filename}: DllMain(DLL_PROCESS_ATTACH) -> {result}")
            if result == 0:
                # Real LoadLibrary treats a FALSE DllMain(DLL_PROCESS_ATTACH)
                # as a load failure and never calls further into the DLL.
                # Calling DllGetClassObject on a DLL that just told us its
                # own init failed is undefined territory -- don't.
                logger.error("com",
                    f"{dll_filename}: DllMain(DLL_PROCESS_ATTACH) returned FALSE"
                    " -- treating load as failed")
                return None
        return loaded

    def _call_dll_get_class_object(
        cpu: "CPU", loaded: "LoadedDLL", rclsid: int, riid: int, ppv: int,
    ) -> int:
        addr = dll_loader.get_export_address(loaded.name, "DllGetClassObject") if dll_loader else None
        if not addr:
            logger.warn("com", f"{loaded.name}: no DllGetClassObject export found")
            return REGDB_E_CLASSNOTREG
        sentinel = _get_dialog_sentinel(state, memory)
        return _invoke_emulated_proc(
            cpu, memory, addr, [rclsid, riid, ppv], sentinel,
            scheduler=state.scheduler) & 0xFFFFFFFF

    def _hr_failed(hr: int) -> bool:
        """HRESULT failure = bit 31 set. hr here is always an unsigned
        32-bit Python int (masked by every producer below), so `hr < 0`
        would never trigger -- REGDB_E_CLASSNOTREG (0x80040154) is a large
        *positive* Python int, not negative."""
        return bool(hr & 0x80000000)

    def _dispatch_com_method(cpu: "CPU", obj_addr: int, slot: int, args: list[int]) -> int:
        vtable = memory.read32(obj_addr)
        method_addr = memory.read32((vtable + slot * 4) & 0xFFFFFFFF)
        sentinel = _get_dialog_sentinel(state, memory)
        return _invoke_emulated_proc(
            cpu, memory, method_addr, [obj_addr] + args, sentinel,
            scheduler=state.scheduler) & 0xFFFFFFFF

    # DispCallFunc(pvInstance, oVft, cc, vtReturn, cActuals, prgvt, prgpvarg,
    #              pvargResult) -> HRESULT
    #
    # Real, generic late-bound function invocation used throughout OLE
    # Automation/VBA -- expsrv.dll's own init (ordinal #2000) probes for
    # this via GetProcAddress before doing anything else (see the DAO-3075
    # investigation, status.md "2026-08-18"). Was unimplemented; expsrv.dll
    # walked away holding a NULL pointer for it. Marshals a VARIANT
    # argument array into a real guest stack frame, then invokes the
    # resolved target through the same _invoke_emulated_proc nested-call
    # mechanism already used for DllMain/DllGetClassObject calls above.
    #
    # Target resolution (real DispCallFunc semantics): if pvInstance != 0,
    # the target is *(int*)(*(int*)pvInstance + oVft) -- a vtable dispatch,
    # oVft a BYTE offset (same pattern as _dispatch_com_method above, just
    # without the *4 index scaling -- oVft is already byte-granular). If
    # pvInstance == 0, oVft IS the function address directly.
    #
    # Calling convention: only CC_CDECL(1) and CC_STDCALL(4) supported --
    # the only two realistic on x86 Win32 COM/automation, and the only two
    # _invoke_emulated_proc's push-args-right-to-left mechanism can express
    # (stdcall/cdecl differ only in who cleans the stack afterward, which
    # doesn't matter here since the full CPU state is restored after every
    # call regardless). Anything else halts loudly rather than guess at a
    # different argument order.
    _CC_CDECL = 1
    _CC_STDCALL = 4

    _VT_UI1 = 17
    _VT_UI2 = 18
    _VT_UI4 = 19
    _VT_I1  = 16
    _VT_INT  = 22
    _VT_UINT = 23
    _VT_ERROR = 10
    _VT_R4 = 4
    _VT_R8 = 5
    _VT_I8  = 20
    _VT_UI8 = 21
    _VT_CY  = 6
    _VT_DATE = 7
    _VT_BSTR = 8
    _VT_DISPATCH = 9
    _VT_UNKNOWN = 13
    _VT_VOID = 24
    _VT_BYREF_FLAG = 0x4000
    _VT_TYPEMASK = 0x0FFF

    # Argument marshaling: for each of cActuals arguments, reads prgvt[i]
    # (VARTYPE, a packed 2-byte array -- real Win32 header type) and
    # *prgpvarg[i] (a VARIANTARG, 16-byte layout: vt at +0, value union at
    # +8 -- same layout _VariantChangeType above already relies on),
    # extracts the raw value per VARTYPE, and produces the flat list of
    # 32-bit words _invoke_emulated_proc expects (its own docstring: "args
    # is the argument list in left-to-right (C) order"). Multi-word types
    # (VT_R8/VT_CY/VT_DATE/VT_I8/VT_UI8, all 8 bytes) contribute two words,
    # low dword first -- matching how _invoke_emulated_proc's own
    # right-to-left push reversal lays a real 8-byte argument out on the
    # stack (low dword ends up at the lower address, [ESP+4], as x86
    # requires). VT_BYREF pushes the VARIANT's stored pointer directly,
    # regardless of base type, since the callee expects a pointer parameter
    # either way -- no need to dereference or know the pointee's real type.
    def _dispcallfunc_arg_words(pvarg: int, vt: int) -> "list[int] | None":
        if vt & _VT_BYREF_FLAG:
            return [memory.read32((pvarg + 8) & 0xFFFFFFFF)]
        base = vt & _VT_TYPEMASK
        if base in (_VT_I2, _VT_UI2, _VT_BOOL):
            return [memory.read16((pvarg + 8) & 0xFFFFFFFF)]
        if base in (_VT_I1, _VT_UI1):
            return [memory.read8((pvarg + 8) & 0xFFFFFFFF)]
        if base in (_VT_I4, _VT_UI4, _VT_INT, _VT_UINT, _VT_ERROR,
                    _VT_BSTR, _VT_DISPATCH, _VT_UNKNOWN, _VT_R4):
            # VT_R4 passed as its raw 4-byte bit pattern -- correct for a
            # real, explicitly-`float`-typed C parameter on x86 cdecl/
            # stdcall (no register/FPU involvement, unlike varargs'
            # float->double promotion, which doesn't apply to a fixed
            # non-vararg signature like this).
            return [memory.read32((pvarg + 8) & 0xFFFFFFFF)]
        if base in (_VT_R8, _VT_I8, _VT_UI8, _VT_CY, _VT_DATE):
            return [memory.read32((pvarg + 8) & 0xFFFFFFFF),
                    memory.read32((pvarg + 12) & 0xFFFFFFFF)]
        return None

    # Return marshaling: only integer/pointer/BSTR-shaped return types are
    # supported -- all come back in EAX, matching real x86 cdecl/stdcall.
    # Float returns (VT_R4/VT_R8: real ABI puts these in ST(0), not EAX)
    # are NOT supported and halt loudly rather than silently return
    # garbage -- no confirmed live need for them yet; extend if one shows
    # up (would need _invoke_emulated_proc to also expose the FPU top-of-
    # stack value before its own cpu.restore_state() call discards it).
    _DISPCALLFUNC_INT_RETURN_VTS = {
        0,   # VT_EMPTY
        _VT_I2, _VT_I4, _VT_BSTR, _VT_DISPATCH, _VT_ERROR, _VT_BOOL,
        _VT_UNKNOWN, _VT_I1, _VT_UI1, _VT_UI2, _VT_UI4, _VT_INT, _VT_UINT,
        _VT_VOID,
    }

    def _write_dispcallfunc_result(pvarg_result: int, vt_return: int, eax_val: int) -> bool:
        if vt_return not in _DISPCALLFUNC_INT_RETURN_VTS:
            return False
        # Same minimal-write convention as _variant_write_i2/_i4/_bool
        # above -- vt at +0, value at +8, reserved bytes untouched.
        memory.write16(pvarg_result & 0xFFFFFFFF, vt_return)
        memory.write32((pvarg_result + 8) & 0xFFFFFFFF, eax_val & 0xFFFFFFFF)
        return True

    def _DispCallFunc(cpu: "CPU") -> None:
        esp0 = cpu.regs[ESP]
        pv_instance  = memory.read32((esp0 + 4)  & 0xFFFFFFFF)
        o_vft        = memory.read32((esp0 + 8)  & 0xFFFFFFFF)
        cc           = memory.read32((esp0 + 12) & 0xFFFFFFFF)
        vt_return    = memory.read32((esp0 + 16) & 0xFFFFFFFF) & 0xFFFF
        c_actuals    = memory.read32((esp0 + 20) & 0xFFFFFFFF)
        prgvt        = memory.read32((esp0 + 24) & 0xFFFFFFFF)
        prgpvarg     = memory.read32((esp0 + 28) & 0xFFFFFFFF)
        pvarg_result = memory.read32((esp0 + 32) & 0xFFFFFFFF)

        if cc not in (_CC_CDECL, _CC_STDCALL):
            logger.error("handlers",
                f"[UNIMPLEMENTED] DispCallFunc: calling convention {cc} not "
                "supported (only CC_CDECL/CC_STDCALL) — halting")
            cpu.halted = True
            cpu.fatal_halt = True
            return

        if pv_instance != 0:
            vtable = memory.read32(pv_instance)
            target = memory.read32((vtable + o_vft) & 0xFFFFFFFF)
        else:
            target = o_vft & 0xFFFFFFFF

        words: list[int] = []
        for i in range(c_actuals):
            vt = memory.read16((prgvt + i * 2) & 0xFFFFFFFF)
            pvarg = memory.read32((prgpvarg + i * 4) & 0xFFFFFFFF)
            arg_words = _dispcallfunc_arg_words(pvarg, vt)
            if arg_words is None:
                logger.error("handlers",
                    f"[UNIMPLEMENTED] DispCallFunc: unsupported argument VARTYPE "
                    f"0x{vt:04x} (arg {i} of {c_actuals}) — halting")
                cpu.halted = True
                cpu.fatal_halt = True
                return
            words.extend(arg_words)

        logger.debug("handlers",
            f"[DispCallFunc] target=0x{target:08x} cc={cc} vtReturn=0x{vt_return:04x} "
            f"cActuals={c_actuals} words={[hex(w) for w in words]}")
        sentinel = _get_dialog_sentinel(state, memory)
        result = _invoke_emulated_proc(
            cpu, memory, target, words, sentinel, scheduler=state.scheduler)

        if pvarg_result != 0:
            if not _write_dispcallfunc_result(pvarg_result, vt_return, result):
                logger.error("handlers",
                    f"[UNIMPLEMENTED] DispCallFunc: unsupported return VARTYPE "
                    f"0x{vt_return:04x} — halting")
                cpu.halted = True
                cpu.fatal_halt = True
                return

        cpu.regs[EAX] = S_OK
        cleanup_stdcall(cpu, memory, 32)

    stubs.register_handler("oleaut32.dll", "DispCallFunc", _DispCallFunc)

    # CoCreateInstance(rclsid, pUnkOuter, dwClsContext, riid, ppv) -> HRESULT
    def _CoCreateInstance(cpu: "CPU") -> None:
        rclsid     = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        p_unk_outer = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        riid       = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        ppv        = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)

        clsid_str = _read_guid_str(rclsid)
        dll_name = _resolve_com_server(rclsid)
        hr = REGDB_E_CLASSNOTREG
        if dll_name is None:
            logger.warn("com", f"CoCreateInstance(clsid={{{clsid_str}}}) — not registered, REGDB_E_CLASSNOTREG")
        elif dll_name.lower() not in _KNOWN_COM_SERVERS:
            logger.warn("com", f"CoCreateInstance(clsid={{{clsid_str}}}) — registered to \"{dll_name}\", which this emulator doesn't implement — REGDB_E_CLASSNOTREG")
        else:
            loaded = _ensure_dll_ready(dll_name, cpu)
            if loaded is None:
                # _ensure_dll_ready already logged the specific reason
                # (file not found, or DllMain returned FALSE) -- don't
                # assert a cause here too, it's not always "from disk".
                logger.warn("com", f"CoCreateInstance: \"{dll_name}\" activation failed (see above)")
            else:
                hr = _call_dll_get_class_object(cpu, loaded, rclsid, _get_iid_iclassfactory_addr(), ppv)
                if not _hr_failed(hr):
                    factory_obj = memory.read32(ppv)
                    if factory_obj:
                        hr = _dispatch_com_method(cpu, factory_obj, 3, [p_unk_outer, riid, ppv])  # IClassFactory::CreateInstance
                        _dispatch_com_method(cpu, factory_obj, 2, [])  # IUnknown::Release
                    else:
                        hr = 0x80004005  # E_FAIL — DllGetClassObject "succeeded" but returned NULL
                final_obj = memory.read32(ppv) if ppv else 0
                logger.info("com",
                    f"CoCreateInstance(clsid={{{clsid_str}}}, riid={{{_read_guid_str(riid)}}}) "
                    f"via real {dll_name} -> hr=0x{hr & 0xFFFFFFFF:08x} *ppv=0x{final_obj:08x}")

        if ppv and _hr_failed(hr):
            memory.write32(ppv, 0)
        cpu.regs[EAX] = hr & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("ole32.dll", "CoCreateInstance", _CoCreateInstance)

    # CoCreateInstanceEx(rclsid, pUnkOuter, dwClsCtx, pServerInfo, dwCount,
    #                     pResults: MULTI_QI[]) -> HRESULT
    # MULTI_QI = { const IID *pIID; IUnknown *pItf; HRESULT hr; } (12 bytes).
    # Creates one instance, then QueryInterfaces it for each of dwCount
    # requested interfaces -- writes each result directly into its own
    # MULTI_QI.pItf/hr fields (real COM proxies do the same in-place
    # marshaling, so no scratch buffer is needed for the [out] pointers).
    def _CoCreateInstanceEx(cpu: "CPU") -> None:
        rclsid       = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        p_unk_outer  = memory.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        dw_count     = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        p_results    = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)

        clsid_str = _read_guid_str(rclsid)

        def _fail_all(hr: int) -> None:
            for i in range(dw_count):
                entry = (p_results + i * 12) & 0xFFFFFFFF
                memory.write32(entry + 4, 0)
                memory.write32(entry + 8, hr & 0xFFFFFFFF)

        if dw_count == 0:
            hr = E_INVALIDARG
            logger.warn("com", "CoCreateInstanceEx: dwCount=0 — E_INVALIDARG")
            cpu.regs[EAX] = hr & 0xFFFFFFFF
            cleanup_stdcall(cpu, memory, 24)
            return

        dll_name = _resolve_com_server(rclsid)
        if dll_name is None:
            logger.warn("com", f"CoCreateInstanceEx(clsid={{{clsid_str}}}) — not registered, REGDB_E_CLASSNOTREG")
            hr = REGDB_E_CLASSNOTREG
            _fail_all(hr)
        elif dll_name.lower() not in _KNOWN_COM_SERVERS:
            logger.warn("com", f"CoCreateInstanceEx(clsid={{{clsid_str}}}) — registered to \"{dll_name}\", which this emulator doesn't implement — REGDB_E_CLASSNOTREG")
            hr = REGDB_E_CLASSNOTREG
            _fail_all(hr)
        else:
            loaded = _ensure_dll_ready(dll_name, cpu)
            if loaded is None:
                logger.warn("com", f"CoCreateInstanceEx: \"{dll_name}\" activation failed (see above)")
                hr = REGDB_E_CLASSNOTREG
                _fail_all(hr)
            else:
                factory_ppv = state.simple_alloc(4)
                hr = _call_dll_get_class_object(cpu, loaded, rclsid, _get_iid_iclassfactory_addr(), factory_ppv)
                factory_obj = memory.read32(factory_ppv) if not _hr_failed(hr) else 0
                if factory_obj:
                    unk_ppv = state.simple_alloc(4)
                    hr = _dispatch_com_method(cpu, factory_obj, 3, [p_unk_outer, _get_iid_iunknown_addr(), unk_ppv])  # IClassFactory::CreateInstance
                    _dispatch_com_method(cpu, factory_obj, 2, [])  # IUnknown::Release (factory)
                    unk_obj = memory.read32(unk_ppv) if not _hr_failed(hr) else 0
                    if unk_obj:
                        any_failed = False
                        for i in range(dw_count):
                            entry = (p_results + i * 12) & 0xFFFFFFFF
                            iid_ptr = memory.read32(entry)
                            qi_hr = _dispatch_com_method(cpu, unk_obj, 0, [iid_ptr, entry + 4])  # QueryInterface
                            if _hr_failed(qi_hr):
                                memory.write32(entry + 4, 0)
                                any_failed = True
                            memory.write32(entry + 8, qi_hr & 0xFFFFFFFF)
                        _dispatch_com_method(cpu, unk_obj, 2, [])  # IUnknown::Release (temp)
                        hr = CO_S_NOTALLINTERFACES if any_failed else S_OK
                    elif not _hr_failed(hr):
                        hr = 0x80004005  # E_FAIL — CreateInstance "succeeded" but returned NULL
                        _fail_all(hr)
                    else:
                        _fail_all(hr)
                elif not _hr_failed(hr):
                    hr = 0x80004005  # E_FAIL — DllGetClassObject "succeeded" but returned NULL
                    _fail_all(hr)
                else:
                    _fail_all(hr)
                logger.info("com",
                    f"CoCreateInstanceEx(clsid={{{clsid_str}}}, count={dw_count}) "
                    f"via real {dll_name} -> hr=0x{hr & 0xFFFFFFFF:08x}")

        cpu.regs[EAX] = hr & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 24)

    stubs.register_handler("ole32.dll", "CoCreateInstanceEx", _CoCreateInstanceEx)

    # CoGetClassObject(rclsid, dwClsContext, pServerInfo, riid, ppv) -> HRESULT
    def _CoGetClassObject(cpu: "CPU") -> None:
        rclsid = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        riid   = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        ppv    = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)

        clsid_str = _read_guid_str(rclsid)
        dll_name = _resolve_com_server(rclsid)
        hr = REGDB_E_CLASSNOTREG
        if dll_name is None:
            logger.warn("com", f"CoGetClassObject(clsid={{{clsid_str}}}) — not registered, REGDB_E_CLASSNOTREG")
        elif dll_name.lower() not in _KNOWN_COM_SERVERS:
            logger.warn("com", f"CoGetClassObject(clsid={{{clsid_str}}}) — registered to \"{dll_name}\", which this emulator doesn't implement — REGDB_E_CLASSNOTREG")
        else:
            loaded = _ensure_dll_ready(dll_name, cpu)
            if loaded is None:
                # _ensure_dll_ready already logged the specific reason
                # (file not found, or DllMain returned FALSE) -- don't
                # assert a cause here too, it's not always "from disk".
                logger.warn("com", f"CoGetClassObject: \"{dll_name}\" activation failed (see above)")
            else:
                hr = _call_dll_get_class_object(cpu, loaded, rclsid, riid, ppv)
                obj_addr = memory.read32(ppv) if ppv else 0
                logger.info("com",
                    f"CoGetClassObject(clsid={{{clsid_str}}}, riid={{{_read_guid_str(riid)}}}) "
                    f"via real {dll_name} -> hr=0x{hr & 0xFFFFFFFF:08x} *ppv=0x{obj_addr:08x}")

        if ppv and _hr_failed(hr):
            memory.write32(ppv, 0)  # *ppv = NULL on failure, per COM contract
        cpu.regs[EAX] = hr & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("ole32.dll", "CoGetClassObject", _CoGetClassObject)

    # CLSIDFromProgID(lpszProgID, lpclsid) -> HRESULT
    def _CLSIDFromProgID(cpu: "CPU") -> None:
        lp_progid = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_clsid  = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        progid = read_wide_string(lp_progid, memory)
        clsid_str = _resolve_progid_clsid(progid)
        if clsid_str is None:
            logger.warn("com", f'CLSIDFromProgID("{progid}") — not registered, CO_E_CLASSSTRING')
            memory.write32(lp_clsid, 0)
            memory.write32(lp_clsid + 4, 0)
            memory.write32(lp_clsid + 8, 0)
            memory.write32(lp_clsid + 12, 0)
            hr = CO_E_CLASSSTRING
        else:
            _write_guid(lp_clsid, clsid_str)
            logger.info("com", f'CLSIDFromProgID("{progid}") -> {{{clsid_str}}}')
            hr = S_OK
        cpu.regs[EAX] = hr & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("ole32.dll", "CLSIDFromProgID", _CLSIDFromProgID)

    # CLSIDFromProgIDEx(lpszProgID, lpclsid) -> HRESULT
    # Same ProgID->CLSID registry lookup as CLSIDFromProgID, but also
    # verifies the resolved CLSID actually has a server registration
    # (InprocServer32/LocalServer32) -- the extra integrity check real
    # Windows makes that plain CLSIDFromProgID doesn't.
    def _CLSIDFromProgIDEx(cpu: "CPU") -> None:
        lp_progid = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_clsid  = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        progid = read_wide_string(lp_progid, memory)
        clsid_str = _resolve_progid_clsid(progid)
        if clsid_str is not None and not _clsid_has_server(clsid_str):
            logger.warn("com",
                f'CLSIDFromProgIDEx("{progid}") -> {{{clsid_str}}} has no server registration — REGDB_E_CLASSNOTREG')
            clsid_str = None
            hr_not_found = REGDB_E_CLASSNOTREG
        else:
            hr_not_found = CO_E_CLASSSTRING
        if clsid_str is None:
            logger.warn("com", f'CLSIDFromProgIDEx("{progid}") — not registered')
            memory.write32(lp_clsid, 0)
            memory.write32(lp_clsid + 4, 0)
            memory.write32(lp_clsid + 8, 0)
            memory.write32(lp_clsid + 12, 0)
            hr = hr_not_found
        else:
            _write_guid(lp_clsid, clsid_str)
            logger.info("com", f'CLSIDFromProgIDEx("{progid}") -> {{{clsid_str}}}')
            hr = S_OK
        cpu.regs[EAX] = hr & 0xFFFFFFFF
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("ole32.dll", "CLSIDFromProgIDEx", _CLSIDFromProgIDEx)

    # OleInitialize(pvReserved) -> HRESULT
    def _OleInitialize(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # S_OK — 1 stdcall arg (pvReserved, must be NULL)
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("ole32.dll", "OleInitialize", _OleInitialize)

    # OleUninitialize() -> void
    def _OleUninitialize(cpu: "CPU") -> None:
        cleanup_stdcall(cpu, memory, 0)  # void return, no args

    stubs.register_handler("ole32.dll", "OleUninitialize", _OleUninitialize)
