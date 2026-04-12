"""PE file parser — top-level EXEFile class."""

from __future__ import annotations
import struct
from pathlib import Path

from tew.helpers import hex_val
from tew.logger import logger
from tew.pe.base_relocation_table import BaseRelocationTable
from tew.pe.bound_import_table import BoundImportTable
from tew.pe.coff_file_header import COFFFileHeader
from tew.pe.data_directory import DataDirectory
from tew.pe.debug_directory import DebugDirectory
from tew.pe.delay_import_table import DelayImportTable
from tew.pe.exception_table import ExceptionTable
from tew.pe.export_table import ExportTable
from tew.pe.import_table import ImportTable
from tew.pe.load_config_directory import LoadConfigDirectory
from tew.pe.optional_header import OptionalHeader
from tew.pe.section_header import SectionHeader
from tew.pe.tls_table import TLSDirectory


class EXEFile:
    def __init__(self, file_path: str | Path, dll_search_paths: list[str] | None = None) -> None:
        from tew.loader.import_resolver import ImportResolver

        self._file_path = str(file_path)
        logger.debug("loader", f"loading {self._file_path}")

        self._file_image = Path(self._file_path).read_bytes()
        self._image_size = len(self._file_image)

        # PE start offset: DOS header at 0x3C + 4 to skip the "PE\0\0" signature
        self._pe_start_offset = struct.unpack_from("<h", self._file_image, 0x3C)[0] + 4

        # COFF file header
        coff_start = self._pe_start_offset
        self._coff_file_header = COFFFileHeader(
            self._file_image[coff_start : coff_start + COFFFileHeader.SIZE_OF]
        )

        # Optional header
        opt_start = coff_start + COFFFileHeader.SIZE_OF
        self._optional_header = OptionalHeader(
            self._file_image[opt_start : opt_start + self._coff_file_header.size_of_optional_header]
        )

        # Section headers
        section_table_start = opt_start + self._coff_file_header.size_of_optional_header
        self._section_headers: list[SectionHeader] = []
        for i in range(self._coff_file_header.number_of_sections):
            offset = section_table_start + i * SectionHeader.SIZE_OF
            sh = SectionHeader(self._file_image[offset : offset + SectionHeader.SIZE_OF])
            self._section_headers.append(sh)

        for sh in self._section_headers:
            sh.resolve(self._file_image)

        for dd in self._optional_header.data_directories:
            dd.resolve(self._file_image, self._section_headers)

        self._parse_data_directories()

        self._import_resolver = ImportResolver(dll_search_paths or [])

    def _parse_data_directories(self) -> None:
        dirs = self._optional_header.data_directories
        img = self._file_image
        sects = self._section_headers
        pe32plus = self._optional_header.is_pe32_plus

        def dd(index: int) -> DataDirectory | None:
            d = dirs[index] if index < len(dirs) else None
            return d if d and d.data else None

        # [0] Export Table
        export_dir = dd(0)
        self._export_table: ExportTable | None = None
        if export_dir:
            self._export_table = ExportTable(
                export_dir.data, img, sects, export_dir.virtual_address, export_dir.size
            )

        # [1] Import Table
        import_dir = dd(1)
        self._import_table: ImportTable | None = None
        if import_dir:
            self._import_table = ImportTable(import_dir.data, img, sects, pe32plus)

        # [3] Exception Table
        exception_dir = dd(3)
        self._exception_table: ExceptionTable | None = None
        if exception_dir:
            self._exception_table = ExceptionTable(exception_dir.data)

        # [5] Base Relocation Table
        reloc_dir = dd(5)
        self._base_relocation_table: BaseRelocationTable | None = None
        if reloc_dir:
            self._base_relocation_table = BaseRelocationTable(reloc_dir.data)

        # [6] Debug Directory
        debug_dir = dd(6)
        self._debug_directory: DebugDirectory | None = None
        if debug_dir:
            self._debug_directory = DebugDirectory(debug_dir.data, img)

        # [9] TLS Table
        tls_dir = dd(9)
        self._tls_directory: TLSDirectory | None = None
        if tls_dir:
            self._tls_directory = TLSDirectory(
                tls_dir.data, img, sects, pe32plus, self._optional_header.image_base
            )

        # [10] Load Config Directory
        load_config_dir = dd(10)
        self._load_config_directory: LoadConfigDirectory | None = None
        if load_config_dir:
            self._load_config_directory = LoadConfigDirectory(load_config_dir.data, pe32plus)

        # [11] Bound Import
        bound_dir = dd(11)
        self._bound_import_table: BoundImportTable | None = None
        if bound_dir:
            self._bound_import_table = BoundImportTable(bound_dir.data)

        # [13] Delay Import Descriptor
        delay_dir = dd(13)
        self._delay_import_table: DelayImportTable | None = None
        if delay_dir:
            self._delay_import_table = DelayImportTable(delay_dir.data, img, sects, pe32plus)

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def file_path(self) -> str: return self._file_path
    @property
    def size_on_disk(self) -> int: return self._image_size
    @property
    def file_signature(self) -> str: return self._file_image[:2].decode("latin-1")
    @property
    def pe_start_offset(self) -> int: return self._pe_start_offset
    @property
    def machine_type(self) -> str: return self._coff_file_header.machine
    @property
    def optional_header(self) -> OptionalHeader: return self._optional_header
    @property
    def section_headers(self) -> list[SectionHeader]: return self._section_headers
    @property
    def coff_file_header(self) -> COFFFileHeader: return self._coff_file_header
    @property
    def export_table(self) -> ExportTable | None: return self._export_table
    @property
    def import_table(self) -> ImportTable | None: return self._import_table
    @property
    def exception_table(self) -> ExceptionTable | None: return self._exception_table
    @property
    def base_relocation_table(self) -> BaseRelocationTable | None: return self._base_relocation_table
    @property
    def debug_directory(self) -> DebugDirectory | None: return self._debug_directory
    @property
    def tls_directory(self) -> TLSDirectory | None: return self._tls_directory
    @property
    def load_config_directory(self) -> LoadConfigDirectory | None: return self._load_config_directory
    @property
    def bound_import_table(self) -> BoundImportTable | None: return self._bound_import_table
    @property
    def delay_import_table(self) -> DelayImportTable | None: return self._delay_import_table
    @property
    def import_resolver(self):
        return self._import_resolver

    def __str__(self) -> str:
        sections = "\n\n".join(
            f"--- Section {i + 1} ---\n{s}" for i, s in enumerate(self._section_headers)
        )
        return "\n".join([
            f"=== {self._file_path} ===",
            f"File Size: {self._image_size} bytes",
            f"File Signature: {self.file_signature}",
            f"PE Start Offset: {hex_val(self._pe_start_offset)}",
            "",
            "--- COFF File Header ---",
            str(self._coff_file_header),
            "",
            "--- Optional Header ---",
            str(self._optional_header),
            "",
            sections,
        ])
