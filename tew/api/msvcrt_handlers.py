"""msvcrt.dll handler registrations for the Win32 emulator.

Ported from /data/Code/exe/src/api/Win32Handlers.ts lines 4157-4654.
All handlers use the cdecl calling convention (caller cleans up) unless
noted otherwise — msvcrt.dll exports are exclusively cdecl.
"""

from __future__ import annotations

import math
import struct
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import Win32Handlers
from tew.api._state import CRTState, read_cstring
from tew.logger import logger

# ── Fixed data region addresses ───────────────────────────────────────────────
# These match addresses established by the outer Win32Handlers registration
# (kernel32 section writes these before msvcrt section runs).

_CMD_LINE_ADDR   = 0x00210024   # "MCity_d.exe\0" — set by kernel32 GetCommandLineA handler
_ENV_STR_A_ADDR  = 0x0021004C   # empty env string — set by kernel32 GetEnvironmentStringsA handler

# __getmainargs data lives in the fixed region at 0x210050+
_ARGC_ADDR       = 0x00210050
_ARGV_ADDR       = 0x00210054
_ENVP_ADDR       = 0x00210058
_ARGV_ARRAY_ADDR = 0x0021005C

# __p__fmode / __p__commode data
_FMODE_ADDR  = 0x0021001C
_COMMODE_ADDR = 0x00210020


# ── printf helper: write a Python str into emulator memory as C string ────────

def _write_cstring(ptr: int, s: str, memory: "Memory") -> int:
    """Write *s* into emulator memory at *ptr* as a null-terminated Latin-1 string.

    Returns the number of bytes written (not counting the null terminator).
    """
    data = s.encode("latin-1", errors="replace")
    for i, b in enumerate(data):
        memory.write8((ptr + i) & 0xFFFFFFFF, b)
    memory.write8((ptr + len(data)) & 0xFFFFFFFF, 0)
    return len(data)


# ── printf helper: format engine ─────────────────────────────────────────────

def _sprintf_format(fmt: str, get_arg: Callable[[], int], memory: "Memory") -> str:
    """Format *fmt* using x86 stack arguments supplied by *get_arg*.

    *get_arg* is called once per format argument (advancing its own offset).
    For 64-bit floating-point specifiers (%f, %e, %g) it is called twice —
    first for the low 32 bits, then for the high 32 bits (little-endian double).
    """
    out: list[str] = []
    i = 0
    n = len(fmt)

    while i < n:
        if fmt[i] != "%":
            out.append(fmt[i])
            i += 1
            continue

        i += 1
        if i >= n:
            break
        if fmt[i] == "%":
            out.append("%")
            i += 1
            continue

        # ── Flags ────────────────────────────────────────────────────────────
        left_align = False
        zero_pad   = False
        force_sign = False
        space_sign = False
        while i < n:
            c = fmt[i]
            if   c == "-": left_align = True;  i += 1
            elif c == "0": zero_pad   = True;  i += 1
            elif c == "+": force_sign = True;  i += 1
            elif c == " ": space_sign = True;  i += 1
            elif c == "#": i += 1              # alt form — ignored
            else: break

        # ── Width ────────────────────────────────────────────────────────────
        width = 0
        if i < n and fmt[i] == "*":
            width = get_arg() | 0  # treat as signed
            i += 1
            if width < 0:
                left_align = True
                width = -width
        else:
            while i < n and "0" <= fmt[i] <= "9":
                width = width * 10 + int(fmt[i])
                i += 1

        # ── Precision ────────────────────────────────────────────────────────
        prec = -1
        if i < n and fmt[i] == ".":
            i += 1
            if i < n and fmt[i] == "*":
                prec = get_arg() | 0
                i += 1
            else:
                prec = 0
                while i < n and "0" <= fmt[i] <= "9":
                    prec = prec * 10 + int(fmt[i])
                    i += 1

        # ── Length modifier — consume, ignore (32-bit) ───────────────────────
        if i < n and fmt[i] in ("l", "h", "L"):
            i += 1
            if i < n and fmt[i] == "l":
                i += 1  # ll
        elif i + 2 < n and fmt[i] == "I" and fmt[i + 1] == "6" and fmt[i + 2] == "4":
            i += 3  # MSVC I64 extension

        spec = fmt[i] if i < n else "?"
        i += 1
        val: str

        if spec in ("d", "i"):
            raw = get_arg()
            n_val = (raw | 0) if raw < 0x80000000 else (raw - 0x100000000)
            digits = str(abs(n_val))
            if prec >= 0 and len(digits) < prec:
                digits = "0" * (prec - len(digits)) + digits
            if n_val < 0:
                val = "-" + digits
            elif force_sign:
                val = "+" + digits
            elif space_sign:
                val = " " + digits
            else:
                val = digits

        elif spec == "u":
            val = str(get_arg() & 0xFFFFFFFF)
            if prec >= 0 and len(val) < prec:
                val = "0" * (prec - len(val)) + val

        elif spec == "x":
            val = format(get_arg() & 0xFFFFFFFF, "x")
            if prec >= 0 and len(val) < prec:
                val = "0" * (prec - len(val)) + val

        elif spec == "X":
            val = format(get_arg() & 0xFFFFFFFF, "X")
            if prec >= 0 and len(val) < prec:
                val = "0" * (prec - len(val)) + val

        elif spec == "o":
            val = format(get_arg() & 0xFFFFFFFF, "o")
            if prec >= 0 and len(val) < prec:
                val = "0" * (prec - len(val)) + val

        elif spec == "p":
            val = "0x" + format(get_arg() & 0xFFFFFFFF, "08x")

        elif spec == "c":
            val = chr(get_arg() & 0xFF)

        elif spec == "s":
            sptr = get_arg() & 0xFFFFFFFF
            if sptr == 0:
                val = ""
            else:
                max_read = (prec + 1) if prec >= 0 else 4096
                val = read_cstring(sptr, memory, max_read)
            if prec >= 0 and len(val) > prec:
                val = val[:prec]

        elif spec in ("f", "F"):
            lo = get_arg() & 0xFFFFFFFF
            hi = get_arg() & 0xFFFFFFFF
            f64 = struct.unpack("<d", struct.pack("<II", lo, hi))[0]
            precision = prec if prec >= 0 else 6
            val = format(f64, f".{precision}f")
            if force_sign and f64 >= 0:
                val = "+" + val

        elif spec in ("e", "E"):
            lo = get_arg() & 0xFFFFFFFF
            hi = get_arg() & 0xFFFFFFFF
            f64 = struct.unpack("<d", struct.pack("<II", lo, hi))[0]
            precision = prec if prec >= 0 else 6
            val = format(f64, f".{precision}e")
            if spec == "E":
                val = val.upper()

        elif spec in ("g", "G"):
            lo = get_arg() & 0xFFFFFFFF
            hi = get_arg() & 0xFFFFFFFF
            f64 = struct.unpack("<d", struct.pack("<II", lo, hi))[0]
            precision = prec if prec > 0 else 6
            val = format(f64, f".{precision}g")
            if spec == "G":
                val = val.upper()

        elif spec == "n":
            memory.write32(get_arg() & 0xFFFFFFFF, len("".join(out)) & 0xFFFFFFFF)
            val = ""

        else:
            val = "%" + spec

        # ── Width padding ─────────────────────────────────────────────────────
        if len(val) < width:
            pad_len = width - len(val)
            if left_align:
                val = val + " " * pad_len
            elif zero_pad and spec not in ("s", "c"):
                if val and val[0] in ("-", "+", " "):
                    sign_char = val[0]
                    val = sign_char + "0" * pad_len + val[1:]
                else:
                    val = "0" * pad_len + val
            else:
                val = " " * pad_len + val

        out.append(val)

    return "".join(out)


# ── Main registration function ────────────────────────────────────────────────

def register_msvcrt_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register all msvcrt.dll handlers."""

    # ── Static initializers — UNIMPLEMENTED (halt loudly) ────────────────────

    # _initterm(PVOID* pfbegin, PVOID* pfend) -> void [cdecl]
    # Spec: calls each non-null function pointer in [pfbegin, pfend) — C++ static initializers.
    # UNIMPLEMENTED: cannot call back into game code from handler yet.
    def _initterm(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] _initterm — halting")
        cpu.halted = True

    stubs.register_handler("msvcrt.dll", "_initterm", _initterm)

    # _initterm_e(PVOID* pfbegin, PVOID* pfend) -> int [cdecl]
    # Spec: same as _initterm but each function returns int; non-zero aborts startup.
    # UNIMPLEMENTED: same reasoning as _initterm.
    def _initterm_e(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] _initterm_e — halting")
        cpu.halted = True

    stubs.register_handler("msvcrt.dll", "_initterm_e", _initterm_e)

    # ── __set_app_type — no-op (cdecl, caller cleans) ────────────────────────

    # __set_app_type(int apptype) -> void [cdecl]
    def _set_app_type(cpu: "CPU") -> None:
        pass  # cdecl: caller cleans up args; no state change needed

    stubs.register_handler("msvcrt.dll", "__set_app_type", _set_app_type)

    # ── __p__fmode — return pointer to _fmode global ─────────────────────────

    # __p__fmode() -> int* [cdecl]
    # CRT reads the result as a pointer to the global _fmode (int) and sets it
    # to _O_TEXT (0) or _O_BINARY (0x8000). We return a pointer to a zeroed DWORD.
    memory.write32(_FMODE_ADDR, 0)  # _O_TEXT

    def _p_fmode(cpu: "CPU") -> None:
        cpu.regs[EAX] = _FMODE_ADDR

    stubs.register_handler("msvcrt.dll", "__p__fmode", _p_fmode)

    # ── __p__commode — return pointer to _commode global ─────────────────────

    # __p__commode() -> int* [cdecl]
    memory.write32(_COMMODE_ADDR, 0)

    def _p_commode(cpu: "CPU") -> None:
        cpu.regs[EAX] = _COMMODE_ADDR

    stubs.register_handler("msvcrt.dll", "__p__commode", _p_commode)

    # ── _controlfp — return default FP control word ───────────────────────────

    # _controlfp(unsigned int new, unsigned int mask) -> unsigned int [cdecl]
    def _controlfp(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0x0001001F  # default FP control word
        # cdecl: caller cleans up

    stubs.register_handler("msvcrt.dll", "_controlfp", _controlfp)

    # ── _except_handler3 — SEH handler ───────────────────────────────────────

    # _except_handler3 — SEH handler [cdecl]
    def _except_handler3(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1  # ExceptionContinueSearch
        # cdecl: caller cleans up args

    stubs.register_handler("msvcrt.dll", "_except_handler3", _except_handler3)

    # ── __getmainargs — set up argc/argv ─────────────────────────────────────

    # __getmainargs data: fixed region at 0x210050+
    # argv[0] points to "MCity_d.exe\0" at _CMD_LINE_ADDR; argv[1] = NULL
    memory.write32(_ARGV_ARRAY_ADDR,     _CMD_LINE_ADDR)  # argv[0] = "MCity_d.exe"
    memory.write32(_ARGV_ARRAY_ADDR + 4, 0)               # NULL terminator
    memory.write32(_ARGC_ADDR,  1)
    memory.write32(_ARGV_ADDR,  _ARGV_ARRAY_ADDR)
    memory.write32(_ENVP_ADDR,  _ENV_STR_A_ADDR)

    # __getmainargs(int* argc, char*** argv, char*** envp, int doWildCard, _startupinfo*) -> int [cdecl]
    # CRT writes returned pointers to its own globals; game reads argc/argv to decide
    # multiplayer vs single-player mode. We supply argc=1, argv=["MCity_d.exe"].
    def _getmainargs(cpu: "CPU") -> None:
        p_argc = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        p_argv = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        p_envp = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        memory.write32(p_argc, 1)
        memory.write32(p_argv, _ARGV_ARRAY_ADDR)
        memory.write32(p_envp, _ENV_STR_A_ADDR)
        cpu.regs[EAX] = 0  # success
        # cdecl: caller cleans up

    stubs.register_handler("msvcrt.dll", "__getmainargs", _getmainargs)

    # ── Heap allocators ───────────────────────────────────────────────────────

    # malloc(size_t size) -> void* [cdecl]
    def _malloc(cpu: "CPU") -> None:
        size = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = state.simple_alloc(size) if size > 0 else 0

    stubs.register_handler("msvcrt.dll", "malloc", _malloc)

    # _malloc_crt(size_t size) -> void* [cdecl] — alias for malloc
    def _malloc_crt(cpu: "CPU") -> None:
        size = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = state.simple_alloc(size) if size > 0 else 0

    stubs.register_handler("msvcrt.dll", "_malloc_crt", _malloc_crt)

    # calloc(size_t num, size_t size) -> void* [cdecl]
    # simpleAlloc memory is already zeroed by the bump allocator.
    def _calloc(cpu: "CPU") -> None:
        num  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        size = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        total = (num * size) & 0xFFFFFFFF
        cpu.regs[EAX] = state.simple_alloc(total) if total > 0 else 0

    stubs.register_handler("msvcrt.dll", "calloc", _calloc)

    # realloc(void* ptr, size_t size) -> void* [cdecl]
    # Old data is not copied (bump allocator; good enough for game init paths).
    def _realloc(cpu: "CPU") -> None:
        size = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        cpu.regs[EAX] = state.simple_alloc(size) if size > 0 else 0

    stubs.register_handler("msvcrt.dll", "realloc", _realloc)

    # free(void* ptr) -> void [cdecl]
    # No-op: bump allocator does not support freeing.
    def _free(cpu: "CPU") -> None:
        pass  # bump allocator; nothing to free

    stubs.register_handler("msvcrt.dll", "free", _free)

    # operator new(size_t size) -> void* [cdecl]  (MSVC mangled name)
    def _operator_new(cpu: "CPU") -> None:
        size = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = state.simple_alloc(size) if size > 0 else 0

    stubs.register_handler("msvcrt.dll", "??2@YAPAXI@Z", _operator_new)

    # operator delete(void* ptr) -> void [cdecl]  (MSVC mangled name)
    # No-op: bump allocator has no free.
    def _operator_delete(cpu: "CPU") -> None:
        pass

    stubs.register_handler("msvcrt.dll", "??3@YAXPAX@Z", _operator_delete)

    # ── FILE I/O ──────────────────────────────────────────────────────────────

    # fopen(const char* filename, const char* mode) -> FILE* [cdecl]
    def _fopen(cpu: "CPU") -> None:
        filename_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        mode_ptr     = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        filename = read_cstring(filename_ptr, memory)
        mode     = read_cstring(mode_ptr, memory)
        writable = "w" in mode or "a" in mode
        handle = state.open_file_handle(filename, writable)
        cpu.regs[EAX] = 0 if handle == 0xFFFFFFFF else handle

    stubs.register_handler("msvcrt.dll", "fopen", _fopen)

    # _fopen(const char* filename, const char* mode) -> FILE* [cdecl]
    def _fopen_underscore(cpu: "CPU") -> None:
        filename_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        mode_ptr     = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        filename = read_cstring(filename_ptr, memory)
        mode     = read_cstring(mode_ptr, memory)
        writable = "w" in mode or "a" in mode
        handle = state.open_file_handle(filename, writable)
        cpu.regs[EAX] = 0 if handle == 0xFFFFFFFF else handle

    stubs.register_handler("msvcrt.dll", "_fopen", _fopen_underscore)

    # fclose(FILE* stream) -> int [cdecl]
    def _fclose(cpu: "CPU") -> None:
        stream = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        state.file_handle_map.pop(stream, None)
        cpu.regs[EAX] = 0  # 0 = success

    stubs.register_handler("msvcrt.dll", "fclose", _fclose)

    # fread(void* ptr, size_t size, size_t count, FILE* stream) -> size_t [cdecl]
    def _fread(cpu: "CPU") -> None:
        ptr    = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        size   = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        count  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        stream = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(stream)
        if entry is None or entry.writable or size == 0:
            cpu.regs[EAX] = 0
            return
        total_bytes = size * count
        available   = len(entry.data) - entry.position
        to_read     = min(total_bytes, available)
        for idx in range(to_read):
            memory.write8(ptr + idx, entry.data[entry.position + idx])
        entry.position += to_read
        cpu.regs[EAX] = to_read // size  # full items read

    stubs.register_handler("msvcrt.dll", "fread", _fread)

    # fwrite(const void* ptr, size_t size, size_t count, FILE* stream) -> size_t [cdecl]
    # Claims all items written; discards data (no real file output needed here).
    def _fwrite(cpu: "CPU") -> None:
        count = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cpu.regs[EAX] = count  # claim all items written

    stubs.register_handler("msvcrt.dll", "fwrite", _fwrite)

    # fseek(FILE* stream, long offset, int whence) -> int [cdecl]
    def _fseek(cpu: "CPU") -> None:
        stream = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        raw    = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        offset = raw if raw < 0x80000000 else (raw - 0x100000000)  # signed 32-bit
        whence = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(stream)
        if entry is None:
            cpu.regs[EAX] = 0xFFFFFFFF  # -1 = error
            return
        if whence == 0:        # SEEK_SET
            entry.position = offset
        elif whence == 1:      # SEEK_CUR
            entry.position = entry.position + offset
        else:                  # SEEK_END
            entry.position = len(entry.data) + offset
        entry.position = max(0, min(entry.position, len(entry.data)))
        cpu.regs[EAX] = 0  # 0 = success

    stubs.register_handler("msvcrt.dll", "fseek", _fseek)

    # ftell(FILE* stream) -> long [cdecl]
    def _ftell(cpu: "CPU") -> None:
        stream = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(stream)
        cpu.regs[EAX] = (entry.position & 0xFFFFFFFF) if entry is not None else 0xFFFFFFFF

    stubs.register_handler("msvcrt.dll", "ftell", _ftell)

    # feof(FILE* stream) -> int [cdecl]
    def _feof(cpu: "CPU") -> None:
        stream = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(stream)
        cpu.regs[EAX] = 1 if (entry is not None and entry.position >= len(entry.data)) else 0

    stubs.register_handler("msvcrt.dll", "feof", _feof)

    # fgets(char* str, int n, FILE* stream) -> char* [cdecl]
    def _fgets(cpu: "CPU") -> None:
        str_ptr = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        n_raw   = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        stream  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        n       = n_raw if n_raw < 0x80000000 else (n_raw - 0x100000000)  # signed
        entry = state.file_handle_map.get(stream)
        if entry is None or entry.position >= len(entry.data) or n <= 0:
            cpu.regs[EAX] = 0  # NULL = EOF or error
            return
        idx = 0
        while idx < n - 1 and entry.position < len(entry.data):
            ch = entry.data[entry.position]
            entry.position += 1
            memory.write8(str_ptr + idx, ch)
            idx += 1
            if ch == 0x0A:  # '\n'
                break
        memory.write8(str_ptr + idx, 0)  # null terminator
        cpu.regs[EAX] = str_ptr  # return str on success

    stubs.register_handler("msvcrt.dll", "fgets", _fgets)

    # rewind(FILE* stream) -> void [cdecl]
    def _rewind(cpu: "CPU") -> None:
        stream = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(stream)
        if entry is not None:
            entry.position = 0

    stubs.register_handler("msvcrt.dll", "rewind", _rewind)

    # fprintf(FILE* stream, const char* format, ...) -> int [cdecl]
    # Discards output; returns 0.
    def _fprintf(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0

    stubs.register_handler("msvcrt.dll", "fprintf", _fprintf)

    # ── atexit / onexit — no-op (return arg) ─────────────────────────────────

    # __dllonexit(fn, pbegin, pend) -> fn [cdecl]
    def _dllonexit(cpu: "CPU") -> None:
        cpu.regs[EAX] = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)

    stubs.register_handler("msvcrt.dll", "__dllonexit", _dllonexit)

    # _onexit(fn) -> fn [cdecl]
    def _onexit(cpu: "CPU") -> None:
        cpu.regs[EAX] = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)

    stubs.register_handler("msvcrt.dll", "_onexit", _onexit)

    # _atexit(fn) -> int [cdecl]
    # Returns 0 (success) without registering; we do not run atexit callbacks.
    def _atexit(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0

    stubs.register_handler("msvcrt.dll", "_atexit", _atexit)

    # ── Character classification ───────────────────────────────────────────────

    # _isctype(int c, int type) -> int [cdecl]
    # Returns 0; game uses this for locale-aware classification which we don't support.
    def _isctype(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0

    stubs.register_handler("msvcrt.dll", "_isctype", _isctype)

    # ── C++ exception handler ─────────────────────────────────────────────────

    # __CxxFrameHandler: C++ exception frame dispatch — halt, we have no SEH
    def _cxx_frame_handler(cpu: "CPU") -> None:
        logger.error("handlers", "[UNIMPLEMENTED] __CxxFrameHandler — halting")
        cpu.halted = True

    stubs.register_handler("msvcrt.dll", "__CxxFrameHandler", _cxx_frame_handler)

    # ── printf-family ─────────────────────────────────────────────────────────

    # sprintf(char* dst, const char* fmt, ...) -> int [cdecl]
    def _sprintf(cpu: "CPU") -> None:
        dst     = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        fmt_ptr = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        fmt     = read_cstring(fmt_ptr, memory, 4096)
        arg_off = [12]  # mutable cell for closure

        def get_arg() -> int:
            v = memory.read32((cpu.regs[ESP] + arg_off[0]) & 0xFFFFFFFF)
            arg_off[0] += 4
            return v

        result = _sprintf_format(fmt, get_arg, memory)
        cpu.regs[EAX] = _write_cstring(dst, result, memory)

    stubs.register_handler("msvcrt.dll", "sprintf", _sprintf)

    # _snprintf(char* dst, size_t count, const char* fmt, ...) -> int [cdecl]
    def _snprintf(cpu: "CPU") -> None:
        dst     = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        count   = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        fmt_ptr = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        fmt     = read_cstring(fmt_ptr, memory, 4096)
        arg_off = [16]

        def get_arg() -> int:
            v = memory.read32((cpu.regs[ESP] + arg_off[0]) & 0xFFFFFFFF)
            arg_off[0] += 4
            return v

        result = _sprintf_format(fmt, get_arg, memory)
        # Truncate to count-1 to leave room for null terminator
        if count > 0:
            truncated = result[:count - 1]
            _write_cstring(dst, truncated, memory)
            cpu.regs[EAX] = len(result)  # return full length (like snprintf spec)
        else:
            cpu.regs[EAX] = len(result)

    stubs.register_handler("msvcrt.dll", "_snprintf", _snprintf)

    # vsprintf(char* dst, const char* fmt, va_list ap) -> int [cdecl]
    # va_list is a pointer to the first variadic argument in memory.
    def _vsprintf(cpu: "CPU") -> None:
        dst     = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        fmt_ptr = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        ap      = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        fmt     = read_cstring(fmt_ptr, memory, 4096)
        ap_off  = [0]

        def get_arg() -> int:
            v = memory.read32((ap + ap_off[0]) & 0xFFFFFFFF)
            ap_off[0] += 4
            return v

        result = _sprintf_format(fmt, get_arg, memory)
        cpu.regs[EAX] = _write_cstring(dst, result, memory)

    stubs.register_handler("msvcrt.dll", "vsprintf", _vsprintf)

    # printf(const char* fmt, ...) -> int [cdecl]
    def _printf(cpu: "CPU") -> None:
        fmt_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        fmt     = read_cstring(fmt_ptr, memory, 4096)
        arg_off = [8]

        def get_arg() -> int:
            v = memory.read32((cpu.regs[ESP] + arg_off[0]) & 0xFFFFFFFF)
            arg_off[0] += 4
            return v

        result = _sprintf_format(fmt, get_arg, memory)
        logger.info("handlers", f"[printf] {result.rstrip(chr(10) + chr(13))}")
        cpu.regs[EAX] = len(result)

    stubs.register_handler("msvcrt.dll", "printf", _printf)

    # sscanf(const char* str, const char* fmt, ...) -> int [cdecl]
    # Very limited implementation: handles %d, %u, %x, %s, %f, %c only.
    def _sscanf(cpu: "CPU") -> None:
        str_ptr  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        fmt_ptr  = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        src      = read_cstring(str_ptr, memory, 4096)
        fmt      = read_cstring(fmt_ptr, memory, 4096)

        arg_off  = [12]  # pointer args start at ESP+12

        def next_ptr() -> int:
            p = memory.read32((cpu.regs[ESP] + arg_off[0]) & 0xFFFFFFFF)
            arg_off[0] += 4
            return p

        src_pos  = 0
        matched  = 0
        fi       = 0
        fn       = len(fmt)

        while fi < fn and src_pos <= len(src):
            if fmt[fi] != "%":
                # Match literal character (skip whitespace in fmt matches any whitespace in src)
                if fmt[fi] == " ":
                    while src_pos < len(src) and src[src_pos] in " \t\n\r":
                        src_pos += 1
                elif src_pos < len(src) and src[src_pos] == fmt[fi]:
                    src_pos += 1
                fi += 1
                continue

            fi += 1  # skip '%'
            if fi >= fn:
                break

            # Optional width
            width_str = ""
            while fi < fn and fmt[fi].isdigit():
                width_str += fmt[fi]
                fi += 1
            max_width = int(width_str) if width_str else 0

            # Length modifier (ignore)
            if fi < fn and fmt[fi] in ("l", "h", "L"):
                fi += 1
                if fi < fn and fmt[fi] == "l":
                    fi += 1

            if fi >= fn:
                break
            spec = fmt[fi]
            fi += 1

            # Skip leading whitespace in source for most specifiers
            if spec not in ("c", "["):
                while src_pos < len(src) and src[src_pos] in " \t\n\r":
                    src_pos += 1

            if spec in ("d", "i"):
                # Read an integer (decimal, optionally signed)
                start = src_pos
                if src_pos < len(src) and src[src_pos] in "+-":
                    src_pos += 1
                while src_pos < len(src) and src[src_pos].isdigit():
                    if max_width and (src_pos - start) >= max_width:
                        break
                    src_pos += 1
                token = src[start:src_pos]
                if not token or token in ("+", "-"):
                    break
                val = int(token, 10) & 0xFFFFFFFF
                memory.write32(next_ptr(), val)
                matched += 1

            elif spec == "u":
                start = src_pos
                while src_pos < len(src) and src[src_pos].isdigit():
                    if max_width and (src_pos - start) >= max_width:
                        break
                    src_pos += 1
                token = src[start:src_pos]
                if not token:
                    break
                val = int(token, 10) & 0xFFFFFFFF
                memory.write32(next_ptr(), val)
                matched += 1

            elif spec == "x":
                start = src_pos
                if src_pos + 1 < len(src) and src[src_pos:src_pos + 2].lower() == "0x":
                    src_pos += 2
                while src_pos < len(src) and src[src_pos] in "0123456789abcdefABCDEF":
                    if max_width and (src_pos - start) >= max_width:
                        break
                    src_pos += 1
                token = src[start:src_pos]
                if not token:
                    break
                val = int(token, 16) & 0xFFFFFFFF
                memory.write32(next_ptr(), val)
                matched += 1

            elif spec in ("f", "e", "g"):
                start = src_pos
                if src_pos < len(src) and src[src_pos] in "+-":
                    src_pos += 1
                while src_pos < len(src) and (src[src_pos].isdigit() or src[src_pos] in ".eE+-"):
                    if max_width and (src_pos - start) >= max_width:
                        break
                    src_pos += 1
                token = src[start:src_pos]
                if not token:
                    break
                try:
                    fval = float(token)
                except ValueError:
                    break
                # Write as 32-bit float (single precision) to the pointer
                packed = struct.pack("<f", fval)
                val32  = struct.unpack("<I", packed)[0]
                memory.write32(next_ptr(), val32)
                matched += 1

            elif spec == "s":
                start = src_pos
                while src_pos < len(src) and src[src_pos] not in " \t\n\r":
                    if max_width and (src_pos - start) >= max_width:
                        break
                    src_pos += 1
                token = src[start:src_pos]
                dst_ptr = next_ptr()
                for k, ch in enumerate(token):
                    memory.write8(dst_ptr + k, ord(ch) & 0xFF)
                memory.write8(dst_ptr + len(token), 0)
                matched += 1

            elif spec == "c":
                if src_pos < len(src):
                    memory.write8(next_ptr(), ord(src[src_pos]) & 0xFF)
                    src_pos += 1
                    matched += 1
                else:
                    break

            else:
                # Unknown specifier — consume pointer arg and advance
                next_ptr()

        cpu.regs[EAX] = matched

    stubs.register_handler("msvcrt.dll", "sscanf", _sscanf)

    # ── String functions ───────────────────────────────────────────────────────

    # strlen(const char* s) -> size_t [cdecl]
    def _strlen(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        length = 0
        while memory.read8(ptr + length) != 0:
            length += 1
        cpu.regs[EAX] = length

    stubs.register_handler("msvcrt.dll", "strlen", _strlen)

    # strcpy(char* dst, const char* src) -> char* [cdecl]
    def _strcpy(cpu: "CPU") -> None:
        dst = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        src = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        i = 0
        while True:
            ch = memory.read8(src + i)
            memory.write8(dst + i, ch)
            if ch == 0:
                break
            i += 1
        cpu.regs[EAX] = dst

    stubs.register_handler("msvcrt.dll", "strcpy", _strcpy)

    # strncpy(char* dst, const char* src, size_t n) -> char* [cdecl]
    def _strncpy(cpu: "CPU") -> None:
        dst = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        src = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        n   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        i = 0
        while i < n:
            ch = memory.read8(src + i)
            memory.write8(dst + i, ch)
            i += 1
            if ch == 0:
                break
        # Pad remainder with zeros
        while i < n:
            memory.write8(dst + i, 0)
            i += 1
        cpu.regs[EAX] = dst

    stubs.register_handler("msvcrt.dll", "strncpy", _strncpy)

    # strcat(char* dst, const char* src) -> char* [cdecl]
    def _strcat(cpu: "CPU") -> None:
        dst = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        src = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        # Find end of dst
        end = 0
        while memory.read8(dst + end) != 0:
            end += 1
        # Append src
        i = 0
        while True:
            ch = memory.read8(src + i)
            memory.write8(dst + end + i, ch)
            if ch == 0:
                break
            i += 1
        cpu.regs[EAX] = dst

    stubs.register_handler("msvcrt.dll", "strcat", _strcat)

    # strcmp(const char* s1, const char* s2) -> int [cdecl]
    def _strcmp(cpu: "CPU") -> None:
        s1 = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        s2 = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        i = 0
        while True:
            a = memory.read8(s1 + i)
            b = memory.read8(s2 + i)
            if a != b:
                cpu.regs[EAX] = (1 if a > b else 0xFFFFFFFF) & 0xFFFFFFFF
                return
            if a == 0:
                cpu.regs[EAX] = 0
                return
            i += 1

    stubs.register_handler("msvcrt.dll", "strcmp", _strcmp)

    # strncmp(const char* s1, const char* s2, size_t n) -> int [cdecl]
    def _strncmp(cpu: "CPU") -> None:
        s1 = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        s2 = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        n  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        i = 0
        while i < n:
            a = memory.read8(s1 + i)
            b = memory.read8(s2 + i)
            if a != b:
                cpu.regs[EAX] = (1 if a > b else 0xFFFFFFFF) & 0xFFFFFFFF
                return
            if a == 0:
                cpu.regs[EAX] = 0
                return
            i += 1
        cpu.regs[EAX] = 0

    stubs.register_handler("msvcrt.dll", "strncmp", _strncmp)

    # strchr(const char* s, int c) -> char* [cdecl]
    def _strchr(cpu: "CPU") -> None:
        s   = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        c   = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF) & 0xFF
        i = 0
        while True:
            ch = memory.read8(s + i)
            if ch == c:
                cpu.regs[EAX] = (s + i) & 0xFFFFFFFF
                return
            if ch == 0:
                break
            i += 1
        cpu.regs[EAX] = 0  # NULL

    stubs.register_handler("msvcrt.dll", "strchr", _strchr)

    # strrchr(const char* s, int c) -> char* [cdecl]
    def _strrchr(cpu: "CPU") -> None:
        s   = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        c   = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF) & 0xFF
        last = 0  # NULL (not found)
        i = 0
        while True:
            ch = memory.read8(s + i)
            if ch == c:
                last = (s + i) & 0xFFFFFFFF
            if ch == 0:
                break
            i += 1
        cpu.regs[EAX] = last

    stubs.register_handler("msvcrt.dll", "strrchr", _strrchr)

    # strstr(const char* haystack, const char* needle) -> char* [cdecl]
    def _strstr(cpu: "CPU") -> None:
        haystack_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        needle_ptr   = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        haystack = read_cstring(haystack_ptr, memory, 4096)
        needle   = read_cstring(needle_ptr,   memory, 4096)
        if not needle:
            cpu.regs[EAX] = haystack_ptr
            return
        idx = haystack.find(needle)
        cpu.regs[EAX] = (haystack_ptr + idx) & 0xFFFFFFFF if idx >= 0 else 0

    stubs.register_handler("msvcrt.dll", "strstr", _strstr)

    # ── Memory functions ───────────────────────────────────────────────────────

    # memcpy(void* dst, const void* src, size_t n) -> void* [cdecl]
    def _memcpy(cpu: "CPU") -> None:
        dst = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        src = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        n   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        for idx in range(n):
            memory.write8(dst + idx, memory.read8(src + idx))
        cpu.regs[EAX] = dst

    stubs.register_handler("msvcrt.dll", "memcpy", _memcpy)

    # memmove(void* dst, const void* src, size_t n) -> void* [cdecl]
    # Handles overlapping regions correctly by choosing copy direction.
    def _memmove(cpu: "CPU") -> None:
        dst = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        src = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        n   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if dst <= src or dst >= src + n:
            # No overlap or dst is before src: copy forward
            for idx in range(n):
                memory.write8(dst + idx, memory.read8(src + idx))
        else:
            # Overlap with dst after src: copy backward to avoid corruption
            for idx in range(n - 1, -1, -1):
                memory.write8(dst + idx, memory.read8(src + idx))
        cpu.regs[EAX] = dst

    stubs.register_handler("msvcrt.dll", "memmove", _memmove)

    # memset(void* ptr, int value, size_t n) -> void* [cdecl]
    def _memset(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        val = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF) & 0xFF
        n   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        for idx in range(n):
            memory.write8(ptr + idx, val)
        cpu.regs[EAX] = ptr

    stubs.register_handler("msvcrt.dll", "memset", _memset)

    # memcmp(const void* p1, const void* p2, size_t n) -> int [cdecl]
    def _memcmp(cpu: "CPU") -> None:
        p1 = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        p2 = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        n  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        for idx in range(n):
            a = memory.read8(p1 + idx)
            b = memory.read8(p2 + idx)
            if a != b:
                cpu.regs[EAX] = (1 if a > b else 0xFFFFFFFF) & 0xFFFFFFFF
                return
        cpu.regs[EAX] = 0

    stubs.register_handler("msvcrt.dll", "memcmp", _memcmp)

    # ── Character classification / conversion ─────────────────────────────────

    # toupper(int c) -> int [cdecl]
    def _toupper(cpu: "CPU") -> None:
        c = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF) & 0xFF
        cpu.regs[EAX] = ord(chr(c).upper()) if 0 < c < 128 else c

    stubs.register_handler("msvcrt.dll", "toupper", _toupper)

    # tolower(int c) -> int [cdecl]
    def _tolower(cpu: "CPU") -> None:
        c = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF) & 0xFF
        cpu.regs[EAX] = ord(chr(c).lower()) if 0 < c < 128 else c

    stubs.register_handler("msvcrt.dll", "tolower", _tolower)

    # isdigit(int c) -> int [cdecl]
    def _isdigit(cpu: "CPU") -> None:
        c = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF) & 0xFF
        cpu.regs[EAX] = 1 if chr(c).isdigit() else 0

    stubs.register_handler("msvcrt.dll", "isdigit", _isdigit)

    # isalpha(int c) -> int [cdecl]
    def _isalpha(cpu: "CPU") -> None:
        c = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF) & 0xFF
        cpu.regs[EAX] = 1 if chr(c).isalpha() else 0

    stubs.register_handler("msvcrt.dll", "isalpha", _isalpha)

    # isalnum(int c) -> int [cdecl]
    def _isalnum(cpu: "CPU") -> None:
        c = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF) & 0xFF
        cpu.regs[EAX] = 1 if chr(c).isalnum() else 0

    stubs.register_handler("msvcrt.dll", "isalnum", _isalnum)

    # isspace(int c) -> int [cdecl]
    def _isspace(cpu: "CPU") -> None:
        c = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF) & 0xFF
        cpu.regs[EAX] = 1 if chr(c) in " \t\n\r\x0b\x0c" else 0

    stubs.register_handler("msvcrt.dll", "isspace", _isspace)

    # isupper(int c) -> int [cdecl]
    def _isupper(cpu: "CPU") -> None:
        c = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF) & 0xFF
        cpu.regs[EAX] = 1 if chr(c).isupper() else 0

    stubs.register_handler("msvcrt.dll", "isupper", _isupper)

    # islower(int c) -> int [cdecl]
    def _islower(cpu: "CPU") -> None:
        c = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF) & 0xFF
        cpu.regs[EAX] = 1 if chr(c).islower() else 0

    stubs.register_handler("msvcrt.dll", "islower", _islower)

    # isprint(int c) -> int [cdecl]
    def _isprint(cpu: "CPU") -> None:
        c = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF) & 0xFF
        cpu.regs[EAX] = 1 if chr(c).isprintable() else 0

    stubs.register_handler("msvcrt.dll", "isprint", _isprint)

    # ── String-to-number conversions ──────────────────────────────────────────

    # atoi(const char* str) -> int [cdecl]
    def _atoi(cpu: "CPU") -> None:
        s = read_cstring(memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF), memory)
        try:
            val = int(s.strip(), 10)
        except ValueError:
            val = 0
        # Clamp to signed 32-bit range (matches C behavior for atoi)
        val = val & 0xFFFFFFFF
        cpu.regs[EAX] = val

    stubs.register_handler("msvcrt.dll", "atoi", _atoi)

    # atol(const char* str) -> long [cdecl]
    def _atol(cpu: "CPU") -> None:
        s = read_cstring(memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF), memory)
        try:
            val = int(s.strip(), 10)
        except ValueError:
            val = 0
        cpu.regs[EAX] = val & 0xFFFFFFFF

    stubs.register_handler("msvcrt.dll", "atol", _atol)

    # atof(const char* str) -> double [cdecl]
    # Returns result on FPU ST(0) (pushed), like most double-returning CRT functions.
    def _atof(cpu: "CPU") -> None:
        s = read_cstring(memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF), memory)
        try:
            val = float(s.strip())
        except ValueError:
            val = 0.0
        cpu.fpu_top = (cpu.fpu_top - 1) & 7
        cpu.fpu_stack[cpu.fpu_top] = val

    stubs.register_handler("msvcrt.dll", "atof", _atof)

    # strtol(const char* str, char** endptr, int base) -> long [cdecl]
    def _strtol(cpu: "CPU") -> None:
        str_ptr  = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        end_ptr  = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        base     = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        s = read_cstring(str_ptr, memory)
        s_stripped = s.lstrip(" \t\n\r")
        try:
            val = int(s_stripped, base if base != 0 else 10)
        except ValueError:
            val = 0
        cpu.regs[EAX] = val & 0xFFFFFFFF
        # endptr: point past consumed chars (approximate — point to end of string)
        if end_ptr != 0:
            memory.write32(end_ptr, (str_ptr + len(s)) & 0xFFFFFFFF)

    stubs.register_handler("msvcrt.dll", "strtol", _strtol)

    # strtoul(const char* str, char** endptr, int base) -> unsigned long [cdecl]
    def _strtoul(cpu: "CPU") -> None:
        str_ptr  = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        end_ptr  = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        base     = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        s = read_cstring(str_ptr, memory)
        s_stripped = s.lstrip(" \t\n\r")
        try:
            val = int(s_stripped, base if base != 0 else 10)
        except ValueError:
            val = 0
        cpu.regs[EAX] = val & 0xFFFFFFFF
        if end_ptr != 0:
            memory.write32(end_ptr, (str_ptr + len(s)) & 0xFFFFFFFF)

    stubs.register_handler("msvcrt.dll", "strtoul", _strtoul)

    # strtod(const char* str, char** endptr) -> double [cdecl]
    # Returns result on FPU ST(0).
    def _strtod(cpu: "CPU") -> None:
        str_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        end_ptr = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        s = read_cstring(str_ptr, memory)
        s_stripped = s.lstrip(" \t\n\r")
        try:
            val = float(s_stripped)
        except ValueError:
            val = 0.0
        cpu.fpu_top = (cpu.fpu_top - 1) & 7
        cpu.fpu_stack[cpu.fpu_top] = val
        if end_ptr != 0:
            memory.write32(end_ptr, (str_ptr + len(s)) & 0xFFFFFFFF)

    stubs.register_handler("msvcrt.dll", "strtod", _strtod)

    # ── Math ───────────────────────────────────────────────────────────────────

    # abs(int x) -> int [cdecl]
    def _abs(cpu: "CPU") -> None:
        raw = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        n   = raw if raw < 0x80000000 else (raw - 0x100000000)  # signed
        cpu.regs[EAX] = abs(n) & 0xFFFFFFFF

    stubs.register_handler("msvcrt.dll", "abs", _abs)

    # rand() -> int [cdecl]
    # Returns a pseudo-random number in [0, 32767]. We use Python's random module.
    import random as _random

    def _rand(cpu: "CPU") -> None:
        cpu.regs[EAX] = _random.randint(0, 32767)

    stubs.register_handler("msvcrt.dll", "rand", _rand)

    # srand(unsigned int seed) -> void [cdecl]
    def _srand(cpu: "CPU") -> None:
        seed = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        _random.seed(seed)

    stubs.register_handler("msvcrt.dll", "srand", _srand)

    # ── FPU math functions — args on stack as 64-bit doubles ──────────────────
    # Convention for double-returning functions: push result to FPU ST(0).
    # These use cdecl; the 64-bit double argument sits at ESP+4 (lo) and ESP+8 (hi).

    def _read_double_arg(cpu: "CPU", off: int = 4) -> float:
        lo = memory.read32((cpu.regs[ESP] + off)     & 0xFFFFFFFF)
        hi = memory.read32((cpu.regs[ESP] + off + 4) & 0xFFFFFFFF)
        return struct.unpack("<d", struct.pack("<II", lo, hi))[0]

    def _push_fpu(cpu: "CPU", val: float) -> None:
        cpu.fpu_top = (cpu.fpu_top - 1) & 7
        cpu.fpu_stack[cpu.fpu_top] = val

    # floor(double x) -> double [cdecl]
    def _floor(cpu: "CPU") -> None:
        f = _read_double_arg(cpu)
        _push_fpu(cpu, math.floor(f))

    stubs.register_handler("msvcrt.dll", "floor", _floor)

    # ceil(double x) -> double [cdecl]
    def _ceil(cpu: "CPU") -> None:
        f = _read_double_arg(cpu)
        _push_fpu(cpu, math.ceil(f))

    stubs.register_handler("msvcrt.dll", "ceil", _ceil)

    # sqrt(double x) -> double [cdecl]
    def _sqrt(cpu: "CPU") -> None:
        f = _read_double_arg(cpu)
        _push_fpu(cpu, math.sqrt(f) if f >= 0.0 else float("nan"))

    stubs.register_handler("msvcrt.dll", "sqrt", _sqrt)

    # sin(double x) -> double [cdecl]
    def _sin(cpu: "CPU") -> None:
        _push_fpu(cpu, math.sin(_read_double_arg(cpu)))

    stubs.register_handler("msvcrt.dll", "sin", _sin)

    # cos(double x) -> double [cdecl]
    def _cos(cpu: "CPU") -> None:
        _push_fpu(cpu, math.cos(_read_double_arg(cpu)))

    stubs.register_handler("msvcrt.dll", "cos", _cos)

    # tan(double x) -> double [cdecl]
    def _tan(cpu: "CPU") -> None:
        _push_fpu(cpu, math.tan(_read_double_arg(cpu)))

    stubs.register_handler("msvcrt.dll", "tan", _tan)

    # atan(double x) -> double [cdecl]
    def _atan(cpu: "CPU") -> None:
        _push_fpu(cpu, math.atan(_read_double_arg(cpu)))

    stubs.register_handler("msvcrt.dll", "atan", _atan)

    # atan2(double y, double x) -> double [cdecl]
    # Stack: y at ESP+4, x at ESP+12 (each occupies 8 bytes).
    def _atan2(cpu: "CPU") -> None:
        y = _read_double_arg(cpu, 4)
        x = _read_double_arg(cpu, 12)
        _push_fpu(cpu, math.atan2(y, x))

    stubs.register_handler("msvcrt.dll", "atan2", _atan2)

    # log(double x) -> double [cdecl]
    def _log(cpu: "CPU") -> None:
        f = _read_double_arg(cpu)
        _push_fpu(cpu, math.log(f) if f > 0.0 else float("-inf") if f == 0.0 else float("nan"))

    stubs.register_handler("msvcrt.dll", "log", _log)

    # exp(double x) -> double [cdecl]
    def _exp(cpu: "CPU") -> None:
        _push_fpu(cpu, math.exp(_read_double_arg(cpu)))

    stubs.register_handler("msvcrt.dll", "exp", _exp)

    # fmod(double x, double y) -> double [cdecl]
    # Stack: x at ESP+4, y at ESP+12.
    def _fmod(cpu: "CPU") -> None:
        x = _read_double_arg(cpu, 4)
        y = _read_double_arg(cpu, 12)
        _push_fpu(cpu, math.fmod(x, y) if y != 0.0 else float("nan"))

    stubs.register_handler("msvcrt.dll", "fmod", _fmod)

    # pow(double x, double y) -> double [cdecl]
    # Stack: x at ESP+4, y at ESP+12.
    def _pow(cpu: "CPU") -> None:
        x = _read_double_arg(cpu, 4)
        y = _read_double_arg(cpu, 12)
        try:
            _push_fpu(cpu, x ** y)
        except (ValueError, ZeroDivisionError):
            _push_fpu(cpu, float("nan"))

    stubs.register_handler("msvcrt.dll", "pow", _pow)

    # fabs(double x) -> double [cdecl]
    def _fabs(cpu: "CPU") -> None:
        _push_fpu(cpu, abs(_read_double_arg(cpu)))

    stubs.register_handler("msvcrt.dll", "fabs", _fabs)

    # ── FPU intrinsics — operands already on FPU stack ─────────────────────────

    # _ftol(): convert FPU ST(0) to 32-bit integer in EAX, pop ST(0) [cdecl, no stack args]
    # Truncates toward zero (same as C's (int) cast).
    def _ftol(cpu: "CPU") -> None:
        val = cpu.fpu_stack[cpu.fpu_top & 7]
        cpu.fpu_top = (cpu.fpu_top + 1) & 7  # pop ST(0)
        cpu.regs[EAX] = (math.trunc(val) & 0xFFFFFFFF)

    stubs.register_handler("msvcrt.dll", "_ftol", _ftol)

    # _CIpow(): ST(0)=y (exponent), ST(1)=x (base) → ST(0) = pow(x, y) [cdecl, no stack args]
    # Pops both operands, pushes result.
    def _CIpow(cpu: "CPU") -> None:
        y = cpu.fpu_stack[cpu.fpu_top & 7]
        x = cpu.fpu_stack[(cpu.fpu_top + 1) & 7]
        cpu.fpu_top = (cpu.fpu_top + 1) & 7  # consume y (ST0); x (ST1) becomes ST0
        try:
            cpu.fpu_stack[cpu.fpu_top & 7] = x ** y
        except (ValueError, ZeroDivisionError):
            cpu.fpu_stack[cpu.fpu_top & 7] = float("nan")

    stubs.register_handler("msvcrt.dll", "_CIpow", _CIpow)

    # _CIsqrt(): ST(0) = sqrt(ST(0)) [cdecl, no stack args]
    def _CIsqrt(cpu: "CPU") -> None:
        val = cpu.fpu_stack[cpu.fpu_top & 7]
        cpu.fpu_stack[cpu.fpu_top & 7] = math.sqrt(val) if val >= 0.0 else float("nan")

    stubs.register_handler("msvcrt.dll", "_CIsqrt", _CIsqrt)

    # _CIsin(): ST(0) = sin(ST(0)) [cdecl, no stack args]
    def _CIsin(cpu: "CPU") -> None:
        cpu.fpu_stack[cpu.fpu_top & 7] = math.sin(cpu.fpu_stack[cpu.fpu_top & 7])

    stubs.register_handler("msvcrt.dll", "_CIsin", _CIsin)

    # _CIcos(): ST(0) = cos(ST(0)) [cdecl, no stack args]
    def _CIcos(cpu: "CPU") -> None:
        cpu.fpu_stack[cpu.fpu_top & 7] = math.cos(cpu.fpu_stack[cpu.fpu_top & 7])

    stubs.register_handler("msvcrt.dll", "_CIcos", _CIcos)

    # _CItan(): ST(0) = tan(ST(0)) [cdecl, no stack args]
    def _CItan(cpu: "CPU") -> None:
        cpu.fpu_stack[cpu.fpu_top & 7] = math.tan(cpu.fpu_stack[cpu.fpu_top & 7])

    stubs.register_handler("msvcrt.dll", "_CItan", _CItan)

    # _CIatan(): ST(0) = atan(ST(0)) [cdecl, no stack args]
    def _CIatan(cpu: "CPU") -> None:
        cpu.fpu_stack[cpu.fpu_top & 7] = math.atan(cpu.fpu_stack[cpu.fpu_top & 7])

    stubs.register_handler("msvcrt.dll", "_CIatan", _CIatan)

    # _CIatan2(): ST(0)=x, ST(1)=y → ST(0) = atan2(y, x) [cdecl, no stack args]
    # Pops x, replaces ST(0) (formerly ST(1)) with atan2(y, x).
    def _CIatan2(cpu: "CPU") -> None:
        x = cpu.fpu_stack[cpu.fpu_top & 7]
        y = cpu.fpu_stack[(cpu.fpu_top + 1) & 7]
        cpu.fpu_top = (cpu.fpu_top + 1) & 7  # pop x; y becomes ST(0)
        cpu.fpu_stack[cpu.fpu_top & 7] = math.atan2(y, x)

    stubs.register_handler("msvcrt.dll", "_CIatan2", _CIatan2)

    # _CIlog(): ST(0) = log(ST(0)) [cdecl, no stack args]
    def _CIlog(cpu: "CPU") -> None:
        val = cpu.fpu_stack[cpu.fpu_top & 7]
        cpu.fpu_stack[cpu.fpu_top & 7] = (
            math.log(val) if val > 0.0 else float("-inf") if val == 0.0 else float("nan")
        )

    stubs.register_handler("msvcrt.dll", "_CIlog", _CIlog)

    # _CIexp(): ST(0) = exp(ST(0)) [cdecl, no stack args]
    def _CIexp(cpu: "CPU") -> None:
        cpu.fpu_stack[cpu.fpu_top & 7] = math.exp(cpu.fpu_stack[cpu.fpu_top & 7])

    stubs.register_handler("msvcrt.dll", "_CIexp", _CIexp)

    # _CIfloor(): ST(0) = floor(ST(0)) [cdecl, no stack args]
    def _CIfloor(cpu: "CPU") -> None:
        cpu.fpu_stack[cpu.fpu_top & 7] = math.floor(cpu.fpu_stack[cpu.fpu_top & 7])

    stubs.register_handler("msvcrt.dll", "_CIfloor", _CIfloor)

    # _CIfmod(): ST(0)=y, ST(1)=x → ST(0) = fmod(x, y) [cdecl, no stack args]
    # Pops y, replaces ST(0) (formerly ST(1)) with fmod(x, y).
    def _CIfmod(cpu: "CPU") -> None:
        y = cpu.fpu_stack[cpu.fpu_top & 7]
        x = cpu.fpu_stack[(cpu.fpu_top + 1) & 7]
        cpu.fpu_top = (cpu.fpu_top + 1) & 7  # pop y; x becomes ST(0)
        cpu.fpu_stack[cpu.fpu_top & 7] = math.fmod(x, y) if y != 0.0 else float("nan")

    stubs.register_handler("msvcrt.dll", "_CIfmod", _CIfmod)

    # ── Time functions ─────────────────────────────────────────────────────────
    import time as _time_module

    # time(time_t* timer) -> time_t [cdecl]
    # Returns current Unix time; writes it to *timer if non-NULL.
    def _time(cpu: "CPU") -> None:
        t = int(_time_module.time()) & 0xFFFFFFFF
        timer_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if timer_ptr != 0:
            memory.write32(timer_ptr, t)
        cpu.regs[EAX] = t

    stubs.register_handler("msvcrt.dll", "time", _time)

    # localtime(const time_t* timer) -> struct tm* [cdecl]
    # Returns a pointer to a static tm struct in emulator memory.
    _LOCALTIME_BUF = 0x00210064  # 36 bytes for struct tm (9 ints × 4 bytes)

    def _localtime(cpu: "CPU") -> None:
        timer_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        t = memory.read32(timer_ptr) if timer_ptr != 0 else int(_time_module.time())
        tm = _time_module.localtime(t)
        memory.write32(_LOCALTIME_BUF +  0, tm.tm_sec)
        memory.write32(_LOCALTIME_BUF +  4, tm.tm_min)
        memory.write32(_LOCALTIME_BUF +  8, tm.tm_hour)
        memory.write32(_LOCALTIME_BUF + 12, tm.tm_mday)
        memory.write32(_LOCALTIME_BUF + 16, tm.tm_mon - 1)   # tm_mon: 0-based
        memory.write32(_LOCALTIME_BUF + 20, tm.tm_year - 1900)
        memory.write32(_LOCALTIME_BUF + 24, tm.tm_wday)
        memory.write32(_LOCALTIME_BUF + 28, tm.tm_yday - 1)  # 0-based
        memory.write32(_LOCALTIME_BUF + 32, 1 if tm.tm_isdst > 0 else 0)
        cpu.regs[EAX] = _LOCALTIME_BUF

    stubs.register_handler("msvcrt.dll", "localtime", _localtime)

    # gmtime(const time_t* timer) -> struct tm* [cdecl]
    _GMTIME_BUF = 0x00210090  # 36 bytes after localtime buffer

    def _gmtime(cpu: "CPU") -> None:
        timer_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        t = memory.read32(timer_ptr) if timer_ptr != 0 else int(_time_module.time())
        tm = _time_module.gmtime(t)
        memory.write32(_GMTIME_BUF +  0, tm.tm_sec)
        memory.write32(_GMTIME_BUF +  4, tm.tm_min)
        memory.write32(_GMTIME_BUF +  8, tm.tm_hour)
        memory.write32(_GMTIME_BUF + 12, tm.tm_mday)
        memory.write32(_GMTIME_BUF + 16, tm.tm_mon - 1)
        memory.write32(_GMTIME_BUF + 20, tm.tm_year - 1900)
        memory.write32(_GMTIME_BUF + 24, tm.tm_wday)
        memory.write32(_GMTIME_BUF + 28, tm.tm_yday - 1)
        memory.write32(_GMTIME_BUF + 32, 0)  # tm_isdst = 0 for UTC
        cpu.regs[EAX] = _GMTIME_BUF

    stubs.register_handler("msvcrt.dll", "gmtime", _gmtime)

    # mktime(struct tm* timeptr) -> time_t [cdecl]
    def _mktime(cpu: "CPU") -> None:
        ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        tm_sec  = memory.read32(ptr +  0)
        tm_min  = memory.read32(ptr +  4)
        tm_hour = memory.read32(ptr +  8)
        tm_mday = memory.read32(ptr + 12)
        tm_mon  = memory.read32(ptr + 16)
        tm_year = memory.read32(ptr + 20)
        try:
            import calendar
            t = int(calendar.timegm((
                tm_year + 1900, tm_mon + 1, tm_mday,
                tm_hour, tm_min, tm_sec, 0, 0, 0
            )))
        except (OverflowError, ValueError):
            t = 0xFFFFFFFF  # -1 (error)
        cpu.regs[EAX] = t & 0xFFFFFFFF

    stubs.register_handler("msvcrt.dll", "mktime", _mktime)

    # difftime(time_t end, time_t start) -> double [cdecl]
    # Returns end - start on FPU ST(0).
    def _difftime(cpu: "CPU") -> None:
        end_t   = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        start_t = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        diff = float(end_t - start_t)
        _push_fpu(cpu, diff)

    stubs.register_handler("msvcrt.dll", "difftime", _difftime)

    # clock() -> clock_t [cdecl]
    # Returns process CPU time in CLOCKS_PER_SEC units.  We use wall time * 1000.
    _CLOCK_START = _time_module.monotonic()

    def _clock(cpu: "CPU") -> None:
        elapsed_ms = int((_time_module.monotonic() - _CLOCK_START) * 1000)
        cpu.regs[EAX] = elapsed_ms & 0xFFFFFFFF

    stubs.register_handler("msvcrt.dll", "clock", _clock)

    # ── Low-level POSIX-style file I/O (_open, _close, _read, _write, _lseek) ──

    # _open(const char* path, int oflag, ...) -> int [cdecl]
    # Returns a file descriptor (handle value).
    def _open(cpu: "CPU") -> None:
        path_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        oflag    = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        path     = read_cstring(path_ptr, memory)
        # O_WRONLY=1, O_RDWR=2; anything with write bit is writable
        writable = bool(oflag & 0x3)
        handle = state.open_file_handle(path, writable)
        cpu.regs[EAX] = 0xFFFFFFFF if handle == 0xFFFFFFFF else handle  # -1 on error

    stubs.register_handler("msvcrt.dll", "_open", _open)

    # _close(int fd) -> int [cdecl]
    def _close(cpu: "CPU") -> None:
        fd = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        state.file_handle_map.pop(fd, None)
        cpu.regs[EAX] = 0  # 0 = success

    stubs.register_handler("msvcrt.dll", "_close", _close)

    # _read(int fd, void* buf, unsigned int count) -> int [cdecl]
    def _read(cpu: "CPU") -> None:
        fd    = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        buf   = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        count = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        entry = state.file_handle_map.get(fd)
        if entry is None or entry.writable:
            cpu.regs[EAX] = 0xFFFFFFFF  # -1 = error
            return
        available = len(entry.data) - entry.position
        to_read   = min(count, available)
        for idx in range(to_read):
            memory.write8(buf + idx, entry.data[entry.position + idx])
        entry.position += to_read
        cpu.regs[EAX] = to_read

    stubs.register_handler("msvcrt.dll", "_read", _read)

    # _write(int fd, const void* buf, unsigned int count) -> int [cdecl]
    # Claims all bytes written; discards data.
    def _write(cpu: "CPU") -> None:
        count = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        cpu.regs[EAX] = count  # claim all bytes written

    stubs.register_handler("msvcrt.dll", "_write", _write)

    # _lseek(int fd, long offset, int whence) -> long [cdecl]
    def _lseek(cpu: "CPU") -> None:
        fd     = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        raw    = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        offset = raw if raw < 0x80000000 else (raw - 0x100000000)  # signed
        whence = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        entry  = state.file_handle_map.get(fd)
        if entry is None:
            cpu.regs[EAX] = 0xFFFFFFFF  # -1 = error
            return
        if whence == 0:       # SEEK_SET
            entry.position = offset
        elif whence == 1:     # SEEK_CUR
            entry.position = entry.position + offset
        else:                 # SEEK_END
            entry.position = len(entry.data) + offset
        entry.position = max(0, min(entry.position, len(entry.data)))
        cpu.regs[EAX] = entry.position & 0xFFFFFFFF

    stubs.register_handler("msvcrt.dll", "_lseek", _lseek)

    # ── getenv — no environment variables configured ──────────────────────────

    # getenv(const char* name) -> char* [cdecl]
    def _getenv(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0  # NULL — no environment variables

    stubs.register_handler("msvcrt.dll", "getenv", _getenv)
