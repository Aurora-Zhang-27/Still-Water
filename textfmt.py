"""Align CLI tables by display width (wide characters such as CJK take two columns)."""

from __future__ import annotations

import unicodedata


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, size: int, right: bool = False) -> str:
    """Pad with spaces to a display width; right=True aligns right."""
    fill = " " * max(size - width(text), 0)
    return fill + text if right else text + fill
