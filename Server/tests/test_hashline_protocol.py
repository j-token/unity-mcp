import re

from services.hashline.apply import AppliedPatch, apply_changes
from services.hashline.hash import ANCHOR_LENGTH, format_hashline, generate_anchors, parse_anchor
from services.hashline.protocol import build_read_response
from services.hashline.snapshot import Snapshot


def make_snapshot(text: str, *, newline: str = "lf") -> Snapshot:
    return Snapshot.create("default::Assets/test.txt", "sha", "utf-8", newline, text)


def test_anchors_are_url_safe_unique_and_deterministic_for_duplicate_lines():
    lines = ["same", "same", "different", "same"]
    first = generate_anchors(lines)
    second = generate_anchors(lines)
    assert first == second
    assert len(set(first)) == len(lines)
    assert all(re.fullmatch(rf"[A-Za-z0-9_-]{{{ANCHOR_LENGTH}}}", anchor) for anchor in first)
    assert parse_anchor(first[0]) == first[0]
    assert format_hashline(first[0], "same") == f"{first[0]}│same"


def test_snapshot_sha_namespaces_anchors_to_prevent_cross_generation_reuse():
    before = Snapshot.create("key", "a" * 64, "utf-8", "lf", "same\n")
    after = Snapshot.create("key", "b" * 64, "utf-8", "lf", "same\n")
    assert before.anchors != after.anchors


def test_partial_read_returns_only_requested_hashlines_and_pagination_hint():
    snapshot = make_snapshot("one\ntwo\nthree\nfour\n")
    response = build_read_response(snapshot, path="Assets/test.txt", offset=2, limit=2)
    data = response["data"]
    assert data["returned_lines"] == 2
    assert data["total_lines"] == 4
    assert data["has_more"] is True
    assert data["next_offset"] == 4
    assert data["contents"].splitlines() == [
        f"{snapshot.anchors[1]}│two",
        f"{snapshot.anchors[2]}│three",
    ]


def test_bulk_replace_delete_and_bottom_up_application():
    snapshot = make_snapshot("a\nb\nc\nd\ne\n")
    result = apply_changes(snapshot, [
        {"hash_range_inclusive": [snapshot.anchors[3], snapshot.anchors[4]], "content_lines": ["D"]},
        {"hash_range_inclusive": [snapshot.anchors[1], snapshot.anchors[1]], "content_lines": []},
    ])
    assert isinstance(result, AppliedPatch)
    assert result.text == "a\nc\nD\n"
    assert result.lines_added == 1
    assert result.lines_removed == 3


def test_prepend_and_append_support_file_and_anchor_targets():
    snapshot = make_snapshot("a\nb")
    result = apply_changes(snapshot, [
        {"op": "prepend", "content_lines": ["top"]},
        {"op": "append", "anchor": snapshot.anchors[0], "content_lines": ["after-a"]},
        {"op": "append", "content_lines": ["bottom"]},
    ])
    assert isinstance(result, AppliedPatch)
    assert result.text == "top\na\nafter-a\nb\nbottom"


def test_overlap_and_invalid_patch_are_rejected_before_apply():
    snapshot = make_snapshot("a\nb\nc")
    overlap = apply_changes(snapshot, [
        {"hash_range_inclusive": [snapshot.anchors[0], snapshot.anchors[1]], "content_lines": ["x"]},
        {"hash_range_inclusive": [snapshot.anchors[1], snapshot.anchors[2]], "content_lines": ["y"]},
    ])
    assert overlap["code"] == "E_OVERLAP"

    invalid = apply_changes(snapshot, [
        {"hash_range_inclusive": [snapshot.anchors[0], snapshot.anchors[0]], "content_lines": [f"{snapshot.anchors[0]}│a"]},
    ])
    assert invalid["code"] == "E_INVALID_PATCH"


def test_unknown_well_formed_anchor_is_stale_and_legacy_shape_is_explicit():
    snapshot = make_snapshot("a\nb")
    stale = apply_changes(snapshot, [
        {"hash_range_inclusive": ["ABC123", "ABC123"], "content_lines": ["x"]},
    ])
    assert stale["code"] == "E_STALE_ANCHOR"

    legacy = apply_changes(snapshot, [
        {"startLine": 1, "startCol": 1, "endLine": 1, "endCol": 2, "newText": "x"},
    ])
    assert legacy["code"] == "E_LEGACY_SHAPE"


def test_crlf_and_terminal_newline_are_preserved():
    snapshot = make_snapshot("a\r\nb\r\n", newline="crlf")
    result = apply_changes(snapshot, [
        {"hash_range_inclusive": [snapshot.anchors[1], snapshot.anchors[1]], "content_lines": ["B"]},
    ])
    assert isinstance(result, AppliedPatch)
    assert result.text == "a\r\nB\r\n"


def test_empty_file_has_no_synthetic_anchor_and_accepts_append():
    snapshot = make_snapshot("")
    read = build_read_response(snapshot, path="Assets/empty.txt")
    assert snapshot.anchors == ()
    assert "empty_file_hint" in read["data"]
    result = apply_changes(snapshot, [{"op": "append", "content_lines": ["first"]}])
    assert isinstance(result, AppliedPatch)
    assert result.text == "first"


def test_only_cr_lf_sequences_split_hashlines():
    snapshot = make_snapshot("one\fstill-one\ntwo")
    assert snapshot.lines == ("one\fstill-one", "two")


def test_large_file_partial_read_and_hashline_replace():
    snapshot = make_snapshot("\n".join(f"line-{index}" for index in range(1, 2501)))
    read = build_read_response(snapshot, path="Assets/large.txt", offset=1999, limit=5)
    assert read["data"]["returned_lines"] == 5
    assert read["data"]["total_lines"] == 2500
    assert read["data"]["has_more"] is True
    assert "│line-2001" in read["data"]["contents"]

    result = apply_changes(snapshot, [{
        "hash_range_inclusive": [snapshot.anchors[2000], snapshot.anchors[2000]],
        "content_lines": ["replaced-2001"],
    }])
    assert isinstance(result, AppliedPatch)
    assert result.text.splitlines()[2000] == "replaced-2001"
