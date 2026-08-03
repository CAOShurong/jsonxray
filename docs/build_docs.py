#!/usr/bin/env python3
"""Generate README.md, images included, by running the tool.

Every figure and every code block in the README comes from an actual run
against the committed example file, so a change in output shows up as a README
diff instead of documentation quietly drifting away from the code.

    python docs/build_docs.py            # regenerate
    python docs/build_docs.py --check    # fail if it would change (for CI)

The example file is generated from a fixed seed, so the numbers are the same
on every machine and the CI freshness gate is meaningful. Images need Pillow,
which is not a runtime dependency:

    python -m pip install pillow
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "docs" / "readme_template.md"
README = ROOT / "README.md"
EXAMPLE = "docs/example.jsonl"
LATER = "docs/example-next-week.jsonl"
BASELINE = "docs/example-profile.json"

SGR = re.compile(r"\x1b\[([0-9;]*)m")

BACKGROUND = "#161719"
DEFAULT_INK = "#c8c9c4"

# Whatever monospace the machine has. The PNG bytes therefore differ between
# platforms, which is why --check compares the README text and not the images.
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\consola.ttf",
    r"C:\Windows\Fonts\cour.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]

#: placeholder -> (image stem, width, CLI arguments)
FIGURES = {
    "<!--SHOT_FIELDS-->": (
        "fields",
        94,
        [EXAMPLE, "--only", "summary", "--only", "tree"],
    ),
    "<!--SHOT_DRIFT-->": (
        "drift",
        94,
        [LATER, "--compare", BASELINE],
    ),
}

#: placeholder -> (width, CLI arguments). Captured without colour so they stay
#: copy-pasteable out of the README.
BLOCKS = {
    "<!--TEXT_CONFLICTS-->": (
        88,
        [EXAMPLE, "--only", "conflicts", "--only", "shapes"],
    ),
    "<!--TEXT_ENUMS-->": (88, [EXAMPLE, "--only", "enums"]),
}


def run_tool(arguments: list[str], *, width: int, colour: bool) -> str:
    """Run the CLI the way a user would, and capture exactly what they see."""
    command = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "jsonxray",
        *arguments,
        "--width",
        str(width),
        "--color",
        "truecolor" if colour else "none",
    ]
    env = {"PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    import os

    merged = dict(os.environ)
    merged.update(env)
    merged.pop("NO_COLOR", None)
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=merged,
    )
    # --compare exits non-zero by design when it finds drift; that is the
    # figure being illustrated, not a build failure.
    if result.returncode not in (0, 2):
        raise SystemExit(
            f"jsonxray {' '.join(arguments)} failed ({result.returncode}):\n"
            f"{result.stderr}"
        )
    return result.stdout.rstrip("\n")


def load_font(size: int):
    from PIL import ImageFont

    for path in FONT_CANDIDATES:
        candidate = pathlib.Path(path)
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:  # pragma: no cover - broken font file
                continue
    raise SystemExit(
        "no monospace font found; install DejaVu Sans Mono or edit "
        "FONT_CANDIDATES in this script"
    )


def parse_sgr(line: str) -> list[tuple[str, str]]:
    """Split an ANSI line into (text, colour) runs.

    Only the codes this tool emits are handled: a truecolor foreground, bold,
    and reset. Anything else is dropped rather than guessed at.
    """
    runs: list[tuple[str, str]] = []
    colour = DEFAULT_INK
    position = 0
    for match in SGR.finditer(line):
        if match.start() > position:
            runs.append((line[position : match.start()], colour))
        codes = [c for c in match.group(1).split(";") if c]
        if not codes or codes == ["0"]:
            colour = DEFAULT_INK
        elif codes[0] == "38" and len(codes) >= 5 and codes[1] == "2":
            red, green, blue = (int(c) for c in codes[2:5])
            colour = f"#{red:02x}{green:02x}{blue:02x}"
        position = match.end()
    if position < len(line):
        runs.append((line[position:], colour))
    return runs


def render_png(text: str, out: pathlib.Path, *, font_size: int = 15) -> None:
    from PIL import Image, ImageDraw

    font = load_font(font_size)
    # Measure the advance of a real character rather than assuming a ratio;
    # Consolas and DejaVu Sans Mono have different aspect ratios, and guessing
    # makes the coloured runs drift out of alignment across the line.
    advance = font.getlength("M")
    line_height = int(font_size * 1.45)
    pad = 18

    lines = text.split("\n")
    columns = max((len(SGR.sub("", line)) for line in lines), default=1)
    width = int(columns * advance) + pad * 2
    height = line_height * len(lines) + pad * 2

    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(lines):
        x = float(pad)
        y = pad + row * line_height
        for chunk, colour in parse_sgr(line):
            draw.text((x, y), chunk, font=font, fill=colour)
            x += font.getlength(chunk)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, optimize=True)


def build(check: bool) -> int:
    if not TEMPLATE.exists():
        raise SystemExit(f"missing template: {TEMPLATE}")

    # The baseline the drift figure compares against, written fresh so the
    # figure can never illustrate a stale profile format.
    run_tool([EXAMPLE, "--save", BASELINE, "--only", "summary"], width=88, colour=False)

    text = TEMPLATE.read_text(encoding="utf-8")

    for placeholder, (stem, width, arguments) in FIGURES.items():
        if placeholder not in text:
            raise SystemExit(f"template has no {placeholder}")
        captured = run_tool(arguments, width=width, colour=True)
        target = ROOT / "docs" / f"{stem}.png"
        if not check:
            render_png(captured, target)
        # An absolute URL, so the image also renders on PyPI, which does not
        # resolve repository-relative paths.
        url = (
            f"https://raw.githubusercontent.com/TeresaCSR/jsonxray/main/docs/{stem}.png"
        )
        text = text.replace(placeholder, f"![{stem}]({url})")

    for placeholder, (width, arguments) in BLOCKS.items():
        if placeholder not in text:
            raise SystemExit(f"template has no {placeholder}")
        captured = run_tool(arguments, width=width, colour=False)
        text = text.replace(placeholder, f"```text\n{captured}\n```")

    if check:
        current = README.read_text(encoding="utf-8") if README.exists() else ""
        if current != text:
            print(
                "README.md is out of date. Run:\n\n    python docs/build_docs.py\n",
                file=sys.stderr,
            )
            return 1
        print("README.md is current")
        return 0

    README.write_text(text, encoding="utf-8", newline="\n")
    print(f"README.md: {len(text.splitlines())} lines")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the README would change",
    )
    args = parser.parse_args()
    return build(args.check)


if __name__ == "__main__":
    raise SystemExit(main())
