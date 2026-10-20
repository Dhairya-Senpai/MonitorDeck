"""MonitorDeck entry point.

Launches the GUI. Command-line modes (--preset and friends) will be routed
through cli.py once presets exist.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

LOG_FILENAME = "monitordeck.log"


def config_dir() -> Path:
    """Return %APPDATA%\\MonitorDeck, creating it if needed."""
    base = os.environ.get("APPDATA") or str(Path.home())
    directory = Path(base) / "MonitorDeck"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def setup_logging(level: int = logging.INFO) -> None:
    """Log to the console and to %APPDATA%\\MonitorDeck\\monitordeck.log."""
    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.FileHandler(
        config_dir() / LOG_FILENAME, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="MonitorDeck",
        description="Control an external monitor over DDC/CI.")
    parser.add_argument("--debug", action="store_true",
                        help="verbose logging")
    return parser.parse_args(argv)


def run_gui() -> int:
    """Start the Qt application and show the main window."""
    from PySide6.QtWidgets import QApplication

    from gui import MainWindow, apply_dark_theme

    app = QApplication(sys.argv[:1])
    app.setApplicationName("MonitorDeck")
    apply_dark_theme(app)
    window = MainWindow()
    window.show()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    """Entry point: configure logging, then launch the GUI."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    setup_logging(logging.DEBUG if args.debug else logging.INFO)
    logging.getLogger(__name__).info("MonitorDeck starting")
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
