# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
