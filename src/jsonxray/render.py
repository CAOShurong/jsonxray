"""Turning the profile into something a person reads in ten seconds.

The design rule throughout: put the thing that is wrong where the eye lands
first. A file where every field is present in every record needs one line of
output. A file where three fields are sometimes missing and one holds two
different types needs those four facts at the top, with line numbers, and
everything else underneath.

Percentages are always relative to a stated denominator. "Present in 40%" is
meaningless without saying 40% of what, and for a field nested inside an
optional parent, or inside an array, the denominator is not the record count.
"""

from __future__ import annotations

from .palette import Palette
from .profile import FieldNode, Profile, denominator_for

__all__ = ["Renderer"]

#: Glyphs for the presence bar, in the two character sets. The ASCII set is
#: not a lesser fallback -- it is what gets pasted into an issue tracker that
#: mangles box drawing, which is most of them.
GLYPHS = {
    True: {
        "full": "█",
        "part": "▌",
        "empty": "·",
        "bullet": "•",
        "sep": " · ",
        "ellipsis": "…",
    },
    False: {
        "full": "#",
        "part": "=",
        "empty": ".",
        "bullet": "*",
        # Not the middot, and not the pipe either: the pipe already means
        # "this field holds either of these types" a few columns to the left.
        "sep": " - ",
        "ellipsis": "..",
    },
}

BAR_WIDTH = 12


def plural(count: int, noun: str, suffix: str = "s") -> str:
    """``1 line``, ``2 lines``. Small thing; reads as broken when wrong."""
    return f"{format_count(count)} {noun}{'' if count == 1 else suffix}"


def format_count(value: int) -> str:
    """Counts get large. 1.2M reads faster than 1204418."""
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k".replace(".0k", "k")
    if value < 1_000_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    return f"{value / 1_000_000_000:.1f}G".replace(".0G", "G")


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def format_number(value: float) -> str:
    """Render a measured number without pretending to precision it lacks."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.4g}"


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    if len(text) <= width:
        return text
    if width <= len(ellipsis):
        return text[:width]
    return text[: width - len(ellipsis)] + ellipsis


class Renderer:
    def __init__(
        self,
        palette: Palette,
        *,
        width: int = 100,
        unicode: bool = True,
    ) -> None:
        self.palette = palette
        self.width = max(60, width)
        self.glyphs = GLYPHS[unicode]
        self.unicode = unicode

    # -- pieces ------------------------------------------------------------

    def bar(self, fraction: float, width: int = BAR_WIDTH) -> str:
        """A presence bar that never rounds a partial value to full.

        99.9% presence and 100% presence mean very different things -- one of
        them has a record that will break your loader -- so a fraction below 1
        always leaves the last cell unfilled.
        """
        fraction = min(1.0, max(0.0, fraction))
        if fraction >= 1.0:
            return self.glyphs["full"] * width
        filled = int(fraction * width)
        if filled >= width:
            filled = width - 1
        remainder = fraction * width - filled
        cells = self.glyphs["full"] * filled
        # A field present in 3% of records would otherwise draw as an empty
        # bar, which reads as "not there" -- the opposite of the truth, and
        # exactly the rare-field case worth noticing.
        if (remainder >= 0.5 or (filled == 0 and fraction > 0)) and filled < width:
            cells += self.glyphs["part"]
        return cells + self.glyphs["empty"] * (width - len(cells))

    def percent(self, fraction: float) -> str:
        value = fraction * 100
        if 0 < value < 0.1:
            return "<0.1%"
        if 99.9 < value < 100:
            return ">99.9%"
        return f"{value:.0f}%" if value in (0, 100) else f"{value:.1f}%"

    def role_for(self, node: FieldNode, fraction: float) -> str:
        if node.has_type_conflict:
            return "bad"
        if fraction >= 1.0:
            return "ok"
        return "warn"

    # -- sections ----------------------------------------------------------

    def summary(self, profile: Profile) -> list[str]:
        rows = [self.palette.bold(f"jsonxray  {profile.source}")]
        parts = [
            plural(profile.records, "record"),
            format_bytes(profile.bytes_read),
            plural(_field_count(profile), "field"),
        ]
        rows.append("  " + self.palette.paint(self.glyphs["sep"].join(parts), "muted"))

        notes: list[str] = []
        if profile.truncated_at is not None:
            notes.append(
                self.palette.paint(
                    f"stopped after {plural(profile.truncated_at, 'record')}"
                    + self.glyphs["sep"]
                    + "every percentage below describes only those",
                    "warn",
                )
            )
        if profile.malformed_count:
            where = ", ".join(f"line {m.lineno}" for m in profile.malformed)
            more = (
                f" (+{profile.malformed_count - len(profile.malformed)} more)"
                if profile.malformed_count > len(profile.malformed)
                else ""
            )
            notes.append(
                self.palette.paint(
                    f"{plural(profile.malformed_count, 'unparseable line')}: "
                    f"{where}{more}",
                    "bad",
                )
            )
        if profile.non_object_records:
            notes.append(
                self.palette.paint(
                    f"{plural(profile.non_object_records, 'record')} were not "
                    "objects, so they have no fields",
                    "warn",
                )
            )
        if profile.blank_lines:
            notes.append(
                self.palette.paint(
                    f"{plural(profile.blank_lines, 'blank line')} skipped",
                    "muted",
                )
            )
        rows.extend("  " + note for note in notes)
        return rows

    def tree(self, profile: Profile, *, max_depth: int = 99) -> list[str]:
        """The field tree, each row carrying its own denominator."""
        rows = [self.palette.bold("Fields")]
        if not profile.root.children:
            rows.append("  " + self.palette.paint("(no object fields)", "muted"))
            return rows
        self._tree_rows(profile.root, rows, depth=0, max_depth=max_depth)
        return rows

    def _tree_rows(
        self,
        node: FieldNode,
        rows: list[str],
        *,
        depth: int,
        max_depth: int,
    ) -> None:
        if depth > max_depth:
            return
        for key, child in sorted(node.children.items()):
            denominator = denominator_for(node, key)
            fraction = child.occurrences / denominator if denominator else 0.0
            rows.append(self._tree_row(child, key, fraction, depth))
            self._tree_rows(child, rows, depth=depth + 1, max_depth=max_depth)

    def _tree_row(
        self,
        node: FieldNode,
        key: str,
        fraction: float,
        depth: int,
    ) -> str:
        indent = "  " + "  " * depth
        role = self.role_for(node, fraction)
        label_width = max(14, 26 - depth * 2)
        label = truncate(key, label_width, self.glyphs["ellipsis"])
        bar = self.palette.paint(self.bar(fraction), role)
        share = self.palette.paint(f"{self.percent(fraction):>6}", role)

        types = node.non_null_types
        if node.has_type_conflict:
            type_text = self.palette.paint(" | ".join(types), "bad")
        else:
            type_text = self.palette.paint(" | ".join(types) or "null", "accent")

        detail = self._detail(node)
        row = f"{indent}{label:<{label_width}} {bar} {share}  {type_text}{detail}"
        return row

    def _detail(self, node: FieldNode) -> str:
        """The one extra fact worth carrying on the field's own line."""
        bits: list[str] = []
        if node.null_count:
            # A count, not a share. The bar is a fraction of the parent and
            # this would be a fraction of the field's own occurrences; two
            # percentages on different denominators in one row is a reliable
            # way to be misread.
            bits.append(f"{format_count(node.null_count)} null")
        if node.numeric.count and not node.has_type_conflict:
            low = format_number(node.numeric.minimum or 0)
            high = format_number(node.numeric.maximum or 0)
            bits.append(f"{low}..{high}")
        elif node.string_lengths.count and not node.has_type_conflict:
            lengths = node.string_lengths
            if lengths.minimum == lengths.maximum:
                bits.append(f"len {lengths.minimum}")
            else:
                bits.append(f"len {lengths.minimum}..{lengths.maximum}")
        if node.array_lengths.count:
            lengths = node.array_lengths
            bits.append(f"{lengths.minimum}..{lengths.maximum} items")
        if not bits:
            return ""
        return "  " + self.palette.paint(self.glyphs["sep"].join(bits), "muted")

    def conflicts(self, profile: Profile) -> list[str]:
        """Fields holding more than one kind of thing, with line numbers."""
        found = profile.conflicts()
        rows = [self.palette.bold("Type conflicts")]
        if not found:
            rows.append(
                "  " + self.palette.paint("none: every field is one type", "ok")
            )
            return rows
        for node in found:
            rows.append("  " + self.palette.paint(node.path or "(root)", "bad"))
            for kind in sorted(node.type_counts, key=lambda k: -node.type_counts[k]):
                if kind == "null":
                    continue
                count = node.type_counts[kind]
                share = count / node.occurrences if node.occurrences else 0
                lines = node.examples.get(kind, [])
                where = "line " + ", ".join(str(n) for n in lines) if lines else ""
                rows.append(
                    f"      {kind:<9} {self.percent(share):>6}  "
                    + self.palette.paint(where, "muted")
                )
        return rows

    def shapes(self, profile: Profile, limit: int = 5) -> list[str]:
        """What the file mostly looks like, and what it does not."""
        modal = profile.modal_shape
        rows = [self.palette.bold("Record shapes")]
        if modal is None:
            rows.append("  " + self.palette.paint("(no object records)", "muted"))
            return rows

        share = modal.count / profile.records if profile.records else 0
        rows.append(
            "  "
            + self.palette.paint(
                f"{self.percent(share)} of records share one shape "
                f"({len(modal.paths)} paths)",
                "ok" if share > 0.9 else "warn",
            )
        )

        outliers = profile.outliers(limit)
        if not outliers:
            rows.append("  " + self.palette.paint("every record has that shape", "ok"))
            return rows

        rows.append(
            "  "
            + self.palette.paint(
                "least typical records, by distance from that shape:", "muted"
            )
        )
        for group, distance in outliers:
            missing = sorted(modal.paths - group.paths)
            extra = sorted(group.paths - modal.paths)
            where = ", ".join(str(n) for n in group.examples)
            rows.append(
                f"    {self.glyphs['bullet']} "
                + self.palette.paint(f"{format_count(group.count)}x", "accent")
                + self.palette.paint(f"  line {where}", "muted")
            )
            if missing:
                rows.append(
                    "        "
                    + self.palette.paint("missing ", "bad")
                    + self.palette.paint(_join(missing), "muted")
                )
            if extra:
                rows.append(
                    "        "
                    + self.palette.paint("extra   ", "warn")
                    + self.palette.paint(_join(extra), "muted")
                )
            if not missing and not extra:  # pragma: no cover - defensive
                rows.append(f"        differs by {distance} paths")
        if profile.shapes_overflowed:
            rows.append(
                "  "
                + self.palette.paint(
                    "more shapes existed than could be tracked; this list is "
                    "the ones seen first",
                    "warn",
                )
            )
        return rows

    def enums(self, profile: Profile, limit: int = 8) -> list[str]:
        """Fields whose values form a small closed set."""
        found = [
            node
            for node in profile.root.walk()
            if node.is_enum and len(node.values) > 1 and node.path
        ]
        found.sort(key=lambda n: (len(n.values), n.path))
        rows = [self.palette.bold("Small value sets")]
        if not found:
            rows.append(
                "  " + self.palette.paint("no field has a small value set", "muted")
            )
            return rows
        for node in found[:limit]:
            rows.append("  " + self.palette.paint(node.path, "accent"))
            ordered = sorted(node.values.items(), key=lambda kv: -kv[1])
            total = sum(node.values.values())
            for value, count in ordered[:6]:
                share = count / total if total else 0
                shown = truncate(value, 34, self.glyphs["ellipsis"])
                rows.append(
                    f"      {shown:<34} {self.percent(share):>6}  "
                    + self.palette.paint(format_count(count), "muted")
                )
            if len(ordered) > 6:
                rows.append(
                    "      "
                    + self.palette.paint(f"+{len(ordered) - 6} more values", "muted")
                )
        if len(found) > limit:
            rows.append(
                "  "
                + self.palette.paint(
                    f"+{len(found) - limit} more fields with small value sets",
                    "muted",
                )
            )
        return rows

    def report(
        self,
        profile: Profile,
        sections: list[str],
        *,
        max_depth: int = 99,
    ) -> str:
        methods = {
            "summary": self.summary,
            "tree": lambda p: self.tree(p, max_depth=max_depth),
            "conflicts": self.conflicts,
            "shapes": self.shapes,
            "enums": self.enums,
        }
        return "\n\n".join("\n".join(methods[name](profile)) for name in sections)


def _join(paths: list[str], limit: int = 4) -> str:
    if len(paths) <= limit:
        return ", ".join(paths)
    return ", ".join(paths[:limit]) + f", +{len(paths) - limit} more"


def _field_count(profile: Profile) -> int:
    # The root is not a field, and neither is an array's element node: nobody
    # thinks of ``items[]`` as a field alongside ``items``.
    return sum(
        1 for node in profile.root.walk() if node.path and not node.path.endswith("[]")
    )
