#!/usr/bin/env python3
"""Generate the example file used by the README and the tests.

Deterministic: a fixed seed, so the numbers in the README are reproducible and
CI can verify the documentation still matches the tool. The defects in it are
placed on purpose, because a demo file where nothing is wrong demonstrates
nothing:

* ``user.plan`` is a small closed set -- the enum case
* ``price`` is an integer for most records and a string for a few -- the
  single most common real defect, an upstream serialiser changing
* ``discount`` is usually absent, sometimes null, sometimes a number -- the
  absent-versus-null distinction
* ``shipping.country`` exists only on some records, inside an optional object,
  so its presence figure is only correct against the right denominator
* two records are missing ``items`` entirely -- the shape outliers
* one line is not valid JSON at all
"""

from __future__ import annotations

import json
import pathlib
import random

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "example.jsonl"
#: The same export a week later, with the changes an upstream team makes
#: without telling anyone. Used to demonstrate --compare, and committed so the
#: README figures are reproducible on any machine.
DRIFTED = HERE / "example-next-week.jsonl"

PLANS = ["free", "free", "free", "pro", "pro", "enterprise"]
COUNTRIES = ["US", "GB", "DE", "JP", "BR"]
SKUS = ["SKU-100", "SKU-220", "SKU-347", "SKU-512", "SKU-909"]
RECORDS = 400


def build() -> list[str]:
    rng = random.Random(20260803)
    lines: list[str] = []

    for index in range(RECORDS):
        record: dict = {
            "id": f"ord_{index:05d}",
            "created_at": f"2026-07-{(index % 28) + 1:02d}T09:00:00Z",
            "user": {
                "id": rng.randrange(1000, 9999),
                "plan": rng.choice(PLANS),
                "verified": rng.random() > 0.2,
            },
            "total": round(rng.uniform(5, 400), 2),
        }

        # An upstream serialiser started quoting the field. Rare enough to
        # survive a spot check of the first few lines, common enough to break
        # a nightly job.
        if index in (57, 198, 331):
            record["price"] = f"{rng.randrange(5, 400)}.00"
        else:
            record["price"] = rng.randrange(5, 400)

        # Absent, explicitly null, and present -- three different things that
        # a schema alone renders as one.
        if index % 7 == 0:
            record["discount"] = None
        elif index % 3 == 0:
            record["discount"] = round(rng.uniform(0.05, 0.4), 2)

        if index % 4 != 0:
            record["shipping"] = {
                "country": rng.choice(COUNTRIES),
                "express": rng.random() > 0.7,
            }
            # Deliberately not a multiple of 4: the branch above already
            # excludes those, so a multiple-of-20 condition here would make
            # this key unreachable and quietly drop the rare-nested-field case
            # from the example.
            if index % 22 == 0:
                record["shipping"]["note"] = "leave with neighbour"

        if index not in (12, 240):
            record["items"] = [
                {
                    "sku": rng.choice(SKUS),
                    "qty": rng.randrange(1, 5),
                    **({"gift": True} if rng.random() > 0.85 else {}),
                }
                for _ in range(rng.randrange(1, 4))
            ]

        lines.append(json.dumps(record, sort_keys=True))

    lines.insert(150, '{"id": "ord_bad", "total": 12.0,')
    return lines


def drift(lines: list[str]) -> list[str]:
    """The same export after a week of upstream changes nobody announced."""
    out: list[str] = []
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue  # the deliberately broken line; upstream fixed it
        record.pop("created_at", None)  # a field quietly disappears
        record["total"] = str(record["total"])  # a number becomes a string
        record["region"] = "eu"  # a new field appears
        if index % 5 == 0:
            record["user"]["plan"] = "trial"  # a new value in a closed set
        if index % 9 == 0:
            record["id"] = None  # a required field goes nullable
        out.append(json.dumps(record, sort_keys=True))
    return out


def main() -> int:
    lines = build()
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{OUT.name}: {len(lines)} lines, {OUT.stat().st_size} bytes")

    later = drift(lines)
    DRIFTED.write_text("\n".join(later) + "\n", encoding="utf-8", newline="\n")
    print(f"{DRIFTED.name}: {len(later)} lines, {DRIFTED.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
