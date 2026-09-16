from app.modules.document_restoration.infrastructure.docling_io import (
    docling_bbox_to_top_left,
    item_page_and_bbox,
)


def test_converts_bottom_left_bbox_to_top_left() -> None:
    bbox = {"l": 10, "r": 30, "t": 80, "b": 60, "coord_origin": "BOTTOMLEFT"}

    assert docling_bbox_to_top_left(bbox, 100) == (10.0, 20.0, 30.0, 40.0)


def test_returns_none_for_invalid_or_missing_provenance() -> None:
    assert docling_bbox_to_top_left({"l": 10}, 100) is None
    assert item_page_and_bbox({}, {}) is None
