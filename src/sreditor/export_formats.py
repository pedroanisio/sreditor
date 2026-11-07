"""The formats exports and books download as, apart from the code that writes them, so pages
can offer them without loading the PDF and Excel writers."""

from __future__ import annotations

from enum import StrEnum


class ExportFormat(StrEnum):
    """The formats an export downloads as."""

    JSON = "json"
    XLSX = "xlsx"
    PDF = "pdf"


MEDIA_TYPES = {
    ExportFormat.JSON: "application/json",
    ExportFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ExportFormat.PDF: "application/pdf",
}
