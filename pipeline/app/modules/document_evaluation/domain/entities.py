from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AssembledDocumentBlock:
    block_id: str
    block_type: str
    page: int
    bbox: tuple[float, float, float, float]
    markdown: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "block_type": self.block_type,
            "page": self.page,
            "bbox": list(self.bbox),
            "markdown": self.markdown,
        }


@dataclass(frozen=True)
class DocumentEvaluationChunk:
    chunk_id: str
    blocks: tuple[AssembledDocumentBlock, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "blocks": [block.to_dict() for block in self.blocks],
        }


@dataclass(frozen=True)
class DocumentEvaluationJob:
    job_id: str
    markdown_sha256: str
    pdf_reference: str
    chunks: tuple[DocumentEvaluationChunk, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "document-evaluation-job.v1",
            "job_id": self.job_id,
            "status": "pending_external_evaluator",
            "source": {
                "markdown_sha256": self.markdown_sha256,
                "pdf_reference": self.pdf_reference,
            },
            "constraints": {
                "evaluate_only_provided_blocks": True,
                "do_not_rewrite_without_crop_evidence": True,
                "max_crop_attempts_per_block": 3,
                "mandatory_crop_review_block_types": ["equation_candidate"],
                "mandatory_crop_review_markers": ["복원 필요"],
            },
            "crop_tool_contract": {
                "name": "render_block_crop",
                "arguments": ["job_id", "block_id"],
                "result": ["image", "page", "bbox"],
            },
            "result_contract": {
                "chunk_id": "string",
                "accepted_block_ids": ["block_id"],
                "crop_requests": [
                    {
                        "block_id": "string",
                        "reason": "string",
                    }
                ],
                "unresolved": [
                    {
                        "block_id": "string",
                        "reason": "string",
                    }
                ],
            },
            "chunks": [chunk.to_dict() for chunk in self.chunks],
        }
