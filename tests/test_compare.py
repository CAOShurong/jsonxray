"""Drift tests.

The design constraint for this module is false positives. A check that fires
on ordinary day-to-day variation gets switched off within a week, at which
point it catches nothing at all. So the tests here are as much about what must
*not* be reported as breaking as about what must.
"""

from __future__ import annotations

import io
import json
import unittest

from jsonxray.compare import PRESENCE_THRESHOLD, compare
from jsonxray.profile import MAX_DISTINCT_VALUES, Profile
from jsonxray.scan import scan


def profile_of(records: list, source: str = "test") -> Profile:
    text = "\n".join(json.dumps(r) for r in records) + "\n"
    return scan(io.StringIO(text), Profile(source=source), fmt="jsonl")


def changes(before: list, after: list) -> dict[tuple[str, str], str]:
    result = compare(profile_of(before), profile_of(after))
    return {(c.path, c.kind): c.detail for c in result.changes}


def kinds(before: list, after: list) -> set[tuple[str, str]]:
    return set(changes(before, after))


class TestStructure(unittest.TestCase):
    def test_a_removed_field_is_breaking(self):
        result = compare(profile_of([{"a": 1, "b": 2}]), profile_of([{"a": 1}]))
        self.assertEqual([c.path for c in result.breaking], ["b"])
        self.assertFalse(result.ok)

    def test_a_new_field_is_reported_but_not_breaking(self):
        # Nothing that worked against the baseline stops working because a
        # field appeared. It is news, not a failure.
        result = compare(profile_of([{"a": 1}]), profile_of([{"a": 1, "b": 2}]))
        self.assertEqual(result.breaking, [])
        self.assertIn(("b", "field-added"), kinds([{"a": 1}], [{"a": 1, "b": 2}]))
        self.assertTrue(result.ok)

    def test_nested_fields_are_compared(self):
        self.assertIn(
            ("o.b", "field-removed"),
            kinds([{"o": {"a": 1, "b": 2}}], [{"o": {"a": 1}}]),
        )

    def test_fields_inside_arrays_are_compared(self):
        self.assertIn(
            ("items[].sku", "field-removed"),
            kinds([{"items": [{"sku": "a"}]}], [{"items": [{}]}]),
        )

    def test_an_identical_file_produces_no_changes(self):
        records = [{"a": 1, "o": {"b": "x"}, "items": [{"sku": "s"}]}]
        self.assertEqual(compare(profile_of(records), profile_of(records)).changes, [])


class TestTypes(unittest.TestCase):
    def test_a_new_type_is_breaking(self):
        result = compare(profile_of([{"a": 1}]), profile_of([{"a": "1"}]))
        self.assertIn("type-added", [c.kind for c in result.breaking])

    def test_a_new_type_carries_the_lines_where_it_appears(self):
        detail = changes([{"a": 1}], [{"a": 1}, {"a": "x"}])[("a", "type-added")]
        self.assertIn("line 2", detail)

    def test_a_type_no_longer_appearing_is_not_breaking(self):
        # The producer got stricter. Nothing downstream breaks from being
        # handed fewer shapes than it already handles.
        result = compare(profile_of([{"a": 1}, {"a": "x"}]), profile_of([{"a": 1}]))
        self.assertEqual(result.breaking, [])
        self.assertIn(("a", "type-removed"), kinds([{"a": 1}, {"a": "x"}], [{"a": 1}]))

    def test_becoming_nullable_is_breaking(self):
        result = compare(profile_of([{"a": 1}]), profile_of([{"a": None}, {"a": 1}]))
        self.assertIn("nullable", [c.kind for c in result.breaking])

    def test_already_nullable_is_not_reported_again(self):
        self.assertNotIn(
            ("a", "nullable"),
            kinds([{"a": None}, {"a": 1}], [{"a": None}, {"a": 2}]),
        )

    def test_an_integer_becoming_a_float_is_reported(self):
        # Not a conflict for the report, but still a change worth naming: a
        # typed destination will reject it.
        self.assertIn(("a", "type-added"), kinds([{"a": 1}], [{"a": 1.5}]))


class TestPresence(unittest.TestCase):
    def test_a_small_move_is_ignored(self):
        # Sample noise between two days of an export is routinely a fraction
        # of a percent. Reporting it trains people to ignore the output.
        before = [{"a": 1}] * 99 + [{}]
        after = [{"a": 1}] * 98 + [{}, {}]
        self.assertNotIn(("a", "presence"), kinds(before, after))

    def test_a_large_move_is_reported(self):
        before = [{"a": 1}] * 100
        after = [{"a": 1}] * 50 + [{}] * 50
        self.assertIn(("a", "presence"), kinds(before, after))

    def test_going_from_always_present_to_sometimes_missing_is_breaking(self):
        # This is the one that breaks a consumer which never checked.
        result = compare(
            profile_of([{"a": 1}] * 10),
            profile_of([{"a": 1}] * 5 + [{}] * 5),
        )
        self.assertEqual([c.kind for c in result.breaking], ["presence"])

    def test_becoming_more_common_is_not_breaking(self):
        result = compare(
            profile_of([{"a": 1}] * 5 + [{}] * 5),
            profile_of([{"a": 1}] * 10),
        )
        self.assertEqual(result.breaking, [])

    def test_the_threshold_is_the_documented_one(self):
        total = 100
        moved = int(total * (PRESENCE_THRESHOLD + 0.01))
        before = [{"a": 1}] * 90 + [{}] * 10
        after = [{"a": 1}] * (90 - moved) + [{}] * (10 + moved)
        self.assertIn(("a", "presence"), kinds(before, after))

    def test_nested_presence_uses_the_nested_denominator(self):
        # `inner` is on half the `outer` objects in both, even though the
        # number of records with an `outer` at all changed. Comparing against
        # the record count would invent a change that did not happen.
        before = [{"outer": {"inner": 1}}, {"outer": {}}]
        after = [{"outer": {"inner": 1}}, {"outer": {}}, {}, {}]
        self.assertNotIn(("outer.inner", "presence"), kinds(before, after))


class TestValueSets(unittest.TestCase):
    def test_a_new_value_is_reported(self):
        detail = changes([{"s": "a"}, {"s": "b"}], [{"s": "a"}, {"s": "c"}])
        self.assertIn("c", detail[("s", "values-added")])

    def test_a_new_value_is_not_breaking(self):
        result = compare(profile_of([{"s": "a"}]), profile_of([{"s": "b"}]))
        self.assertEqual(result.breaking, [])

    def test_high_cardinality_fields_are_not_differenced(self):
        # Once either side overflowed its bound its value table is not a
        # complete set, so differencing the two would invent changes on every
        # single run.
        before = [{"s": f"v{i}"} for i in range(MAX_DISTINCT_VALUES + 5)]
        after = [{"s": f"w{i}"} for i in range(MAX_DISTINCT_VALUES + 5)]
        self.assertNotIn(("s", "values-added"), kinds(before, after))


class TestFileLevel(unittest.TestCase):
    def test_more_unparseable_lines_is_breaking(self):
        before = scan(io.StringIO('{"a": 1}\n'), Profile())
        after = scan(io.StringIO('{"a": 1}\nbroken\n'), Profile())
        result = compare(before, after)
        self.assertEqual([c.kind for c in result.breaking], ["malformed"])

    def test_fewer_unparseable_lines_is_not_reported(self):
        before = scan(io.StringIO('{"a": 1}\nbroken\n'), Profile())
        after = scan(io.StringIO('{"a": 1}\n'), Profile())
        self.assertEqual(compare(before, after).changes, [])


class TestSerialisation(unittest.TestCase):
    def test_a_comparison_survives_a_json_round_trip(self):
        result = compare(profile_of([{"a": 1, "b": 2}]), profile_of([{"a": "1"}]))
        raw = json.loads(json.dumps(result.as_dict()))
        self.assertEqual(raw["breaking"], len(result.breaking))
        self.assertEqual(len(raw["changes"]), len(result.changes))
        self.assertEqual(raw["schema"], "jsonxray/comparison-v1")

    def test_comparing_a_saved_profile_matches_comparing_a_live_one(self):
        before = profile_of([{"a": 1, "b": 2}])
        after = profile_of([{"a": "1"}])
        restored = Profile.from_dict(json.loads(json.dumps(before.as_dict())))
        self.assertEqual(
            [c.as_dict() for c in compare(restored, after).changes],
            [c.as_dict() for c in compare(before, after).changes],
        )


if __name__ == "__main__":
    unittest.main()
