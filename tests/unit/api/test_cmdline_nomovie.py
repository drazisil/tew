"""
Tests that MCity_d.exe's emulated command line includes "-nomovie" by
default (see crt_handlers.py/kernel32_system.py/msvcrt_handlers.py) --
skips the opening movie, whose MAD decoder is the source of the known
EIP=0x00a6bfcb fault documented in tew/kernel/seh.py.

Covers both mechanisms real CRT startup would derive from the same
command line: GetCommandLineA/W's raw string, and __getmainargs's argc/argv
split.
"""
from __future__ import annotations

from tew.api.crt_handlers import register_crt_handlers
from tew.api.win32_handlers import Win32Handlers
from tew.hardware.cpu_zig import ZigCPU as CPU, EAX, ESP
from tew.hardware.memory import Memory

MEM_SIZE = 16 * 1024 * 1024
STACK = 0x00030000
HEAP_START = 0x00600000  # default (0x04000000) is past MEM_SIZE here


def _read_cstr(mem: Memory, addr: int, max_len: int = 128) -> str:
    out = []
    for i in range(max_len):
        ch = mem.read8(addr + i)
        if ch == 0:
            break
        out.append(chr(ch))
    return "".join(out)


def _read_wstr(mem: Memory, addr: int, max_len: int = 128) -> str:
    out = []
    for i in range(max_len):
        lo = mem.read8(addr + i * 2)
        hi = mem.read8(addr + i * 2 + 1)
        cp = lo | (hi << 8)
        if cp == 0:
            break
        out.append(chr(cp))
    return "".join(out)


def _env():
    mem = Memory(MEM_SIZE)
    cpu = CPU(mem)
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    stubs = Win32Handlers(mem)
    state = register_crt_handlers(stubs, mem)
    state.next_heap_alloc = HEAP_START  # after registration, before any handler runs
    return mem, cpu, stubs, state


def test_get_command_line_a_includes_nomovie():
    mem, cpu, stubs, state = _env()
    handler = stubs._handlers["kernel32.dll!GetCommandLineA"].handler
    handler(cpu)
    result_addr = cpu.regs[EAX] & 0xFFFFFFFF
    assert _read_cstr(mem, result_addr) == "MCity_d.exe -nomovie -dbEnableLog"


def test_get_command_line_w_includes_nomovie():
    mem, cpu, stubs, state = _env()
    handler = stubs._handlers["kernel32.dll!GetCommandLineW"].handler
    handler(cpu)
    result_addr = cpu.regs[EAX] & 0xFFFFFFFF
    assert _read_wstr(mem, result_addr) == "MCity_d.exe -nomovie -dbEnableLog"


def test_getmainargs_reports_three_args():
    mem, cpu, stubs, state = _env()

    p_argc, p_argv, p_envp = 0x00040000, 0x00040004, 0x00040008
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4, p_argc)
    mem.write32(STACK + 8, p_argv)
    mem.write32(STACK + 12, p_envp)
    mem.write32(STACK + 16, 0)  # doWildCard
    mem.write32(STACK + 20, 0)  # _startupinfo*

    handler = stubs._handlers["msvcrt.dll!__getmainargs"].handler
    handler(cpu)

    argc = mem.read32(p_argc)
    argv_array = mem.read32(p_argv)
    assert argc == 3

    argv0_ptr = mem.read32(argv_array)
    argv1_ptr = mem.read32(argv_array + 4)
    argv2_ptr = mem.read32(argv_array + 8)
    argv3_ptr = mem.read32(argv_array + 12)

    assert _read_cstr(mem, argv0_ptr) == "MCity_d.exe"
    assert _read_cstr(mem, argv1_ptr) == "-nomovie"
    assert _read_cstr(mem, argv2_ptr) == "-dbEnableLog"
    assert argv3_ptr == 0
