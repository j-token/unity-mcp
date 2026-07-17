"""Hashline read response and Unity wire helpers."""

from __future__ import annotations

import base64
import os
from urllib.parse import unquote, urlparse

from .hash import PROTOCOL_VERSION, format_hashline
from .snapshot import Snapshot

DEFAULT_READ_LIMIT = 200
MAX_READ_LIMIT = 500
MAX_RESPONSE_CHARS = 60 * 1024


def error(code: str, message: str, **data: object) -> dict[str, object]:
    payload: dict[str, object] = {"success": False, "code": code, "message": message}
    if data:
        payload["data"] = data
    return payload


def normalize_text_uri(uri: str) -> str:
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("path must be a non-empty string")
    value = uri.strip()
    if value.startswith("mcpforunity://path/"):
        value = value[len("mcpforunity://path/"):]
    elif value.startswith("file://"):
        parsed = urlparse(value)
        path = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            path = f"//{parsed.netloc}{path}"
        value = path
        if os.name == "nt" and len(value) >= 3 and value[0] == "/" and value[2] == ":":
            value = value[1:]
    value = unquote(value).replace("\\", "/")
    return os.path.normpath(value).replace("\\", "/")


def decode_unity_text_response(response: dict[str, object]) -> tuple[str, dict[str, object]]:
    data = response.get("data")
    if not isinstance(data, dict):
        raise ValueError("Unity response did not include text metadata")
    contents = data.get("contents")
    if contents is None and data.get("encodedContents"):
        contents = base64.b64decode(str(data["encodedContents"])).decode("utf-8")
    if not isinstance(contents, str):
        raise ValueError("Unity response did not include text contents")
    return contents, data


def snapshot_key(unity_instance: str | None, canonical_path: str) -> str:
    normalized = canonical_path.replace("\\", "/").lower()
    return f"{unity_instance or 'default'}::{normalized}"


def build_read_response(
    snapshot: Snapshot,
    *,
    path: str,
    offset: int = 1,
    limit: int = DEFAULT_READ_LIMIT,
    raw: bool = False,
    max_chars: int = MAX_RESPONSE_CHARS,
) -> dict[str, object]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 1:
        return error("E_BAD_SHAPE", "offset must be a 1-indexed positive integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return error("E_BAD_SHAPE", "limit must be a positive integer")
    limit = min(limit, MAX_READ_LIMIT)
    start = min(offset - 1, len(snapshot.lines))
    requested_end = min(start + limit, len(snapshot.lines))
    rendered: list[str] = []
    used = 0
    line_truncated = False
    for index in range(start, requested_end):
        content = snapshot.lines[index]
        tagged = content if raw else format_hashline(snapshot.anchors[index], content)
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(tagged) + 1 > remaining:
            if not rendered:
                tagged = tagged[: max(0, remaining - 1)] + "…"
                line_truncated = True
            else:
                break
        rendered.append(tagged)
        used += len(tagged) + 1

    returned = len(rendered)
    next_offset = start + returned + 1
    has_more = start + returned < len(snapshot.lines)
    data: dict[str, object] = {
        "path": path,
        "offset": offset,
        "limit": limit,
        "returned_lines": returned,
        "total_lines": len(snapshot.lines),
        "has_more": has_more,
        "file_sha256": snapshot.file_sha256,
        "encoding": snapshot.encoding,
        "newline": snapshot.newline,
        "protocol": PROTOCOL_VERSION,
        "contents": "\n".join(rendered),
        "raw": raw,
    }
    if raw and rendered:
        separator = {"lf": "\n", "crlf": "\r\n", "cr": "\r"}.get(snapshot.newline, "\n")
        data["contents"] = separator.join(rendered)
        if start + returned == len(snapshot.lines) and snapshot.trailing_newline and not line_truncated:
            data["contents"] += separator
    if has_more:
        data["next_offset"] = next_offset
        data["pagination_hint"] = f"Read again with offset={next_offset} and limit={limit}."
    if line_truncated:
        data["line_truncated"] = True
        data["pagination_hint"] = "A single line exceeded the response cap; use a narrower external file reader for its full content."
    if not snapshot.lines:
        data["empty_file_hint"] = "The file is empty; use prepend or append without an anchor."
    return {"success": True, "data": data}
