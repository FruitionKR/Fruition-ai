from app.modules.query.domain.entities import QueryAnswer
from app.modules.query.interfaces.http.schemas import (
    GraphContextResponse,
    EvidenceSnippetResponse,
    QueryResponse,
    RelatedPageResponse,
    SourceReferenceResponse,
    TraversalEdgeResponse,
    TraversalPathResponse,
)


def _to_response(result: QueryAnswer) -> QueryResponse:
    return QueryResponse(
        answer=result.answer.content,
        updated_conversation_summary=result.updated_conversation_summary,
        related_pages=[
            RelatedPageResponse(
                id=item.page.id,
                page_type=item.page.page_type,
                title=item.page.title,
                slug=item.page.slug,
                relevance_score=item.score,
                role=item.role,
                depth=item.depth,
            )
            for item in result.related_pages
        ],
        evidence_snippets=[
            EvidenceSnippetResponse(
                rank=snippet.rank,
                source_document_id=snippet.source_document_id,
                source_block_ids=snippet.source_block_ids,
                source_refs=[
                    SourceReferenceResponse(
                        source_document_id=ref.source_document_id,
                        source_block_id=ref.source_block_id,
                    )
                    for ref in snippet.source_refs
                ],
                text=snippet.text,
            )
            for snippet in result.evidence_snippets
        ],
        graph_context=GraphContextResponse(
            nodes=[
                RelatedPageResponse(
                    id=item.page.id,
                    page_type=item.page.page_type,
                    title=item.page.title,
                    slug=item.page.slug,
                    relevance_score=item.score,
                    role=item.role,
                    depth=item.depth,
                )
                for item in result.graph_context.nodes
            ],
            edges=[
                TraversalEdgeResponse(
                    from_page_id=edge.from_page_id,
                    to_page_id=edge.to_page_id,
                    link_type=edge.link_type,
                    role=edge.role,
                    score=edge.score,
                )
                for edge in result.graph_context.edges
            ],
        ),
        traversal_paths=[
            TraversalPathResponse(
                path_id=path.path_id,
                role=path.role,
                used_for_answer=path.used_for_answer,
                score=path.score,
                stop_reason=path.stop_reason,
                nodes=path.nodes,
                edges=[
                    TraversalEdgeResponse(
                        from_page_id=edge.from_page_id,
                        to_page_id=edge.to_page_id,
                        link_type=edge.link_type,
                        role=edge.role,
                        score=edge.score,
                    )
                    for edge in path.edges
                ],
            )
            for path in result.traversal_paths
        ],
        web_search_requested=result.web_search.requested,
        web_search_executed=result.web_search.executed,
        result_count=result.web_search.result_count,
        error_code=result.web_search.error_code,
    )
