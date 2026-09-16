from app.modules.document_restoration.domain.bounding_box import (
    bbox_center_inside,
    bbox_match_score,
    bbox_overlap_ratio,
)


def test_calculates_directional_overlap_and_symmetric_match() -> None:
    small = (0.0, 0.0, 2.0, 2.0)
    large = (0.0, 0.0, 4.0, 4.0)

    assert bbox_overlap_ratio(small, large) == 1.0
    assert bbox_overlap_ratio(large, small) == 0.25
    assert bbox_match_score(small, large) == 1.0


def test_checks_center_and_zero_area() -> None:
    assert bbox_center_inside((1.0, 1.0, 2.0, 2.0), (0.0, 0.0, 3.0, 3.0))
    assert bbox_overlap_ratio((1.0, 1.0, 1.0, 2.0), (0.0, 0.0, 3.0, 3.0)) == 0.0
