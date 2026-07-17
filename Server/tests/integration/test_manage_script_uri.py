import os

import pytest

from services.hashline.protocol import normalize_text_uri


@pytest.mark.parametrize("uri, expected", [
    ("mcpforunity://path/Assets/Scripts/MyScript.cs", "Assets/Scripts/MyScript.cs"),
    ("Assets/Data/settings.json", "Assets/Data/settings.json"),
    ("ProjectSettings/TagManager.asset", "ProjectSettings/TagManager.asset"),
    ("file:///C:/Users/Alex/Proj/Assets/Scripts/Hello.cs", "C:/Users/Alex/Proj/Assets/Scripts/Hello.cs"),
])
def test_normalize_text_uri_preserves_full_file_path(uri, expected):
    actual = normalize_text_uri(uri)
    if os.name == "nt":
        assert actual == expected
    else:
        assert actual.endswith(expected)


def test_normalize_text_uri_decodes_percent_escapes():
    assert normalize_text_uri("mcpforunity://path/Assets/Foo%20Bar.txt") == "Assets/Foo Bar.txt"
