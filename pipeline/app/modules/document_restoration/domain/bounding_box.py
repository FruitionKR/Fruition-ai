from __future__ import annotations


BBox = tuple[float, float, float, float]


def bbox_center_inside(inner: BBox, outer: BBox) -> bool:
    x = (inner[0] + inner[2]) / 2
    y = (inner[1] + inner[3]) / 2
    return outer[0] <= x <= outer[2] and outer[1] <= y <= outer[3]


def bbox_overlap_ratio(candidate: BBox, target: BBox) -> float:
    area = max(0.0, candidate[2] - candidate[0]) * max(0.0, candidate[3] - candidate[1])
    if area == 0:
        return 0.0
    overlap = (
        max(0.0, min(candidate[2], target[2]) - max(candidate[0], target[0]))
        * max(0.0, min(candidate[3], target[3]) - max(candidate[1], target[1]))
    )
    return overlap / area


def bbox_match_score(first: BBox, second: BBox) -> float:
    return max(bbox_overlap_ratio(first, second), bbox_overlap_ratio(second, first))
