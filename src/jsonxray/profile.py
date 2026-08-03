"""The accumulating picture of a file's structure.

Everything here is designed around one constraint: the file does not fit in
memory, and may not even fit on disk twice. So there is exactly one pass, and
every statistic is either O(1) per record or explicitly bounded.

That constraint is what makes the honest-reporting problem interesting. An
accumulator that silently stops being exact is worse than one that never
claimed to be, so where a bound bites -- distinct values, distinct record
shapes -- the profile records that it bit, and the renderer says so rather
than showing a number it can no longer stand behind.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

__all__ = [
    "FieldNode",
    "Profile",
    "ShapeGroup",
    "type_name",
]

#: How many example line numbers to remember per (path, type). Enough to go
#: and look, few enough that a pathological file cannot grow them without
#: bound.
EXAMPLES_PER_TYPE = 3

#: Distinct values kept per field before it is declared high-cardinality.
#: A genuine enum -- status, country, log level -- is far below this; an id
#: or a timestamp blows past it on the first few hundred records.
MAX_DISTINCT_VALUES = 50

#: Distinct record shapes kept before the tail is lumped together. Real files
#: have a handful; a file with thousands of shapes has a different problem,
#: and the profile says so instead of trying to enumerate them.
MAX_SHAPES = 512

#: Values longer than this are not kept for the enum table. A 4 KB blob is
#: never an enum member, and holding 50 of them per field is how a "constant
#: memory" tool quietly stops being one.
MAX_VALUE_LENGTH = 200


def denominator_for(parent: FieldNode, key: str) -> int:
    """What a child's presence is a fraction *of*.

    Not the record count. A key inside an optional object is only missing when
    that object was there without it, and a key inside an array is present in
    some fraction of *elements*. Quoting either against the record count
    produces a number that looks like a data quality problem and is really an
    arithmetic one, so the denominator comes from the parent every time.
    """
    return parent.element_total if key == "[]" else parent.object_count


def type_name(value: object) -> str:
    """The type label used throughout the profile.

    ``bool`` is checked before ``int`` because in Python it *is* an ``int``,
    and a file where a field is sometimes ``true`` and sometimes ``1`` is
    exactly the kind of thing this tool exists to surface. Getting the order
    wrong would hide it.

    Integers and floats are reported separately for the same reason: a field
    that is ``0`` for the first ten thousand records and ``0.5`` afterwards
    will load fine into JSON and then fail against any typed destination.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


#: Type pairs that coexist without being a defect. An integer and a float are
#: both numbers; anything else appearing in the same field means the producer
#: is inconsistent, which is worth flagging.
COMPATIBLE_PAIRS = frozenset({frozenset({"integer", "float"})})


@dataclass
class Numeric:
    """Streaming numeric summary.

    Mean and standard deviation come from running sums rather than a stored
    sample. The textbook warning about ``sum(x^2)`` losing precision applies,
    but only past about 2^53 in magnitude; for the field sizes this tool is
    pointed at the simplicity is worth more than the last few digits, and the
    values are only ever displayed rounded.
    """

    count: int = 0
    total: float = 0.0
    total_sq: float = 0.0
    minimum: float | None = None
    maximum: float | None = None

    def add(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.total_sq += value * value
        if self.minimum is None or value < self.minimum:
            self.minimum = value
        if self.maximum is None or value > self.maximum:
            self.maximum = value

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0

    @property
    def stdev(self) -> float:
        if self.count < 2:
            return 0.0
        variance = self.total_sq / self.count - self.mean**2
        # Cancellation in the running sums can push a genuinely zero variance
        # a hair below zero; a negative under the root would be an exception
        # rather than a rounding difference.
        return math.sqrt(max(0.0, variance))

    def as_dict(self) -> dict:
        return {
            "count": self.count,
            "total": self.total,
            "total_sq": self.total_sq,
            "min": self.minimum,
            "max": self.maximum,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Numeric:
        return cls(
            count=int(raw.get("count", 0)),
            total=float(raw.get("total", 0.0)),
            total_sq=float(raw.get("total_sq", 0.0)),
            minimum=raw.get("min"),
            maximum=raw.get("max"),
        )


@dataclass
class Lengths:
    """Streaming length summary, for strings and for arrays."""

    count: int = 0
    total: int = 0
    minimum: int | None = None
    maximum: int | None = None

    def add(self, length: int) -> None:
        self.count += 1
        self.total += length
        if self.minimum is None or length < self.minimum:
            self.minimum = length
        if self.maximum is None or length > self.maximum:
            self.maximum = length

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0

    def as_dict(self) -> dict:
        return {
            "count": self.count,
            "total": self.total,
            "min": self.minimum,
            "max": self.maximum,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Lengths:
        return cls(
            count=int(raw.get("count", 0)),
            total=int(raw.get("total", 0)),
            minimum=raw.get("min"),
            maximum=raw.get("max"),
        )


@dataclass
class FieldNode:
    """One path in the document tree, and everything measured about it.

    Paths use ``parent.child`` for object keys and ``parent[]`` for array
    elements, so ``orders[].items[].sku`` reads the way people describe it out
    loud.
    """

    path: str
    #: Times a value existed here at all, ``null`` included.
    occurrences: int = 0
    type_counts: dict[str, int] = field(default_factory=dict)
    #: Up to :data:`EXAMPLES_PER_TYPE` line numbers per type, so a reported
    #: inconsistency comes with somewhere to go and look.
    examples: dict[str, list[int]] = field(default_factory=dict)

    #: Times the value was an object. The denominator for this node's object
    #: children -- *not* the record count, because a key nested inside an
    #: optional parent is only missing when the parent was there without it.
    object_count: int = 0
    #: Total elements across every array seen here. The denominator for the
    #: ``[]`` child, so "present in 12% of elements" means elements.
    element_total: int = 0

    numeric: Numeric = field(default_factory=Numeric)
    string_lengths: Lengths = field(default_factory=Lengths)
    array_lengths: Lengths = field(default_factory=Lengths)

    #: Bounded value tally, for spotting enums. ``values_overflowed`` records
    #: that the bound was hit, after which this tally is no longer a truthful
    #: top-N and must not be displayed as one.
    values: dict[str, int] = field(default_factory=dict)
    values_overflowed: bool = False

    children: dict[str, FieldNode] = field(default_factory=dict)

    # -- accumulation ------------------------------------------------------

    def observe(self, value: object, lineno: int) -> None:
        kind = type_name(value)
        self.occurrences += 1
        self.type_counts[kind] = self.type_counts.get(kind, 0) + 1
        seen = self.examples.setdefault(kind, [])
        if len(seen) < EXAMPLES_PER_TYPE:
            seen.append(lineno)

        if kind in ("integer", "float"):
            self.numeric.add(float(value))  # type: ignore[arg-type]
        elif kind == "string":
            self.string_lengths.add(len(value))  # type: ignore[arg-type]
        elif kind == "array":
            self.array_lengths.add(len(value))  # type: ignore[arg-type]
            self.element_total += len(value)  # type: ignore[arg-type]
        elif kind == "object":
            self.object_count += 1

        self._tally_value(value, kind)

    def _tally_value(self, value: object, kind: str) -> None:
        """Count a scalar towards the enum table, within the bound.

        ``null`` is left out: it is already reported as a presence figure, and
        counting it here would push every optional field into the enum table
        with one uninteresting member.
        """
        if kind in ("object", "array", "null"):
            return
        if self.values_overflowed:
            return
        key = value if isinstance(value, str) else json.dumps(value)
        if len(key) > MAX_VALUE_LENGTH:
            self.values_overflowed = True
            self.values.clear()
            return
        if key not in self.values and len(self.values) >= MAX_DISTINCT_VALUES:
            # Past the bound the tally cannot be a truthful top-N: a value
            # that is common but first appears late was never counted. Drop
            # it rather than render a plausible, wrong table.
            self.values_overflowed = True
            self.values.clear()
            return
        self.values[key] = self.values.get(key, 0) + 1

    def child(self, key: str, *, array: bool = False) -> FieldNode:
        node = self.children.get(key)
        if node is None:
            if array:
                path = f"{self.path}[]"
            elif self.path:
                path = f"{self.path}.{key}"
            else:
                path = key
            node = FieldNode(path=path)
            self.children[key] = node
        return node

    # -- queries -----------------------------------------------------------

    @property
    def null_count(self) -> int:
        return self.type_counts.get("null", 0)

    @property
    def non_null_types(self) -> list[str]:
        return sorted(k for k in self.type_counts if k != "null")

    @property
    def has_type_conflict(self) -> bool:
        """True when the field holds genuinely incompatible types.

        ``null`` alongside anything is optionality, not a conflict, and is
        reported separately. Integer alongside float is one number type
        rendered two ways.
        """
        kinds = set(self.non_null_types)
        if len(kinds) < 2:
            return False
        return frozenset(kinds) not in COMPATIBLE_PAIRS or len(kinds) > 2

    @property
    def is_enum(self) -> bool:
        """A small, closed set of scalar values -- worth showing in full."""
        return (
            not self.values_overflowed
            and 0 < len(self.values) <= MAX_DISTINCT_VALUES
            and "object" not in self.type_counts
            and "array" not in self.type_counts
        )

    def walk(self):
        yield self
        for _, node in sorted(self.children.items()):
            yield from node.walk()

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "occurrences": self.occurrences,
            "type_counts": dict(self.type_counts),
            "examples": {k: list(v) for k, v in self.examples.items()},
            "object_count": self.object_count,
            "element_total": self.element_total,
            "numeric": self.numeric.as_dict(),
            "string_lengths": self.string_lengths.as_dict(),
            "array_lengths": self.array_lengths.as_dict(),
            "values": dict(self.values),
            "values_overflowed": self.values_overflowed,
            "children": {k: v.as_dict() for k, v in self.children.items()},
        }

    @classmethod
    def from_dict(cls, raw: dict) -> FieldNode:
        node = cls(
            path=str(raw.get("path", "")),
            occurrences=int(raw.get("occurrences", 0)),
            type_counts=dict(raw.get("type_counts", {})),
            examples={k: list(v) for k, v in raw.get("examples", {}).items()},
            object_count=int(raw.get("object_count", 0)),
            element_total=int(raw.get("element_total", 0)),
            numeric=Numeric.from_dict(raw.get("numeric", {})),
            string_lengths=Lengths.from_dict(raw.get("string_lengths", {})),
            array_lengths=Lengths.from_dict(raw.get("array_lengths", {})),
            values=dict(raw.get("values", {})),
            values_overflowed=bool(raw.get("values_overflowed", False)),
        )
        node.children = {
            k: cls.from_dict(v) for k, v in raw.get("children", {}).items()
        }
        return node


@dataclass
class ShapeGroup:
    """A distinct set of paths, and where records having it were seen."""

    paths: frozenset[str]
    count: int = 0
    examples: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "paths": sorted(self.paths),
            "count": self.count,
            "examples": list(self.examples),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> ShapeGroup:
        return cls(
            paths=frozenset(raw.get("paths", ())),
            count=int(raw.get("count", 0)),
            examples=list(raw.get("examples", ())),
        )


@dataclass
class Malformed:
    lineno: int
    message: str

    def as_dict(self) -> dict:
        return {"lineno": self.lineno, "message": self.message}


class Profile:
    """Everything learned about one file, in a single pass."""

    def __init__(self, source: str = "-") -> None:
        self.source = source
        self.root = FieldNode(path="")
        self.records = 0
        self.blank_lines = 0
        self.bytes_read = 0
        #: Records whose top level was not an object. Legal JSON, and common
        #: in exports, but it means "field" has no meaning for them, so they
        #: are counted and kept out of the field tree.
        self.non_object_records = 0
        self.malformed: list[Malformed] = []
        self.malformed_count = 0
        self.shapes: dict[frozenset[str], ShapeGroup] = {}
        self.shapes_overflowed = False
        self.truncated_at: int | None = None

    # -- accumulation ------------------------------------------------------

    def add_record(self, record: object, lineno: int) -> None:
        self.records += 1
        if not isinstance(record, dict):
            self.non_object_records += 1
            self.root.observe(record, lineno)
            return
        self.root.observe(record, lineno)
        paths: set[str] = set()
        _descend(self.root, record, lineno, paths)
        self._add_shape(frozenset(paths), lineno)

    def add_malformed(self, lineno: int, message: str) -> None:
        self.malformed_count += 1
        if len(self.malformed) < EXAMPLES_PER_TYPE:
            self.malformed.append(Malformed(lineno, message))

    def _add_shape(self, paths: frozenset[str], lineno: int) -> None:
        group = self.shapes.get(paths)
        if group is None:
            if len(self.shapes) >= MAX_SHAPES:
                # Past the bound, new shapes are not tracked. The count of
                # known shapes stays truthful; ``shapes_overflowed`` says the
                # list is no longer complete.
                self.shapes_overflowed = True
                return
            group = ShapeGroup(paths=paths)
            self.shapes[paths] = group
        group.count += 1
        if len(group.examples) < EXAMPLES_PER_TYPE:
            group.examples.append(lineno)

    # -- queries -----------------------------------------------------------

    @property
    def modal_shape(self) -> ShapeGroup | None:
        """The most common record shape -- what the file mostly looks like."""
        if not self.shapes:
            return None
        return max(self.shapes.values(), key=lambda g: (g.count, -len(g.paths)))

    def outliers(self, limit: int = 5) -> list[tuple[ShapeGroup, int]]:
        """Shapes that differ most from the modal one, rarest first.

        Ranked by how far a shape is from the norm and then by how rare it is,
        because the useful record is not the one that is merely uncommon --
        it is the one that is uncommon *and* shaped differently. Ties break
        towards the earlier line so the output is stable.
        """
        modal = self.modal_shape
        if modal is None:
            return []
        scored = [
            (group, len(group.paths ^ modal.paths))
            for group in self.shapes.values()
            if group.paths != modal.paths
        ]
        scored.sort(
            key=lambda item: (
                -item[1],
                item[0].count,
                item[0].examples[0] if item[0].examples else 0,
            )
        )
        return scored[:limit]

    def conflicts(self) -> list[FieldNode]:
        """Fields holding incompatible types, worst first."""
        found = [node for node in self.root.walk() if node.has_type_conflict]
        found.sort(key=lambda n: (-len(n.non_null_types), n.path))
        return found

    def as_dict(self) -> dict:
        return {
            "schema": "jsonxray/profile-v1",
            "source": self.source,
            "records": self.records,
            "blank_lines": self.blank_lines,
            "bytes_read": self.bytes_read,
            "non_object_records": self.non_object_records,
            "malformed_count": self.malformed_count,
            "malformed": [m.as_dict() for m in self.malformed],
            "truncated_at": self.truncated_at,
            "shapes_overflowed": self.shapes_overflowed,
            "shapes": [g.as_dict() for g in self.shapes.values()],
            "root": self.root.as_dict(),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Profile:
        profile = cls(source=str(raw.get("source", "-")))
        profile.records = int(raw.get("records", 0))
        profile.blank_lines = int(raw.get("blank_lines", 0))
        profile.bytes_read = int(raw.get("bytes_read", 0))
        profile.non_object_records = int(raw.get("non_object_records", 0))
        profile.malformed_count = int(raw.get("malformed_count", 0))
        profile.malformed = [
            Malformed(int(m["lineno"]), str(m["message"]))
            for m in raw.get("malformed", ())
        ]
        profile.truncated_at = raw.get("truncated_at")
        profile.shapes_overflowed = bool(raw.get("shapes_overflowed", False))
        for entry in raw.get("shapes", ()):
            group = ShapeGroup.from_dict(entry)
            profile.shapes[group.paths] = group
        profile.root = FieldNode.from_dict(raw.get("root", {"path": ""}))
        return profile


def _descend(
    node: FieldNode,
    value: object,
    lineno: int,
    paths: set[str],
) -> None:
    """Record every path inside ``value`` under ``node``.

    Only containers recurse. A scalar was already counted by the caller's
    ``observe``, which is what keeps this a single walk of each record rather
    than one walk per statistic.
    """
    if isinstance(value, dict):
        for key, child_value in value.items():
            child = node.child(key)
            child.observe(child_value, lineno)
            paths.add(child.path)
            _descend(child, child_value, lineno, paths)
    elif isinstance(value, list):
        child = node.child("[]", array=True)
        for element in value:
            child.observe(element, lineno)
            _descend(child, element, lineno, paths)
        if value:
            paths.add(child.path)
