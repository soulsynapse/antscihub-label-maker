#!/usr/bin/env python
"""
PyQt6 GUI for building Avery 5267 label sheets.

Layout:
- Left: live sheet preview
- Right: settings + per-label text table
"""

from __future__ import absolute_import, division, print_function

import json
import os
import sys
import tempfile

from PyQt6.QtCore import QRectF, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QImage,
    QImageReader,
    QPageLayout,
    QPageSize,
    QPainter,
    QPen,
)
from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    import cv2
except ImportError:
    cv2 = None

# Ensure importing sibling script works when launching from repo root.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from generate_avery_5267_labels import (  # noqa: E402
    COLS,
    DEFAULT_ARUCO_DICT,
    DEFAULT_DPI,
    H_PITCH_IN,
    LABEL_HEIGHT_IN,
    LABEL_WIDTH_IN,
    LEFT_MARGIN_IN,
    MAX_LABELS,
    PAGE_HEIGHT_IN,
    PAGE_WIDTH_IN,
    ROWS,
    TOP_MARGIN_IN,
    V_PITCH_IN,
    generate_avery_5267_sheet,
)


ARUCO_DICTS = [
    "DICT_4X4_50",
    "DICT_4X4_100",
    "DICT_4X4_250",
    "DICT_5X5_50",
    "DICT_5X5_100",
    "DICT_5X5_250",
    "DICT_6X6_50",
    "DICT_6X6_100",
    "DICT_6X6_250",
    "DICT_7X7_50",
    "DICT_7X7_100",
    "DICT_7X7_250",
]


def _safe_int(text, fallback):
    try:
        return int(text)
    except (TypeError, ValueError):
        return fallback


def _safe_bool(value, fallback=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return fallback


class SheetPreviewWidget(QWidget):
    wheel_adjust_requested = pyqtSignal(int, bool)

    def __init__(self, parent=None):
        super(SheetPreviewWidget, self).__init__(parent)
        self.setMinimumSize(520, 700)
        self.missing = 0
        self.row_specs = []
        self._aruco_cache = {}

    def wheelEvent(self, event):  # pylint: disable=invalid-name
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return

        steps = int(delta / 120)
        if steps == 0:
            steps = 1 if delta > 0 else -1

        ctrl_held = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        self.wheel_adjust_requested.emit(steps, ctrl_held)
        event.accept()

    def set_preview_data(self, missing, row_specs):
        self.missing = int(missing)
        self.row_specs = row_specs[:]
        self.update()

    def _fit_page_rect(self):
        margin = 18
        area = self.rect().adjusted(margin, margin, -margin, -margin)
        page_ratio = PAGE_WIDTH_IN / PAGE_HEIGHT_IN
        area_ratio = area.width() / float(max(1, area.height()))

        if area_ratio > page_ratio:
            page_h = area.height()
            page_w = int(round(page_h * page_ratio))
        else:
            page_w = area.width()
            page_h = int(round(page_w / page_ratio))

        x = area.x() + (area.width() - page_w) // 2
        y = area.y() + (area.height() - page_h) // 2
        return QRectF(x, y, page_w, page_h)

    def _label_rect(self, slot_index, page_rect):
        row = slot_index // COLS
        col = slot_index % COLS

        x_in = LEFT_MARGIN_IN + (col * H_PITCH_IN)
        y_in = TOP_MARGIN_IN + (row * V_PITCH_IN)

        x = page_rect.x() + (x_in / PAGE_WIDTH_IN) * page_rect.width()
        y = page_rect.y() + (y_in / PAGE_HEIGHT_IN) * page_rect.height()
        w = (LABEL_WIDTH_IN / PAGE_WIDTH_IN) * page_rect.width()
        h = (LABEL_HEIGHT_IN / PAGE_HEIGHT_IN) * page_rect.height()
        return QRectF(x, y, w, h)

    def _get_aruco_qimage(self, side_px, marker_id, dict_name):
        if side_px < 8:
            return None

        marker_id = _safe_int(marker_id, 0)
        key = (str(dict_name), int(marker_id), int(side_px))
        cached = self._aruco_cache.get(key)
        if cached is not None:
            return cached

        if cv2 is None or not hasattr(cv2, "aruco"):
            return None
        if not hasattr(cv2.aruco, str(dict_name)):
            return None

        try:
            dictionary_id = getattr(cv2.aruco, str(dict_name))
            dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)

            # Keep marker IDs valid for the selected dictionary.
            dictionary_size = 0
            if hasattr(dictionary, "bytesList"):
                try:
                    dictionary_size = int(dictionary.bytesList.shape[0])
                except Exception:  # pylint: disable=broad-except
                    dictionary_size = 0
            if dictionary_size > 0:
                marker_id = marker_id % dictionary_size

            if hasattr(cv2.aruco, "generateImageMarker"):
                marker = cv2.aruco.generateImageMarker(dictionary, marker_id, int(side_px))
            elif hasattr(cv2.aruco, "drawMarker"):
                marker = cv2.aruco.drawMarker(dictionary, marker_id, int(side_px))
            else:
                return None

            h, w = marker.shape[:2]
            bytes_per_line = marker.strides[0]
            qimg = QImage(
                marker.data,
                w,
                h,
                bytes_per_line,
                QImage.Format.Format_Grayscale8,
            ).copy()
        except Exception:  # pylint: disable=broad-except
            return None

        self._aruco_cache[key] = qimg
        if len(self._aruco_cache) > 512:
            self._aruco_cache.pop(next(iter(self._aruco_cache)))
        return qimg

    def paintEvent(self, event):  # pylint: disable=unused-argument
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(245, 246, 248))

        page_rect = self._fit_page_rect()

        # Soft shadow + page.
        shadow_rect = page_rect.adjusted(6, 6, 6, 6)
        painter.fillRect(shadow_rect, QColor(220, 223, 228))
        painter.fillRect(page_rect, QColor(255, 255, 255))
        painter.setPen(QPen(QColor(170, 174, 181), 1))
        painter.drawRect(page_rect)

        # Draw all label outlines.
        painter.setPen(QPen(QColor(228, 230, 234), 1))
        for slot in range(MAX_LABELS):
            painter.drawRect(self._label_rect(slot, page_rect))

        # Used/missing labels shading.
        for slot in range(max(0, min(self.missing, MAX_LABELS))):
            r = self._label_rect(slot, page_rect)
            painter.fillRect(r, QColor(238, 240, 243))
            painter.setPen(QPen(QColor(210, 212, 216), 1))
            painter.drawLine(r.topLeft(), r.bottomRight())
            painter.drawLine(r.topRight(), r.bottomLeft())

        # Active labels and text.
        for seq_index, spec in enumerate(self.row_specs):
            slot = self.missing + seq_index
            if slot >= MAX_LABELS:
                break
            r = self._label_rect(slot, page_rect)
            painter.fillRect(r, QColor(232, 245, 255))
            painter.setPen(QPen(QColor(86, 152, 210), 1.4))
            painter.drawRect(r)

            inner = r.adjusted(2.0, 2.0, -2.0, -2.0)
            text_x = inner.x()
            text_w = inner.width()
            has_aruco = bool(spec.get("has_aruco", False))

            if has_aruco:
                marker_size = min(inner.height() - 4.0, inner.width() * 0.25)
                marker = QRectF(
                    inner.x(),
                    inner.y() + (inner.height() - marker_size) / 2.0,
                    marker_size,
                    marker_size,
                )
                marker_side_px = max(16, int(round(marker_size)))
                marker_img = self._get_aruco_qimage(
                    marker_side_px,
                    spec.get("aruco_id", 0),
                    spec.get("aruco_dict", DEFAULT_ARUCO_DICT),
                )
                if marker_img is not None:
                    painter.drawImage(marker, marker_img)
                    painter.setPen(QPen(QColor(28, 38, 46), 1))
                    painter.drawRect(marker)
                else:
                    painter.fillRect(marker, QColor(18, 18, 18))
                    painter.setPen(QPen(QColor(255, 255, 255), 1))
                    painter.drawRect(marker)
                text_x = marker.x() + marker.width() + 4.0
                text_w = max(4.0, inner.right() - text_x)

            lines = [line for line in spec.get("lines", []) if line.strip()]
            if not lines:
                continue

            font = QFont("Arial", 6)
            painter.setFont(font)
            painter.setPen(QPen(QColor(14, 34, 48), 1))
            line_h = inner.height() / float(max(1, min(3, len(lines))))
            for i, line in enumerate(lines[:3]):
                line_rect = QRectF(
                    text_x,
                    inner.y() + i * line_h,
                    text_w,
                    line_h,
                )
                painter.drawText(
                    line_rect,
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    line,
                )

        # Title.
        title_rect = QRectF(page_rect.x(), page_rect.y() - 18, page_rect.width(), 14)
        painter.setPen(QPen(QColor(90, 96, 104), 1))
        painter.setFont(QFont("Arial", 8))
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            "Avery 5267 preview",
        )

        painter.end()


class Avery5267Window(QMainWindow):
    COL_SLOT = 0
    COL_USE_ARUCO = 1
    COL_ARUCO_ID = 2
    COL_LINE1 = 3
    COL_LINE2 = 4
    COL_LINE3 = 5
    SETTINGS_FILENAME = ".avery_5267_gui.ini"

    def __init__(self):
        super(Avery5267Window, self).__init__()
        self._table_syncing = False
        repo_root = os.path.dirname(SCRIPT_DIR)
        self.settings_path = os.path.join(repo_root, self.SETTINGS_FILENAME)
        self.settings = QSettings(self.settings_path, QSettings.Format.IniFormat)

        self.setWindowTitle("Avery 5267 Label Maker")
        self.resize(1400, 900)

        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(self.splitter)

        # Left: preview
        self.preview = SheetPreviewWidget()
        self.splitter.addWidget(self.preview)

        # Right: controls
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setSpacing(10)
        self.splitter.addWidget(right_panel)

        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        right_layout.addLayout(form)

        self.missing_spin = QSpinBox()
        self.missing_spin.setRange(0, MAX_LABELS - 1)
        self.missing_spin.setValue(0)
        form.addRow("Missing Labels", self.missing_spin)

        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, MAX_LABELS)
        self.count_spin.setValue(MAX_LABELS)
        form.addRow("Labels To Print", self.count_spin)

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(300, 2400)
        self.dpi_spin.setSingleStep(100)
        self.dpi_spin.setValue(DEFAULT_DPI)
        form.addRow("DPI", self.dpi_spin)

        self.output_edit = QLineEdit("avery_5267_labels")
        form.addRow("Output Stem", self.output_edit)

        self.default_aruco_checkbox = QCheckBox("Use ArUco by default")
        self.default_aruco_checkbox.setChecked(False)
        form.addRow("", self.default_aruco_checkbox)

        self.aruco_start_spin = QSpinBox()
        self.aruco_start_spin.setRange(0, 999999)
        self.aruco_start_spin.setValue(0)
        form.addRow("ArUco Start ID", self.aruco_start_spin)

        self.aruco_dict_combo = QComboBox()
        self.aruco_dict_combo.addItems(ARUCO_DICTS)
        default_idx = self.aruco_dict_combo.findText(DEFAULT_ARUCO_DICT)
        if default_idx >= 0:
            self.aruco_dict_combo.setCurrentIndex(default_idx)
        form.addRow("ArUco Dictionary", self.aruco_dict_combo)

        btn_row = QHBoxLayout()
        right_layout.addLayout(btn_row)

        self.apply_defaults_btn = QPushButton("Apply Defaults To Rows")
        btn_row.addWidget(self.apply_defaults_btn)

        self.refresh_preview_btn = QPushButton("Refresh Preview")
        btn_row.addWidget(self.refresh_preview_btn)

        self.save_btn = QPushButton("Save")
        btn_row.addWidget(self.save_btn)

        self.print_btn = QPushButton("Print")
        btn_row.addWidget(self.print_btn)

        table_label = QLabel("Text Entry Table (one row per label)")
        right_layout.addWidget(table_label)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["Slot", "Use ArUco", "ArUco ID", "Line 1", "Line 2", "Line 3"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_SLOT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(
            self.COL_USE_ARUCO, QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(
            self.COL_ARUCO_ID, QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(self.COL_LINE1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_LINE2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_LINE3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        right_layout.addWidget(self.table, 1)

        self.status_label = QLabel("Ready.")
        right_layout.addWidget(self.status_label)

        self._build_menu_bar()

        # Signals
        self.missing_spin.valueChanged.connect(self._on_missing_changed)
        self.count_spin.valueChanged.connect(self._on_count_changed)
        self.refresh_preview_btn.clicked.connect(self.refresh_preview)
        self.apply_defaults_btn.clicked.connect(self.apply_defaults_to_rows)
        self.save_btn.clicked.connect(self.save_files)
        self.print_btn.clicked.connect(self.print_sheet)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.default_aruco_checkbox.toggled.connect(self._on_default_toggled)
        self.aruco_start_spin.valueChanged.connect(self._on_defaults_changed)
        self.preview.wheel_adjust_requested.connect(self._on_preview_wheel_adjust)

        self._load_session_state()
        self._on_missing_changed(self.missing_spin.value())
        self.refresh_preview()

    def _build_menu_bar(self):
        file_menu = self.menuBar().addMenu("&File")

        save_labels_action = QAction("Save Labels", self)
        save_labels_action.setShortcut("Ctrl+S")
        save_labels_action.triggered.connect(self.save_files)
        file_menu.addAction(save_labels_action)

        print_action = QAction("Print...", self)
        print_action.setShortcut("Ctrl+P")
        print_action.triggered.connect(self.print_sheet)
        file_menu.addAction(print_action)

        file_menu.addSeparator()

        save_session_action = QAction("Save Session...", self)
        save_session_action.setShortcut("Ctrl+Shift+S")
        save_session_action.triggered.connect(self.save_session_as)
        file_menu.addAction(save_session_action)

        load_session_action = QAction("Load Session...", self)
        load_session_action.setShortcut("Ctrl+O")
        load_session_action.triggered.connect(self.load_session_from_file)
        file_menu.addAction(load_session_action)

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    def _on_preview_wheel_adjust(self, steps, ctrl_held):
        step_count = abs(int(steps))
        direction = 1 if steps > 0 else -1
        for _ in range(step_count):
            if ctrl_held:
                self.count_spin.setValue(self.count_spin.value() + direction)
            else:
                self.missing_spin.setValue(self.missing_spin.value() + direction)

    def closeEvent(self, event):  # pylint: disable=invalid-name
        self._save_session_state()
        super(Avery5267Window, self).closeEvent(event)

    def _capture_session_payload(self):
        self._sync_table_rows()
        return {
            "version": 1,
            "missing": self.missing_spin.value(),
            "count": self.count_spin.value(),
            "dpi": self.dpi_spin.value(),
            "output_stem": self.output_edit.text().strip(),
            "default_aruco": self.default_aruco_checkbox.isChecked(),
            "aruco_start_id": self.aruco_start_spin.value(),
            "aruco_dict": self.aruco_dict_combo.currentText(),
            "rows": self._collect_row_specs(),
            "splitter_sizes": self.splitter.sizes(),
        }

    def _apply_session_payload(self, payload):
        if not isinstance(payload, dict):
            return

        rows_payload = payload.get("rows", [])
        if not isinstance(rows_payload, list):
            rows_payload = []

        default_missing = self.missing_spin.value()
        default_count = self.count_spin.value()
        default_row_count = len(rows_payload) if rows_payload else default_count

        missing = _safe_int(payload.get("missing", default_missing), default_missing)
        missing = max(0, min(MAX_LABELS - 1, missing))

        count = _safe_int(payload.get("count", default_row_count), default_row_count)
        count = max(1, min(MAX_LABELS - missing, count))

        dpi = _safe_int(payload.get("dpi", DEFAULT_DPI), DEFAULT_DPI)
        dpi = max(300, min(2400, dpi))

        output_stem = str(payload.get("output_stem", self.output_edit.text().strip())).strip()
        default_aruco = _safe_bool(
            payload.get("default_aruco", self.default_aruco_checkbox.isChecked())
        )
        aruco_start = _safe_int(
            payload.get("aruco_start_id", self.aruco_start_spin.value()),
            self.aruco_start_spin.value(),
        )
        aruco_start = max(0, min(999999, aruco_start))
        aruco_dict = str(payload.get("aruco_dict", self.aruco_dict_combo.currentText())).strip()

        widgets_to_block = [
            self.missing_spin,
            self.count_spin,
            self.dpi_spin,
            self.output_edit,
            self.default_aruco_checkbox,
            self.aruco_start_spin,
            self.aruco_dict_combo,
        ]
        for w in widgets_to_block:
            w.blockSignals(True)
        try:
            self.missing_spin.setValue(missing)
            self.count_spin.setMaximum(MAX_LABELS - missing)
            self.count_spin.setValue(count)
            self.dpi_spin.setValue(dpi)
            if output_stem:
                self.output_edit.setText(output_stem)
            self.default_aruco_checkbox.setChecked(default_aruco)
            self.aruco_start_spin.setValue(aruco_start)
            dict_idx = self.aruco_dict_combo.findText(aruco_dict)
            if dict_idx >= 0:
                self.aruco_dict_combo.setCurrentIndex(dict_idx)
        finally:
            for w in widgets_to_block:
                w.blockSignals(False)

        self._sync_table_rows()

        if rows_payload:
            self._table_syncing = True
            try:
                for row in range(min(len(rows_payload), self.table.rowCount())):
                    spec = rows_payload[row] if isinstance(rows_payload[row], dict) else {}
                    lines = spec.get("lines", [])
                    if isinstance(lines, str):
                        lines = [lines]
                    lines = [str(x) for x in lines]

                    use_aruco_item = self.table.item(row, self.COL_USE_ARUCO)
                    if use_aruco_item is not None:
                        use_aruco_item.setCheckState(
                            Qt.CheckState.Checked
                            if _safe_bool(spec.get("has_aruco", False))
                            else Qt.CheckState.Unchecked
                        )

                    aruco_item = self.table.item(row, self.COL_ARUCO_ID)
                    if aruco_item is not None:
                        aruco_item.setText(str(_safe_int(spec.get("aruco_id", row), row)))

                    for i, col in enumerate((self.COL_LINE1, self.COL_LINE2, self.COL_LINE3)):
                        item = self.table.item(row, col)
                        if item is not None:
                            item.setText(lines[i] if i < len(lines) else "")
            finally:
                self._table_syncing = False

        splitter_sizes = payload.get("splitter_sizes")
        if isinstance(splitter_sizes, str):
            splitter_sizes = [x.strip() for x in splitter_sizes.split(",") if x.strip()]
        if isinstance(splitter_sizes, list) and len(splitter_sizes) >= 2:
            try:
                self.splitter.setSizes([int(splitter_sizes[0]), int(splitter_sizes[1])])
            except (TypeError, ValueError):
                pass

        self.refresh_preview()

    def _save_session_state(self):
        payload = self._capture_session_payload()
        self.settings.setValue("window_geometry", self.saveGeometry())
        self.settings.setValue("splitter_sizes", payload.get("splitter_sizes", []))
        self.settings.setValue("missing", payload.get("missing", 0))
        self.settings.setValue("count", payload.get("count", 1))
        self.settings.setValue("dpi", payload.get("dpi", DEFAULT_DPI))
        self.settings.setValue("output_stem", payload.get("output_stem", ""))
        self.settings.setValue("default_aruco", payload.get("default_aruco", False))
        self.settings.setValue("aruco_start_id", payload.get("aruco_start_id", 0))
        self.settings.setValue("aruco_dict", payload.get("aruco_dict", DEFAULT_ARUCO_DICT))
        self.settings.setValue("rows_json", json.dumps(payload.get("rows", [])))
        self.settings.setValue("session_json", json.dumps(payload))
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            raise RuntimeError("Unable to persist settings at: {0}".format(self.settings_path))

    def _load_session_state(self):
        payload = None
        session_json = self.settings.value("session_json", "")
        if session_json:
            try:
                payload = json.loads(str(session_json))
            except Exception:  # pylint: disable=broad-except
                payload = None

        if not isinstance(payload, dict):
            rows_json = self.settings.value("rows_json", "")
            try:
                saved_rows = json.loads(rows_json) if rows_json else []
            except Exception:  # pylint: disable=broad-except
                saved_rows = []
            payload = {
                "missing": self.settings.value("missing", self.missing_spin.value()),
                "count": self.settings.value("count", self.count_spin.value()),
                "dpi": self.settings.value("dpi", DEFAULT_DPI),
                "output_stem": self.settings.value("output_stem", self.output_edit.text()),
                "default_aruco": self.settings.value(
                    "default_aruco", self.default_aruco_checkbox.isChecked()
                ),
                "aruco_start_id": self.settings.value(
                    "aruco_start_id", self.aruco_start_spin.value()
                ),
                "aruco_dict": self.settings.value(
                    "aruco_dict", self.aruco_dict_combo.currentText()
                ),
                "rows": saved_rows if isinstance(saved_rows, list) else [],
                "splitter_sizes": self.settings.value("splitter_sizes", []),
            }

        self._apply_session_payload(payload)

        geom = self.settings.value("window_geometry")
        if geom is not None:
            try:
                self.restoreGeometry(geom)
            except Exception:  # pylint: disable=broad-except
                pass

        splitter_sizes = self.settings.value("splitter_sizes")
        if isinstance(splitter_sizes, str):
            splitter_sizes = [x.strip() for x in splitter_sizes.split(",") if x.strip()]
        if isinstance(splitter_sizes, list) and len(splitter_sizes) >= 2:
            try:
                self.splitter.setSizes([int(splitter_sizes[0]), int(splitter_sizes[1])])
            except (TypeError, ValueError):
                pass

    def save_session_as(self):
        default_path = str(self.settings.value("session_last_path", "")).strip()
        if not default_path:
            default_path = os.path.join(os.path.dirname(SCRIPT_DIR), "label_session.json")

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Session",
            default_path,
            "Label Session (*.json);;All Files (*)",
        )
        if not file_path:
            self.status_label.setText("Save session cancelled.")
            return
        if not file_path.lower().endswith(".json"):
            file_path += ".json"

        payload = self._capture_session_payload()
        try:
            with open(file_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            self.settings.setValue("session_last_path", file_path)
            self.settings.sync()
        except Exception as exc:  # pylint: disable=broad-except
            QMessageBox.critical(self, "Save Session Failed", str(exc))
            self.status_label.setText("Save session failed.")
            return

        self.status_label.setText("Session saved: {0}".format(file_path))

    def load_session_from_file(self):
        default_path = str(self.settings.value("session_last_path", "")).strip()
        if not default_path:
            default_path = os.path.join(os.path.dirname(SCRIPT_DIR), "label_session.json")

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Session",
            default_path,
            "Label Session (*.json);;All Files (*)",
        )
        if not file_path:
            self.status_label.setText("Load session cancelled.")
            return

        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self._apply_session_payload(payload)
            self._save_session_state()
            self.settings.setValue("session_last_path", file_path)
            self.settings.sync()
        except Exception as exc:  # pylint: disable=broad-except
            QMessageBox.critical(self, "Load Session Failed", str(exc))
            self.status_label.setText("Load session failed.")
            return

        self.status_label.setText("Session loaded: {0}".format(file_path))

    def _make_text_item(self, text, editable=True, center=False):
        item = QTableWidgetItem(text)
        flags = (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemNeverHasChildren
        )
        if editable:
            flags |= Qt.ItemFlag.ItemIsEditable
        item.setFlags(flags)
        if center:
            item.setTextAlignment(
                int(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
            )
        return item

    def _make_checkbox_item(self, checked):
        item = QTableWidgetItem("")
        flags = (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemNeverHasChildren
        )
        item.setFlags(flags)
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        item.setCheckState(state)
        item.setTextAlignment(
            int(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        )
        return item

    def _get_item_text(self, row, col):
        item = self.table.item(row, col)
        return item.text().strip() if item else ""

    def _is_row_aruco_enabled(self, row):
        item = self.table.item(row, self.COL_USE_ARUCO)
        if item is None:
            return False
        return item.checkState() == Qt.CheckState.Checked

    def _on_missing_changed(self, value):
        max_count = MAX_LABELS - int(value)
        self.count_spin.setMaximum(max_count)
        if self.count_spin.value() > max_count:
            self.count_spin.setValue(max_count)
        self._sync_table_rows()
        self.refresh_preview()

    def _on_count_changed(self, value):  # pylint: disable=unused-argument
        self._sync_table_rows()
        self.refresh_preview()

    def _on_default_toggled(self, checked):  # pylint: disable=unused-argument
        self.status_label.setText("Defaults updated. Click 'Apply Defaults To Rows' to copy.")

    def _on_defaults_changed(self, value):  # pylint: disable=unused-argument
        self.status_label.setText("Defaults updated. Click 'Apply Defaults To Rows' to copy.")

    def _on_table_item_changed(self, item):  # pylint: disable=unused-argument
        if self._table_syncing:
            return
        self.refresh_preview()

    def _sync_table_rows(self):
        self._table_syncing = True
        try:
            missing = self.missing_spin.value()
            count = self.count_spin.value()
            existing = self.table.rowCount()
            self.table.setRowCount(count)

            for row in range(count):
                slot = missing + row
                slot_item = self.table.item(row, self.COL_SLOT)
                if slot_item is None:
                    slot_item = self._make_text_item(str(slot), editable=False, center=True)
                    self.table.setItem(row, self.COL_SLOT, slot_item)
                else:
                    slot_item.setText(str(slot))

                use_aruco = self.table.item(row, self.COL_USE_ARUCO)
                if use_aruco is None:
                    use_aruco = self._make_checkbox_item(self.default_aruco_checkbox.isChecked())
                    self.table.setItem(row, self.COL_USE_ARUCO, use_aruco)

                aruco_item = self.table.item(row, self.COL_ARUCO_ID)
                if aruco_item is None:
                    default_id = self.aruco_start_spin.value() + row
                    aruco_item = self._make_text_item(str(default_id), editable=True, center=True)
                    self.table.setItem(row, self.COL_ARUCO_ID, aruco_item)

                for col in (self.COL_LINE1, self.COL_LINE2, self.COL_LINE3):
                    if self.table.item(row, col) is None:
                        self.table.setItem(row, col, self._make_text_item("", editable=True))

            # If row count increased, ensure new rows inherit defaults.
            if count > existing:
                for row in range(existing, count):
                    use_aruco = self.table.item(row, self.COL_USE_ARUCO)
                    if use_aruco is not None:
                        state = (
                            Qt.CheckState.Checked
                            if self.default_aruco_checkbox.isChecked()
                            else Qt.CheckState.Unchecked
                        )
                        use_aruco.setCheckState(state)
        finally:
            self._table_syncing = False

    def apply_defaults_to_rows(self):
        self._table_syncing = True
        try:
            count = self.table.rowCount()
            start_id = self.aruco_start_spin.value()
            state = (
                Qt.CheckState.Checked
                if self.default_aruco_checkbox.isChecked()
                else Qt.CheckState.Unchecked
            )
            for row in range(count):
                use_aruco = self.table.item(row, self.COL_USE_ARUCO)
                if use_aruco is None:
                    use_aruco = self._make_checkbox_item(state == Qt.CheckState.Checked)
                    self.table.setItem(row, self.COL_USE_ARUCO, use_aruco)
                use_aruco.setCheckState(state)

                aruco_item = self.table.item(row, self.COL_ARUCO_ID)
                if aruco_item is None:
                    aruco_item = self._make_text_item("", editable=True, center=True)
                    self.table.setItem(row, self.COL_ARUCO_ID, aruco_item)
                aruco_item.setText(str(start_id + row))
        finally:
            self._table_syncing = False

        self.refresh_preview()
        self.status_label.setText("Defaults copied into rows.")

    def _collect_row_specs(self):
        specs = []
        for row in range(self.table.rowCount()):
            line1 = self._get_item_text(row, self.COL_LINE1)
            line2 = self._get_item_text(row, self.COL_LINE2)
            line3 = self._get_item_text(row, self.COL_LINE3)
            lines = [line for line in (line1, line2, line3) if line]

            aruco_default = self.aruco_start_spin.value() + row
            aruco_text = self._get_item_text(row, self.COL_ARUCO_ID)
            aruco_id = _safe_int(aruco_text, aruco_default)

            specs.append(
                {
                    "lines": lines,
                    "has_aruco": self._is_row_aruco_enabled(row),
                    "aruco_id": aruco_id,
                    "aruco_dict": self.aruco_dict_combo.currentText(),
                }
            )
        return specs

    def refresh_preview(self):
        self._sync_table_rows()
        specs = self._collect_row_specs()
        self.preview.set_preview_data(self.missing_spin.value(), specs)
        self.status_label.setText("Preview updated.")

    def _generate_outputs(self, output_stem):
        self._sync_table_rows()
        if not output_stem:
            raise ValueError("Please set an output file stem.")

        output_dir = os.path.dirname(output_stem)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        row_specs = self._collect_row_specs()

        def label_spec_fn(seq_index, slot_index):  # pylint: disable=unused-argument
            if seq_index < len(row_specs):
                return row_specs[seq_index]
            return {
                "lines": [],
                "has_aruco": False,
                "aruco_id": 0,
                "aruco_dict": self.aruco_dict_combo.currentText(),
            }

        return generate_avery_5267_sheet(
            label_spec_fn=label_spec_fn,
            output_stem=output_stem,
            missing=self.missing_spin.value(),
            count=self.count_spin.value(),
            dpi=self.dpi_spin.value(),
        )

    def _print_png(self, png_path):
        if hasattr(QImageReader, "setAllocationLimit"):
            # 0 disables the default cap (commonly 256 MB), needed for 1200 DPI sheets.
            QImageReader.setAllocationLimit(0)

        reader = QImageReader(png_path)
        image = reader.read()
        if image.isNull():
            err = reader.errorString() if hasattr(reader, "errorString") else "unknown error"
            raise RuntimeError(
                "Unable to load print image: {0} ({1})".format(png_path, err)
            )

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setFullPage(True)
        printer.setPageOrientation(QPageLayout.Orientation.Portrait)
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
        printer.setResolution(self.dpi_spin.value())

        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle("Print Avery 5267 Sheet")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False

        painter = QPainter()
        if not painter.begin(printer):
            raise RuntimeError("Unable to start print job.")
        try:
            target = printer.pageLayout().paintRectPixels(printer.resolution())
            painter.drawImage(target, image)
        finally:
            painter.end()
        return True

    def save_files(self):
        output_stem = self.output_edit.text().strip()
        try:
            outputs = self._generate_outputs(output_stem)
            self._save_session_state()
        except Exception as exc:  # pylint: disable=broad-except
            QMessageBox.critical(self, "Save Failed", str(exc))
            self.status_label.setText("Save failed.")
            return

        msg = "Generated:\n{0}\n{1}".format(outputs["png"], outputs["pdf"])
        QMessageBox.information(self, "Success", msg)
        self.status_label.setText("Saved files successfully.")

    def print_sheet(self):
        try:
            temp_stem = os.path.join(tempfile.gettempdir(), "avery_5267_print_job")
            outputs = self._generate_outputs(temp_stem)
            printed = self._print_png(outputs["png"])
            if printed:
                self.status_label.setText("Print job sent.")
            else:
                self.status_label.setText("Print cancelled.")
            self._save_session_state()
        except Exception as exc:  # pylint: disable=broad-except
            QMessageBox.critical(self, "Print Failed", str(exc))
            self.status_label.setText("Print failed.")


def main():
    app = QApplication(sys.argv)
    win = Avery5267Window()
    win.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
