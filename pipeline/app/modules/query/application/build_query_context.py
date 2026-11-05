from app.modules.query.application.answer_context_formatter import AnswerContextFormatter
from app.modules.query.application.evidence_selector import EvidenceSelector
from app.modules.query.application.ports import EmbeddingSearchPort, EvidenceSelectorPort, TextSearchPort
from dataclasses import replace

from app.modules.query.domain.entities import (
    EvidenceSnippet,
    GraphContext,
    OutputLanguage,
    QueryContext,
    ResponseLength,
    RetrievedPage,
    TraversalPath,
    WikiEmbeddingUnit,
)


class BuildQueryContextUseCase:
    def __init__(
        self,
        embedding_search: EmbeddingSearchPort | None = None,
        text_search: TextSearchPort | None = None,
        max_related_pages: int = 8,
        max_paths: int = 5,
        max_paragraphs_per_page: int = 4,
        max_paragraph_chars: int = 900,
        min_evidence_score: float = 0.0,
        evidence_relative_score_floor: float = 0.85,
        max_evidence_snippets: int = 8,
        answer_context_formatter: AnswerContextFormatter | None = None,
        evidence_selector: EvidenceSelectorPort | None = None,
    ) -> None:
        self._evidence_selector = evidence_selector or EvidenceSelector(
            embedding_search=embedding_search,
            text_search=text_search,
            max_related_pages=max_related_pages,
            max_paragraphs_per_page=max_paragraphs_per_page,
            min_evidence_score=min_evidence_score,
            evidence_relative_score_floor=evidence_relative_score_floor,
            max_evidence_snippets=max_evidence_snippets,
        )
        self._answer_context_formatter = answer_context_formatter or AnswerContextFormatter(
            max_related_pages=max_related_pages,
            max_paths=max_paths,
            max_paragraph_chars=max_paragraph_chars,
        )

    def execute(
        self,
        question: str,
        related_pages: list[RetrievedPage],
        graph_context: GraphContext,
        traversal_paths: list[TraversalPath],
        original_question: str | None = None,
        evidence_question: str | None = None,
        answer_mode: str | None = None,
        embedding_units_by_page_id: dict[str, list[WikiEmbeddingUnit]] | None = None,
        workspace_id: str | None = None,
        user_id: str | None = None,
        output_language: OutputLanguage | None = None,
        response_length: ResponseLength | None = None,
        allow_web_search: bool | None = None,
        pinned_evidence: tuple[EvidenceSnippet, ...] = (),
    ) -> QueryContext:
        selected = self._evidence_selector.select(
            evidence_question or question,
            related_pages,
            embedding_units_by_page_id or {},
            workspace_id=workspace_id,
        )
        evidence_snippets = _with_pinned_evidence(pinned_evidence, selected)
        return QueryContext(
            question=question,
            graph_context=graph_context,
            traversal_paths=traversal_paths,
            related_pages=related_pages,
            evidence_snippets=evidence_snippets,
            answer_context=self._answer_context_formatter.format(
                question=question,
                related_pages=related_pages,
                traversal_paths=traversal_paths,
                evidence_snippets=evidence_snippets,
                original_question=original_question,
                answer_mode=answer_mode,
            ),
            workspace_id=workspace_id,
            user_id=user_id,
            output_language=output_language,
            response_length=response_length,
            allow_web_search=allow_web_search,
            pinned_evidence=pinned_evidence,
        )


def _with_pinned_evidence(
    pinned: tuple[EvidenceSnippet, ...],
    selected: list[EvidenceSnippet],
) -> list[EvidenceSnippet]:
    """고정 근거를 앞에 두고 인용 번호(rank)를 1부터 다시 매긴다."""
    if not pinned:
        return selected
    return [replace(snippet, rank=rank) for rank, snippet in enumerate((*pinned, *selected), start=1)]
