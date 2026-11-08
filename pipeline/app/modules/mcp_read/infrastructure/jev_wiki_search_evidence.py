"""MCP 검색이 이미 추린 후보만 웹 질의와 같은 Jev 근거 판정으로 고른다.

workspace 전체 unit을 다시 읽지 않는다. 후보마다 snippet을 만든 embedding unit 하나를 판정 후보로 쓰고,
본문·블록·문서가 같은 후보는 웹 선택기처럼 하나로 묶는다. Jev를 쓸 수 없으면 빈 목록을 돌려 하이브리드 순서를 유지한다.
"""

from __future__ import annotations

import logging

from app.core.jev_client import JevClient, JevUnavailable
from app.modules.mcp_read.application.wiki_search import WikiSearchActor, WikiSearchCandidate
from app.modules.model_usage.infrastructure.usage_ledger import usage_scope
from app.modules.query.application.source_references import source_references_from_ids
from app.modules.query.domain.entities import WikiEmbeddingUnit
from app.modules.query.infrastructure.jev_evidence_selector import EvidenceCandidate, select_evidence

logger = logging.getLogger(__name__)


class JevWikiSearchEvidence:
    def __init__(self, client: JevClient, *, max_selected: int) -> None:
        self._client = client
        self._max_selected = max_selected

    def select(self, actor: WikiSearchActor, query: str, candidates: list[WikiSearchCandidate]) -> list[str]:
        groups: dict[tuple[str, tuple[str, ...], str], list[WikiSearchCandidate]] = {}
        for candidate in candidates:
            text = candidate.embedding_text.strip()
            if (
                not candidate.unit_id
                or not text
                or not candidate.source_document_id
                or not source_references_from_ids(list(candidate.block_refs), candidate.source_document_id)
            ):
                continue
            key = (text, tuple(sorted(candidate.block_refs)), candidate.source_document_id)
            groups.setdefault(key, []).append(candidate)
        if not groups:
            return []
        members_by_unit_id: dict[str, list[WikiSearchCandidate]] = {}
        evidence: list[EvidenceCandidate] = []
        for members in groups.values():
            first = min(members, key=lambda member: member.unit_id)
            members_by_unit_id[first.unit_id] = members
            evidence.append(
                EvidenceCandidate(
                    unit=WikiEmbeddingUnit(
                        id=first.unit_id,
                        page_id=first.page_id,
                        source_document_id=first.source_document_id,
                        unit_type=first.unit_type,
                        source_block_ids=list(first.block_refs),
                        text=first.embedding_text,
                        weight=first.unit_weight,
                    ),
                    source_titles=tuple(sorted({member.title for member in members if member.title})),
                    page_ids=tuple(sorted({member.page_id for member in members})),
                )
            )
        command = {
            "run_id": actor.invocation_id,
            "workspace_id": actor.workspace_id,
            "user_id": actor.user_id,
            "kind": "mcp_wiki_search_jev_evidence",
        }
        try:
            with usage_scope(command):
                selected = select_evidence(self._client, query, evidence, max_selected=self._max_selected)
        except JevUnavailable as exc:
            logger.warning("[MCP Jev evidence 대체] %s", exc)
            return []
        # 같은 근거를 가진 page는 하이브리드 순서대로 함께 앞으로 둔다.
        return [member.page_id for item in selected for member in members_by_unit_id[item.unit.id]]
