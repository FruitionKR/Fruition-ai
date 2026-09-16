from app.modules.query.application.ports import WikiRepositoryPort
from app.modules.query.domain.entities import SemanticQueryEmbedding, WikiPage, WikiPageLink
from app.modules.query.domain.scoring import TRAVERSABLE_RELATION_TYPES


class InMemoryWikiRepository(WikiRepositoryPort):
    def __init__(self, pages: list[WikiPage], links: list[WikiPageLink]) -> None:
        self._pages = pages
        self._links = links

    def list_candidate_pages(
        self,
        workspace_id: str,
        query: str,
        source_limit: int,
        concept_limit: int,
        semantic_query: SemanticQueryEmbedding | None = None,
    ) -> list[WikiPage]:
        del workspace_id, query, semantic_query
        source_pages = [page for page in self._pages if page.is_source][:source_limit]
        concept_pages = [page for page in self._pages if page.is_concept][:concept_limit]
        return source_pages + concept_pages

    def list_links_for_page_ids(
        self,
        workspace_id: str,
        page_ids: list[str],
        limit: int,
        excluded_page_ids: list[str] | None = None,
    ) -> list[WikiPageLink]:
        del workspace_id
        candidate_ids = set(page_ids)
        excluded_ids = set(excluded_page_ids or [])
        return [
            link
            for link in self._links
            if link.link_type in TRAVERSABLE_RELATION_TYPES
            and (
                (
                    link.from_page_id in candidate_ids
                    and link.to_page_id not in excluded_ids
                )
                or (
                    link.to_page_id in candidate_ids
                    and link.from_page_id not in excluded_ids
                )
            )
        ][:limit]

    def list_pages_by_ids(
        self,
        workspace_id: str,
        page_ids: list[str],
    ) -> list[WikiPage]:
        del workspace_id
        requested_ids = set(page_ids)
        return [page for page in self._pages if page.id in requested_ids]
