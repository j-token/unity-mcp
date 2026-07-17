"""Process-local hashline snapshots; file contents are never persisted."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from time import monotonic

from .hash import generate_anchors


@dataclass(frozen=True)
class Snapshot:
    canonical_key: str
    file_sha256: str
    encoding: str
    newline: str
    lines: tuple[str, ...]
    anchors: tuple[str, ...]
    trailing_newline: bool
    updated_at: float

    @classmethod
    def create(
        cls,
        canonical_key: str,
        file_sha256: str,
        encoding: str,
        newline: str,
        text: str,
    ) -> "Snapshot":
        lines, trailing_newline = split_text(text)
        return cls(
            canonical_key=canonical_key,
            file_sha256=file_sha256,
            encoding=encoding,
            newline=newline,
            lines=tuple(lines),
            anchors=tuple(generate_anchors(lines, namespace=file_sha256)),
            trailing_newline=trailing_newline,
            updated_at=monotonic(),
        )

    def anchor_indexes(self) -> dict[str, int]:
        return {anchor: index for index, anchor in enumerate(self.anchors)}


def split_text(text: str) -> tuple[list[str], bool]:
    if text == "":
        return [], False
    trailing = text.endswith(("\n", "\r"))
    lines = re.split(r"\r\n|\n|\r", text)
    if trailing:
        lines.pop()
    return lines, trailing


class SnapshotStore:
    _ttl_seconds = 60 * 60
    _max_entries = 512

    def __init__(self) -> None:
        self._snapshots: dict[str, Snapshot] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> Snapshot | None:
        snapshot = self._snapshots.get(key)
        if snapshot is not None and monotonic() - snapshot.updated_at > self._ttl_seconds:
            self.remove(key)
            return None
        return snapshot

    def put(self, snapshot: Snapshot) -> Snapshot:
        self._snapshots[snapshot.canonical_key] = snapshot
        cutoff = monotonic() - self._ttl_seconds
        expired = [key for key, value in self._snapshots.items() if value.updated_at < cutoff]
        for key in expired:
            self.remove(key)
        overflow = len(self._snapshots) - self._max_entries
        if overflow > 0:
            oldest = sorted(self._snapshots.values(), key=lambda value: value.updated_at)[:overflow]
            for value in oldest:
                self.remove(value.canonical_key)
        return snapshot

    def remove(self, key: str) -> None:
        self._snapshots.pop(key, None)
        lock = self._locks.get(key)
        if lock is not None and not lock.locked():
            self._locks.pop(key, None)

    def lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def clear(self) -> None:
        self._snapshots.clear()
        self._locks.clear()


snapshots = SnapshotStore()
