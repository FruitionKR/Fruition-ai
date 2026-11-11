"""MCP 위키 검색 후보는 웹 근거 선택과 같은 저장소와 후보 순위를 그대로 쓴다.

MCP가 더하는 것은 외부 호출 경계뿐이다. ADR 0046대로 후보 조회 단계부터 unit 행 수·총 text bytes·deadline
상한을 걸고(넘으면 일부만 검색하지 않고 거절), 결과 page의 version·content hash를 만든다.

후보 묶기와 점수는 한 번만 계산하고 두 순서로 쓴다.
- Jev 판정 후보: 웹 Jev 근거 선택과 같은 BGE-M3 0.75 + BM25 0.25(ADR-0027) 상위 CANDIDATE_LIMIT(200)개
- 기본 결과 순서: 웹이 Jev 없이 근거 점수를 매기는 질문 언어별 비중(ADR 0009, evidence_embedding_weight) 상위 CANDIDATE_LIMIT개
"""

from __future__ import annotations

import hashlib
import json

from psycopg.errors import QueryCanceled

from app.core.pipeline_control import DeadlineExceededError, ensure_task_active

from app.modules.mcp_read.application.wiki_search import WikiSearchCandidates, WikiSearchError, WikiSearchPage
from app.modules.query.application.ports import EmbeddingSearchPort, TextSearchPort
from app.modules.query.domain.scoring import evidence_embedding_weight
from app.modules.query.infrastructure.jev_evidence_selector import (
    CANDIDATE_EMBEDDING_WEIGHT,
    CANDIDATE_LIMIT,
    group_candidates,
    order_candidates,
)
from app.modules.query.infrastructure.postgres_wiki_repository import PostgresWikiRepository
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database


class WebWikiSearchCandidates:
    def __init__(
        self,
        embedding_search: EmbeddingSearchPort,
        text_search: TextSearchPort,
        wiki_repository: PostgresWikiRepository | None = None,
    ) -> None:
        self._embedding_search = embedding_search
        self._text_search = text_search
        self._wiki_repository = wiki_repository or PostgresWikiRepository()

    def list_candidates(
        self,
        *,
        workspace_id: str,
        query: str,
        unit_limit: int,
        text_bytes_limit: int,
        statement_timeout_ms: int,
    ) -> WikiSearchCandidates:
        try:
            units = self._wiki_repository.list_workspace_embedding_units(
                workspace_id, limit=unit_limit + 1, statement_timeout_ms=statement_timeout_ms
            )
        except QueryCanceled:
            raise DeadlineExceededError("Wiki candidate query exceeded the request deadline.") from None
        if len(units) > unit_limit or sum(len(unit.text.encode("utf-8")) for unit, _ in units) > text_bytes_limit:
            raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki search scope exceeds configured capacity.")
        ensure_task_active()
        grouped = group_candidates(units)
        texts = [candidate.unit.text for candidate in grouped]
        dense = self._embedding_search.score(query, texts) if grouped else []
        lexical = self._text_search.score(query, texts) if grouped else []
        ranked = order_candidates(
            grouped, dense, lexical, embedding_weight=evidence_embedding_weight(query), limit=CANDIDATE_LIMIT
        )
        judged = order_candidates(
            grouped, dense, lexical, embedding_weight=CANDIDATE_EMBEDDING_WEIGHT, limit=CANDIDATE_LIMIT
        )
        ensure_task_active()
        unit_ids: dict[str, list[str]] = {}
        for unit, _ in units:
            unit_ids.setdefault(unit.page_id, []).append(unit.id)
        page_ids = sorted({page_id for candidate in (*ranked, *judged) for page_id in candidate.page_ids})
        pages = {}
        for row in self._page_rows(workspace_id, page_ids, statement_timeout_ms):
            page_id = str(row["id"])
            title = str(row["title"])
            # unit ID는 page·종류·block·본문의 해시다. 제목과 함께 묶어 검색 표현이 바뀌면 hash도 바뀐다.
            digest = hashlib.sha256(
                json.dumps([title, sorted(unit_ids.get(page_id, []))], ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            pages[page_id] = WikiSearchPage(
                page_id=page_id,
                page_type=str(row["page_type"]),
                title=title,
                version=int(row["version"]),
                content_hash=f"sha256:{digest}",
            )
        return WikiSearchCandidates(ranked=ranked, judged=judged, pages=pages)

    @staticmethod
    def _page_rows(workspace_id: str, page_ids: list[str], statement_timeout_ms: int) -> list[dict]:
        if not page_ids:
            return []
        with database.connect() as conn:
            conn.execute("SELECT set_config('statement_timeout', %s, true)", (f"{statement_timeout_ms}ms",))
            return conn.execute(
                """
                SELECT id, page_type, title,
                       greatest(1, floor(extract(epoch FROM updated_at) * 1000000)::bigint) AS version
                FROM wiki_pages
                WHERE workspace_id = %s
                  AND status = 'active'
                  AND id = ANY(%s)
                """,
                (workspace_id, page_ids),
            ).fetchall()
