"""Tests for tew.api.patch_internals — CRT internal function patches.

Unlike the rest of the Win32 handlers cluster, patch_crt_internals writes
INT 0xFE; RET bytes directly into memory at hardcoded game addresses via
Win32Handlers.patch_address, so these tests use the REAL Win32Handlers
class (not the _StubHandlers test fake used elsewhere) to exercise that
side effect for real.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tew.api._state import CRTState, EmulatorConfig, FileHandleEntry
from tew.api.patch_internals import patch_crt_internals
from tew.api.win32_handlers import (
    Win32Handlers,
    DIALOG_TRAMPOLINE,
    DLLMAIN_TRAMPOLINE,
    DLLMAIN_HANDLE_STORE,
)
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP, EBP, ZF_BIT
from tew import logger as logger_module

MEM_SIZE = 64 * 1024 * 1024  # must cover SNDMEMI_STRUCT_PTR (~33MB)
STACK    = 0x200000

CHKESP_ADDR       = 0x009F1BC0
CRT_DBG_REPORT    = 0x009F9300
CHANNEL_DBG_PRINT = 0x004CC5B0
CHANNEL_SYS_PRINT = 0x004CBDE0
FREE_DBG          = 0x009F6E20
WINMAIN_CHECK1    = 0x0040D1D4
WINMAIN_CHECK2    = 0x0040159B
SNDMEMI_STRUCT_PTR = 0x020DEF78
SNDMEMI_INIT_ADDR  = 0x00A5422A
SNDMEMI_VALIDATE_ADDR = 0x00A54107


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False
        self.fatal_halt = False
        self.eflags = 0

    def get_flag(self, bit: int) -> bool:
        return ((self.eflags >> bit) & 1) == 1

    def set_flag(self, bit: int, val: bool) -> None:
        if val:
            self.eflags |= (1 << bit)
        else:
            self.eflags &= ~(1 << bit)


@pytest.fixture
def env(tmp_path):
    # Scoped path mapping (not the real ~/.emu32/ tree) -- Channel_DebugPrint's
    # patch now always writes a real host file (channel_log.txt, see
    # write_channel_log/_state.py), so this fixture must not resolve to real
    # user paths as an unintended side effect of running the test suite.
    config = EmulatorConfig(
        path_mappings={"c:/": str(tmp_path) + "/"},
        interactive_on_missing_file=False,
    )
    mem   = Memory(MEM_SIZE)
    state = CRTState(config=config)
    stubs = Win32Handlers(mem)
    patch_crt_internals(stubs, mem, state)
    cpu = _FakeCPU()
    return cpu, mem, state, stubs


@pytest.fixture
def captured_logs():
    lines: list[str] = []
    logger_module.set_emit_hook(lambda level, line: lines.append(line))
    yield lines
    logger_module.set_emit_hook(None)


def patched(stubs, addr):
    return stubs._patched_addrs[addr].handler


def write_cstring(mem, addr, s: str) -> None:
    data = s.encode("ascii") + b"\x00"
    for i, b in enumerate(data):
        mem.write8(addr + i, b)


# ── Patch side effect ──────────────────────────────────────────────────────────

class TestPatchSideEffect:

    def test_bytes_written_at_patched_address(self, env):
        cpu, mem, state, stubs = env
        assert mem.read8(CHKESP_ADDR) == 0xCD
        assert mem.read8(CHKESP_ADDR + 1) == 0xFE
        assert mem.read8(CHKESP_ADDR + 2) == 0xC3

    def test_bytes_written_at_second_address(self, env):
        cpu, mem, state, stubs = env
        assert mem.read8(CRT_DBG_REPORT) == 0xCD
        assert mem.read8(CRT_DBG_REPORT + 1) == 0xFE
        assert mem.read8(CRT_DBG_REPORT + 2) == 0xC3


# ── Dialog / DllMain trampolines ────────────────────────────────────────────────

class TestDialogFinishIdok:

    def test_returns_idok(self, env):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        patched(stubs, DIALOG_TRAMPOLINE)(cpu)
        assert cpu.regs[EAX] == 1

    def test_skips_four_stack_args(self, env):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        patched(stubs, DIALOG_TRAMPOLINE)(cpu)
        assert cpu.regs[ESP] == STACK + 16


class TestDllMainFinish:

    def test_restores_handle_from_store(self, env):
        cpu, mem, state, stubs = env
        mem.write32(DLLMAIN_HANDLE_STORE, 0xDEADBEEF)
        patched(stubs, DLLMAIN_TRAMPOLINE)(cpu)
        assert cpu.regs[EAX] == 0xDEADBEEF


# ── WinMain checks ──────────────────────────────────────────────────────────────

class TestWinmainCheck1:

    def test_returns_nonzero_sentinel(self, env):
        cpu, mem, state, stubs = env
        patched(stubs, WINMAIN_CHECK1)(cpu)
        assert cpu.regs[EAX] == 0x12345678


class TestWinmainCheck2:

    def test_writes_parseable_version_string(self, env):
        cpu, mem, state, stubs = env
        buf = 0x300000
        cpu.regs[ESP] = STACK
        mem.write32(STACK + 4, buf)
        patched(stubs, WINMAIN_CHECK2)(cpu)
        out = bytearray()
        while True:
            b = mem.read8(buf + len(out))
            if b == 0:
                break
            out.append(b)
        assert out == b"1, 2, 3"

    def test_returns_nonzero(self, env):
        cpu, mem, state, stubs = env
        buf = 0x300000
        cpu.regs[ESP] = STACK
        mem.write32(STACK + 4, buf)
        patched(stubs, WINMAIN_CHECK2)(cpu)
        assert cpu.regs[EAX] == 1


# ── __chkesp ─────────────────────────────────────────────────────────────────

class TestChkesp:

    def test_zf_set_does_not_halt(self, env):
        cpu, mem, state, stubs = env
        cpu.set_flag(ZF_BIT, True)
        patched(stubs, CHKESP_ADDR)(cpu)
        assert cpu.halted is False

    def test_zf_clear_halts(self, env):
        cpu, mem, state, stubs = env
        cpu.set_flag(ZF_BIT, False)
        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0x00401234)  # fake return address
        cpu.regs[EBP] = STACK
        patched(stubs, CHKESP_ADDR)(cpu)
        assert cpu.halted is True

    def test_zf_clear_sets_fatal_halt(self, env):
        cpu, mem, state, stubs = env
        cpu.set_flag(ZF_BIT, False)
        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0x00401234)
        cpu.regs[EBP] = STACK
        patched(stubs, CHKESP_ADDR)(cpu)
        assert cpu.fatal_halt is True

    def test_zf_clear_diagnostic_read_does_not_crash(self, env):
        cpu, mem, state, stubs = env
        cpu.set_flag(ZF_BIT, False)
        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0x00401234)
        cpu.regs[EBP] = STACK + 100
        patched(stubs, CHKESP_ADDR)(cpu)  # must not raise
        assert cpu.halted is True


# ── _CrtDbgReport ────────────────────────────────────────────────────────────

class TestCrtDbgReport:

    def _set_args(self, mem, sp, report_type, filename_ptr=0, line_number=0,
                   module_name_ptr=0, format_ptr=0):
        mem.write32(sp + 4,  report_type)
        mem.write32(sp + 8,  filename_ptr)
        mem.write32(sp + 12, line_number)
        mem.write32(sp + 16, module_name_ptr)
        mem.write32(sp + 20, format_ptr)

    def test_crt_warn_does_not_halt(self, env):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        self._set_args(mem, STACK, report_type=0)
        patched(stubs, CRT_DBG_REPORT)(cpu)
        assert cpu.halted is False

    def test_crt_warn_returns_zero(self, env):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        self._set_args(mem, STACK, report_type=0)
        patched(stubs, CRT_DBG_REPORT)(cpu)
        assert cpu.regs[EAX] == 0

    def test_crt_error_halts(self, env):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        self._set_args(mem, STACK, report_type=1)
        patched(stubs, CRT_DBG_REPORT)(cpu)
        assert cpu.halted is True
        assert cpu.fatal_halt is True
        assert cpu.regs[EAX] == 1

    def test_crt_assert_halts(self, env):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        self._set_args(mem, STACK, report_type=2)
        patched(stubs, CRT_DBG_REPORT)(cpu)
        assert cpu.halted is True
        assert cpu.fatal_halt is True

    def test_null_ish_pointers_do_not_crash(self, env):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        self._set_args(mem, STACK, report_type=1, filename_ptr=0, format_ptr=0)
        patched(stubs, CRT_DBG_REPORT)(cpu)  # must not raise
        assert cpu.halted is True

    def test_percent_s_substitution(self, env, captured_logs):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        arg_ptr = 0x300100
        write_cstring(mem, fmt_ptr, "assert failed: %s")
        write_cstring(mem, arg_ptr, "pool corrupt")
        self._set_args(mem, STACK, report_type=1, format_ptr=fmt_ptr)
        mem.write32(STACK + 24, arg_ptr)
        patched(stubs, CRT_DBG_REPORT)(cpu)
        assert any("pool corrupt" in line for line in captured_logs)

    def test_percent_d_substitution(self, env, captured_logs):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        write_cstring(mem, fmt_ptr, "code=%d")
        self._set_args(mem, STACK, report_type=1, format_ptr=fmt_ptr)
        mem.write32(STACK + 24, 42)
        patched(stubs, CRT_DBG_REPORT)(cpu)
        assert any("code=42" in line for line in captured_logs)

    def test_hex_specifier_is_substituted(self, env, captured_logs):
        # Regression guard for the 2026-08-29 fix: substitution now goes
        # through the shared _sprintf_format engine (same one msvcrt's
        # printf/sprintf use) instead of a %s-or-%d-only special case, so
        # %x/%08X/%u/%hs/etc. from the debug CRT's own report strings (e.g.
        # _CrtDumpMemoryLeaks's "normal block at 0x%08X, %u bytes long.")
        # now substitute correctly instead of appearing literally.
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        write_cstring(mem, fmt_ptr, "value=%x")
        self._set_args(mem, STACK, report_type=1, format_ptr=fmt_ptr)
        mem.write32(STACK + 24, 0)
        patched(stubs, CRT_DBG_REPORT)(cpu)  # must not raise
        assert any("value=0" in line for line in captured_logs)

    def test_unrecognized_specifier_is_passthrough(self, env, captured_logs):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        write_cstring(mem, fmt_ptr, "value=%q")  # not a real conversion
        self._set_args(mem, STACK, report_type=1, format_ptr=fmt_ptr)
        mem.write32(STACK + 24, 0)
        patched(stubs, CRT_DBG_REPORT)(cpu)  # must not raise
        assert any("value=%q" in line for line in captured_logs)

    def test_unreadable_filename_pointer_does_not_crash(self, env):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        self._set_args(mem, STACK, report_type=0, filename_ptr=0xFFFFFFF0)
        patched(stubs, CRT_DBG_REPORT)(cpu)  # must not raise
        assert cpu.halted is False

    def test_unreadable_format_pointer_does_not_crash(self, env):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        self._set_args(mem, STACK, report_type=0, format_ptr=0xFFFFFFF0)
        patched(stubs, CRT_DBG_REPORT)(cpu)  # must not raise
        assert cpu.halted is False

    def test_unreadable_percent_s_arg_pointer_does_not_crash(self, env, captured_logs):
        # The shared _sprintf_format engine (2026-08-29) has no per-call
        # bad-pointer fallback of its own -- an unreadable %s pointer raises
        # inside read_cstring, which _crt_dbg_report now catches around the
        # whole substitution call, logging the failure and falling back to
        # the raw, unsubstituted format string rather than crashing.
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        write_cstring(mem, fmt_ptr, "bad: %s")
        self._set_args(mem, STACK, report_type=1, format_ptr=fmt_ptr)
        mem.write32(STACK + 24, 0xFFFFFFF0)
        patched(stubs, CRT_DBG_REPORT)(cpu)  # must not raise -- the failure is
        # only logged at DEBUG (filtered out under this fixture's default
        # level), so check the fallback behavior visible at the report's own
        # level instead: the raw, unsubstituted format string.
        assert any("bad: %s" in line for line in captured_logs)

    def test_null_percent_s_arg_pointer_substitutes_empty_string(self, env, captured_logs):
        # _sprintf_format's own %s handling (shared with printf/sprintf)
        # treats exactly NULL as an empty string, not the literal "(null)"
        # tew's old bespoke substitution used for any near-null pointer.
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        write_cstring(mem, fmt_ptr, "val: %s")
        self._set_args(mem, STACK, report_type=1, format_ptr=fmt_ptr)
        mem.write32(STACK + 24, 0)
        patched(stubs, CRT_DBG_REPORT)(cpu)
        assert any("val: " in line and "val: (null)" not in line for line in captured_logs)


@pytest.fixture(autouse=True)
def _channel_debug_level(request):
    """Channel_DebugPrint/Channel_SystemPrint log at DEBUG (2026-08-07,
    demoted from WARN/INFO -- real per-track/per-asset chatter that
    drowned out run_exe.py's [alive] progress heartbeat under default
    LOG_LEVEL=info). Autouse, but scoped to just the two classes that
    exercise these functions' formatting logic (via a marker below) --
    everything else in this file keeps the module's real default level.
    """
    if request.cls is None or request.cls.__name__ not in (
        "TestChannelDebugPrint", "TestChannelSystemPrint",
    ):
        yield
        return
    saved_level = logger_module._active_level
    logger_module.configure_logger(level="debug")
    yield
    logger_module._active_level = saved_level


# ── Channel_DebugPrint ─────────────────────────────────────────────────────────

class TestChannelDebugPrint:

    def test_multi_vararg_substitution(self, env, captured_logs):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        str_ptr = 0x300100
        write_cstring(mem, fmt_ptr, "Hello %s number %d!")
        write_cstring(mem, str_ptr, "World")
        mem.write32(STACK + 4, 1)   # user
        mem.write32(STACK + 8, 2)   # channel
        mem.write32(STACK + 12, fmt_ptr)
        mem.write32(STACK + 16, str_ptr)  # first vararg (%s)
        mem.write32(STACK + 20, 42)       # second vararg (%d)
        patched(stubs, CHANNEL_DBG_PRINT)(cpu)
        assert any("Hello World number 42!" in line for line in captured_logs)

    def test_no_percent_is_passthrough(self, env, captured_logs):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        write_cstring(mem, fmt_ptr, "plain message")
        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, 0)
        mem.write32(STACK + 12, fmt_ptr)
        patched(stubs, CHANNEL_DBG_PRINT)(cpu)
        assert any("plain message" in line for line in captured_logs)

    def test_unreadable_format_pointer_does_not_crash(self, env, captured_logs):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, 0)
        mem.write32(STACK + 12, 0xFFFFFFF0)
        patched(stubs, CHANNEL_DBG_PRINT)(cpu)  # must not raise
        assert any("(null)" in line for line in captured_logs)

    def test_unreadable_percent_s_arg_pointer_uses_bad_ptr_fallback(self, env, captured_logs):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        write_cstring(mem, fmt_ptr, "bad: %s")
        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, 0)
        mem.write32(STACK + 12, fmt_ptr)
        mem.write32(STACK + 16, 0xFFFFFFF0)
        patched(stubs, CHANNEL_DBG_PRINT)(cpu)  # must not raise
        assert any("<bad ptr" in line for line in captured_logs)

    def test_null_ish_format_pointer_stays_null(self, env, captured_logs):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, 0)
        mem.write32(STACK + 12, 0)  # fmt_ptr <= 0x1000 -- skip the read entirely
        patched(stubs, CHANNEL_DBG_PRINT)(cpu)
        assert any("(null)" in line for line in captured_logs)

    def test_null_ish_percent_s_arg_pointer_stays_null(self, env, captured_logs):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        write_cstring(mem, fmt_ptr, "val: %s")
        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, 0)
        mem.write32(STACK + 12, fmt_ptr)
        mem.write32(STACK + 16, 0)  # arg_ptr <= 0x1000
        patched(stubs, CHANNEL_DBG_PRINT)(cpu)
        assert any("val: (null)" in line for line in captured_logs)


class TestChannelSystemPrint:
    """Channel_SystemPrint(const char *format, ...) -- unlike
    Channel_DebugPrint (user, channel, format, ...), the format string is
    the *first* arg (at [esp+4]), varargs start at [esp+8]. Molly requested
    (2026-08-07) this one also write to the guest's real stdout stream when
    CRTState.guest_stdout_handle is set, not just tew's own log."""

    def test_multi_vararg_substitution_is_logged(self, env, captured_logs):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        str_ptr = 0x300100
        write_cstring(mem, fmt_ptr, "Hello %s number %d!")
        write_cstring(mem, str_ptr, "World")
        mem.write32(STACK + 4, fmt_ptr)
        mem.write32(STACK + 8, str_ptr)   # first vararg (%s)
        mem.write32(STACK + 12, 42)       # second vararg (%d)
        patched(stubs, CHANNEL_SYS_PRINT)(cpu)
        assert any("Hello World number 42!" in line for line in captured_logs)

    def test_null_ish_format_pointer_does_not_crash(self, env, captured_logs):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        mem.write32(STACK + 4, 0)  # fmt_ptr <= 0x1000 -- skip the read entirely
        patched(stubs, CHANNEL_SYS_PRINT)(cpu)  # must not raise
        assert any("(null)" in line for line in captured_logs)

    def test_no_guest_stdout_handle_set_does_not_crash(self, env, captured_logs):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        write_cstring(mem, fmt_ptr, "plain message")
        mem.write32(STACK + 4, fmt_ptr)
        assert state.guest_stdout_handle is None
        patched(stubs, CHANNEL_SYS_PRINT)(cpu)  # must not raise
        assert any("plain message" in line for line in captured_logs)

    def test_writes_to_guest_stdout_handle_when_set(self, env, captured_logs, tmp_path):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        write_cstring(mem, fmt_ptr, "to stdout: %d\n")
        mem.write32(STACK + 4, fmt_ptr)
        mem.write32(STACK + 8, 7)

        import os
        out_path = tmp_path / "stdout.txt"
        fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT, 0o644)
        state.file_handle_map[0x5000] = FileHandleEntry(
            path=str(out_path), data=b"", position=0, writable=True, fd=fd
        )
        state.guest_stdout_handle = 0x5000

        patched(stubs, CHANNEL_SYS_PRINT)(cpu)
        os.close(fd)

        assert out_path.read_bytes() == b"to stdout: 7\n"


@pytest.fixture
def channel_category_filtered_out():
    """Simulates a real run where "channel" isn't in LOG_CATEGORIES --
    both patches must skip their vararg-formatting work entirely in this
    case (2026-08-07, Molly: "too laggy" once channel logging started
    actually producing output at real gameplay volume)."""
    saved_level = logger_module._active_level
    saved_categories = logger_module._active_categories
    logger_module.configure_logger(level="info", categories="cpu")  # anything without "channel"
    yield
    logger_module._active_level = saved_level
    logger_module._active_categories = saved_categories


class TestChannelPrintSkipsWorkWhenFiltered:
    def test_debug_print_skips_tew_log_but_still_writes_channel_log_when_filtered(
        self, env, captured_logs, channel_category_filtered_out,
    ):
        """channel_log.txt (2026-08-08, Molly: "so we can tell it from the
        other 'normal' stuff") is a real host file Channel_DebugPrint
        writes to unconditionally -- unlike tew's own /tmp/emu.log, it is
        NOT subject to LOG_LEVEL/LOG_CATEGORIES filtering, so it must still
        get the formatted message even while tew's own log stays silent."""
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        write_cstring(mem, fmt_ptr, "still reaches channel_log.txt")
        mem.write32(STACK + 4, 0)
        mem.write32(STACK + 8, 0)
        mem.write32(STACK + 12, fmt_ptr)

        patched(stubs, CHANNEL_DBG_PRINT)(cpu)  # must not raise

        assert captured_logs == []
        assert state.channel_log_fd is not None
        os.fsync(state.channel_log_fd)
        host_path = state.translate_windows_path("channel_log.txt")
        assert "still reaches channel_log.txt" in Path(host_path).read_text()

    def test_system_print_skips_when_filtered_and_no_stdout_handle(
        self, env, captured_logs, channel_category_filtered_out,
    ):
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        write_cstring(mem, fmt_ptr, "should not be formatted")
        mem.write32(STACK + 4, fmt_ptr)
        assert state.guest_stdout_handle is None

        patched(stubs, CHANNEL_SYS_PRINT)(cpu)  # must not raise, must not format

        assert captured_logs == []

    def test_system_print_still_writes_stdout_when_filtered(
        self, env, captured_logs, channel_category_filtered_out, tmp_path,
    ):
        """Even with "channel" logging filtered out, the real stdout
        redirect must still work -- that's the whole point of
        guest_stdout_handle, independent of what tew's own log shows."""
        cpu, mem, state, stubs = env
        cpu.regs[ESP] = STACK
        fmt_ptr = 0x300000
        write_cstring(mem, fmt_ptr, "still reaches stdout.txt\n")
        mem.write32(STACK + 4, fmt_ptr)

        import os
        out_path = tmp_path / "stdout.txt"
        fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT, 0o644)
        state.file_handle_map[0x5000] = FileHandleEntry(
            path=str(out_path), data=b"", position=0, writable=True, fd=fd
        )
        state.guest_stdout_handle = 0x5000

        patched(stubs, CHANNEL_SYS_PRINT)(cpu)
        os.close(fd)

        assert out_path.read_bytes() == b"still reaches stdout.txt\n"
        assert captured_logs == []  # log itself still suppressed


# ── __free_dbg ───────────────────────────────────────────────────────────────

class TestFreeDbgNoop:

    def test_no_observable_effect(self, env):
        cpu, mem, state, stubs = env
        cpu.regs[EAX] = 0x1234
        patched(stubs, FREE_DBG)(cpu)  # must not raise
        assert cpu.regs[EAX] == 0x1234
        assert cpu.halted is False


# ── SNDMEMI_init ─────────────────────────────────────────────────────────────

class TestSndmemiInit:

    def test_alignment_and_field_writes(self, env):
        cpu, mem, state, stubs = env
        param_1 = 0x00300002  # deliberately unaligned
        param_2 = 0x1000
        cpu.regs[ESP] = STACK
        mem.write32(STACK + 4, param_1)
        mem.write32(STACK + 8, param_2)
        patched(stubs, SNDMEMI_INIT_ADDR)(cpu)

        expected_pool_base = ((param_1 + 40) + 3) & ~3
        assert mem.read32(SNDMEMI_STRUCT_PTR) == param_1
        assert mem.read32(param_1) == expected_pool_base
        assert mem.read32(param_1 + 4) == (param_1 + param_2 - 0x18) & 0xFFFFFFFF
        assert mem.read32(param_1 + 8) == param_2
        assert mem.read32(param_1 + 12) == (param_2 - 0x43) & 0xFFFFFFFF
        assert mem.read32(param_1 + 16) == (param_2 - 3) & 0xFFFFFFFF
        assert mem.read32(param_1 + 20) == 0


# ── SNDMEMI_validate ───────────────────────────────────────────────────────────

class TestSndmemiValidate:

    def test_null_pool_early_return(self, env):
        cpu, mem, state, stubs = env
        # SNDMEMI_STRUCT_PTR reads 0 on fresh memory -- never initialised.
        patched(stubs, SNDMEMI_VALIDATE_ADDR)(cpu)  # must not raise

    def test_zero_entry_count_logs_first_alloc(self, env, captured_logs):
        cpu, mem, state, stubs = env
        pool_ptr = 0x00300000
        mem.write32(SNDMEMI_STRUCT_PTR, pool_ptr)
        mem.write32(pool_ptr, 0x00310000)      # pool_base
        mem.write32(pool_ptr + 4, 0x00320000)  # blist_ptr
        mem.write32(pool_ptr + 20, 0)          # entry_count = 0
        cpu.regs[EBP] = STACK
        mem.write32(STACK + 8, 0x00330000)  # param1 for the [count=0] log line
        patched(stubs, SNDMEMI_VALIDATE_ADDR)(cpu)
        assert any("[count=0]" in line for line in captured_logs)

    def test_zero_entry_count_unreadable_ebp_falls_back_to_zero(self, env, captured_logs):
        cpu, mem, state, stubs = env
        pool_ptr = 0x00300000
        mem.write32(SNDMEMI_STRUCT_PTR, pool_ptr)
        mem.write32(pool_ptr, 0x00310000)
        mem.write32(pool_ptr + 4, 0x00320000)
        mem.write32(pool_ptr + 20, 0)
        cpu.regs[EBP] = 0xFFFFFFF0  # unreadable -- forces the except -> param1=0 path
        patched(stubs, SNDMEMI_VALIDATE_ADDR)(cpu)  # must not raise
        assert any("param1=0x00000000" in line for line in captured_logs)

    def test_valid_sentinels_no_corruption_reported(self, env, captured_logs):
        cpu, mem, state, stubs = env
        pool_ptr  = 0x00300000
        pool_base = 0x00310000
        blist_ptr = 0x00320000
        mem.write32(SNDMEMI_STRUCT_PTR, pool_ptr)
        mem.write32(pool_ptr, pool_base)
        mem.write32(pool_ptr + 4, blist_ptr)
        mem.write32(pool_ptr + 20, (-2) & 0xFFFFFFFF)  # entry_count: 2 entries

        # entry 0 at blist_ptr
        mem.write32(blist_ptr, 0)       # start
        mem.write32(blist_ptr + 4, 8)   # size
        mem.write32(pool_base + 0, 0xDEADDEAD)
        mem.write32(pool_base + 4, 0xDEADDEAD)

        # entry 1 at blist_ptr - 0x18
        entry1 = blist_ptr - 0x18
        mem.write32(entry1, 0x100)      # start
        mem.write32(entry1 + 4, 8)      # size
        mem.write32(pool_base + 0x100, 0xDEADDEAD)
        mem.write32(pool_base + 0x104, 0xDEADDEAD)

        patched(stubs, SNDMEMI_VALIDATE_ADDR)(cpu)
        assert not any("CORRUPTION" in line for line in captured_logs)

    def test_corrupted_sentinel_reports_corruption(self, env, captured_logs):
        cpu, mem, state, stubs = env
        pool_ptr  = 0x00300000
        pool_base = 0x00310000
        blist_ptr = 0x00320000
        mem.write32(SNDMEMI_STRUCT_PTR, pool_ptr)
        mem.write32(pool_ptr, pool_base)
        mem.write32(pool_ptr + 4, blist_ptr)
        mem.write32(pool_ptr + 20, (-1) & 0xFFFFFFFF)  # entry_count: 1 entry

        mem.write32(blist_ptr, 0)       # start
        mem.write32(blist_ptr + 4, 8)   # size
        mem.write32(pool_base + 0, 0xDEADDEAD)
        mem.write32(pool_base + 4, 0xBADBADBA)  # corrupted hi sentinel

        patched(stubs, SNDMEMI_VALIDATE_ADDR)(cpu)
        assert any("CORRUPTION" in line for line in captured_logs)

    def test_corrupted_sentinel_ebp_walk_does_not_crash_out_of_bounds(self, env, captured_logs):
        """EBP within the plausible-stack range but unreadable in our test
        memory must break the walk via the try/except, not raise."""
        cpu, mem, state, stubs = env
        pool_ptr  = 0x00300000
        pool_base = 0x00310000
        blist_ptr = 0x00320000
        mem.write32(SNDMEMI_STRUCT_PTR, pool_ptr)
        mem.write32(pool_ptr, pool_base)
        mem.write32(pool_ptr + 4, blist_ptr)
        mem.write32(pool_ptr + 20, (-1) & 0xFFFFFFFF)

        mem.write32(blist_ptr, 0)
        mem.write32(blist_ptr + 4, 8)
        mem.write32(pool_base + 0, 0xDEADDEAD)
        mem.write32(pool_base + 4, 0xBADBADBA)

        cpu.regs[EBP] = 0x07000000  # in-range per the walk's magic bounds, out of our Memory bounds
        patched(stubs, SNDMEMI_VALIDATE_ADDR)(cpu)  # must not raise
        assert any("CORRUPTION" in line for line in captured_logs)

    def test_corrupted_sentinel_ebp_walk_logs_one_valid_frame(self, captured_logs):
        """A separate, larger Memory instance so a genuinely valid EBP frame
        (within the walk's hardcoded 0x07000000-0x7FFFFFFF magic range) is
        actually readable, exercising the walk's successful-log branch
        rather than only its exception/break paths."""
        mem   = Memory(0x08000000)  # 128MB, covers ebp=0x07000000
        state = CRTState()
        stubs = Win32Handlers(mem)
        patch_crt_internals(stubs, mem, state)
        cpu = _FakeCPU()

        pool_ptr  = 0x00300000
        pool_base = 0x00310000
        blist_ptr = 0x00320000
        mem.write32(SNDMEMI_STRUCT_PTR, pool_ptr)
        mem.write32(pool_ptr, pool_base)
        mem.write32(pool_ptr + 4, blist_ptr)
        mem.write32(pool_ptr + 20, (-1) & 0xFFFFFFFF)

        mem.write32(blist_ptr, 0)
        mem.write32(blist_ptr + 4, 8)
        mem.write32(pool_base + 0, 0xDEADDEAD)
        mem.write32(pool_base + 4, 0xBADBADBA)  # corrupted

        ebp = 0x07000000
        mem.write32(ebp, 0)              # saved_ebp = 0 -> next iteration breaks (out of range)
        mem.write32(ebp + 4, 0x00401234)  # ret_addr
        mem.write32(ebp + 8, 0xCAFEBABE)  # arg0
        cpu.regs[EBP] = ebp

        patched(stubs, SNDMEMI_VALIDATE_ADDR)(cpu)
        assert any("EBP=0x07000000" in line and "ret=0x00401234" in line and "arg0=0xcafebabe" in line
                   for line in captured_logs)

    def test_insane_count_logged_and_skipped(self, env, captured_logs):
        cpu, mem, state, stubs = env
        pool_ptr = 0x00300000
        mem.write32(SNDMEMI_STRUCT_PTR, pool_ptr)
        mem.write32(pool_ptr, 0x00310000)
        mem.write32(pool_ptr + 4, 0x00320000)
        mem.write32(pool_ptr + 20, (-2000) & 0xFFFFFFFF)  # n > 1024
        patched(stubs, SNDMEMI_VALIDATE_ADDR)(cpu)  # must not raise
        assert any("insane count" in line for line in captured_logs)
