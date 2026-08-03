"""End-to-end tests through the command line.

These go through ``main`` with real files, because the things that break in a
CLI are the joins: an exit code that does not distinguish "the data changed"
from "the tool fell over", a flag that silently does nothing, output that
cannot be encoded on the terminal it was printed to.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from jsonxray.cli import EXIT_DRIFT, main

ANSI = re.compile(r"\x1b\[[0-9;]*m")

RECORDS = [
    {"id": "a1", "n": 1, "tag": "x", "o": {"k": 1}},
    {"id": "a2", "n": 2, "tag": "y", "o": {"k": 2}},
    {"id": "a3", "n": "3", "tag": "x"},
]


def run(argv: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    previous = None
    if stdin is not None:
        import sys

        previous = sys.stdin
        sys.stdin = _Stdin(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
    finally:
        if previous is not None:
            import sys

            sys.stdin = previous
    return code, out.getvalue(), err.getvalue()


class _Stdin(io.StringIO):
    def isatty(self) -> bool:
        return False


class CliCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.dir = self._temp.name
        self.path = self.write("data.jsonl", RECORDS)

    def write(self, name: str, records: list) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(json.dumps(r) for r in records) + "\n")
        return path

    def temp(self, name: str) -> str:
        return os.path.join(self.dir, name)


class TestReport(CliCase):
    def test_a_plain_run(self):
        code, out, _ = run([self.path, "--color", "none"])
        self.assertEqual(code, 0)
        self.assertIn("3 records", out)
        self.assertIn("id", out)

    def test_the_conflict_is_reported_with_its_line(self):
        _, out, _ = run([self.path, "--color", "none"])
        self.assertIn("Type conflicts", out)
        self.assertIn("line 3", out)

    def test_colour_off_leaves_no_escapes(self):
        _, out, _ = run([self.path, "--color", "none"])
        self.assertEqual(out, ANSI.sub("", out))

    def test_ascii_output_is_encodable(self):
        _, out, _ = run([self.path, "--ascii", "--color", "none"])
        out.encode("ascii", errors="strict")

    def test_only_prints_just_that_section(self):
        _, out, _ = run([self.path, "--color", "none", "--only", "conflicts"])
        self.assertIn("Type conflicts", out)
        self.assertNotIn("Fields", out)

    def test_only_can_be_repeated(self):
        _, out, _ = run(
            [self.path, "--color", "none", "--only", "summary", "--only", "tree"]
        )
        self.assertIn("3 records", out)
        self.assertIn("Fields", out)
        self.assertNotIn("Record shapes", out)

    def test_depth_limits_the_tree(self):
        _, deep, _ = run([self.path, "--color", "none", "--only", "tree"])
        _, shallow, _ = run(
            [self.path, "--color", "none", "--only", "tree", "--depth", "0"]
        )
        self.assertIn("k", deep)
        self.assertLess(len(shallow.splitlines()), len(deep.splitlines()))

    def test_limit_truncates_and_says_so(self):
        _, out, _ = run([self.path, "--color", "none", "--limit", "2"])
        self.assertIn("stopped after 2 records", out)

    def test_reading_from_stdin(self):
        text = "\n".join(json.dumps(r) for r in RECORDS) + "\n"
        code, out, _ = run(["-", "--color", "none"], stdin=text)
        self.assertEqual(code, 0)
        self.assertIn("3 records", out)

    def test_no_path_with_piped_input_reads_the_pipe(self):
        text = '{"a": 1}\n'
        code, out, _ = run(["--color", "none"], stdin=text)
        self.assertEqual(code, 0)
        self.assertIn("1 record", out)


class TestJson(CliCase):
    def test_json_output_is_valid_and_round_trips(self):
        code, out, _ = run([self.path, "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["records"], 3)
        self.assertEqual(data["schema"], "jsonxray/profile-v1")

    def test_json_output_carries_the_conflict(self):
        _, out, _ = run([self.path, "--json"])
        node = json.loads(out)["root"]["children"]["n"]
        self.assertEqual(sorted(node["type_counts"]), ["integer", "string"])

    def test_save_writes_a_profile_that_can_be_reloaded(self):
        target = self.temp("base.json")
        code, _, _ = run([self.path, "--save", target, "--color", "none"])
        self.assertEqual(code, 0)
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["records"], 3)


class TestCompare(CliCase):
    def setUp(self):
        super().setUp()
        self.baseline = self.temp("base.json")
        run([self.path, "--save", self.baseline, "--color", "none"])

    def test_an_unchanged_file_exits_zero(self):
        code, out, _ = run([self.path, "--compare", self.baseline, "--color", "none"])
        self.assertEqual(code, 0)
        self.assertIn("no change", out)

    def test_a_breaking_change_uses_its_own_exit_code(self):
        # Distinct from 1 so a pipeline can tell "the data changed" from "the
        # tool fell over" and page someone for only one of them.
        changed = self.write("changed.jsonl", [{"id": "a1"}])
        code, out, _ = run([changed, "--compare", self.baseline, "--color", "none"])
        self.assertEqual(code, EXIT_DRIFT)
        self.assertIn("BREAK", out)
        self.assertIn("breaking change", out)

    def test_an_additive_change_exits_zero(self):
        added = [dict(r, extra=1) for r in RECORDS]
        changed = self.write("added.jsonl", added)
        code, out, _ = run([changed, "--compare", self.baseline, "--color", "none"])
        self.assertEqual(code, 0)
        self.assertIn("extra", out)

    def test_compare_as_json(self):
        changed = self.write("changed.jsonl", [{"id": "a1"}])
        code, out, _ = run([changed, "--compare", self.baseline, "--json"])
        self.assertEqual(code, EXIT_DRIFT)
        data = json.loads(out)
        self.assertGreater(data["breaking"], 0)

    def test_a_missing_baseline_is_a_tool_error_not_drift(self):
        code, _, err = run(
            [self.path, "--compare", self.temp("nope.json"), "--color", "none"]
        )
        self.assertEqual(code, 1)
        self.assertIn("jsonxray:", err)

    def test_a_baseline_that_is_not_a_profile_is_a_tool_error(self):
        broken = self.temp("broken.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("not json")
        code, _, err = run([self.path, "--compare", broken, "--color", "none"])
        self.assertEqual(code, 1)
        self.assertIn("jsonxray:", err)


class TestFailures(CliCase):
    def test_a_missing_file_exits_one(self):
        code, _, err = run([self.temp("nope.jsonl")])
        self.assertEqual(code, 1)
        self.assertIn("no such file", err)

    def test_an_empty_file_is_reported_rather_than_shown_as_zeroes(self):
        empty = self.temp("empty.jsonl")
        open(empty, "w", encoding="utf-8").close()
        code, _, err = run([empty, "--color", "none"])
        self.assertEqual(code, 1)
        self.assertIn("no JSON records", err)

    def test_a_file_of_only_broken_lines_still_reports(self):
        # Nothing parsed, but "every line is broken" is the answer, and it is
        # worth more than an error message.
        path = self.temp("broken.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("nope\nnope\n")
        code, out, _ = run([path, "--color", "none"])
        self.assertEqual(code, 0)
        self.assertIn("2 unparseable lines", out)

    def test_an_array_file_is_detected_without_a_flag(self):
        path = self.temp("array.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(RECORDS, handle, indent=2)
        code, out, _ = run([path, "--color", "none"])
        self.assertEqual(code, 0)
        self.assertIn("3 records", out)

    def test_forcing_the_wrong_format_does_not_crash(self):
        path = self.temp("array.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(RECORDS, handle, indent=2)
        code, out, _ = run([path, "--format", "jsonl", "--color", "none"])
        self.assertEqual(code, 0)
        self.assertIn("unparseable", out)


if __name__ == "__main__":
    unittest.main()
