# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-08-03

### Fixed

- **Reading from a pipe profiled nothing.** `cat data.jsonl | jsonxray -`
  reported "stdin contained no JSON records" for input that was perfectly
  good. Format sniffing reads a chunk to decide between JSONL and a JSON
  array, then attempted to rewind; on a real pipe `seek(0)` neither raises
  nor rewinds, so the scanner was handed an exhausted stream.

  Nothing about the stream can be asked to detect this. A pipe answers
  `seekable()` with True, accepts `seek(0)` without complaint, and then
  reports `tell() == 0` -- while every subsequent read returns `""`. The
  peeked text is therefore always pushed back in front of the stream now
  rather than rewound, which is correct on every input.

  The test suite had covered this with a stream that refuses to seek, which
  is the honest behaviour and not the one real stdin has, so the suite passed
  while the feature was broken. It now also tests against a stream that
  claims to seek and does not.

## [0.1.0] - 2026-08-03

First release.

### Added

- Streaming profile of a JSON Lines file in constant memory: field tree with
  presence, type distribution, numeric and length statistics.
- Line numbers for every reported inconsistency, so a finding comes with
  somewhere to look.
- Absent and null counted separately, rather than collapsed into "optional".
- Presence quoted against the correct denominator: a nested field against its
  parent, a field inside an array against elements.
- Record shape ranking, surfacing the records furthest from the norm rather
  than merely the rarest.
- Bounded value tallies for enum detection, discarded rather than truncated
  once the bound is exceeded.
- Incremental decoding of a single top-level JSON array, for the
  pretty-printed exports that defeat `json.load`.
- `--save` and `--compare` for schema drift, with exit code 2 reserved for a
  breaking change and 1 for the tool failing.
- `--json`, `--limit`, `--only`, `--depth`, `--ascii`, and colour depth
  degradation with `NO_COLOR` support.

[0.1.0]: https://github.com/CAOShurong/jsonxray/releases/tag/v0.1.0
[0.1.1]: https://github.com/CAOShurong/jsonxray/releases/tag/v0.1.1
