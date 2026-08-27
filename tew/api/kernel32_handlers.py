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
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory
    from tew.loader.dll_loader import DLLLoader

from tew.hardware.cpu_zig import EAX, ESP
from tew.api.win32_handlers import (
    Win32Handlers, cleanup_stdcall, DLLMAIN_TRAMPOLINE, DLLMAIN_HANDLE_STORE,
    unimplemented_halt as _halt,
)
from tew.api._state import CRTState, DynamicModule, find_file_ci, read_cstring, read_wide_string
from tew.api.user32_handlers import _invoke_emulated_proc, _get_dialog_sentinel
from tew.logger import logger


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


def _invoke_dependency_dllmain(
    cpu: "CPU", memory: "Memory", state: CRTState, loaded,
) -> bool:
    """Synchronously run a dependency DLL's own DllMain(DLL_PROCESS_ATTACH).

    Returns True if DllMain returned TRUE (or the DLL has no entry point at
    all, so there's nothing to check), False if it explicitly returned
    FALSE -- callers must not silently treat a FALSE return as success
    (real LoadLibrary treats it as a load failure); see ole32_handlers.py's
    _ensure_dll_ready for the same contract on its own DllMain call.

    dll_loader.load_dll() recursively loads a DLL's own PE-import
    dependencies but never runs their DllMain -- only the top-level
    LoadLibraryA path does that (via _load_dll_with_dllmain above, an async
    stack-trampoline trick appropriate for redirecting the *current*
    thread's own next instruction). A dependency loaded mid-recursion has
    no such "current thread about to resume" hook to detour through, so
    this uses the existing synchronous nested-call mechanism instead
    (_invoke_emulated_proc, already battle-tested for calling a real loaded
    DLL's real DllMain -- see its own docstring/comments for the
    thread-scheduler pitfalls already worked out there).

    Real, confirmed bug this fixes: msjint35.dll (pulled in only as
    msjter35.dll's own PE-import dependency) never ran its CRT startup, so
    its "my own HINSTANCE" global stayed at its zero default, and its own
    exported code later handed that NULL to LoadStringA, silently failing
    every Jet error-message resource lookup.
    """
    handle = loaded.base_address & 0xFFFFFFFF
    sentinel = _get_dialog_sentinel(state, memory)
    logger.debug("handlers",
        f"[dependency-dllmain] invoking DllMain @ 0x{loaded.entry_point:x} for {loaded.name}")
    result = _invoke_emulated_proc(
        cpu, memory, loaded.entry_point,
        [handle, 1, 0],  # hinstDLL, DLL_PROCESS_ATTACH, lpvReserved
        sentinel,
        # Default max_steps=5_000_000 was too small here: with real
        # cooperative threads (timers etc.) running while this thread is
        # swapped out, the whole budget was routinely exhausted before this
        # thread ever got back to finish its own call -- see
        # ole32_handlers.py's _ensure_dll_ready, which hit and fixed the
        # exact same issue on its own DllMain call.
        max_steps=50_000_000,
        scheduler=state.scheduler,
    )
    logger.debug("handlers",
        f"[dependency-dllmain] {loaded.name}'s DllMain returned {result}")
    if result == 0:
        logger.error("handlers",
            f"[dependency-dllmain] {loaded.name}: DllMain(DLL_PROCESS_ATTACH) "
            "returned FALSE -- treating as a failed load")
        return False
    return True


def register_kernel32_handlers(
    stubs: Win32Handlers,
    memory: "Memory",
    state: CRTState,
    dll_loader: Optional["DLLLoader"] = None,
) -> None:
    """Register all kernel32.dll handlers."""

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
            proc_name = f"Ordinal #{name_ptr}"
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

    def _normalized_dll_name(name: str) -> str:
        """Lowercase, always ".dll"-suffixed module name for DynamicModule
        caching -- must match get_stub_dll_handle/register_handler's own
        normalization exactly. A bare name like "kernel32" (real Windows
        accepts LoadLibraryA/GetModuleHandleA without the extension) cached
        verbatim here permanently shadows the correct, suffixed lookup for
        every later GetProcAddress(hModule, ...) call using this same
        handle -- confirmed live: GetProcAddress("kernel32", ...) always
        returned NULL for real, registered handlers (e.g.
        IsProcessorFeaturePresent) once "kernel32" (no suffix) had been
        cached this way, even though "kernel32.dll!IsProcessorFeaturePresent"
        was a real, working registration the whole time.
        """
        norm = name.lower()
        if not norm.endswith(".dll"):
            norm += ".dll"
        return norm

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
                    loaded = dll_loader.load_dll(
                        basename, memory,
                        lambda dep: _invoke_dependency_dllmain(cpu, memory, state, dep))
                    if loaded:
                        dll_loader.patch_dll_iats(memory, stubs)
                        handle = loaded.base_address & 0xFFFFFFFF
                        state.dynamic_modules[handle] = DynamicModule(
                            dll_name=_normalized_dll_name(basename),
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
                basename = os.path.basename(name)
                stub_handle = stubs.get_stub_dll_handle(basename)
                if stub_handle is not None:
                    state.dynamic_modules[stub_handle] = DynamicModule(
                        dll_name=_normalized_dll_name(basename),
                        base_address=stub_handle,
                    )
                    logger.debug("handlers",
                        f'LoadLibraryA("{name}") -> 0x{stub_handle:x} (stub-only, path)')
                    cpu.regs[EAX] = stub_handle
                else:
                    logger.warn("handlers",
                        f'LoadLibraryA("{name}") -> NULL (not found: no real file, no handler coverage)')
                    cpu.regs[EAX] = 0
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
            loaded = dll_loader.load_dll(
                name, memory,
                lambda dep: _invoke_dependency_dllmain(cpu, memory, state, dep))
            if loaded:
                dll_loader.patch_dll_iats(memory, stubs)
                handle = loaded.base_address & 0xFFFFFFFF
                state.dynamic_modules[handle] = DynamicModule(
                    dll_name=_normalized_dll_name(name), base_address=loaded.base_address)
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
        stub_handle = stubs.get_stub_dll_handle(name)
        if stub_handle is not None:
            state.dynamic_modules[stub_handle] = DynamicModule(
                dll_name=_normalized_dll_name(name), base_address=stub_handle)
            logger.debug("handlers", f'LoadLibraryA("{name}") -> 0x{stub_handle:x} (stub-only)')
            cpu.regs[EAX] = stub_handle
        else:
            logger.warn("handlers",
                f'LoadLibraryA("{name}") -> NULL (not found: no real file, no handler coverage)')
            cpu.regs[EAX] = 0
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

    # dwFlags bits that only narrow *where* the loader searches for the DLL
    # (a real-Windows DLL-planting defense) -- irrelevant here since
    # dll_loader's own search path list is fixed and not attacker-influenced.
    # Safe to ignore and load exactly as LoadLibrary(Ex) with dwFlags=0 would.
    _LOAD_LIBRARY_SEARCH_FLAGS = (
        0x8       # LOAD_WITH_ALTERED_SEARCH_PATH
        | 0x10    # LOAD_IGNORE_CODE_AUTHZ_LEVEL
        | 0x200   # LOAD_LIBRARY_SEARCH_APPLICATION_DIR
        | 0x400   # LOAD_LIBRARY_SEARCH_USER_DIRS
        | 0x800   # LOAD_LIBRARY_SEARCH_SYSTEM32
        | 0x1000  # LOAD_LIBRARY_SEARCH_DEFAULT_DIRS
    )

    def _load_library_ex_common(name: str, dw_flags: int, cpu: "CPU", memory: "Memory",
                                caller_label: str) -> None:
        if dw_flags & ~_LOAD_LIBRARY_SEARCH_FLAGS:
            logger.error("handlers",
                f"[UNIMPLEMENTED] {caller_label} dwFlags=0x{dw_flags:x} — halting")
            cpu.halted = True
            cpu.fatal_halt = True
            return
        if dw_flags:
            logger.debug("handlers",
                f"{caller_label}: ignoring search-scope-only dwFlags=0x{dw_flags:x}")
        has_sep = "\\" in name or "/" in name
        if has_sep:
            _load_dll_by_path(name, 12, cpu, memory)
        else:
            _load_dll_by_name(name, 12, cpu, memory)

    def _load_library_ex_a(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        name = read_cstring(name_ptr, memory) if name_ptr else ""
        _load_library_ex_common(name, dw_flags, cpu, memory, "LoadLibraryExA")

    def _load_library_ex_w(cpu: "CPU") -> None:
        name_ptr = memory.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_flags = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        name = read_wide_string(name_ptr, memory) if name_ptr else ""
        _load_library_ex_common(name, dw_flags, cpu, memory, "LoadLibraryExW")

    def _free_library(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    def _disable_thread_lib(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("kernel32.dll", "LoadLibraryA",              _load_library_a)
    stubs.register_handler("kernel32.dll", "LoadLibraryExA",            _load_library_ex_a)
    stubs.register_handler("kernel32.dll", "LoadLibraryExW",            _load_library_ex_w)
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
