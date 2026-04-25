"""Tests for RegEnumKeyExA and RegEnumValueA — both previously returned NO_MORE_ITEMS unconditionally."""
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


MEM_SIZE   = 8 * 1024 * 1024
STACK      = 0x200000
NAME_BUF   = 0x201000
SIZE_PTR   = 0x202000
TYPE_PTR   = 0x203000
DATA_BUF   = 0x204000
DATA_SZ    = 0x205000

ERROR_SUCCESS       = 0
ERROR_MORE_DATA     = 234
ERROR_NO_MORE_ITEMS = 259


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_advapi32_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)  # return address
    return cpu, mem, state, stubs


def read_ansi(mem, addr, max_len=256) -> str:
    result = []
    for i in range(max_len):
        c = mem.read8(addr + i)
        if c == 0:
            break
        result.append(chr(c))
    return "".join(result)


# ── RegEnumKeyExA ─────────────────────────────────────────────────────────────

class TestRegEnumKeyExAEmptyRegistry:
    def test_returns_no_more_items_when_no_subkeys(self, env):
        cpu, mem, state, stubs = env
        h_key = 0x80000002  # HKLM — no entries in registry_values
        mem.write32(STACK + 4, h_key)
        mem.write32(STACK + 8, 0)             # dwIndex = 0
        mem.write32(STACK + 12, NAME_BUF)
        mem.write32(STACK + 16, SIZE_PTR)
        mem.write32(SIZE_PTR, 256)
        mem.write32(STACK + 20, 0)            # lpReserved
        mem.write32(STACK + 24, 0)            # lpClass
        mem.write32(STACK + 28, 0)            # lpcchClass
        mem.write32(STACK + 32, 0)            # lpftLastWriteTime
        stubs.get("advapi32.dll", "RegEnumKeyExA")(cpu)
        assert cpu.regs[EAX] == ERROR_NO_MORE_ITEMS


class TestRegEnumKeyExAWithSubkeys:
    @pytest.fixture
    def populated(self, env):
        cpu, mem, state, stubs = env
        # Populate registry with two direct children of hklm
        state.registry_values["hklm\\software\\ea"] = {"version": RegistryEntry(type=4, value=1)}
        state.registry_values["hklm\\software\\mco"] = {"server": RegistryEntry(type=1, value="localhost")}
        # Also a deeper key that should NOT appear as direct child of hklm\\software
        state.registry_values["hklm\\software\\ea\\settings"] = {}
        # Register a handle for hklm\\software
        _reg_key_names[0xBEEF1000] = "hklm\\software"
        return cpu, mem, state, stubs

    def test_first_subkey_returned(self, populated):
        cpu, mem, state, stubs = populated
        mem.write32(STACK + 4, 0xBEEF1000)
        mem.write32(STACK + 8, 0)  # index 0
        mem.write32(STACK + 12, NAME_BUF)
        mem.write32(STACK + 16, SIZE_PTR)
        mem.write32(SIZE_PTR, 256)
        mem.write32(STACK + 20, 0)
        mem.write32(STACK + 24, 0)
        mem.write32(STACK + 28, 0)
        mem.write32(STACK + 32, 0)
        stubs.get("advapi32.dll", "RegEnumKeyExA")(cpu)
        assert cpu.regs[EAX] == ERROR_SUCCESS
        name = read_ansi(mem, NAME_BUF)
        assert name == "ea"  # sorted: ea < mco

    def test_second_subkey_returned(self, populated):
        cpu, mem, state, stubs = populated
        mem.write32(STACK + 4, 0xBEEF1000)
        mem.write32(STACK + 8, 1)  # index 1
        mem.write32(STACK + 12, NAME_BUF)
        mem.write32(STACK + 16, SIZE_PTR)
        mem.write32(SIZE_PTR, 256)
        mem.write32(STACK + 20, 0)
        mem.write32(STACK + 24, 0)
        mem.write32(STACK + 28, 0)
        mem.write32(STACK + 32, 0)
        stubs.get("advapi32.dll", "RegEnumKeyExA")(cpu)
        assert cpu.regs[EAX] == ERROR_SUCCESS
        name = read_ansi(mem, NAME_BUF)
        assert name == "mco"

    def test_index_past_end_returns_no_more_items(self, populated):
        cpu, mem, state, stubs = populated
        mem.write32(STACK + 4, 0xBEEF1000)
        mem.write32(STACK + 8, 2)  # index 2, only 2 direct children
        mem.write32(STACK + 12, NAME_BUF)
        mem.write32(STACK + 16, SIZE_PTR)
        mem.write32(SIZE_PTR, 256)
        mem.write32(STACK + 20, 0)
        mem.write32(STACK + 24, 0)
        mem.write32(STACK + 28, 0)
        mem.write32(STACK + 32, 0)
        stubs.get("advapi32.dll", "RegEnumKeyExA")(cpu)
        assert cpu.regs[EAX] == ERROR_NO_MORE_ITEMS

    def test_deeper_key_not_treated_as_direct_child(self, populated):
        cpu, mem, state, stubs = populated
        results = []
        for i in range(20):
            mem.write32(STACK + 4, 0xBEEF1000)
            mem.write32(STACK + 8, i)
            mem.write32(STACK + 12, NAME_BUF)
            mem.write32(STACK + 16, SIZE_PTR)
            mem.write32(SIZE_PTR, 256)
            mem.write32(STACK + 20, 0)
            mem.write32(STACK + 24, 0)
            mem.write32(STACK + 28, 0)
            mem.write32(STACK + 32, 0)
            stubs.get("advapi32.dll", "RegEnumKeyExA")(cpu)
            if cpu.regs[EAX] == ERROR_NO_MORE_ITEMS:
                break
            results.append(read_ansi(mem, NAME_BUF))
        assert "settings" not in results

    def test_buffer_too_small_returns_more_data(self, populated):
        cpu, mem, state, stubs = populated
        mem.write32(STACK + 4, 0xBEEF1000)
        mem.write32(STACK + 8, 0)
        mem.write32(STACK + 12, NAME_BUF)
        mem.write32(STACK + 16, SIZE_PTR)
        mem.write32(SIZE_PTR, 1)  # too small for "ea\0" (needs 3)
        mem.write32(STACK + 20, 0)
        mem.write32(STACK + 24, 0)
        mem.write32(STACK + 28, 0)
        mem.write32(STACK + 32, 0)
        stubs.get("advapi32.dll", "RegEnumKeyExA")(cpu)
        assert cpu.regs[EAX] == ERROR_MORE_DATA


# ── RegEnumValueA ─────────────────────────────────────────────────────────────

class TestRegEnumValueAEmptyKey:
    def test_returns_no_more_items_for_key_with_no_values(self, env):
        cpu, mem, state, stubs = env
        _reg_key_names[0xBEEF2000] = "hklm\\emptykey"
        state.registry_values["hklm\\emptykey"] = {}
        mem.write32(STACK + 4, 0xBEEF2000)
        mem.write32(STACK + 8, 0)
        mem.write32(STACK + 12, NAME_BUF)
        mem.write32(STACK + 16, SIZE_PTR)
        mem.write32(SIZE_PTR, 256)
        mem.write32(STACK + 20, 0)
        mem.write32(STACK + 24, TYPE_PTR)
        mem.write32(STACK + 28, DATA_BUF)
        mem.write32(STACK + 32, DATA_SZ)
        mem.write32(DATA_SZ, 256)
        stubs.get("advapi32.dll", "RegEnumValueA")(cpu)
        assert cpu.regs[EAX] == ERROR_NO_MORE_ITEMS


class TestRegEnumValueAWithValues:
    @pytest.fixture
    def val_env(self, env):
        cpu, mem, state, stubs = env
        _reg_key_names[0xBEEF3000] = "hklm\\testkey"
        state.registry_values["hklm\\testkey"] = {
            "alpha": RegistryEntry(type=1, value="hello"),
            "beta":  RegistryEntry(type=4, value=42),
        }
        return cpu, mem, state, stubs

    def _enum_call(self, stubs, cpu, mem, handle, index):
        mem.write32(STACK + 4, handle)
        mem.write32(STACK + 8, index)
        mem.write32(STACK + 12, NAME_BUF)
        mem.write32(STACK + 16, SIZE_PTR)
        mem.write32(SIZE_PTR, 256)
        mem.write32(STACK + 20, 0)
        mem.write32(STACK + 24, TYPE_PTR)
        mem.write32(STACK + 28, DATA_BUF)
        mem.write32(STACK + 32, DATA_SZ)
        mem.write32(DATA_SZ, 256)
        stubs.get("advapi32.dll", "RegEnumValueA")(cpu)

    def test_first_value_name_is_alpha(self, val_env):
        cpu, mem, state, stubs = val_env
        self._enum_call(stubs, cpu, mem, 0xBEEF3000, 0)
        assert cpu.regs[EAX] == ERROR_SUCCESS
        assert read_ansi(mem, NAME_BUF) == "alpha"

    def test_second_value_name_is_beta(self, val_env):
        cpu, mem, state, stubs = val_env
        self._enum_call(stubs, cpu, mem, 0xBEEF3000, 1)
        assert cpu.regs[EAX] == ERROR_SUCCESS
        assert read_ansi(mem, NAME_BUF) == "beta"

    def test_reg_sz_type_written(self, val_env):
        cpu, mem, state, stubs = val_env
        self._enum_call(stubs, cpu, mem, 0xBEEF3000, 0)  # alpha = REG_SZ
        assert mem.read32(TYPE_PTR) == 1

    def test_reg_dword_type_written(self, val_env):
        cpu, mem, state, stubs = val_env
        self._enum_call(stubs, cpu, mem, 0xBEEF3000, 1)  # beta = REG_DWORD
        assert mem.read32(TYPE_PTR) == 4

    def test_reg_sz_data_written(self, val_env):
        cpu, mem, state, stubs = val_env
        self._enum_call(stubs, cpu, mem, 0xBEEF3000, 0)  # alpha = "hello"
        assert read_ansi(mem, DATA_BUF) == "hello"

    def test_reg_dword_data_written(self, val_env):
        cpu, mem, state, stubs = val_env
        self._enum_call(stubs, cpu, mem, 0xBEEF3000, 1)  # beta = 42
        assert mem.read32(DATA_BUF) == 42

    def test_index_past_end_returns_no_more_items(self, val_env):
        cpu, mem, state, stubs = val_env
        self._enum_call(stubs, cpu, mem, 0xBEEF3000, 2)
        assert cpu.regs[EAX] == ERROR_NO_MORE_ITEMS

    def test_name_buffer_too_small_returns_more_data(self, val_env):
        cpu, mem, state, stubs = val_env
        mem.write32(STACK + 4, 0xBEEF3000)
        mem.write32(STACK + 8, 0)  # alpha
        mem.write32(STACK + 12, NAME_BUF)
        mem.write32(STACK + 16, SIZE_PTR)
        mem.write32(SIZE_PTR, 1)   # too small for "alpha\0"
        mem.write32(STACK + 20, 0)
        mem.write32(STACK + 24, TYPE_PTR)
        mem.write32(STACK + 28, DATA_BUF)
        mem.write32(STACK + 32, DATA_SZ)
        mem.write32(DATA_SZ, 256)
        stubs.get("advapi32.dll", "RegEnumValueA")(cpu)
        assert cpu.regs[EAX] == ERROR_MORE_DATA
