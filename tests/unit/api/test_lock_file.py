"""
Tests for kernel32.dll!LockFile / UnlockFile -- added 2026-08-07 right after
fixing GetFileType (see test_kernel32_system_info.py), which unblocked real
Jet's Workspace::OpenDatabase and immediately surfaced this as the next
honest gap: real Jet locks its database file (byte-range locking) as a
completely normal part of opening it.

Locks are tracked per real host path (not handle) in CRTState.file_locks,
matching real Win32 byte-range lock visibility across every handle open on
the same file -- this emulator only has "other guest threads" to worry
about (single host process), but real Jet genuinely opens the same database
from more than one handle.
"""
from __future__ import annotations

from tew.api._state import CRTState, EmulatorConfig, TEB_BASE
from tew.api.crt_handlers import register_crt_handlers
from tew.api.win32_handlers import Win32Handlers
from tew.api.win32_errors import Win32Error
from tew.hardware.cpu_zig import ZigCPU as CPU, EAX, ESP
from tew.hardware.memory import Memory

MEM_SIZE = 4 * 1024 * 1024
STACK = 0x00030000

GENERIC_READ  = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_ALWAYS   = 4
INVALID_HANDLE = 0xFFFFFFFF


def _write_cstr(mem: Memory, addr: int, s: str) -> None:
    for i, ch in enumerate(s.encode("latin-1")):
        mem.write8(addr + i, ch)
    mem.write8(addr + len(s), 0)


def _last_error(mem: Memory) -> int:
    return mem.read32(TEB_BASE + 0x34)


def _env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = EmulatorConfig(
        path_mappings={"c:/": str(tmp_path) + "/"},
        interactive_on_missing_file=False,
    )
    mem = Memory(MEM_SIZE)
    stubs = Win32Handlers(mem)
    state = register_crt_handlers(stubs, mem, config=config)
    cpu = CPU(mem)
    return mem, state, stubs, cpu


def _open_file(mem, stubs, cpu, path: str) -> int:
    name_addr = 0x00200000
    _write_cstr(mem, name_addr, path)
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4, name_addr)
    mem.write32(STACK + 8, GENERIC_READ | GENERIC_WRITE)
    mem.write32(STACK + 12, 0)
    mem.write32(STACK + 16, 0)
    mem.write32(STACK + 20, OPEN_ALWAYS)
    mem.write32(STACK + 24, 0)
    mem.write32(STACK + 28, 0)
    stubs._handlers["kernel32.dll!CreateFileA"].handler(cpu)
    return cpu.regs[EAX]


def _lock(mem, stubs, cpu, handle: int, offset: int, length: int) -> int:
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4, handle)
    mem.write32(STACK + 8, offset)
    mem.write32(STACK + 12, 0)
    mem.write32(STACK + 16, length)
    mem.write32(STACK + 20, 0)
    stubs._handlers["kernel32.dll!LockFile"].handler(cpu)
    return cpu.regs[EAX]


def _unlock(mem, stubs, cpu, handle: int, offset: int, length: int) -> int:
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4, handle)
    mem.write32(STACK + 8, offset)
    mem.write32(STACK + 12, 0)
    mem.write32(STACK + 16, length)
    mem.write32(STACK + 20, 0)
    stubs._handlers["kernel32.dll!UnlockFile"].handler(cpu)
    return cpu.regs[EAX]


class TestLockFile:
    def test_lock_succeeds_on_fresh_range(self, tmp_path, monkeypatch):
        mem, state, stubs, cpu = _env(tmp_path, monkeypatch)
        handle = _open_file(mem, stubs, cpu, "C:\\Tmp.MDB")
        assert handle != INVALID_HANDLE

        result = _lock(mem, stubs, cpu, handle, 0, 4096)
        assert result == 1

    def test_overlapping_lock_from_different_handle_fails(self, tmp_path, monkeypatch):
        mem, state, stubs, cpu = _env(tmp_path, monkeypatch)
        h1 = _open_file(mem, stubs, cpu, "C:\\Tmp.MDB")
        h2 = _open_file(mem, stubs, cpu, "C:\\Tmp.MDB")
        assert h1 != h2

        assert _lock(mem, stubs, cpu, h1, 0, 100) == 1
        result = _lock(mem, stubs, cpu, h2, 50, 100)  # overlaps [0,100)
        assert result == 0
        assert _last_error(mem) == Win32Error.ERROR_LOCK_VIOLATION

    def test_non_overlapping_lock_from_different_handle_succeeds(self, tmp_path, monkeypatch):
        mem, state, stubs, cpu = _env(tmp_path, monkeypatch)
        h1 = _open_file(mem, stubs, cpu, "C:\\Tmp.MDB")
        h2 = _open_file(mem, stubs, cpu, "C:\\Tmp.MDB")

        assert _lock(mem, stubs, cpu, h1, 0, 100) == 1
        result = _lock(mem, stubs, cpu, h2, 100, 100)  # [100,200), adjacent not overlapping
        assert result == 1

    def test_unlock_then_relock_from_other_handle_succeeds(self, tmp_path, monkeypatch):
        mem, state, stubs, cpu = _env(tmp_path, monkeypatch)
        h1 = _open_file(mem, stubs, cpu, "C:\\Tmp.MDB")
        h2 = _open_file(mem, stubs, cpu, "C:\\Tmp.MDB")

        assert _lock(mem, stubs, cpu, h1, 0, 100) == 1
        assert _unlock(mem, stubs, cpu, h1, 0, 100) == 1
        assert _lock(mem, stubs, cpu, h2, 0, 100) == 1

    def test_unlock_unlocked_range_fails(self, tmp_path, monkeypatch):
        mem, state, stubs, cpu = _env(tmp_path, monkeypatch)
        handle = _open_file(mem, stubs, cpu, "C:\\Tmp.MDB")

        result = _unlock(mem, stubs, cpu, handle, 0, 100)
        assert result == 0
        assert _last_error(mem) == Win32Error.ERROR_NOT_LOCKED

    def test_closehandle_releases_locks(self, tmp_path, monkeypatch):
        mem, state, stubs, cpu = _env(tmp_path, monkeypatch)
        h1 = _open_file(mem, stubs, cpu, "C:\\Tmp.MDB")
        h2 = _open_file(mem, stubs, cpu, "C:\\Tmp.MDB")

        assert _lock(mem, stubs, cpu, h1, 0, 100) == 1

        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0xDEAD)
        mem.write32(STACK + 4, h1)
        stubs._handlers["kernel32.dll!CloseHandle"].handler(cpu)

        # h1's lock should be gone now, so h2 can lock the same range.
        assert _lock(mem, stubs, cpu, h2, 0, 100) == 1

    def test_lock_on_unknown_handle_fails(self, tmp_path, monkeypatch):
        mem, state, stubs, cpu = _env(tmp_path, monkeypatch)
        result = _lock(mem, stubs, cpu, 0xDEADBEEF, 0, 100)
        assert result == 0
        assert _last_error(mem) == Win32Error.ERROR_INVALID_HANDLE
