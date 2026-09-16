"""PDF reader infrastructure."""

from app.pdf.pdfjs_scheme import (
    PDFJS_VERSION,
    PdfJsSchemeHandler,
    build_pdfjs_viewer_url,
    register_pdfjs_scheme,
)

__all__ = [
    "PDFJS_VERSION",
    "PdfJsSchemeHandler",
    "build_pdfjs_viewer_url",
    "register_pdfjs_scheme",
]
