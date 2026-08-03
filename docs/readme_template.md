# jsonxray

**See what is actually inside a JSON Lines file — including the parts that
don't match the rest.**

[![CI](https://github.com/CAOShurong/jsonxray/actions/workflows/ci.yml/badge.svg)](https://github.com/CAOShurong/jsonxray/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/jsonxray.svg)](https://pypi.org/project/jsonxray/)
[![Python](https://img.shields.io/pypi/pyversions/jsonxray.svg)](https://pypi.org/project/jsonxray/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Someone hands you a 4 GB `.jsonl`. You run `head -1 | jq`, write a loader
against what you see, and forty minutes into the job it dies on line 91,204,
where one field arrived as a string instead of a number.

`jsonxray` reads the whole file once, in constant memory, and tells you that
up front — with the line numbers.

<!--SHOT_FIELDS-->

```bash
pip install jsonxray
jsonxray data.jsonl
```

No dependencies. Python 3.9+.

## What this tells you that a schema doesn't

A JSON Schema says what shapes are legal. That is a different question from
what your file actually contains, and three gaps between them account for most
of the time people lose:

**Absent is not null.** `{"discount": null}` and a record with no `discount`
key are different, and code that handles one usually mishandles the other. A
schema renders both as optional. `jsonxray` counts them separately: the bar is
how often the key was there at all, and `58 null` is how many of those times
it was explicitly null.

**A percentage needs a denominator.** A field inside an optional object is not
missing from 60% of your records — it is missing from 60% of the records that
had the parent object. A field inside an array is present in some fraction of
*elements*. Quoting either against the record count invents a data quality
problem that isn't there. Every row in the tree is a fraction of its own
parent.

**Averages hide the file's real problem.** The record you need to see is the
one that doesn't look like the others, and it is by definition rare enough to
be invisible in any summary statistic. So the report ranks record shapes by
how far they are from the norm, and hands you line numbers.

<!--TEXT_CONFLICTS-->

Two records out of four hundred are missing `items` entirely. They are ranked
first, ahead of the seventeen-record group, because being *structurally*
unusual matters more than being uncommon.

## Enums you didn't know you had

Fields whose values form a small closed set are worth knowing about — they are
the ones that become a database enum, a validation rule, or a bug when a new
value turns up.

<!--TEXT_ENUMS-->

Once a field exceeds fifty distinct values, the table is **discarded rather
than truncated**. Past that point the tally is no longer a truthful top-N — a
value that is common but first appears late was never counted — and a
plausible wrong answer is worse than no answer.

## Catching drift in CI

Running this once tells you what is in a file. Running it in a pipeline tells
you when that stopped being true.

```bash
# Once, when you are happy with the data
jsonxray exports/monday.jsonl --save baseline.json

# Every night after that
jsonxray exports/today.jsonl --compare baseline.json
```

<!--SHOT_DRIFT-->

Exit code `2` means a breaking change: a field disappeared, a new type
appeared, something that was always present became optional, or something that
was never null now is. Exit `1` is reserved for the tool failing, so a
pipeline can tell "the data changed" from "the check is broken" and page
someone for only one of them.

Additive changes — a new field, a new enum value, a type that stopped
appearing — are printed as notes and exit `0`. A check that fires on ordinary
variation gets switched off within a week, at which point it catches nothing.

## Working on files that don't fit in memory

One pass, and every statistic is either O(1) per record or explicitly bounded.
Nothing is retained but the record in hand. Profiling a 77 MB, 300,000-record
file peaks at **0.2 MB** of Python heap (measured with `tracemalloc`); CI
enforces the property directly by profiling a 110 MB file under a hard
`ulimit -v`, which a reader that accumulated records could not survive.

That includes the case that usually defeats this: a **single top-level JSON
array**, pretty-printed across a million lines, which is what most "export to
JSON" buttons produce. `json.load` on one of those is exactly the
out-of-memory failure people hit. `jsonxray` detects it and decodes it
incrementally.

```bash
jsonxray dump.json          # detected automatically
jsonxray dump.json --format array
```

Where a bound bites, the report says so rather than quietly becoming
approximate — a truncated `--limit` run states that its percentages describe
only the records it read.

## Options

| Flag | What it does |
|---|---|
| `--limit N` | Stop after N records; the report says it was truncated |
| `--format jsonl\|array\|auto` | Input shape (default: detected) |
| `--only SECTION` | Print one section; repeatable |
| `--depth N` | How deep to print the field tree |
| `--json` | The whole profile as JSON |
| `--save FILE` / `--compare FILE` | Drift detection |
| `--ascii` | No block characters, for issue trackers that mangle them |
| `--color` | `auto`, `truecolor`, `256`, `16`, `none`. `NO_COLOR` always wins |

Reads stdin when given `-`, or when nothing is piped in:

```bash
zcat events.jsonl.gz | jsonxray -
```

## As a library

```python
from jsonxray import Profile, scan

profile = Profile(source="events.jsonl")
with open("events.jsonl", encoding="utf-8") as handle:
    scan(handle, profile)

for node in profile.conflicts():
    print(node.path, node.non_null_types, node.examples)
```

## Development

```bash
git clone https://github.com/CAOShurong/jsonxray
cd jsonxray
python -m unittest discover -s tests
```

The example file is generated from a fixed seed, so the numbers in this README
are reproducible:

```bash
python docs/make_example.py
python docs/build_docs.py          # regenerate the README and its images
python docs/build_docs.py --check  # what CI runs
```

CI runs the suite on Ubuntu, Windows, and macOS across Python 3.9–3.13, and
fails if this README no longer matches what the tool prints.

## License

MIT. See [LICENSE](LICENSE).
