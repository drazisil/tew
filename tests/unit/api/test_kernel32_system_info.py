"""Tests for kernel32.dll system-info handlers (version, time, process/thread
identity, environment strings, std handles, pointer encode/decode).

Sleep (the Scheduler-driven handler in the same source file) is tested
separately in test_kernel32_sleep.py.
"""
from __future__ import annotations

import time

import pytest

from tew.api._state import CRTState, TEB_BASE
from tew.api.kernel32_system import register_kernel32_system_handlers
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP


class _StubHandlers:
    def __init__(self):
        self._h: dict = {}

    def register_handler(self, dll, name, fn):
        self._h[(dll, name)] = fn

    def get(self, dll, name):
        return self._h[(dll, name)]


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False


MEM_SIZE = 4 * 1024 * 1024
STACK    = 0x200000
BUF_A    = 0x300000
BUF_B    = 0x310000


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_kernel32_system_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    return cpu, mem, state, stubs


def call(stubs, cpu, mem, name, args):
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    for i, val in enumerate(args):
        mem.write32(STACK + 4 + i * 4, val)
    stubs.get("kernel32.dll", name)(cpu)


def read_cstring(mem, addr) -> str:
    out = bytearray()
    while True:
        b = mem.read8(addr + len(out))
        if b == 0:
            break
        out.append(b)
    return out.decode("ascii")


def read_wstring(mem, addr) -> str:
    out = []
    i = 0
    while True:
        ch = mem.read16(addr + i * 2)
        if ch == 0:
            break
        out.append(chr(ch))
        i += 1
    return "".join(out)


# ── Version ────────────────────────────────────────────────────────────────────

class TestGetVersion:

    def test_returns_winxp_5_1_2600(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetVersion", [])
        assert cpu.regs[EAX] == (2600 << 16) | (1 << 8) | 5


class TestGetVersionExA:

    def test_fields_written(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetVersionExA", [BUF_A])
        assert mem.read32(BUF_A + 4) == 5
        assert mem.read32(BUF_A + 8) == 1
        assert mem.read32(BUF_A + 12) == 2600
        assert mem.read32(BUF_A + 16) == 2

    def test_service_pack_string_is_ansi(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetVersionExA", [BUF_A])
        assert read_cstring(mem, BUF_A + 20) == "Service Pack 2"

    def test_returns_true_and_cleans_up(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetVersionExA", [BUF_A])
        assert cpu.regs[EAX] == 1
        assert cpu.regs[ESP] == STACK + 4


class TestGetVersionExW:

    def test_fields_written(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetVersionExW", [BUF_A])
        assert mem.read32(BUF_A + 4) == 5
        assert mem.read32(BUF_A + 8) == 1
        assert mem.read32(BUF_A + 12) == 2600
        assert mem.read32(BUF_A + 16) == 2

    def test_service_pack_string_is_utf16le(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetVersionExW", [BUF_A])
        assert read_wstring(mem, BUF_A + 20) == "Service Pack 2"


# ── Command line / startup ─────────────────────────────────────────────────────

class TestCommandLineAndStartup:

    def test_get_command_line_a(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetCommandLineA", [])
        assert cpu.regs[EAX] == 0x00210024

    def test_get_command_line_w(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetCommandLineW", [])
        assert cpu.regs[EAX] == 0x00210070

    def test_get_startup_info_a_size_field(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetStartupInfoA", [BUF_A])
        assert mem.read32(BUF_A) == 68

    def test_get_startup_info_a_zeroes_rest(self, env):
        cpu, mem, state, stubs = env
        for i in range(4, 68, 4):
            mem.write32(BUF_A + i, 0xFFFFFFFF)
        call(stubs, cpu, mem, "GetStartupInfoA", [BUF_A])
        assert all(mem.read32(BUF_A + i) == 0 for i in range(4, 68, 4))

    def test_get_startup_info_w_size_field(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetStartupInfoW", [BUF_A])
        assert mem.read32(BUF_A) == 68


# ── Process / thread identity ─────────────────────────────────────────────────

class TestProcessThreadIdentity:

    def test_get_current_process(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetCurrentProcess", [])
        assert cpu.regs[EAX] == 0xFFFFFFFF

    def test_get_current_process_id(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetCurrentProcessId", [])
        assert cpu.regs[EAX] == 1

    def test_get_current_thread(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetCurrentThread", [])
        assert cpu.regs[EAX] == 0xFFFFFFFE

    def test_get_current_thread_id_matches_state(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetCurrentThreadId", [])
        assert cpu.regs[EAX] == state.tls_current_thread_id()


# ── Error / tick / time ────────────────────────────────────────────────────────

class TestLastError:

    def test_round_trip(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "SetLastError", [6])
        call(stubs, cpu, mem, "GetLastError", [])
        assert cpu.regs[EAX] == 6

    def test_set_last_error_writes_teb(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "SetLastError", [123])
        assert mem.read32(TEB_BASE + 0x34) == 123


class TestGetTickCount:

    def test_matches_virtual_ticks(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetTickCount", [])
        assert cpu.regs[EAX] == state.virtual_ticks_ms

    def test_starts_at_zero(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetTickCount", [])
        assert cpu.regs[EAX] == 0


class TestQueryPerformance:

    def test_frequency_is_one_mhz(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "QueryPerformanceFrequency", [BUF_A])
        assert mem.read32(BUF_A) == 1_000_000
        assert mem.read32(BUF_A + 4) == 0

    def test_counter_null_pointer_still_returns_true(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "QueryPerformanceCounter", [0])
        assert cpu.regs[EAX] == 1

    def test_frequency_null_pointer_still_returns_true(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "QueryPerformanceFrequency", [0])
        assert cpu.regs[EAX] == 1

    def test_counter_writes_monotonic_value(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "QueryPerformanceCounter", [BUF_A])
        first = mem.read32(BUF_A) | (mem.read32(BUF_A + 4) << 32)
        time.sleep(0.001)
        call(stubs, cpu, mem, "QueryPerformanceCounter", [BUF_A])
        second = mem.read32(BUF_A) | (mem.read32(BUF_A + 4) << 32)
        assert second >= first


# ── System info ────────────────────────────────────────────────────────────────

class TestGetSystemInfo:

    def test_page_size(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetSystemInfo", [BUF_A])
        assert mem.read32(BUF_A + 4) == 4096

    def test_processor_type(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetSystemInfo", [BUF_A])
        assert mem.read32(BUF_A + 24) == 586

    def test_processor_architecture_intel(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetSystemInfo", [BUF_A])
        assert mem.read16(BUF_A) == 0


# ── Exit / debug ───────────────────────────────────────────────────────────────

class TestExitAndDebug:

    def test_exit_process_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "ExitProcess", [0])
        assert cpu.halted is True

    def test_is_debugger_present_false(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "IsDebuggerPresent", [])
        assert cpu.regs[EAX] == 0

    @pytest.mark.parametrize("feature", [2, 3, 8])
    def test_supported_processor_features(self, env, feature):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "IsProcessorFeaturePresent", [feature])
        assert cpu.regs[EAX] == 1

    def test_unsupported_processor_feature(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "IsProcessorFeaturePresent", [99])
        assert cpu.regs[EAX] == 0

    def test_set_unhandled_exception_filter(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "SetUnhandledExceptionFilter", [0])
        assert cpu.regs[EAX] == 0

    def test_unhandled_exception_filter(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "UnhandledExceptionFilter", [0])
        assert cpu.regs[EAX] == 0


# ── Environment strings ────────────────────────────────────────────────────────

class TestEnvironmentStrings:

    def test_get_environment_strings_w(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetEnvironmentStringsW", [])
        assert cpu.regs[EAX] == 0x002100F0

    def test_free_environment_strings_w(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "FreeEnvironmentStringsW", [0x002100F0])
        assert cpu.regs[EAX] == 1

    def test_get_environment_strings_a(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetEnvironmentStrings", [])
        assert cpu.regs[EAX] == 0x002100F8

    def test_free_environment_strings_a(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "FreeEnvironmentStringsA", [0x002100F8])
        assert cpu.regs[EAX] == 1


# ── Standard handles / file type ──────────────────────────────────────────────

class TestGetStdHandle:

    def test_stdin_handle(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetStdHandle", [0xFFFFFFF6])
        assert cpu.regs[EAX] == 0x00000100 | 0xF6

    def test_stdout_handle_registers_writable_fd_1(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetStdHandle", [0xFFFFFFF5])
        handle = cpu.regs[EAX]
        entry = state.file_handle_map[handle]
        assert entry.writable is True
        assert entry.fd == 1

    def test_stderr_handle_registers_writable_fd_2(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetStdHandle", [0xFFFFFFF4])
        handle = cpu.regs[EAX]
        entry = state.file_handle_map[handle]
        assert entry.writable is True
        assert entry.fd == 2

    def test_unrecognized_n_does_not_create_entry(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetStdHandle", [0x12345678])
        handle = cpu.regs[EAX]
        assert handle not in state.file_handle_map

    def test_second_call_does_not_recreate_entry(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetStdHandle", [0xFFFFFFF5])
        handle = cpu.regs[EAX]
        entry_first = state.file_handle_map[handle]
        call(stubs, cpu, mem, "GetStdHandle", [0xFFFFFFF5])
        entry_second = state.file_handle_map[handle]
        assert entry_first is entry_second


class TestGetFileType:

    def test_known_std_handle_returns_file_type_char(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetStdHandle", [0xFFFFFFF5])
        handle = cpu.regs[EAX]
        call(stubs, cpu, mem, "GetFileType", [handle])
        assert cpu.regs[EAX] == 2

    def test_unknown_handle_halts(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "GetFileType", [0xDEADBEEF])
        assert cpu.halted is True

    def test_real_disk_file_returns_file_type_disk(self, env, tmp_path):
        """The actual 2026-08-07 bug: this used to key off `entry.fd is not
        None`, which every real disk file also satisfies (read-write and
        write-only entries both keep a live fd open) -- so real files always
        got FILE_TYPE_CHAR(2) instead of FILE_TYPE_DISK(1). Confirmed live as
        the true cause of msjet35.dll rejecting a byte-perfect, correctly-
        signed Tmp.MDB as "unrecognized database format": real Jet calls
        GetFileType right after CreateFile and aborts immediately if it isn't
        exactly FILE_TYPE_DISK, before ever reading a single byte."""
        from tew.api._state import FileHandleEntry
        cpu, mem, state, stubs = env
        real_path = tmp_path / "Tmp.MDB"
        real_path.write_bytes(b"Standard Jet DB")
        import os
        fd = os.open(str(real_path), os.O_RDWR)
        handle = 0x5041
        state.file_handle_map[handle] = FileHandleEntry(
            path=str(real_path), data=b"", position=0, writable=True, fd=fd, readable=True)

        call(stubs, cpu, mem, "GetFileType", [handle])

        assert cpu.regs[EAX] == 1  # FILE_TYPE_DISK
        os.close(fd)

    def test_nul_device_still_returns_file_type_char(self, env):
        """NUL entries keep a real, live fd too (open_file_handle's NUL
        branch does os.open("/dev/null", O_WRONLY)) -- must still report
        FILE_TYPE_CHAR despite having an fd, same as std handles."""
        from tew.api._state import FileHandleEntry
        cpu, mem, state, stubs = env
        import os
        fd = os.open("/dev/null", os.O_WRONLY)
        handle = 0x5099
        state.file_handle_map[handle] = FileHandleEntry(
            path="/dev/null", data=b"", position=0, writable=True, fd=fd)

        call(stubs, cpu, mem, "GetFileType", [handle])

        assert cpu.regs[EAX] == 2  # FILE_TYPE_CHAR
        os.close(fd)


# ── Pointer encode/decode ──────────────────────────────────────────────────────

class TestEncodeDecodePointer:

    def test_encode_pointer_is_identity(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "EncodePointer", [0x12345678])
        assert cpu.regs[EAX] == 0x12345678

    def test_decode_pointer_is_identity(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "DecodePointer", [0x12345678])
        assert cpu.regs[EAX] == 0x12345678


# ── InterlockedCompareExchange ────────────────────────────────────────────────

class TestInterlockedCompareExchange:

    def test_match_writes_exchange_value(self, env):
        cpu, mem, state, stubs = env
        mem.write32(BUF_A, 100)
        call(stubs, cpu, mem, "InterlockedCompareExchange", [BUF_A, 200, 100])
        assert mem.read32(BUF_A) == 200
        assert cpu.regs[EAX] == 100

    def test_no_match_leaves_memory_untouched(self, env):
        cpu, mem, state, stubs = env
        mem.write32(BUF_A, 100)
        call(stubs, cpu, mem, "InterlockedCompareExchange", [BUF_A, 200, 999])
        assert mem.read32(BUF_A) == 100
        assert cpu.regs[EAX] == 100
