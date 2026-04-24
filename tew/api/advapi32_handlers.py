"""advapi32.dll, winmm.dll, and shell32.dll handler registrations.

Ported from Win32Handlers.ts lines 5709–6634.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall, pending_timers, PendingTimer
from tew.api._state import CRTState, RegistryEntry, save_registry_json
from tew.logger import logger

# ── Win32 error constants ────────────────────────────────────────────────────

ERROR_SUCCESS        = 0
ERROR_FILE_NOT_FOUND = 2
ERROR_MORE_DATA      = 234
ERROR_NO_MORE_ITEMS  = 259

# ── Registry key handle allocator (module-level, shared across calls) ────────

_next_reg_key: int = 0xBEEF0200

# Pre-seed predefined root handles with empty-string names so that
# subkeys built from them ("" + "\\Software\\...") collapse to just the
# subkey path, matching what registry.json stores.  All predefined HKEY_*
# roots share the same flat namespace in this single-process emulator.
_reg_key_names: dict[int, str] = {
    0x80000000: "hkcr",  # HKEY_CLASSES_ROOT
    0x80000001: "hkcu",  # HKEY_CURRENT_USER
    0x80000002: "hklm",  # HKEY_LOCAL_MACHINE
    0x80000003: "hku",   # HKEY_USERS
    0x80000004: "hkpd",  # HKEY_PERFORMANCE_DATA
    0x80000005: "hkcc",  # HKEY_CURRENT_CONFIG
    0x80000006: "hkdd",  # HKEY_DYN_DATA
}


def _read_ansi_str(ptr: int, memory: "Memory", max_len: int = 256) -> str:
    s = []
    for i in range(max_len):
        c = memory.read8(ptr + i)
        if c == 0:
            break
        s.append(chr(c))
    return "".join(s)


def _read_wide_str(ptr: int, memory: "Memory", max_chars: int = 256) -> str:
    """Read a null-terminated UTF-16LE string from emulator memory."""
    s = []
    for i in range(max_chars):
        lo = memory.read8(ptr + i * 2)
        hi = memory.read8(ptr + i * 2 + 1)
        cp = lo | (hi << 8)
        if cp == 0:
            break
        s.append(chr(cp))
    return "".join(s)


def _write_wide_str(ptr: int, s: str, memory: "Memory") -> None:
    """Write a null-terminated UTF-16LE string into emulator memory."""
    for i, ch in enumerate(s):
        cp = ord(ch)
        memory.write8(ptr + i * 2,     cp & 0xFF)
        memory.write8(ptr + i * 2 + 1, (cp >> 8) & 0xFF)
    # null terminator
    memory.write8(ptr + len(s) * 2,     0)
    memory.write8(ptr + len(s) * 2 + 1, 0)


def _write_ansi_str(ptr: int, s: str, memory: "Memory") -> None:
    for i, ch in enumerate(s):
        memory.write8(ptr + i, ord(ch))
    memory.write8(ptr + len(s), 0)


def _reg_build_path(h_key_in: int, sub_key: str) -> str:
    """Build a normalised full registry path from a parent handle and subkey string.

    Predefined root handles (HKLM etc.) are stored with empty-string names so
    their subkeys resolve to bare paths matching what registry.json stores.
    Unknown handles fall back to a ``HKEY:<hex>`` prefix rather than silently
    losing the parent.
    """
    parent = _reg_key_names.get(h_key_in)
    if parent is None:
        parent = f"hkey:{h_key_in:x}"
    sub = sub_key.lower().replace("/", "\\").strip("\\")
    if parent and sub:
        return f"{parent}\\{sub}"
    return parent or sub


def _reg_query_value(
    key_handle: int,
    value_name: str,
    state: "CRTState",
) -> "RegistryEntry | None":
    key_name = (_reg_key_names.get(key_handle) or "").lower()
    lower_value = value_name.lower()
    for pattern, values in state.registry_values.items():
        if key_name == pattern:
            v = values.get(lower_value)
            if v is not None:
                return v
    # key_handle == 0: global scan
    if key_handle == 0:
        for values in state.registry_values.values():
            v = values.get(lower_value)
            if v is not None:
                return v
    return None


# ── Timer ID counter ─────────────────────────────────────────────────────────

_next_timer_id: int = 1
TIME_PERIODIC = 1


# ── Registration entry point ─────────────────────────────────────────────────


def register_advapi32_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register all advapi32.dll (and winmm.dll) handlers."""

    global _next_reg_key, _next_timer_id

    # ── advapi32.dll: Registry API ────────────────────────────────────────────

    # RegOpenKeyA(hKey, lpSubKey, phkResult) - stdcall, 3 args (12 bytes)
    def _reg_open_key_a(cpu: "CPU") -> None:
        global _next_reg_key
        h_key_in   = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        lp_sub_key = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        ph_result  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        key_name   = _read_ansi_str(lp_sub_key, memory) if lp_sub_key else ""
        full_name  = _reg_build_path(h_key_in, key_name)
        logger.info("registry", f'RegOpenKeyA(parent=0x{h_key_in:x}, "{key_name}") -> "{full_name}"')
        handle = _next_reg_key
        _next_reg_key += 1
        _reg_key_names[handle] = full_name
        if ph_result:
            memory.write32(ph_result, handle)
        cpu.regs[EAX] = ERROR_SUCCESS
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("advapi32.dll", "RegOpenKeyA", _reg_open_key_a)

    # RegOpenKeyExA(hKey, lpSubKey, ulOptions, samDesired, phkResult) - 5 args (20 bytes)
    def _reg_open_key_ex_a(cpu: "CPU") -> None:
        global _next_reg_key
        h_key_in   = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        lp_sub_key = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        ph_result  = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        key_name   = _read_ansi_str(lp_sub_key, memory) if lp_sub_key else ""
        full_name  = _reg_build_path(h_key_in, key_name)
        logger.info(
            "registry",
            f'RegOpenKeyExA(parent=0x{h_key_in:x}, "{key_name}") -> "{full_name}"',
        )
        handle = _next_reg_key
        _next_reg_key += 1
        _reg_key_names[handle] = full_name
        if ph_result:
            memory.write32(ph_result, handle)
        cpu.regs[EAX] = ERROR_SUCCESS
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("advapi32.dll", "RegOpenKeyExA", _reg_open_key_ex_a)

    # RegOpenKeyExW(hKey, lpSubKey, ulOptions, samDesired, phkResult) - 5 args (20 bytes)
    def _reg_open_key_ex_w(cpu: "CPU") -> None:
        global _next_reg_key
        h_key_in   = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        lp_sub_key = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        ph_result  = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        key_name   = _read_wide_str(lp_sub_key, memory) if lp_sub_key else ""
        full_name  = _reg_build_path(h_key_in, key_name)
        logger.info(
            "registry",
            f'RegOpenKeyExW(parent=0x{h_key_in:x}, "{key_name}") -> "{full_name}"',
        )
        handle = _next_reg_key
        _next_reg_key += 1
        _reg_key_names[handle] = full_name
        if ph_result:
            memory.write32(ph_result, handle)
        cpu.regs[EAX] = ERROR_SUCCESS
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("advapi32.dll", "RegOpenKeyExW", _reg_open_key_ex_w)

    # RegCreateKeyA(hKey, lpSubKey, phkResult) - 3 args (12 bytes)
    def _reg_create_key_a(cpu: "CPU") -> None:
        global _next_reg_key
        h_key_in   = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        lp_sub_key = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        ph_result  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        key_name   = _read_ansi_str(lp_sub_key, memory) if lp_sub_key else ""
        full_name  = _reg_build_path(h_key_in, key_name)
        handle = _next_reg_key
        _next_reg_key += 1
        _reg_key_names[handle] = full_name
        if ph_result:
            memory.write32(ph_result, handle)
        logger.info("registry", f'RegCreateKeyA("{full_name}") -> 0x{handle:x}')
        cpu.regs[EAX] = ERROR_SUCCESS
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("advapi32.dll", "RegCreateKeyA", _reg_create_key_a)

    # RegCreateKeyExA(hKey, lpSubKey, Reserved, lpClass, dwOptions, samDesired,
    #                 lpSecurityAttributes, phkResult, lpdwDisposition) - 9 args (36 bytes)
    def _reg_create_key_ex_a(cpu: "CPU") -> None:
        global _next_reg_key
        h_key_in         = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        lp_sub_key       = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        ph_result        = memory.read32((cpu.regs[ESP] + 32) & 0xFFFFFFFF)  # 8th arg
        lpdw_disposition = memory.read32((cpu.regs[ESP] + 36) & 0xFFFFFFFF)  # 9th arg
        key_name  = _read_ansi_str(lp_sub_key, memory) if lp_sub_key else ""
        full_name = _reg_build_path(h_key_in, key_name)
        handle = _next_reg_key
        _next_reg_key += 1
        _reg_key_names[handle] = full_name
        if ph_result:
            memory.write32(ph_result, handle)
        if lpdw_disposition:
            memory.write32(lpdw_disposition, 2)  # REG_CREATED_NEW_KEY
        logger.info("registry", f'RegCreateKeyExA("{full_name}") -> 0x{handle:x}')
        cpu.regs[EAX] = ERROR_SUCCESS
        cleanup_stdcall(cpu, memory, 36)

    stubs.register_handler("advapi32.dll", "RegCreateKeyExA", _reg_create_key_ex_a)

    # RegQueryValueA(hKey, lpSubKey, lpValue, lpcbValue) - 4 args (16 bytes)
    # Retrieves the default (unnamed) value of hKey\lpSubKey (or hKey if lpSubKey
    # is NULL). Data type must be REG_SZ. lpcbValue is buffer size in/out in bytes.
    def _reg_query_value_a(cpu: "CPU") -> None:
        h_key      = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        lp_sub_key = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        lp_value   = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lpcb_value = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        if lp_sub_key:
            sub = _read_ansi_str(lp_sub_key, memory)
            full_path = _reg_build_path(h_key, sub)
            # Create a temporary handle for the subkey lookup
            temp_handle = _next_reg_key
            _reg_key_names[temp_handle] = full_path
            lookup_handle = temp_handle
        else:
            lookup_handle = h_key
            full_path = _reg_key_names.get(h_key, "")
        entry = _reg_query_value(lookup_handle, "", state)
        logger.info(
            "registry",
            f'RegQueryValueA(key="{full_path}", "") -> '
            f'{repr(entry.value) if entry else "NOT FOUND"}',
        )
        if entry is None or entry.type != 1:
            cpu.regs[EAX] = ERROR_FILE_NOT_FOUND
        else:
            s = str(entry.value)
            needed = len(s) + 1
            if lpcb_value:
                cb = memory.read32(lpcb_value)
                memory.write32(lpcb_value, needed)
                if lp_value and cb >= needed:
                    _write_ansi_str(lp_value, s, memory)
            cpu.regs[EAX] = ERROR_SUCCESS
        cleanup_stdcall(cpu, memory, 16)

    stubs.register_handler("advapi32.dll", "RegQueryValueA", _reg_query_value_a)

    # RegQueryValueExA(hKey, lpValueName, lpReserved, lpType, lpData, lpcbData) - 6 args (24 bytes)
    def _reg_query_value_ex_a(cpu: "CPU") -> None:
        h_key        = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        lp_val_name  = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        lp_type      = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_data      = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        lpcb_data    = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        value_name   = _read_ansi_str(lp_val_name, memory) if lp_val_name else ""
        entry        = _reg_query_value(h_key, value_name, state)
        logger.info(
            "registry",
            f'RegQueryValueExA(key=0x{h_key:x}, "{value_name}") -> '
            f'{repr(entry.value) if entry else "NOT FOUND"}',
        )
        if entry is None:
            cpu.regs[EAX] = ERROR_FILE_NOT_FOUND
        elif entry.type == 4:  # REG_DWORD
            needed = 4
            if lpcb_data:
                cb_data = memory.read32(lpcb_data)
                memory.write32(lpcb_data, needed)
                if lp_data and cb_data >= needed:
                    memory.write32(lp_data, entry.value)  # type: ignore[arg-type]
            if lp_type:
                memory.write32(lp_type, 4)
            cpu.regs[EAX] = ERROR_SUCCESS
        else:  # REG_SZ (type 1)
            s = entry.value  # type: ignore[assignment]
            needed = len(s) + 1
            if lpcb_data:
                cb_data = memory.read32(lpcb_data)
                memory.write32(lpcb_data, needed)
                if lp_data and cb_data >= needed:
                    _write_ansi_str(lp_data, s, memory)
            if lp_type:
                memory.write32(lp_type, 1)
            cpu.regs[EAX] = ERROR_SUCCESS
        cleanup_stdcall(cpu, memory, 24)

    stubs.register_handler("advapi32.dll", "RegQueryValueExA", _reg_query_value_ex_a)

    # RegQueryValueExW(hKey, lpValueName, lpReserved, lpType, lpData, lpcbData) - 6 args (24 bytes)
    # Same as RegQueryValueExA but value name is UTF-16LE and REG_SZ output is UTF-16LE.
    # lpcbData is in bytes (each char = 2 bytes, plus 2-byte null terminator).
    def _reg_query_value_ex_w(cpu: "CPU") -> None:
        h_key       = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        lp_val_name = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        lp_type     = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_data     = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        lpcb_data   = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        value_name  = _read_wide_str(lp_val_name, memory) if lp_val_name else ""
        entry       = _reg_query_value(h_key, value_name, state)
        logger.info(
            "registry",
            f'RegQueryValueExW(key=0x{h_key:x}, "{value_name}") -> '
            f'{repr(entry.value) if entry else "NOT FOUND"}',
        )
        if entry is None:
            cpu.regs[EAX] = ERROR_FILE_NOT_FOUND
        elif entry.type == 4:  # REG_DWORD
            needed = 4
            if lpcb_data:
                cb = memory.read32(lpcb_data)
                memory.write32(lpcb_data, needed)
                if lp_data and cb >= needed:
                    memory.write32(lp_data, entry.value)  # type: ignore[arg-type]
            if lp_type:
                memory.write32(lp_type, 4)
            cpu.regs[EAX] = ERROR_SUCCESS
        else:  # REG_SZ — output as UTF-16LE
            s = str(entry.value)
            needed = (len(s) + 1) * 2  # bytes including wide null terminator
            if lpcb_data:
                cb = memory.read32(lpcb_data)
                memory.write32(lpcb_data, needed)
                if lp_data and cb >= needed:
                    _write_wide_str(lp_data, s, memory)
            if lp_type:
                memory.write32(lp_type, 1)
            cpu.regs[EAX] = ERROR_SUCCESS
        cleanup_stdcall(cpu, memory, 24)

    stubs.register_handler("advapi32.dll", "RegQueryValueExW", _reg_query_value_ex_w)

    # RegSetValueExA(hKey, lpValueName, Reserved, dwType, lpData, cbData) - 6 args (24 bytes)
    def _reg_set_value_ex_a(cpu: "CPU") -> None:
        h_key      = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        lp_val     = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        dw_type    = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_data    = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        cb_data    = memory.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        value_name = _read_ansi_str(lp_val, memory) if lp_val else ""
        key_name   = _reg_key_names.get(h_key, "").lower()
        if dw_type == 4:  # REG_DWORD
            value: str | int = memory.read32(lp_data & 0xFFFFFFFF) if lp_data else 0
        else:  # REG_SZ and others — store as string
            raw = []
            for i in range(min(cb_data, 512)):
                c = memory.read8((lp_data + i) & 0xFFFFFFFF)
                if c == 0:
                    break
                raw.append(chr(c))
            value = "".join(raw)
        if key_name not in state.registry_values:
            state.registry_values[key_name] = {}
        state.registry_values[key_name][value_name.lower()] = RegistryEntry(type=dw_type, value=value)
        logger.info("registry",
            f'RegSetValueExA("{key_name}", "{value_name}") = {repr(value)}')
        save_registry_json(state.registry_values)
        cpu.regs[EAX] = ERROR_SUCCESS
        cleanup_stdcall(cpu, memory, 24)

    stubs.register_handler("advapi32.dll", "RegSetValueExA", _reg_set_value_ex_a)

    # RegDeleteValueA(hKey, lpValueName) - 2 args (8 bytes)
    def _reg_delete_value_a(cpu: "CPU") -> None:
        h_key      = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_val     = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        value_name = _read_ansi_str(lp_val, memory).lower() if lp_val else ""
        key_name   = _reg_key_names.get(h_key, "").lower()
        key_map    = state.registry_values.get(key_name)
        if key_map is not None and value_name in key_map:
            del key_map[value_name]
            logger.info("registry", f'RegDeleteValueA("{key_name}", "{value_name}") -> OK')
            save_registry_json(state.registry_values)
            cpu.regs[EAX] = ERROR_SUCCESS
        else:
            logger.info("registry",
                f'RegDeleteValueA("{key_name}", "{value_name}") -> NOT FOUND')
            cpu.regs[EAX] = ERROR_FILE_NOT_FOUND
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("advapi32.dll", "RegDeleteValueA", _reg_delete_value_a)

    # RegCloseKey(hKey) - 1 arg (4 bytes)
    def _reg_close_key(cpu: "CPU") -> None:
        cpu.regs[EAX] = ERROR_SUCCESS
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("advapi32.dll", "RegCloseKey", _reg_close_key)

    # RegFlushKey(hKey) - 1 arg (4 bytes)
    def _reg_flush_key(cpu: "CPU") -> None:
        cpu.regs[EAX] = ERROR_SUCCESS
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("advapi32.dll", "RegFlushKey", _reg_flush_key)

    # RegEnumKeyExA(hKey, dwIndex, lpName, lpcchName, lpReserved, lpClass, lpcchClass,
    #               lpftLastWriteTime) - 8 args (32 bytes)
    def _reg_enum_key_ex_a(cpu: "CPU") -> None:
        cpu.regs[EAX] = ERROR_NO_MORE_ITEMS
        cleanup_stdcall(cpu, memory, 32)

    stubs.register_handler("advapi32.dll", "RegEnumKeyExA", _reg_enum_key_ex_a)

    # RegEnumValueA(hKey, dwIndex, lpValueName, lpcchValueName, lpReserved, lpType,
    #               lpData, lpcbData) - 8 args (32 bytes)
    def _reg_enum_value_a(cpu: "CPU") -> None:
        cpu.regs[EAX] = ERROR_NO_MORE_ITEMS
        cleanup_stdcall(cpu, memory, 32)

    stubs.register_handler("advapi32.dll", "RegEnumValueA", _reg_enum_value_a)

    # ── advapi32.dll: Event Log API ───────────────────────────────────────────

    # OpenEventLogA(lpUNCServerName, lpSourceName) - 2 args (8 bytes)
    def _open_event_log_a(cpu: "CPU") -> None:
        cpu.regs[EAX] = 0xBEEF0200  # fake event log handle
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("advapi32.dll", "OpenEventLogA", _open_event_log_a)

    # ReportEventA(hEventLog, wType, wCategory, dwEventID, lpUserSid, wNumStrings,
    #              dwDataSize, lpStrings, lpRawData) - 9 args (36 bytes)
    def _report_event_a(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 36)

    stubs.register_handler("advapi32.dll", "ReportEventA", _report_event_a)

    # CloseEventLog(hEventLog) - 1 arg (4 bytes)
    def _close_event_log(cpu: "CPU") -> None:
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("advapi32.dll", "CloseEventLog", _close_event_log)

    # ── advapi32.dll: Security/User API ──────────────────────────────────────

    # GetUserNameA(lpBuffer, pcbBuffer) - 2 args (8 bytes)
    def _get_user_name_a(cpu: "CPU") -> None:
        lp_buffer  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        pcb_buffer = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        username   = "Player\0"
        if lp_buffer and pcb_buffer:
            max_len = memory.read32(pcb_buffer)
            for i in range(min(len(username), max_len)):
                memory.write8(lp_buffer + i, ord(username[i]))
            memory.write32(pcb_buffer, len(username))
        cpu.regs[EAX] = 1  # TRUE
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("advapi32.dll", "GetUserNameA", _get_user_name_a)

    # ── winmm.dll: Audio device absence handlers ──────────────────────────────

    MMSYSERR_NODRIVER = 10

    # mixerGetNumDevs() -> UINT [stdcall, no args] — 0 = no mixer hardware
    def _mixer_get_num_devs(cpu: "CPU") -> None:
        logger.warn("handlers", "[winmm] mixerGetNumDevs -> 0 (no audio hardware)")
        cpu.regs[EAX] = 0

    stubs.register_handler("winmm.dll", "mixerGetNumDevs", _mixer_get_num_devs)

    # mixerGetLineInfoA(hmxobj, pmxl, fdwInfo) -> MMRESULT [stdcall, 3 args (12 bytes)]
    def _mixer_get_line_info_a(cpu: "CPU") -> None:
        logger.warn("handlers", "[winmm] mixerGetLineInfoA -> MMSYSERR_NODRIVER")
        cpu.regs[EAX] = MMSYSERR_NODRIVER
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("winmm.dll", "mixerGetLineInfoA", _mixer_get_line_info_a)

    # mixerGetLineControlsA(hmxobj, pmxlc, fdwControls) -> MMRESULT [stdcall, 3 args (12 bytes)]
    def _mixer_get_line_controls_a(cpu: "CPU") -> None:
        logger.warn("handlers", "[winmm] mixerGetLineControlsA -> MMSYSERR_NODRIVER")
        cpu.regs[EAX] = MMSYSERR_NODRIVER
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("winmm.dll", "mixerGetLineControlsA", _mixer_get_line_controls_a)

    # mixerGetControlDetailsA(hmxobj, pmxcd, fdwDetails) -> MMRESULT [stdcall, 3 args (12 bytes)]
    def _mixer_get_control_details_a(cpu: "CPU") -> None:
        logger.warn("handlers", "[winmm] mixerGetControlDetailsA -> MMSYSERR_NODRIVER")
        cpu.regs[EAX] = MMSYSERR_NODRIVER
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("winmm.dll", "mixerGetControlDetailsA", _mixer_get_control_details_a)

    # mixerSetControlDetails(hmxobj, pmxcd, fdwDetails) -> MMRESULT [stdcall, 3 args (12 bytes)]
    def _mixer_set_control_details(cpu: "CPU") -> None:
        logger.warn("handlers", "[winmm] mixerSetControlDetails -> MMSYSERR_NODRIVER")
        cpu.regs[EAX] = MMSYSERR_NODRIVER
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("winmm.dll", "mixerSetControlDetails", _mixer_set_control_details)

    # waveOutGetDevCapsA(uDeviceID, pwoc, cbwoc) -> MMRESULT [stdcall, 3 args (12 bytes)]
    def _wave_out_get_dev_caps_a(cpu: "CPU") -> None:
        logger.warn("handlers", "[winmm] waveOutGetDevCapsA -> MMSYSERR_NODRIVER")
        cpu.regs[EAX] = MMSYSERR_NODRIVER
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("winmm.dll", "waveOutGetDevCapsA", _wave_out_get_dev_caps_a)

    # ── winmm.dll: Timer handlers ─────────────────────────────────────────────

    TIMERR_NOERROR = 0

    # timeGetDevCaps(ptc, cbtc) - 2 args (8 bytes)
    # VERIFIED: _TIMER_init checks result != 0 → abortmessage("MULTIMEDIA TIMER NOT FOUND")
    def _time_get_dev_caps(cpu: "CPU") -> None:
        ptc = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if ptc != 0:
            memory.write32(ptc,     1)           # wPeriodMin = 1 ms
            memory.write32(ptc + 4, 1_000_000)  # wPeriodMax = 1 s
        cpu.regs[EAX] = TIMERR_NOERROR
        cleanup_stdcall(cpu, memory, 8)

    stubs.register_handler("winmm.dll", "timeGetDevCaps", _time_get_dev_caps)

    # timeBeginPeriod(uPeriod) - 1 arg (4 bytes)
    # VERIFIED: _TIMER_init checks result != 0 → abortmessage("FAILED TO INITIALIZE MULTIMEDIA TIMER")
    def _time_begin_period(cpu: "CPU") -> None:
        period = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        logger.info("handlers", f"[winmm] timeBeginPeriod({period}) -> 0")
        cpu.regs[EAX] = TIMERR_NOERROR
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("winmm.dll", "timeBeginPeriod", _time_begin_period)

    # timeEndPeriod(uPeriod) - 1 arg (4 bytes)
    # VERIFIED: mmtimer_callback calls timeEndPeriod in shutdown path; return value not checked.
    def _time_end_period(cpu: "CPU") -> None:
        cpu.regs[EAX] = TIMERR_NOERROR
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("winmm.dll", "timeEndPeriod", _time_end_period)

    # timeGetTime() - no args, no stack cleanup
    # VERIFIED: mmtimer_callback uses timeGetTime for scheduling next timeSetEvent delay.
    def _time_get_time(cpu: "CPU") -> None:
        cpu.regs[EAX] = state.virtual_ticks_ms & 0xFFFFFFFF

    stubs.register_handler("winmm.dll", "timeGetTime", _time_get_time)

    # timeSetEvent(uDelay, uResolution, lpTimeProc, dwUser, fuEvent) - 5 args (20 bytes)
    # VERIFIED: mmtimer_callback: if result == 0 → "timeSetEvent failed, shutting down timer".
    #           Must return non-zero or game timer system shuts down permanently.
    def _time_set_event(cpu: "CPU") -> None:
        global _next_timer_id
        base          = cpu.regs[ESP]
        u_delay       = memory.read32(base + 4)
        u_resolution  = memory.read32(base + 8)
        lp_time_proc  = memory.read32(base + 12)
        dw_user       = memory.read32(base + 16)
        fu_event      = memory.read32(base + 20)
        timer_id      = _next_timer_id
        _next_timer_id += 1
        period_ms     = u_delay if (fu_event & TIME_PERIODIC) != 0 else 0
        pending_timers[timer_id] = PendingTimer(
            id=timer_id,
            due_at=state.virtual_ticks_ms + u_delay,
            period_ms=period_ms,
            cb_addr=lp_time_proc,
            dw_user=dw_user,
            fu_event=fu_event,
        )
        logger.info(
            "handlers",
            f"[winmm] timeSetEvent(delay={u_delay}, res={u_resolution},"
            f" cb=0x{lp_time_proc:x}, fuEvent=0x{fu_event:x}, periodic={period_ms > 0}) -> id={timer_id}",
        )
        cpu.regs[EAX] = timer_id
        cleanup_stdcall(cpu, memory, 20)

    stubs.register_handler("winmm.dll", "timeSetEvent", _time_set_event)

    # timeKillEvent(uTimerID) - 1 arg (4 bytes)
    # VERIFIED: _TIMER_restore calls timeKillEvent to cancel the timer.
    def _time_kill_event(cpu: "CPU") -> None:
        timer_id = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        pending_timers.pop(timer_id, None)
        cpu.regs[EAX] = TIMERR_NOERROR
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("winmm.dll", "timeKillEvent", _time_kill_event)
