from app.modules.wiki_generation.domain.entities import SourceBlock
from app.modules.wiki_ingestion.domain.source_block_changes import (
    compare_source_blocks,
)


def _block(block_id: str, text: str) -> SourceBlock:
    return SourceBlock(
        document_id="doc-1",
        block_id=block_id,
        source_reference_id=block_id,
        text=text,
        line_start=1,
        line_end=1,
    )


def test_compare_source_blocks_classifies_single_replacement() -> None:
    existing = [
        {"block_id": "B0001", "text": "제목"},
        {"block_id": "B0002", "text": "10MB"},
        {"block_id": "B0003", "text": "PDF 지원"},
    ]

    result = compare_source_blocks(
        existing,
        [_block("B0001", "제목"), _block("B0002", "100MB"), _block("B0003", "PDF 지원")],
    )

    assert [block.block_id for block in result.blocks] == ["B0001", "B0002", "B0003"]
    assert [block.block_id for block in result.ingest_blocks] == ["B0002"]
    assert result.unchanged_block_ids == ["B0001", "B0003"]
    assert result.modified_block_ids == ["B0002"]
    assert result.invalidated_block_ids == ["B0002"]


def test_compare_source_blocks_preserves_moved_block_id() -> None:
    existing = [
        {"block_id": "B0001", "text": "첫째"},
        {"block_id": "B0002", "text": "둘째"},
        {"block_id": "B0003", "text": "셋째"},
    ]

    result = compare_source_blocks(
        existing,
        [_block("B0001", "둘째"), _block("B0002", "첫째"), _block("B0003", "셋째")],
    )

    assert [block.block_id for block in result.blocks] == ["B0002", "B0001", "B0003"]
    assert result.unchanged_block_ids == ["B0002", "B0001", "B0003"]
    assert result.moved_block_ids == ["B0002", "B0001"]
    assert result.ingest_blocks == []


def test_compare_source_blocks_does_not_reuse_deleted_id_for_added_block() -> None:
    existing = [
        {"block_id": "B0001", "text": "유지"},
        {"block_id": "B0002", "text": "삭제 A"},
        {"block_id": "B0003", "text": "삭제 B"},
    ]

    result = compare_source_blocks(
        existing,
        [_block("B0001", "유지"), _block("B0002", "추가 A"), _block("B0003", "추가 B")],
    )

    assert [block.block_id for block in result.blocks] == ["B0001", "B0004", "B0005"]
    assert result.added_block_ids == ["B0004", "B0005"]
    assert result.deleted_block_ids == ["B0002", "B0003"]
    assert result.invalidated_block_ids == ["B0002", "B0003"]


def test_compare_source_blocks_does_not_reuse_id_deleted_in_previous_run() -> None:
    existing = [
        {"block_id": "B0001", "text": "유지"},
        {"block_id": "B0002", "text": "이전에 삭제됨"},
    ]

    deleted = compare_source_blocks(existing, [_block("B0001", "유지")])
    current = [
        {"block_id": block.block_id, "text": block.text}
        for block in deleted.blocks
    ]
    added = compare_source_blocks(
        current,
        [_block("B0001", "유지"), _block("B0002", "새 문단")],
        previous_max_block_number=deleted.max_block_number,
    )

    assert deleted.max_block_number == 2
    assert [block.block_id for block in added.blocks] == ["B0001", "B0003"]
    assert added.added_block_ids == ["B0003"]
    assert added.max_block_number == 3


def test_compare_source_blocks_deletes_all_blocks_for_empty_document() -> None:
    existing = [
        {"block_id": "B0001", "text": "제목"},
        {"block_id": "B0002", "text": "본문"},
    ]

    result = compare_source_blocks(existing, [])

    assert result.blocks == []
    assert result.ingest_blocks == []
    assert result.deleted_block_ids == ["B0001", "B0002"]
    assert result.invalidated_block_ids == ["B0001", "B0002"]
