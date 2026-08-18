"""Declarative multi-step queries: a small DAG of AWS read calls with fixed,
non-evaluated data extraction between steps.

A step's ``params`` may reference an earlier step's extracted output via a
``{"$ref": "<step_id>"}`` marker anywhere a literal value would go. There is
no expression language and nothing is ever ``eval``'d - only dotted-path
field extraction (``extract="Reservations[].Instances[].InstanceId"``-style,
implemented with a tiny fixed parser, not a general JSONPath engine).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..errors import McpError


class DagError(McpError):
    """Raised for a malformed DAG: unknown step reference, or a dependency cycle."""


@dataclass(frozen=True, slots=True)
class Step:
    """One node in the query DAG.

    Attributes:
        id: Unique step id, referenced by later steps via ``{"$ref": id}``.
        service: boto3 service name.
        operation: PascalCase AWS operation name.
        params: Params for the call. Any nested ``{"$ref": "<id>"}`` value is
            replaced with that step's extracted output before the call runs.
        extract: A tiny fixed-grammar path (dot for dict keys, ``[]``
            suffix to flatten a list of dicts) applied to this step's raw
            response to produce the value later steps can ``$ref``.
        depends_on: Explicit dependencies, in addition to any implied by a
            ``$ref`` inside ``params`` (both are honored).
    """

    id: str
    service: str
    operation: str
    params: dict[str, Any] = field(default_factory=dict)
    extract: str | None = None
    depends_on: tuple[str, ...] = ()


def _refs_in(value: Any) -> set[str]:
    if isinstance(value, dict):
        if set(value) == {"$ref"} and isinstance(value["$ref"], str):
            return {value["$ref"]}
        refs: set[str] = set()
        for v in value.values():
            refs |= _refs_in(v)
        return refs
    if isinstance(value, list):
        refs = set()
        for v in value:
            refs |= _refs_in(v)
        return refs
    return set()


def topological_order(steps: list[Step]) -> list[Step]:
    """Return ``steps`` ordered so every dependency runs before its dependents.

    Raises:
        DagError: On an unknown step reference or a dependency cycle.
    """
    by_id = {s.id: s for s in steps}
    deps: dict[str, set[str]] = {}
    for step in steps:
        step_deps = set(step.depends_on) | _refs_in(step.params)
        unknown = step_deps - by_id.keys()
        if unknown:
            raise DagError(f"step {step.id!r} references unknown step(s): {sorted(unknown)}")
        deps[step.id] = step_deps

    ordered: list[Step] = []
    resolved: set[str] = set()
    remaining = dict(deps)
    while remaining:
        ready = [sid for sid, d in remaining.items() if d <= resolved]
        if not ready:
            raise DagError(f"dependency cycle detected among steps: {sorted(remaining)}")
        for sid in sorted(ready):
            ordered.append(by_id[sid])
            resolved.add(sid)
            del remaining[sid]
    return ordered


def _extract(response: dict[str, Any], path: str | None) -> Any:
    """Apply a tiny fixed-grammar extraction path to one step's response.

    Supports dotted dict-key traversal and a trailing ``[]`` on any segment
    to flatten across a list of dicts, e.g. ``"Reservations[].Instances[].
    InstanceId"``. No arbitrary code ever runs here.
    """
    if path is None:
        return response
    current: Any = response
    for raw_segment in path.split("."):
        flatten = raw_segment.endswith("[]")
        key = raw_segment[:-2] if flatten else raw_segment
        if isinstance(current, list):
            current = [item.get(key) if isinstance(item, dict) else None for item in current]
        elif isinstance(current, dict):
            current = current.get(key)
        else:
            current = None
        if flatten:
            flattened: list[Any] = []
            items = current if isinstance(current, list) else [current]
            for item in items:
                if isinstance(item, list):
                    flattened.extend(item)
                elif item is not None:
                    flattened.append(item)
            current = flattened
    return current


def _substitute_refs(value: Any, extracted: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"} and isinstance(value["$ref"], str):
            return extracted[value["$ref"]]
        return {k: _substitute_refs(v, extracted) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_refs(v, extracted) for v in value]
    return value


def run_dag(
    steps: list[Step],
    *,
    call: Callable[[Step, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Run every step in dependency order, substituting ``$ref`` values as they resolve.

    Args:
        call: Invoked once per step as ``call(step, resolved_params)``; must
            return that step's raw AWS response dict. This module has no
            AWS/credential knowledge itself - ``call`` is the only place
            that happens, so it can be the generic engine, a fan-out across
            many accounts, or a test double.

    Returns:
        A dict with ``"steps"``: per-step ``{"response": ..., "extracted": ...}``.
    """
    ordered = topological_order(steps)
    extracted: dict[str, Any] = {}
    results: dict[str, Any] = {}
    for step in ordered:
        resolved_params = _substitute_refs(step.params, extracted)
        response = call(step, resolved_params)
        value = _extract(response, step.extract)
        extracted[step.id] = value
        results[step.id] = {"response": response, "extracted": value}
    return {"steps": results}
