"""Stable, compact, file-local hashline anchors."""

from __future__ import annotations

import base64
import hashlib
import re

ANCHOR_LENGTH = 6
PROTOCOL_VERSION = "hashline-blake2s-v1"
SEPARATOR = "│"
_ANCHOR_RE = re.compile(rf"^[A-Za-z0-9_-]{{{ANCHOR_LENGTH}}}$")
_TAGGED_RE = re.compile(rf"^(?P<anchor>[A-Za-z0-9_-]{{{ANCHOR_LENGTH}}}){SEPARATOR}(?P<content>.*)$")


def _candidate(line: str, collision_counter: int, namespace: str) -> str:
    payload = namespace.encode("ascii", "strict") + b"\x00" + line.encode("utf-8")
    if collision_counter:
        payload += b"\x00" + str(collision_counter).encode("ascii")
    digest = hashlib.blake2s(payload, digest_size=6).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:ANCHOR_LENGTH]


def generate_anchors(lines: list[str], namespace: str = "") -> list[str]:
    """Generate unique anchors, deterministically resolving file-local collisions."""
    used: set[str] = set()
    anchors: list[str] = []
    for line in lines:
        counter = 0
        while True:
            anchor = _candidate(line, counter, namespace)
            if anchor not in used:
                used.add(anchor)
                anchors.append(anchor)
                break
            counter += 1
    return anchors


def parse_anchor(value: object) -> str | None:
    if not isinstance(value, str) or not _ANCHOR_RE.fullmatch(value):
        return None
    return value


def parse_hashline(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    match = _TAGGED_RE.fullmatch(value)
    if not match:
        return None
    return match.group("anchor"), match.group("content")


def format_hashline(anchor: str, content: str) -> str:
    if parse_anchor(anchor) is None:
        raise ValueError("invalid hashline anchor")
    return f"{anchor}{SEPARATOR}{content}"


def contains_patch_prefix(line: str) -> bool:
    if _TAGGED_RE.match(line):
        return True
    return line.startswith(("+ ", "- ", "+++ ", "--- ", "@@ "))
