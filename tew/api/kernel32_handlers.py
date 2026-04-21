"""kernel32.dll handlers — module handles, GetProcAddress, LoadLibrary, and orchestration.

Delegates to sub-modules:
  kernel32_memory.py  — heap and virtual memory
  kernel32_sync.py    — critical sections and TLS
  kernel32_locale.py  — code pages, locale, string conversion
  kernel32_system.py  — version, time, process info, environment, Sleep scheduler
  kernel32_io.py      — file I/O, synchronization objects, threading
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory
    from tew.loader.dll_loader import DLLLoader

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall, DLLMAIN_TRAMPOLINE, DLLMAIN_HANDLE_STORE
from tew.api._state import CRTState, DynamicModule, find_file_ci, read_cstring, read_wide_string
from tew.logger import logger


def _fake_dll_handle(name: str) -> int:
    """Compute a fake module handle from the DLL name (hash-based)."""
    h = 0
    for ch in name.lower():
        h = ((h << 5) - h + ord(ch)) & 0xFFFFFFFF
    return (h & 0x7FFFFFFF) | 0x10000000


def _load_dll_with_dllmain(
    cpu: "CPU", memory: "Memory", stubs: Win32Handlers,
    state: CRTState, dll_loader, loaded, handle: int, arg_bytes: int,
) -> None:
    """If DLL has an entry point, invoke DllMain via stack trick; otherwise just return handle."""
    if loaded.entry_point != 0:
        logger.debug("handlers", f"LoadLibraryA: invoking DllMain @ 0x{loaded.entry_point:x}")
        memory.write32(DLLMAIN_HANDLE_STORE, handle)
        cleanup_stdcall(cpu, memory, arg_bytes)
        cpu.regs[ESP] = (cpu.regs[ESP] - 20) & 0xFFFFFFFF
        memory.write32(cpu.regs[ESP] + 0,  loaded.entry_point)
        memory.write32(cpu.regs[ESP] + 4,  DLLMAIN_TRAMPOLINE)
        memory.write32(cpu.regs[ESP] + 8,  handle)
        memory.write32(cpu.regs[ESP] + 12, 1)   # DLL_PROCESS_ATTACH
        memory.write32(cpu.regs[ESP] + 16, 0)   # lpReserved
    else:
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, arg_bytes)


def register_kernel32_handlers(
    stubs: Win32Handlers,
    memory: "Memory",
    state: CRTState,
    dll_loader: Optional["DLLLoader"] = None,
) -> None:
    """Register all kernel32.dll handlers."""

    def _halt(name: str):
        def _h(cpu: "CPU") -> None:
            logger.error("handlers", f"[UNIMPLEMENTED] {name} — halting")
            cpu.halted = True
        return _h

    # ── Module handles ────────────────────────────────────────────────────────

    def _resolve_module_handle(name: str) -> int:
        """Resolve a module name to a handle (base address).

        Resolution order:
          1. NULL name → main exe image base.
          2. Name ends in .exe → main exe image base.
          3. Loaded real DLL (from disk) → its base_address.
          4. Stub-only system DLL (kernel32, user32, etc.) → first handler address.
          5. Unresolvable → 0 (caller will warn).
        """
        if not name:
            return 0x00400000
        lower = name.lower()
        if lower.endswith(".exe"):
            return 0x00400000
        if dll_loader:
            canonical = lower.rstrip(".dll").rstrip(".") + ".dll"
            dll = dll_loader.get_dll(name) or dll_loader.get_dll(canonical)
            if dll:
                return dll.base_address
        stub_handle = stubs.get_stub_dll_handle(name)
        if stub_handle is not None:
            return stub_handle
        return 0

    def _get_module_handle_a(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        name = read_cstring(lp, memory) if lp != 0 else ""
        handle = _resolve_module_handle(name)
        if handle:
            logger.debug("handlers", f'GetModuleHandleA("{name}") -> 0x{handle:08x}')
        else:
            logger.warn("handlers", f'GetModuleHandleA("{name}") -> NULL (not loaded)')
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, 4)

    def _get_module_handle_w(cpu: "CPU") -> None:
        lp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        name = read_wide_string(lp, memory) if lp != 0 else ""
        handle = _resolve_module_handle(name)
        if handle:
            logger.debug("handlers", f'GetModuleHandleW("{name}") -> 0x{handle:08x}')
        else:
            logger.warn("handlers", f'GetModuleHandleW("{name}") -> NULL (not loaded)')
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "GetModuleHandleA", _get_module_handle_a)
    stubs.register_handler("kernel32.dll", "GetModuleHandleW", _get_module_handle_w)

    # ── GetProcAddress ────────────────────────────────────────────────────────

    def _get_proc_address(cpu: "CPU") -> None:
        h_module  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        name_ptr  = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        proc_name: str
        if (name_ptr & 0xFFFF0000) == 0:
            proc_name = f"ordinal#{name_ptr}"
        else:
            proc_name = read_cstring(name_ptr, memory)

        dll_name: Optional[str] = None
        if h_module == 0x00400000:
            dll_name = None
        else:
            dyn = state.dynamic_modules.get(h_module)
            if dyn:
                dll_name = dyn.dll_name
            elif dll_loader:
                loaded = dll_loader.find_dll_for_address(h_module)
                if loaded:
                    dll_name = loaded.name
            if dll_name is None:
                dll_name = stubs.get_dll_name_for_stub_handle(h_module)

        if dll_name is None:
            logger.warn("handlers",
                f'GetProcAddress(0x{h_module:x}, "{proc_name}") -> NULL (unknown module)')
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 8)
            return

        handler_addr = stubs.lookup_handler_address(dll_name, proc_name)
        if handler_addr:
            logger.debug("handlers",
                f'GetProcAddress("{dll_name}", "{proc_name}") -> 0x{handler_addr:x} [handler]')
            cpu.regs[EAX] = handler_addr
            cleanup_stdcall(cpu, memory, 8)
            return

        if dll_loader:
            export_addr = dll_loader.get_export_address(dll_name, proc_name)
            if export_addr:
                logger.debug("handlers",
                    f'GetProcAddress("{dll_name}", "{proc_name}") -> 0x{export_addr:x} [export]')
                cpu.regs[EAX] = export_addr
                cleanup_stdcall(cpu, memory, 8)
                return

        logger.warn("handlers",
            f'GetProcAddress("{dll_name}", "{proc_name}") -> NULL (not found)')
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("kernel32.dll", "GetProcAddress", _get_proc_address)

    # ── LoadLibrary ───────────────────────────────────────────────────────────

    def _load_dll_by_path(name: str, arg_bytes: int,
                          cpu: "CPU", memory: "Memory") -> bool:
        """Try to load a path-based DLL. Returns True if handled (caller should return)."""
        linux_path = state.translate_windows_path(name)
        while True:
            real_path = find_file_ci(linux_path)
            if real_path is not None:
                if dll_loader:
                    basename = os.path.basename(real_path)
                    dll_loader.add_search_path(os.path.dirname(real_path))
                    was_loaded = dll_loader.get_dll(basename) is not None
                    loaded = dll_loader.load_dll(basename, memory)
                    if loaded:
                        dll_loader.patch_dll_iats(memory, stubs)
                        handle = loaded.base_address & 0xFFFFFFFF
                        state.dynamic_modules[handle] = DynamicModule(
                            dll_name=basename.lower(),
                            base_address=loaded.base_address,
                        )
                        logger.info("handlers",
                            f'LoadLibraryA("{name}") -> 0x{handle:x} '
                            f'(loaded at 0x{loaded.base_address:x})')
                        if not was_loaded and basename.lower() == "authlogin.dll":
                            # authlogin.dll ships its own MSVC SBH (small-block heap).
                            # The allocator at offset 0xca1e cannot run because our
                            # HeapCreate/HeapAlloc stubs do not set up the SBH metadata
                            # it expects.  Replace it with simple_alloc so the rest of
                            # the DLL (TLS init, critical sections, etc.) can run
                            # normally without any other patches.
                            base = loaded.base_address
                            def _authlogin_alloc(cpu: "CPU") -> None:
                                sz = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
                                cpu.regs[EAX] = state.simple_alloc(sz or 1)
                                # __cdecl: caller cleans the stack — no cleanup_stdcall
                            stubs.patch_address(base + 0xca1e, "authlogin_heapAlloc",
                                                _authlogin_alloc)
                        if not was_loaded and loaded.entry_point != 0:
                            _load_dll_with_dllmain(cpu, memory, stubs, state,
                                                   dll_loader, loaded, handle, arg_bytes)
                            return True
                        cpu.regs[EAX] = handle
                        cleanup_stdcall(cpu, memory, arg_bytes)
                        return True
                fh = _fake_dll_handle(os.path.basename(name))
                state.dynamic_modules[fh] = DynamicModule(
                    dll_name=os.path.basename(name).lower(),
                    base_address=fh,
                )
                logger.debug("handlers",
                    f'LoadLibraryA("{name}") -> 0x{fh:x} (stub-only, path)')
                cpu.regs[EAX] = fh
                cleanup_stdcall(cpu, memory, arg_bytes)
                return True
            if not state.config.interactive_on_missing_file:
                logger.warn("handlers",
                    f'LoadLibraryA("{name}") -> NULL (not found: {linux_path})')
                cpu.regs[EAX] = 0
                cleanup_stdcall(cpu, memory, arg_bytes)
                return True
            print(f"\n[LoadLibrary] DLL not found: {linux_path}")
            print("  Add the file then press Enter to retry, or 'c' to skip.")
            ans = input("  > ").strip().lower()
            if ans != "c":
                linux_path = state.translate_windows_path(name)
                continue
            logger.debug("handlers", f'LoadLibraryA("{name}") -> NULL (user skipped)')
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, arg_bytes)
            return True

    def _load_dll_by_name(name: str, arg_bytes: int,
                          cpu: "CPU", memory: "Memory") -> None:
        """Try to load a name-only DLL (no path separator)."""
        if dll_loader:
            was_loaded = dll_loader.get_dll(name) is not None
            loaded = dll_loader.load_dll(name, memory)
            if loaded:
                dll_loader.patch_dll_iats(memory, stubs)
                handle = loaded.base_address & 0xFFFFFFFF
                state.dynamic_modules[handle] = DynamicModule(
                    dll_name=name.lower(), base_address=loaded.base_address)
                logger.info("handlers",
                    f'LoadLibraryA("{name}") -> 0x{handle:x} '
                    f'(loaded at 0x{loaded.base_address:x})')
                if not was_loaded and loaded.entry_point != 0:
                    _load_dll_with_dllmain(cpu, memory, stubs, state,
                                           dll_loader, loaded, handle, arg_bytes)
                    return
                cpu.regs[EAX] = handle
                cleanup_stdcall(cpu, memory, arg_bytes)
                return
        fh = _fake_dll_handle(name)
        state.dynamic_modules[fh] = DynamicModule(
            dll_name=name.lower(), base_address=fh)
        logger.debug("handlers", f'LoadLibraryA("{name}") -> 0x{fh:x} (stub-only)')
        cpu.regs[EAX] = fh
        cleanup_stdcall(cpu, memory, arg_bytes)

    def _load_library_a(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory)
        if (name.startswith("\\") or name.startswith("/")) and \
                not (len(name) > 1 and name[1] == ':'):
            name = "C:" + name
        has_sep = "\\" in name or "/" in name
        if has_sep:
            _load_dll_by_path(name, 4, cpu, memory)
        else:
            _load_dll_by_name(name, 4, cpu, memory)

    def _load_library_ex_a(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if dw_flags != 0:
            logger.error("handlers",
                f"[UNIMPLEMENTED] LoadLibraryExA dwFlags=0x{dw_flags:x} — halting")
            cpu.halted = True
            return
        name = read_cstring(name_ptr, memory) if name_ptr else ""
        has_sep = "\\" in name or "/" in name
        if has_sep:
            _load_dll_by_path(name, 12, cpu, memory)
        else:
            _load_dll_by_name(name, 12, cpu, memory)

    def _free_library(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    def _disable_thread_lib(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "LoadLibraryA",              _load_library_a)
    stubs.register_handler("kernel32.dll", "LoadLibraryExA",            _load_library_ex_a)
    stubs.register_handler("kernel32.dll", "FreeLibrary",               _free_library)
    stubs.register_handler("kernel32.dll", "DisableThreadLibraryCalls", _disable_thread_lib)

    # ── Delegate to sub-modules ───────────────────────────────────────────────

    from tew.api.kernel32_memory import register_kernel32_memory_handlers
    from tew.api.kernel32_sync   import register_kernel32_sync_handlers
    from tew.api.kernel32_locale import register_kernel32_locale_handlers
    from tew.api.kernel32_system import register_kernel32_system_handlers
    from tew.api.kernel32_io     import register_kernel32_io_handlers

    register_kernel32_memory_handlers(stubs, memory, state)
    register_kernel32_sync_handlers(stubs, memory, state)
    register_kernel32_locale_handlers(stubs, memory, state)
    register_kernel32_system_handlers(stubs, memory, state)
    register_kernel32_io_handlers(stubs, memory, state, dll_loader)
