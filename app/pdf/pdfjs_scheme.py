from __future__ import annotations

import logging
import mimetypes
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from PySide6.QtCore import QByteArray, QFile, QIODevice, QUrl, QUrlQuery
from PySide6.QtWebEngineCore import (
    QWebEngineUrlRequestJob,
    QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
)

from app.runtime_paths import resource_path


LOGGER = logging.getLogger(__name__)

PDFJS_ROOT = resource_path("resources", "pdfjs")
ICON_ROOT = resource_path("resources", "icons")
PDFJS_VERSION = "5.7.284"

SCHEME_NAME = b"research-assistant"
SCHEME_NAME_TEXT = SCHEME_NAME.decode("ascii")
SCHEME_HOST = "app"
VIEWER_REQUEST_PATH = "/pdfjs/web/viewer.html"
DOCUMENT_REQUEST_PATH = "/document/current.pdf"

_SCHEME_REGISTERED = False

_MIME_TYPES = {
    ".bcmap": "application/octet-stream",
    ".css": "text/css",
    ".ftl": "text/plain",
    ".gif": "image/gif",
    ".html": "text/html",
    ".icc": "application/vnd.iccprofile",
    ".js": "text/javascript",
    ".json": "application/json",
    ".mjs": "text/javascript",
    ".pdf": "application/pdf",
    ".pfb": "application/octet-stream",
    ".properties": "text/plain",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
}


def register_pdfjs_scheme() -> None:
    """Register the private PDF.js scheme before QApplication is created."""
    global _SCHEME_REGISTERED

    if _SCHEME_REGISTERED:
        return

    scheme = QWebEngineUrlScheme(SCHEME_NAME)
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    scheme.setFlags(
        QWebEngineUrlScheme.Flag.SecureScheme
        | QWebEngineUrlScheme.Flag.FetchApiAllowed
    )
    QWebEngineUrlScheme.registerScheme(scheme)
    _SCHEME_REGISTERED = True


def is_pdfjs_scheme_registered() -> bool:
    return _SCHEME_REGISTERED


def build_pdfjs_viewer_url(cache_key: str = "") -> QUrl:
    """Return the bundled viewer URL with a same-origin PDF endpoint."""
    viewer_url = QUrl()
    viewer_url.setScheme(SCHEME_NAME_TEXT)
    viewer_url.setHost(SCHEME_HOST)
    viewer_url.setPath(VIEWER_REQUEST_PATH)

    document_url = QUrl()
    document_url.setScheme(SCHEME_NAME_TEXT)
    document_url.setHost(SCHEME_HOST)
    document_url.setPath(DOCUMENT_REQUEST_PATH)
    if cache_key:
        document_query = QUrlQuery()
        document_query.addQueryItem("v", str(cache_key))
        document_url.setQuery(document_query)

    query = QUrlQuery()
    query.addQueryItem("file", document_url.toString())
    viewer_url.setQuery(query)
    return viewer_url


def missing_pdfjs_assets() -> list[Path]:
    required_paths = (
        PDFJS_ROOT / "LICENSE",
        PDFJS_ROOT / "build" / "pdf.mjs",
        PDFJS_ROOT / "build" / "pdf.worker.mjs",
        PDFJS_ROOT / "web" / "viewer.html",
        PDFJS_ROOT / "web" / "viewer.css",
        PDFJS_ROOT / "web" / "viewer.mjs",
        PDFJS_ROOT / "web" / "research_assistant.css",
        ICON_ROOT / "highlighter.svg",
        ICON_ROOT / "message-square.svg",
        ICON_ROOT / "languages.svg",
        ICON_ROOT / "copy.svg",
        ICON_ROOT / "sticky-note.svg",
        ICON_ROOT / "palette.svg",
        ICON_ROOT / "sparkles.svg",
    )
    return [path for path in required_paths if not path.is_file()]


def resolve_pdfjs_asset(request_path: str) -> Path | None:
    """Resolve a /pdfjs/ URL path while preventing directory traversal."""
    decoded_path = unquote(request_path)
    prefix = "/pdfjs/"
    if not decoded_path.startswith(prefix):
        return None

    relative_path = PurePosixPath(decoded_path[len(prefix) :])
    if not relative_path.parts or any(
        part in {"", ".", ".."} for part in relative_path.parts
    ):
        return None

    root = PDFJS_ROOT.resolve()
    candidate = root.joinpath(*relative_path.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None

    return candidate if candidate.is_file() else None


def resolve_icon_asset(request_path: str) -> Path | None:
    """Resolve a public Reader icon while keeping requests inside resources/icons."""
    decoded_path = unquote(request_path)
    prefix = "/icons/"
    if not decoded_path.startswith(prefix):
        return None

    relative_path = PurePosixPath(decoded_path[len(prefix) :])
    if (
        len(relative_path.parts) != 1
        or any(part in {"", ".", ".."} for part in relative_path.parts)
        or relative_path.suffix.lower() != ".svg"
    ):
        return None

    root = ICON_ROOT.resolve()
    candidate = root.joinpath(*relative_path.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _mime_type_for(path: Path) -> QByteArray:
    mime_type = _MIME_TYPES.get(path.suffix.lower())
    if mime_type is None:
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return QByteArray(mime_type.encode("ascii"))


class PdfJsSchemeHandler(QWebEngineUrlSchemeHandler):
    """Serve only bundled PDF.js assets and the active paper."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._document_path: Path | None = None

    def set_document_path(self, document_path: Path) -> None:
        self._document_path = document_path.resolve()

    def requestStarted(self, request: QWebEngineUrlRequestJob) -> None:
        if request.requestMethod() != QByteArray(b"GET"):
            request.fail(QWebEngineUrlRequestJob.Error.RequestDenied)
            return

        url = request.requestUrl()
        if url.scheme() != SCHEME_NAME_TEXT or url.host() != SCHEME_HOST:
            request.fail(QWebEngineUrlRequestJob.Error.UrlInvalid)
            return

        request_path = unquote(url.path())
        initiator = request.initiator()
        if initiator.isEmpty():
            if request_path != VIEWER_REQUEST_PATH:
                request.fail(QWebEngineUrlRequestJob.Error.RequestDenied)
                return
        elif (
            initiator.scheme() != SCHEME_NAME_TEXT
            or initiator.host() != SCHEME_HOST
        ):
            request.fail(QWebEngineUrlRequestJob.Error.RequestDenied)
            return

        if request_path == DOCUMENT_REQUEST_PATH:
            file_path = self._document_path
            if file_path is None or not file_path.is_file():
                request.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
                return
        else:
            file_path = resolve_pdfjs_asset(request_path) or resolve_icon_asset(
                request_path
            )
            if file_path is None:
                LOGGER.debug("Rejected PDF.js resource request: %s", request_path)
                request.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
                return

        device = QFile(str(file_path), request)
        if not device.open(QIODevice.OpenModeFlag.ReadOnly):
            LOGGER.error("Could not open PDF reader resource: %s", file_path)
            request.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
            return

        request.reply(_mime_type_for(file_path), device)
