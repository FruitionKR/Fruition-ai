from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]


@dataclass
class SourceDocument:
    document_id: str
    title: str
    source_path: str
    content_sha1: str


@dataclass
class SourceBlock:
    document_id: str
    block_id: str           # short global anchor, e.g. B0001
    source_reference_id: str # stable DB/source id, e.g. ref_abcd_md_b0001
    text: str
    line_start: int
    line_end: int
    section_path: List[str] = field(default_factory=list)
    block_type: str = "paragraph"

    def to_llm_line(self) -> str:
        return f"[{self.block_id}] {self.text.strip()}"


@dataclass
class SemanticPacket:
    chunk_id: str
    document_id: str
    block_ids: List[str]
    text: str


@dataclass
class NormalizedConcept:
    slug: str
    title: str
    aliases: List[str]
    definition: str
    why_page_worthy: str
    anchor_reference_ids: List[str]
    mention_reference_ids: List[str] = field(default_factory=list)
    display_reference_ids: List[str] = field(default_factory=list)
    source_document_ids: List[str] = field(default_factory=list)
    evidence_claim_ids: List[str] = field(default_factory=list)
    mention_count: int = 0
    importance_score: float = 0.0


@dataclass
class NormalizedEvidence:
    evidence_id: str
    claim: str
    anchor_reference_ids: List[str]
    related_concept_slugs: List[str]
    confidence: float
    source_document_id: str


def dataclass_to_dict(obj: Any) -> JsonDict:
    return asdict(obj)
