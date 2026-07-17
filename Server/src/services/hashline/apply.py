"""Validation and bottom-up application for bulk hashline changes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .hash import contains_patch_prefix, parse_anchor
from .protocol import error
from .snapshot import Snapshot


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    content: tuple[str, ...]
    operation: str


@dataclass(frozen=True)
class AppliedPatch:
    text: str
    changes_applied: int
    lines_added: int
    lines_removed: int
    changed_start: int
    changed_end: int


def _validate_content(value: object) -> tuple[str, ...] | dict[str, object]:
    if not isinstance(value, list) or any(not isinstance(line, str) for line in value):
        return error("E_BAD_SHAPE", "content_lines must be an array of literal strings")
    for line in value:
        if "\n" in line or "\r" in line or contains_patch_prefix(line):
            return error(
                "E_INVALID_PATCH",
                "content_lines must contain literal lines without hashline or diff prefixes",
            )
    return tuple(value)


def _resolve_anchor(value: object, indexes: dict[str, int]) -> int | dict[str, object]:
    anchor = parse_anchor(value)
    if anchor is None:
        return error("E_BAD_REF", "anchor must be a valid fixed-length hashline reference")
    if anchor not in indexes:
        return error(
            "E_STALE_ANCHOR",
            "anchor is not present in the current snapshot; re-read the target range",
            reread_required=True,
        )
    return indexes[anchor]


def _normalize_changes(snapshot: Snapshot, changes: object) -> list[_Span] | dict[str, object]:
    if not isinstance(changes, list) or not changes:
        return error("E_BAD_SHAPE", "changes must be a non-empty array")
    indexes = snapshot.anchor_indexes()
    spans: list[_Span] = []
    for position, raw in enumerate(changes):
        if not isinstance(raw, dict):
            return error("E_BAD_SHAPE", f"changes[{position}] must be an object")
        if any(key in raw for key in ("startLine", "startCol", "endLine", "endCol", "oldText", "newText", "operation")):
            return error("E_LEGACY_SHAPE", "line/column and oldText/newText edit shapes are not executed; read hashline anchors and resend")
        operation = raw.get("op", "replace" if "hash_range_inclusive" in raw else "")
        if operation not in ("replace", "prepend", "append"):
            return error("E_BAD_SHAPE", f"changes[{position}].op must be replace, prepend, or append")
        allowed = {"op", "content_lines"}
        allowed.add("hash_range_inclusive" if operation == "replace" else "anchor")
        unknown = sorted(set(raw) - allowed)
        if unknown:
            return error("E_BAD_SHAPE", f"changes[{position}] has unknown fields", unknown_fields=unknown)
        content = _validate_content(raw.get("content_lines"))
        if isinstance(content, dict):
            return content
        if operation == "replace":
            hash_range = raw.get("hash_range_inclusive")
            if not isinstance(hash_range, list) or len(hash_range) != 2:
                return error("E_BAD_SHAPE", "replace requires hash_range_inclusive with exactly two anchors")
            start = _resolve_anchor(hash_range[0], indexes)
            if isinstance(start, dict):
                return start
            end = _resolve_anchor(hash_range[1], indexes)
            if isinstance(end, dict):
                return end
            if end < start:
                return error("E_BAD_REF", "hash_range_inclusive must be ordered from start to end")
            spans.append(_Span(start, end + 1, content, operation))
        else:
            anchor_value = raw.get("anchor")
            if anchor_value is None:
                insertion = 0 if operation == "prepend" else len(snapshot.lines)
            else:
                resolved = _resolve_anchor(anchor_value, indexes)
                if isinstance(resolved, dict):
                    return resolved
                insertion = resolved if operation == "prepend" else resolved + 1
            spans.append(_Span(insertion, insertion, content, operation))

    ordered = sorted(spans, key=lambda item: (item.start, item.end))
    for previous, current in zip(ordered, ordered[1:]):
        previous_is_insert = previous.start == previous.end
        current_is_insert = current.start == current.end
        overlap = (
            (previous_is_insert and current_is_insert and previous.start == current.start)
            or (previous_is_insert and current.start <= previous.start < current.end)
            or (current_is_insert and previous.start <= current.start < previous.end)
            or (not previous_is_insert and not current_is_insert and current.start < previous.end)
        )
        if overlap:
            return error("E_OVERLAP", "changes overlap or target the same insertion point")
    return spans


def apply_changes(snapshot: Snapshot, changes: object) -> AppliedPatch | dict[str, object]:
    spans = _normalize_changes(snapshot, changes)
    if isinstance(spans, dict):
        return spans
    working = list(snapshot.lines)
    added = 0
    removed = 0
    changed_start = len(working)
    changed_end = 0
    for span in sorted(spans, key=lambda item: item.start, reverse=True):
        removed_here = span.end - span.start
        working[span.start:span.end] = span.content
        added += len(span.content)
        removed += removed_here
        changed_start = min(changed_start, span.start)
        changed_end = max(changed_end, span.start + len(span.content))
    newline = {"lf": "\n", "crlf": "\r\n", "cr": "\r"}.get(snapshot.newline, "\n")
    text = newline.join(working)
    if snapshot.trailing_newline and working:
        text += newline
    return AppliedPatch(
        text=text,
        changes_applied=len(spans),
        lines_added=added,
        lines_removed=removed,
        changed_start=changed_start,
        changed_end=max(changed_start + 1, changed_end),
    )
