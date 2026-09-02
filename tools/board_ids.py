from __future__ import annotations

import re

BOARD_TYPES = ("E32", "C3", "S3")

_CANONICAL_RE = re.compile(r"^(\d{3})-(E32|C3|S3)$", re.IGNORECASE)
_LEGACY_RE = re.compile(r"^(E32|C3|S3)-(\d{3})$", re.IGNORECASE)
_NUMBER_RE = re.compile(r"^\d{1,3}$")


def normalize_board_id(
    value: str,
    expected_type: str | None = None,
    allow_number: bool = False,
) -> str:
    text = str(value or "").strip().upper()
    expected = str(expected_type or "").strip().upper()

    if expected and expected not in BOARD_TYPES:
        raise ValueError(f"Unknown board type: {expected}")

    board_type = ""
    number = 0

    match = _CANONICAL_RE.fullmatch(text)
    if match:
        number = int(match.group(1))
        board_type = match.group(2).upper()
    else:
        match = _LEGACY_RE.fullmatch(text)
        if match:
            board_type = match.group(1).upper()
            number = int(match.group(2))
        elif allow_number and expected and _NUMBER_RE.fullmatch(text):
            board_type = expected
            number = int(text)
        else:
            raise ValueError(f"Invalid board ID: {value}")

    if number < 1 or number > 999:
        raise ValueError(f"Invalid board number: {number}")

    if expected and board_type != expected:
        raise ValueError(
            f"Board ID belongs to {board_type}; expected {expected}."
        )

    return f"{number:03d}-{board_type}"


def try_normalize_board_id(value: str) -> str | None:
    try:
        return normalize_board_id(value)
    except ValueError:
        return None


def board_id_number(value: str) -> int:
    canonical = normalize_board_id(value)
    return int(canonical[:3])


def board_id_type(value: str) -> str:
    canonical = normalize_board_id(value)
    return canonical.split("-", 1)[1]


def legacy_board_id(value: str) -> str:
    canonical = normalize_board_id(value)
    number, board_type = canonical.split("-", 1)
    return f"{board_type}-{number}"
