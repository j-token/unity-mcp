"""Hashline protocol primitives shared by text-reading and editing tools."""

from .apply import AppliedPatch, apply_changes
from .errors import ALL_ERROR_CODES
from .hash import ANCHOR_LENGTH, PROTOCOL_VERSION, format_hashline, parse_anchor
from .protocol import build_read_response, decode_unity_text_response, normalize_text_uri
from .snapshot import Snapshot, snapshots

__all__ = [
    "ANCHOR_LENGTH",
    "ALL_ERROR_CODES",
    "PROTOCOL_VERSION",
    "AppliedPatch",
    "Snapshot",
    "apply_changes",
    "build_read_response",
    "decode_unity_text_response",
    "format_hashline",
    "normalize_text_uri",
    "parse_anchor",
    "snapshots",
]
