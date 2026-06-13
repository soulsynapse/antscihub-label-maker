#!/usr/bin/env python
"""
Generate print-ready Avery 5267 label sheets at high resolution.

This script can be used in two ways:
1) As a library function (`generate_avery_5267_sheet`) that takes a callback.
2) As a CLI utility that repeats one label design across the available slots.
"""

from __future__ import absolute_import, division, print_function

import argparse
import os
import sys


# Avery 5267 / 5167 geometry on US Letter.
# Dimensions are in inches.
PAGE_WIDTH_IN = 8.5
PAGE_HEIGHT_IN = 11.0
LABEL_WIDTH_IN = 1.75
LABEL_HEIGHT_IN = 0.5
COLS = 4
ROWS = 20
LEFT_MARGIN_IN = 0.281
TOP_MARGIN_IN = 0.5
H_PITCH_IN = 2.063
V_PITCH_IN = 0.5

MAX_LABELS = COLS * ROWS
DEFAULT_DPI = 1200
DEFAULT_ARUCO_DICT = "DICT_6X6_250"
FONT_POINT_SIZE = 4.0
MAX_TEXT_LINES = 5
MARKER_TYPE_NONE = "none"
MARKER_TYPE_ARUCO = "aruco"
MARKER_TYPE_DATAMATRIX = "datamatrix"
MARKER_TYPE_QR = "qr"
MARKER_TYPES = (
    MARKER_TYPE_NONE,
    MARKER_TYPE_ARUCO,
    MARKER_TYPE_DATAMATRIX,
    MARKER_TYPE_QR,
)
MM_PER_IN = 25.4
ARUCO_MARKER_SIZE_MM = 8.0
ARUCO_LEFT_OFFSET_MM = 2.0
ARUCO_RIGHT_GAP_MM = 2.0
TEXT_VERTICAL_PADDING_MM = 2.0
SIDE_LINE_WIDTH_MM = 2.0
SIDE_LINE_HEIGHT_MM = 8.0
SIDE_LINE_RIGHT_OFFSET_MM = 2.0
SIDE_LINE_TEXT_GAP_MM = 1.0
ARUCO_MARKER_SIZE_IN = ARUCO_MARKER_SIZE_MM / MM_PER_IN
ARUCO_LEFT_OFFSET_IN = ARUCO_LEFT_OFFSET_MM / MM_PER_IN
ARUCO_RIGHT_GAP_IN = ARUCO_RIGHT_GAP_MM / MM_PER_IN
TEXT_VERTICAL_PADDING_IN = TEXT_VERTICAL_PADDING_MM / MM_PER_IN
SIDE_LINE_WIDTH_IN = SIDE_LINE_WIDTH_MM / MM_PER_IN
SIDE_LINE_HEIGHT_IN = SIDE_LINE_HEIGHT_MM / MM_PER_IN
SIDE_LINE_RIGHT_OFFSET_IN = SIDE_LINE_RIGHT_OFFSET_MM / MM_PER_IN
SIDE_LINE_TEXT_GAP_IN = SIDE_LINE_TEXT_GAP_MM / MM_PER_IN


def _require_pillow():
    try:
        from PIL import Image, ImageDraw, ImageFont  # pylint: disable=import-error
    except ImportError:
        raise RuntimeError(
            "Pillow is required. Install it with: pip install pillow"
        )
    return Image, ImageDraw, ImageFont


def _maybe_import_cv2():
    try:
        import cv2  # pylint: disable=import-error
    except ImportError:
        return None
    return cv2


def _maybe_import_datamatrix_encode():
    try:
        from pylibdmtx.pylibdmtx import encode as dmtx_encode  # pylint: disable=import-error
    except Exception:  # pylint: disable=broad-except
        return None
    return dmtx_encode


def _maybe_import_qrcode():
    try:
        import qrcode  # pylint: disable=import-error
    except Exception:  # pylint: disable=broad-except
        return None
    return qrcode


def _to_px(inches, dpi):
    return int(round(inches * dpi))


def _load_font(ImageFont, size_px):
    # Prefer regular Arial for main label text, then fall back to similar sans-serif fonts.
    font_candidates = [
        "arial.ttf",
        "Arial.ttf",
        "ARIAL.TTF",
        "arialn.ttf",
        "ARIALN.TTF",
        "Arial Narrow.ttf",
        "segoeui.ttf",
        "DejaVuSans.ttf",
        "LiberationSans-Regular.ttf",
    ]
    for font_name in font_candidates:
        try:
            return ImageFont.truetype(font_name, size_px)
        except OSError:
            continue
    return ImageFont.load_default()


def _load_styled_font(ImageFont, size_px, bold=False, italic=False):
    if bold and italic:
        font_candidates = [
            "arialbi.ttf",
            "ARIALBI.TTF",
            "Arial Bold Italic.ttf",
            "segoeuiz.ttf",
            "DejaVuSans-BoldOblique.ttf",
            "LiberationSans-BoldItalic.ttf",
        ]
    elif bold:
        font_candidates = [
            "arialbd.ttf",
            "ARIALBD.TTF",
            "Arial Bold.ttf",
            "segoeuib.ttf",
            "DejaVuSans-Bold.ttf",
            "LiberationSans-Bold.ttf",
        ]
    elif italic:
        font_candidates = [
            "ariali.ttf",
            "ARIALI.TTF",
            "Arial Italic.ttf",
            "segoeuii.ttf",
            "DejaVuSans-Oblique.ttf",
            "LiberationSans-Italic.ttf",
        ]
    else:
        return _load_font(ImageFont, size_px)

    for font_name in font_candidates:
        try:
            return ImageFont.truetype(font_name, size_px)
        except OSError:
            continue
    return _load_font(ImageFont, size_px)


def _get_text_size(font, text):
    # Works across older/newer Pillow versions.
    if hasattr(font, "getbbox"):
        left, top, right, bottom = font.getbbox(text)
        return right - left, bottom - top
    return font.getsize(text)


def _get_text_bbox(font, text):
    if hasattr(font, "getbbox"):
        return font.getbbox(text)
    w, h = font.getsize(text)
    return (0, 0, w, h)


def _fit_font(ImageFont, lines, box_w, box_h):
    if not lines:
        return _load_font(ImageFont, 28)

    max_lines = max(1, len(lines))
    start_size = min(int(box_h * 0.8 / max_lines), 260)
    min_size = 22
    step = 2

    for size in range(start_size, min_size - 1, -step):
        font = _load_font(ImageFont, size)
        line_sizes = [_get_text_size(font, line) for line in lines]
        line_heights = [h for _, h in line_sizes]
        max_width = max(w for w, _ in line_sizes) if line_sizes else 0
        total_height = sum(line_heights)
        if max_width <= box_w and total_height <= box_h:
            return font
    return _load_font(ImageFont, min_size)


def _normalize_marker_type(value):
    marker_type = str(value).strip().lower() if value is not None else ""
    if marker_type in MARKER_TYPES:
        return marker_type
    return MARKER_TYPE_NONE


def _marker_value_text(value, fallback=""):
    if value is None:
        return str(fallback)
    text = str(value).strip()
    if text == "":
        return str(fallback)
    return text


def _numeric_marker_id_or_zero(value):
    text = _marker_value_text(value, "")
    if text.isdigit():
        return int(text)
    return 0


def _normalize_marker_value(value, marker_type):
    if marker_type == MARKER_TYPE_ARUCO:
        return _numeric_marker_id_or_zero(value)
    return _marker_value_text(value, "")


def _normalize_label_spec(label_spec):
    if label_spec is None:
        return {
            "lines": [],
            "marker_type": MARKER_TYPE_NONE,
            "marker_id": 0,
            "marker_dict": DEFAULT_ARUCO_DICT,
            "side_line": "",
        }

    lines = label_spec.get("lines", [])
    if isinstance(lines, str):
        lines = [lines]
    lines = [str(line) if line is not None else "" for line in lines[:MAX_TEXT_LINES]]
    while len(lines) < MAX_TEXT_LINES:
        lines.append("")

    # Backward compatibility with older specs that used has_aruco/aruco_id/aruco_dict.
    has_aruco_legacy = bool(label_spec.get("has_aruco", False))
    marker_type = _normalize_marker_type(label_spec.get("marker_type", None))
    if marker_type == MARKER_TYPE_NONE and has_aruco_legacy:
        marker_type = MARKER_TYPE_ARUCO

    marker_payload_value = label_spec.get(
        "marker_payload",
        label_spec.get(
            "id_text",
            label_spec.get("marker_id", label_spec.get("aruco_id", 0)),
        ),
    )
    marker_payload = _marker_value_text(marker_payload_value, "0")
    marker_id = _normalize_marker_value(label_spec.get("marker_id", marker_payload), marker_type)
    marker_dict = str(
        label_spec.get("marker_dict", label_spec.get("aruco_dict", DEFAULT_ARUCO_DICT))
    )
    side_line = str(label_spec.get("side_line", ""))

    return {
        "lines": lines,
        "marker_type": marker_type,
        "marker_id": marker_id,
        "marker_payload": marker_payload,
        "marker_dict": marker_dict,
        "side_line": side_line,
    }


def _get_aruco_marker(side_px, marker_id, dict_name):
    cv2 = _maybe_import_cv2()
    if cv2 is None:
        raise RuntimeError(
            "OpenCV (opencv-contrib-python) is required for ArUco markers. "
            "Install with: pip install opencv-contrib-python"
        )
    if not hasattr(cv2, "aruco"):
        raise RuntimeError(
            "Your OpenCV build does not include cv2.aruco. "
            "Install opencv-contrib-python."
        )
    if not hasattr(cv2.aruco, dict_name):
        raise ValueError(
            "Unknown ArUco dictionary '{0}'. Example: {1}".format(
                dict_name, DEFAULT_ARUCO_DICT
            )
        )

    marker_id = _numeric_marker_id_or_zero(marker_id)
    dictionary_id = getattr(cv2.aruco, dict_name)
    dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)

    # Keep marker IDs valid for the selected dictionary.
    dictionary_size = 0
    if hasattr(dictionary, "bytesList"):
        try:
            dictionary_size = int(dictionary.bytesList.shape[0])
        except Exception:  # pylint: disable=broad-except
            dictionary_size = 0
    if dictionary_size > 0:
        marker_id = int(marker_id) % dictionary_size

    if hasattr(cv2.aruco, "generateImageMarker"):
        marker = cv2.aruco.generateImageMarker(dictionary, marker_id, side_px)
    elif hasattr(cv2.aruco, "drawMarker"):
        marker = cv2.aruco.drawMarker(dictionary, marker_id, side_px)
    else:
        raise RuntimeError("This OpenCV version cannot generate ArUco marker images.")
    return marker


def _crop_to_dark_pixels(marker):
    gray = marker.convert("L")
    dark_mask = gray.point(lambda px: 255 if px < 245 else 0)
    bbox = dark_mask.getbbox()
    if bbox is None:
        return gray
    return gray.crop(bbox)


def _get_datamatrix_marker(Image, side_px, marker_value):
    dmtx_encode = _maybe_import_datamatrix_encode()
    if dmtx_encode is None:
        raise RuntimeError(
            "pylibdmtx is required for DataMatrix markers. "
            "Install with: pip install pylibdmtx"
        )

    payload = str(marker_value).encode("utf-8")
    encoded = dmtx_encode(payload)
    marker = Image.frombytes("RGB", (encoded.width, encoded.height), encoded.pixels)
    marker = _crop_to_dark_pixels(marker)
    marker = marker.resize((int(side_px), int(side_px)), resample=Image.Resampling.NEAREST)
    return marker


def _get_qr_marker(Image, side_px, marker_value):
    qrcode = _maybe_import_qrcode()
    if qrcode is None:
        raise RuntimeError(
            "qrcode is required for QR markers. "
            "Install with: pip install qrcode"
        )

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=0,
    )
    qr.add_data(str(marker_value))
    qr.make(fit=True)
    marker = qr.make_image(fill_color="black", back_color="white").convert("L")
    marker = marker.resize((int(side_px), int(side_px)), resample=Image.Resampling.NEAREST)
    return marker


def _draw_side_line(
    Image,
    ImageDraw,
    ImageFont,
    page,
    x0,
    y0,
    label_w,
    label_h,
    side_line_text,
    dpi,
    right_x=None,
):
    text = str(side_line_text).strip()
    if not text:
        return None

    box_w = _to_px(SIDE_LINE_WIDTH_IN, dpi)
    box_h = _to_px(SIDE_LINE_HEIGHT_IN, dpi)
    right_offset = _to_px(SIDE_LINE_RIGHT_OFFSET_IN, dpi)

    box_w = max(1, min(box_w, label_w))
    box_h = max(1, min(box_h, label_h))
    if right_x is None:
        side_x = x0 + label_w - right_offset - box_w
    else:
        side_x = int(round(right_x)) - box_w
    side_x = max(x0, min(x0 + label_w - box_w, side_x))
    side_y = y0 + int(round((label_h - box_h) / 2.0))

    side_box = Image.new("L", (box_w, box_h), color=255)

    # Render black text rotated so it reads along the long side of the box.
    text_canvas = Image.new("L", (box_h, box_w), color=255)
    text_draw = ImageDraw.Draw(text_canvas)
    font_px = max(1, int(round((FONT_POINT_SIZE / 72.0) * dpi)))
    chosen_font = _load_font(ImageFont, font_px)

    left, top, right, bottom = _get_text_bbox(chosen_font, text)
    tw = right - left
    th = bottom - top
    tx = int(round((box_h - tw) / 2.0 - left))
    ty = int(round((box_w - th) / 2.0 - top))
    text_draw.text((tx, ty), text, fill=0, font=chosen_font)

    # Keep output orientation aligned with GUI preview direction.
    rotated = text_canvas.rotate(90, expand=True)
    side_box.paste(rotated, (0, 0))
    page.paste(side_box, (side_x, side_y))

    return {
        "x": side_x,
        "y": side_y,
        "w": box_w,
        "h": box_h,
    }


def _draw_right_datamatrix(
    Image, page, x0, y0, label_w, label_h, marker_payload, dpi, marker_cache
):
    marker_side = _to_px(ARUCO_MARKER_SIZE_IN, dpi)
    marker_side = max(1, min(marker_side, label_h))
    marker_x = x0 + label_w - _to_px(ARUCO_LEFT_OFFSET_IN, dpi) - marker_side
    marker_x = max(x0, min(x0 + label_w - marker_side, marker_x))
    marker_y = y0 + int(round((label_h - marker_side) / 2.0))
    marker_pil = _get_cached_marker_pil(
        Image,
        marker_cache,
        MARKER_TYPE_DATAMATRIX,
        marker_side,
        marker_payload,
        DEFAULT_ARUCO_DICT,
    )
    page.paste(marker_pil, (marker_x, marker_y))
    return {
        "x": marker_x,
        "y": marker_y,
        "w": marker_side,
        "h": marker_side,
    }


def _get_cached_marker_pil(
    Image, marker_cache, marker_type, marker_side, marker_value, marker_dict
):
    key = (marker_type, int(marker_side), str(marker_value), str(marker_dict))
    cached = marker_cache.get(key)
    if cached is not None:
        return cached

    if marker_type == MARKER_TYPE_ARUCO:
        marker_img = _get_aruco_marker(marker_side, marker_value, marker_dict)
        marker_pil = Image.fromarray(marker_img).convert("L")
    elif marker_type == MARKER_TYPE_DATAMATRIX:
        marker_pil = _get_datamatrix_marker(Image, marker_side, marker_value)
    elif marker_type == MARKER_TYPE_QR:
        marker_pil = _get_qr_marker(Image, marker_side, marker_value)
    else:
        marker_pil = None

    if marker_pil is not None:
        marker_cache[key] = marker_pil
    return marker_pil


def _draw_label(
    draw,
    Image,
    ImageDraw,
    ImageFont,
    page,
    x0,
    y0,
    label_w,
    label_h,
    label_spec,
    dpi,
    marker_cache,
):
    horizontal_pad = max(8, int(round(0.02 * dpi)))  # ~0.02"
    marker_text_gap = _to_px(ARUCO_RIGHT_GAP_IN, dpi)
    text_vertical_pad = _to_px(TEXT_VERTICAL_PADDING_IN, dpi)

    lines = label_spec["lines"]
    marker_type = label_spec["marker_type"]
    marker_id = label_spec["marker_id"]
    marker_payload = label_spec.get("marker_payload", marker_id)
    marker_dict = label_spec["marker_dict"]
    side_line_text = label_spec.get("side_line", "")

    text_x0 = x0 + horizontal_pad
    text_y0 = y0 + text_vertical_pad
    text_y1 = y0 + label_h - text_vertical_pad
    text_x1 = x0 + label_w - horizontal_pad

    right_datamatrix = _draw_right_datamatrix(
        Image=Image,
        page=page,
        x0=x0,
        y0=y0,
        label_w=label_w,
        label_h=label_h,
        marker_payload=marker_payload,
        dpi=dpi,
        marker_cache=marker_cache,
    )

    side_box = _draw_side_line(
        Image=Image,
        ImageDraw=ImageDraw,
        ImageFont=ImageFont,
        page=page,
        x0=x0,
        y0=y0,
        label_w=label_w,
        label_h=label_h,
        side_line_text=side_line_text,
        dpi=dpi,
        right_x=right_datamatrix["x"],
    )
    if side_box is not None:
        text_x1 = min(text_x1, side_box["x"] - _to_px(SIDE_LINE_TEXT_GAP_IN, dpi))
    else:
        text_x1 = min(
            text_x1,
            right_datamatrix["x"] - _to_px(ARUCO_RIGHT_GAP_IN, dpi),
        )

    if marker_type != MARKER_TYPE_NONE:
        marker_side = _to_px(ARUCO_MARKER_SIZE_IN, dpi)
        marker_side = max(1, min(marker_side, label_h))
        marker_pil = _get_cached_marker_pil(
            Image,
            marker_cache,
            marker_type,
            marker_side,
            marker_id,
            marker_dict,
        )
        marker_x = x0 + _to_px(ARUCO_LEFT_OFFSET_IN, dpi)
        marker_y = y0 + int(round((label_h - marker_side) / 2.0))
        if marker_pil is not None:
            page.paste(marker_pil, (marker_x, marker_y))
            text_x0 = marker_x + marker_side + marker_text_gap

    text_box_w = max(1, text_x1 - text_x0)
    text_box_h = max(1, text_y1 - text_y0)
    if text_box_w <= 1 or text_box_h <= 1:
        return

    font_px = max(1, int(round((FONT_POINT_SIZE / 72.0) * dpi)))
    font = _load_font(ImageFont, font_px)
    text_layer = Image.new("L", (text_box_w, text_box_h), color=255)
    text_layer_draw = ImageDraw.Draw(text_layer)

    line_h = float(text_box_h) / float(MAX_TEXT_LINES)
    for i, line in enumerate(lines[:MAX_TEXT_LINES]):
        if not line.strip():
            continue
        left, top, right, bottom = _get_text_bbox(font, line)
        line_px_h = bottom - top
        current_y = (i * line_h) + max(0.0, (line_h - line_px_h) / 2.0) - top
        current_x = -left
        # Always render text left-justified to match the GUI preview.
        text_layer_draw.text(
            (int(round(current_x)), int(round(current_y))), line, fill=0, font=font
        )

    page.paste(text_layer, (text_x0, text_y0))


def generate_avery_5267_sheet(
    label_spec_fn,
    output_stem,
    missing=0,
    count=None,
    dpi=DEFAULT_DPI,
    save_pdf=True,
):
    """
    Generate one 8.5x11 Avery 5267 label sheet at high resolution.

    Args:
        label_spec_fn: callable(sequence_index, sheet_slot_index) -> dict
            Expected keys:
              - lines: list[str] or str
              - marker_type: "none" | "aruco" | "datamatrix" | "qr"
              - marker_id: int for ArUco, or string/int payload for DataMatrix/QR
              - marker_payload: string/int ID for the right-side DataMatrix (optional)
              - marker_dict: str (ArUco dictionary, e.g. DICT_6X6_250)
              - side_line: str (optional black text inside right-side white strip)
        output_stem: path without extension (or with extension; extension is removed)
        missing: number of labels already used on the sheet (0..79), filled row-major
        count: number of labels to print from remaining slots (default: all remaining)
        dpi: raster output DPI (default: 1200)
        save_pdf: also save a PDF next to the PNG (default: True)

    Returns:
        dict with generated file paths: {"png": "...", "pdf": "... or None"}
    """
    if missing < 0 or missing >= MAX_LABELS:
        raise ValueError("missing must be between 0 and {0}".format(MAX_LABELS - 1))

    remaining = MAX_LABELS - missing
    if count is None:
        count = remaining
    if count < 0:
        raise ValueError("count must be >= 0")
    count = min(count, remaining)

    Image, ImageDraw, ImageFont = _require_pillow()

    page_w = _to_px(PAGE_WIDTH_IN, dpi)
    page_h = _to_px(PAGE_HEIGHT_IN, dpi)
    label_w = _to_px(LABEL_WIDTH_IN, dpi)
    label_h = _to_px(LABEL_HEIGHT_IN, dpi)

    page = Image.new("L", (page_w, page_h), color=255)
    draw = ImageDraw.Draw(page)
    marker_cache = {}

    for seq_index in range(count):
        slot_index = missing + seq_index
        row = slot_index // COLS
        col = slot_index % COLS

        x0 = _to_px(LEFT_MARGIN_IN + (col * H_PITCH_IN), dpi)
        y0 = _to_px(TOP_MARGIN_IN + (row * V_PITCH_IN), dpi)

        label_spec = _normalize_label_spec(label_spec_fn(seq_index, slot_index))
        _draw_label(
            draw,
            Image,
            ImageDraw,
            ImageFont,
            page,
            x0,
            y0,
            label_w,
            label_h,
            label_spec,
            dpi,
            marker_cache,
        )

    output_root = os.path.splitext(output_stem)[0]
    png_path = output_root + ".png"
    pdf_path = output_root + ".pdf"

    page.save(png_path, dpi=(dpi, dpi))
    if save_pdf:
        # For PDF, keep page dimensions aligned to the same physical size at target DPI.
        page.save(pdf_path, "PDF", resolution=dpi)
    else:
        pdf_path = None

    return {"png": png_path, "pdf": pdf_path}


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Generate a print-ready Avery 5267 label sheet (1200 DPI default)."
    )
    parser.add_argument(
        "--output",
        default="avery_5267_labels",
        help="Output file stem (default: avery_5267_labels).",
    )
    parser.add_argument(
        "--missing",
        type=int,
        default=0,
        help="Number of already-used labels to skip from top-left, row-major.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="How many labels to print on this sheet. Default: all remaining.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=DEFAULT_DPI,
        help="Render DPI (default: 1200).",
    )
    parser.add_argument(
        "--marker-type",
        default=MARKER_TYPE_NONE,
        choices=MARKER_TYPES,
        help="Marker type: none, aruco, datamatrix, or qr (default: none).",
    )
    parser.add_argument(
        "--id",
        default="0",
        help="Marker ID/value or DataMatrix/QR payload (default: 0).",
    )
    parser.add_argument(
        "--has-aruco",
        action="store_true",
        help="Legacy alias for --marker-type aruco.",
    )
    parser.add_argument(
        "--aruco-id",
        type=int,
        default=0,
        help="Legacy alias for --id.",
    )
    parser.add_argument(
        "--aruco-dict",
        default=DEFAULT_ARUCO_DICT,
        help="ArUco dictionary name (default: DICT_6X6_250). Used for aruco markers.",
    )
    parser.add_argument(
        "--line",
        action="append",
        default=[],
        help="Label text line. Repeat this flag for multiple lines.",
    )
    parser.add_argument(
        "--side-line",
        default="",
        help="Optional side-strip text (black) for the right-side 8mm x 2mm box.",
    )
    return parser.parse_args(argv)


def _make_static_label_fn(lines, marker_type, marker_id, marker_dict, side_line=""):
    clean_lines = [line for line in lines if line.strip()]
    if not clean_lines:
        clean_lines = ["Label"]

    def _label_fn(seq_index, slot_index):  # pylint: disable=unused-argument
        return {
            "lines": clean_lines,
            "marker_type": marker_type,
            "marker_id": marker_id,
            "marker_dict": marker_dict,
            "side_line": side_line,
        }

    return _label_fn


def _normalize_hex_color(value, fallback="#000000"):
    text = str(value).strip() if value is not None else ""
    if not text:
        text = fallback
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        text = str(fallback).strip().lstrip("#")
    try:
        int(text, 16)
    except ValueError:
        text = str(fallback).strip().lstrip("#")
    return "#{0}".format(text.upper())


def _hex_to_rgb(value):
    text = _normalize_hex_color(value).lstrip("#")
    return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))


def _normalize_alignment(value):
    text = str(value).strip().lower() if value is not None else "center"
    if text in ("left", "center", "right"):
        return text
    return "center"


def _normalize_text_style(style):
    style = style if isinstance(style, dict) else {}
    font_size_pt = style.get("font_size_pt", 14)
    try:
        font_size_pt = float(font_size_pt)
    except (TypeError, ValueError):
        font_size_pt = 14.0
    font_size_pt = max(2.0, min(72.0, font_size_pt))
    return {
        "font_size_pt": font_size_pt,
        "text_color": _normalize_hex_color(style.get("text_color", "#000000")),
        "align": _normalize_alignment(style.get("align", "center")),
        "bold": bool(style.get("bold", False)),
        "italic": bool(style.get("italic", False)),
    }


def _draw_text_label(ImageDraw, ImageFont, page, x0, y0, label_w, label_h, lines, style, dpi):
    clean_lines = []
    if isinstance(lines, str):
        lines = [lines]
    for line in list(lines or [])[:MAX_TEXT_LINES]:
        text = str(line).strip()
        if text:
            clean_lines.append(text)
    if not clean_lines:
        return

    pad_x = max(2, int(round(0.035 * dpi)))
    pad_y = max(2, int(round(0.035 * dpi)))
    box_x = x0 + pad_x
    box_y = y0 + pad_y
    box_w = max(1, label_w - (2 * pad_x))
    box_h = max(1, label_h - (2 * pad_y))

    font_px = max(1, int(round((float(style["font_size_pt"]) / 72.0) * dpi)))
    font = _load_styled_font(
        ImageFont,
        font_px,
        bold=bool(style.get("bold", False)),
        italic=bool(style.get("italic", False)),
    )
    fill = _hex_to_rgb(style.get("text_color", "#000000"))
    draw = ImageDraw.Draw(page)
    line_bboxes = [_get_text_bbox(font, line) for line in clean_lines]
    line_heights = [bottom - top for _left, top, _right, bottom in line_bboxes]
    line_spacing = max(0, int(round(font_px * 0.18))) if len(clean_lines) > 1 else 0
    total_h = sum(line_heights) + (line_spacing * (len(clean_lines) - 1))
    current_y = box_y + max(0, int(round((box_h - total_h) / 2.0)))

    for line, bbox in zip(clean_lines, line_bboxes):
        left, top, right, bottom = bbox
        text_w = right - left
        text_h = bottom - top
        if style["align"] == "left":
            text_x = box_x - left
        elif style["align"] == "right":
            text_x = box_x + box_w - text_w - left
        else:
            text_x = box_x + int(round((box_w - text_w) / 2.0)) - left
        text_y = current_y - top
        draw.text((int(round(text_x)), int(round(text_y))), line, fill=fill, font=font)
        current_y += text_h + line_spacing


def generate_avery_5267_text_sheet(
    label_text_fn,
    output_stem,
    missing=0,
    count=None,
    dpi=DEFAULT_DPI,
    style=None,
    save_pdf=True,
):
    if missing < 0 or missing >= MAX_LABELS:
        raise ValueError("missing must be between 0 and {0}".format(MAX_LABELS - 1))

    remaining = MAX_LABELS - missing
    if count is None:
        count = remaining
    if count < 0:
        raise ValueError("count must be >= 0")
    count = min(count, remaining)

    Image, ImageDraw, ImageFont = _require_pillow()
    style = _normalize_text_style(style)

    page_w = _to_px(PAGE_WIDTH_IN, dpi)
    page_h = _to_px(PAGE_HEIGHT_IN, dpi)
    label_w = _to_px(LABEL_WIDTH_IN, dpi)
    label_h = _to_px(LABEL_HEIGHT_IN, dpi)

    page = Image.new("RGB", (page_w, page_h), color=(255, 255, 255))

    for seq_index in range(count):
        slot_index = missing + seq_index
        row = slot_index // COLS
        col = slot_index % COLS

        x0 = _to_px(LEFT_MARGIN_IN + (col * H_PITCH_IN), dpi)
        y0 = _to_px(TOP_MARGIN_IN + (row * V_PITCH_IN), dpi)

        lines = label_text_fn(seq_index, slot_index)
        _draw_text_label(ImageDraw, ImageFont, page, x0, y0, label_w, label_h, lines, style, dpi)

    output_root = os.path.splitext(output_stem)[0]
    png_path = output_root + ".png"
    pdf_path = output_root + ".pdf"
    page.save(png_path, dpi=(dpi, dpi))
    if save_pdf:
        page.save(pdf_path, "PDF", resolution=dpi)
    else:
        pdf_path = None
    return {"png": png_path, "pdf": pdf_path}


def main(argv=None):
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    marker_type = _normalize_marker_type(args.marker_type)
    marker_id = args.id
    if args.has_aruco:
        marker_type = MARKER_TYPE_ARUCO
        marker_id = int(args.aruco_id)

    label_fn = _make_static_label_fn(
        args.line, marker_type, marker_id, args.aruco_dict, args.side_line
    )
    try:
        outputs = generate_avery_5267_sheet(
            label_spec_fn=label_fn,
            output_stem=args.output,
            missing=args.missing,
            count=args.count,
            dpi=args.dpi,
        )
    except Exception as exc:  # pylint: disable=broad-except
        print("Error:", str(exc), file=sys.stderr)
        return 1

    print("Generated:")
    print("  PNG:", outputs["png"])
    print("  PDF:", outputs["pdf"])
    print("")
    print("Print at 100% scale / Actual size. Do not fit-to-page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
