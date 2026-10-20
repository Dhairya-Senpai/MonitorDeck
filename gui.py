"""MonitorDeck main window: monitor selector, controls, capability table, log.

Every DDC/CI call runs on a background thread (DDCWorker), so the window never
blocks on the monitor. Widgets only ever ask the worker to do something and
react to its signals.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSlider, QSpinBox, QSplitter, QStatusBar, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

import capabilities as caps_mod
import ddc

log = logging.getLogger(__name__)

SLIDER_DEBOUNCE_MS = 250
"""How long a slider must sit still before the value is written."""

CONFIRM_CODES = (0x60, 0xD6)
"""Codes that must never be applied without an explicit confirmation."""


class DDCWorker(QObject):
    """Owns the monitor handles and performs all DDC/CI I/O off the UI thread."""

    monitors_ready = Signal(list)
    capabilities_ready = Signal(object, object, list)
    value_written = Signal(int, int)
    failed = Signal(str)
    busy = Signal(bool, str)

    def __init__(self) -> None:
        super().__init__()
        self._monitors: list[ddc.Monitor] = []

    @Slot()
    def refresh_monitors(self) -> None:
        """Enumerate monitors and keep only those answering DDC/CI."""
        self.busy.emit(True, "Looking for DDC/CI monitors...")
        try:
            ddc.close_monitors(self._monitors)
            self._monitors = []
            usable, skipped = [], []
            for monitor in ddc.list_monitors():
                (usable if monitor.supports_ddc() else skipped).append(monitor)
            for monitor in skipped:
                log.info("skipping %s: no DDC/CI reply (built-in panels "
                         "usually do not support it)", monitor.description)
            ddc.close_monitors(skipped)
            self._monitors = usable
            log.info("%d monitor(s) respond to DDC/CI", len(usable))
            self.monitors_ready.emit(usable)
        except Exception as error:  # surfaced in the log panel, never silent
            log.exception("monitor enumeration failed")
            self.failed.emit(str(error))
        finally:
            self.busy.emit(False, "")

    @Slot(object)
    def scan(self, monitor: ddc.Monitor) -> None:
        """Read the capability string and every published code (read-only)."""
        self.busy.emit(True, "Reading capabilities (this can take a few seconds)...")
        try:
            raw = monitor.read_capabilities()
            caps = caps_mod.parse_capabilities(raw)
            rows = caps_mod.scan_monitor(monitor, caps)
            log.info("%s publishes %d VCP codes", caps.model or
                     monitor.description, len(caps.features))
            self.capabilities_ready.emit(monitor, caps, rows)
        except Exception as error:
            log.exception("capability scan failed")
            self.failed.emit(str(error))
        finally:
            self.busy.emit(False, "")

    @Slot(object, int, int)
    def write(self, monitor: ddc.Monitor, code: int, value: int) -> None:
        """Write a known VCP code, clamped by the monitor's own maximum."""
        try:
            monitor.write_vcp(code, value, known=caps_mod.is_known_code(code))
            self.value_written.emit(code, value)
        except Exception as error:
            log.error("write 0x%02X = %d failed: %s", code, value, error)
            self.failed.emit(str(error))

    @Slot()
    def shutdown(self) -> None:
        """Release every monitor handle."""
        ddc.close_monitors(self._monitors)
        self._monitors = []
        log.info("monitor handles released")


class LogBridge(QObject):
    """Carries log records from any thread to the log panel."""

    message = Signal(str)


class QtLogHandler(logging.Handler):
    """logging handler that forwards formatted records to the GUI."""

    def __init__(self, bridge: LogBridge) -> None:
        super().__init__()
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        """Forward one record; Qt queues it onto the UI thread."""
        self._bridge.message.emit(self.format(record))


class SliderControl(QWidget):
    """Label + slider + spin box for a continuous VCP code."""

    value_set = Signal(int, int)

    def __init__(self, code: int, name: str, maximum: int, current: int) -> None:
        super().__init__()
        self.code = code
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SLIDER_DEBOUNCE_MS)
        self._timer.timeout.connect(self._commit)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, maximum)
        self._slider.setValue(current)
        self._spin = QSpinBox()
        self._spin.setRange(0, maximum)
        self._spin.setValue(current)

        self._slider.valueChanged.connect(self._on_slider)
        self._spin.valueChanged.connect(self._on_spin)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._slider, 1)
        layout.addWidget(self._spin)

    def set_value(self, value: int) -> None:
        """Update the widgets without triggering a write."""
        for widget in (self._slider, self._spin):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)

    def _on_slider(self, value: int) -> None:
        self._spin.blockSignals(True)
        self._spin.setValue(value)
        self._spin.blockSignals(False)
        self._timer.start()

    def _on_spin(self, value: int) -> None:
        self._slider.blockSignals(True)
        self._slider.setValue(value)
        self._slider.blockSignals(False)
        self._timer.start()

    def _commit(self) -> None:
        self.value_set.emit(self.code, self._slider.value())


class ChoiceControl(QWidget):
    """Drop-down plus Apply button for a discrete VCP code.

    Codes in CONFIRM_CODES (input source, power) ask before applying, so no
    input switch or power change can happen by accident.
    """

    value_set = Signal(int, int)

    def __init__(self, code: int, name: str, allowed: list[int],
                 current: Optional[int]) -> None:
        super().__init__()
        self.code = code
        self._name = name
        self._combo = QComboBox()
        for value in allowed:
            label = caps_mod.value_name(code, value) or f"0x{value:02X}"
            self._combo.addItem(label, value)
        if current is not None:
            index = self._combo.findData(current)
            if index >= 0:
                self._combo.setCurrentIndex(index)

        self._apply = QPushButton("Apply")
        self._apply.clicked.connect(self._on_apply)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._combo, 1)
        layout.addWidget(self._apply)

    def set_value(self, value: int) -> None:
        """Select the given value without triggering a write."""
        index = self._combo.findData(value)
        if index >= 0:
            self._combo.blockSignals(True)
            self._combo.setCurrentIndex(index)
            self._combo.blockSignals(False)

    def _on_apply(self) -> None:
        value = self._combo.currentData()
        if value is None:
            return
        if self.code in CONFIRM_CODES and not self._confirm(value):
            return
        self.value_set.emit(self.code, int(value))

    def _confirm(self, value: int) -> bool:
        label = caps_mod.value_name(self.code, value) or f"0x{value:02X}"
        answer = QMessageBox.question(
            self, "Confirm", f"Set {self._name} to {label}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes


def build_control(row: caps_mod.ScanRow) -> Optional[QWidget]:
    """Create the right widget for one scanned code, or None if not usable."""
    if row.current is None or row.maximum is None or row.read_only:
        return None
    if row.allowed:
        return ChoiceControl(row.code, row.name, row.allowed, row.current)
    return SliderControl(row.code, row.name, row.maximum, row.current)


def apply_dark_theme(app: QObject) -> None:
    """Apply a flat dark palette to the whole application."""
    palette = QPalette()
    base = QColor(30, 31, 34)
    panel = QColor(43, 45, 49)
    text = QColor(222, 224, 228)
    accent = QColor(86, 156, 214)
    palette.setColor(QPalette.ColorRole.Window, base)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, QColor(24, 25, 28))
    palette.setColor(QPalette.ColorRole.AlternateBase, panel)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, panel)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.Highlight, accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(18, 18, 20))
    palette.setColor(QPalette.ColorRole.ToolTipBase, panel)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    app.setPalette(palette)
    app.setStyleSheet(
        "QGroupBox { border: 1px solid #3a3d42; border-radius: 6px;"
        " margin-top: 10px; padding-top: 8px; }"
        "QGroupBox::title { subcontrol-origin: margin; left: 10px;"
        " padding: 0 4px; color: #9aa0a6; }"
        "QPushButton { padding: 5px 12px; border: 1px solid #3a3d42;"
        " border-radius: 4px; background: #2b2d31; }"
        "QPushButton:hover { background: #35383d; }"
        "QPushButton:disabled { color: #6b6f76; }"
        "QPlainTextEdit, QTableWidget { border: 1px solid #3a3d42; }"
    )


class MainWindow(QMainWindow):
    """The MonitorDeck window."""

    scan_requested = Signal(object)
    refresh_requested = Signal()
    write_requested = Signal(object, int, int)
    shutdown_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MonitorDeck")
        self.resize(1080, 720)
        self._monitors: list[ddc.Monitor] = []
        self._controls: dict[int, QWidget] = {}

        self._thread = QThread(self)
        self._worker = DDCWorker()
        self._worker.moveToThread(self._thread)
        self._thread.start()

        self._build_ui()
        self._connect_worker()
        self.refresh_requested.emit()

    # ---------------------------------------------------------------- layout

    def _build_ui(self) -> None:
        """Assemble the toolbar, panels and status bar."""
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self._build_toolbar())

        splitter = QSplitter(Qt.Orientation.Vertical)
        panels = QSplitter(Qt.Orientation.Horizontal)
        panels.addWidget(self._build_controls_panel())
        panels.addWidget(self._build_table_panel())
        panels.setSizes([420, 660])
        splitter.addWidget(panels)
        splitter.addWidget(self._build_log_panel())
        splitter.setSizes([500, 200])
        layout.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready")

    def _build_toolbar(self) -> QWidget:
        """Monitor selector, refresh and scan buttons."""
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)

        self._monitor_combo = QComboBox()
        self._monitor_combo.setMinimumWidth(320)
        self._monitor_combo.currentIndexChanged.connect(self._on_monitor_changed)

        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(lambda: self.refresh_requested.emit())
        self._scan_button = QPushButton("Scan capabilities")
        self._scan_button.clicked.connect(self._on_scan_clicked)
        self._scan_button.setEnabled(False)

        layout.addWidget(QLabel("Monitor:"))
        layout.addWidget(self._monitor_combo, 1)
        layout.addWidget(self._refresh_button)
        layout.addWidget(self._scan_button)
        return bar

    def _build_controls_panel(self) -> QWidget:
        """Scrollable area that holds one widget per supported control."""
        self._controls_form = QFormLayout()
        self._controls_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        holder = QWidget()
        holder.setLayout(self._controls_form)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)

        box = QGroupBox("Controls")
        box_layout = QVBoxLayout(box)
        self._controls_hint = QLabel("Scan capabilities to load controls.")
        self._controls_hint.setStyleSheet("color: #9aa0a6;")
        box_layout.addWidget(self._controls_hint)
        box_layout.addWidget(scroll, 1)
        return box

    def _build_table_panel(self) -> QWidget:
        """Capability table: code, name, current, max, access, values."""
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Code", "Name", "Current", "Max", "Access", "Values"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)

        box = QGroupBox("Capabilities")
        layout = QVBoxLayout(box)
        self._model_label = QLabel("No scan yet.")
        self._model_label.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self._model_label)
        layout.addWidget(self._table, 1)
        return box

    def _build_log_panel(self) -> QWidget:
        """Read-only log view fed by the logging module."""
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)
        self._log_view.setFont(QFont("Consolas", 9))

        self._log_bridge = LogBridge()
        self._log_bridge.message.connect(self._log_view.appendPlainText)
        handler = QtLogHandler(self._log_bridge)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s",
                                               datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(handler)

        box = QGroupBox("Log")
        layout = QVBoxLayout(box)
        layout.addWidget(self._log_view)
        return box

    # --------------------------------------------------------------- wiring

    def _connect_worker(self) -> None:
        """Connect window signals to the worker and back."""
        self.refresh_requested.connect(self._worker.refresh_monitors)
        self.scan_requested.connect(self._worker.scan)
        self.write_requested.connect(self._worker.write)
        self.shutdown_requested.connect(self._worker.shutdown)

        self._worker.monitors_ready.connect(self._on_monitors_ready)
        self._worker.capabilities_ready.connect(self._on_capabilities_ready)
        self._worker.value_written.connect(self._on_value_written)
        self._worker.failed.connect(self._on_failed)
        self._worker.busy.connect(self._on_busy)

    # --------------------------------------------------------------- slots

    @Slot(list)
    def _on_monitors_ready(self, monitors: list[ddc.Monitor]) -> None:
        """Fill the selector with the monitors that answered DDC/CI."""
        self._monitors = monitors
        self._monitor_combo.blockSignals(True)
        self._monitor_combo.clear()
        for monitor in monitors:
            self._monitor_combo.addItem(
                f"{monitor.description} ({monitor.device})", monitor)
        self._monitor_combo.blockSignals(False)
        self._scan_button.setEnabled(bool(monitors))
        if not monitors:
            self._model_label.setText(
                "No monitor answered DDC/CI. Check the cable (avoid docks) "
                "and enable DDC/CI in the monitor's OSD.")
        else:
            self._on_monitor_changed(self._monitor_combo.currentIndex())

    @Slot(int)
    def _on_monitor_changed(self, index: int) -> None:
        """Clear controls when the selected monitor changes."""
        self._clear_controls()
        self._table.setRowCount(0)
        monitor = self._current_monitor()
        if monitor is not None:
            self._model_label.setText(f"{monitor.description} - not scanned yet.")

    def _on_scan_clicked(self) -> None:
        monitor = self._current_monitor()
        if monitor is not None:
            self.scan_requested.emit(monitor)

    @Slot(object, object, list)
    def _on_capabilities_ready(self, monitor: ddc.Monitor,
                               caps: caps_mod.Capabilities,
                               rows: list[caps_mod.ScanRow]) -> None:
        """Populate the table and rebuild the controls for the scanned codes."""
        self._model_label.setText(
            f"{caps.model or monitor.description}  -  {caps.monitor_type or '?'}, "
            f"MCCS {caps.mccs_version or '?'}, {len(caps.features)} codes")
        self._fill_table(rows)
        self._build_controls(rows)

    def _fill_table(self, rows: list[caps_mod.ScanRow]) -> None:
        """Write the scan rows into the capability table."""
        self._table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = " ".join(
                f"{v:02X}={caps_mod.value_name(row.code, v)}"
                if caps_mod.value_name(row.code, v) else f"{v:02X}"
                for v in (row.allowed or [])
            )
            cells = [f"0x{row.code:02X}", row.name, row.current_label,
                     "-" if row.maximum is None else str(row.maximum),
                     row.access, values]
            for column, cell in enumerate(cells):
                self._table.setItem(index, column, QTableWidgetItem(cell))
        self._table.resizeColumnsToContents()

    def _build_controls(self, rows: list[caps_mod.ScanRow]) -> None:
        """Create a control for every supported standard code."""
        self._clear_controls()
        by_code = {row.code: row for row in rows}
        for code in caps_mod.STANDARD_CONTROLS:
            row = by_code.get(code)
            if row is None:
                log.info("%s (0x%02X) not published by this monitor - hidden",
                         caps_mod.code_name(code), code)
                continue
            widget = build_control(row)
            if widget is None:
                continue
            widget.value_set.connect(self._on_control_changed)
            self._controls_form.addRow(f"{row.name}", widget)
            self._controls[code] = widget
        self._controls_hint.setText(
            f"{len(self._controls)} of {len(caps_mod.STANDARD_CONTROLS)} standard "
            f"controls supported by this monitor.")

    def _clear_controls(self) -> None:
        """Remove every control widget from the form."""
        while self._controls_form.count():
            item = self._controls_form.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._controls.clear()
        self._controls_hint.setText("Scan capabilities to load controls.")

    @Slot(int, int)
    def _on_control_changed(self, code: int, value: int) -> None:
        """Send a control change to the worker."""
        monitor = self._current_monitor()
        if monitor is not None:
            self.write_requested.emit(monitor, code, value)

    @Slot(int, int)
    def _on_value_written(self, code: int, value: int) -> None:
        self.statusBar().showMessage(
            f"{caps_mod.code_name(code)} set to {value}", 4000)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.statusBar().showMessage(message, 8000)

    @Slot(bool, str)
    def _on_busy(self, busy: bool, message: str) -> None:
        """Disable buttons while the worker is talking to a monitor."""
        self._refresh_button.setEnabled(not busy)
        self._scan_button.setEnabled(not busy and bool(self._monitors))
        self.statusBar().showMessage(message or "Ready")

    # --------------------------------------------------------------- helpers

    def _current_monitor(self) -> Optional[ddc.Monitor]:
        """The monitor selected in the combo box, if any."""
        data = self._monitor_combo.currentData()
        return data if isinstance(data, ddc.Monitor) else None

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Release monitor handles and stop the worker thread."""
        self.shutdown_requested.emit()
        self._thread.quit()
        self._thread.wait(3000)
        super().closeEvent(event)
