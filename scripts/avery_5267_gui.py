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
import re
import sys
import tempfile
from datetime import date

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

try:
    from pylibdmtx.pylibdmtx import encode as dmtx_encode
except Exception:  # pylint: disable=broad-except
    dmtx_encode = None

# Ensure importing sibling script works when launching from repo root.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from generate_avery_5267_labels import (  # noqa: E402
    ARUCO_LEFT_OFFSET_IN,
    ARUCO_MARKER_SIZE_IN,
    ARUCO_RIGHT_GAP_IN,
    COLS,
    DEFAULT_ARUCO_DICT,
    DEFAULT_DPI,
    FONT_POINT_SIZE,
    H_PITCH_IN,
    LABEL_HEIGHT_IN,
    LABEL_WIDTH_IN,
    LEFT_MARGIN_IN,
    MARKER_TYPE_ARUCO,
    MARKER_TYPE_DATAMATRIX,
    MARKER_TYPE_NONE,
    MAX_TEXT_LINES,
    MAX_LABELS,
    PAGE_HEIGHT_IN,
    PAGE_WIDTH_IN,
    ROWS,
    TEXT_VERTICAL_PADDING_IN,
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

MONTH_ABBR_UPPER = {
    1: "JAN",
    2: "FEB",
    3: "MAR",
    4: "APR",
    5: "MAY",
    6: "JUN",
    7: "JUL",
    8: "AUG",
    9: "SEP",
    10: "OCT",
    11: "NOV",
    12: "DEC",
}
TOKEN_PATTERN = re.compile(r"\{(date|name)\}", flags=re.IGNORECASE)
MARKER_TYPE_ITEMS = (
    ("ArUco", MARKER_TYPE_ARUCO),
    ("DataMatrix", MARKER_TYPE_DATAMATRIX),
)
MARKER_TYPE_LABEL_BY_VALUE = {value: label for label, value in MARKER_TYPE_ITEMS}
MARKER_TYPE_VALUE_BY_LABEL = {
    label.strip().lower(): value for label, value in MARKER_TYPE_ITEMS
}
MARKER_TYPE_VALUE_BY_LABEL["none"] = MARKER_TYPE_NONE
MARKER_TYPE_LABEL_NONE = "None"


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


def _safe_float(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _normalize_marker_type(value):
    if value is None:
        return MARKER_TYPE_NONE
    key = str(value).strip().lower()
    if key in (MARKER_TYPE_ARUCO, MARKER_TYPE_DATAMATRIX, MARKER_TYPE_NONE):
        return key
    return MARKER_TYPE_VALUE_BY_LABEL.get(key, MARKER_TYPE_NONE)


def _marker_label(marker_type):
    normalized = _normalize_marker_type(marker_type)
    if normalized == MARKER_TYPE_NONE:
        return MARKER_TYPE_LABEL_NONE
    return MARKER_TYPE_LABEL_BY_VALUE.get(normalized, "ArUco")


def _format_today_line5():
    today = date.today()
    month_code = MONTH_ABBR_UPPER.get(today.month, "{0:02d}".format(today.month))
    return "{0:02d}.{1}.{2:04d}".format(today.day, month_code, today.year)


class SheetPreviewWidget(QWidget):
    wheel_adjust_requested = pyqtSignal(int, bool, bool)

    def __init__(self, parent=None):
        super(SheetPreviewWidget, self).__init__(parent)
        self.setMinimumSize(520, 700)
        self.missing = 0
        self.row_specs = []
        self._aruco_cache = {}
        self.zoom_factor = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._is_panning = False
        self._last_pan_pos = None

    def mousePressEvent(self, event):  # pylint: disable=invalid-name
        if event.button() == Qt.MouseButton.MiddleButton:
            self._is_panning = True
            self._last_pan_pos = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super(SheetPreviewWidget, self).mousePressEvent(event)

    def mouseMoveEvent(self, event):  # pylint: disable=invalid-name
        if self._is_panning and self._last_pan_pos is not None:
            pos = event.position()
            delta = pos - self._last_pan_pos
            self.pan_x += float(delta.x())
            self.pan_y += float(delta.y())
            self._last_pan_pos = pos
            self.update()
            event.accept()
            return
        super(SheetPreviewWidget, self).mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # pylint: disable=invalid-name
        if event.button() == Qt.MouseButton.MiddleButton and self._is_panning:
            self._is_panning = False
            self._last_pan_pos = None
            self.unsetCursor()
            event.accept()
            return
        super(SheetPreviewWidget, self).mouseReleaseEvent(event)

    def wheelEvent(self, event):  # pylint: disable=invalid-name
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return

        steps = int(delta / 120)
        if steps == 0:
            steps = 1 if delta > 0 else -1

        modifiers = event.modifiers()
        ctrl_held = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift_held = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        self.wheel_adjust_requested.emit(steps, ctrl_held, shift_held)
        event.accept()

    def set_preview_data(self, missing, row_specs):
        self.missing = int(missing)
        self.row_specs = row_specs[:]
        self.update()

    def set_zoom_factor(self, value):
        zoom = max(0.35, min(4.0, float(value)))
        if abs(zoom - self.zoom_factor) < 1e-6:
            return
        self.zoom_factor = zoom
        self.update()

    def zoom_by_steps(self, steps):
        if steps == 0:
            return
        self.set_zoom_factor(self.zoom_factor * (1.1 ** int(steps)))

    def reset_zoom(self):
        self.set_zoom_factor(1.0)
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.update()

    def _fit_page_rect(self):
        margin = 18
        area = self.rect().adjusted(margin, margin, -margin, -margin)
        page_ratio = PAGE_WIDTH_IN / PAGE_HEIGHT_IN
        area_ratio = area.width() / float(max(1, area.height()))

        if area_ratio > page_ratio:
            base_h = area.height()
            base_w = int(round(base_h * page_ratio))
        else:
            base_w = area.width()
            base_h = int(round(base_w / page_ratio))

        page_w = int(round(base_w * self.zoom_factor))
        page_h = int(round(base_h * self.zoom_factor))

        max_pan_x = max(0.0, (page_w - area.width()) / 2.0)
        max_pan_y = max(0.0, (page_h - area.height()) / 2.0)
        self.pan_x = max(-max_pan_x, min(max_pan_x, self.pan_x))
        self.pan_y = max(-max_pan_y, min(max_pan_y, self.pan_y))

        x = area.x() + (area.width() - page_w) / 2.0 + self.pan_x
        y = area.y() + (area.height() - page_h) / 2.0 + self.pan_y
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
        key = ("aruco", str(dict_name), int(marker_id), int(side_px))
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

    def _get_datamatrix_qimage(self, side_px, marker_id):
        if side_px < 8:
            return None

        marker_id = _safe_int(marker_id, 0)
        key = ("datamatrix", int(marker_id), int(side_px))
        cached = self._aruco_cache.get(key)
        if cached is not None:
            return cached

        if dmtx_encode is None:
            return None

        try:
            encoded = dmtx_encode(str(marker_id).encode("utf-8"))
            qimg = QImage(
                encoded.pixels,
                int(encoded.width),
                int(encoded.height),
                int(encoded.width) * 3,
                QImage.Format.Format_RGB888,
            ).copy()
            qimg = qimg.convertToFormat(QImage.Format.Format_Grayscale8)
            qimg = qimg.scaled(
                int(side_px),
                int(side_px),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
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
        px_per_in = page_rect.width() / float(PAGE_WIDTH_IN)

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

            horizontal_pad = max(1.0, 0.02 * px_per_in)
            text_vertical_pad = TEXT_VERTICAL_PADDING_IN * px_per_in
            text_x = r.x() + horizontal_pad
            text_right = r.right() - horizontal_pad
            text_w = max(4.0, text_right - text_x)
            text_y0 = r.y() + text_vertical_pad
            text_y1 = (r.y() + r.height()) - text_vertical_pad
            marker_type = _normalize_marker_type(spec.get("marker_type", MARKER_TYPE_NONE))
            marker_id = _safe_int(spec.get("marker_id", 0), 0)

            if marker_type != MARKER_TYPE_NONE:
                marker_size = ARUCO_MARKER_SIZE_IN * px_per_in
                marker_size = min(marker_size, r.height())
                marker_x = r.x() + (ARUCO_LEFT_OFFSET_IN * px_per_in)
                marker_y = r.y() + (r.height() - marker_size) / 2.0
                marker = QRectF(marker_x, marker_y, marker_size, marker_size)
                marker_side_px = max(16, int(round(marker_size)))
                if marker_type == MARKER_TYPE_DATAMATRIX:
                    marker_img = self._get_datamatrix_qimage(marker_side_px, marker_id)
                else:
                    marker_img = self._get_aruco_qimage(
                        marker_side_px,
                        marker_id,
                        spec.get("marker_dict", spec.get("aruco_dict", DEFAULT_ARUCO_DICT)),
                    )
                if marker_img is not None:
                    painter.drawImage(marker, marker_img)
                    painter.setPen(QPen(QColor(28, 38, 46), 1))
                    painter.drawRect(marker)
                else:
                    painter.fillRect(marker, QColor(18, 18, 18))
                    painter.setPen(QPen(QColor(255, 255, 255), 1))
                    painter.drawRect(marker)
                marker_text_gap = ARUCO_RIGHT_GAP_IN * px_per_in
                text_x = marker.x() + marker.width() + marker_text_gap
                text_w = max(4.0, text_right - text_x)

            raw_lines = spec.get("lines", [])
            if isinstance(raw_lines, str):
                raw_lines = [raw_lines]
            lines = [str(line) for line in raw_lines[:MAX_TEXT_LINES]]
            while len(lines) < MAX_TEXT_LINES:
                lines.append("")

            font_px = max(1, int(round((FONT_POINT_SIZE / 72.0) * px_per_in)))
            font = QFont("Arial")
            font.setBold(False)
            font.setPixelSize(font_px)
            painter.setFont(font)
            painter.setPen(QPen(QColor(14, 34, 48), 1))
            text_box_h = max(1.0, text_y1 - text_y0)
            line_h = text_box_h / float(MAX_TEXT_LINES)
            for i, line in enumerate(lines[:MAX_TEXT_LINES]):
                if not line.strip():
                    continue
                line_rect = QRectF(
                    text_x,
                    text_y0 + i * line_h,
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
    COL_USE_MARKER = 1
    COL_MARKER_ID = 2
    COL_LINE1 = 3
    COL_LINE2 = 4
    COL_LINE3 = 5
    COL_LINE4 = 6
    COL_LINE5 = 7
    # Backward-compatible aliases used in older code paths/saved sessions.
    COL_USE_ARUCO = COL_USE_MARKER
    COL_ARUCO_ID = COL_MARKER_ID
    LINE_COLS = (COL_LINE1, COL_LINE2, COL_LINE3, COL_LINE4, COL_LINE5)
    SETTINGS_FILENAME = ".avery_5267_gui.ini"

    def __init__(self):
        super(Avery5267Window, self).__init__()
        self._table_syncing = False
        self._copied_row_payload = None
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

        self.token_hint_label = QLabel("Autofill tokens: {date}, {name}")
        form.addRow("", self.token_hint_label)

        name_row = QWidget()
        name_row_layout = QHBoxLayout(name_row)
        name_row_layout.setContentsMargins(0, 0, 0, 0)
        self.name_token_checkbox = QCheckBox("Enable {name}")
        self.name_token_checkbox.setChecked(True)
        self.researcher_name_edit = QLineEdit("")
        self.researcher_name_edit.setPlaceholderText("Researcher name")
        self.researcher_name_edit.setEnabled(self.name_token_checkbox.isChecked())
        name_row_layout.addWidget(self.name_token_checkbox)
        name_row_layout.addWidget(self.researcher_name_edit, 1)
        form.addRow("", name_row)

        self.default_aruco_checkbox = QCheckBox("Use Marker by default")
        self.default_aruco_checkbox.setChecked(False)
        form.addRow("", self.default_aruco_checkbox)

        self.marker_type_combo = QComboBox()
        for label, value in MARKER_TYPE_ITEMS:
            self.marker_type_combo.addItem(label, value)
        form.addRow("Marker Type", self.marker_type_combo)

        self.aruco_start_spin = QSpinBox()
        self.aruco_start_spin.setRange(0, 999999)
        self.aruco_start_spin.setValue(0)
        form.addRow("Marker Start ID", self.aruco_start_spin)

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
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(
            ["Slot", "Use Marker", "ID", "Line 1", "Line 2", "Line 3", "Line 4", "Line 5"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_SLOT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(
            self.COL_USE_ARUCO, QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(
            self.COL_ARUCO_ID, QHeaderView.ResizeMode.ResizeToContents
        )
        for col in self.LINE_COLS:
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
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
        self.marker_type_combo.currentIndexChanged.connect(self._on_defaults_changed)
        self.aruco_start_spin.valueChanged.connect(self._on_defaults_changed)
        self.name_token_checkbox.toggled.connect(self._on_name_token_changed)
        self.researcher_name_edit.textChanged.connect(self._on_name_token_changed)
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

        edit_menu = self.menuBar().addMenu("&Edit")

        copy_row_action = QAction("Copy Selected Row", self)
        copy_row_action.setShortcut("Ctrl+C")
        copy_row_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy_row_action.triggered.connect(self.copy_selected_row)
        self.table.addAction(copy_row_action)
        edit_menu.addAction(copy_row_action)

        paste_row_action = QAction("Paste To Selected Row", self)
        paste_row_action.setShortcut("Ctrl+V")
        paste_row_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        paste_row_action.triggered.connect(self.paste_selected_row)
        self.table.addAction(paste_row_action)
        edit_menu.addAction(paste_row_action)

        view_menu = self.menuBar().addMenu("&View")

        zoom_in_action = QAction("Zoom In", self)
        zoom_in_action.setShortcut("Ctrl+=")
        zoom_in_action.triggered.connect(lambda checked=False: self._zoom_preview(1))
        view_menu.addAction(zoom_in_action)

        zoom_out_action = QAction("Zoom Out", self)
        zoom_out_action.setShortcut("Ctrl+-")
        zoom_out_action.triggered.connect(lambda checked=False: self._zoom_preview(-1))
        view_menu.addAction(zoom_out_action)

        zoom_reset_action = QAction("Reset Zoom", self)
        zoom_reset_action.setShortcut("Ctrl+0")
        zoom_reset_action.triggered.connect(lambda checked=False: self._zoom_preview(0))
        view_menu.addAction(zoom_reset_action)

    def _zoom_preview(self, steps):
        if int(steps) == 0:
            self.preview.reset_zoom()
        else:
            self.preview.zoom_by_steps(int(steps))
        percent = int(round(self.preview.zoom_factor * 100.0))
        self.status_label.setText("Preview zoom: {0}%".format(percent))

    def _on_preview_wheel_adjust(self, steps, ctrl_held, shift_held):
        step_value = int(steps)
        if step_value == 0:
            return
        if ctrl_held:
            self._zoom_preview(step_value)
            return
        if shift_held:
            self.count_spin.setValue(self.count_spin.value() + step_value)
            return
        self.missing_spin.setValue(self.missing_spin.value() + step_value)

    def _selected_table_rows(self):
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return []
        rows = sorted({index.row() for index in selection_model.selectedRows()})
        if rows:
            return rows
        current_row = self.table.currentRow()
        if current_row >= 0:
            return [current_row]
        return []

    def _get_row_payload(self, row):
        return {
            "use_marker": self._is_row_aruco_enabled(row),
            "id_text": self._get_item_text(row, self.COL_ARUCO_ID),
            "lines": [self._get_item_text(row, col) for col in self.LINE_COLS],
        }

    def _apply_row_payload(self, row, payload):
        use_aruco = self.table.item(row, self.COL_USE_ARUCO)
        if use_aruco is not None:
            use_marker = _safe_bool(payload.get("use_marker", payload.get("has_aruco", False)))
            use_aruco.setCheckState(
                Qt.CheckState.Checked
                if use_marker
                else Qt.CheckState.Unchecked
            )

        aruco_item = self.table.item(row, self.COL_ARUCO_ID)
        if aruco_item is not None:
            aruco_item.setText(str(payload.get("id_text", payload.get("aruco_id_text", ""))))

        lines = payload.get("lines", [])
        if isinstance(lines, str):
            lines = [lines]
        for i, col in enumerate(self.LINE_COLS):
            item = self.table.item(row, col)
            if item is not None:
                item.setText(str(lines[i]) if i < len(lines) else "")

    def copy_selected_row(self):
        self._sync_table_rows()
        rows = self._selected_table_rows()
        if not rows:
            self.status_label.setText("Select a row to copy.")
            return

        source_row = rows[0]
        self._copied_row_payload = self._get_row_payload(source_row)
        self.status_label.setText(
            "Copied row {0}. Select another row and press Ctrl+V.".format(source_row + 1)
        )

    def paste_selected_row(self):
        if not isinstance(self._copied_row_payload, dict):
            self.status_label.setText("Copy a row first (Ctrl+C).")
            return

        self._sync_table_rows()
        rows = self._selected_table_rows()
        if not rows:
            self.status_label.setText("Select a destination row to paste.")
            return

        self._table_syncing = True
        try:
            for row in rows:
                self._apply_row_payload(row, self._copied_row_payload)
        finally:
            self._table_syncing = False

        self.refresh_preview()
        self.status_label.setText("Pasted to row(s): {0}".format(", ".join(str(r + 1) for r in rows)))

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
            "name_token_enabled": self.name_token_checkbox.isChecked(),
            "researcher_name": self.researcher_name_edit.text(),
            "default_aruco": self.default_aruco_checkbox.isChecked(),
            "marker_type": _normalize_marker_type(self.marker_type_combo.currentData()),
            "aruco_start_id": self.aruco_start_spin.value(),
            "aruco_dict": self.aruco_dict_combo.currentText(),
            "rows": self._collect_row_specs(),
            "splitter_sizes": self.splitter.sizes(),
            "preview_zoom": self.preview.zoom_factor,
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
        name_token_enabled = _safe_bool(
            payload.get("name_token_enabled", self.name_token_checkbox.isChecked())
        )
        researcher_name = str(
            payload.get("researcher_name", self.researcher_name_edit.text())
        ).strip()
        default_aruco = _safe_bool(
            payload.get("default_aruco", self.default_aruco_checkbox.isChecked())
        )
        marker_type = _normalize_marker_type(
            payload.get("marker_type", self.marker_type_combo.currentData())
        )
        if "marker_type" not in payload and isinstance(rows_payload, list):
            for row_spec in rows_payload:
                if not isinstance(row_spec, dict):
                    continue
                inferred = _normalize_marker_type(
                    row_spec.get("marker_type", MARKER_TYPE_NONE)
                )
                if inferred != MARKER_TYPE_NONE:
                    marker_type = inferred
                    break
        aruco_start = _safe_int(
            payload.get("aruco_start_id", self.aruco_start_spin.value()),
            self.aruco_start_spin.value(),
        )
        aruco_start = max(0, min(999999, aruco_start))
        aruco_dict = str(payload.get("aruco_dict", self.aruco_dict_combo.currentText())).strip()
        preview_zoom = payload.get("preview_zoom", self.preview.zoom_factor)

        widgets_to_block = [
            self.missing_spin,
            self.count_spin,
            self.dpi_spin,
            self.output_edit,
            self.name_token_checkbox,
            self.researcher_name_edit,
            self.default_aruco_checkbox,
            self.marker_type_combo,
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
            self.name_token_checkbox.setChecked(name_token_enabled)
            self.researcher_name_edit.setText(researcher_name)
            self.default_aruco_checkbox.setChecked(default_aruco)
            marker_idx = self.marker_type_combo.findData(marker_type)
            if marker_idx >= 0:
                self.marker_type_combo.setCurrentIndex(marker_idx)
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
                    marker_type = _normalize_marker_type(spec.get("marker_type", MARKER_TYPE_NONE))
                    if marker_type == MARKER_TYPE_NONE and _safe_bool(spec.get("has_aruco", False)):
                        marker_type = MARKER_TYPE_ARUCO

                    use_aruco_item = self.table.item(row, self.COL_USE_ARUCO)
                    if use_aruco_item is not None:
                        use_aruco_item.setCheckState(
                            Qt.CheckState.Checked
                            if marker_type != MARKER_TYPE_NONE
                            else Qt.CheckState.Unchecked
                        )

                    aruco_item = self.table.item(row, self.COL_ARUCO_ID)
                    if aruco_item is not None:
                        marker_id_value = spec.get("marker_id", spec.get("aruco_id", row))
                        aruco_item.setText(str(_safe_int(marker_id_value, row)))

                    for i, col in enumerate(self.LINE_COLS):
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

        self.preview.set_zoom_factor(_safe_float(preview_zoom, self.preview.zoom_factor))
        self.refresh_preview()

    def _save_session_state(self):
        payload = self._capture_session_payload()
        self.settings.setValue("window_geometry", self.saveGeometry())
        self.settings.setValue("splitter_sizes", payload.get("splitter_sizes", []))
        self.settings.setValue("missing", payload.get("missing", 0))
        self.settings.setValue("count", payload.get("count", 1))
        self.settings.setValue("dpi", payload.get("dpi", DEFAULT_DPI))
        self.settings.setValue("output_stem", payload.get("output_stem", ""))
        self.settings.setValue("name_token_enabled", payload.get("name_token_enabled", False))
        self.settings.setValue("researcher_name", payload.get("researcher_name", ""))
        self.settings.setValue("default_aruco", payload.get("default_aruco", False))
        self.settings.setValue("marker_type", payload.get("marker_type", MARKER_TYPE_ARUCO))
        self.settings.setValue("aruco_start_id", payload.get("aruco_start_id", 0))
        self.settings.setValue("aruco_dict", payload.get("aruco_dict", DEFAULT_ARUCO_DICT))
        self.settings.setValue("preview_zoom", payload.get("preview_zoom", 1.0))
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
                "name_token_enabled": self.settings.value("name_token_enabled", False),
                "researcher_name": self.settings.value("researcher_name", ""),
                "default_aruco": self.settings.value(
                    "default_aruco", self.default_aruco_checkbox.isChecked()
                ),
                "marker_type": self.settings.value(
                    "marker_type", self.marker_type_combo.currentData()
                ),
                "aruco_start_id": self.settings.value(
                    "aruco_start_id", self.aruco_start_spin.value()
                ),
                "aruco_dict": self.settings.value(
                    "aruco_dict", self.aruco_dict_combo.currentText()
                ),
                "preview_zoom": self.settings.value("preview_zoom", 1.0),
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

    def _is_row_marker_enabled(self, row):
        return self._is_row_aruco_enabled(row)

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

    def _on_name_token_changed(self, _value):  # pylint: disable=unused-argument
        self.researcher_name_edit.setEnabled(self.name_token_checkbox.isChecked())
        self.refresh_preview()
        self.status_label.setText("Token settings updated.")

    def _on_defaults_changed(self, value):  # pylint: disable=unused-argument
        self.refresh_preview()
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

                for col in self.LINE_COLS:
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

    def _resolve_line_tokens(self, text):
        raw_text = str(text) if text is not None else ""
        name_value = ""
        if self.name_token_checkbox.isChecked():
            name_value = self.researcher_name_edit.text().strip()

        values = {
            "date": _format_today_line5(),
            "name": name_value,
        }

        def _replace(match):
            token_name = match.group(1).lower()
            return values.get(token_name, match.group(0))

        return TOKEN_PATTERN.sub(_replace, raw_text)

    def _collect_row_specs(self, resolve_tokens=False):
        specs = []
        selected_marker_type = _normalize_marker_type(self.marker_type_combo.currentData())
        for row in range(self.table.rowCount()):
            lines = [self._get_item_text(row, col) for col in self.LINE_COLS]
            if resolve_tokens:
                lines = [self._resolve_line_tokens(line) for line in lines]

            aruco_default = self.aruco_start_spin.value() + row
            aruco_text = self._get_item_text(row, self.COL_ARUCO_ID)
            aruco_id = _safe_int(aruco_text, aruco_default)
            has_marker = self._is_row_marker_enabled(row)
            marker_type = selected_marker_type if has_marker else MARKER_TYPE_NONE

            specs.append(
                {
                    "lines": lines,
                    "marker_type": marker_type,
                    "marker_id": aruco_id,
                    "marker_dict": self.aruco_dict_combo.currentText(),
                    # Backward compatibility for older payload consumers.
                    "has_aruco": bool(has_marker and marker_type == MARKER_TYPE_ARUCO),
                    "aruco_id": aruco_id,
                    "aruco_dict": self.aruco_dict_combo.currentText(),
                }
            )
        return specs

    def refresh_preview(self):
        self._sync_table_rows()
        specs = self._collect_row_specs(resolve_tokens=True)
        self.preview.set_preview_data(self.missing_spin.value(), specs)
        self.status_label.setText("Preview updated.")

    def _generate_outputs(self, output_stem):
        self._sync_table_rows()
        if not output_stem:
            raise ValueError("Please set an output file stem.")

        output_dir = os.path.dirname(output_stem)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        row_specs = self._collect_row_specs(resolve_tokens=True)

        def label_spec_fn(seq_index, slot_index):  # pylint: disable=unused-argument
            if seq_index < len(row_specs):
                return row_specs[seq_index]
            return {
                "lines": [],
                "marker_type": MARKER_TYPE_NONE,
                "marker_id": 0,
                "marker_dict": self.aruco_dict_combo.currentText(),
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
