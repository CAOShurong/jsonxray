"""Command line entry point."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys

from . import __version__
from .compare import compare
from .palette import Palette
from .profile import Profile
from .render import Renderer
from .scan import ScanError, open_source, scan

__all__ = ["main"]

SECTIONS = ("summary", "tree", "conflicts", "shapes", "enums")

#: Exit status when a comparison finds a breaking change. Distinct from 1 so a
#: CI step can tell "the data changed" from "the tool fell over", which
#: matters when the pipeline should page someone for one and not the other.
EXIT_DRIFT = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jsonxray",
        description=(
            "Show what is really inside a JSON Lines file: which fields "
            "exist, how often, what types they hold, and which lines break "
            "the pattern."
        ),
        epilog=(
            "Examples:\n"
            "  jsonxray data.jsonl\n"
            "  cat data.jsonl | jsonxray -\n"
            "  jsonxray big.jsonl --limit 50000\n"
            "  jsonxray data.jsonl --save baseline.json\n"
            "  jsonxray today.jsonl --compare baseline.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="file to read, or - for standard input",
    )
    parser.add_argument(
        "--format",
        choices=("auto", "jsonl", "array"),
        default="auto",
        help="input shape; auto detects a top-level JSON array (default: auto)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="stop after N records; the report says it was truncated",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=99,
        metavar="N",
        help="how deep to print the field tree (default: all)",
    )
    parser.add_argument(
        "--only",
        metavar="SECTION",
        action="append",
        choices=SECTIONS,
        help=(f"print only this section; repeatable. one of: {', '.join(SECTIONS)}"),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="write the profile as JSON instead of a report",
    )
    parser.add_argument(
        "--save",
        metavar="FILE",
        help="write the profile to FILE for a later --compare",
    )
    parser.add_argument(
        "--compare",
        metavar="FILE",
        help=(
            "compare against a saved profile and report drift; "
            f"exits {EXIT_DRIFT} if a breaking change is found"
        ),
    )
    parser.add_argument(
        "--color",
        choices=("auto", "truecolor", "256", "16", "none"),
        default="auto",
        help="colour depth (default: auto; NO_COLOR is always honoured)",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="use only ASCII, for terminals and issue trackers that mangle blocks",
    )
    parser.add_argument(
        "--width",
        type=int,
        help="output width (default: the terminal's)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"jsonxray {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    _use_utf8(sys.stdout)
    _use_utf8(sys.stderr)

    if args.path is None:
        # A bare invocation on a terminal is someone finding their way. Piped
        # input with no path is unambiguous, so read it rather than nagging.
        if sys.stdin.isatty():
            parser.print_help()
            return 2
        args.path = "-"

    try:
        handle, source = open_source(args.path)
    except ScanError as exc:
        print(f"jsonxray: {exc}", file=sys.stderr)
        return 1

    profile = Profile(source=os.path.basename(source) if source != "-" else "stdin")
    try:
        scan(handle, profile, fmt=args.format, limit=args.limit)
    except ScanError as exc:
        print(f"jsonxray: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("jsonxray: interrupted", file=sys.stderr)
        return 130
    finally:
        if handle is not sys.stdin:
            handle.close()

    if args.save:
        with open(args.save, "w", encoding="utf-8") as out:
            json.dump(profile.as_dict(), out, indent=2, sort_keys=True)

    if args.compare:
        return _run_compare(args, profile)

    if args.json:
        json.dump(profile.as_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    if profile.records == 0 and profile.malformed_count == 0:
        print(f"jsonxray: {profile.source} contained no JSON records", file=sys.stderr)
        return 1

    renderer = Renderer(
        Palette(args.color),
        width=args.width or shutil.get_terminal_size((100, 24)).columns,
        unicode=not args.ascii,
    )
    sections = args.only or list(SECTIONS)
    print(renderer.report(profile, sections, max_depth=args.depth))
    return 0


def _run_compare(args, profile: Profile) -> int:
    try:
        with open(args.compare, encoding="utf-8") as handle:
            baseline = Profile.from_dict(json.load(handle))
    except (OSError, ValueError) as exc:
        print(f"jsonxray: cannot read {args.compare}: {exc}", file=sys.stderr)
        return 1

    result = compare(baseline, profile)
    if args.json:
        json.dump(result.as_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0 if result.ok else EXIT_DRIFT

    palette = Palette(args.color)
    if not result.changes:
        print(palette.paint("no change against " + args.compare, "ok"))
        return 0

    print(palette.bold(f"{profile.source} vs {args.compare}"))
    for change in result.changes:
        marker = "BREAK" if change.breaking else "note "
        role = "bad" if change.breaking else "muted"
        label = change.path or "(file)"
        print(
            f"  {palette.paint(marker, role)} "
            f"{palette.paint(label, 'accent')}  {change.detail}"
        )

    if result.breaking:
        print()
        print(
            palette.paint(
                f"{len(result.breaking)} breaking change"
                f"{'s' if len(result.breaking) != 1 else ''}",
                "bad",
            )
        )
        return EXIT_DRIFT
    return 0


def _use_utf8(stream) -> None:
    """Make sure block characters survive a non-UTF-8 console.

    A Windows console defaults to the system codepage, which cannot encode the
    bar glyphs, and the failure is a UnicodeEncodeError in the middle of the
    report rather than anything a user could act on.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    # Nothing to do if the stream refuses; the report still prints, and on
    # a console that cannot encode blocks --ascii is the answer.
    with contextlib.suppress(OSError, ValueError):
        reconfigure(encoding="utf-8", errors="replace")
