"""Colour, and knowing when not to use it.

Output from this tool gets piped into files, pasted into issues, and read on
terminals that predate 24-bit colour. So the palette degrades: truecolor to
256 to 16 to nothing, and nothing at all when the output is not a terminal or
when ``NO_COLOR`` is set.

Colour is never the only signal. Every state that has a colour also has a
word, because a reader on a monochrome terminal, or one who cannot distinguish
red from green, has to get the same information.
"""

from __future__ import annotations

import os
import sys

__all__ = ["Palette", "detect_depth"]

RESET = "\x1b[0m"

#: Foreground colours for each semantic role, per depth. Chosen to stay
#: legible on both light and dark backgrounds, which rules out the darkest and
#: lightest ends of the 256-colour cube.
_TRUECOLOR = {
    "ok": (86, 156, 104),
    "warn": (196, 146, 58),
    "bad": (196, 92, 92),
    "muted": (124, 134, 146),
    "accent": (86, 148, 184),
    "key": (188, 192, 200),
}
_256 = {
    "ok": 71,
    "warn": 179,
    "bad": 167,
    "muted": 244,
    "accent": 74,
    "key": 251,
}
_16 = {
    "ok": 32,
    "warn": 33,
    "bad": 31,
    "muted": 90,
    "accent": 36,
    "key": 37,
}


def detect_depth(stream=None) -> str:
    """Pick the richest colour depth this output can actually carry."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") is not None:
        return "none"
    if not hasattr(stream, "isatty") or not stream.isatty():
        return "none"
    if os.environ.get("TERM") == "dumb":
        return "none"
    colorterm = os.environ.get("COLORTERM", "").lower()
    if "truecolor" in colorterm or "24bit" in colorterm:
        return "truecolor"
    term = os.environ.get("TERM", "")
    if "256" in term:
        return "256"
    # Windows Terminal and modern conhost both do truecolor, and neither
    # advertises it through TERM, which does not exist there at all.
    if sys.platform == "win32" and os.environ.get("WT_SESSION"):
        return "truecolor"
    return "16" if term else "none"


class Palette:
    def __init__(self, depth: str = "auto", stream=None) -> None:
        self.depth = detect_depth(stream) if depth == "auto" else depth

    def paint(self, text: str, role: str) -> str:
        if self.depth == "none" or not text:
            return text
        if self.depth == "truecolor":
            red, green, blue = _TRUECOLOR[role]
            return f"\x1b[38;2;{red};{green};{blue}m{text}{RESET}"
        if self.depth == "256":
            return f"\x1b[38;5;{_256[role]}m{text}{RESET}"
        return f"\x1b[{_16[role]}m{text}{RESET}"

    def bold(self, text: str) -> str:
        if self.depth == "none" or not text:
            return text
        return f"\x1b[1m{text}{RESET}"
