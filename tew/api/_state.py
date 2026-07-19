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

from tew.api.window_manager import WindowManager
from tew.kernel.kernel import Kernel
from tew.kernel.scheduler import Scheduler, ThreadState

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

# ThreadState is the canonical thread descriptor; PendingThreadInfo is a legacy alias.
PendingThreadInfo = ThreadState


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
    Resolves every path component case-insensitively, not just the final one.
    """
    if os.path.exists(linux_path):
        return linux_path
    head, tail = os.path.split(linux_path)
    if not tail:
        # Root or bare separator — exists check above already failed.
        return None
    resolved_dir = find_file_ci(head)
    if resolved_dir is None:
        return None
    tail_lower = tail.lower()
    try:
        for entry in os.listdir(resolved_dir):
            if entry.lower() == tail_lower:
                return os.path.join(resolved_dir, entry)
    except OSError as e:
        from tew.logger import logger
        logger.debug("fileio", f"find_file_ci: cannot list {resolved_dir!r}: {e}")
    return None


def load_registry_json(
    base_dir: str | None = None,
    config: "EmulatorConfig | None" = None,
) -> RegistryMap:
    """Load fake registry values from registry.json in the project root.
    Keys and value names are normalized to lowercase. Returns empty map on error.

    If config is provided and its path_mappings map c:/ to a directory that
    differs from registry.json's _install_dir, all registry string values have
    the template install dir substituted with the actual Windows install root.
    """
    from tew.logger import logger
    try:
        file_path = os.path.join(base_dir or os.getcwd(), "registry.json")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Compute install-dir substitution when config overrides the default.
        # _install_dir is the Windows path the registry was authored for (e.g. C:\MCity\).
        # When --install-dir maps c:/ to a different root the game files live at C:\ directly,
        # so every registry path that starts with _install_dir needs updating.
        template_dir: str = data.get("_install_dir", "")
        actual_dir: str = ""
        if config and template_dir and "c:/" in config.path_mappings:
            candidate = "C:\\"
            if candidate.lower() != template_dir.lower().rstrip("\\") + "\\":
                actual_dir = candidate
                logger.info(
                    "registry",
                    f"Substituting install dir: {template_dir!r} → {actual_dir!r}",
                )

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
                    value = entry["value"]
                    if actual_dir and template_dir and isinstance(value, str):
                        lower = value.lower()
                        tmpl = template_dir.lower()
                        if tmpl in lower:
                            idx = lower.find(tmpl)
                            value = value[:idx] + actual_dir + value[idx + len(template_dir):]
                    result[normalized_key][vname.lower()] = RegistryEntry(
                        type=entry["type"], value=value
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

    def __init__(
        self,
        config: EmulatorConfig | None = None,
        registry_dir: str | None = None,
    ) -> None:
        # ── Emulator config ───────────────────────────────────────────────
        self.config: EmulatorConfig = config if config is not None else load_emulator_config()

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

        # ── Find handles (FindFirstFileA / FindNextFileA) ──────────────
        # Each entry: list of (filename: str, attrs: int) tuples, current index
        self.find_handle_map: dict[int, list[tuple[str, int]]] = {}
        self.find_handle_idx: dict[int, int] = {}
        self.next_find_handle: int = 0x6000

        # ── Kernel objects ────────────────────────────────────────────────
        self.kernel_handle_map: dict[int, KernelHandle] = {}
        self.next_kernel_handle: int = 0x7000

        # ── Dynamic modules ───────────────────────────────────────────────
        self.dynamic_modules: dict[int, DynamicModule] = {}   # handle → module

        # ── Cooperative threads ───────────────────────────────────────────
        self.pending_threads: list[PendingThreadInfo] = []
        self.next_thread_id: int = 1001
        self.next_thread_handle: int = 0x0000BEEF

        # ── TLS ───────────────────────────────────────────────────────────
        self.tls_slots: set[int] = set()
        self.next_tls_slot: int = 0
        self.tls_store: dict[int, dict[int, int]] = {}   # tid → (slot → value)
        TLS_MAX_SLOTS = 64
        self.tls_max_slots: int = TLS_MAX_SLOTS

        # ── Kernel scheduler ──────────────────────────────────────────────
        # tls_slots is passed by reference so TlsAlloc additions are visible.
        # Main thread TID 1000 matches the tls_current_thread_id() fallback.
        self.scheduler: Scheduler = Scheduler(tls_slots=self.tls_slots)
        self.scheduler.create_main_thread(thread_id=1000, handle=0xFFFFFFFF)
        # Kernel owns async I/O completions; wired into the scheduler so
        # tick() fires from _pick_next_ready() when no thread is READY.
        self.kernel: Kernel = Kernel(self)
        self.scheduler._kernel = self.kernel

        # ── Registry ──────────────────────────────────────────────────────
        self.registry_values: RegistryMap = load_registry_json(registry_dir, config=self.config)

        # ── Timers ────────────────────────────────────────────────────────
        self.next_timer_id: int = 1

        # ── Local/GlobalAlloc tracking ────────────────────────────────────
        self.local_alloc_map: dict[int, int] = {}   # addr → size

        # ── Current working directory ─────────────────────────────────────
        self.current_directory: str = "C:\\MCity"

        # ── Window / dialog system ────────────────────────────────────────
        self.window_manager: WindowManager = WindowManager()
        # pe_resources is set by run_exe.py after the PE is loaded
        self.pe_resources: Optional["PEResources"] = None

        # ── Fatal dialogs ─────────────────────────────────────────────────
        # Every MessageBoxA/W shown with a stop/hand icon (MB_ICONERROR /
        # MB_ICONSTOP / MB_ICONHAND) lands here, whether it was auto-answered
        # or shown for real. A voluntary ExitProcess after one of these is
        # NOT a clean exit -- see run_exe.py's post-run summary.
        self.fatal_dialogs: list[tuple[str, str]] = []

    # ── Virtual clock ─────────────────────────────────────────────────────────

    @property
    def virtual_ticks_ms(self) -> int:
        return self.scheduler.virtual_ticks_ms

    @virtual_ticks_ms.setter
    def virtual_ticks_ms(self, val: int) -> None:
        self.scheduler.virtual_ticks_ms = val

    # ── Heap allocation ───────────────────────────────────────────────────────

    def simple_alloc(self, size: int) -> int:
        """Bump-allocator for HeapAlloc/malloc/etc."""
        addr = self.next_heap_alloc
        self.next_heap_alloc = (self.next_heap_alloc + size + 15) & ~15
        self.heap_alloc_sizes[addr] = size
        return addr

    # ── Path translation ──────────────────────────────────────────────────────

    def translate_windows_path(self, win_path: str) -> str:
        """Map a Windows path to a host Linux path, resolving case-insensitively.

        Windows paths are case-insensitive; Linux is not.  After applying the
        drive/prefix mapping we run find_file_ci to resolve every path component
        to the actual on-disk case.  If no match exists we return the naively-
        translated path so callers can report ENOENT normally.
        """
        p = win_path.replace("\\", "/")
        mappings = sorted(self.config.path_mappings.items(), key=lambda kv: -len(kv[0]))
        for win_prefix, linux_prefix in mappings:
            if p.lower().startswith(win_prefix):
                naive = (linux_prefix + p[len(win_prefix):]).replace("//", "/")
                resolved = find_file_ci(naive)
                return resolved if resolved is not None else naive
        naive = p.replace("//", "/")
        resolved = find_file_ci(naive)
        return resolved if resolved is not None else naive

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

    def open_file_handle(self, win_name: str, writable: bool, no_create_prompt: bool = False) -> int:
        """Open a file and register it in file_handle_map. Returns the handle."""
        from tew.logger import logger
        # Device namespace paths (\\.\xxx) are kernel driver handles — never a
        # real file.  Return INVALID_HANDLE_VALUE without touching the OS.
        normalized = win_name.replace("\\", "/")
        if normalized.startswith("/./") or normalized.startswith("//./"):
            logger.debug("fileio", f'CreateFile("{win_name}") -> INVALID_HANDLE_VALUE (device path, not emulated)')
            return 0xFFFFFFFF
        if not win_name:
            logger.debug("fileio", 'CreateFile("") -> INVALID_HANDLE_VALUE (empty path)')
            return 0xFFFFFFFF
        # Win32 reserved device names — case-insensitive, ignore any path prefix.
        _dev_name = normalized.rsplit("/", 1)[-1].upper().split(".")[0]
        if _dev_name == "NUL":
            handle = self.next_file_handle
            self.next_file_handle += 1
            if writable:
                fd = os.open("/dev/null", os.O_WRONLY)
                self.file_handle_map[handle] = FileHandleEntry(
                    path="/dev/null", data=b"", position=0, writable=True, fd=fd
                )
            else:
                self.file_handle_map[handle] = FileHandleEntry(
                    path="/dev/null", data=b"", position=0, writable=False, fd=None
                )
            logger.debug("fileio", f'CreateFile("{win_name}") -> 0x{handle:x} [NUL device]')
            return handle
        if _dev_name in ("CON", "AUX", "PRN", "COM1", "COM2", "COM3", "COM4",
                         "COM5", "COM6", "COM7", "COM8", "COM9",
                         "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
                         "LPT6", "LPT7", "LPT8", "LPT9"):
            logger.debug("fileio", f'CreateFile("{win_name}") -> INVALID_HANDLE_VALUE (unsupported device {_dev_name})')
            return 0xFFFFFFFF
        handle = self.next_file_handle
        self.next_file_handle += 1
        if writable:
            real_path = self.translate_windows_path(win_name)
            # A bare relative filename (e.g. the game writing "trace000.txt"
            # with no path prefix at all) has no directory component --
            # os.path.dirname() returns "", and os.makedirs("",
            # exist_ok=True) raises FileNotFoundError rather than being a
            # no-op (real CreateFile needs no directory creation for this
            # case either: the process's current directory already exists).
            #
            # VERIFIED 2026-07-12 (merged temporarily with the dialog-click/
            # nomovie branches' work for this live check, not otherwise
            # related): with this fix, the previously-reliable "abortmessage:
            # mono.c:260" halt no longer occurs at all -- the run progresses
            # ~5M steps further (197.1M total, past real d3d8 rendering) to
            # a clean, honest, unrelated stop: "[UNIMPLEMENTED]
            # user32.dll!IsIconic -- halting". Not a crash, not investigated
            # further here.
            try:
                dirname = os.path.dirname(real_path)
                if dirname:
                    os.makedirs(dirname, exist_ok=True)
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
            if not self.config.interactive_on_missing_file or no_create_prompt:
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
        return self.scheduler.current_thread().thread_id

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
