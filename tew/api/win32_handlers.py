"""Win32 API stub handler infrastructure.

Instead of executing real DLL code, intercepts IAT calls and dispatches them
to Python callbacks via an INT 0xFE trampoline mechanism.

Architecture:
  1. Reserve memory at HANDLER_BASE (0x00200000) for stub trampolines
  2. Each stub writes: INT 0xFE; RET; <INT3 padding>
  3. cpu.on_interrupt dispatches INT 0xFE to _handle_api_int
  4. ImportResolver writes stub addresses into the IAT
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu import ESP

# ── Constants ────────────────────────────────────────────────────────────────

# Stub region: 0x00200000 – 0x002FFFFF (1 MB reserved)
HANDLER_BASE: int = 0x00200000
HANDLER_SIZE: int = 32          # bytes per stub (generous, most need ~7)
MAX_HANDLERS: int = 4096

# INT number used for stubs that need Python logic
STUB_INT: int = 0xFE

# Trampolines used by dialog / DllMain bootstrap sequences
DIALOG_TRAMPOLINE: int = 0x00210000
DLLMAIN_TRAMPOLINE: int = 0x00210010
DLLMAIN_HANDLE_STORE: int = 0x00210018

# ── Types ────────────────────────────────────────────────────────────────────

ApiHandler = Callable[["CPU"], None]


@dataclass
class HandlerEntry:
    name: str        # e.g. "kernel32.dll!GetVersion"
    dll_name: str    # e.g. "kernel32.dll"
    func_name: str   # e.g. "GetVersion"
    address: int     # address of the stub trampoline in memory
    handler_id: int  # index for INT 0xFE dispatch
    handler: ApiHandler


# ── Timer type ───────────────────────────────────────────────────────────────

_TIME_CALLBACK_EVENT_SET   = 0x10
_TIME_CALLBACK_EVENT_PULSE = 0x20


@dataclass
class PendingTimer:
    id: int
    due_at: float    # absolute virtual-ms timestamp when callback fires
    period_ms: int   # 0 = one-shot, >0 = periodic interval
    cb_addr: int     # TIMECALLBACK address OR Win32 event handle (see fu_event)
    dw_user: int     # passed through as arg 3
    fu_event: int = 0  # fuEvent flags from timeSetEvent


# Module-level timer table: id → PendingTimer
pending_timers: dict[int, PendingTimer] = {}

# ── Helper ───────────────────────────────────────────────────────────────────


def cleanup_stdcall(cpu: "CPU", memory: "Memory", arg_bytes: int) -> None:
    """For stdcall: move return address past args so the RET skips them."""
    ret_addr = memory.read32(cpu.regs[ESP] & 0xFFFFFFFF)
    cpu.regs[ESP] = (cpu.regs[ESP] + arg_bytes) & 0xFFFFFFFF
    memory.write32(cpu.regs[ESP], ret_addr)


# ── Win32Handlers ─────────────────────────────────────────────────────────────


class Win32Handlers:
    """Manages Win32 API stub trampolines and INT 0xFE dispatch."""

    def __init__(self, memory: "Memory") -> None:
        self._handlers: dict[str, HandlerEntry] = {}          # "dllname!funcName" → entry
        self._handlers_by_id: list[HandlerEntry] = []
        self._patched_addrs: dict[int, HandlerEntry] = {}     # patched code address → entry
        self._next_handler_addr: int = HANDLER_BASE
        self._memory: "Memory" = memory
        self._installed: bool = False
        self._call_log: list[str] = []
        self._call_log_size: int = 2000

    # ── Registration ─────────────────────────────────────────────────────────

    def register_handler(self, dll_name: str, func_name: str, handler: ApiHandler) -> None:
        """Register a stub for a Win32 API function.

        The handler receives the CPU and should set EAX (and optionally write
        to memory via pointers in registers/stack) then return. The stub
        trampoline handles the RET.
        """
        key = f"{dll_name.lower()}!{func_name}"
        if key in self._handlers:
            return  # already registered

        handler_id = len(self._handlers_by_id)
        address = self._next_handler_addr
        self._next_handler_addr += HANDLER_SIZE

        if handler_id >= MAX_HANDLERS:
            raise RuntimeError(f"Too many Win32 stubs (max {MAX_HANDLERS})")

        entry = HandlerEntry(
            name=key,
            dll_name=dll_name.lower(),
            func_name=func_name,
            address=address,
            handler_id=handler_id,
            handler=handler,
        )

        self._handlers[key] = entry
        self._handlers_by_id.append(entry)

        # Write stub machine code into memory:
        #   INT 0xFE  → CD FE   (triggers Python handler via on_interrupt)
        #   RET       → C3      (return to caller)
        #   INT3 (CC) padding for safety
        offset = address
        self._memory.write8(offset, 0xCD)       # INT
        offset += 1
        self._memory.write8(offset, STUB_INT)   # 0xFE
        offset += 1
        self._memory.write8(offset, 0xC3)       # RET
        offset += 1
        while offset < address + HANDLER_SIZE:
            self._memory.write8(offset, 0xCC)   # INT3
            offset += 1

    def patch_address(self, addr: int, name: str, handler: ApiHandler) -> None:
        """Patch a specific address in loaded code to redirect to a Python handler.

        Overwrites the first 3 bytes at ``addr`` with INT 0xFE; RET.
        Use this for internal functions not called through the IAT
        (e.g. CRT internal functions like _sbh_heap_init).
        Must be called AFTER sections are loaded into memory.
        """
        handler_id = len(self._handlers_by_id)
        entry = HandlerEntry(
            name=f"patch:{name}",
            dll_name="patch",
            func_name=name,
            address=addr,
            handler_id=handler_id,
            handler=handler,
        )

        self._handlers_by_id.append(entry)
        self._patched_addrs[addr] = entry

        # Overwrite code at addr with: INT 0xFE; RET
        self._memory.write8(addr, 0xCD)           # INT
        self._memory.write8(addr + 1, STUB_INT)   # 0xFE
        self._memory.write8(addr + 2, 0xC3)       # RET

        from tew.logger import logger
        logger.debug("handlers", f"[Win32Handlers] Patched 0x{addr:x} => {name}")

    # ── Lookup helpers ────────────────────────────────────────────────────────

    def get_handler_address(self, dll_name: str, func_name: str) -> int | None:
        """Return the stub address for a function, or None if not stubbed."""
        key = f"{dll_name.lower()}!{func_name}"
        entry = self._handlers.get(key)
        return entry.address if entry is not None else None

    def lookup_handler_address(self, dll_name: str, func_name: str) -> int:
        """Return the trampoline address or 0 if not registered."""
        key = f"{dll_name.lower()}!{func_name}"
        entry = self._handlers.get(key)
        return entry.address if entry is not None else 0

    def has_handler(self, dll_name: str, func_name: str) -> bool:
        """Return True if the function is stubbed."""
        return f"{dll_name.lower()}!{func_name}" in self._handlers

    def find_handler_by_func_name(self, func_name: str) -> HandlerEntry | None:
        """Find a stub entry by function name across all DLL registrations."""
        for entry in self._handlers_by_id:
            if entry.func_name == func_name:
                return entry
        return None

    def get_stub_dll_handle(self, dll_name: str) -> int | None:
        """Return a stable handle for a stub-only DLL, or None if not registered.

        For DLLs implemented entirely by handler stubs (kernel32, user32, etc.)
        there is no real LoadedDLL entry in the DLL loader.  The address of the
        first registered handler for the DLL is a stable non-NULL value in our
        memory space, so it works as a module handle that satisfies pointer
        comparisons and NULL checks in the game.
        """
        norm = dll_name.lower()
        if not norm.endswith(".dll"):
            norm += ".dll"
        for entry in self._handlers_by_id:
            if entry.dll_name == norm:
                return entry.address
        return None

    def get_dll_name_for_stub_handle(self, handle: int) -> str | None:
        """Reverse of get_stub_dll_handle: given a stub-region address, return the DLL name.

        GetModuleHandleA returns the first stub address for a DLL (i.e. get_stub_dll_handle).
        GetProcAddress then passes that address back as hModule.  This method resolves it.
        """
        for entry in self._handlers_by_id:
            if entry.address == handle:
                return entry.dll_name
        return None

    def get_registered_handlers(self) -> list[dict]:
        """Return all registered stubs (for diagnostics)."""
        return [
            {"dll_name": e.dll_name, "func_name": e.func_name, "address": e.address}
            for e in self._handlers_by_id
        ]

    def get_call_log(self) -> list[str]:
        """Return a copy of the recent stub call log."""
        return list(self._call_log)

    @property
    def count(self) -> int:
        """Total number of registered stubs."""
        return len(self._handlers_by_id)

    # ── Installation ──────────────────────────────────────────────────────────

    def install(self, cpu: "CPU") -> None:
        """Install the INT 0xFE handler on the CPU.

        Must be called after all stubs are registered.
        """
        if self._installed:
            return
        self._installed = True

        # Capture the existing interrupt handler (if any) so we can delegate
        # unrecognised interrupt numbers to it.
        existing_handler = cpu._int_handler

        stubs = self

        def _dispatch(int_num: int, c: "CPU") -> None:
            if int_num == STUB_INT:
                stubs._handle_api_int(c)
                return
            if int_num == 3:
                # INT3 debug breakpoint — halt so the run loop dumps state
                from tew.logger import logger
                logger.warn(
                    "handlers",
                    f"INT3 breakpoint at EIP=0x{(c.eip & 0xFFFFFFFF):08x} — halting",
                )
                c.halted = True
                return
            # Delegate to whatever was installed before us
            if existing_handler is not None:
                existing_handler(int_num, c)
            else:
                raise RuntimeError(
                    f"Unhandled interrupt INT 0x{int_num:02x} at EIP=0x{(c.eip & 0xFFFFFFFF):08x}"
                )

        cpu.on_interrupt(_dispatch)

    # ── INT 0xFE dispatch ─────────────────────────────────────────────────────

    def _handle_api_int(self, cpu: "CPU") -> None:
        """Handle INT 0xFE — find which stub was called and execute its handler.

        EIP is now pointing past the INT 0xFE instruction (at the RET).
        The stub address is EIP - 2 (INT 0xFE is 2 bytes).
        """
        handler_addr = (cpu.eip - 2) & 0xFFFFFFFF

        # Check patched addresses first (O(1)), then fall back to linear scan
        entry = self._patched_addrs.get(handler_addr)
        if entry is None:
            for candidate in self._handlers_by_id:
                if candidate.address == handler_addr:
                    entry = candidate
                    break

        if entry is None:
            raise RuntimeError(f"Unknown Win32 stub at 0x{handler_addr:08x}")

        # Log the stub call; deduplicate consecutive identical calls with a counter
        log_entry = f"{entry.name} @ 0x{handler_addr:x}"
        if self._call_log and self._call_log[-1].startswith(log_entry):
            last = self._call_log[-1]
            count_match = re.search(r" x(\d+)$", last)
            count = (int(count_match.group(1)) + 1) if count_match else 2
            self._call_log[-1] = f"{log_entry} x{count}"
        else:
            self._call_log.append(log_entry)
            if len(self._call_log) > self._call_log_size:
                self._call_log.pop(0)

        # Execute the Python handler
        # EIP already points at RET, so the CPU will execute RET next
        entry.handler(cpu)
