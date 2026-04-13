"""
INI file parsing and writing for GetPrivateProfileStringA / GetPrivateProfileIntA
and their WritePrivateProfile* counterparts.

Parses the classic Windows "private profile" format:
    [SectionName]
    Key=Value
    ; comment

Win32 reference:
    https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getprivateprofilestringa
"""

from __future__ import annotations

import os
from dataclasses import dataclass


# Parsed INI data: {section_lower: {key_lower: value_original_case}}
IniData = dict[str, dict[str, str]]


# ── DTOs ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GetPrivateProfileStringArgs:
    """
    Parsed stdcall stack frame for GetPrivateProfileStringA.

    Offsets relative to ESP at handler entry (before cleanup):
        ESP+ 4   lpAppName          section name, or None (NULL = enumerate sections)
        ESP+ 8   lpKeyName          key name, or None (NULL = enumerate keys in section)
        ESP+12   lpDefault          default string returned when key is absent
        ESP+16   lpReturnedString   output buffer pointer in guest memory
        ESP+20   nSize              output buffer capacity in chars (includes null)
        ESP+24   lpFileName         Windows path to the .ini file
    """
    app_name:  str | None   # section name, or None to enumerate all sections
    key_name:  str | None   # key name, or None to enumerate all keys in section
    default:   str          # returned when key is not found
    out_ptr:   int          # guest pointer to the output buffer
    n_size:    int          # output buffer capacity (chars, including null terminator)
    file_name: str          # Windows-style path to the .ini file


@dataclass(frozen=True)
class GetPrivateProfileIntArgs:
    """
    Parsed stdcall stack frame for GetPrivateProfileIntA.

    Offsets relative to ESP at handler entry (before cleanup):
        ESP+ 4   lpAppName   section name
        ESP+ 8   lpKeyName   key name
        ESP+12   nDefault    signed default value
        ESP+16   lpFileName  Windows path to the .ini file
    """
    app_name:  str
    key_name:  str
    default:   int   # signed default (stored as Python int)
    file_name: str


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_ini(text: str) -> IniData:
    """
    Parse INI-format text into a nested dict.

    Rules:
    - Section names and key names are lowercased for case-insensitive lookup.
    - Values preserve their original case.
    - Lines beginning with ``;`` or ``#`` are comments and are skipped.
    - Inline comments are NOT stripped — Win32 treats them as part of the value.
    - Duplicate keys within a section: the last definition wins.
    - A key=value line outside any section is silently ignored.
    """
    result: IniData = {}
    current_section: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        # Blank lines and comment lines
        if not line or line.startswith(";") or line.startswith("#"):
            continue

        # Section header:  [SectionName]
        if line.startswith("[") and "]" in line:
            end = line.index("]")
            current_section = line[1:end].strip().lower()
            if current_section not in result:
                result[current_section] = {}
            continue

        # Key=Value pair (only valid inside a section)
        if "=" in line and current_section is not None:
            key, _, value = line.partition("=")
            result[current_section][key.strip().lower()] = value.strip()

    return result


# ── Lookup ────────────────────────────────────────────────────────────────────

def read_profile_string(
    ini: IniData,
    app_name: str | None,
    key_name: str | None,
    default: str,
) -> str:
    """
    Retrieve a string value from *ini*, or enumerate section/key names.

    Modes:
        ``app_name`` is None          → return null-separated section names
        ``key_name`` is None          → return null-separated key names in section
        both set                      → return the value, or *default* if absent

    For enumeration modes the returned string uses ``\\0`` as a separator with
    NO trailing null.  The handler is responsible for appending an extra ``\\0``
    to produce the double-null termination that Win32 callers expect.

    All lookups are case-insensitive (keys and sections are lowercased at parse
    time).
    """
    if app_name is None:
        return "\0".join(ini.keys())

    section = ini.get(app_name.lower(), {})

    if key_name is None:
        return "\0".join(section.keys())

    return section.get(key_name.lower(), default)


def read_profile_int(ini: IniData, app_name: str, key_name: str, default: int) -> int:
    """
    Retrieve an integer value from *ini*, or return *default*.

    Accepts decimal, octal (0-prefixed), and hexadecimal (0x-prefixed) literals.
    Returns *default* when the key is absent or its value cannot be parsed.
    """
    raw = ini.get(app_name.lower(), {}).get(key_name.lower())
    if raw is None:
        return default
    raw = raw.strip()
    try:
        # Handles 0x-prefixed hex, 0o-prefixed octal, and plain decimal.
        return int(raw, 0)
    except ValueError:
        pass
    try:
        # Fallback: decimal with leading zeros stripped (matches Win32 strtol base-10).
        return int(raw, 10)
    except (ValueError, TypeError):
        return default


# ── Serialiser ────────────────────────────────────────────────────────────────

def _serialise_ini(ini: IniData) -> str:
    """Render *ini* back to INI-format text (one trailing newline per section)."""
    parts: list[str] = []
    for section, keys in ini.items():
        parts.append(f"[{section}]")
        for k, v in keys.items():
            parts.append(f"{k}={v}")
        parts.append("")          # blank line between sections
    return "\n".join(parts)


def _read_ini_from_disk(linux_path: str) -> IniData:
    """Read and parse an INI file from disk; return empty dict if absent/unreadable."""
    if not os.path.exists(linux_path):
        return {}
    try:
        with open(linux_path, "r", encoding="latin-1") as fh:
            return parse_ini(fh.read())
    except OSError:
        return {}


def _write_ini_to_disk(linux_path: str, ini: IniData) -> bool:
    """Serialise *ini* and write it to *linux_path*; return True on success."""
    try:
        dir_path = os.path.dirname(linux_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        with open(linux_path, "w", encoding="latin-1") as fh:
            fh.write(_serialise_ini(ini))
        return True
    except OSError:
        return False


# ── Writers ───────────────────────────────────────────────────────────────────

def write_profile_string(
    linux_path: str,
    app_name: str | None,
    key_name: str | None,
    value: str | None,
) -> bool:
    """
    Write, update, or delete a key/section in a Windows INI file on disk.

    Semantics match WritePrivateProfileStringA:
        ``app_name`` is None              → invalid; returns False
        ``key_name`` is None              → delete the entire section
        ``value`` is None                 → delete the key
        both ``key_name`` and ``value``   → set ``key = value``

    The file is created if it does not exist.  Returns True on success.
    """
    if app_name is None:
        return False

    ini = _read_ini_from_disk(linux_path)
    section_lower = app_name.lower()

    if key_name is None:
        # Delete the whole section.
        ini.pop(section_lower, None)
    elif value is None:
        # Delete a single key.
        if section_lower in ini:
            ini[section_lower].pop(key_name.lower(), None)
    else:
        # Create or update the key.
        if section_lower not in ini:
            ini[section_lower] = {}
        ini[section_lower][key_name.lower()] = value

    return _write_ini_to_disk(linux_path, ini)


def write_profile_section(
    linux_path: str,
    app_name: str | None,
    pairs: dict[str, str],
) -> bool:
    """
    Replace all key/value pairs in a section with *pairs*.

    Semantics match WritePrivateProfileSectionA — the entire section is
    replaced atomically.  *pairs* must already have lowercased keys.
    The file is created if it does not exist.  Returns True on success.
    """
    if app_name is None:
        return False

    ini = _read_ini_from_disk(linux_path)
    ini[app_name.lower()] = dict(pairs)
    return _write_ini_to_disk(linux_path, ini)
