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


def _to_px(inches, dpi):
    return int(round(inches * dpi))


def _load_font(ImageFont, size_px):
    # Try common TrueType fonts first for scalable text.
    font_candidates = [
        "arial.ttf",
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


def _get_text_size(font, text):
    # Works across older/newer Pillow versions.
    if hasattr(font, "getbbox"):
        left, top, right, bottom = font.getbbox(text)
        return right - left, bottom - top
    return font.getsize(text)


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


def _normalize_label_spec(label_spec):
    if label_spec is None:
        return {
            "lines": [],
            "has_aruco": False,
            "aruco_id": 0,
            "aruco_dict": DEFAULT_ARUCO_DICT,
        }

    lines = label_spec.get("lines", [])
    if isinstance(lines, str):
        lines = [lines]
    lines = [str(line) for line in lines if str(line).strip()]

    return {
        "lines": lines,
        "has_aruco": bool(label_spec.get("has_aruco", False)),
        "aruco_id": int(label_spec.get("aruco_id", 0)),
        "aruco_dict": str(label_spec.get("aruco_dict", DEFAULT_ARUCO_DICT)),
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
    if hasattr(cv2.aruco, "generateImageMarker"):
        marker = cv2.aruco.generateImageMarker(dictionary, marker_id, side_px)
    elif hasattr(cv2.aruco, "drawMarker"):
        marker = cv2.aruco.drawMarker(dictionary, marker_id, side_px)
    else:
        raise RuntimeError("This OpenCV version cannot generate ArUco marker images.")
    return marker


def _draw_label(
    draw, Image, ImageFont, page, x0, y0, label_w, label_h, label_spec, dpi
):
    inner_pad = max(8, int(round(0.02 * dpi)))  # ~0.02"
    gap = max(8, int(round(0.02 * dpi)))

    lines = label_spec["lines"]
    has_aruco = label_spec["has_aruco"]
    aruco_id = label_spec["aruco_id"]
    aruco_dict = label_spec["aruco_dict"]

    text_x0 = x0 + inner_pad
    text_y0 = y0 + inner_pad
    text_y1 = y0 + label_h - inner_pad
    text_x1 = x0 + label_w - inner_pad

    if has_aruco:
        marker_side = label_h - (2 * inner_pad)
        marker_side = max(20, marker_side)
        marker_img = _get_aruco_marker(marker_side, aruco_id, aruco_dict)
        marker_pil = Image.fromarray(marker_img).convert("L")
        marker_x = x0 + inner_pad
        marker_y = y0 + inner_pad
        page.paste(marker_pil, (marker_x, marker_y))
        text_x0 = marker_x + marker_side + gap

    text_box_w = max(10, text_x1 - text_x0)
    text_box_h = max(10, text_y1 - text_y0)
    font = _fit_font(ImageFont, lines, text_box_w, text_box_h)

    line_sizes = [_get_text_size(font, line) for line in lines]
    total_height = sum(h for _, h in line_sizes)
    current_y = text_y0 + max(0, (text_box_h - total_height) // 2)

    for line, (line_w, line_h) in zip(lines, line_sizes):
        line_x = text_x0 + max(0, (text_box_w - line_w) // 2)
        draw.text((line_x, current_y), line, fill=0, font=font)
        current_y += line_h


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
              - has_aruco: bool
              - aruco_id: int
              - aruco_dict: str (e.g. DICT_6X6_250)
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
            draw, Image, ImageFont, page, x0, y0, label_w, label_h, label_spec, dpi
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
        "--has-aruco",
        action="store_true",
        help="Include an ArUco marker in each generated label.",
    )
    parser.add_argument(
        "--aruco-id",
        type=int,
        default=0,
        help="ArUco marker ID (default: 0).",
    )
    parser.add_argument(
        "--aruco-dict",
        default=DEFAULT_ARUCO_DICT,
        help="ArUco dictionary name (default: DICT_6X6_250).",
    )
    parser.add_argument(
        "--line",
        action="append",
        default=[],
        help="Label text line. Repeat this flag for multiple lines.",
    )
    return parser.parse_args(argv)


def _make_static_label_fn(lines, has_aruco, aruco_id, aruco_dict):
    clean_lines = [line for line in lines if line.strip()]
    if not clean_lines:
        clean_lines = ["Label"]

    def _label_fn(seq_index, slot_index):  # pylint: disable=unused-argument
        return {
            "lines": clean_lines,
            "has_aruco": has_aruco,
            "aruco_id": aruco_id,
            "aruco_dict": aruco_dict,
        }

    return _label_fn


def main(argv=None):
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    label_fn = _make_static_label_fn(
        args.line, args.has_aruco, args.aruco_id, args.aruco_dict
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
