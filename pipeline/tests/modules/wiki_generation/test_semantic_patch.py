from app.modules.wiki_generation.application.semantic_patch import (
    apply_semantic_patch,
    build_semantic_patch_targets,
)


def _notes() -> list[dict[str, object]]:
    return [
        {
            "chunk_id": "chunk_0001",
            "observations": [
                {"title": "유지", "summary": "변경하지 않음", "anchor_block_ids": ["B0002"]}
            ],
            "evidence_claims": [
                {"claim": "첫 주장", "anchor_block_ids": ["B0001"]},
                {"claim": "너무 넓은 주장", "anchor_block_ids": ["B0002"]},
            ],
        },
        {
            "chunk_id": "chunk_0002",
            "evidence_claims": [
                {"claim": "다른 chunk 주장", "anchor_block_ids": ["B0004"]}
            ],
        },
    ]


def test_builds_only_exact_evaluator_target_as_editable() -> None:
    targets = build_semantic_patch_targets(
        _notes(),
        {"issues": [{"target": ["ev_0002"]}]},
        ["B0002"],
    )

    assert [(item["collection"], item["index"]) for item in targets] == [
        ("evidence_claims", 1)
    ]


def test_applies_patch_without_changing_unrelated_items() -> None:
    notes = _notes()
    targets = build_semantic_patch_targets(
        notes,
        {"issues": [{"target": ["ev_0002"]}]},
        ["B0002"],
    )
    patch = {
        "operations": [
            {
                "op": "replace",
                "chunk_id": "chunk_0001",
                "collection": "evidence_claims",
                "index": 1,
                "items": [
                    {"claim": "원자 주장 A", "anchor_block_ids": ["B0002"]},
                    {"claim": "원자 주장 B", "anchor_block_ids": ["B0002"]},
                ],
            }
        ]
    }

    patched = apply_semantic_patch(notes, patch, targets, ["B0001", "B0002", "B0003"])

    assert patched is not None
    assert patched[0]["observations"] == notes[0]["observations"]
    assert patched[0]["evidence_claims"][0] == notes[0]["evidence_claims"][0]
    assert patched[1] == notes[1]
    assert [item["claim"] for item in patched[0]["evidence_claims"]] == [
        "첫 주장",
        "원자 주장 A",
        "원자 주장 B",
    ]


def test_rejects_uneditable_path_or_unknown_anchor() -> None:
    notes = _notes()
    targets = build_semantic_patch_targets(
        notes,
        {"issues": [{"target": ["ev_0002"]}]},
        ["B0002"],
    )

    assert apply_semantic_patch(
        notes,
        {
            "operations": [
                {
                    "op": "remove",
                    "chunk_id": "chunk_0001",
                    "collection": "evidence_claims",
                    "index": 0,
                    "items": [],
                }
            ]
        },
        targets,
        ["B0001", "B0002", "B0003"],
    ) is None
    assert apply_semantic_patch(
        notes,
        {
            "operations": [
                {
                    "op": "replace",
                    "chunk_id": "chunk_0001",
                    "collection": "evidence_claims",
                    "index": 1,
                    "items": [{"claim": "잘못된 anchor", "anchor_block_ids": ["B9999"]}],
                }
            ]
        },
        targets,
        ["B0001", "B0002", "B0003"],
    ) is None
