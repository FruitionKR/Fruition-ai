from app.modules.wiki_schema.domain.entities import SchemaFeature, SchemaFragments


FEATURE_SECTIONS: dict[SchemaFeature, tuple[str, ...]] = {
    "query": ("global_markdown", "query_markdown"),
    "ingest": ("global_markdown", "ingest_markdown", "concept_markdown"),
    "edit": ("global_markdown", "edit_markdown"),
    "concept": ("global_markdown", "concept_markdown"),
    "template": ("global_markdown", "template_markdown"),
}


def select_schema_fragments(fragments: SchemaFragments, feature: SchemaFeature) -> str:
    sections = FEATURE_SECTIONS[feature]
    selected = [getattr(fragments, section).strip() for section in sections]
    return "\n\n".join(section for section in selected if section)
