"""Reader tests.

Two things matter here and nothing else does much: that a record is never
silently lost, and that a line number always points at the line the reader
actually saw. Everything downstream is arithmetic on what this module yields.
"""

from __future__ import annotations

import io
import json
import unittest

from jsonxray.profile import Profile
from jsonxray.scan import CHUNK, ScanError, detect_format, scan


def run(text: str, **kwargs) -> Profile:
    return scan(io.StringIO(text), Profile(source="test"), **kwargs)


class Pipe(io.TextIOBase):
    """A stream that cannot seek, which is what stdin is.

    Reading through a real pipe in a test is slow and racy; refusing to seek
    is the only property that matters, and this reproduces it exactly.
    """

    def __init__(self, text: str) -> None:
        self._inner = io.StringIO(text)

    def read(self, size: int = -1) -> str:
        return self._inner.read(size)

    def readline(self, size: int = -1) -> str:  # type: ignore[override]
        return self._inner.readline(size)

    def seek(self, *args, **kwargs):  # type: ignore[override]
        raise io.UnsupportedOperation("not seekable")

    def seekable(self) -> bool:
        return False

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line


class TestLines(unittest.TestCase):
    def test_one_record_per_line(self):
        p = run('{"a": 1}\n{"a": 2}\n')
        self.assertEqual(p.records, 2)

    def test_blank_lines_are_skipped_and_counted(self):
        p = run('{"a": 1}\n\n   \n{"a": 2}\n')
        self.assertEqual(p.records, 2)
        self.assertEqual(p.blank_lines, 2)

    def test_a_missing_trailing_newline_still_yields_the_last_record(self):
        p = run('{"a": 1}\n{"a": 2}')
        self.assertEqual(p.records, 2)

    def test_bytes_read_covers_every_line(self):
        text = '{"a": 1}\n{"a": 2}\n'
        self.assertEqual(run(text).bytes_read, len(text))


class TestMalformed(unittest.TestCase):
    def test_a_bad_line_does_not_stop_the_scan(self):
        # A 40 GB export with nine broken lines is still worth profiling, and
        # finding out which nine is usually the whole reason for running this.
        p = run('{"a": 1}\nnot json\n{"a": 2}\n')
        self.assertEqual(p.records, 2)
        self.assertEqual(p.malformed_count, 1)

    def test_the_reported_line_number_is_the_real_one(self):
        p = run('{"a": 1}\n\n{"a": 2}\nbroken\n')
        self.assertEqual(p.malformed[0].lineno, 4)

    def test_only_a_few_examples_are_kept_but_all_are_counted(self):
        p = run("bad\n" * 50)
        self.assertEqual(p.malformed_count, 50)
        self.assertLessEqual(len(p.malformed), 3)

    def test_a_truncated_line_is_malformed_not_fatal(self):
        p = run('{"a": 1}\n{"a": 2,\n')
        self.assertEqual(p.records, 1)
        self.assertEqual(p.malformed_count, 1)


class TestFormatDetection(unittest.TestCase):
    def test_an_object_per_line_is_jsonl(self):
        self.assertEqual(detect_format('{"a": 1}\n{"a": 2}\n'), "jsonl")

    def test_a_bracket_then_a_newline_is_an_array(self):
        self.assertEqual(detect_format('[\n  {"a": 1}\n]\n'), "array")

    def test_lines_that_are_themselves_arrays_are_jsonl(self):
        # The ambiguous case: a JSON Lines file whose records happen to be
        # arrays also starts with '['. Reading it as one enormous malformed
        # array would lose the entire file.
        self.assertEqual(detect_format("[1, 2]\n[3, 4]\n"), "jsonl")

    def test_leading_whitespace_does_not_confuse_it(self):
        self.assertEqual(detect_format('\n\n  [\n {"a": 1}]'), "array")

    def test_empty_input_is_jsonl(self):
        self.assertEqual(detect_format(""), "jsonl")

    def test_detection_is_applied_automatically(self):
        p = run('[\n  {"a": 1},\n  {"a": 2}\n]\n')
        self.assertEqual(p.records, 2)


class TestArrayStreaming(unittest.TestCase):
    def test_a_pretty_printed_array(self):
        p = run('[\n  {"a": 1},\n  {"a": 2}\n]\n', fmt="array")
        self.assertEqual(p.records, 2)

    def test_a_compact_array(self):
        p = run('[{"a":1},{"a":2},{"a":3}]', fmt="array")
        self.assertEqual(p.records, 3)

    def test_an_empty_array(self):
        p = run("[]", fmt="array")
        self.assertEqual(p.records, 0)

    def test_line_numbers_track_through_the_array(self):
        text = '[\n{"a": 1},\n{"a": 2},\n{"a": "x"}\n]\n'
        p = run(text, fmt="array")
        found = next(n for n in p.root.walk() if n.path == "a")
        self.assertEqual(found.examples["string"], [4])

    def test_records_spanning_a_chunk_boundary_are_not_lost(self):
        # raw_decode fails identically on a broken element and on one merely
        # split across a read, so the reader has to keep going until the input
        # is genuinely exhausted. Getting this wrong drops records silently,
        # which is the worst failure this tool could have.
        filler = "x" * 900
        records = [{"i": i, "pad": filler} for i in range(200)]
        text = json.dumps(records)
        self.assertGreater(len(text), CHUNK * 2)
        p = run(text, fmt="array")
        self.assertEqual(p.records, 200)
        self.assertEqual(p.root.children["i"].numeric.maximum, 199)

    def test_a_single_record_larger_than_a_chunk(self):
        record = {"blob": "y" * (CHUNK * 2)}
        p = run(json.dumps([record]), fmt="array")
        self.assertEqual(p.records, 1)

    def test_an_unterminated_array_reports_rather_than_hangs(self):
        p = run('[{"a": 1}, {"a": 2}', fmt="array")
        self.assertEqual(p.records, 2)

    def test_a_broken_element_is_reported(self):
        p = run('[{"a": 1}, {oops}]', fmt="array")
        self.assertEqual(p.records, 1)
        self.assertEqual(p.malformed_count, 1)

    def test_input_that_is_not_an_array_at_all_is_an_error(self):
        with self.assertRaises(ScanError):
            run('{"a": 1}', fmt="array")

    def test_an_unknown_format_is_an_error(self):
        with self.assertRaises(ScanError):
            run("{}", fmt="parquet")


class TestNonSeekableInput(unittest.TestCase):
    """stdin cannot be rewound, and the peeked bytes must survive that."""

    def test_a_pipe_shorter_than_the_sniff_buffer(self):
        p = scan(Pipe('{"a": 1}\n{"a": 2}\n'), Profile())
        self.assertEqual(p.records, 2)

    def test_a_pipe_longer_than_the_sniff_buffer_loses_nothing(self):
        # The regression this exists for: sniffing reads a chunk to decide the
        # format, and an implementation that discards the wrapper it built to
        # push those bytes back silently drops the first 64 KB.
        records = [{"i": i} for i in range(20_000)]
        text = "\n".join(json.dumps(r) for r in records) + "\n"
        self.assertGreater(len(text), CHUNK)
        p = scan(Pipe(text), Profile())
        self.assertEqual(p.records, 20_000)
        self.assertEqual(p.root.children["i"].numeric.minimum, 0)

    def test_a_piped_array_is_detected_and_read(self):
        records = [{"i": i, "pad": "z" * 40} for i in range(3_000)]
        p = scan(Pipe(json.dumps(records)), Profile())
        self.assertEqual(p.records, 3_000)

    def test_an_empty_pipe(self):
        p = scan(Pipe(""), Profile())
        self.assertEqual(p.records, 0)


class TestLimit(unittest.TestCase):
    def test_the_limit_stops_the_scan(self):
        p = run("\n".join('{"a": 1}' for _ in range(100)), limit=10)
        self.assertEqual(p.records, 10)

    def test_truncation_is_recorded_so_the_report_can_say_so(self):
        # A percentage from the first thousand records of a sorted file is not
        # a percentage of the file, and silence about that is a lie.
        p = run("\n".join('{"a": 1}' for _ in range(100)), limit=10)
        self.assertEqual(p.truncated_at, 10)

    def test_a_limit_larger_than_the_file_is_not_truncation(self):
        p = run('{"a": 1}\n', limit=500)
        self.assertIsNone(p.truncated_at)


if __name__ == "__main__":
    unittest.main()
