"""PE resource section parser.

Parses the IMAGE_RESOURCE_DIRECTORY tree to extract dialog templates and
bitmap resources from a loaded PE binary.

Supports:
  - DLGTEMPLATE (old format, not DLGTEMPLATEEX)
  - RT_DIALOG (type 5) lookup by integer ID
  - RT_BITMAP (type 2) lookup by integer ID
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from tew.logger import logger


# ── Dialog unit conversion ────────────────────────────────────────────────────
# Defined here so callers that only need pe_resources don't have to import
# window_manager.  The same functions are also available in window_manager.

_DU_BASE_X = 6
_DU_BASE_Y = 13


def du_to_px_x(du: int) -> int:
    """Convert a horizontal dialog unit to pixels (MS Sans Serif 8pt at 96 DPI)."""
    return (du * _DU_BASE_X + 2) // 4


def du_to_px_y(du: int) -> int:
    """Convert a vertical dialog unit to pixels (MS Sans Serif 8pt at 96 DPI)."""
    return (du * _DU_BASE_Y + 4) // 8


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class DialogControl:
    id: int
    class_name: str    # "EDIT", "BUTTON", "STATIC", "LISTBOX", "SCROLLBAR",
                       # "COMBOBOX", or "#{ordinal}" for anything else
    title: str
    x: int
    y: int
    cx: int
    cy: int
    style: int
    ex_style: int


@dataclass
class DialogTemplate:
    title: str
    x: int
    y: int
    cx: int
    cy: int
    style: int
    font_name: str
    font_pt: int
    controls: list[DialogControl] = field(default_factory=list)


# ── Ordinal → class name mapping (Win32 predefined control classes) ───────────

_CTRL_CLASS: dict[int, str] = {
    0x80: "BUTTON",
    0x81: "EDIT",
    0x82: "STATIC",
    0x83: "LISTBOX",
    0x84: "SCROLLBAR",
    0x85: "COMBOBOX",
}

# Win32 resource type IDs
_RT_BITMAP = 2
_RT_DIALOG = 5


# ── Parser ────────────────────────────────────────────────────────────────────

class PEResources:
    """Parses PE resource data from a raw PE binary (bytes).

    Usage:
        res = PEResources(pe_bytes)
        dlg = res.find_dialog(114)
        bmp = res.find_bitmap(117)
    """

    def __init__(self, pe_data: bytes) -> None:
        self._data = pe_data
        self._rsrc_rva: int = 0    # virtual address of .rsrc section
        self._rsrc_raw: int = 0    # file offset of .rsrc section
        self._rsrc_size: int = 0   # raw size
        self._valid: bool = False

        self._parse_pe_header()

    # ── Public API ────────────────────────────────────────────────────────────

    def find_dialog(self, template_id: int) -> DialogTemplate | None:
        """Return the parsed DialogTemplate for the given resource ID, or None."""
        if not self._valid:
            return None
        raw = self._find_resource(_RT_DIALOG, template_id)
        if raw is None:
            logger.warn("window", f"[PEResources] Dialog {template_id} not found")
            return None
        return self._parse_dlgtemplate(raw)

    def find_bitmap(self, bitmap_id: int) -> bytes | None:
        """Return raw DIB bytes for the given bitmap resource ID, or None."""
        if not self._valid:
            return None
        raw = self._find_resource(_RT_BITMAP, bitmap_id)
        if raw is None:
            logger.warn("window", f"[PEResources] Bitmap {bitmap_id} not found")
            return None
        return raw

    # ── PE header parsing ─────────────────────────────────────────────────────

    def _parse_pe_header(self) -> None:
        data = self._data
        if len(data) < 0x40:
            logger.error("window", "[PEResources] File too small to be a PE")
            return

        e_magic = struct.unpack_from("<H", data, 0)[0]
        if e_magic != 0x5A4D:
            logger.error("window", f"[PEResources] Bad DOS magic: 0x{e_magic:04x}")
            return

        e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        if e_lfanew + 24 > len(data):
            logger.error("window", "[PEResources] PE header offset out of range")
            return

        pe_sig = struct.unpack_from("<I", data, e_lfanew)[0]
        if pe_sig != 0x00004550:
            logger.error("window", f"[PEResources] Bad PE signature: 0x{pe_sig:08x}")
            return

        coff_off = e_lfanew + 4
        num_sections = struct.unpack_from("<H", data, coff_off + 2)[0]
        opt_size = struct.unpack_from("<H", data, coff_off + 16)[0]
        opt_off = coff_off + 20

        if opt_size < 100:
            logger.error("window", "[PEResources] Optional header too small")
            return

        # Data directory entry 2 = resource directory
        rsrc_dir_rva, _rsrc_dir_size = struct.unpack_from("<II", data, opt_off + 96 + 2 * 8)
        logger.debug("window", f"[PEResources] Resource dir RVA=0x{rsrc_dir_rva:x}")

        # Find the section that contains this RVA
        sec_table_off = opt_off + opt_size
        rsrc_rva = 0
        rsrc_raw = 0
        rsrc_size = 0

        for i in range(num_sections):
            sec_off = sec_table_off + i * 40
            if sec_off + 40 > len(data):
                break
            virt_size = struct.unpack_from("<I", data, sec_off + 8)[0]
            virt_addr = struct.unpack_from("<I", data, sec_off + 12)[0]
            raw_size  = struct.unpack_from("<I", data, sec_off + 16)[0]
            raw_ptr   = struct.unpack_from("<I", data, sec_off + 20)[0]
            span = max(virt_size, raw_size)
            if virt_addr <= rsrc_dir_rva < virt_addr + span:
                rsrc_rva  = virt_addr
                rsrc_raw  = raw_ptr
                rsrc_size = raw_size
                sec_name = data[sec_off:sec_off + 8].rstrip(b"\x00").decode("ascii", errors="replace")
                logger.debug("window", f"[PEResources] .rsrc in section '{sec_name}': raw=0x{raw_ptr:x} size=0x{raw_size:x}")
                break

        if rsrc_rva == 0:
            logger.error("window", "[PEResources] Could not locate .rsrc section")
            return

        self._rsrc_rva  = rsrc_rva
        self._rsrc_raw  = rsrc_raw
        self._rsrc_size = rsrc_size
        self._valid = True

    # ── Resource tree navigation ──────────────────────────────────────────────

    def _rva_to_file(self, rva: int) -> int:
        """Convert an RVA within the .rsrc section to a file byte offset."""
        return self._rsrc_raw + (rva - self._rsrc_rva)

    def _find_resource(self, res_type: int, res_id: int) -> bytes | None:
        """Walk the 3-level resource tree and return the raw resource bytes."""
        data = self._data
        rsrc_raw = self._rsrc_raw

        # Level 1: resource type
        type_dir_off = rsrc_raw
        type_entry_off = self._find_dir_entry(type_dir_off, res_type, named=False)
        if type_entry_off is None:
            logger.debug("window", f"[PEResources] Resource type {res_type} not in directory")
            return None

        _, type_data_off = struct.unpack_from("<II", data, type_entry_off)
        if not (type_data_off & 0x80000000):
            logger.warn("window", f"[PEResources] Resource type entry is not a subdirectory")
            return None
        name_dir_off = rsrc_raw + (type_data_off & 0x7FFFFFFF)

        # Level 2: resource name/ID
        name_entry_off = self._find_dir_entry(name_dir_off, res_id, named=False)
        if name_entry_off is None:
            logger.debug("window", f"[PEResources] Resource id {res_id} not found in type {res_type}")
            return None

        _, name_data_off = struct.unpack_from("<II", data, name_entry_off)
        if not (name_data_off & 0x80000000):
            logger.warn("window", f"[PEResources] Resource name entry is not a subdirectory")
            return None
        lang_dir_off = rsrc_raw + (name_data_off & 0x7FFFFFFF)

        # Level 3: language — take the first available language
        _chars, _ts, _maj, _min, named_cnt, id_cnt = struct.unpack_from("<IIHHHH", data, lang_dir_off)
        total = named_cnt + id_cnt
        if total == 0:
            logger.warn("window", f"[PEResources] No language entries for resource {res_id}")
            return None

        lang_entry_off = lang_dir_off + 16   # first entry
        _lang_id, data_entry_off = struct.unpack_from("<II", data, lang_entry_off)
        if data_entry_off & 0x80000000:
            logger.warn("window", "[PEResources] Language entry points to subdirectory (unexpected)")
            return None

        # IMAGE_RESOURCE_DATA_ENTRY
        de_off = rsrc_raw + (data_entry_off & 0x7FFFFFFF)
        if de_off + 16 > len(data):
            logger.error("window", f"[PEResources] Data entry offset 0x{de_off:x} out of bounds")
            return None

        res_rva, res_size, _codepage, _reserved = struct.unpack_from("<IIII", data, de_off)
        file_off = self._rva_to_file(res_rva)
        if file_off < 0 or file_off + res_size > len(data):
            logger.error("window", f"[PEResources] Resource data out of bounds at 0x{file_off:x} size={res_size}")
            return None

        return data[file_off : file_off + res_size]

    def _find_dir_entry(self, dir_off: int, target_id: int, *, named: bool) -> int | None:
        """Search an IMAGE_RESOURCE_DIRECTORY for an entry with the given integer ID.
        Returns the file offset of the 8-byte entry, or None if not found.
        """
        data = self._data
        if dir_off + 16 > len(data):
            return None

        _chars, _ts, _maj, _min, named_cnt, id_cnt = struct.unpack_from("<IIHHHH", data, dir_off)
        # Named entries come first, then ID entries
        entries_start = dir_off + 16
        total = named_cnt + id_cnt

        for i in range(total):
            entry_off = entries_start + i * 8
            if entry_off + 8 > len(data):
                break
            name_id, _data_off = struct.unpack_from("<II", data, entry_off)
            is_name = bool(name_id & 0x80000000)
            entry_val = name_id & 0x7FFFFFFF

            if is_name:
                continue   # skip string-named entries when looking for IDs

            if entry_val == target_id:
                return entry_off

        return None

    # ── DLGTEMPLATE parser ────────────────────────────────────────────────────

    def _parse_dlgtemplate(self, raw: bytes) -> DialogTemplate | None:
        """Parse a DLGTEMPLATE binary blob and return a DialogTemplate."""
        if len(raw) < 18:
            logger.error("window", "[PEResources] Dialog data too short")
            return None

        # DLGTEMPLATE header: style(4), exStyle(4), cDlgItems(2), x(2), y(2), cx(2), cy(2)
        style, ex_style = struct.unpack_from("<II", raw, 0)
        c_items, x, y, cx, cy = struct.unpack_from("<Hhhhh", raw, 8)

        pos = 18

        # Variable-length fields: menu, windowClass, title
        _menu, pos = _read_var_field(raw, pos)
        _wnd_class, pos = _read_var_field(raw, pos)
        title, pos = _read_var_field(raw, pos)

        font_name = ""
        font_pt = 0

        # DS_SETFONT = 0x40: font point size + name follow
        if style & 0x40:
            if pos + 2 > len(raw):
                logger.error("window", "[PEResources] Truncated font data in dialog")
                return None
            font_pt = struct.unpack_from("<H", raw, pos)[0]
            pos += 2
            font_name, pos = _read_utf16_str(raw, pos)

        # Each DLGITEMTEMPLATE starts at a DWORD boundary
        pos = _align4(pos)

        controls: list[DialogControl] = []
        for i in range(c_items):
            ctrl = self._parse_dlgitemtemplate(raw, pos)
            if ctrl is None:
                logger.error("window", f"[PEResources] Failed to parse control {i} of dialog")
                return None
            ctrl_obj, pos = ctrl
            controls.append(ctrl_obj)

        logger.debug("window",
            f"[PEResources] Parsed dialog '{title}': {len(controls)} controls, "
            f"size={cx}x{cy} du"
        )
        return DialogTemplate(
            title=title,
            x=x, y=y, cx=cx, cy=cy,
            style=style,
            font_name=font_name,
            font_pt=font_pt,
            controls=controls,
        )

    def _parse_dlgitemtemplate(
        self, raw: bytes, pos: int
    ) -> tuple[DialogControl, int] | None:
        """Parse one DLGITEMTEMPLATE at `pos`.  Returns (control, new_pos) or None."""
        if pos + 18 > len(raw):
            return None

        style, ex_style = struct.unpack_from("<II", raw, pos)
        x, y, cx, cy = struct.unpack_from("<hhhh", raw, pos + 8)
        ctrl_id = struct.unpack_from("<H", raw, pos + 16)[0]

        var_pos = pos + 18

        # Class field
        class_str, var_pos = _read_var_field(raw, var_pos)
        # Title field
        title_str, var_pos = _read_var_field(raw, var_pos)
        # extraCount WORD
        if var_pos + 2 > len(raw):
            return None
        extra_count = struct.unpack_from("<H", raw, var_pos)[0]
        var_pos += 2 + extra_count

        # Next control starts at DWORD boundary
        next_pos = _align4(var_pos)

        # Resolve class ordinal to name
        class_name = _resolve_class_name(class_str)

        return (
            DialogControl(
                id=ctrl_id,
                class_name=class_name,
                title=title_str,
                x=x, y=y, cx=cx, cy=cy,
                style=style,
                ex_style=ex_style,
            ),
            next_pos,
        )


# ── Low-level binary helpers ──────────────────────────────────────────────────

def _align4(pos: int) -> int:
    return (pos + 3) & ~3


def _read_utf16_str(buf: bytes, pos: int) -> tuple[str, int]:
    """Read a null-terminated UTF-16LE string.  Returns (string, pos_after_null)."""
    chars: list[str] = []
    while pos + 2 <= len(buf):
        code = struct.unpack_from("<H", buf, pos)[0]
        pos += 2
        if code == 0:
            break
        chars.append(chr(code))
    return "".join(chars), pos


def _read_var_field(buf: bytes, pos: int) -> tuple[str, int]:
    """Read a Win32 resource variable-length string field.

    Encoding:
      0x0000         → empty string, consume 2 bytes
      0xFFFF + WORD  → ordinal reference "#N", consume 4 bytes
      other WORD...  → UTF-16LE null-terminated string, first char already read
    """
    if pos + 2 > len(buf):
        return "", pos
    first = struct.unpack_from("<H", buf, pos)[0]
    if first == 0x0000:
        return "", pos + 2
    if first == 0xFFFF:
        if pos + 4 > len(buf):
            return "", pos + 2
        ordinal = struct.unpack_from("<H", buf, pos + 2)[0]
        return f"#{ordinal}", pos + 4
    # Regular UTF-16LE string; first char was `first`
    chars = [chr(first)]
    pos += 2
    while pos + 2 <= len(buf):
        code = struct.unpack_from("<H", buf, pos)[0]
        pos += 2
        if code == 0:
            break
        chars.append(chr(code))
    return "".join(chars), pos


def _resolve_class_name(class_str: str) -> str:
    """Convert a raw class field value (e.g. '#129') to a Win32 class name."""
    if class_str.startswith("#"):
        try:
            ordinal = int(class_str[1:])
            return _CTRL_CLASS.get(ordinal, class_str)
        except ValueError:
            return class_str
    return class_str
