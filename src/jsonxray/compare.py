"""Comparing two profiles, so drift is caught by CI instead of by a customer.

Running this once tells you what is in a file. Running it in a pipeline tells
you when that stopped being true, which is the thing that actually breaks
downstream: a field quietly disappears from an upstream export, or starts
arriving as a string, and nothing notices until a dashboard goes blank.

The design constraint is false positives. A check that fires on ordinary
variation gets switched off within a week, so presence changes have to clear a
threshold, and only structural changes -- a field appearing or vanishing, a
type appearing or vanishing -- are treated as breaking by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .profile import FieldNode, Profile, denominator_for

__all__ = ["Change", "Comparison", "compare"]

#: How much a field's presence must move before it is worth reporting. Sample
#: noise between two days of an export is routinely a fraction of a percent;
#: five points is a change someone made.
PRESENCE_THRESHOLD = 0.05


@dataclass(frozen=True)
class Change:
    """One difference between two profiles."""

    path: str
    kind: str
    detail: str
    #: True when this would break a consumer that worked against the baseline.
    breaking: bool

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "kind": self.kind,
            "detail": self.detail,
            "breaking": self.breaking,
        }


@dataclass
class Comparison:
    baseline: str
    current: str
    changes: list[Change] = field(default_factory=list)

    @property
    def breaking(self) -> list[Change]:
        return [c for c in self.changes if c.breaking]

    @property
    def ok(self) -> bool:
        return not self.breaking

    def as_dict(self) -> dict:
        return {
            "schema": "jsonxray/comparison-v1",
            "baseline": self.baseline,
            "current": self.current,
            "breaking": len(self.breaking),
            "changes": [c.as_dict() for c in self.changes],
        }


def compare(baseline: Profile, current: Profile) -> Comparison:
    """Every difference between two profiles, breaking ones marked."""
    result = Comparison(baseline=baseline.source, current=current.source)
    _compare_nodes(baseline.root, current.root, result)

    if current.malformed_count > baseline.malformed_count:
        result.changes.append(
            Change(
                path="",
                kind="malformed",
                detail=(
                    f"unparseable lines rose from {baseline.malformed_count} "
                    f"to {current.malformed_count}"
                ),
                breaking=True,
            )
        )
    return result


def _compare_nodes(
    before: FieldNode | None,
    after: FieldNode | None,
    result: Comparison,
    old_denominator: int | None = None,
    new_denominator: int | None = None,
) -> None:
    if before is None and after is None:  # pragma: no cover - not reachable
        return

    if before is not None and after is not None:
        _compare_present(before, after, old_denominator, new_denominator, result)

    keys = set()
    if before is not None:
        keys |= set(before.children)
    if after is not None:
        keys |= set(after.children)

    for key in sorted(keys):
        old = before.children.get(key) if before else None
        new = after.children.get(key) if after else None
        if old is not None and new is None:
            result.changes.append(
                Change(
                    path=old.path,
                    kind="field-removed",
                    detail="present in the baseline, absent now",
                    breaking=True,
                )
            )
            continue
        if old is None and new is not None:
            result.changes.append(
                Change(
                    path=new.path,
                    kind="field-added",
                    detail=f"new field, {' | '.join(new.non_null_types) or 'null'}",
                    # A new field breaks nothing that worked before. It is
                    # reported because it is usually news, not because it is
                    # a failure.
                    breaking=False,
                )
            )
        _compare_nodes(
            old,
            new,
            result,
            denominator_for(before, key) if before else None,
            denominator_for(after, key) if after else None,
        )


def _compare_present(
    before: FieldNode,
    after: FieldNode,
    old_denominator: int | None,
    new_denominator: int | None,
    result: Comparison,
) -> None:
    old_types = set(before.non_null_types)
    new_types = set(after.non_null_types)

    for kind in sorted(new_types - old_types):
        lines = after.examples.get(kind, [])
        where = f" (line {', '.join(str(n) for n in lines)})" if lines else ""
        result.changes.append(
            Change(
                path=after.path,
                kind="type-added",
                detail=f"now also holds {kind}{where}",
                breaking=True,
            )
        )
    for kind in sorted(old_types - new_types):
        result.changes.append(
            Change(
                path=after.path,
                kind="type-removed",
                detail=f"no longer holds {kind}",
                breaking=False,
            )
        )

    had_null = before.null_count > 0
    has_null = after.null_count > 0
    if has_null and not had_null:
        lines = after.examples.get("null", [])
        where = f" (line {', '.join(str(n) for n in lines)})" if lines else ""
        result.changes.append(
            Change(
                path=after.path,
                kind="nullable",
                detail=f"now sometimes null{where}",
                breaking=True,
            )
        )

    old_share = _presence(before, old_denominator)
    new_share = _presence(after, new_denominator)
    if old_share is not None and new_share is not None:
        delta = new_share - old_share
        if abs(delta) >= PRESENCE_THRESHOLD:
            direction = "rose" if delta > 0 else "fell"
            result.changes.append(
                Change(
                    path=after.path,
                    kind="presence",
                    detail=(
                        f"presence {direction} from {old_share * 100:.1f}% to "
                        f"{new_share * 100:.1f}%"
                    ),
                    # A field that was always there and now sometimes is not
                    # breaks a consumer that never checked. The reverse does
                    # not.
                    breaking=old_share >= 1.0 > new_share,
                )
            )

    # Enum drift, but only where both sides genuinely knew their full value
    # set. Once either side overflowed its bound, its value table is not a
    # complete set and differencing the two would invent changes.
    if before.is_enum and after.is_enum:
        added = set(after.values) - set(before.values)
        if added:
            result.changes.append(
                Change(
                    path=after.path,
                    kind="values-added",
                    detail="new values: " + ", ".join(sorted(added)[:5]),
                    breaking=False,
                )
            )


def _presence(node: FieldNode, denominator: int | None) -> float | None:
    """How often this field appeared, relative to its own parent.

    ``None`` at the root, which has no parent to be relative to, and when the
    parent was never an object or array -- dividing by that zero would be a
    made-up number rather than a missing one.
    """
    if not node.path or not denominator:
        return None
    return node.occurrences / denominator
