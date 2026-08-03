"""Rendering tests.

The report is read at a glance, so the failures that matter are the ones that
make a glance wrong: a bar that rounds 99.9% up to full, a rare field that
draws as absent, or block characters in output that was asked to be ASCII.
"""

from __future__ import annotations

import io
import json
import re
import unittest

from jsonxray.palette import Palette, detect_depth
from jsonxray.profile import Profile
from jsonxray.render import (
    BAR_WIDTH,
    Renderer,
    format_bytes,
    format_count,
    format_number,
    plural,
    truncate,
)
from jsonxray.scan import scan

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def profile_of(records: list) -> Profile:
    text = "\n".join(json.dumps(r) for r in records) + "\n"
    return scan(io.StringIO(text), Profile(source="test"), fmt="jsonl")


class TestBar(unittest.TestCase):
    def setUp(self):
        self.renderer = Renderer(Palette("none"))

    def test_full_only_when_actually_complete(self):
        self.assertEqual(self.renderer.bar(1.0), "█" * BAR_WIDTH)

    def test_almost_full_is_not_drawn_full(self):
        # 99.9% and 100% mean very different things -- one of them has a
        # record that will break your loader.
        self.assertNotEqual(self.renderer.bar(0.999), "█" * BAR_WIDTH)

    def test_a_rare_field_is_visible(self):
        # A field present in 3% of records must not draw as an empty bar; that
        # reads as "not there", the opposite of the truth.
        bar = self.renderer.bar(0.03)
        self.assertNotEqual(bar.strip("·"), "")
        self.assertIn("▌", bar)

    def test_zero_is_empty(self):
        self.assertEqual(self.renderer.bar(0.0), "·" * BAR_WIDTH)

    def test_width_is_constant_across_every_fraction(self):
        for fraction in (0, 0.001, 0.03, 0.5, 0.999, 1.0, 5.0, -1.0):
            self.assertEqual(len(self.renderer.bar(fraction)), BAR_WIDTH, fraction)

    def test_out_of_range_values_are_clamped(self):
        self.assertEqual(self.renderer.bar(5.0), "█" * BAR_WIDTH)
        self.assertEqual(self.renderer.bar(-1.0), "·" * BAR_WIDTH)


class TestPercent(unittest.TestCase):
    def setUp(self):
        self.renderer = Renderer(Palette("none"))

    def test_exact_values_have_no_decimal(self):
        self.assertEqual(self.renderer.percent(1.0), "100%")
        self.assertEqual(self.renderer.percent(0.0), "0%")

    def test_a_tiny_nonzero_share_is_not_rounded_to_zero(self):
        self.assertEqual(self.renderer.percent(0.0002), "<0.1%")

    def test_almost_everything_is_not_rounded_to_all(self):
        self.assertEqual(self.renderer.percent(0.99999), ">99.9%")


class TestCharacterSets(unittest.TestCase):
    def setUp(self):
        self.profile = profile_of(
            [
                {"a": 1, "n": {"x": "p"}, "items": [{"sku": "s"}]},
                {"a": "1", "items": [{"sku": "t"}]},
                {"a": None},
            ]
        )

    def test_ascii_mode_emits_only_ascii(self):
        renderer = Renderer(Palette("none"), unicode=False)
        text = renderer.report(
            self.profile, ["summary", "tree", "conflicts", "shapes", "enums"]
        )
        text.encode("ascii", errors="strict")

    def test_unicode_mode_uses_blocks(self):
        renderer = Renderer(Palette("none"), unicode=True)
        self.assertIn("█", "\n".join(renderer.tree(self.profile)))

    def test_colour_does_not_change_visible_width(self):
        plain = Renderer(Palette("none")).tree(self.profile)
        painted = Renderer(Palette("truecolor")).tree(self.profile)
        self.assertEqual(
            [len(ANSI.sub("", row)) for row in plain],
            [len(ANSI.sub("", row)) for row in painted],
        )

    def test_every_depth_produces_the_same_visible_text(self):
        base = "\n".join(Renderer(Palette("none")).tree(self.profile))
        for depth in ("truecolor", "256", "16"):
            painted = "\n".join(Renderer(Palette(depth)).tree(self.profile))
            self.assertEqual(ANSI.sub("", painted), base, depth)


class TestSections(unittest.TestCase):
    def test_a_clean_file_says_so_rather_than_printing_nothing(self):
        profile = profile_of([{"a": 1}, {"a": 2}])
        text = "\n".join(Renderer(Palette("none")).conflicts(profile))
        self.assertIn("none", text)

    def test_conflicts_name_the_field_and_the_lines(self):
        profile = profile_of([{"a": 1}, {"a": "x"}])
        text = "\n".join(Renderer(Palette("none")).conflicts(profile))
        self.assertIn("a", text)
        self.assertIn("string", text)
        self.assertIn("line 2", text)

    def test_shapes_report_what_is_missing_and_extra(self):
        profile = profile_of([{"a": 1, "b": 2}] * 5 + [{"a": 1, "c": 3}])
        text = "\n".join(Renderer(Palette("none")).shapes(profile))
        self.assertIn("missing", text)
        self.assertIn("extra", text)

    def test_shapes_on_a_uniform_file_says_every_record_matches(self):
        profile = profile_of([{"a": 1}] * 5)
        text = "\n".join(Renderer(Palette("none")).shapes(profile))
        self.assertIn("every record", text)

    def test_truncation_is_stated_in_the_summary(self):
        profile = profile_of([{"a": 1}] * 5)
        profile.truncated_at = 5
        text = "\n".join(Renderer(Palette("none")).summary(profile))
        self.assertIn("stopped after", text)

    def test_enums_list_values_with_shares(self):
        profile = profile_of([{"s": "a"}] * 3 + [{"s": "b"}])
        text = "\n".join(Renderer(Palette("none")).enums(profile))
        self.assertIn("75.0%", text)

    def test_a_field_with_one_value_is_not_called_a_value_set(self):
        profile = profile_of([{"s": "only"}] * 4)
        text = "\n".join(Renderer(Palette("none")).enums(profile))
        self.assertIn("no field", text)

    def test_depth_limits_the_tree(self):
        profile = profile_of([{"outer": {"middle": {"inner": 1}}}])
        renderer = Renderer(Palette("none"))
        shallow = renderer.tree(profile, max_depth=0)
        full = renderer.tree(profile)
        # A heading plus one row, against a heading plus three.
        self.assertEqual(len(shallow), 2)
        self.assertEqual(len(full), 4)
        self.assertIn("outer", shallow[1])
        self.assertNotIn("inner", "\n".join(shallow))


class TestFormatting(unittest.TestCase):
    def test_counts_shorten_as_they_grow(self):
        self.assertEqual(format_count(42), "42")
        self.assertEqual(format_count(1_500), "1.5k")
        self.assertEqual(format_count(2_000), "2k")
        self.assertEqual(format_count(3_400_000), "3.4M")
        self.assertEqual(format_count(5_000_000_000), "5G")

    def test_bytes_scale(self):
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(2048), "2.0 KB")
        self.assertEqual(format_bytes(5 * 1024 * 1024), "5.0 MB")

    def test_numbers_drop_a_pointless_decimal(self):
        self.assertEqual(format_number(4.0), "4")
        self.assertEqual(format_number(4.25), "4.25")

    def test_plural_agrees_with_its_count(self):
        self.assertEqual(plural(1, "record"), "1 record")
        self.assertEqual(plural(2, "record"), "2 records")
        self.assertEqual(plural(0, "record"), "0 records")

    def test_truncate_marks_what_it_cut(self):
        self.assertEqual(truncate("abcdefgh", 5), "abcd…")
        self.assertEqual(truncate("abc", 5), "abc")

    def test_truncate_in_ascii_mode(self):
        self.assertEqual(truncate("abcdefgh", 5, ".."), "abc..")


class TestPalette(unittest.TestCase):
    def test_no_colour_leaves_text_alone(self):
        self.assertEqual(Palette("none").paint("x", "ok"), "x")

    def test_each_depth_emits_an_escape(self):
        for depth in ("truecolor", "256", "16"):
            self.assertIn("\x1b[", Palette(depth).paint("x", "ok"))

    def test_no_color_env_wins_over_a_tty(
        self,
    ):
        class Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        import os

        previous = os.environ.get("NO_COLOR")
        os.environ["NO_COLOR"] = "1"
        try:
            self.assertEqual(detect_depth(Tty()), "none")
        finally:
            if previous is None:
                os.environ.pop("NO_COLOR")
            else:  # pragma: no cover - depends on the caller's environment
                os.environ["NO_COLOR"] = previous

    def test_a_pipe_gets_no_colour(self):
        self.assertEqual(detect_depth(io.StringIO()), "none")


if __name__ == "__main__":
    unittest.main()
