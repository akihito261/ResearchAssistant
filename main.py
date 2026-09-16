from __future__ import annotations

import logging
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from app.database.database import init_database
from app.logging_config import configure_logging
from app.pdf.pdfjs_scheme import register_pdfjs_scheme
from app.runtime_paths import ensure_writable_directories, resource_path, writable_path
from app.version import __version__


def _install_exception_logger() -> None:
    original_hook = sys.excepthook

    def log_uncaught_exception(exc_type, exc_value, traceback) -> None:
        logging.getLogger(__name__).critical(
            "Unhandled application exception",
            exc_info=(exc_type, exc_value, traceback),
        )
        original_hook(exc_type, exc_value, traceback)

    sys.excepthook = log_uncaught_exception


def main() -> int:
    ensure_writable_directories()
    try:
        configure_logging()
    except OSError:
        # A read-only installation must still be able to open the library.
        logging.basicConfig(level=logging.INFO)
        logging.getLogger(__name__).exception(
            "File logging is unavailable; continuing with console logging"
        )
    _install_exception_logger()

    # Custom WebEngine schemes must be registered before QApplication exists.
    register_pdfjs_scheme()

    from app.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Research Assistant")
    app.setApplicationDisplayName("Research Assistant")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("ResearchAssistant")
    application_icon = QIcon(
        str(resource_path("resources", "icons", "research_assistant.ico"))
    )
    if not application_icon.isNull():
        app.setWindowIcon(application_icon)

    try:
        init_database()
    except Exception as error:
        logging.getLogger(__name__).critical(
            "Could not initialize the research database",
            exc_info=True,
        )
        QMessageBox.critical(
            None,
            "Research Assistant",
            "The research database could not be opened.\n\n"
            f"{error}\n\nSee {writable_path('logs', 'research_assistant.log')} "
            "for details.",
        )
        return 1

    # Credential migration stays entirely inside the OS vault. Failure is
    # intentionally non-fatal; Settings can still accept a replacement key.
    from app.services.ai_credential_service import AICredentialService

    AICredentialService.migrate_legacy_custom_credential()

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
