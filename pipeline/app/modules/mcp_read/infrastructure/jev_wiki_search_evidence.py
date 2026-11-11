"""MCP 검색 후보(웹 근거 후보와 같다)를 웹 질의와 같은 Jev 근거 판정으로 고른다.

Jev를 쓸 수 없으면 빈 목록을 돌려 하이브리드 순서를 유지한다.
"""

from __future__ import annotations

import logging

from app.core.jev_client import JevClient, JevUnavailable
from app.modules.mcp_read.application.wiki_search import WikiSearchActor
from app.modules.model_usage.infrastructure.usage_ledger import usage_scope
from app.modules.query.infrastructure.jev_evidence_selector import EvidenceCandidate, select_evidence

logger = logging.getLogger(__name__)


class JevWikiSearchEvidence:
    def __init__(self, client: JevClient, *, max_selected: int) -> None:
        self._client = client
        self._max_selected = max_selected

    def select(self, actor: WikiSearchActor, query: str, candidates: list[EvidenceCandidate]) -> list[str]:
        if not candidates:
            return []
        command = {
            "run_id": actor.invocation_id,
            "workspace_id": actor.workspace_id,
            "user_id": actor.user_id,
            "kind": "mcp_wiki_search_jev_evidence",
        }
        try:
            with usage_scope(command):
                selected = select_evidence(self._client, query, candidates, max_selected=self._max_selected)
        except JevUnavailable as exc:
            logger.warning("[MCP Jev evidence 대체] %s", exc)
            return []
        return [candidate.unit.id for candidate in selected]
