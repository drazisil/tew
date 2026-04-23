"""ZigCPU — drop-in replacement for CPU backed by libcpu.so (Zig x86-32 dispatch)."""

from __future__ import annotations

import ctypes
import math
import os
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from tew.hardware.memory import Memory
from tew.logger import logger

if TYPE_CHECKING:
    from tew.kernel.kernel_structures import KernelStructures

# Register indices (mirrors cpu.py)
EAX, ECX, EDX, EBX = 0, 1, 2, 3
ESP, EBP, ESI, EDI = 4, 5, 6, 7

REG = {"EAX": 0, "ECX": 1, "EDX": 2, "EBX": 3, "ESP": 4, "EBP": 5, "ESI": 6, "EDI": 7}
REG_NAMES = ["EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI"]

# Flag bit positions
CF_BIT = 0
PF_BIT = 2
ZF_BIT = 6
SF_BIT = 7
DF_BIT = 10
OF_BIT = 11

FLAG = {"CF": CF_BIT, "PF": PF_BIT, "ZF": ZF_BIT, "SF": SF_BIT, "DF": DF_BIT, "OF": OF_BIT}

# RunResult enum values from Zig
_RUN_OK         = 0
_RUN_HALTED     = 1
_RUN_FAULTED    = 2
_RUN_STEP_LIMIT = 3

# ── Load libcpu.so ────────────────────────────────────────────────────────────

_LIB_PATH = Path(__file__).parent.parent.parent / "cpu" / "zig-out" / "lib" / "libcpu.so"

def _load_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL(str(_LIB_PATH))

    _u8  = ctypes.c_uint8
    _u16 = ctypes.c_uint16
    _u32 = ctypes.c_uint32
    _u64 = ctypes.c_uint64
    _i32 = ctypes.c_int
    _f64 = ctypes.c_double
    _vp  = ctypes.c_void_p
    _sz  = ctypes.c_size_t
    _b   = ctypes.c_bool

    lib.cpu_create.argtypes      = [ctypes.POINTER(_u8), _sz]
    lib.cpu_create.restype       = _vp

    lib.cpu_destroy.argtypes     = [_vp]
    lib.cpu_destroy.restype      = None

    lib.cpu_set_int_handler.argtypes = [_vp, _vp]
    lib.cpu_set_int_handler.restype  = None

    lib.cpu_run.argtypes         = [_vp, _u64]
    lib.cpu_run.restype          = _i32

    lib.cpu_get_reg.argtypes     = [_vp, _u32]
    lib.cpu_get_reg.restype      = _u32
    lib.cpu_set_reg.argtypes     = [_vp, _u32, _u32]
    lib.cpu_set_reg.restype      = None

    lib.cpu_get_eip.argtypes     = [_vp]
    lib.cpu_get_eip.restype      = _u32
    lib.cpu_set_eip.argtypes     = [_vp, _u32]
    lib.cpu_set_eip.restype      = None

    lib.cpu_get_eflags.argtypes  = [_vp]
    lib.cpu_get_eflags.restype   = _u32
    lib.cpu_set_eflags.argtypes  = [_vp, _u32]
    lib.cpu_set_eflags.restype   = None

    lib.cpu_is_halted.argtypes   = [_vp]
    lib.cpu_is_halted.restype    = _b
    lib.cpu_is_faulted.argtypes  = [_vp]
    lib.cpu_is_faulted.restype   = _b
    lib.cpu_set_halted.argtypes  = [_vp]
    lib.cpu_set_halted.restype   = None
    lib.cpu_clear_halted.argtypes= [_vp]
    lib.cpu_clear_halted.restype = None

    lib.cpu_get_step_count.argtypes  = [_vp]
    lib.cpu_get_step_count.restype   = _u64
    lib.cpu_get_last_opcode.argtypes = [_vp]
    lib.cpu_get_last_opcode.restype  = _u8

    lib.cpu_set_fs_base.argtypes = [_vp, _u32]
    lib.cpu_set_fs_base.restype  = None
    lib.cpu_set_gs_base.argtypes = [_vp, _u32]
    lib.cpu_set_gs_base.restype  = None
    lib.cpu_get_fs_base.argtypes = [_vp]
    lib.cpu_get_fs_base.restype  = _u32
    lib.cpu_get_gs_base.argtypes = [_vp]
    lib.cpu_get_gs_base.restype  = _u32

    lib.cpu_fpu_get.argtypes     = [_vp, _u32]
    lib.cpu_fpu_get.restype      = _f64
    lib.cpu_fpu_set.argtypes     = [_vp, _u32, _f64]
    lib.cpu_fpu_set.restype      = None
    lib.cpu_fpu_get_top.argtypes = [_vp]
    lib.cpu_fpu_get_top.restype  = _u32
    lib.cpu_fpu_set_top.argtypes = [_vp, _u32]
    lib.cpu_fpu_set_top.restype  = None

    lib.cpu_fpu_get_status.argtypes  = [_vp]
    lib.cpu_fpu_get_status.restype   = _u16
    lib.cpu_fpu_set_status.argtypes  = [_vp, _u16]
    lib.cpu_fpu_set_status.restype   = None
    lib.cpu_fpu_get_control.argtypes = [_vp]
    lib.cpu_fpu_get_control.restype  = _u16
    lib.cpu_fpu_set_control.argtypes = [_vp, _u16]
    lib.cpu_fpu_set_control.restype  = None
    lib.cpu_fpu_get_tag.argtypes     = [_vp]
    lib.cpu_fpu_get_tag.restype      = _u16
    lib.cpu_fpu_set_tag.argtypes     = [_vp, _u16]
    lib.cpu_fpu_set_tag.restype      = None

    return lib

_lib = _load_lib()

# C callback type: fn(state: *anyopaque, int_num: u8) callconv(.C) void
_IntHandlerCType = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint8)


# ── Proxy types ───────────────────────────────────────────────────────────────

class _RegProxy:
    """Proxies cpu.regs[i] read/write to Zig via ctypes."""
    __slots__ = ("_lib", "_state")

    def __init__(self, lib: ctypes.CDLL, state: int) -> None:
        self._lib   = lib
        self._state = state

    def __getitem__(self, idx: int) -> int:
        return self._lib.cpu_get_reg(self._state, idx)

    def __setitem__(self, idx: int, val: int) -> None:
        self._lib.cpu_set_reg(self._state, idx, val & 0xFFFFFFFF)

    def __iter__(self):
        return (self._lib.cpu_get_reg(self._state, i) for i in range(8))

    def __len__(self) -> int:
        return 8

    def copy(self) -> list[int]:
        return [self._lib.cpu_get_reg(self._state, i) for i in range(8)]


class _FpuStackProxy:
    """Proxies cpu.fpu_stack[i] read/write to Zig.

    Note: Zig's cpu_fpu_get/set use *absolute* stack indices (0-7), not
    ST(n) relative indices.  msvcrt_handlers uses absolute indices too
    (e.g. ``cpu.fpu_stack[cpu.fpu_top & 7]``), so we pass straight through.
    """
    __slots__ = ("_lib", "_state")

    def __init__(self, lib: ctypes.CDLL, state: int) -> None:
        self._lib   = lib
        self._state = state

    def __getitem__(self, idx: int) -> float:
        return self._lib.cpu_fpu_get(self._state, idx & 7)

    def __setitem__(self, idx: int, val: float) -> None:
        self._lib.cpu_fpu_set(self._state, idx & 7, val)

    def __iter__(self):
        return (self._lib.cpu_fpu_get(self._state, i) for i in range(8))

    def __len__(self) -> int:
        return 8

    def copy(self) -> list[float]:
        return [self._lib.cpu_fpu_get(self._state, i) for i in range(8)]


# ── SavedCPUState ─────────────────────────────────────────────────────────────

class SavedCPUState:
    __slots__ = (
        "regs", "eip", "eflags",
        "fpu_stack", "fpu_top", "fpu_status_word", "fpu_control_word", "fpu_tag_word",
    )

    def __init__(
        self,
        regs: list[int],
        eip: int,
        eflags: int,
        fpu_stack: list[float],
        fpu_top: int,
        fpu_status_word: int,
        fpu_control_word: int,
        fpu_tag_word: int,
    ) -> None:
        self.regs             = regs
        self.eip              = eip
        self.eflags           = eflags
        self.fpu_stack        = fpu_stack
        self.fpu_top          = fpu_top
        self.fpu_status_word  = fpu_status_word
        self.fpu_control_word = fpu_control_word
        self.fpu_tag_word     = fpu_tag_word


# ── ZigCPU ────────────────────────────────────────────────────────────────────

class ZigCPU:
    """x86-32 CPU backed by the Zig shared library.  Drop-in for CPU."""

    def __init__(self, memory: Memory) -> None:
        self.memory: Memory = memory

        # Pin the bytearray in a ctypes array so Zig can access it directly.
        # from_buffer keeps the bytearray alive via this reference.
        buf = memory._buffer
        self._ctypes_buf = (ctypes.c_uint8 * len(buf)).from_buffer(buf)
        self._state: int = _lib.cpu_create(self._ctypes_buf, len(buf))
        if not self._state:
            raise RuntimeError("cpu_create returned NULL")

        # Proxies
        self.regs      = _RegProxy(_lib, self._state)
        self.fpu_stack = _FpuStackProxy(_lib, self._state)

        # Python-side state (not mirrored into Zig struct)
        self.last_error:        Exception | None = None
        self._int_handler:      Callable | None  = None
        self._step_handler:     Callable | None  = None
        self._kernel_structures: "KernelStructures | None" = None
        self._last_fs_base:     int = 0
        # Override flags for handle_exception() called from Python (not from Zig)
        self._py_halted:        bool = False
        self._py_faulted:       bool = False

        # Keep C callback alive for the lifetime of this object
        self._c_callback = _IntHandlerCType(self._c_int_dispatch)
        _lib.cpu_set_int_handler(self._state, self._c_callback)

        # Compatibility shim — callers that do cpu.segments["FS"] = ...
        self.segments: dict[str, int] = {"ES": 0, "DS": 0, "CS": 0, "SS": 0, "FS": 0, "GS": 0}

    # ── Kernel structures / FS-GS sync ────────────────────────────────────────

    @property
    def kernel_structures(self) -> "KernelStructures | None":
        return self._kernel_structures

    @kernel_structures.setter
    def kernel_structures(self, ks: "KernelStructures | None") -> None:
        self._kernel_structures = ks
        self._sync_fs_gs()

    def _sync_fs_gs(self) -> None:
        if self._kernel_structures is None:
            return
        base = self._kernel_structures.get_fs_base()
        if base != self._last_fs_base:
            _lib.cpu_set_fs_base(self._state, base)
            _lib.cpu_set_gs_base(self._state, base)
            self._last_fs_base = base

    # ── C callback (called by Zig on INT instruction) ─────────────────────────

    def _c_int_dispatch(self, _state_ptr: int, int_num: int) -> None:
        if self._int_handler:
            self._int_handler(int_num, self)

    # ── Execution ─────────────────────────────────────────────────────────────

    def step(self) -> None:
        self._sync_fs_gs()
        result = _lib.cpu_run(self._state, 1)
        if result == _RUN_FAULTED:
            self.last_error = RuntimeError(
                f"CPU fault at EIP=0x{self.eip:08x} opcode=0x{_lib.cpu_get_last_opcode(self._state):02x}"
            )

    def run(self, max_steps: int = 1_000_000) -> None:
        self._sync_fs_gs()
        _lib.cpu_run(self._state, max_steps)

    # ── Register/state properties ─────────────────────────────────────────────

    @property
    def eip(self) -> int:
        return _lib.cpu_get_eip(self._state)

    @eip.setter
    def eip(self, val: int) -> None:
        _lib.cpu_set_eip(self._state, val & 0xFFFFFFFF)

    @property
    def eflags(self) -> int:
        return _lib.cpu_get_eflags(self._state)

    @eflags.setter
    def eflags(self, val: int) -> None:
        _lib.cpu_set_eflags(self._state, val & 0xFFFFFFFF)

    @property
    def halted(self) -> bool:
        return self._py_halted or _lib.cpu_is_halted(self._state)

    @halted.setter
    def halted(self, val: bool) -> None:
        if not val:
            self._py_halted = False
            _lib.cpu_clear_halted(self._state)
        else:
            self._py_halted = True
            _lib.cpu_set_halted(self._state)

    @property
    def faulted(self) -> bool:
        return self._py_faulted or _lib.cpu_is_faulted(self._state)

    @faulted.setter
    def faulted(self, val: bool) -> None:
        if not val:
            self._py_faulted = False
            _lib.cpu_clear_halted(self._state)
        else:
            self._py_faulted = True

    @property
    def step_count(self) -> int:
        return _lib.cpu_get_step_count(self._state)

    @property
    def _step_count(self) -> int:
        return _lib.cpu_get_step_count(self._state)

    # ── FPU properties ────────────────────────────────────────────────────────

    @property
    def fpu_top(self) -> int:
        return _lib.cpu_fpu_get_top(self._state)

    @fpu_top.setter
    def fpu_top(self, val: int) -> None:
        _lib.cpu_fpu_set_top(self._state, val & 7)

    @property
    def fpu_status_word(self) -> int:
        return _lib.cpu_fpu_get_status(self._state)

    @fpu_status_word.setter
    def fpu_status_word(self, val: int) -> None:
        _lib.cpu_fpu_set_status(self._state, val & 0xFFFF)

    @property
    def fpu_control_word(self) -> int:
        return _lib.cpu_fpu_get_control(self._state)

    @fpu_control_word.setter
    def fpu_control_word(self, val: int) -> None:
        _lib.cpu_fpu_set_control(self._state, val & 0xFFFF)

    @property
    def fpu_tag_word(self) -> int:
        return _lib.cpu_fpu_get_tag(self._state)

    @fpu_tag_word.setter
    def fpu_tag_word(self, val: int) -> None:
        _lib.cpu_fpu_set_tag(self._state, val & 0xFFFF)

    # ── FPU methods (used by msvcrt_handlers directly) ────────────────────────

    def fpu_get(self, i: int) -> float:
        return _lib.cpu_fpu_get(self._state, i)

    def fpu_set(self, i: int, val: float) -> None:
        _lib.cpu_fpu_set(self._state, i, val)

    def fpu_push(self, val: float) -> None:
        top = (self.fpu_top - 1) & 7
        _lib.cpu_fpu_set_top(self._state, top)
        _lib.cpu_fpu_set(self._state, top, val)
        tag = _lib.cpu_fpu_get_tag(self._state) & ~(3 << (top * 2))
        _lib.cpu_fpu_set_tag(self._state, tag)
        sw = (_lib.cpu_fpu_get_status(self._state) & ~0x3800) | (top << 11)
        _lib.cpu_fpu_set_status(self._state, sw)

    def fpu_pop(self) -> float:
        top = self.fpu_top
        val = _lib.cpu_fpu_get(self._state, top)
        tag = _lib.cpu_fpu_get_tag(self._state) | (3 << (top * 2))
        _lib.cpu_fpu_set_tag(self._state, tag)
        new_top = (top + 1) & 7
        _lib.cpu_fpu_set_top(self._state, new_top)
        sw = (_lib.cpu_fpu_get_status(self._state) & ~0x3800) | (new_top << 11)
        _lib.cpu_fpu_set_status(self._state, sw)
        return val

    def fpu_set_cc(self, c3: bool, c2: bool, c0: bool) -> None:
        sw = _lib.cpu_fpu_get_status(self._state) & ~0x4500
        if c0: sw |= 0x0100
        if c2: sw |= 0x0400
        if c3: sw |= 0x4000
        _lib.cpu_fpu_set_status(self._state, sw)

    def fpu_compare(self, a: float, b: float) -> None:
        if math.isnan(a) or math.isnan(b):
            self.fpu_set_cc(True, True, True)
        elif a > b:
            self.fpu_set_cc(False, False, False)
        elif a < b:
            self.fpu_set_cc(False, False, True)
        else:
            self.fpu_set_cc(True, False, False)

    # ── Flag helpers ──────────────────────────────────────────────────────────

    def get_flag(self, bit: int) -> bool:
        return ((self.eflags >> bit) & 1) == 1

    def set_flag(self, bit: int, val: bool) -> None:
        ef = self.eflags
        if val:
            self.eflags = ef | (1 << bit)
        else:
            self.eflags = ef & ~(1 << bit)

    # ── Interrupt / step handler registration ────────────────────────────────

    def on_interrupt(self, handler: Callable[[int, "ZigCPU"], None]) -> None:
        self._int_handler = handler

    def on_step(self, handler: Callable) -> None:
        self._step_handler = handler

    def trigger_interrupt(self, int_num: int) -> None:
        if self._int_handler:
            self._int_handler(int_num, self)
        else:
            raise RuntimeError(f"Unhandled interrupt: INT 0x{int_num:02x}")

    def handle_exception(self, error: Exception) -> None:
        self.last_error = error
        # Signal Zig that the CPU has faulted by setting EIP to a known-bad
        # address and letting the main loop see halted=True from Zig's own fault
        # flag — but since this is called from Python (not from inside Zig), we
        # have no Zig-side setter for halted.  Work around: store a Python-side
        # override and check it in step().
        self._py_faulted = True
        self._py_halted  = True

    # ── Save / restore ────────────────────────────────────────────────────────

    def save_state(self) -> SavedCPUState:
        return SavedCPUState(
            regs            = [_lib.cpu_get_reg(self._state, i) for i in range(8)],
            eip             = _lib.cpu_get_eip(self._state),
            eflags          = _lib.cpu_get_eflags(self._state),
            fpu_stack       = [_lib.cpu_fpu_get(self._state, i) for i in range(8)],
            fpu_top         = _lib.cpu_fpu_get_top(self._state),
            fpu_status_word = _lib.cpu_fpu_get_status(self._state),
            fpu_control_word= _lib.cpu_fpu_get_control(self._state),
            fpu_tag_word    = _lib.cpu_fpu_get_tag(self._state),
        )

    def restore_state(self, s: SavedCPUState) -> None:
        for i, v in enumerate(s.regs):
            _lib.cpu_set_reg(self._state, i, v & 0xFFFFFFFF)
        _lib.cpu_set_eip(self._state, s.eip)
        _lib.cpu_set_eflags(self._state, s.eflags)
        for i, v in enumerate(s.fpu_stack):
            _lib.cpu_fpu_set(self._state, i, v)
        _lib.cpu_fpu_set_top(self._state, s.fpu_top)
        _lib.cpu_fpu_set_status(self._state, s.fpu_status_word)
        _lib.cpu_fpu_set_control(self._state, s.fpu_control_word)
        _lib.cpu_fpu_set_tag(self._state, s.fpu_tag_word)

    # ── Float memory helpers ──────────────────────────────────────────────────

    def read_double(self, addr: int) -> float:
        lo = self.memory.read32(addr)
        hi = self.memory.read32(addr + 4)
        return struct.unpack("<d", struct.pack("<II", lo, hi))[0]

    def write_double(self, addr: int, val: float) -> None:
        lo, hi = struct.unpack("<II", struct.pack("<d", val))
        self.memory.write32(addr, lo)
        self.memory.write32(addr + 4, hi)

    def read_float(self, addr: int) -> float:
        raw = self.memory.read32(addr)
        return struct.unpack("<f", struct.pack("<I", raw))[0]

    def write_float(self, addr: int, val: float) -> None:
        raw = struct.unpack("<I", struct.pack("<f", val))[0]
        self.memory.write32(addr, raw)

    # ── Opcode registration shim (no-op — Zig handles all opcodes) ────────────

    def register(self, opcode: int, handler: Callable) -> None:
        pass

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def __del__(self) -> None:
        if self._state:
            _lib.cpu_destroy(self._state)
            self._state = 0

    # ── Debug repr ────────────────────────────────────────────────────────────

    def __str__(self) -> str:
        regs = "  ".join(
            f"{name}={_lib.cpu_get_reg(self._state, i) & 0xFFFFFFFF:08x}"
            for i, name in enumerate(REG_NAMES)
        )
        flags = " ".join([
            "CF" if self.get_flag(CF_BIT) else "cf",
            "ZF" if self.get_flag(ZF_BIT) else "zf",
            "SF" if self.get_flag(SF_BIT) else "sf",
            "OF" if self.get_flag(OF_BIT) else "of",
        ])
        return f"EIP={self.eip & 0xFFFFFFFF:08x}  {regs}  [{flags}]"
