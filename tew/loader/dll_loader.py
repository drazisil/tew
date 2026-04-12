"""Windows DLL loader — loads PE files into emulator memory."""

from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tew.logger import logger

if TYPE_CHECKING:
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers


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


class DLLLoader:
    _DLL_SIZE = 0x01000000   # 16 MB per DLL
    _MAX_ADDRESS = 0x40000000

    def __init__(self, search_paths: list[str] | None = None) -> None:
        self._search_paths: list[str] = list(search_paths or [])
        self._loaded_dlls: dict[str, LoadedDLL] = {}
        self._address_mappings: list[AddressMapping] = []
        self._dll_iat_entries: list[_DLLIATEntry] = []

    def add_search_path(self, path: str) -> None:
        if path not in self._search_paths:
            self._search_paths.append(path)

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

    def _find_dll_file(self, dll_name: str) -> str | None:
        for path in self._search_paths:
            full_path = os.path.join(path, dll_name)
            if os.path.exists(full_path):
                return full_path

        lower_name = dll_name.lower()
        for dir_path in self._search_paths:
            try:
                entries = os.listdir(dir_path)
                match = next((e for e in entries if e.lower() == lower_name), None)
                if match:
                    return os.path.join(dir_path, match)
            except OSError as e:
                logger.debug("dll", f"Search path {dir_path} unreadable: {e}")

        return None

    def _get_forwarding_candidates(self, dll_name: str) -> list[str]:
        lower = dll_name.lower()
        for prefix, candidates in _FORWARDING_MAP.items():
            if lower.startswith(prefix):
                return candidates
        return ["kernel32", "ntdll"]

    def load_dll(self, dll_name: str, memory: "Memory") -> LoadedDLL | None:
        from tew.pe.exe_file import EXEFile

        key = dll_name.lower()
        if key in self._loaded_dlls:
            return self._loaded_dlls[key]

        dll_path = self._find_dll_file(dll_name)
        if not dll_path:
            if dll_name.startswith("api-ms-win-"):
                logger.debug("dll", f"{dll_name} not found (API forwarding DLL - imports will be resolved at runtime)")
            else:
                logger.warn("dll", f"Could not find {dll_name}")
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
                    imported_dll = self.load_dll(descriptor.dll_name, memory)

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

        except Exception as err:
            logger.warn("dll", f"Failed to load {dll_name}: {err}")
            import traceback
            for line in traceback.format_exc().split("\n")[:3]:
                logger.debug("dll", f"  {line}")
            return None

    def patch_dll_iats(self, memory: "Memory", win32_handlers: "Win32Handlers") -> None:
        """Re-patch all loaded DLLs' IAT entries with Win32 stubs where available."""
        patched_count = 0
        for entry in self._dll_iat_entries:
            handler_addr = (
                win32_handlers.get_handler_address(entry.imported_dll_name, entry.func_name)
                or win32_handlers.get_handler_address(entry.imported_dll_name + ".dll", entry.func_name)
            )
            if handler_addr is not None:
                memory.write32(entry.iat_addr, handler_addr)
                patched_count += 1
        logger.info("loader", f"Patched {patched_count}/{len(self._dll_iat_entries)} DLL IAT entries with stubs")

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
