"""
Regression tests for the 2026-08-07 read+write file handle fix.

Root cause: CreateFile/fopen callers collapsed dwDesiredAccess (or the fopen
mode string) into a single `writable` boolean, so open_file_handle always
opened the real fd with os.O_WRONLY and kernel32_io.py's ReadFile /
msvcrt_handlers.py's fread unconditionally rejected any handle flagged
writable -- regardless of whether the guest also requested GENERIC_READ (or
an fopen "+" mode). Confirmed live as the real cause of msjet35.dll reporting
"unrecognized database format" on a byte-perfect, correctly-signed Tmp.MDB:
DAO/Jet opens it GENERIC_READ|GENERIC_WRITE (needs to read its own header
back after writing), and got a handle that could only ever write.

FileHandleEntry now has a separate `readable` field; open_file_handle takes
an `also_readable` parameter that opens the real fd with os.O_RDWR instead
of os.O_WRONLY and threads through to `readable`; ReadFile/fread/the
low-level _read now do a real os.pread() for handles that are both writable
and readable, while still correctly rejecting write-only handles exactly as
before.
"""
from __future__ import annotations

from tew.api._state import CRTState, EmulatorConfig
from tew.api.crt_handlers import register_crt_handlers
from tew.api.win32_handlers import Win32Handlers
from tew.hardware.cpu_zig import ZigCPU as CPU, EAX, ESP
from tew.hardware.memory import Memory

INVALID_HANDLE = 0xFFFFFFFF
MEM_SIZE = 4 * 1024 * 1024
STACK = 0x00030000

GENERIC_READ  = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_ALWAYS   = 4


def _state(tmp_path, monkeypatch) -> CRTState:
    monkeypatch.chdir(tmp_path)
    config = EmulatorConfig(
        path_mappings={"c:/": str(tmp_path) + "/"},
        interactive_on_missing_file=False,
    )
    return CRTState(config=config)


# ── CRTState.open_file_handle / FileHandleEntry ─────────────────────────────

class TestOpenFileHandleAlsoReadable:
    def test_defaults_to_not_readable(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)
        mem = Memory(MEM_SIZE)
        handle = state.open_file_handle("C:\\foo.txt", writable=True, memory=mem)
        entry = state.file_handle_map[handle]
        assert entry.writable
        assert entry.readable is False

    def test_also_readable_marks_entry_readable(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)
        mem = Memory(MEM_SIZE)
        handle = state.open_file_handle(
            "C:\\foo.txt", writable=True, memory=mem, also_readable=True)
        entry = state.file_handle_map[handle]
        assert entry.writable
        assert entry.readable

    def test_also_readable_handle_can_read_back_what_it_wrote(self, tmp_path, monkeypatch):
        """The actual bug: os.O_WRONLY vs os.O_RDWR on the real fd."""
        state = _state(tmp_path, monkeypatch)
        mem = Memory(MEM_SIZE)
        handle = state.open_file_handle(
            "C:\\foo.txt", writable=True, memory=mem, also_readable=True)
        entry = state.file_handle_map[handle]

        import os
        os.write(entry.fd, b"hello world")
        data = os.pread(entry.fd, 11, 0)
        assert data == b"hello world"


# ── kernel32.dll!CreateFileA / ReadFile / WriteFile round trip ──────────────

def _write_cstr(mem: Memory, addr: int, s: str) -> None:
    for i, ch in enumerate(s.encode("latin-1")):
        mem.write8(addr + i, ch)
    mem.write8(addr + len(s), 0)


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


class TestReadFileWriteFileRoundTrip:
    def test_generic_read_write_handle_can_read_after_write(self, tmp_path, monkeypatch):
        mem, state, stubs, cpu = _env(tmp_path, monkeypatch)
        name_addr = 0x00200000
        _write_cstr(mem, name_addr, "C:\\Tmp.MDB")

        # CreateFileA(lpFileName, dwDesiredAccess, dwShareMode, lpSecurityAttrs,
        #             dwCreationDisposition, dwFlagsAndAttributes, hTemplateFile)
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
        handle = cpu.regs[EAX]
        assert handle != INVALID_HANDLE
        entry = state.file_handle_map[handle]
        assert entry.writable and entry.readable

        # WriteFile(hFile, lpBuffer, nBytesToWrite, lpBytesWritten, lpOverlapped)
        payload = b"Standard Jet DB"
        buf_addr = 0x00201000
        for i, b in enumerate(payload):
            mem.write8(buf_addr + i, b)
        written_addr = 0x00202000
        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0xDEAD)
        mem.write32(STACK + 4, handle)
        mem.write32(STACK + 8, buf_addr)
        mem.write32(STACK + 12, len(payload))
        mem.write32(STACK + 16, written_addr)
        mem.write32(STACK + 20, 0)
        stubs._handlers["kernel32.dll!WriteFile"].handler(cpu)
        assert cpu.regs[EAX] == 1
        assert mem.read32(written_addr) == len(payload)

        # SetFilePointer back to the start before reading back.
        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0xDEAD)
        mem.write32(STACK + 4, handle)
        mem.write32(STACK + 8, 0)
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, 0)  # FILE_BEGIN
        stubs._handlers["kernel32.dll!SetFilePointer"].handler(cpu)

        # ReadFile(hFile, lpBuffer, nBytesToRead, lpBytesRead, lpOverlapped) --
        # this is the exact call that was previously an unconditional FALSE.
        read_buf_addr = 0x00203000
        read_addr = 0x00204000
        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0xDEAD)
        mem.write32(STACK + 4, handle)
        mem.write32(STACK + 8, read_buf_addr)
        mem.write32(STACK + 12, len(payload))
        mem.write32(STACK + 16, read_addr)
        mem.write32(STACK + 20, 0)
        stubs._handlers["kernel32.dll!ReadFile"].handler(cpu)

        assert cpu.regs[EAX] == 1, "ReadFile must succeed on a GENERIC_READ|GENERIC_WRITE handle"
        assert mem.read32(read_addr) == len(payload)
        got = bytes(mem.read8(read_buf_addr + i) for i in range(len(payload)))
        assert got == payload

    def test_write_only_handle_still_rejects_readfile(self, tmp_path, monkeypatch):
        """Regression guard: a genuinely write-only handle (no GENERIC_READ)
        must still fail ReadFile, exactly like real Windows."""
        mem, state, stubs, cpu = _env(tmp_path, monkeypatch)
        name_addr = 0x00200000
        _write_cstr(mem, name_addr, "C:\\WriteOnly.txt")

        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0xDEAD)
        mem.write32(STACK + 4, name_addr)
        mem.write32(STACK + 8, GENERIC_WRITE)  # no GENERIC_READ
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, 0)
        mem.write32(STACK + 20, OPEN_ALWAYS)
        mem.write32(STACK + 24, 0)
        mem.write32(STACK + 28, 0)
        stubs._handlers["kernel32.dll!CreateFileA"].handler(cpu)
        handle = cpu.regs[EAX]
        assert handle != INVALID_HANDLE
        entry = state.file_handle_map[handle]
        assert entry.writable and not entry.readable

        read_buf_addr = 0x00203000
        read_addr = 0x00204000
        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0xDEAD)
        mem.write32(STACK + 4, handle)
        mem.write32(STACK + 8, read_buf_addr)
        mem.write32(STACK + 12, 4)
        mem.write32(STACK + 16, read_addr)
        mem.write32(STACK + 20, 0)
        stubs._handlers["kernel32.dll!ReadFile"].handler(cpu)

        assert cpu.regs[EAX] == 0
        assert mem.read32(read_addr) == 0
