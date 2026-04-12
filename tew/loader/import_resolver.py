"""Import resolver — builds and populates the Import Address Table."""

from __future__ import annotations
from typing import TYPE_CHECKING

from tew.loader.dll_loader import DLLLoader, LoadedDLL
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

    def build_iat_map(self, import_table: "ImportTable | None", image_base: int) -> None:
        if not import_table or not self._memory:
            return

        for descriptor in import_table.descriptors:
            dll_name = descriptor.dll_name.lower()
            loaded_dll = self._dll_loader.load_dll(descriptor.dll_name, self._memory)

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

                handler_addr: int | None = None
                if win32_handlers:
                    handler_addr = (
                        win32_handlers.get_handler_address(map_entry["dll_name"] + ".dll", map_entry["function_name"])
                        or win32_handlers.get_handler_address(map_entry["dll_name"], map_entry["function_name"])
                    )

                if handler_addr is not None:
                    memory.write32(iat_addr, handler_addr)
                    handler_count += 1
                elif map_entry["real_addr"]:
                    memory.write32(iat_addr, map_entry["real_addr"])
                    real_count += 1
                elif win32_handlers:
                    dll_name = map_entry["dll_name"]
                    func_name = map_entry["function_name"]

                    def _make_unimplemented_handler(dn: str, fn: str):
                        def _handler(cpu):
                            logger.error("handlers", f"[UNIMPLEMENTED] {dn}!{fn} — halting")
                            logger.error(
                                "cpu",
                                f"  EIP=0x{(cpu.eip) & 0xFFFFFFFF:08x}  "
                                f"EAX=0x{cpu.regs[0] & 0xFFFFFFFF:08x}  "
                                f"ECX=0x{cpu.regs[1] & 0xFFFFFFFF:08x}  "
                                f"ESP=0x{cpu.regs[4] & 0xFFFFFFFF:08x}  "
                                f"EBP=0x{cpu.regs[5] & 0xFFFFFFFF:08x}",
                            )
                            cpu.halted = True
                        return _handler

                    win32_handlers.register_handler(dll_name, func_name, _make_unimplemented_handler(dll_name, func_name))
                    auto_addr = (
                        win32_handlers.get_handler_address(dll_name, func_name)
                        or win32_handlers.get_handler_address(dll_name + ".dll", func_name)
                    )
                    if auto_addr is not None:
                        memory.write32(iat_addr, auto_addr)
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
