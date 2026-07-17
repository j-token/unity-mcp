import pytest

from .test_helpers import DummyContext, setup_script_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_change", [
    {"range": {"start": {"line": 10, "character": 2}, "end": {"line": 10, "character": 2}}, "newText": "// lsp\n"},
    {"range": [0, 0], "text": "// index\n"},
    {"startLine": 1, "startCol": 1, "endLine": 1, "endCol": 1, "newText": "x"},
    {"operation": "replace", "oldText": "a", "newText": "b"},
])
async def test_legacy_text_shapes_are_rejected_without_transport(monkeypatch, legacy_change):
    called = False

    async def fake_send(*args, **kwargs):
        nonlocal called
        called = True

    import services.tools.manage_script as module
    monkeypatch.setattr(module, "send_with_unity_instance", fake_send)
    apply = setup_script_tools()["apply_text_edits"]
    response = await apply(
        DummyContext(),
        uri="mcpforunity://path/Assets/Scripts/F.cs",
        changes=[legacy_change],
    )
    assert response["code"] == "E_LEGACY_SHAPE"
    assert called is False


@pytest.mark.asyncio
async def test_legacy_options_are_not_forwarded(monkeypatch):
    apply = setup_script_tools()["apply_text_edits"]
    response = await apply(
        DummyContext(),
        uri="Assets/F.cs",
        changes=[{"op": "append", "content_lines": ["x"]}],
        options={"validate": "relaxed", "applyMode": "atomic"},
    )
    assert response["code"] == "E_BAD_SHAPE"
