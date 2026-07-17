import hashlib

import pytest

from services.hashline.snapshot import snapshots
from services.tools import manage_script as manage_script_module
from services.tools import find_in_file as find_module

from .test_helpers import DummyContext, setup_script_tools


class FakeUnityTextFile:
    def __init__(self, contents: str, path: str = "Assets/test.txt"):
        self.contents = contents
        self.path = path
        self.write_calls = 0

    @property
    def sha(self):
        return hashlib.sha256(self.contents.encode("utf-8")).hexdigest()

    async def send(self, command, params, **kwargs):
        assert command == "manage_script"
        if params["action"] == "read_text":
            return {"success": True, "data": {
                "path": self.path,
                "contents": self.contents,
                "sha256": self.sha,
                "encoding": "utf-8",
                "newline": "lf",
            }}
        if params["action"] == "apply_hashline_edits":
            assert params["precondition_sha256"] == self.sha
            import base64
            self.contents = base64.b64decode(params["encodedContents"]).decode("utf-8")
            self.write_calls += 1
            return {"success": True, "data": {"path": self.path, "sha256": self.sha}}
        raise AssertionError(params)

    async def transport(self, sender, unity_instance, command, params, **kwargs):
        return await self.send(command, params, **kwargs)


@pytest.fixture(autouse=True)
def clear_snapshots():
    snapshots.clear()
    yield
    snapshots.clear()


@pytest.mark.asyncio
async def test_read_then_bulk_edit_returns_auto_read(monkeypatch):
    fake = FakeUnityTextFile("one\ntwo\nthree\n")
    monkeypatch.setattr(manage_script_module, "send_with_unity_instance", fake.transport)
    monkeypatch.setattr(manage_script_module, "send_mutation", lambda *args, **kwargs: fake.send(args[2], args[3]))
    tools = setup_script_tools()
    read = await tools["read_text"](DummyContext(), "Assets/test.txt", offset=2, limit=1)
    anchor = read["data"]["contents"].split("│", 1)[0]

    edited = await tools["apply_text_edits"](
        DummyContext(),
        "Assets/test.txt",
        changes=[{"hash_range_inclusive": [anchor, anchor], "content_lines": ["TWO"]}],
    )
    assert edited["success"] is True
    assert edited["changes_applied"] == 1
    assert "│TWO" in edited["auto_read"]["contents"]
    assert fake.contents == "one\nTWO\nthree\n"
    assert fake.write_calls == 1


@pytest.mark.asyncio
async def test_stale_anchor_does_not_write(monkeypatch):
    fake = FakeUnityTextFile("one\ntwo\n")
    monkeypatch.setattr(manage_script_module, "send_with_unity_instance", fake.transport)
    tools = setup_script_tools()
    read = await tools["read_text"](DummyContext(), "Assets/test.txt")
    anchor = read["data"]["contents"].splitlines()[1].split("│", 1)[0]
    fake.contents = "one\nexternally changed\n"

    result = await tools["apply_text_edits"](
        DummyContext(),
        "Assets/test.txt",
        changes=[{"hash_range_inclusive": [anchor, anchor], "content_lines": ["TWO"]}],
    )
    assert result["code"] == "E_STALE_ANCHOR"
    assert fake.write_calls == 0
    assert fake.contents == "one\nexternally changed\n"


@pytest.mark.asyncio
async def test_invalid_member_aborts_entire_bulk_without_write(monkeypatch):
    fake = FakeUnityTextFile("one\ntwo\nthree\n")
    monkeypatch.setattr(manage_script_module, "send_with_unity_instance", fake.transport)
    tools = setup_script_tools()
    read = await tools["read_text"](DummyContext(), "Assets/test.txt")
    anchors = [line.split("│", 1)[0] for line in read["data"]["contents"].splitlines()]

    result = await tools["apply_text_edits"](
        DummyContext(),
        "Assets/test.txt",
        changes=[
            {"hash_range_inclusive": [anchors[0], anchors[0]], "content_lines": ["ONE"]},
            {"hash_range_inclusive": [anchors[1], anchors[1]], "content_lines": [f"{anchors[1]}│two"]},
        ],
    )

    assert result["code"] == "E_INVALID_PATCH"
    assert fake.write_calls == 0
    assert fake.contents == "one\ntwo\nthree\n"


@pytest.mark.asyncio
async def test_legacy_shape_is_rejected_without_unity_call(monkeypatch):
    called = False

    async def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(manage_script_module, "send_with_unity_instance", fail_if_called)
    tools = setup_script_tools()
    result = await tools["apply_text_edits"](
        DummyContext(),
        "Assets/test.txt",
        edits=[{"startLine": 1, "startCol": 1, "endLine": 1, "endCol": 2, "newText": "x"}],
    )
    assert result["code"] == "E_LEGACY_SHAPE"
    assert called is False


@pytest.mark.asyncio
async def test_find_result_hash_can_feed_edit(monkeypatch):
    fake = FakeUnityTextFile("alpha\nneedle here\nomega\n")
    monkeypatch.setattr(find_module, "send_with_unity_instance", fake.transport)
    monkeypatch.setattr(manage_script_module, "send_with_unity_instance", fake.transport)
    monkeypatch.setattr(manage_script_module, "send_mutation", lambda *args, **kwargs: fake.send(args[2], args[3]))

    found = await find_module.find_in_file(DummyContext(), "Assets/test.txt", "needle")
    anchor = found["data"]["matches"][0]["hash"]
    tools = setup_script_tools()
    result = await tools["apply_text_edits"](
        DummyContext(),
        "Assets/test.txt",
        changes=[{"hash_range_inclusive": [anchor, anchor], "content_lines": ["replacement"]}],
    )
    assert result["success"] is True
    assert fake.contents == "alpha\nreplacement\nomega\n"


@pytest.mark.asyncio
async def test_find_in_file_maps_cr_only_line_numbers(monkeypatch):
    fake = FakeUnityTextFile("alpha\rneedle\romega")
    monkeypatch.setattr(find_module, "send_with_unity_instance", fake.transport)

    found = await find_module.find_in_file(DummyContext(), "Assets/test.txt", "needle")

    assert found["data"]["matches"][0]["line"] == 2
    assert found["data"]["matches"][0]["content"] == "needle"
