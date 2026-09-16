SCHEMA_SECTIONS: tuple[tuple[str, str], ...] = (
    ("global_markdown", "공통 작성 기준"),
    ("query_markdown", "질문 답변 기준"),
    ("ingest_markdown", "문서 수집 기준"),
    ("edit_markdown", "문서 편집 기준"),
    ("concept_markdown", "Concept 기준"),
    ("template_markdown", "Template 기준"),
)

SCHEMA_SECTION_NAMES: tuple[str, ...] = tuple(field_name for field_name, _ in SCHEMA_SECTIONS)
