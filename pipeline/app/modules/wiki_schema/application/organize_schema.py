from app.modules.wiki_schema.application.filter_schema_fragments import filter_schema_fragments
from app.modules.wiki_schema.application.ports import SchemaOrganizerPort
from app.modules.wiki_schema.domain.entities import SchemaFilterResult, SchemaIssue


class OrganizeSchemaUseCase:
    def __init__(self, organizer: SchemaOrganizerPort) -> None:
        self._organizer = organizer

    def execute(self, raw_markdown: str) -> SchemaFilterResult:
        if not raw_markdown.strip():
            raise ValueError("raw_markdown is required.")

        candidate = self._organizer.organize(raw_markdown)
        result = filter_schema_fragments(raw_markdown=raw_markdown, fragments=candidate.fragments)
        return SchemaFilterResult(
            fragments=result.fragments,
            issues=[
                *result.issues,
                *[
                    SchemaIssue(
                        severity="blocked",
                        category="organizer_blocked",
                        text=item,
                        reason="LLM organizer가 적용하지 않은 요청입니다.",
                        section="blocked_candidates",
                    )
                    for item in candidate.blocked_candidates
                ],
                *[
                    SchemaIssue(
                        severity="unclear",
                        category="unclear_preference",
                        text=item,
                        reason="사용자 확인이 필요한 모호한 설정입니다.",
                        section="unclear_items",
                    )
                    for item in candidate.unclear_items
                ],
            ],
        )
