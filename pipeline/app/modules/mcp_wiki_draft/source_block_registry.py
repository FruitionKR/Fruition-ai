"""MCP 원문 블록을 웹 편입과 같은 문서별 영구 블록 ID(`source_blocks`)로 맞춘다.

웹 편입은 `MarkdownBlockExtractor`로 문서를 나누고 `compare_source_blocks`로 기존 블록 ID를 이어 쓴다.
MCP도 같은 분할·같은 비교를 써야 프론트 하이라이트(`?blocks=B0005`)와 기존 근거가 같은 블록을 가리킨다.
"""
from __future__ import annotations

from typing import Any

from app.modules.wiki_generation.domain.entities import SourceBlock
from app.modules.wiki_generation.domain.text_utils import normalize_space
from app.modules.wiki_generation.infrastructure.extract import MarkdownBlockExtractor
from app.modules.wiki_ingestion.domain.source_block_changes import SourceBlockChanges, compare_source_blocks
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_ingestion_repository import (
    _SOURCE_BLOCK_ORDER,
    latest_source_extraction_artifact_in,
)


class SourceBlocksChangedError(RuntimeError):
    """시작 시점에 받은 블록 ID가 현재 레지스트리로 다시 계산한 ID와 다르다(그 사이 다른 편입이 반영됐다)."""


def split_source_markdown(document_id: str, markdown: str) -> list[tuple[SourceBlock, str]]:
    """웹 편입과 같은 분할. 웹 편입은 저장된 Markdown 원문을 전처리 없이 `extract_text`에 넘긴다.

    돌려주는 값: (정규화 텍스트를 가진 블록, `_split_blocks`가 자른 원문 조각) 문서 순서 목록.
    """
    extractor = MarkdownBlockExtractor()
    _document, blocks = extractor.extract_text(markdown, source_path=document_id)
    raws = [raw for _type, raw, _start, _end in extractor._split_blocks(markdown)]
    if len(raws) != len(blocks):
        raise RuntimeError("markdown block split is not deterministic")
    return [(_with_document(block, document_id), raw) for block, raw in zip(blocks, raws, strict=True)]


def load_source_block_registry(conn, document_id: str) -> tuple[list[dict[str, Any]], int]:
    """현재 영구 블록(웹 재편입과 같은 순서)과 웹 재편입이 쓰는 이전 최대 블록 번호."""
    rows = [dict(row) for row in conn.execute(
        f"SELECT block_id,text FROM source_blocks WHERE document_id=%s ORDER BY {_SOURCE_BLOCK_ORDER}",
        (document_id,),
    ).fetchall()]
    artifact = latest_source_extraction_artifact_in(conn, document_id) or {}
    return rows, int(artifact.get("max_block_number", 0) or 0)


def resolve_source_blocks(conn, document_id: str, markdown: str) -> list[dict[str, Any]]:
    """`markdown`을 웹 편입이 지금 반영하면 받을 블록 ID로 나눈다. 아무것도 저장하지 않는다.

    레지스트리가 비어 있으면 B0001부터 순서대로다. 레지스트리가 있으면 웹 재편입과 같이 유지·이동·수정 블록은
    기존 ID를, 새 블록은 이전 최대 번호 다음 번호를 받는다(발행 전까지는 잠정 번호다).
    """
    split = split_source_markdown(document_id, markdown)
    existing, previous_max = load_source_block_registry(conn, document_id)
    changes = compare_source_blocks(existing, [block for block, _raw in split], previous_max)
    return [
        {
            "block_id": block.block_id,
            "ordinal": ordinal,
            "line_start": block.line_start,
            "line_end": block.line_end,
            "block_type": block.block_type,
            "text": block.text,
            "raw_text": raw,
        }
        for ordinal, (block, (_original, raw)) in enumerate(zip(changes.blocks, split, strict=True), start=1)
    ]


def match_snapshot_to_registry(
    document_id: str,
    snapshot_blocks: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    previous_max: int,
) -> SourceBlockChanges:
    """시작 시점 스냅샷(원문 텍스트·영구 ID)을 현재 레지스트리와 다시 비교한다. ID가 다르면 막는다.

    스냅샷 텍스트는 원문 조각이라 웹 레지스트리와 같은 정규화 텍스트로 비교한다.
    """
    incoming = [
        SourceBlock(
            document_id=document_id,
            block_id=str(item["block_id"]),
            source_reference_id=str(item["block_id"]),
            text=normalize_space(str(item["text"])),
            line_start=int(item.get("line_start") or 0),
            line_end=int(item.get("line_end") or 0),
            block_type=str(item.get("block_type") or "paragraph"),
        )
        for item in snapshot_blocks
    ]
    changes = compare_source_blocks(existing, incoming, previous_max)
    if [block.block_id for block in changes.blocks] != [str(item["block_id"]) for item in snapshot_blocks]:
        raise SourceBlocksChangedError("source block registry changed since the snapshot was taken")
    return changes


def _with_document(block: SourceBlock, document_id: str) -> SourceBlock:
    block.document_id = document_id
    return block
