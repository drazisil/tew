"""Import resolver — builds and populates the Import Address Table."""

from __future__ import annotations
from typing import Callable, TYPE_CHECKING

from tew.loader.dll_loader import DLLLoader, LoadedDLL, patch_iat_entry, should_invoke_dependency_dllmain
from tew.logger import logger

if TYPE_CHECKING:
    from tew.hardware.memory import Memory
    from tew.pe.import_table import ImportTable
    from tew.api.win32_handlers import Win32Handlers


class ImportResolver:
    def __init__(self, dll_search_paths: list[str]) -> None:
        self._dll_loader = DLLLoader(dll_search_paths)
        # iat_rva -> {dll_name, function_name, real_addr}
        self._iat_map: dict[int, dict] = {}
        self._memory: "Memory | None" = None

    def set_memory(self, memory: "Memory") -> None:
        self._memory = memory

    def build_iat_map(
        self,
        import_table: "ImportTable | None",
        image_base: int,
        on_dependency_loaded: "Callable[[LoadedDLL], None] | None" = None,
    ) -> None:
        """Resolve the main EXE's own direct imports.

        on_dependency_loaded, if given, is invoked for each of these
        directly-imported DLLs (and, via load_dll's own recursive descent,
        any real-DLL dependencies of theirs) the first time it's loaded --
        mirroring exactly how load_dll already handles a DLL's own PE-import
        dependencies (should_invoke_dependency_dllmain), so a dependency
        always gets the callback before the DLL that depends on it. Passing
        None here (the old default) reproduces the pre-fix behavior exactly:
        these DLLs get mapped and their exports resolved, but never run
        their own DllMain.
        """
        if not import_table or not self._memory:
            return

        for descriptor in import_table.descriptors:
            dll_name = descriptor.dll_name.lower()
            dep_was_loaded = self._dll_loader.get_dll(dll_name) is not None
            loaded_dll = self._dll_loader.load_dll(descriptor.dll_name, self._memory, on_dependency_loaded)

            if should_invoke_dependency_dllmain(dep_was_loaded, loaded_dll, on_dependency_loaded):
                on_dependency_loaded(loaded_dll)

            for entry in descriptor.entries:
                real_addr: int | None = None
                if loaded_dll:
                    real_addr = loaded_dll.exports.get(entry.name)

                self._iat_map[entry.iat_rva] = {
                    "dll_name": dll_name,
                    "function_name": entry.name,
                    "real_addr": real_addr,
                }

                if real_addr:
                    logger.trace("loader", f"{dll_name}!{entry.name} => 0x{real_addr:08x}")

        logger.info("loader", f"Built IAT map with {len(self._iat_map)} imports")

    def write_iat_handlers(
        self,
        memory: "Memory",
        image_base: int,
        import_table: "ImportTable | None",
        win32_handlers: "Win32Handlers | None" = None,
    ) -> None:
        if not import_table:
            return

        handler_count = 0
        real_count = 0
        auto_handler_count = 0

        for descriptor in import_table.descriptors:
            for entry in descriptor.entries:
                map_entry = self._iat_map.get(entry.iat_rva)
                if not map_entry:
                    continue

                iat_addr = image_base + entry.iat_rva

                if win32_handlers is None:
                    if map_entry["real_addr"]:
                        memory.write32(iat_addr, map_entry["real_addr"])
                        real_count += 1
                    continue

                outcome = patch_iat_entry(
                    memory, win32_handlers, iat_addr,
                    map_entry["dll_name"], map_entry["function_name"],
                    real_addr=map_entry["real_addr"],
                )
                if outcome == "handler":
                    handler_count += 1
                elif outcome == "real":
                    real_count += 1
                else:
                    auto_handler_count += 1

        logger.info(
            "loader",
            f"IAT written: {handler_count} stubs, {real_count} real DLL, {auto_handler_count} auto-stubs (unimplemented)",
        )

        if win32_handlers:
            self._dll_loader.patch_dll_iats(memory, win32_handlers)
            self._dll_loader.patch_dll_exports(memory, win32_handlers)

    def get_dll_search_paths(self) -> list[str]:
        return list(self._dll_loader._search_paths)

    def add_dll_search_path(self, path: str) -> None:
        self._dll_loader.add_search_path(path)

    def get_dll_loader(self) -> DLLLoader:
        return self._dll_loader

    def find_dll_for_address(self, address: int) -> dict | None:
        dll = self._dll_loader.find_dll_for_address(address)
        if dll is None:
            return None
        return {"name": dll.name, "base_address": dll.base_address, "size": dll.size}

    def is_in_dll_range(self, address: int) -> bool:
        return self._dll_loader.is_in_dll_range(address)

    def get_address_mappings(self) -> list[dict]:
        return self._dll_loader.get_address_mappings()
