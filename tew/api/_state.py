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
from tew.hardware.alloc_zig import bump_alloc_next
from tew.hardware.scheduler_zig import ZigScheduler
from tew.kernel.kernel import Kernel

if TYPE_CHECKING:
    from tew.hardware.memory import Memory
    from tew.api.pe_resources import PEResources


# ── Win32 CreateFile dwCreationDisposition values ───────────────────────────────
# Real, stable OS API constants (kernel32.h) -- open_file_handle's writable
# branch switches on these directly instead of a collapsed "writable" bool,
# since "opened with write access" and "allowed to create/truncate" are
# orthogonal in real Win32 (e.g. OPEN_EXISTING + GENERIC_WRITE must fail on
# a missing file and must never truncate an existing one).
CREATE_NEW        = 1
CREATE_ALWAYS     = 2
OPEN_EXISTING     = 3
OPEN_ALWAYS       = 4
TRUNCATE_EXISTING = 5

_DISPOSITION_NAMES = {
    CREATE_NEW: "CREATE_NEW",
    CREATE_ALWAYS: "CREATE_ALWAYS",
    OPEN_EXISTING: "OPEN_EXISTING",
    OPEN_ALWAYS: "OPEN_ALWAYS",
    TRUNCATE_EXISTING: "TRUNCATE_EXISTING",
}


def disposition_name(disposition: int) -> str:
    """Human-readable name for a dwCreationDisposition value, for log messages."""
    name = _DISPOSITION_NAMES.get(disposition)
    return f"{name}({disposition})" if name else f"UNKNOWN({disposition})"

# ── File handle types ─────────────────────────────────────────────────────────

@dataclass
class FileHandleEntry:
    path: str
    data: bytes          # file contents (empty bytes for write-only)
    position: int        # current read/write position
    writable: bool
    fd: Optional[int]    # host file descriptor (None = no real file backing)
    # True when the guest's CreateFile/fopen call also requested read access
    # (GENERIC_READ, or an fopen mode with "+") alongside write access --
    # distinct from `writable`, which only tracks whether *write* access was
    # requested. A real Win32 handle opened GENERIC_READ|GENERIC_WRITE
    # supports both ReadFile and WriteFile; before this field existed, any
    # writable=True handle always opened the real fd with os.O_WRONLY and
    # ReadFile unconditionally rejected it regardless of what access was
    # actually granted -- confirmed live 2026-08-07 as the real cause of
    # msjet35.dll's "unrecognized database format" on Tmp.MDB: Jet opens it
    # GENERIC_READ|GENERIC_WRITE (needs to read its own header back), and
    # got a handle that could only ever write.
    readable: bool = False


@dataclass
class FileMappingHandle:
    file_handle: Optional[int]  # underlying HANDLE from CreateFile, or None for an
                                 # anonymous (page-file-backed) mapping
    protect: int                # flProtect (PAGE_READONLY / PAGE_READWRITE / ...)
    max_size: int                # 0 means "size of the underlying file"


@dataclass
class MappedView:
    base_addr: int
    size: int
    mapping_handle: int
    file_offset: int
    writable: bool


def file_entry_size(entry: "FileHandleEntry") -> int:
    """Return the real length of a file handle's backing data.

    `len(entry.data)` is wrong for any fd-backed handle (`entry.fd is not
    None`) -- `open_file_handle` always sets `data=b""` for those and does
    real I/O through the fd instead, so `len(entry.data)` is always 0.
    Confirmed live 2026-08-28: `_lseek`/`_llseek` clamping against
    `len(entry.data)` silently reset a writable+readable (fd-backed)
    handle's position to 0 on any nonzero seek -- the exact access mode
    DAO/Jet uses to reread its own `.MDB` header (see `readable`'s own
    docstring above).
    """
    if entry.fd is not None:
        return os.fstat(entry.fd).st_size
    return len(entry.data)


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


def _win32_error_from_errno(e: OSError):
    """Maps a real host OSError to the closest real Win32 error code, for
    setting GetLastError() correctly on a CreateFile failure real guest code
    may branch on (e.g. ERROR_FILE_NOT_FOUND vs ERROR_ACCESS_DENIED)."""
    import errno
    from tew.api.win32_errors import Win32Error
    if e.errno == errno.ENOENT:
        return Win32Error.ERROR_PATH_NOT_FOUND
    if e.errno == errno.EEXIST:
        return Win32Error.ERROR_ALREADY_EXISTS
    if e.errno in (errno.EACCES, errno.EPERM, errno.EISDIR):
        return Win32Error.ERROR_ACCESS_DENIED
    return Win32Error.ERROR_ACCESS_DENIED

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
        self.virtual_protect: dict[int, int] = {}    # addr → PAGE_* at last VirtualAlloc call

        # ── File handles ──────────────────────────────────────────────────
        self.file_handle_map: dict[int, FileHandleEntry] = {}
        self.next_file_handle: int = 0x5000

        # ── File mappings (CreateFileMappingA / MapViewOfFile) ─────────────
        self.file_mapping_map: dict[int, FileMappingHandle] = {}
        self.mapped_views: dict[int, MappedView] = {}  # keyed by view base address

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
        # Handles (int), not ThreadState objects -- there is no Python object
        # to hold a reference to once thread state lives in Zig. Callers that
        # need a thread's fields go through self.scheduler's handle-keyed
        # accessors (get_suspended/set_suspended/get_completed/etc.).
        self.pending_threads: list[int] = []
        self.next_thread_id: int = 1001
        self.next_thread_handle: int = 0x0000BEEF

        # ── TLS ───────────────────────────────────────────────────────────
        # Which slots are allocated now lives in self.scheduler's native TLS
        # bitset (tls_alloc_slot/tls_free_slot/tls_slot_allocated) -- see
        # kernel32_sync.py's Tls* handlers. next_tls_slot/tls_max_slots stay
        # here: pure "next index to try" bookkeeping, not part of the port.
        self.next_tls_slot: int = 0
        self.tls_store: dict[int, dict[int, int]] = {}   # tid → (slot → value)
        TLS_MAX_SLOTS = 64
        self.tls_max_slots: int = TLS_MAX_SLOTS

        # ── COM per-thread error info (SetErrorInfo/GetErrorInfo, ordinals
        # 201/? in oleaut32.dll) ─────────────────────────────────────────
        self.error_info_store: dict[int, int] = {}   # tid → IErrorInfo ptr (0 = none)

        # ── Kernel scheduler ──────────────────────────────────────────────
        # Main thread TID 1000 matches the tls_current_thread_id() fallback.
        self.scheduler: ZigScheduler = ZigScheduler()
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

        # ── Guest stdout handle ───────────────────────────────────────────
        # Set by open_file_handle() the moment the guest opens "stdout.txt"
        # or "NUL" for write (WinMain's fclose(&_iobuf)+fopen("stdout.txt"/
        # "NUL","wt") stdout-redirect sequence) -- lets Channel_SystemPrint's
        # patch (patch_internals.py) write real text into the same stream
        # real puts()/printf() output lands in, instead of only the
        # game's own unrendered on-screen "SYSTEM" debug console.
        self.guest_stdout_handle: Optional[int] = None

        # ── Channel_DebugPrint host-side log file ────────────────────────────
        # channel_log.txt -- a real host file Channel_DebugPrint's patch
        # (patch_internals.py) writes to unconditionally, independent of
        # LOG_LEVEL/LOG_CATEGORIES filtering. Deliberately a *separate* file
        # from guest_stdout_handle's stream (Molly's request 2026-08-08:
        # "so we can tell it from the other 'normal' stuff") -- unlike
        # guest_stdout_handle, there's no guest-visible Win32 handle behind
        # this at all (the real game routes Channel_DebugPrint to its own
        # unrendered on-screen debug console, never to real stdout), so this
        # is opened directly by tew itself, lazily, on first write.
        self.channel_log_fd: Optional[int] = None

        # ── Byte-range file locks (LockFile/UnlockFile) ─────────────────────
        # Keyed by real host path (not handle -- real Win32 byte-range locks
        # are visible across every handle open on the same file, including
        # from other threads/processes; this emulator only has "other
        # threads" to worry about, but real Jet genuinely opens the same
        # database from more than one handle). Each entry is a list of
        # (start, end, owning_handle) tuples for currently-held exclusive
        # ranges -- plain LockFile has no shared-lock concept, only
        # LockFileEx does, and that's not implemented (not yet needed).
        self.file_locks: dict[str, list[tuple[int, int, int]]] = {}

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

    def write_guest_stdout(self, text: str) -> None:
        """Write real text into whatever real host file guest_stdout_handle
        currently points at (stdout.txt / NUL, see the field's docstring
        above) -- a no-op if the guest hasn't opened it yet, or opened it
        read-only, or already closed it. Shared by every internal patch/
        handler that wants its output to land in the same real stream real
        puts()/printf() output does, not just tew's own /tmp/emu.log --
        Channel_SystemPrint (patch_internals.py) and OutputDebugStringA/W
        (kernel32_io.py) both use this rather than each reimplementing the
        same os.write dance."""
        if self.guest_stdout_handle is None:
            return
        entry = self.file_handle_map.get(self.guest_stdout_handle)
        if entry is None or not entry.writable or entry.fd < 0:
            return
        data = text.encode("latin-1", errors="replace")
        os.write(entry.fd, data)

    def write_channel_log(self, text: str) -> None:
        """Write real text to channel_log.txt (see channel_log_fd's
        docstring above) -- opened lazily, on this first call, at the same
        host directory stdout.txt resolves to (translate_windows_path on
        "channel_log.txt", a driveless name anchored to current_directory,
        same as stdout.txt's own resolution) so both land next to each
        other for easy comparison, but as two genuinely separate files."""
        if self.channel_log_fd is None:
            host_path = self.translate_windows_path("channel_log.txt")
            dirname = os.path.dirname(host_path)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            self.channel_log_fd = os.open(host_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.write(self.channel_log_fd, text.encode("latin-1", errors="replace"))

    # ── Heap allocation ───────────────────────────────────────────────────────

    def simple_alloc(self, size: int) -> int:
        """Bump-allocator for HeapAlloc/malloc/etc. Cursor math is done by
        libcpu.so's bump_alloc_next (cpu/src/alloc.zig); the cursor itself
        and the size-tracking dict stay Python-owned, same split as
        ZigMemory leaving the buffer Python-owned in memory_zig.py."""
        addr = self.next_heap_alloc
        new_cursor = bump_alloc_next(self.next_heap_alloc, size)
        if new_cursor > THREAD_STACK_BASE:
            raise RuntimeError(
                f"heap allocator ran into THREAD_STACK_BASE: alloc of {size} bytes at "
                f"0x{addr:x} would push the heap cursor to 0x{new_cursor:x}, past "
                f"THREAD_STACK_BASE (0x{THREAD_STACK_BASE:x}) -- this would silently "
                f"alias live thread-stack memory instead of failing"
            )
        self.next_heap_alloc = new_cursor
        self.heap_alloc_sizes[addr] = size
        return addr

    # ── Path translation ──────────────────────────────────────────────────────

    def translate_windows_path(self, win_path: str) -> str:
        """Map a Windows path to a host Linux path, resolving case-insensitively.

        Windows paths are case-insensitive; Linux is not.  After applying the
        drive/prefix mapping we run find_file_ci to resolve every path component
        to the actual on-disk case.  If no match exists we return the naively-
        translated path so callers can report ENOENT normally.

        A path with no drive letter (e.g. "stdout.txt") is relative to the
        guest's own current directory, exactly like real CreateFile/fopen --
        NOT to this host Python process's cwd. Previously this fell straight
        through to the no-mapping-matched fallback below and got returned
        untranslated, so a bare relative fopen() landed wherever run_exe.py
        happened to be launched from instead of under the emulated
        filesystem root -- confirmed live 2026-08-07 via WinMain's
        fopen("stdout.txt","wt"), which landed in the tew repo's own working
        directory instead of ~/.emu32/MCity/stdout.txt alongside every other
        guest file.
        """
        p = win_path.replace("\\", "/")
        if len(p) < 2 or p[1] != ":":
            p = self.current_directory.replace("\\", "/").rstrip("/") + "/" + p
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

    def open_file_handle(
        self, win_name: str, writable: bool, memory: "Memory", no_create_prompt: bool = False,
        disposition: int = CREATE_ALWAYS, also_readable: bool = False,
    ) -> int:
        """Open a file and register it in file_handle_map. Returns the handle.

        Also sets the real Win32 last-error code (TEB+0x34) on every failure
        path -- previously this function only logged, so GetLastError() after
        a failed CreateFile always read whatever unrelated call happened to
        set it last. Real "check if exists via OPEN_EXISTING, fall back to
        creating it" guest code relies on GetLastError() == ERROR_FILE_NOT_FOUND
        to know a missing-file failure is expected/recoverable, not fatal.

        CORRECTED 2026-08-07: an earlier version of this docstring claimed
        DAO/Jet was supposed to retry 'C:\\SaveData\\DB\\Tmp.MDB' with
        CREATE_ALWAYS/CREATE_NEW after an OPEN_EXISTING miss, and that tew
        was missing something that prevented that retry. That was wrong --
        confirmed via Ghidra decompile of the real MCity_d.exe, Tmp.MDB is
        never created by any OPEN_EXISTING-retry logic at all. It's created
        once, early, by Dbcode_CopyDataBaseToSaveData (WinMain, real copy of
        a shipped 'C:\\Data\\DB\\Online.MDB' template via FeTools_CopyFile),
        entirely separate from DB_StartUpDatabase's later OPEN_EXISTING open.
        The actual bug was a tew-side patch (patch_internals.py's old
        _winmain_check3) that faked that copy's success without ever running
        it -- removed; see changelog.md "2026-08-07".
        """
        from tew.logger import logger
        from tew.api.win32_errors import Win32Error
        # Device namespace paths (\\.\xxx) are kernel driver handles — never a
        # real file.  Return INVALID_HANDLE_VALUE without touching the OS.
        normalized = win_name.replace("\\", "/")
        if normalized.startswith("/./") or normalized.startswith("//./"):
            logger.debug("fileio", f'CreateFile("{win_name}") -> INVALID_HANDLE_VALUE (device path, not emulated)')
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
            return 0xFFFFFFFF
        if not win_name:
            logger.debug("fileio", 'CreateFile("") -> INVALID_HANDLE_VALUE (empty path)')
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_INVALID_PARAMETER))
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
                self.guest_stdout_handle = handle
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
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_ACCESS_DENIED))
            return 0xFFFFFFFF
        handle = self.next_file_handle
        self.next_file_handle += 1
        if writable:
            # "Opened for write access" (writable) and "allowed to create or
            # truncate" (disposition) are orthogonal in real Win32 -- e.g.
            # OPEN_EXISTING + GENERIC_WRITE must fail on a missing file and
            # must never truncate an existing one, even though it's a
            # perfectly normal "open this existing file for read+write"
            # request. Previously this branch always did O_CREAT|O_TRUNC
            # regardless of disposition, silently fabricating an empty file
            # for a genuinely-missing OPEN_EXISTING target (or worse,
            # truncating a real existing file's data) instead of the
            # honest ERROR_FILE_NOT_FOUND failure real Windows gives here.
            must_exist = disposition in (OPEN_EXISTING, TRUNCATE_EXISTING)
            existing_path = find_file_ci(self.translate_windows_path(win_name))
            if must_exist and existing_path is None:
                logger.warn("fileio",
                    f'CreateFile("{win_name}") -> INVALID ({disposition_name(disposition)} '
                    f'requires the file to already exist, but it was not found)')
                memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
                return 0xFFFFFFFF
            if disposition == CREATE_NEW and existing_path is not None:
                logger.warn("fileio",
                    f'CreateFile("{win_name}") -> INVALID (CREATE_NEW: already exists)')
                memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_ALREADY_EXISTS))
                return 0xFFFFFFFF
            real_path = existing_path or self.translate_windows_path(win_name)
            flags = os.O_RDWR if also_readable else os.O_WRONLY
            if disposition == CREATE_NEW:
                # Already confirmed non-existent above; O_EXCL is still the
                # correct real flag (atomic fail-if-exists), not O_TRUNC.
                flags |= os.O_CREAT | os.O_EXCL
            else:
                if not must_exist:
                    flags |= os.O_CREAT
                if disposition in (CREATE_ALWAYS, TRUNCATE_EXISTING):
                    flags |= os.O_TRUNC
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
                if flags & os.O_CREAT:
                    dirname = os.path.dirname(real_path)
                    if dirname:
                        os.makedirs(dirname, exist_ok=True)
                fd = os.open(real_path, flags, 0o644)
            except OSError as e:
                logger.warn("fileio", f'CreateFile("{win_name}") -> INVALID (write open failed: {e})')
                memory.write32(TEB_BASE + 0x34, int(_win32_error_from_errno(e)))
                return 0xFFFFFFFF
            self.file_handle_map[handle] = FileHandleEntry(
                path=real_path, data=b"", position=0, writable=True, fd=fd, readable=also_readable
            )
            logger.debug(
                "fileio",
                f'CreateFile("{win_name}") -> 0x{handle:x} [write{"+read" if also_readable else ""}]',
            )
            bare_name = win_name.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if bare_name in ("stdout.txt", "nul"):
                self.guest_stdout_handle = handle
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
                except OSError as e:
                    logger.warn("fileio", f'CreateFile("{win_name}") -> INVALID (read error)')
                    memory.write32(TEB_BASE + 0x34, int(_win32_error_from_errno(e)))
                    return 0xFFFFFFFF
            if not self.config.interactive_on_missing_file or no_create_prompt:
                logger.warn("fileio", f'CreateFile("{win_name}") -> INVALID (read-only open, file not found: {linux_path})')
                memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
                return 0xFFFFFFFF
            print(f"\n[FileIO] File not found: {linux_path}")
            print("  Add the file then press Enter to retry, or type 'c' to continue without it.")
            answer = input("  > ").strip().lower()
            if answer != "c":
                linux_path = self.translate_windows_path(win_name)
                continue
            logger.warn("fileio", f'CreateFile("{win_name}") -> INVALID (user skipped)')
            memory.write32(TEB_BASE + 0x34, int(Win32Error.ERROR_FILE_NOT_FOUND))
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
    """Read a null-terminated ANSI string from emulator memory.

    Reads the whole (up to max_len) span in one bulk call and scans for
    the terminator in Python, instead of one memory.read8() FFI call per
    character -- confirmed via cProfile 2026-08-07 this was among the
    hottest functions in the entire codebase (called from dozens of sites:
    every %s vararg substitution, every filename/registry-value read,
    getenv, etc. -- 210,993 calls / 6.97s cumulative time in one 300M-step
    profiled run, the top identified drag on real throughput alongside
    WriteFile/ReadFile's now-fixed per-byte loops). Clamped to the
    actually-addressable span so a genuinely invalid starting pointer still
    raises the same bounds error read8() would -- callers rely on that to
    catch unreadable pointers (see e.g. test_patch_internals.py's
    "unreadable format pointer" cases).
    """
    avail = memory.size - ptr
    if avail <= 0:
        memory.read8(ptr)  # raises the real bounds error
    data = memory.read_bytes(ptr, min(max_len, avail))
    nul = data.find(b"\x00")
    if nul != -1:
        data = data[:nul]
    return data.decode("latin-1")


def read_wide_string(ptr: int, memory: "Memory", max_len: int = 260) -> str:
    """Read a null-terminated UTF-16LE string from emulator memory.

    Same bulk-read rationale as read_cstring above -- one FFI call instead
    of up to 2*max_len. The null-terminator scan still walks the (already
    local, no-FFI) bytes in 2-byte steps rather than using bytes.find(),
    since a lone 00 byte at an odd offset would be a false match otherwise.
    """
    avail = memory.size - ptr
    if avail <= 0:
        memory.read8(ptr)  # raises the real bounds error
    n_chars = min(max_len, avail // 2)
    data = memory.read_bytes(ptr, n_chars * 2)
    for i in range(0, len(data) - 1, 2):
        if data[i] == 0 and data[i + 1] == 0:
            data = data[:i]
            break
    return data.decode("utf-16-le")
