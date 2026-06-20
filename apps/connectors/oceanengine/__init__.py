"""OceanEngine (巨量) offline-import connector.

A standalone, network-free ETL stage that reads RPA-archived XLSX exports and
emits :class:`MetricsImportRequest` payloads for the genesis case-metrics import
API. Content/manifest fingerprint dedupe makes re-import a full skip.
"""

from apps.connectors.oceanengine.archive import ImportArchive
from apps.connectors.oceanengine.ingest import (
    IngestResult,
    IngestSummary,
    build_import_rows,
    default_archive,
    import_archive_tree,
    import_archived_xlsx,
    infer_source_page,
    sha256_file,
)
from apps.connectors.oceanengine.normalize import NORMALIZERS, normalize_rows
from apps.connectors.oceanengine.xlsx import XlsxUnsupportedError, openpyxl_available, read_first_sheet

__all__ = [
    "ImportArchive",
    "IngestResult",
    "IngestSummary",
    "NORMALIZERS",
    "XlsxUnsupportedError",
    "build_import_rows",
    "default_archive",
    "import_archive_tree",
    "import_archived_xlsx",
    "infer_source_page",
    "normalize_rows",
    "openpyxl_available",
    "read_first_sheet",
    "sha256_file",
]
