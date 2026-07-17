import pytest

from .test_helpers import DummyContext, setup_script_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [None, False, True])
async def test_zero_based_coordinates_are_never_normalized(strict):
    apply_edits = setup_script_tools()["apply_text_edits"]
    response = await apply_edits(
        DummyContext(),
        uri="mcpforunity://path/Assets/Scripts/F.cs",
        edits=[{"startLine": 0, "startCol": 0, "endLine": 0, "endCol": 0, "newText": "//x"}],
        strict=strict,
    )
    assert response["success"] is False
    assert response["code"] == "E_LEGACY_SHAPE"
