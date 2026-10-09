import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication
from pysync_client_cli.config import config_dir

from pysync_client_gui.backend import Backend
from pysync_client_gui.theme import apply_theme
from pysync_client_gui.window import MainWindow


def main() -> None:
    parser = argparse.ArgumentParser(description="PySync desktop client")
    parser.add_argument(
        "--config-dir", type=Path, default=config_dir(), help="Shared client state directory"
    )
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    application = QApplication([sys.argv[0]])
    application.setApplicationName("PySync")
    application.setOrganizationName("PySync")
    application.setApplicationDisplayName("PySync")
    apply_theme(application)
    window = MainWindow(Backend(args.config_dir.absolute()))
    window.show()
    if args.smoke_test:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(1500, window.close)
    raise SystemExit(application.exec())


if __name__ == "__main__":
    main()
