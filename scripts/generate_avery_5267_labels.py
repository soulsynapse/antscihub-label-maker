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


def _load_bold_font(ImageFont, size_px):
    # Prefer bold Arial for side-line text.
    font_candidates = [
        "arialbd.ttf",
        "ARIALBD.TTF",
        "Arial Bold.ttf",
        "arialnb.ttf",
        "ARIALNB.TTF",
        "Arial Narrow Bold.ttf",
        "arialn.ttf",
        "ARIALN.TTF",
        "Arial Narrow.ttf",
        "arial.ttf",
        "Arial.ttf",
        "ARIAL.TTF",
        "DejaVuSans-Bold.ttf",
        "LiberationSans-Bold.ttf",
    ]
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

    marker_id = int(label_spec.get("marker_id", label_spec.get("aruco_id", 0)))
    marker_dict = str(
        label_spec.get("marker_dict", label_spec.get("aruco_dict", DEFAULT_ARUCO_DICT))
    )
    side_line = str(label_spec.get("side_line", ""))

    return {
        "lines": lines,
        "marker_type": marker_type,
        "marker_id": marker_id,
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
    marker = marker.convert("L")
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
):
    text = str(side_line_text).strip()
    if not text:
        return None

    box_w = _to_px(SIDE_LINE_WIDTH_IN, dpi)
    box_h = _to_px(SIDE_LINE_HEIGHT_IN, dpi)
    right_offset = _to_px(SIDE_LINE_RIGHT_OFFSET_IN, dpi)

    box_w = max(1, min(box_w, label_w))
    box_h = max(1, min(box_h, label_h))
    side_x = x0 + label_w - right_offset - box_w
    side_x = max(x0, min(x0 + label_w - box_w, side_x))
    side_y = y0 + int(round((label_h - box_h) / 2.0))

    # Draw black box.
    side_box = Image.new("L", (box_w, box_h), color=0)

    # Render white bold text rotated so it reads along the long side of the box.
    text_canvas = Image.new("L", (box_h, box_w), color=0)
    text_draw = ImageDraw.Draw(text_canvas)
    pad = 2
    max_font_size = max(6, box_w)
    chosen_font = _load_bold_font(ImageFont, max_font_size)
    for size in range(max_font_size, 0, -1):
        trial_font = _load_bold_font(ImageFont, size)
        left, top, right, bottom = _get_text_bbox(trial_font, text)
        tw = right - left
        th = bottom - top
        if tw <= (box_h - (2 * pad)) and th <= (box_w - (2 * pad)):
            chosen_font = trial_font
            break

    left, top, right, bottom = _get_text_bbox(chosen_font, text)
    tw = right - left
    th = bottom - top
    tx = int(round((box_h - tw) / 2.0 - left))
    ty = int(round((box_w - th) / 2.0 - top))
    text_draw.text((tx, ty), text, fill=255, font=chosen_font)

    # Keep output orientation aligned with GUI preview direction.
    rotated = text_canvas.rotate(90, expand=True)
    side_box.paste(rotated, (0, 0), rotated)
    page.paste(side_box, (side_x, side_y))

    return {
        "x": side_x,
        "y": side_y,
        "w": box_w,
        "h": box_h,
    }


def _draw_label(
    draw, Image, ImageDraw, ImageFont, page, x0, y0, label_w, label_h, label_spec, dpi
):
    horizontal_pad = max(8, int(round(0.02 * dpi)))  # ~0.02"
    marker_text_gap = _to_px(ARUCO_RIGHT_GAP_IN, dpi)
    text_vertical_pad = _to_px(TEXT_VERTICAL_PADDING_IN, dpi)

    lines = label_spec["lines"]
    marker_type = label_spec["marker_type"]
    marker_id = label_spec["marker_id"]
    marker_dict = label_spec["marker_dict"]
    side_line_text = label_spec.get("side_line", "")

    text_x0 = x0 + horizontal_pad
    text_y0 = y0 + text_vertical_pad
    text_y1 = y0 + label_h - text_vertical_pad
    text_x1 = x0 + label_w - horizontal_pad

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
    )
    if side_box is not None:
        text_x1 = min(text_x1, side_box["x"] - _to_px(SIDE_LINE_TEXT_GAP_IN, dpi))

    if marker_type != MARKER_TYPE_NONE:
        marker_side = _to_px(ARUCO_MARKER_SIZE_IN, dpi)
        marker_side = max(1, min(marker_side, label_h))
        if marker_type == MARKER_TYPE_ARUCO:
            marker_img = _get_aruco_marker(marker_side, marker_id, marker_dict)
            marker_pil = Image.fromarray(marker_img).convert("L")
        elif marker_type == MARKER_TYPE_DATAMATRIX:
            marker_pil = _get_datamatrix_marker(Image, marker_side, marker_id)
        elif marker_type == MARKER_TYPE_QR:
            marker_pil = _get_qr_marker(Image, marker_side, marker_id)
        else:
            marker_pil = None
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
):
    """
    Generate one 8.5x11 Avery 5267 label sheet at high resolution.

    Args:
        label_spec_fn: callable(sequence_index, sheet_slot_index) -> dict
            Expected keys:
              - lines: list[str] or str
              - marker_type: "none" | "aruco" | "datamatrix" | "qr"
              - marker_id: int
              - marker_dict: str (ArUco dictionary, e.g. DICT_6X6_250)
              - side_line: str (optional text inside right-side black strip)
        output_stem: path without extension (or with extension; extension is removed)
        missing: number of labels already used on the sheet (0..79), filled row-major
        count: number of labels to print from remaining slots (default: all remaining)
        dpi: raster output DPI (default: 1200)

    Returns:
        dict with generated file paths: {"png": "...", "pdf": "..."}
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

    for seq_index in range(count):
        slot_index = missing + seq_index
        row = slot_index // COLS
        col = slot_index % COLS

        x0 = _to_px(LEFT_MARGIN_IN + (col * H_PITCH_IN), dpi)
        y0 = _to_px(TOP_MARGIN_IN + (row * V_PITCH_IN), dpi)

        label_spec = _normalize_label_spec(label_spec_fn(seq_index, slot_index))
        _draw_label(
            draw, Image, ImageDraw, ImageFont, page, x0, y0, label_w, label_h, label_spec, dpi
        )

    output_root = os.path.splitext(output_stem)[0]
    png_path = output_root + ".png"
    pdf_path = output_root + ".pdf"

    page.save(png_path, dpi=(dpi, dpi))
    # For PDF, keep page dimensions aligned to the same physical size at target DPI.
    page.save(pdf_path, "PDF", resolution=dpi)

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
        help="Marker type: none, aruco, or datamatrix (default: none).",
    )
    parser.add_argument(
        "--id",
        type=int,
        default=0,
        help="Marker ID/value (default: 0).",
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
        help="Optional side-strip text (white, bold) for the right-side 8mm x 2mm box.",
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


def main(argv=None):
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    marker_type = _normalize_marker_type(args.marker_type)
    marker_id = int(args.id)
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
