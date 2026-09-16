from __future__ import annotations

import hashlib

from app.modules.document_evaluation.application.ports import DocumentEvaluatorPort
from app.modules.document_evaluation.domain.entities import (
    AssembledDocumentBlock,
    DocumentEvaluationChunk,
    DocumentEvaluationJob,
)


def prepare_document_evaluation(
    markdown: str,
    pdf_reference: str,
    blocks: list[AssembledDocumentBlock],
    evaluator: DocumentEvaluatorPort | None = None,
    max_blocks: int = 12,
    max_chars: int = 6000,
) -> tuple[DocumentEvaluationJob, dict | None]:
    markdown_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    job_id = f"document-evaluation-{markdown_sha256[:16]}"
    job = DocumentEvaluationJob(
        job_id=job_id,
        markdown_sha256=markdown_sha256,
        pdf_reference=pdf_reference,
        chunks=tuple(_build_chunks(blocks, max_blocks=max_blocks, max_chars=max_chars)),
    )
    result = evaluator.evaluate(job) if evaluator is not None else None
    return job, result


def _build_chunks(
    blocks: list[AssembledDocumentBlock],
    max_blocks: int,
    max_chars: int,
) -> list[DocumentEvaluationChunk]:
    chunks = []
    current = []
    current_chars = 0
    for block in blocks:
        block_chars = len(block.markdown)
        if current and (len(current) >= max_blocks or current_chars + block_chars > max_chars):
            chunks.append(_chunk(len(chunks) + 1, current))
            current = []
            current_chars = 0
        current.append(block)
        current_chars += block_chars
    if current:
        chunks.append(_chunk(len(chunks) + 1, current))
    return chunks


def _chunk(index: int, blocks: list[AssembledDocumentBlock]) -> DocumentEvaluationChunk:
    return DocumentEvaluationChunk(
        chunk_id=f"chunk-{index:04d}",
        blocks=tuple(blocks),
    )
