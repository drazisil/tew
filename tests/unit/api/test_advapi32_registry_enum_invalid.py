"""Invalid-input tests for RegEnumKeyExA and RegEnumValueA.

These verify that bad arguments fail cleanly with an error code rather than
crashing, reading from address 0, or silently writing garbage.
"""
from __future__ import annotations

import pytest

from tew.api._state import CRTState, RegistryEntry
from tew.api.advapi32_handlers import register_advapi32_handlers, _reg_key_names
from tew.hardware.memory import Memory
from tew.hardware.cpu import EAX, ESP


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


MEM_SIZE = 8 * 1024 * 1024
STACK    = 0x200000
NAME_BUF = 0x201000
SIZE_PTR = 0x202000
TYPE_PTR = 0x203000
DATA_BUF = 0x204000
DATA_SZ  = 0x205000

ERROR_SUCCESS        = 0
ERROR_MORE_DATA      = 234
ERROR_NO_MORE_ITEMS  = 259
ERROR_INVALID_HANDLE = 6


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_advapi32_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    return cpu, mem, state, stubs


@pytest.fixture
def populated(env):
    cpu, mem, state, stubs = env
    _reg_key_names[0xBEEF4000] = "hklm\\testinvalid"
    # Key with both a value (for RegEnumValueA) and a child key (for RegEnumKeyExA)
    state.registry_values["hklm\\testinvalid"] = {
        "myval": RegistryEntry(type=1, value="data"),
    }
    state.registry_values["hklm\\testinvalid\\child"] = {}
    return cpu, mem, state, stubs


def enum_key_call(stubs, cpu, mem, h_key, index, lp_name, lpch_name):
    mem.write32(STACK + 4, h_key)
    mem.write32(STACK + 8, index)
    mem.write32(STACK + 12, lp_name)
    mem.write32(STACK + 16, lpch_name)
    mem.write32(STACK + 20, 0)
    mem.write32(STACK + 24, 0)
    mem.write32(STACK + 28, 0)
    mem.write32(STACK + 32, 0)
    stubs.get("advapi32.dll", "RegEnumKeyExA")(cpu)
    return cpu.regs[EAX]


def enum_val_call(stubs, cpu, mem, h_key, index, lp_vname, lpch_vname,
                  lp_type=0, lp_data=0, lpcb_data=0):
    mem.write32(STACK + 4, h_key)
    mem.write32(STACK + 8, index)
    mem.write32(STACK + 12, lp_vname)
    mem.write32(STACK + 16, lpch_vname)
    mem.write32(STACK + 20, 0)
    mem.write32(STACK + 24, lp_type)
    mem.write32(STACK + 28, lp_data)
    mem.write32(STACK + 32, lpcb_data)
    stubs.get("advapi32.dll", "RegEnumValueA")(cpu)
    return cpu.regs[EAX]


# ── RegEnumKeyExA invalid inputs ──────────────────────────────────────────────

class TestRegEnumKeyExAInvalid:

    def test_null_size_pointer_does_not_crash(self, populated):
        """lpch_name=0 means we cannot check buffer size — must not read address 0."""
        cpu, mem, state, stubs = populated
        result = enum_key_call(stubs, cpu, mem, 0xBEEF4000, 0,
                               lp_name=NAME_BUF, lpch_name=0)
        assert result != ERROR_SUCCESS  # must not pretend it worked

    def test_null_size_pointer_returns_error_code(self, populated):
        cpu, mem, state, stubs = populated
        result = enum_key_call(stubs, cpu, mem, 0xBEEF4000, 0,
                               lp_name=NAME_BUF, lpch_name=0)
        assert result in (ERROR_MORE_DATA, ERROR_INVALID_HANDLE)

    def test_null_name_buffer_with_valid_size_ptr_does_not_crash(self, populated):
        """lp_name=0 with valid lpch_name: caller is probing the required size."""
        cpu, mem, state, stubs = populated
        mem.write32(SIZE_PTR, 256)
        result = enum_key_call(stubs, cpu, mem, 0xBEEF4000, 0,
                               lp_name=0, lpch_name=SIZE_PTR)
        assert result == ERROR_SUCCESS

    def test_null_name_buffer_updates_size(self, populated):
        cpu, mem, state, stubs = populated
        mem.write32(SIZE_PTR, 256)
        enum_key_call(stubs, cpu, mem, 0xBEEF4000, 0,
                      lp_name=0, lpch_name=SIZE_PTR)
        # lpcchName should be updated to the length of the key name
        assert mem.read32(SIZE_PTR) > 0

    def test_unknown_handle_does_not_crash(self, env):
        """A handle not in _reg_key_names must not crash or read wild memory."""
        cpu, mem, state, stubs = env
        mem.write32(SIZE_PTR, 256)
        result = enum_key_call(stubs, cpu, mem, 0xDEADBEEF, 0,
                               lp_name=NAME_BUF, lpch_name=SIZE_PTR)
        assert isinstance(result, int)

    def test_unknown_handle_returns_error_or_no_more_items(self, env):
        cpu, mem, state, stubs = env
        mem.write32(SIZE_PTR, 256)
        result = enum_key_call(stubs, cpu, mem, 0xDEADBEEF, 0,
                               lp_name=NAME_BUF, lpch_name=SIZE_PTR)
        assert result in (ERROR_NO_MORE_ITEMS, ERROR_INVALID_HANDLE)


# ── RegEnumValueA invalid inputs ──────────────────────────────────────────────

class TestRegEnumValueAInvalid:

    def test_null_size_pointer_does_not_crash(self, populated):
        """lpch_value_name=0 means we cannot safely write the name."""
        cpu, mem, state, stubs = populated
        result = enum_val_call(stubs, cpu, mem, 0xBEEF4000, 0,
                               lp_vname=NAME_BUF, lpch_vname=0)
        assert result != ERROR_SUCCESS

    def test_null_size_pointer_returns_error_code(self, populated):
        cpu, mem, state, stubs = populated
        result = enum_val_call(stubs, cpu, mem, 0xBEEF4000, 0,
                               lp_vname=NAME_BUF, lpch_vname=0)
        assert result in (ERROR_MORE_DATA, ERROR_INVALID_HANDLE)

    def test_null_type_pointer_does_not_crash(self, populated):
        """lp_type=0 is valid — caller doesn't want the type."""
        cpu, mem, state, stubs = populated
        mem.write32(SIZE_PTR, 256)
        mem.write32(DATA_SZ, 256)
        result = enum_val_call(stubs, cpu, mem, 0xBEEF4000, 0,
                               lp_vname=NAME_BUF, lpch_vname=SIZE_PTR,
                               lp_type=0, lp_data=DATA_BUF, lpcb_data=DATA_SZ)
        assert result == ERROR_SUCCESS

    def test_null_data_pointer_with_valid_size_does_not_crash(self, populated):
        """lp_data=0 with valid lpcb_data: caller probing required data size."""
        cpu, mem, state, stubs = populated
        mem.write32(SIZE_PTR, 256)
        mem.write32(DATA_SZ, 256)
        result = enum_val_call(stubs, cpu, mem, 0xBEEF4000, 0,
                               lp_vname=NAME_BUF, lpch_vname=SIZE_PTR,
                               lp_type=TYPE_PTR, lp_data=0, lpcb_data=DATA_SZ)
        assert result == ERROR_SUCCESS

    def test_null_data_pointer_still_updates_size(self, populated):
        cpu, mem, state, stubs = populated
        mem.write32(SIZE_PTR, 256)
        mem.write32(DATA_SZ, 256)
        enum_val_call(stubs, cpu, mem, 0xBEEF4000, 0,
                      lp_vname=NAME_BUF, lpch_vname=SIZE_PTR,
                      lp_type=TYPE_PTR, lp_data=0, lpcb_data=DATA_SZ)
        assert mem.read32(DATA_SZ) > 0  # required size was written

    def test_null_size_and_data_pointers_does_not_crash(self, populated):
        """Both lp_data and lpcb_data null: no data output at all."""
        cpu, mem, state, stubs = populated
        mem.write32(SIZE_PTR, 256)
        result = enum_val_call(stubs, cpu, mem, 0xBEEF4000, 0,
                               lp_vname=NAME_BUF, lpch_vname=SIZE_PTR,
                               lp_type=TYPE_PTR, lp_data=0, lpcb_data=0)
        assert result == ERROR_SUCCESS

    def test_unknown_handle_returns_no_more_items_or_error(self, env):
        cpu, mem, state, stubs = env
        mem.write32(SIZE_PTR, 256)
        mem.write32(DATA_SZ, 256)
        result = enum_val_call(stubs, cpu, mem, 0xDEADBEEF, 0,
                               lp_vname=NAME_BUF, lpch_vname=SIZE_PTR,
                               lp_type=TYPE_PTR, lp_data=DATA_BUF, lpcb_data=DATA_SZ)
        assert result in (ERROR_NO_MORE_ITEMS, ERROR_INVALID_HANDLE)
