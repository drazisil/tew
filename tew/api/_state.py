"""Shared mutable state for Win32/CRT handler registrations.

All handlers registered in register_crt_handlers share one CRTState instance.
This replaces the TypeScript closure approach where local variables were shared
between all the registerHandler callbacks.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from tew.hardware.cpu import SavedCPUState
from tew.api.window_manager import WindowManager

if TYPE_CHECKING:
    from tew.hardware.memory import Memory
    from tew.api.pe_resources import PEResources


# ── File handle types ─────────────────────────────────────────────────────────

@dataclass
class FileHandleEntry:
    path: str
    data: bytes          # file contents (empty bytes for write-only)
    position: int        # current read/write position
    writable: bool
    fd: Optional[int]    # host file descriptor (None = no real file backing)


# ── Kernel object types ───────────────────────────────────────────────────────

@dataclass
class MutexHandle:
    type: str = "mutex"
    locked: bool = False
    name: str = ""
    owner_tid: Optional[int] = None   # thread ID holding the mutex; None = unowned
    recursion_count: int = 0          # depth of recursive acquisitions by owner_tid


@dataclass
class EventHandle:
    type: str = "event"
    signaled: bool = False
    manual_reset: bool = False


KernelHandle = MutexHandle | EventHandle


# ── Dynamic module (LoadLibrary result) ──────────────────────────────────────

@dataclass
class DynamicModule:
    dll_name: str
    base_address: int
    dll_path: str = ""   # full Windows-style path when known; empty if unknown


# ── Cooperative thread state ─────────────────────────────────────────────────

@dataclass
class PendingThreadInfo:
    start_address: int
    parameter: int
    handle: int
    thread_id: int
    suspended: bool
    completed: bool
    calls_seen: Optional[set[str]] = None     # dedup set for call logging
    saved_state: Optional[SavedCPUState] = None
    # When non-empty the thread is blocked waiting for one of these handles.
    # The scheduler will skip it until an event in this set is signaled OR
    # the virtual clock reaches wait_deadline_ms.
    waiting_on_handles: Optional[frozenset[int]] = None
    # virtual_ticks_ms value at which a finite-timeout wait expires.
    # None means the wait has no deadline (INFINITE or thread is not waiting).
    wait_deadline_ms: Optional[int] = None
    # Set to True by the scheduler when a finite-timeout wait expires before
    # the handle was signaled.  The retried WaitForSingle/Multiple handler
    # reads this, clears it, and returns WAIT_TIMEOUT (0x102).
    wait_timed_out: bool = False
    # CS address this thread is blocking on (set by _enter_cs, cleared by scheduler).
    # EIP is saved at the EnterCriticalSection stub so the handler retries on resume.
    waiting_on_cs: Optional[int] = None


# ── Registry types ────────────────────────────────────────────────────────────

@dataclass
class RegistryEntry:
    type: int
    value: str | int


RegistryMap = dict[str, dict[str, RegistryEntry]]


# ── Emulator config ───────────────────────────────────────────────────────────

@dataclass
class EmulatorConfig:
    path_mappings: dict[str, str]          # lowercased win prefix → linux prefix
    interactive_on_missing_file: bool


# ── Helper functions (module-level, no shared state) ─────────────────────────

def find_file_ci(linux_path: str) -> Optional[str]:
    """Case-insensitive file lookup for Linux (Windows paths are case-insensitive).
    Returns the real on-disk path if found (any case), or None if not found.
    """
    if os.path.exists(linux_path):
        return linux_path
    dir_path = os.path.dirname(linux_path)
    name_lower = os.path.basename(linux_path).lower()
    try:
        entries = os.listdir(dir_path)
        for entry in entries:
            if entry.lower() == name_lower:
                return os.path.join(dir_path, entry)
    except OSError as e:
        from tew.logger import logger
        logger.debug("fileio", f"find_file_ci: cannot list {dir_path!r}: {e}")
    return None


def load_registry_json() -> RegistryMap:
    """Load fake registry values from registry.json in the project root.
    Keys and value names are normalized to lowercase. Returns empty map on error.
    """
    from tew.logger import logger
    try:
        file_path = os.path.join(os.getcwd(), "registry.json")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        result: RegistryMap = {}
        for key, values in data.items():
            if key.startswith("_"):
                continue
            if not isinstance(values, dict):
                continue
            normalized_key = key.lower().replace("/", "\\")
            result[normalized_key] = {}
            for vname, entry in values.items():
                if isinstance(entry, dict) and "type" in entry and "value" in entry:
                    result[normalized_key][vname.lower()] = RegistryEntry(
                        type=entry["type"], value=entry["value"]
                    )
        logger.info("registry", f"Loaded {len(result)} keys from registry.json")
        return result
    except Exception as e:
        from tew.logger import logger
        logger.warn("registry", f"Could not load registry.json: {e} — using empty registry")
        return {}


def save_registry_json(registry_values: RegistryMap) -> None:
    """Persist current in-memory registry values back to registry.json.

    Preserves any ``_``-prefixed comment/metadata keys that were in the
    original file.  All registry key paths and value names are written in
    the normalised (lowercase) form that load_registry_json expects.
    """
    from tew.logger import logger
    file_path = os.path.join(os.getcwd(), "registry.json")
    existing: dict = {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except Exception:
        pass
    result: dict = {}
    for k, v in existing.items():
        if k.startswith("_"):
            result[k] = v
    for key_path, values in registry_values.items():
        result[key_path] = {
            vname: {"type": entry.type, "value": entry.value}
            for vname, entry in values.items()
        }
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        logger.debug("registry", f"Persisted {len(registry_values)} keys to registry.json")
    except Exception as e:
        logger.warn("registry", f"Could not save registry.json: {e}")


def load_emulator_config() -> EmulatorConfig:
    """Load emulator.json from the project root. Returns safe defaults on error."""
    from tew.logger import logger
    try:
        file_path = os.path.join(os.getcwd(), "emulator.json")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw_mappings = data.get("pathMappings", {})
        path_mappings: dict[str, str] = {}
        for win, linux in raw_mappings.items():
            if win.startswith("_"):
                continue
            path_mappings[win.replace("\\", "/").lower()] = linux
        interactive = data.get("interactiveOnMissingFile") is True
        logger.info("startup", f"[EmulatorConfig] Loaded {len(path_mappings)} path mapping(s) from emulator.json")
        return EmulatorConfig(path_mappings=path_mappings, interactive_on_missing_file=interactive)
    except Exception as e:
        logger.warn("startup", f"[EmulatorConfig] Could not load emulator.json: {e} — using defaults")
        return EmulatorConfig(path_mappings={}, interactive_on_missing_file=False)


# ── Fixed kernel structure addresses ─────────────────────────────────────────

TEB_BASE = 0x00320000   # Thread Environment Block (FS base)
PEB_BASE = 0x00300000   # Process Environment Block (TEB+0x30 points here)

# ── Thread / stack constants ──────────────────────────────────────────────────

THREAD_STACK_BASE = 0x08000000
THREAD_STACK_SIZE = 256 * 1024
THREAD_SENTINEL   = 0x001FE000


# ── CRTState ──────────────────────────────────────────────────────────────────

class CRTState:
    """All shared mutable state for CRT/Win32 handler callbacks."""

    def __init__(self) -> None:
        # ── Emulator config ───────────────────────────────────────────────
        self.config: EmulatorConfig = load_emulator_config()

        # ── Exe path (set by run_exe.py after construction) ───────────────
        # Linux path to the executable being emulated.  Used by the
        # GetModuleFileNameA handler to return the Windows-style exe path.
        self.exe_path: str = ""

        # ── Heap allocator ────────────────────────────────────────────────
        self.next_heap_alloc: int = 0x04000000
        self.heap_alloc_sizes: dict[int, int] = {}   # addr → user size
        self.heap_alloc_owner: dict[int, int] = {}   # addr → heap handle
        self.heap_handles: set[int] = set()
        self.next_heap_handle: int = 0x9000
        # Pre-register process heap
        self.process_heap: int = self.next_heap_handle
        self.next_heap_handle += 1
        self.heap_handles.add(self.process_heap)

        # ── VirtualAlloc ──────────────────────────────────────────────────
        self.next_virtual_alloc: int = 0x40000000
        self.virtual_reserved: dict[int, int] = {}   # addr → size
        self.virtual_committed: dict[int, int] = {}  # addr → size

        # ── File handles ──────────────────────────────────────────────────
        self.file_handle_map: dict[int, FileHandleEntry] = {}
        self.next_file_handle: int = 0x5000

        # ── Kernel objects ────────────────────────────────────────────────
        self.kernel_handle_map: dict[int, KernelHandle] = {}
        self.next_kernel_handle: int = 0x7000

        # ── Dynamic modules ───────────────────────────────────────────────
        self.dynamic_modules: dict[int, DynamicModule] = {}   # handle → module

        # ── Cooperative threads ───────────────────────────────────────────
        self.pending_threads: list[PendingThreadInfo] = []
        self.next_thread_id: int = 1001
        self.next_thread_handle: int = 0x0000BEEF
        self.thread_stack_next: int = THREAD_STACK_BASE
        self.sleep_count: int = 0
        self.virtual_ticks_ms: int = 0   # virtual clock: advanced by dwMilliseconds per Sleep/SleepEx
        self.is_running_thread: bool = False
        self.current_thread_idx: int = -1
        self.last_scheduled_idx: int = -1

        # ── Thread yield-without-death flag ──────────────────────────────
        # Set by wait handlers (WaitForSingle/Multiple) when a background
        # thread must block: _run_thread_slice saves state instead of
        # marking the thread dead, then clears this flag.
        self.thread_yield_requested: bool = False

        # ── TLS ───────────────────────────────────────────────────────────
        self.tls_slots: set[int] = set()
        self.next_tls_slot: int = 0
        self.tls_store: dict[int, dict[int, int]] = {}   # tid → (slot → value)
        TLS_MAX_SLOTS = 64
        self.tls_max_slots: int = TLS_MAX_SLOTS

        # ── Registry ──────────────────────────────────────────────────────
        self.registry_values: RegistryMap = load_registry_json()

        # ── Timers ────────────────────────────────────────────────────────
        self.next_timer_id: int = 1

        # ── Local/GlobalAlloc tracking ────────────────────────────────────
        self.local_alloc_map: dict[int, int] = {}   # addr → size

        # ── Window / dialog system ────────────────────────────────────────
        self.window_manager: WindowManager = WindowManager()
        # pe_resources is set by run_exe.py after the PE is loaded
        self.pe_resources: Optional["PEResources"] = None

    # ── Heap allocation ───────────────────────────────────────────────────────

    def simple_alloc(self, size: int) -> int:
        """Bump-allocator for HeapAlloc/malloc/etc."""
        addr = self.next_heap_alloc
        self.next_heap_alloc = (self.next_heap_alloc + size + 15) & ~15
        self.heap_alloc_sizes[addr] = size
        return addr

    # ── Path translation ──────────────────────────────────────────────────────

    def translate_windows_path(self, win_path: str) -> str:
        """Map a Windows path to a host Linux path using config path_mappings."""
        p = win_path.replace("\\", "/")
        # Sort by key length descending so longer prefixes match first
        mappings = sorted(self.config.path_mappings.items(), key=lambda kv: -len(kv[0]))
        for win_prefix, linux_prefix in mappings:
            if p.lower().startswith(win_prefix):
                result = linux_prefix + p[len(win_prefix):]
                return result.replace("//", "/")
        return p.replace("//", "/")

    def reverse_translate_path(self, linux_path: str) -> str:
        """
        Convert a Linux path back to a Windows-style path.

        Reverses the config path_mappings (linux_prefix → Windows prefix).
        Longest Linux prefix wins so that nested mappings are handled correctly.

        Example with mapping ``{"c:/": "/home/user/.emu32/"}``:
            ``/home/user/.emu32/MCO/MCity_d.exe``  →  ``C:\\MCO\\MCity_d.exe``
        """
        # Sort by Linux prefix length descending (longest match first).
        mappings = sorted(self.config.path_mappings.items(), key=lambda kv: -len(kv[1]))
        for win_prefix_lower, linux_prefix in mappings:
            if linux_path.startswith(linux_prefix):
                # win_prefix_lower is like "c:/" or "d:/game/" — strip trailing slash,
                # convert forward slashes to backslashes, then uppercase.
                win_base = win_prefix_lower.rstrip("/").replace("/", "\\").upper()  # "C:" or "D:\GAME"
                remaining = linux_path[len(linux_prefix):]        # "MCO/MCity_d.exe"
                return win_base + "\\" + remaining.replace("/", "\\")
        # No mapping matched — return as-is with backslashes.
        return linux_path.replace("/", "\\")

    def open_file_handle(self, win_name: str, writable: bool) -> int:
        """Open a file and register it in file_handle_map. Returns the handle."""
        from tew.logger import logger
        # Device namespace paths (\\.\xxx) are kernel driver handles — never a
        # real file.  Return INVALID_HANDLE_VALUE without touching the OS.
        normalized = win_name.replace("\\", "/")
        if normalized.startswith("/./") or normalized.startswith("//./"):
            logger.debug("fileio", f'CreateFile("{win_name}") -> INVALID_HANDLE_VALUE (device path, not emulated)')
            return 0xFFFFFFFF
        handle = self.next_file_handle
        self.next_file_handle += 1
        if writable:
            real_path = self.translate_windows_path(win_name)
            try:
                fd = os.open(real_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            except OSError as e:
                logger.warn("fileio", f'CreateFile("{win_name}") -> INVALID (write open failed: {e})')
                return 0xFFFFFFFF
            self.file_handle_map[handle] = FileHandleEntry(
                path=real_path, data=b"", position=0, writable=True, fd=fd
            )
            logger.debug("fileio", f'CreateFile("{win_name}") -> 0x{handle:x} [write]')
            return handle
        linux_path = self.translate_windows_path(win_name)
        while True:
            real_path = find_file_ci(linux_path)
            if real_path is not None:
                try:
                    with open(real_path, "rb") as f:
                        data = f.read()
                    self.file_handle_map[handle] = FileHandleEntry(
                        path=real_path, data=data, position=0, writable=False, fd=None
                    )
                    logger.debug("fileio", f'CreateFile("{win_name}") -> 0x{handle:x} [read, {len(data)} bytes]')
                    return handle
                except OSError:
                    logger.warn("fileio", f'CreateFile("{win_name}") -> INVALID (read error)')
                    return 0xFFFFFFFF
            if not self.config.interactive_on_missing_file:
                logger.warn("fileio", f'CreateFile("{win_name}") -> INVALID (not found: {linux_path})')
                return 0xFFFFFFFF
            print(f"\n[FileIO] File not found: {linux_path}")
            print("  Add the file then press Enter to retry, or type 'c' to continue without it.")
            answer = input("  > ").strip().lower()
            if answer != "c":
                linux_path = self.translate_windows_path(win_name)
                continue
            logger.warn("fileio", f'CreateFile("{win_name}") -> INVALID (user skipped)')
            return 0xFFFFFFFF

    # ── TLS helpers ───────────────────────────────────────────────────────────

    def tls_current_thread_id(self) -> int:
        if self.current_thread_idx >= 0 and self.current_thread_idx < len(self.pending_threads):
            return self.pending_threads[self.current_thread_idx].thread_id
        return 1000  # main thread

    def tls_thread_store(self, tid: int) -> dict[int, int]:
        if tid not in self.tls_store:
            self.tls_store[tid] = {}
        return self.tls_store[tid]


# ── String helpers (take memory as arg, no state needed) ─────────────────────

def read_cstring(ptr: int, memory: "Memory", max_len: int = 260) -> str:
    """Read a null-terminated ANSI string from emulator memory."""
    s = []
    for i in range(max_len):
        ch = memory.read8(ptr + i)
        if ch == 0:
            break
        s.append(chr(ch))
    return "".join(s)


def read_wide_string(ptr: int, memory: "Memory", max_len: int = 260) -> str:
    """Read a null-terminated UTF-16LE string from emulator memory."""
    s = []
    for i in range(max_len):
        lo = memory.read8(ptr + i * 2)
        hi = memory.read8(ptr + i * 2 + 1)
        code = lo | (hi << 8)
        if code == 0:
            break
        s.append(chr(code))
    return "".join(s)
