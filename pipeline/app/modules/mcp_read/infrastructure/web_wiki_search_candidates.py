"""MCP 위키 검색 후보는 웹 근거 선택과 같은 저장소와 후보 순위를 그대로 쓴다.

workspace 범위에 MCP 전용 상한을 두지 않는다. MCP가 더하는 것은 결과 page의 version·content hash projection뿐이다.
"""

from __future__ import annotations

import hashlib
import json

from app.modules.mcp_read.application.wiki_search import WikiSearchCandidates, WikiSearchPage
from app.modules.query.application.ports import EmbeddingSearchPort, TextSearchPort
from app.modules.query.infrastructure.jev_evidence_selector import CANDIDATE_LIMIT, rank_candidates
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

    def list_candidates(self, *, workspace_id: str, query: str) -> WikiSearchCandidates:
        units = self._wiki_repository.list_workspace_embedding_units(workspace_id)
        ranked = rank_candidates(query, units, self._embedding_search, self._text_search, limit=CANDIDATE_LIMIT)
        unit_ids: dict[str, list[str]] = {}
        for unit, _ in units:
            unit_ids.setdefault(unit.page_id, []).append(unit.id)
        page_ids = sorted({page_id for candidate in ranked for page_id in candidate.page_ids})
        pages = {}
        for row in self._page_rows(workspace_id, page_ids):
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
        return WikiSearchCandidates(ranked=ranked, pages=pages)

    @staticmethod
    def _page_rows(workspace_id: str, page_ids: list[str]) -> list[dict]:
        if not page_ids:
            return []
        with database.connect() as conn:
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
