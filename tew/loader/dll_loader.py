"""Windows DLL loader — loads PE files into emulator memory."""

from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Callable, TYPE_CHECKING

from tew.logger import logger
from tew.api._state import find_file_ci
from tew.hardware.cpu_zig import FatalHaltError

if TYPE_CHECKING:
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers


def should_invoke_dependency_dllmain(
    dep_was_loaded: bool,
    imported_dll: "LoadedDLL | None",
    on_dependency_loaded: "Callable[[LoadedDLL], None] | None",
) -> bool:
    """Decides whether a just-resolved PE-import dependency needs its own
    DllMain invoked. Pulled out as a standalone, easily unit-testable
    predicate since it's the part of the dependency-DllMain fix most likely
    to grow a subtle off-by-one (double-invoking a shared dependency's
    DllMain, or missing a freshly-loaded one) as this code changes.

    True only when: the dependency was NOT already loaded before this
    resolution (a second DLL importing an already-loaded dependency must
    not re-run its DllMain), the load actually succeeded, it has a real
    entry point (some DLLs are pure resource/data containers with none),
    and a callback was actually supplied (callers that never pass one --
    e.g. startup-time static-import loading -- get the pre-fix behavior
    completely unchanged).
    """
    return (
        not dep_was_loaded
        and imported_dll is not None
        and imported_dll.entry_point != 0
        and on_dependency_loaded is not None
    )


def apply_base_relocations(
    memory: "Memory",
    blocks: list,
    base_address: int,
    preferred_base: int,
) -> None:
    """Apply type-3 (HIGHLOW, 32-bit absolute) base relocation entries."""
    relocation_delta = (base_address - preferred_base) & 0xFFFFFFFF
    if relocation_delta == 0:
        return

    for block in blocks:
        for entry in block.entries:
            if entry.type == 3:  # HIGHLOW
                reloc_addr = (base_address + block.page_rva + entry.offset) & 0xFFFFFFFF
                current_value = memory.read32(reloc_addr)
                new_value = (current_value + relocation_delta) & 0xFFFFFFFF
                memory.write32(reloc_addr, new_value)


@dataclass
class LoadedDLL:
    name: str
    base_address: int
    size: int
    exports: dict[str, int] = field(default_factory=dict)
    entry_point: int = 0


@dataclass
class AddressMapping:
    dll_name: str
    base_address: int
    end_address: int


@dataclass
class _DLLIATEntry:
    iat_addr: int
    dll_name: str
    imported_dll_name: str
    func_name: str


_FORWARDING_MAP: dict[str, list[str]] = {
    "api-ms-win-core-rtlsupport":     ["ntdll", "kernel32"],
    "api-ms-win-core-processthreads": ["kernel32", "ntdll"],
    "api-ms-win-core-synch":          ["kernel32", "ntdll"],
    "api-ms-win-core-file":           ["kernel32", "ntdll"],
    "api-ms-win-core-memory":         ["kernel32", "ntdll"],
    "api-ms-win-core-heap":           ["kernel32", "ntdll"],
    "api-ms-win-core-registry":       ["advapi32", "kernel32"],
    "api-ms-win-core-io":             ["kernel32", "ntdll"],
    "api-ms-win-core-handle":         ["kernel32", "ntdll"],
    "api-ms-win-core-errorhandling":  ["kernel32", "ntdll"],
    "api-ms-win-core-string":         ["kernel32", "ntdll"],
    "api-ms-win-core-localization":   ["kernel32", "ntdll"],
    "api-ms-win-core-sysinfo":        ["kernel32", "ntdll"],
    "api-ms-win-core-datetime":       ["kernel32", "ntdll"],
    "api-ms-win-core-libraryloader":  ["kernel32", "ntdll"],
    "api-ms-win-core-console":        ["kernel32"],
    "api-ms-win-security-":           ["advapi32", "ntdll"],
    "api-ms-win-crt-":                ["msvcrt"],
    "api-ms-win-shell-":              ["shell32", "kernel32"],
    "api-ms-win-mm-":                 ["winmm", "kernel32"],
    "api-ms-win-gdi-":                ["gdi32", "kernel32"],
}

# Legacy DLL names real-loaded third-party components sometimes import from,
# aliased to the DLL name tew's own handlers are actually registered under.
# e.g. dao350.dll (VC4-era build) imports from "MSVCRT40.dll", not the
# "MSVCRT.dll" tew/api/msvcrt_handlers.py registers under -- same functions,
# older DLL naming. Used by patch_dll_iats as a last-resort lookup.
_LEGACY_DLL_ALIASES: dict[str, str] = {
    "msvcrt40.dll": "msvcrt.dll",
    "msvcrt20.dll": "msvcrt.dll",
    "crtdll.dll":   "msvcrt.dll",
}


def _make_unimplemented_handler(dll_name: str, func_name: str):
    def _handler(cpu):
        logger.error("handlers", f"[UNIMPLEMENTED] {dll_name}!{func_name} — halting")
        logger.error(
            "cpu",
            f"  EIP=0x{(cpu.eip) & 0xFFFFFFFF:08x}  "
            f"EAX=0x{cpu.regs[0] & 0xFFFFFFFF:08x}  "
            f"ECX=0x{cpu.regs[1] & 0xFFFFFFFF:08x}  "
            f"ESP=0x{cpu.regs[4] & 0xFFFFFFFF:08x}  "
            f"EBP=0x{cpu.regs[5] & 0xFFFFFFFF:08x}",
        )
        cpu.halted = True
        cpu.fatal_halt = True
    return _handler


def patch_iat_entry(
    memory: "Memory",
    win32_handlers: "Win32Handlers",
    iat_addr: int,
    dll_name: str,
    func_name: str,
    real_addr: int | None = None,
    alias: str | None = None,
) -> str:
    """Resolve one IAT slot and write the result to iat_addr.

    Tries, in order: a registered handler, the legacy alias's handler (if
    given), a real DLL export address (if given), and finally an
    auto-generated fatal-halt stub -- the single fallback path shared by the
    main EXE's IAT (import_resolver.write_iat_handlers) and secondary DLLs'
    IATs (DLLLoader.patch_dll_iats). Without this fallback, an unmatched
    import's IAT slot is left holding whatever raw, unrelocated bytes were on
    disk. Since tew's memory is an unprotected flat bytearray, CALLing
    through that garbage doesn't fault the way it would on real Windows -- it
    silently executes whatever's there, with no halt, no SEH activity, and no
    log line, until it wanders somewhere that happens to look like a return.
    That's exactly what made tid=1012 (DAO's DllMain-calling worker thread,
    dao350.dll's IsDBCSLeadByte import) appear to "return normally" while
    actually skipping every real stack frame above it -- confirmed via a
    logpoint at the CALL site, which showed ESI holding a bogus address
    instead of a resolved handler. See memory/status.md.

    Returns "handler", "real", or "auto" so callers can keep their own counts.
    """
    handler_addr = (
        win32_handlers.get_handler_address(dll_name, func_name)
        or win32_handlers.get_handler_address(dll_name + ".dll", func_name)
    )
    if handler_addr is None and alias is not None:
        handler_addr = win32_handlers.get_handler_address(alias, func_name)

    if handler_addr is not None:
        memory.write32(iat_addr, handler_addr)
        return "handler"

    if real_addr:
        memory.write32(iat_addr, real_addr)
        return "real"

    win32_handlers.register_handler(dll_name, func_name, _make_unimplemented_handler(dll_name, func_name))
    auto_addr = (
        win32_handlers.get_handler_address(dll_name, func_name)
        or win32_handlers.get_handler_address(dll_name + ".dll", func_name)
    )
    if auto_addr is not None:
        memory.write32(iat_addr, auto_addr)
    return "auto"


class DLLLoader:
    _DLL_SIZE = 0x01000000   # 16 MB per DLL
    _MAX_ADDRESS = 0x40000000

    def __init__(self, search_paths: list[str] | None = None) -> None:
        self._search_paths: list[str] = list(search_paths or [])
        self._loaded_dlls: dict[str, LoadedDLL] = {}
        self._address_mappings: list[AddressMapping] = []
        self._dll_iat_entries: list[_DLLIATEntry] = []
        # Every real DLL's import table includes kernel32.dll/user32.dll/etc,
        # names this emulator never has on disk (they're Python-simulated,
        # not real files) -- without this, find_dll_file's full
        # case-insensitive directory walk (find_file_ci, os.listdir at every
        # path component) re-ran from scratch for the same always-missing
        # name on every single DLL load, for the whole run.
        self._not_found: set[str] = set()
        # High-water mark into _dll_iat_entries: an entry's correct patch
        # (real_addr, handler, or auto-stub) is fully determined the moment
        # it's appended -- its target DLL was already recursively
        # load_dll'd first (see the loop that appends these), and every
        # Win32 handler is registered once at startup before any DLL loads
        # -- so re-patching an already-patched entry can never produce a
        # different result. patch_dll_iats only needs to process the slice
        # added since its last call, not rescan everything from index 0.
        self._iat_patch_cursor: int = 0

    def add_search_path(self, path: str) -> None:
        if path not in self._search_paths:
            self._search_paths.append(path)
            # A newly-added path could contain a name a prior lookup missed
            # -- the negative cache is only valid against the search paths
            # that were in effect when it was populated.
            self._not_found.clear()

    def _is_address_range_available(self, base_address: int, size: int) -> bool:
        end_address = base_address + size - 1
        for mapping in self._address_mappings:
            if not (end_address < mapping.base_address or base_address > mapping.end_address):
                return False
        return True

    def _find_available_base(self, preferred_base: int) -> int:
        if 0 < preferred_base < self._MAX_ADDRESS:
            if self._is_address_range_available(preferred_base, self._DLL_SIZE):
                return preferred_base

        base = 0x10000000
        while base < self._MAX_ADDRESS:
            if self._is_address_range_available(base, self._DLL_SIZE):
                return base
            base += self._DLL_SIZE

        raise RuntimeError(
            f"No available address space for DLL (needed 0x{self._DLL_SIZE:08x} bytes)"
        )

    def find_dll_file(self, dll_name: str) -> str | None:
        for path in self._search_paths:
            full_path = os.path.join(path, dll_name)
            resolved = find_file_ci(full_path)
            if resolved is not None:
                return resolved
        return None

    def _get_forwarding_candidates(self, dll_name: str) -> list[str]:
        lower = dll_name.lower()
        for prefix, candidates in _FORWARDING_MAP.items():
            if lower.startswith(prefix):
                return candidates
        return ["kernel32", "ntdll"]

    def load_dll(
        self, dll_name: str, memory: "Memory",
        on_dependency_loaded: "Callable[[LoadedDLL], None] | None" = None,
    ) -> LoadedDLL | None:
        from tew.pe.exe_file import EXEFile

        key = dll_name.lower()
        if key in self._loaded_dlls:
            return self._loaded_dlls[key]
        if key in self._not_found:
            return None

        dll_path = self.find_dll_file(dll_name)
        if not dll_path:
            if dll_name.startswith("api-ms-win-"):
                logger.debug("dll", f"{dll_name} not found (API forwarding DLL - imports will be resolved at runtime)")
            else:
                logger.warn("dll", f"Could not find {dll_name}")
            self._not_found.add(key)
            return None

        try:
            logger.debug("dll", f"Loading {dll_name} from {dll_path}")
            exe = EXEFile(dll_path)

            preferred_base = exe.optional_header.image_base
            base_address = self._find_available_base(preferred_base)

            if base_address == preferred_base:
                logger.trace("dll", f"  Loaded at preferred base 0x{base_address:08x}")
            else:
                logger.debug("dll", f"  Preferred base 0x{preferred_base:08x} unavailable, using 0x{base_address:08x}")

            for section in exe.section_headers:
                vaddr = base_address + section.virtual_address
                if section.data:
                    memory.load(vaddr, section.data)

            if exe.base_relocation_table and exe.base_relocation_table.blocks:
                relocation_delta = (base_address - preferred_base) & 0xFFFFFFFF
                if relocation_delta != 0:
                    logger.trace("dll", f"  [Relocations] Applying delta 0x{relocation_delta:08x}")
                apply_base_relocations(memory, exe.base_relocation_table.blocks, base_address, preferred_base)

            exports: dict[str, int] = {}
            if exe.export_table:
                for exp in exe.export_table.entries:
                    func_addr = base_address + exp.rva
                    if exp.name:
                        exports[exp.name] = func_addr
                    ordinal_key = f"Ordinal #{exp.ordinal}"
                    exports[ordinal_key] = func_addr
                    label = exp.name if exp.name else ordinal_key
                    logger.trace("dll", f"  [Export] {label} @ 0x{func_addr:08x}")

            entry_point = (
                (base_address + exe.optional_header.address_of_entry_point) & 0xFFFFFFFF
                if exe.optional_header.address_of_entry_point != 0
                else 0
            )
            dll = LoadedDLL(
                name=dll_name,
                base_address=base_address,
                size=self._DLL_SIZE,
                exports=exports,
                entry_point=entry_point,
            )

            self._loaded_dlls[key] = dll
            self._address_mappings.append(
                AddressMapping(dll_name=dll_name, base_address=base_address, end_address=base_address + self._DLL_SIZE - 1)
            )

            logger.info("dll", f"Loaded {dll_name} at 0x{base_address:08x}-0x{base_address + self._DLL_SIZE - 1:08x} with {len(exports)} exports")

            if exe.import_table:
                logger.debug("loader", f"  [IAT Resolution] Resolving {len(exe.import_table.descriptors)} import descriptors for {dll_name}")
                for descriptor in exe.import_table.descriptors:
                    dep_was_loaded = descriptor.dll_name.lower() in self._loaded_dlls
                    imported_dll = self.load_dll(descriptor.dll_name, memory, on_dependency_loaded)
                    # A DLL loaded only as another DLL's PE-import dependency
                    # (never via an explicit guest LoadLibraryA call) never
                    # ran its own DllMain before this -- its CRT startup
                    # (which stashes e.g. "my own HINSTANCE" into a global)
                    # never executed. Real-world hit: msjter35.dll pulls in
                    # msjint35.dll this way; msjint35.dll's own exported code
                    # later reads that never-set global as 0 and hands NULL
                    # to LoadStringA, silently failing every resource lookup.
                    # on_dependency_loaded (wired up only at the runtime
                    # LoadLibraryA call sites, where a cpu exists) fixes this
                    # by invoking the dependency's real DllMain synchronously,
                    # the moment it's first loaded -- correctly ordered before
                    # the *caller's* own DllMain runs, since that only
                    # happens after this whole import-resolution loop (and
                    # thus every transitively-loaded dependency) completes.
                    if should_invoke_dependency_dllmain(dep_was_loaded, imported_dll, on_dependency_loaded):
                        on_dependency_loaded(imported_dll)

                    for entry in descriptor.entries:
                        import_addr: int | None = None

                        if imported_dll:
                            import_addr = imported_dll.exports.get(entry.name)

                        if import_addr is None and descriptor.dll_name.startswith("api-ms-win-"):
                            candidates = self._get_forwarding_candidates(descriptor.dll_name)
                            for candidate in candidates:
                                candidate_dll = self._loaded_dlls.get(candidate.lower())
                                if candidate_dll and candidate_dll.exports:
                                    found = candidate_dll.exports.get(entry.name)
                                    if found:
                                        import_addr = found
                                        break

                            if import_addr is None:
                                for loaded_name, loaded_dll in self._loaded_dlls.items():
                                    if loaded_name.startswith("api-ms-win-") or loaded_name == key:
                                        continue
                                    found = loaded_dll.exports.get(entry.name)
                                    if found:
                                        import_addr = found
                                        break

                        iat_addr = base_address + entry.iat_rva
                        if import_addr is not None:
                            memory.write32(iat_addr, import_addr)
                        self._dll_iat_entries.append(
                            _DLLIATEntry(
                                iat_addr=iat_addr,
                                dll_name=key,
                                imported_dll_name=descriptor.dll_name.lower(),
                                func_name=entry.name,
                            )
                        )

            return dll

        except FatalHaltError:
            # A dependency's DllMain (invoked synchronously above, via
            # on_dependency_loaded) can run arbitrary guest code through a
            # nested cpu.run() -- if *anything* live in the emulator hits an
            # unimplemented API or corruption check during that window, this
            # is where cpu.run() surfaces it, even though it has nothing to
            # do with this DLL failing to load. fatal_halt means the whole
            # emulator session must stop; swallowing it here as "DLL not
            # found" (the broad except below) would silently downgrade a
            # fatal condition to a per-call warning and let the caller limp
            # on. Let it propagate to wherever it's actually meant to be
            # handled (see FatalHaltError's docstring, cpu_zig.py).
            raise

        except Exception as err:
            logger.warn("dll", f"Failed to load {dll_name}: {err}")
            import traceback
            for line in traceback.format_exc().split("\n")[:3]:
                logger.debug("dll", f"  {line}")
            return None

    def patch_dll_iats(self, memory: "Memory", win32_handlers: "Win32Handlers") -> None:
        """Patch newly-accumulated DLL IAT entries with Win32 stubs where available.

        See patch_iat_entry (module-level) for what happens to an unmatched
        import -- this is the secondary-DLL side of that shared fallback;
        write_iat_handlers (import_resolver.py) is the main-EXE side. Passes
        the already-resolved real export address (from load_dll) through as
        real_addr, so a genuine DLL-to-DLL call (e.g. msjet35.dll calling
        into msjint35.dll) keeps executing as real code unless a Python
        handler specifically overrides it -- without this, every such call
        lacking a matching handler was silently clobbered with the
        unimplemented-stub fallback, even though load_dll had already wired
        up the correct address.

        Only processes entries added since the last call (see
        _iat_patch_cursor): an entry's target DLL is already recursively
        load_dll'd, and every Win32 handler is registered before any DLL
        loads, so an already-patched entry's outcome can never change on a
        later call -- rescanning it again would be pure repeated work.
        """
        new_entries = self._dll_iat_entries[self._iat_patch_cursor:]
        self._iat_patch_cursor = len(self._dll_iat_entries)

        # Breakdown by (DLL being patched, DLL its imports come from) --
        # a single call here can cover more than one DLL's entries at once
        # (load_dll recursively loads and IAT-resolves everything a newly
        # loaded DLL itself imports before returning), so the one-line
        # overall summary below doesn't say which DLL any given entry
        # actually belongs to.
        group_counts: dict[tuple[str, str], int] = {}
        for entry in new_entries:
            key = (entry.dll_name, entry.imported_dll_name)
            group_counts[key] = group_counts.get(key, 0) + 1
        for (dll_name, imported_dll_name), count in group_counts.items():
            logger.debug(
                "loader",
                f"  patching {dll_name}: {count} import(s) from {imported_dll_name}",
            )

        # Per-dll_name breakdown -- a single call here can cover more than one
        # DLL's entries at once (see docstring above), so one aggregate count
        # across all of them would blur which DLL actually had unimplemented
        # imports.
        per_dll_counts: dict[str, dict[str, int]] = {}
        for entry in new_entries:
            alias = _LEGACY_DLL_ALIASES.get(entry.imported_dll_name)
            imported_dll = self._loaded_dlls.get(entry.imported_dll_name)
            real_addr = imported_dll.exports.get(entry.func_name) if imported_dll else None
            outcome = patch_iat_entry(
                memory, win32_handlers, entry.iat_addr,
                entry.imported_dll_name, entry.func_name, real_addr=real_addr, alias=alias,
            )
            counts = per_dll_counts.setdefault(entry.dll_name, {"handler": 0, "real": 0, "auto": 0})
            counts[outcome] += 1

        for dll_name, counts in per_dll_counts.items():
            handler_count = counts["handler"]
            real_count = counts["real"]
            auto_handler_count = counts["auto"]
            total = handler_count + real_count + auto_handler_count
            # "with stubs" covers both a registered Python handler and an
            # auto-generated fatal-halt stub -- both are real stubs, unlike a
            # "real" outcome (the IAT slot points at genuine DLL code).
            stub_count = handler_count + auto_handler_count
            logger.info(
                "loader",
                f"Patched {stub_count}/{total} {dll_name} IAT entries with stubs "
                f"({real_count} real DLL exports, {auto_handler_count} auto-stubs for unimplemented imports)",
            )

    def patch_dll_exports(self, memory: "Memory", win32_handlers: "Win32Handlers") -> None:
        """Patch DLL export addresses in-place with INT 0xFE; RET trampolines."""
        patched_count = 0
        for dll_name, dll in self._loaded_dlls.items():
            for func_name, export_addr in dll.exports.items():
                if func_name.startswith("Ordinal #"):
                    continue
                handler_entry = win32_handlers.find_handler_by_func_name(func_name)
                if not handler_entry:
                    continue
                if 0x00200000 <= export_addr < 0x00210000:
                    continue
                win32_handlers.patch_address(export_addr, f"{dll_name}!{func_name}", handler_entry.handler)
                patched_count += 1
        logger.info("loader", f"Patched {patched_count} DLL export addresses with stub trampolines")

    def get_export_address(self, dll_name: str, function_name: str) -> int | None:
        dll = self._loaded_dlls.get(dll_name.lower())
        return dll.exports.get(function_name) if dll else None

    def get_dll(self, dll_name: str) -> LoadedDLL | None:
        return self._loaded_dlls.get(dll_name.lower())

    def get_loaded_dlls(self) -> list[LoadedDLL]:
        return list(self._loaded_dlls.values())

    def find_dll_for_address(self, address: int) -> LoadedDLL | None:
        for mapping in self._address_mappings:
            if mapping.base_address <= address <= mapping.end_address:
                return self._loaded_dlls.get(mapping.dll_name.lower())
        return None

    def get_address_mappings(self) -> list[dict]:
        return [
            {"dll_name": m.dll_name, "base_address": m.base_address, "end_address": m.end_address}
            for m in self._address_mappings
        ]

    def is_in_dll_range(self, address: int) -> bool:
        return self.find_dll_for_address(address) is not None
