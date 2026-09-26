"""cleanup_stdcall: the libcpu fast path (real ZigCPU) must match the Python path."""
from __future__ import annotations

import pytest

from tew.api.win32_handlers import cleanup_stdcall
from tew.hardware.cpu_zig import ZigCPU, ESP
from tew.hardware.memory_zig import ZigMemory

MEM_SIZE = 0x10000


class _FakeCPU:
    def __init__(self, memory) -> None:
        self.memory = memory
        self.regs = [0] * 8


@pytest.mark.parametrize("cpu_cls", [ZigCPU, _FakeCPU])
def test_moves_return_address_over_args(cpu_cls):
    mem = ZigMemory(MEM_SIZE)
    cpu = cpu_cls(mem)
    cpu.regs[ESP] = 0x8000
    mem.write32(0x8000, 0x00401234)
    cleanup_stdcall(cpu, mem, 12)
    assert cpu.regs[ESP] == 0x800C
    assert mem.read32(0x800C) == 0x00401234


def test_zig_path_raises_when_new_slot_out_of_bounds():
    mem = ZigMemory(MEM_SIZE)
    cpu = ZigCPU(mem)
    cpu.regs[ESP] = MEM_SIZE - 8
    with pytest.raises(RuntimeError, match="out of bounds"):
        cleanup_stdcall(cpu, mem, 8)
    assert cpu.regs[ESP] == MEM_SIZE - 8
