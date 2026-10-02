"""Tests for Win32Handlers.register_guest_code / patch_address_to_guest_code:
exports implemented as guest x86 code that trap to Python only at hooks."""
from __future__ import annotations

import pytest

from tew.api.win32_handlers import HANDLER_SIZE, Win32Handlers
from tew.hardware.cpu_zig import EAX, ESP, ZigCPU
from tew.hardware.memory import Memory

MEM_SIZE = 0x00400000
STACK = 0x00100000
CALLER = 0x00050000

# f(x): if x == 0 { INT 0xFE (hook sets EAX=7) } else { EAX = x }; ret 4
#   +0  mov eax, [esp+4]      8b 44 24 04
#   +4  test eax, eax         85 c0
#   +6  jnz +2                75 02
#   +8  int 0xFE              cd fe
#   +a  ret 4                 c2 04 00
CODE = bytes.fromhex("8b442404" "85c0" "7502" "cdfe" "c20400")
HOOK = 0x08


def _env():
    mem = Memory(MEM_SIZE)
    cpu = ZigCPU(mem)
    stubs = Win32Handlers(mem)
    calls = []

    def hook(c):
        calls.append(c.eip)
        c.regs[EAX] = 7

    stubs.register_guest_code("test.dll", CODE, {"F": 0}, {HOOK: ("F:zero", hook)})
    stubs.install(cpu)
    return cpu, mem, stubs, calls


def _call(cpu, mem, target, arg):
    rel = (target - (CALLER + 10)) & 0xFFFFFFFF
    mem.load(CALLER, b"\x68" + arg.to_bytes(4, "little") + b"\xE8" + rel.to_bytes(4, "little") + b"\xF4")
    cpu.regs[ESP] = STACK
    cpu.eip = CALLER
    cpu.halted = False
    cpu.run(1000)
    assert cpu.halted and cpu.regs[ESP] == STACK
    return cpu.regs[EAX]


def test_fast_path_runs_as_guest_code_without_the_hook():
    cpu, mem, stubs, calls = _env()
    assert _call(cpu, mem, stubs.lookup_handler_address("test.dll", "F"), 5) == 5
    assert calls == []


def test_hook_runs_and_execution_continues_after_it():
    cpu, mem, stubs, calls = _env()
    start = stubs.lookup_handler_address("test.dll", "F")
    assert _call(cpu, mem, start, 0) == 7
    assert calls == [start + HOOK + 2]  # handler sees EIP past the INT 0xFE


def test_code_is_padded_with_int3_to_whole_slots():
    cpu, mem, stubs, calls = _env()
    start = stubs.lookup_handler_address("test.dll", "F")
    assert mem.read_bytes(start, len(CODE)) == CODE
    assert mem.read_bytes(start + len(CODE), HANDLER_SIZE - len(CODE)) == b"\xCC" * (HANDLER_SIZE - len(CODE))


def test_hook_offset_must_be_an_int_fe():
    stubs = Win32Handlers(Memory(MEM_SIZE))
    with pytest.raises(ValueError, match="hook offset"):
        stubs.register_guest_code("test.dll", CODE, {"F": 0}, {0: ("bad", lambda c: None)})


def test_duplicate_export_refused():
    stubs = Win32Handlers(Memory(MEM_SIZE))
    stubs.register_handler("test.dll", "F", lambda c: None)
    with pytest.raises(ValueError, match="already registered"):
        stubs.register_guest_code("test.dll", CODE, {"F": 0}, {})


def test_patch_address_to_guest_code_writes_a_jmp():
    cpu, mem, stubs, calls = _env()
    start = stubs.lookup_handler_address("test.dll", "F")
    export = 0x00060000  # a real DLL's export of the same function
    stubs.patch_address_to_guest_code(export, "real.dll!F", start)
    assert mem.read8(export) == 0xE9
    assert _call(cpu, mem, export, 9) == 9
