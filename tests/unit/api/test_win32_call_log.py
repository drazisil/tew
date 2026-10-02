"""Tests for Win32Handlers' recent-call log (dedupe counter, eviction, formatting)."""
from __future__ import annotations

from tew.api.win32_handlers import HANDLER_BASE, HANDLER_SIZE, Win32Handlers
from tew.hardware.memory import Memory

MEM_SIZE = 16 * 1024 * 1024


class _FakeCPU:
    def __init__(self) -> None:
        self.eip = 0


def _setup():
    stubs = Win32Handlers(Memory(MEM_SIZE))
    stubs.register_handler("kernel32.dll", "GetVersion", lambda cpu: None)
    stubs.register_handler("kernel32.dll", "GetTickCount", lambda cpu: None)
    return stubs, _FakeCPU()


def _call(stubs, cpu, index: int) -> None:
    # EIP sits past the 2-byte INT 0xFE, as it does on real dispatch.
    cpu.eip = HANDLER_BASE + index * HANDLER_SIZE + 2
    stubs._handle_api_int(cpu)


def test_single_call_has_no_repeat_suffix():
    stubs, cpu = _setup()
    _call(stubs, cpu, 0)
    assert stubs.get_call_log() == [f"kernel32.dll!GetVersion @ 0x{HANDLER_BASE:x}"]


def test_consecutive_repeats_collapse_with_counter():
    stubs, cpu = _setup()
    for _ in range(3):
        _call(stubs, cpu, 0)
    _call(stubs, cpu, 1)
    _call(stubs, cpu, 0)
    a = f"kernel32.dll!GetVersion @ 0x{HANDLER_BASE:x}"
    b = f"kernel32.dll!GetTickCount @ 0x{HANDLER_BASE + HANDLER_SIZE:x}"
    assert stubs.get_call_log() == [f"{a} x3", b, a]


def test_log_evicts_oldest_beyond_size():
    stubs, cpu = _setup()
    for i in range(stubs._call_log_size + 5):
        _call(stubs, cpu, i % 2)
    log = stubs.get_call_log()
    assert len(log) == stubs._call_log_size
    # 2005 alternating calls: the first 5 were evicted, so the oldest kept
    # entry is call #5 (odd index -> GetTickCount).
    assert log[0].startswith("kernel32.dll!GetTickCount")
