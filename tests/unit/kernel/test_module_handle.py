"""
Tests for GetModuleHandleA / GetModuleHandleW resolution logic, and for the
Win32Handlers.get_stub_dll_handle helper that backs it.

Strategy: Win32Handlers is light to construct (only needs a Memory), so we
exercise the pure lookup helpers without a full CPU emulation loop.
"""

import pytest

from tew.hardware.memory import Memory
from tew.api.win32_handlers import Win32Handlers


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

MEM_SIZE = 4 * 1024 * 1024   # 4 MB is plenty for trampoline region


@pytest.fixture
def mem() -> Memory:
    return Memory(MEM_SIZE)


@pytest.fixture
def handlers(mem: Memory) -> Win32Handlers:
    h = Win32Handlers(mem)
    # Register a handful of handlers across two DLLs so we can test
    # get_stub_dll_handle without needing the full registration pipeline.
    h.register_handler("kernel32.dll", "GetVersion", lambda cpu: None)
    h.register_handler("kernel32.dll", "GetLastError", lambda cpu: None)
    h.register_handler("user32.dll",   "MessageBoxA",  lambda cpu: None)
    return h


# ─────────────────────────────────────────────────────────────────────────────
# Win32Handlers.get_stub_dll_handle
# ─────────────────────────────────────────────────────────────────────────────

class TestGetStubDllHandle:

    def test_returns_nonzero_for_registered_dll(self, handlers: Win32Handlers) -> None:
        handle = handlers.get_stub_dll_handle("kernel32.dll")
        assert handle is not None
        assert handle != 0

    def test_handle_is_in_trampoline_region(self, handlers: Win32Handlers) -> None:
        """The returned address must be within the handler trampoline region."""
        from tew.api.win32_handlers import HANDLER_BASE, HANDLER_SIZE, MAX_HANDLERS
        handle = handlers.get_stub_dll_handle("kernel32.dll")
        assert HANDLER_BASE <= handle < HANDLER_BASE + MAX_HANDLERS * HANDLER_SIZE

    def test_returns_first_registered_handler_address(self, handlers: Win32Handlers) -> None:
        """Handle should be the address of the first registered handler for that DLL."""
        kernel32_entries = [
            e for e in handlers._handlers_by_id if e.dll_name == "kernel32.dll"
        ]
        first_addr = kernel32_entries[0].address
        assert handlers.get_stub_dll_handle("kernel32.dll") == first_addr

    def test_case_insensitive_name(self, handlers: Win32Handlers) -> None:
        """DLL names are case-insensitive on Windows."""
        lower = handlers.get_stub_dll_handle("kernel32.dll")
        upper = handlers.get_stub_dll_handle("KERNEL32.DLL")
        mixed = handlers.get_stub_dll_handle("Kernel32.DLL")
        assert lower == upper == mixed

    def test_no_suffix_normalised_to_dll(self, handlers: Win32Handlers) -> None:
        """'KERNEL32' (without .dll) should resolve the same as 'kernel32.dll'."""
        with_suffix    = handlers.get_stub_dll_handle("kernel32.dll")
        without_suffix = handlers.get_stub_dll_handle("KERNEL32")
        assert with_suffix == without_suffix

    def test_returns_none_for_unregistered_dll(self, handlers: Win32Handlers) -> None:
        result = handlers.get_stub_dll_handle("ntdll.dll")
        assert result is None

    def test_different_dlls_get_different_handles(self, handlers: Win32Handlers) -> None:
        k32 = handlers.get_stub_dll_handle("kernel32.dll")
        u32 = handlers.get_stub_dll_handle("user32.dll")
        assert k32 is not None
        assert u32 is not None
        assert k32 != u32

    def test_stable_across_calls(self, handlers: Win32Handlers) -> None:
        """Same DLL queried twice must return the same address."""
        first  = handlers.get_stub_dll_handle("kernel32.dll")
        second = handlers.get_stub_dll_handle("kernel32.dll")
        assert first == second


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_module_handle logic (tested via kernel32_handlers internals)
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveModuleHandle:
    """
    These tests verify the module-handle resolution path used by
    _get_module_handle_a / _get_module_handle_w without running a full CPU
    loop.  We call get_stub_dll_handle directly since _resolve_module_handle
    is a closure inside register_kernel32_handlers.
    """

    def test_kernel32_resolves_to_nonzero(self, handlers: Win32Handlers) -> None:
        """KERNEL32 is always present as a stub DLL; must never return 0."""
        handle = handlers.get_stub_dll_handle("KERNEL32")
        assert handle is not None and handle != 0

    def test_user32_resolves_to_nonzero(self, handlers: Win32Handlers) -> None:
        handle = handlers.get_stub_dll_handle("user32.dll")
        assert handle is not None and handle != 0

    def test_completely_unknown_module_returns_none(self, handlers: Win32Handlers) -> None:
        """A module that has no stub handlers and is not a loaded DLL → None."""
        assert handlers.get_stub_dll_handle("nosuchlib.dll") is None
