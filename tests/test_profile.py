"""Accumulator tests.

The numbers this tool prints are the product. A presence figure quoted against
the wrong denominator, or a type conflict that is really just an integer next
to a float, is worse than no output at all -- it sends someone to look for a
data problem that does not exist, or lets a real one through.
"""

from __future__ import annotations

import io
import json
import unittest

from jsonxray.profile import (
    MAX_DISTINCT_VALUES,
    MAX_SHAPES,
    MAX_VALUE_LENGTH,
    Profile,
    denominator_for,
    type_name,
)
from jsonxray.scan import scan


def profile_of(records: list, source: str = "test") -> Profile:
    text = "\n".join(json.dumps(r) for r in records) + "\n"
    return scan(io.StringIO(text), Profile(source=source), fmt="jsonl")


def node(profile: Profile, path: str):
    for candidate in profile.root.walk():
        if candidate.path == path:
            return candidate
    raise AssertionError(f"no node at {path!r}")


def presence(profile: Profile, path: str) -> float:
    """Presence the way the report computes it, via the parent."""
    parts = path.replace("[]", ".[]").split(".")
    parent = profile.root
    for part in parts[:-1]:
        parent = parent.children[part]
    key = parts[-1]
    denominator = denominator_for(parent, key)
    return parent.children[key].occurrences / denominator


class TestTypeNaming(unittest.TestCase):
    def test_bool_is_not_reported_as_an_integer(self):
        # bool subclasses int in Python. Checking int first would silently
        # merge `true` and `1`, which is one of the exact defects this tool
        # exists to surface.
        self.assertEqual(type_name(True), "boolean")
        self.assertEqual(type_name(False), "boolean")
        self.assertEqual(type_name(1), "integer")
        self.assertEqual(type_name(0), "integer")

    def test_integers_and_floats_are_distinguished(self):
        self.assertEqual(type_name(1), "integer")
        self.assertEqual(type_name(1.0), "float")

    def test_containers_and_null(self):
        self.assertEqual(type_name(None), "null")
        self.assertEqual(type_name([]), "array")
        self.assertEqual(type_name({}), "object")
        self.assertEqual(type_name("x"), "string")


class TestConflicts(unittest.TestCase):
    def test_string_beside_integer_is_a_conflict(self):
        p = profile_of([{"a": 1}, {"a": "1"}])
        self.assertTrue(node(p, "a").has_type_conflict)
        self.assertEqual(p.conflicts()[0].path, "a")

    def test_integer_beside_float_is_not_a_conflict(self):
        # Both are numbers. Flagging this would fire on almost every real
        # file and get the check switched off.
        p = profile_of([{"a": 1}, {"a": 1.5}])
        self.assertFalse(node(p, "a").has_type_conflict)

    def test_null_beside_one_type_is_optionality_not_a_conflict(self):
        p = profile_of([{"a": 1}, {"a": None}])
        self.assertFalse(node(p, "a").has_type_conflict)
        self.assertEqual(node(p, "a").null_count, 1)

    def test_three_types_conflict_even_including_the_numeric_pair(self):
        p = profile_of([{"a": 1}, {"a": 1.5}, {"a": "x"}])
        self.assertTrue(node(p, "a").has_type_conflict)

    def test_boolean_beside_integer_is_a_conflict(self):
        p = profile_of([{"a": True}, {"a": 1}])
        self.assertTrue(node(p, "a").has_type_conflict)

    def test_conflicts_carry_line_numbers_for_each_type(self):
        p = profile_of([{"a": 1}, {"a": 2}, {"a": "x"}])
        found = node(p, "a")
        self.assertEqual(found.examples["integer"], [1, 2])
        self.assertEqual(found.examples["string"], [3])


class TestDenominators(unittest.TestCase):
    """Presence is a fraction of something, and it is rarely the record count."""

    def test_top_level_field_is_relative_to_records(self):
        p = profile_of([{"a": 1}, {"a": 2}, {"b": 3}])
        self.assertAlmostEqual(presence(p, "a"), 2 / 3)

    def test_nested_field_is_relative_to_its_parent_not_the_file(self):
        # `inner` appears in one record out of four, but in one of the two
        # records that had an `outer` at all. Quoting 25% would look like a
        # data problem; the honest figure is 50%.
        p = profile_of(
            [
                {"outer": {"inner": 1}},
                {"outer": {}},
                {},
                {},
            ]
        )
        self.assertAlmostEqual(presence(p, "outer"), 0.5)
        self.assertAlmostEqual(presence(p, "outer.inner"), 0.5)

    def test_array_field_is_relative_to_elements(self):
        # Four elements across two records; `gift` is on one of them.
        p = profile_of(
            [
                {"items": [{"sku": "a", "gift": True}, {"sku": "b"}]},
                {"items": [{"sku": "c"}, {"sku": "d"}]},
            ]
        )
        self.assertEqual(node(p, "items").element_total, 4)
        self.assertAlmostEqual(presence(p, "items[].gift"), 0.25)
        self.assertAlmostEqual(presence(p, "items[].sku"), 1.0)

    def test_empty_array_contributes_no_elements(self):
        p = profile_of([{"items": []}, {"items": [{"sku": "a"}]}])
        self.assertEqual(node(p, "items").element_total, 1)
        self.assertAlmostEqual(presence(p, "items[].sku"), 1.0)

    def test_denominator_of_a_parent_that_was_never_an_object_is_zero(self):
        p = profile_of([{"a": 5}])
        self.assertEqual(denominator_for(node(p, "a"), "b"), 0)


class TestAbsentVersusNull(unittest.TestCase):
    """The distinction a plain schema collapses, and everyone trips over."""

    def test_absent_and_null_are_counted_separately(self):
        p = profile_of([{"a": 1}, {"a": None}, {}, {}])
        found = node(p, "a")
        self.assertEqual(found.occurrences, 2)
        self.assertEqual(found.null_count, 1)
        self.assertAlmostEqual(presence(p, "a"), 0.5)

    def test_always_null_still_counts_as_present(self):
        p = profile_of([{"a": None}, {"a": None}])
        self.assertAlmostEqual(presence(p, "a"), 1.0)
        self.assertEqual(node(p, "a").null_count, 2)
        self.assertEqual(node(p, "a").non_null_types, [])


class TestValueSets(unittest.TestCase):
    def test_a_small_set_is_reported_in_full(self):
        p = profile_of([{"s": "a"}, {"s": "b"}, {"s": "a"}])
        found = node(p, "s")
        self.assertTrue(found.is_enum)
        self.assertEqual(found.values, {"a": 2, "b": 1})

    def test_null_is_not_a_value_set_member(self):
        p = profile_of([{"s": "a"}, {"s": None}])
        self.assertEqual(node(p, "s").values, {"a": 1})

    def test_exceeding_the_bound_discards_the_table_rather_than_biasing_it(self):
        # Past the cap the tally can no longer be a truthful top-N: a value
        # that is common but first appears late was never counted. Keeping a
        # partial table would render a plausible, wrong answer.
        records = [{"s": f"v{i}"} for i in range(MAX_DISTINCT_VALUES + 5)]
        found = node(profile_of(records), "s")
        self.assertTrue(found.values_overflowed)
        self.assertEqual(found.values, {})
        self.assertFalse(found.is_enum)

    def test_staying_exactly_at_the_bound_is_still_an_enum(self):
        records = [{"s": f"v{i}"} for i in range(MAX_DISTINCT_VALUES)]
        found = node(profile_of(records), "s")
        self.assertFalse(found.values_overflowed)
        self.assertTrue(found.is_enum)

    def test_a_long_value_overflows_immediately(self):
        # Holding fifty 4 KB blobs per field is how "constant memory" quietly
        # stops being true.
        p = profile_of([{"s": "x" * (MAX_VALUE_LENGTH + 1)}])
        self.assertTrue(node(p, "s").values_overflowed)

    def test_containers_are_never_value_counted(self):
        p = profile_of([{"o": {"k": 1}}, {"a": [1, 2]}])
        self.assertEqual(node(p, "o").values, {})
        self.assertEqual(node(p, "a").values, {})


class TestStatistics(unittest.TestCase):
    def test_numeric_summary(self):
        p = profile_of([{"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}])
        stats = node(p, "n").numeric
        self.assertEqual(stats.count, 4)
        self.assertEqual(stats.minimum, 1)
        self.assertEqual(stats.maximum, 4)
        self.assertAlmostEqual(stats.mean, 2.5)
        self.assertAlmostEqual(stats.stdev, 1.118033988, places=6)

    def test_stdev_of_a_constant_series_is_zero_not_a_domain_error(self):
        # Cancellation in the running sums can push a genuinely zero variance
        # just below zero, and sqrt of that is an exception rather than a
        # rounding difference.
        p = profile_of([{"n": 0.1} for _ in range(500)])
        self.assertEqual(node(p, "n").numeric.stdev, 0.0)

    def test_string_and_array_lengths(self):
        p = profile_of([{"s": "ab", "a": [1]}, {"s": "abcd", "a": [1, 2, 3]}])
        strings = node(p, "s").string_lengths
        arrays = node(p, "a").array_lengths
        self.assertEqual((strings.minimum, strings.maximum), (2, 4))
        self.assertEqual((arrays.minimum, arrays.maximum), (1, 3))
        self.assertAlmostEqual(arrays.mean, 2.0)

    def test_booleans_are_not_added_to_the_numeric_summary(self):
        p = profile_of([{"b": True}, {"b": False}])
        self.assertEqual(node(p, "b").numeric.count, 0)


class TestShapes(unittest.TestCase):
    def test_modal_shape_is_the_most_common_one(self):
        p = profile_of([{"a": 1}, {"a": 2}, {"a": 3}, {"a": 1, "b": 2}])
        modal = p.modal_shape
        self.assertEqual(modal.count, 3)
        self.assertEqual(modal.paths, frozenset({"a"}))

    def test_outliers_are_ranked_by_distance_then_rarity(self):
        records = [{"a": 1, "b": 2} for _ in range(10)]
        records.append({"a": 1})  # one path missing
        records.append({"z": 9})  # three paths different
        p = profile_of(records)
        ranked = p.outliers()
        self.assertEqual(ranked[0][0].paths, frozenset({"z"}))
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_the_modal_shape_is_not_listed_as_its_own_outlier(self):
        p = profile_of([{"a": 1}, {"a": 2}])
        self.assertEqual(p.outliers(), [])

    def test_shape_examples_point_at_real_lines(self):
        p = profile_of([{"a": 1}, {"a": 1}, {"b": 1}])
        odd = p.outliers()[0][0]
        self.assertEqual(odd.examples, [3])

    def test_exceeding_the_shape_bound_is_recorded_not_hidden(self):
        records = [{f"k{i}": 1} for i in range(MAX_SHAPES + 10)]
        p = profile_of(records)
        self.assertTrue(p.shapes_overflowed)
        self.assertLessEqual(len(p.shapes), MAX_SHAPES)

    def test_arrays_collapse_to_one_path_regardless_of_length(self):
        # Two records differing only in how many items they carry are the
        # same shape; treating them as different would bury the real
        # outliers under noise.
        p = profile_of(
            [
                {"items": [{"sku": "a"}]},
                {"items": [{"sku": "a"}, {"sku": "b"}, {"sku": "c"}]},
            ]
        )
        self.assertEqual(len(p.shapes), 1)


class TestNonObjects(unittest.TestCase):
    def test_non_object_records_are_counted_and_kept_out_of_the_tree(self):
        p = profile_of([{"a": 1}, [1, 2], "text", 5])
        self.assertEqual(p.records, 4)
        self.assertEqual(p.non_object_records, 3)
        self.assertEqual(sorted(p.root.children), ["a"])

    def test_the_root_records_what_the_top_level_types_were(self):
        p = profile_of([{"a": 1}, [1, 2]])
        self.assertEqual(p.root.type_counts["object"], 1)
        self.assertEqual(p.root.type_counts["array"], 1)


class TestSerialisation(unittest.TestCase):
    def setUp(self):
        self.profile = profile_of(
            [
                {"a": 1, "n": {"x": "p"}, "items": [{"sku": "s"}]},
                {"a": "1", "items": [{"sku": "t"}, {"sku": "s"}]},
                {"a": None},
            ]
        )

    def test_round_trip_preserves_the_tree(self):
        restored = Profile.from_dict(json.loads(json.dumps(self.profile.as_dict())))
        self.assertEqual(restored.records, self.profile.records)
        self.assertEqual(
            [n.path for n in restored.root.walk()],
            [n.path for n in self.profile.root.walk()],
        )

    def test_round_trip_preserves_conflicts_and_examples(self):
        restored = Profile.from_dict(self.profile.as_dict())
        self.assertEqual(
            [n.path for n in restored.conflicts()],
            [n.path for n in self.profile.conflicts()],
        )
        self.assertEqual(node(restored, "a").examples, node(self.profile, "a").examples)

    def test_round_trip_preserves_denominators(self):
        restored = Profile.from_dict(self.profile.as_dict())
        self.assertAlmostEqual(presence(restored, "items[].sku"), 1.0)
        self.assertEqual(node(restored, "items").element_total, 3)

    def test_round_trip_preserves_shapes(self):
        restored = Profile.from_dict(self.profile.as_dict())
        self.assertEqual(restored.modal_shape.paths, self.profile.modal_shape.paths)
        self.assertEqual(len(restored.shapes), len(self.profile.shapes))

    def test_serialised_form_is_json(self):
        text = json.dumps(self.profile.as_dict(), sort_keys=True)
        self.assertIn('"schema": "jsonxray/profile-v1"', text)


if __name__ == "__main__":
    unittest.main()
