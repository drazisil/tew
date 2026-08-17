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


# ── OVERLAPPED positioned read/write (2026-08-15 fix) ───────────────────────
#
# Root cause: ReadFile/WriteFile's real 5th parameter, lpOverlapped, was never
# read anywhere in kernel32_io.py -- every read/write used the handle's own
# sequential entry.position regardless of what the guest actually requested.
# Found while tracing why msjet35.dll's DAO/Jet B-tree code lands on a
# specific database page: DAO/Jet issues real positioned reads via
# ReadFile(..., &overlapped) specifically to avoid disturbing the shared file
# pointer across threads/pages -- tew could not honor that at all, so which
# page got served depended only on how many prior sequential reads had
# happened, not on the offset DAO/Jet actually asked for.

def _write_overlapped(mem: Memory, addr: int, offset: int) -> None:
    """Lay out a minimal real OVERLAPPED struct: Internal/InternalHigh zeroed,
    Offset/OffsetHigh at +8/+0xC (a 64-bit position split low/high), hEvent
    zeroed at +0x10 -- the same layout FUN_7a842abc (msjet35.dll) uses."""
    mem.write32(addr, 0)
    mem.write32(addr + 4, 0)
    mem.write32(addr + 8, offset & 0xFFFFFFFF)
    mem.write32(addr + 0xC, (offset >> 32) & 0xFFFFFFFF)
    mem.write32(addr + 0x10, 0)


class TestOverlappedReadWrite:
    def test_overlapped_read_reads_at_offset_not_sequential_position(self, tmp_path, monkeypatch):
        mem, state, stubs, cpu = _env(tmp_path, monkeypatch)
        name_addr = 0x00200000
        _write_cstr(mem, name_addr, "C:\\Tmp.MDB")
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
        entry = state.file_handle_map[handle]

        # Sequentially write "AAAABBBBCCCCDDDD" (16 bytes), leaving
        # entry.position at 16.
        payload = b"AAAABBBBCCCCDDDD"
        buf_addr = 0x00201000
        for i, b in enumerate(payload):
            mem.write8(buf_addr + i, b)
        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0xDEAD)
        mem.write32(STACK + 4, handle)
        mem.write32(STACK + 8, buf_addr)
        mem.write32(STACK + 12, len(payload))
        mem.write32(STACK + 16, 0)
        mem.write32(STACK + 20, 0)
        stubs._handlers["kernel32.dll!WriteFile"].handler(cpu)
        assert entry.position == 16

        # Positioned ReadFile of 4 bytes at offset 8 ("CCCC") via a real
        # OVERLAPPED struct -- must return the byte-8 data, not whatever
        # sequential position 16 would give (nothing/EOF).
        overlapped_addr = 0x00205000
        _write_overlapped(mem, overlapped_addr, 8)
        read_buf_addr = 0x00203000
        read_addr = 0x00204000
        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0xDEAD)
        mem.write32(STACK + 4, handle)
        mem.write32(STACK + 8, read_buf_addr)
        mem.write32(STACK + 12, 4)
        mem.write32(STACK + 16, read_addr)
        mem.write32(STACK + 20, overlapped_addr)
        stubs._handlers["kernel32.dll!ReadFile"].handler(cpu)

        assert cpu.regs[EAX] == 1
        assert mem.read32(read_addr) == 4
        got = bytes(mem.read8(read_buf_addr + i) for i in range(4))
        assert got == b"CCCC"
        # The handle's own sequential cursor must be untouched by the
        # positioned read.
        assert entry.position == 16

    def test_overlapped_write_writes_at_offset_without_disturbing_position(self, tmp_path, monkeypatch):
        mem, state, stubs, cpu = _env(tmp_path, monkeypatch)
        name_addr = 0x00200000
        _write_cstr(mem, name_addr, "C:\\Tmp.MDB")
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
        entry = state.file_handle_map[handle]

        payload = b"AAAABBBBCCCCDDDD"
        buf_addr = 0x00201000
        for i, b in enumerate(payload):
            mem.write8(buf_addr + i, b)
        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0xDEAD)
        mem.write32(STACK + 4, handle)
        mem.write32(STACK + 8, buf_addr)
        mem.write32(STACK + 12, len(payload))
        mem.write32(STACK + 16, 0)
        mem.write32(STACK + 20, 0)
        stubs._handlers["kernel32.dll!WriteFile"].handler(cpu)
        assert entry.position == 16

        # Positioned WriteFile of "ZZZZ" at offset 4, overwriting "BBBB".
        overlapped_addr = 0x00205000
        _write_overlapped(mem, overlapped_addr, 4)
        write_buf_addr = 0x00206000
        for i, b in enumerate(b"ZZZZ"):
            mem.write8(write_buf_addr + i, b)
        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0xDEAD)
        mem.write32(STACK + 4, handle)
        mem.write32(STACK + 8, write_buf_addr)
        mem.write32(STACK + 12, 4)
        mem.write32(STACK + 16, 0)
        mem.write32(STACK + 20, overlapped_addr)
        stubs._handlers["kernel32.dll!WriteFile"].handler(cpu)
        assert cpu.regs[EAX] == 1
        # Sequential cursor untouched by the positioned write.
        assert entry.position == 16

        # Read the whole file back sequentially from the start to verify the
        # overlapped write landed at the right offset and nowhere else.
        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0xDEAD)
        mem.write32(STACK + 4, handle)
        mem.write32(STACK + 8, 0)
        mem.write32(STACK + 12, 0)
        mem.write32(STACK + 16, 0)  # FILE_BEGIN
        stubs._handlers["kernel32.dll!SetFilePointer"].handler(cpu)

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
        got = bytes(mem.read8(read_buf_addr + i) for i in range(len(payload)))
        assert got == b"AAAAZZZZCCCCDDDD"
