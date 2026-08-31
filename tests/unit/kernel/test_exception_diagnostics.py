"""Tests for tew.kernel.exception_diagnostics -- static address classification
and the structured crash-log file (/tmp/emu_crash.json in production,
monkeypatched to a tmp_path here so tests never touch the real file).
"""
from __future__ import annotations

import json

import pytest

from tew.api._state import CRTState
from tew.api.win32_handlers import Win32Handlers
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import ZigCPU, EAX, ESP, EBP, FatalHaltError
from tew.kernel import exception_diagnostics as ed

MEM_SIZE = 0x00400000
DUMP_MEM_SIZE = 0x06000000  # covers _CRT_DUMP_MEMORY_LEAKS_ADDR (~0x009F81B0) and
                             # the heap base (0x04000000) _get_dialog_sentinel allocates from


class _FakeImportResolver:
    """Minimal stand-in for ImportResolver's interface (find_dll_for_address
    + get_address_mappings) -- exception_diagnostics.py doesn't need a real
    DLLLoader, just something matching this shape."""

    def __init__(self, dlls: list[dict]):
        self._dlls = dlls  # each: {name, base_address, size}

    def find_dll_for_address(self, addr: int):
        for dll in self._dlls:
            if dll["base_address"] <= addr < dll["base_address"] + dll["size"]:
                return dll
        return None

    def get_address_mappings(self):
        return [
            {
                "dll_name": d["name"],
                "base_address": d["base_address"],
                "end_address": d["base_address"] + d["size"] - 1,
            }
            for d in self._dlls
        ]


@pytest.fixture
def cpu():
    mem = Memory(size_bytes=MEM_SIZE)
    return ZigCPU(mem)


class TestClassifyStaticRegion:
    def test_exe_range(self):
        assert ed._classify_static_region(0x00500000) == "exe"

    def test_stub_range(self):
        assert ed._classify_static_region(0x00210000) == "stub"

    def test_heap_range(self):
        assert ed._classify_static_region(0x05000000) == "heap"

    def test_thread_stack_range(self):
        assert ed._classify_static_region(0x08100000) == "thread stack"

    def test_main_stack_no_upper_bound(self):
        assert ed._classify_static_region(0x7FFFFFFF) == "main stack"
        assert ed._classify_static_region(0xFFFFFFFF) == "main stack"

    def test_virtualalloc_no_upper_bound(self):
        assert ed._classify_static_region(0x40000000) == "VirtualAlloc"

    def test_unclassified_gap_returns_none(self):
        # Between exe's end (0x02200000) and heap's start (0x04000000).
        assert ed._classify_static_region(0x03000000) is None

    def test_exe_checked_before_overlap_candidates(self):
        # 0x00400000-0x02200000 (exe) fully contains no other range, but
        # confirms priority order doesn't misfire at its own boundaries.
        assert ed._classify_static_region(0x00400000) == "exe"
        assert ed._classify_static_region(0x021FFFFF) == "exe"
        assert ed._classify_static_region(0x02200000) is None


class TestAnnotateAddress:
    def test_dll_hit_takes_priority(self):
        resolver = _FakeImportResolver([{"name": "expsrv.dll", "base_address": 0x19000000, "size": 0x1000000}])
        result = ed._annotate_address(0x1901d9eb, resolver)
        assert result == "  ← expsrv.dll+0x1d9eb"

    def test_falls_back_to_static_region_on_dll_miss(self):
        resolver = _FakeImportResolver([{"name": "expsrv.dll", "base_address": 0x19000000, "size": 0x1000000}])
        result = ed._annotate_address(0x08100000, resolver)
        assert result == "  ← thread stack"

    def test_no_resolver_still_classifies_static_region(self):
        result = ed._annotate_address(0x7FFF0000, None)
        assert result == "  ← main stack"

    def test_unclassifiable_address_returns_empty_string(self):
        result = ed._annotate_address(0x03000000, None)
        assert result == ""


class TestWriteCrashLog:
    def test_diagnose_halt_writes_valid_json(self, cpu, tmp_path, monkeypatch):
        crash_path = tmp_path / "crash.json"
        monkeypatch.setattr(ed, "CRASH_LOG_PATH", crash_path)

        cpu.eip = 0x1901d9eb
        cpu.regs[ESP] = 0x08100000
        cpu.regs[EBP] = 0x08100010
        resolver = _FakeImportResolver([{"name": "expsrv.dll", "base_address": 0x19000000, "size": 0x1000000}])

        ed.diagnose_halt(cpu, resolver)

        assert crash_path.exists()
        data = json.loads(crash_path.read_text())
        assert data["kind"] == "halt"
        assert data["eip"] == 0x1901d9eb
        assert data["eip_annotation"] == "expsrv.dll+0x1d9eb"
        assert "registers" in data and len(data["registers"]) == 8
        assert "stack_dump" in data
        assert "ebp_chain" in data
        assert data["dll_table"] == [{"name": "expsrv.dll", "base": 0x19000000, "end": 0x19ffffff}]
        assert len(data["static_memory_map"]) > 0

    def test_diagnose_fault_writes_memory_access_section(self, cpu, tmp_path, monkeypatch):
        crash_path = tmp_path / "crash.json"
        monkeypatch.setattr(ed, "CRASH_LOG_PATH", crash_path)

        cpu.eip = 0x00500000
        cpu.regs[ESP] = 0x08100000
        cpu.regs[EBP] = 0x08100010
        cpu.last_error = RuntimeError("bad memory access at 0x0000000d")
        resolver = _FakeImportResolver([])  # no DLLs loaded -- in_dll must resolve to None, not be omitted

        ed.diagnose_fault(cpu, resolver)

        data = json.loads(crash_path.read_text())
        assert data["kind"] == "fault"
        assert data["memory_access"]["attempted_address"] == 0x0000000d
        assert data["memory_access"]["in_dll"] is None
        assert data["memory_access"]["looks_like_unresolved_import"] is True

    def test_overwrites_previous_crash_file(self, cpu, tmp_path, monkeypatch):
        crash_path = tmp_path / "crash.json"
        monkeypatch.setattr(ed, "CRASH_LOG_PATH", crash_path)

        cpu.eip = 0x00500000
        ed.diagnose_halt(cpu, None)
        first = json.loads(crash_path.read_text())

        cpu.eip = 0x00600000
        ed.diagnose_halt(cpu, None)
        second = json.loads(crash_path.read_text())

        assert first["eip"] != second["eip"]
        assert second["eip"] == 0x00600000


class TestDumpCrtMemoryLeaksFatalHalt:
    def test_propagates_fatal_halt_from_nested_call(self):
        # Regression test for _dump_crt_memory_leaks's bare `except Exception`
        # around its nested _invoke_emulated_proc call swallowing FatalHaltError
        # instead of letting it propagate -- see
        # tests/unit/api/test_invoke_emulated_proc_fatal_halt.py for the same
        # invariant one layer down, at _invoke_emulated_proc itself.
        mem = Memory(size_bytes=DUMP_MEM_SIZE)
        state = CRTState()  # constructs state.scheduler with a main thread already

        cpu = ZigCPU(mem)
        cpu.regs[ESP] = 0x00200000

        stubs = Win32Handlers(mem)

        def _fake_unimplemented(c: "ZigCPU") -> None:
            c.halted = True
            c.fatal_halt = True

        stubs.register_handler("test", "FakeUnimplemented", _fake_unimplemented)
        stubs.install(cpu)

        halt_addr = stubs.get_handler_address("test", "FakeUnimplemented")
        assert halt_addr is not None

        # mov edx, halt_addr ; call edx -- guest's _CrtDumpMemoryLeaks jumps
        # straight into the fatally-halting handler instead of doing real work.
        proc = bytes([0xBA]) + halt_addr.to_bytes(4, "little") + bytes([0xFF, 0xD2])
        for i, b in enumerate(proc):
            mem.write8(ed._CRT_DUMP_MEMORY_LEAKS_ADDR + i, b)

        with pytest.raises(FatalHaltError):
            ed._dump_crt_memory_leaks(cpu, mem, state)

        assert cpu.fatal_halt is True
