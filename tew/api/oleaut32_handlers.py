"""oleaut32.dll and ole32.dll handler registrations.

Implements BSTR heap, VARIANT lifecycle, SafeArray allocation, COM initialisation
stubs, and the ordinal-aliased exports from WinXP OLEAUT32.dll.
"""

from __future__ import annotations

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
    def _VariantChangeType(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] VariantChangeType — halting")
        cpu.halted = True
        cpu.fatal_halt = True

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
    def _ord9(cpu: "CPU") -> None:
        pv = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if pv:
            memory.write16(pv,     0)
            memory.write16(pv + 2, 0)
        cpu.regs[EAX] = 0  # S_OK
        cleanup_stdcall(cpu, memory, 4)

    _ole_ord(9, _ord9)

    # Ordinal 10 — VariantCopy(pvargDest, pvargSrc) -> HRESULT
    def _ord10(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] VariantCopy (Ordinal 10) — halting")
        cpu.halted = True
        cpu.fatal_halt = True

    _ole_ord(10, _ord10)

    # Ordinal 12 — VariantChangeType(pvargDest, pvarSrc, wFlags, vt) -> HRESULT
    def _ord12(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] VariantChangeType (Ordinal 12) — halting")
        cpu.halted = True
        cpu.fatal_halt = True

    _ole_ord(12, _ord12)

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

    # Ordinal 154 — LoadTypeLibEx(...) -> HRESULT
    def _ord154(cpu: "CPU") -> None:
        logger.warn("com", "LoadTypeLibEx (Ordinal 154) called — returning E_NOTIMPL (type library loading not implemented)")
        cpu.regs[EAX] = 0x80004001  # E_NOTIMPL
        cleanup_stdcall(cpu, memory, 12)

    _ole_ord(154, _ord154)

    # Ordinal 155 — RegisterTypeLib(...) -> HRESULT
    def _ord155(cpu: "CPU") -> None:
        logger.warn("com", "RegisterTypeLib (Ordinal 155) called — returning E_NOTIMPL (type library registration not implemented)")
        cpu.regs[EAX] = 0x80004001  # E_NOTIMPL
        cleanup_stdcall(cpu, memory, 12)

    _ole_ord(155, _ord155)

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
