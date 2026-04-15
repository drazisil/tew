"""oleaut32.dll and ole32.dll handler registrations.

Implements BSTR heap, VARIANT lifecycle, SafeArray allocation, COM initialisation
stubs, and the ordinal-aliased exports from WinXP OLEAUT32.dll.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall
from tew.api._state import CRTState
from tew.logger import logger


def register_oleaut32_ole32_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register all oleaut32.dll and ole32.dll handlers."""

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

    stubs.register_handler("oleaut32.dll", "SafeArrayRedim", _SafeArrayRedim)

    # SafeArrayPutElement(SAFEARRAY *psa, LONG *rgIndices, void *pv) -> HRESULT
    def _SafeArrayPutElement(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] SafeArrayPutElement — halting")
        cpu.halted = True

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

    _ole_ord(10, _ord10)

    # Ordinal 12 — VariantChangeType(pvargDest, pvarSrc, wFlags, vt) -> HRESULT
    def _ord12(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] VariantChangeType (Ordinal 12) — halting")
        cpu.halted = True

    _ole_ord(12, _ord12)

    # Ordinal 82 — VarR8FromCy(cyIn, pdblOut) -> HRESULT
    def _ord82(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] VarR8FromCy (Ordinal 82) — halting")
        cpu.halted = True

    _ole_ord(82, _ord82)

    # Ordinal 104 — VarCyFromStr(strIn, lcid, dwFlags, pcyOut) -> HRESULT
    def _ord104(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x80004001  # E_NOTIMPL
        cleanup_stdcall(cpu, memory, 20)

    _ole_ord(104, _ord104)

    # Ordinal 113 — VarBstrFromCy(cyIn, lcid, dwFlags, pbstrOut) -> HRESULT
    def _ord113(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] VarBstrFromCy (Ordinal 113) — halting")
        cpu.halted = True

    _ole_ord(113, _ord113)

    # Ordinal 149 — SysStringByteLen(bstr) -> UINT
    def _ord149(cpu: "CPU") -> None:
        bstr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = memory.read32(bstr - 4) if bstr else 0
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
        cpu.regs[EAX] = 0x80004001  # E_NOTIMPL
        cleanup_stdcall(cpu, memory, 12)

    _ole_ord(154, _ord154)

    # Ordinal 155 — RegisterTypeLib(...) -> HRESULT
    def _ord155(cpu: "CPU") -> None:
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

    # CoCreateInstance(rclsid, pUnkOuter, dwClsContext, riid, ppv) -> HRESULT
    def _CoCreateInstance(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x80040154  # REGDB_E_CLASSNOTREG
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("ole32.dll", "CoCreateInstance", _CoCreateInstance)

    # OleInitialize(pvReserved) -> HRESULT
    def _OleInitialize(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # S_OK — 1 stdcall arg (pvReserved, must be NULL)
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("ole32.dll", "OleInitialize", _OleInitialize)

    # OleUninitialize() -> void
    def _OleUninitialize(cpu: "CPU") -> None:
        cleanup_stdcall(cpu, memory, 0)  # void return, no args

    stubs.register_handler("ole32.dll", "OleUninitialize", _OleUninitialize)
