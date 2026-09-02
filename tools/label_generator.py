from __future__ import annotations

import argparse
import html
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from board_ids import normalize_board_id


TARGET_OBJECT_NAMES = ("labelText", "labelName")


def _parse_test_date(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("Test timestamp is missing.")

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid test timestamp: {value}") from exc


def _validate_summary_values(
    board_id: str,
    result: str,
    test_timestamp: str,
) -> tuple[str, str, str]:
    board_id = normalize_board_id(board_id)
    result = result.strip().upper()

    if result not in {"PASS", "FAIL", "UNRATED"}:
        raise ValueError(f"Invalid test result for label: {result}")

    month_year = _parse_test_date(test_timestamp).strftime("%m/%y")
    return board_id, result, month_year


def _replace_target_text(
    label_xml: str,
    board_id: str,
    result: str,
    month_year: str,
) -> str:
    target_match = None

    for object_name in TARGET_OBJECT_NAMES:
        pattern = re.compile(
            rf"(<text:text>.*?<pt:expanded\b[^>]*\bobjectName=[\"']{re.escape(object_name)}[\"'][^>]*/>.*?</text:text>)",
            re.DOTALL,
        )
        target_match = pattern.search(label_xml)
        if target_match:
            break

    if not target_match:
        names = re.findall(r'\bobjectName=["\']([^"\']+)["\']', label_xml)
        found = ", ".join(names) if names else "none"
        raise ValueError(
            "Text object labelText/labelName was not found in label.xml. "
            f"Found objectName values: {found}"
        )

    segment = target_match.group(1)
    label_text = f"{board_id}\n{result} {month_year}"
    escaped_text = html.escape(label_text, quote=False)

    segment, count = re.subn(
        r"<pt:data>.*?</pt:data>",
        f"<pt:data>{escaped_text}</pt:data>",
        segment,
        count=1,
        flags=re.DOTALL,
    )
    if count != 1:
        raise ValueError("pt:data of the label text object could not be identified uniquely.")

    # The supplied Brother template contains five equally formatted text runs:
    # 1 character + rest of board ID + line break + result + space + 'MM/YY'.
    char_lengths = [1, max(0, len(board_id) - 1), 1, len(result) + 1, len(month_year)]
    matches = list(
        re.finditer(
            r'(<text:stringItem\b[^>]*\bcharLen=["\'])(\d+)(["\'])',
            segment,
        )
    )

    if len(matches) != 5:
        raise ValueError(
            f"The template contains {len(matches)} text:stringItem blocks instead of the expected 5."
        )

    parts: list[str] = []
    last = 0
    for match, new_length in zip(matches, char_lengths):
        parts.append(segment[last:match.start(2)])
        parts.append(str(new_length))
        last = match.end(2)
    parts.append(segment[last:])
    segment = "".join(parts)

    return label_xml[:target_match.start()] + segment + label_xml[target_match.end():]


def create_brother_label(
    template_path: Path,
    output_path: Path,
    board_id: str,
    result: str,
    test_timestamp: str,
) -> Path:
    board_id, result, month_year = _validate_summary_values(
        board_id,
        result,
        test_timestamp,
    )

    if not template_path.is_file():
        raise FileNotFoundError(f"Brother label template is missing: {template_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(template_path, "r") as source:
        if "label.xml" not in source.namelist():
            raise ValueError("The LBX template does not contain label.xml.")

        with zipfile.ZipFile(output_path, "w") as target:
            for info in source.infolist():
                data = source.read(info.filename)

                if info.filename == "label.xml":
                    label_xml = data.decode("utf-8-sig")
                    label_xml = _replace_target_text(
                        label_xml,
                        board_id,
                        result,
                        month_year,
                    )
                    data = label_xml.encode("utf-8")

                target.writestr(info, data)

    return output_path


def _mm_to_px(value_mm: float, dpi: int) -> int:
    return max(1, int(round((value_mm / 25.4) * dpi)))


def _load_font(size_px: int, bold: bool):
    try:
        from PIL import ImageFont
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for the generic PNG label backend. "
            "Run scripts\\start.cmd once so the local Python environment is updated."
        ) from exc

    size_px = max(8, int(size_px))
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = []

    if bold:
        candidates.extend(
            [
                windir / "Fonts" / "segoeuib.ttf",
                windir / "Fonts" / "arialbd.ttf",
            ]
        )
    else:
        candidates.extend(
            [
                windir / "Fonts" / "segoeui.ttf",
                windir / "Fonts" / "arial.ttf",
            ]
        )

    for path in candidates:
        if not path.is_file():
            continue
        try:
            return ImageFont.truetype(str(path), size_px)
        except OSError:
            pass

    fallback_names = (
        ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf")
        if bold
        else ("DejaVuSans.ttf",)
    )
    for name in fallback_names:
        try:
            return ImageFont.truetype(name, size_px)
        except OSError:
            pass

    return ImageFont.load_default()


def _text_bbox(draw: Any, text: str, font: Any) -> tuple[int, int, int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return int(box[0]), int(box[1]), int(box[2]), int(box[3])


def _fit_font(
    draw: Any,
    text: str,
    max_width_px: int,
    initial_size_px: int,
    bold: bool,
) -> Any:
    size = max(8, initial_size_px)

    while size >= 8:
        font = _load_font(size, bold)
        left, _top, right, _bottom = _text_bbox(draw, text, font)
        if right - left <= max_width_px:
            return font
        size -= max(1, int(round(size * 0.05)))

    return _load_font(8, bold)


def create_png_label(
    output_path: Path,
    board_id: str,
    result: str,
    test_timestamp: str,
    width_mm: float = 62.0,
    height_mm: float = 0.0,
    dpi: int = 300,
    margin_mm: float = 2.0,
) -> Path:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for the generic PNG label backend. "
            "Run scripts\\start.cmd once so the local Python environment is updated."
        ) from exc

    board_id, result, month_year = _validate_summary_values(
        board_id,
        result,
        test_timestamp,
    )

    if width_mm <= 0:
        raise ValueError("Label width_mm must be greater than 0.")
    if height_mm < 0:
        raise ValueError("Label height_mm must be 0 (automatic) or greater than 0.")
    if dpi < 72 or dpi > 1200:
        raise ValueError("Label dpi must be between 72 and 1200.")
    if margin_mm < 0 or margin_mm * 2 >= width_mm:
        raise ValueError("Label margin_mm is invalid for the configured width.")

    width_px = _mm_to_px(width_mm, dpi)
    margin_px = max(0, int(round((margin_mm / 25.4) * dpi)))
    usable_width_px = max(1, width_px - (2 * margin_px))
    measuring_image = Image.new("L", (width_px, _mm_to_px(40.0, dpi)), 255)
    measuring_draw = ImageDraw.Draw(measuring_image)

    board_font = _fit_font(
        measuring_draw,
        board_id,
        usable_width_px,
        _mm_to_px(9.0, dpi),
        True,
    )
    detail_text = f"{result} {month_year}"
    detail_font = _fit_font(
        measuring_draw,
        detail_text,
        usable_width_px,
        _mm_to_px(5.5, dpi),
        True,
    )

    board_box = _text_bbox(measuring_draw, board_id, board_font)
    detail_box = _text_bbox(measuring_draw, detail_text, detail_font)
    board_width = board_box[2] - board_box[0]
    board_height = board_box[3] - board_box[1]
    detail_width = detail_box[2] - detail_box[0]
    detail_height = detail_box[3] - detail_box[1]
    gap_px = _mm_to_px(1.0, dpi)
    required_height_px = (2 * margin_px) + board_height + gap_px + detail_height

    if height_mm == 0:
        height_px = required_height_px
    else:
        height_px = _mm_to_px(height_mm, dpi)
        if height_px < required_height_px:
            minimum_mm = (required_height_px / dpi) * 25.4
            raise ValueError(
                f"Configured label height {height_mm:.2f} mm is too small; "
                f"at least {minimum_mm:.2f} mm is required."
            )

    image = Image.new("L", (width_px, height_px), 255)
    draw = ImageDraw.Draw(image)
    content_height = board_height + gap_px + detail_height
    y = max(margin_px, (height_px - content_height) // 2)

    board_x = (width_px - board_width) // 2 - board_box[0]
    draw.text(
        (board_x, y - board_box[1]),
        board_id,
        font=board_font,
        fill=0,
    )
    y += board_height + gap_px

    detail_x = (width_px - detail_width) // 2 - detail_box[0]
    draw.text(
        (detail_x, y - detail_box[1]),
        detail_text,
        font=detail_font,
        fill=0,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(
        output_path,
        format="PNG",
        dpi=(dpi, dpi),
        optimize=True,
    )
    return output_path


def create_brother_label_from_summary(
    template_path: Path,
    output_path: Path,
    summary: dict[str, Any],
) -> Path:
    return create_brother_label(
        template_path=template_path,
        output_path=output_path,
        board_id=str(summary.get("board_id") or ""),
        result=str(summary.get("result") or ""),
        test_timestamp=str(summary.get("timestamp") or ""),
    )


def create_png_label_from_summary(
    output_path: Path,
    summary: dict[str, Any],
    width_mm: float,
    height_mm: float,
    dpi: int,
    margin_mm: float,
) -> Path:
    return create_png_label(
        output_path=output_path,
        board_id=str(summary.get("board_id") or ""),
        result=str(summary.get("result") or ""),
        test_timestamp=str(summary.get("timestamp") or ""),
        width_mm=width_mm,
        height_mm=height_mm,
        dpi=dpi,
        margin_mm=margin_mm,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate an ESP32 board-test label from summary.json"
    )
    parser.add_argument("summary", type=Path, help="Path to summary.json")
    parser.add_argument(
        "--format",
        choices=("png", "brother-lbx"),
        default="brother-lbx",
        help="Output format / printer backend artifact",
    )
    parser.add_argument("--template", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--width-mm", type=float, default=62.0)
    parser.add_argument("--height-mm", type=float, default=0.0)
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--margin-mm", type=float, default=2.0)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    board_id = normalize_board_id(str(summary.get("board_id") or ""))

    if args.format == "png":
        output = args.output or (args.summary.parent / f"label_{board_id}.png")
        create_png_label_from_summary(
            output,
            summary,
            width_mm=args.width_mm,
            height_mm=args.height_mm,
            dpi=args.dpi,
            margin_mm=args.margin_mm,
        )
    else:
        template = args.template or (root / "templates" / "label_brother.lbx")
        output = args.output or (
            args.summary.parent / f"label_{board_id}_brother.lbx"
        )
        create_brother_label_from_summary(template, output, summary)

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
