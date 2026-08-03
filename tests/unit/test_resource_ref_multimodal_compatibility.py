from __future__ import annotations

from chatcopilot.contracts.agent import ResourceRef


def test_image_metadata_extension_preserves_positional_schema_contract() -> None:
    resource = ResourceRef("report.csv", "/workspace/report.csv", "file", {"v": 1})

    assert resource.schema == {"v": 1}
    assert resource.media_type is None
    assert resource.size_bytes is None
    assert resource.sha256 is None
