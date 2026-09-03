"""Tests for dinput.dll / dinput8.dll DirectInput COM stubs.

COM vtable slots are registered through the same Win32Handlers.register_handler
path as ordinary Win32 API stubs, via the shared _com_stub helper (tew.api.d3d8
._helpers), which also calls get_handler_address to obtain a trampoline address
for the vtable entry -- unused by these tests (only the real INT 0xFE trampoline
machinery cares about that address), so the test fake returns 0.
"""
from __future__ import annotations

import pytest

from tew.api.dinput_handlers import (
    register_dinput_handlers,
    DI_OBJ,
    DI_VTABLE,
    DI_DEV_VTABLE,
    DI_OK,
    DI_NOTATTACHED,
    DI_POLLEDDEVICE,
    E_NOTIMPL,
    E_NOINTERFACE,
    DIERR_DEVICENOTREG,
    DIERR_UNSUPPORTED,
    DIERR_OBJECTNOTFOUND,
)
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP


class _StubHandlers:
    def __init__(self):
        self._h: dict = {}

    def register_handler(self, dll, name, fn):
        self._h[f"{dll.lower()}!{name}"] = fn

    def get(self, dll, name):
        return self._h[f"{dll.lower()}!{name}"]

    def get_handler_address(self, dll, name):
        return 0


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False


MEM_SIZE = 272 * 1024 * 1024  # must exceed the D3D8 private heap limit (0x10000000)
STACK    = 0x200000
BUF_A    = 0x300000
BUF_B    = 0x310000


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    stubs = _StubHandlers()
    register_dinput_handlers(stubs, mem)
    cpu = _FakeCPU()
    return cpu, mem, stubs


def call(stubs, cpu, mem, dll, name, args):
    """Plain stdcall handler (DirectInputCreateA/DirectInput8Create)."""
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    for i, val in enumerate(args):
        mem.write32(STACK + 4 + i * 4, val)
    stubs.get(dll, name)(cpu)


def com_call(stubs, cpu, mem, dll, name, this_ptr, args):
    """COM-style call: [ESP]=ret, [ESP+4]=this, [ESP+8..]=args."""
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4, this_ptr)
    for i, val in enumerate(args):
        mem.write32(STACK + 8 + i * 4, val)
    stubs.get(dll, name)(cpu)


# ── DirectInputCreateA / DirectInput8Create ────────────────────────────────────

class TestDirectInputCreateA:

    @pytest.mark.parametrize("dll", ["dinput.dll", "dinput8.dll"])
    def test_writes_di_obj_into_out_pointer(self, env, dll):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, dll, "DirectInputCreateA", [0, 0, BUF_A, 0])
        assert mem.read32(BUF_A) == DI_OBJ

    def test_returns_di_ok(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "dinput.dll", "DirectInputCreateA", [0, 0, BUF_A, 0])
        assert cpu.regs[EAX] == DI_OK

    def test_stdcall_cleanup(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "dinput.dll", "DirectInputCreateA", [0, 0, BUF_A, 0])
        assert cpu.regs[ESP] == STACK + 16

    def test_null_out_pointer_does_not_crash(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "dinput.dll", "DirectInputCreateA", [0, 0, 0, 0])  # must not raise
        assert cpu.regs[EAX] == DI_OK


class TestDirectInput8Create:

    def test_writes_di_obj_into_ppv_out(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "dinput8.dll", "DirectInput8Create", [0, 0, 0, BUF_A, 0])
        assert mem.read32(BUF_A) == DI_OBJ

    def test_stdcall_cleanup_five_args(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "dinput8.dll", "DirectInput8Create", [0, 0, 0, BUF_A, 0])
        assert cpu.regs[ESP] == STACK + 20

    def test_null_out_pointer_does_not_crash(self, env):
        cpu, mem, stubs = env
        call(stubs, cpu, mem, "dinput8.dll", "DirectInput8Create", [0, 0, 0, 0, 0])  # must not raise
        assert cpu.regs[EAX] == DI_OK


# ── IDirectInput2A vtable ───────────────────────────────────────────────────────

class TestDIQueryInterface:

    def test_writes_null_to_ppv(self, env):
        cpu, mem, stubs = env
        mem.write32(BUF_A, 0xFFFFFFFF)  # pre-dirty
        com_call(stubs, cpu, mem, "dinput.dll", "DI::QueryInterface", DI_OBJ, [0, BUF_A])
        assert mem.read32(BUF_A) == 0

    def test_returns_e_nointerface(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "DI::QueryInterface", DI_OBJ, [0, BUF_A])
        assert cpu.regs[EAX] == E_NOINTERFACE

    def test_null_ppv_does_not_crash(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "DI::QueryInterface", DI_OBJ, [0, 0])  # must not raise
        assert cpu.regs[EAX] == E_NOINTERFACE


class TestDICreateDevice:

    def test_allocates_device_object(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "DI::CreateDevice", DI_OBJ, [0, BUF_A, 0])
        dev_obj = mem.read32(BUF_A)
        assert dev_obj != 0
        assert mem.read32(dev_obj) == DI_DEV_VTABLE
        assert mem.read32(dev_obj + 4) == 0

    def test_returns_di_ok(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "DI::CreateDevice", DI_OBJ, [0, BUF_A, 0])
        assert cpu.regs[EAX] == DI_OK

    def test_null_out_pointer_does_not_crash(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "DI::CreateDevice", DI_OBJ, [0, 0, 0])  # must not raise
        assert cpu.regs[EAX] == DI_OK


DI_FIXED_RETURN_CASES = [
    ("DI::EnumDevices",     16, DI_OK),
    ("DI::GetDeviceStatus",  4, DI_NOTATTACHED),
    ("DI::RunControlPanel",  8, E_NOTIMPL),
    ("DI::Initialize",       8, DI_OK),
    ("DI::FindDevice",      12, DIERR_DEVICENOTREG),
]


class TestDIFixedReturnMethods:

    @pytest.mark.parametrize("name,arg_bytes,expected", DI_FIXED_RETURN_CASES)
    def test_returns_expected_code(self, env, name, arg_bytes, expected):
        cpu, mem, stubs = env
        args = [0] * (arg_bytes // 4)
        com_call(stubs, cpu, mem, "dinput.dll", name, DI_OBJ, args)
        assert cpu.regs[EAX] == expected


class TestExpectedThisGuard:

    def test_wrong_this_halts(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "DI::AddRef", 0xBADBAD00, [])
        assert cpu.halted is True

    def test_correct_this_does_not_halt(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "DI::AddRef", DI_OBJ, [])
        assert cpu.halted is False


# ── IDirectInputDevice2A vtable ─────────────────────────────────────────────────

DEV_OBJ = 0x00500000  # arbitrary device object address -- Dev:: stubs don't check `this`


class TestDevQueryInterface:

    def test_echoes_this_pointer_into_ppv(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::QueryInterface", DEV_OBJ, [0, BUF_A])
        assert mem.read32(BUF_A) == DEV_OBJ

    def test_returns_di_ok(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::QueryInterface", DEV_OBJ, [0, BUF_A])
        assert cpu.regs[EAX] == DI_OK

    def test_null_ppv_does_not_crash(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::QueryInterface", DEV_OBJ, [0, 0])  # must not raise
        assert cpu.regs[EAX] == DI_OK


class TestDevGetCapabilities:

    def test_null_pointer_no_write(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::GetCapabilities", DEV_OBJ, [0])
        assert cpu.regs[EAX] == DI_OK

    def test_caller_specified_zero_uses_minimum_44(self, env):
        cpu, mem, stubs = env
        for off in range(0, 100, 4):
            mem.write32(BUF_A + off, 0xFFFFFFFF)
        mem.write32(BUF_A, 0)  # dwSize = 0, written last so it isn't clobbered by the dirty-fill above
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::GetCapabilities", DEV_OBJ, [BUF_A])
        assert all(mem.read32(BUF_A + off) == 0 for off in range(0, 44, 4))
        assert mem.read32(BUF_A + 44) == 0xFFFFFFFF  # untouched past the minimum

    def test_caller_specified_larger_size_zeroes_full_size(self, env):
        cpu, mem, stubs = env
        for off in range(0, 104, 4):
            mem.write32(BUF_A + off, 0xFFFFFFFF)
        mem.write32(BUF_A, 100)  # dwSize = 100, larger than the 44-byte minimum, written last
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::GetCapabilities", DEV_OBJ, [BUF_A])
        assert all(mem.read32(BUF_A + off) == 0 for off in range(0, 100, 4))


class TestDevGetDeviceState:

    def test_zero_fills_buffer(self, env):
        cpu, mem, stubs = env
        for i in range(16):
            mem.write8(BUF_A + i, 0xFF)
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::GetDeviceState", DEV_OBJ, [16, BUF_A])
        assert all(mem.read8(BUF_A + i) == 0 for i in range(16))

    def test_null_pointer_no_crash(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::GetDeviceState", DEV_OBJ, [16, 0])  # must not raise
        assert cpu.regs[EAX] == DI_OK

    def test_zero_cb_data_no_crash(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::GetDeviceState", DEV_OBJ, [0, BUF_A])  # must not raise
        assert cpu.regs[EAX] == DI_OK


class TestDevGetDeviceData:

    def test_writes_zero_events_when_pointer_given(self, env):
        cpu, mem, stubs = env
        mem.write32(BUF_A, 0xFFFFFFFF)
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::GetDeviceData", DEV_OBJ, [0, 0, BUF_A, 0])
        assert mem.read32(BUF_A) == 0

    def test_null_pointer_no_write(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::GetDeviceData", DEV_OBJ, [0, 0, 0, 0])  # must not raise
        assert cpu.regs[EAX] == DI_OK


class TestDevSetEventNotification:

    def test_null_handle_returns_polled(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::SetEventNotification", DEV_OBJ, [0])
        assert cpu.regs[EAX] == DI_POLLEDDEVICE

    def test_nonzero_handle_returns_di_ok(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::SetEventNotification", DEV_OBJ, [0x1234])
        assert cpu.regs[EAX] == DI_OK


class TestDevGetDeviceInfo:

    def test_caller_specified_zero_uses_minimum_80(self, env):
        cpu, mem, stubs = env
        for off in range(0, 120, 4):
            mem.write32(BUF_A + off, 0xFFFFFFFF)
        mem.write32(BUF_A, 0)  # dwSize = 0, written last so it isn't clobbered by the dirty-fill above
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::GetDeviceInfo", DEV_OBJ, [BUF_A])
        assert all(mem.read32(BUF_A + off) == 0 for off in range(0, 80, 4))
        assert mem.read32(BUF_A + 80) == 0xFFFFFFFF

    def test_null_pointer_no_crash(self, env):
        cpu, mem, stubs = env
        com_call(stubs, cpu, mem, "dinput.dll", "Dev::GetDeviceInfo", DEV_OBJ, [0])  # must not raise
        assert cpu.regs[EAX] == DI_OK


DEV_FIXED_RETURN_CASES = [
    ("Dev::CreateEffect",              16, DIERR_UNSUPPORTED),
    ("Dev::EnumEffects",               12, DI_OK),
    ("Dev::GetEffectInfo",              8, DIERR_UNSUPPORTED),
    ("Dev::GetForceFeedbackState",      4, DIERR_UNSUPPORTED),
    ("Dev::SendForceFeedbackCommand",   4, DIERR_UNSUPPORTED),
    ("Dev::EnumCreatedEffectObjects",  12, DI_OK),
    ("Dev::Escape",                     4, DIERR_UNSUPPORTED),
    ("Dev::Poll",                       0, DI_OK),
]


class TestDevFixedReturnMethods:
    """Real bug this covers: these 8 slots (CreateEffect..Poll) didn't exist
    at all until now -- DI_DEV_VTABLE was only 18 slots (offsets 0-0x44),
    so calling any of them (Poll in particular, real IDirectInputDevice2
    offset 0x64) read 32 bytes past the vtable's own end and crashed with
    EIP=0 -- confirmed live via an unhandled CPU fault. Poll is the one
    the real game code actually calls (_INPUT_getdevicedata, 0x00a73d40);
    the rest exist so it lands at the right offset relative to vtable start."""

    @pytest.mark.parametrize("name,arg_bytes,expected", DEV_FIXED_RETURN_CASES)
    def test_returns_expected_code(self, env, name, arg_bytes, expected):
        cpu, mem, stubs = env
        args = [0] * (arg_bytes // 4)
        com_call(stubs, cpu, mem, "dinput.dll", name, DEV_OBJ, args)
        assert cpu.regs[EAX] == expected

    def test_none_of_them_halt(self, env):
        cpu, mem, stubs = env
        for name, arg_bytes, _ in DEV_FIXED_RETURN_CASES:
            args = [0] * (arg_bytes // 4)
            com_call(stubs, cpu, mem, "dinput.dll", name, DEV_OBJ, args)
            assert cpu.halted is False, f"{name} halted"


# ── Setup smoke test ────────────────────────────────────────────────────────────

class TestVtableSetup:

    def test_di_obj_points_to_di_vtable(self, env):
        cpu, mem, stubs = env
        assert mem.read32(DI_OBJ) == DI_VTABLE

    def test_dev_vtable_spans_26_real_slots(self, env):
        # Regression guard for the exact bug this session found: a vtable
        # short by even one trailing slot means whatever real game code
        # calls that offset reads into unrelated memory instead of a real
        # function pointer -- confirmed live as an EIP=0 unhandled CPU
        # fault (real IDirectInputDevice2::Poll, offset 0x64). This test
        # env's get_handler_address fake always returns 0 (see module
        # docstring), so it can't check the *written* vtable bytes -- what
        # it can check is that every one of the 26 real slot names is
        # actually registered as a callable handler (a genuinely missing
        # slot raises KeyError from stubs.get, same as it would from a
        # real unresolved trampoline).
        cpu, mem, stubs = env
        real_slot_names = [
            "Dev::QueryInterface", "Dev::AddRef", "Dev::Release",
            "Dev::GetCapabilities", "Dev::EnumObjects", "Dev::GetProperty",
            "Dev::SetProperty", "Dev::Acquire", "Dev::Unacquire",
            "Dev::GetDeviceState", "Dev::GetDeviceData", "Dev::SetDataFormat",
            "Dev::SetEventNotification", "Dev::SetCooperativeLevel",
            "Dev::GetObjectInfo", "Dev::GetDeviceInfo", "Dev::RunControlPanel",
            "Dev::Initialize", "Dev::CreateEffect", "Dev::EnumEffects",
            "Dev::GetEffectInfo", "Dev::GetForceFeedbackState",
            "Dev::SendForceFeedbackCommand", "Dev::EnumCreatedEffectObjects",
            "Dev::Escape", "Dev::Poll",
        ]
        assert len(real_slot_names) == 26
        for name in real_slot_names:
            stubs.get("dinput.dll", name)  # raises KeyError if missing
