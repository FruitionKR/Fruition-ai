from app.modules.wiki_generation.infrastructure.assemble import _source_mention_line
from app.modules.wiki_generation.infrastructure.extract import MarkdownBlockExtractor
from app.modules.wiki_generation.infrastructure.meaning_cluster_artifact import MeaningClusterArtifactAssembler
from app.modules.wiki_generation.infrastructure.normalize import SemanticNormalizer
from app.modules.wiki_ingestion.infrastructure.embedding_units import extract_embedding_units


def _normalized_with_mention() -> dict:
    document, blocks = MarkdownBlockExtractor().blocks_from_records(
        [{"block_id": "B0001", "text": "디플로이먼트는 kubectl로   롤백한다."}],
        text="디플로이먼트는 kubectl로 롤백한다.\n",
        source_path="k8s.md",
        fallback_title="k8s",
    )
    note = {
        "chunk_id": "chunk_0001",
        # 모델이 설명을 붙여도 언급에는 남기지 않는다.
        "mentions": [{"name": "kubectl", "slug_hint": "kubectl", "context": "모델이 쓴 설명", "evidence_block_ids": ["B0001"]}],
    }
    return SemanticNormalizer(document, blocks).normalize_notes([note])


def test_mention_keeps_name_and_source_text_without_description() -> None:
    mention = _normalized_with_mention()["mentions"][0]

    assert "context" not in mention
    assert mention["source_text"] == "디플로이먼트는 kubectl로 롤백한다."
    assert _source_mention_line(mention, "doc").startswith("- kubectl")
    assert "모델이 쓴 설명" not in _source_mention_line(mention, "doc")


def test_mention_cluster_candidate_uses_source_text() -> None:
    candidates = MeaningClusterArtifactAssembler().candidate_claims(_normalized_with_mention())

    assert [candidate["claim"] for candidate in candidates] == ["kubectl - 디플로이먼트는 kubectl로 롤백한다."]


def test_mention_lines_are_not_embedding_units() -> None:
    markdown = "## Mentions\n- kubectl [doc:B0001]\n\n## Key Points\n- 롤백은 kubectl로 한다. [doc:B0001]\n"

    units = extract_embedding_units(markdown)

    assert [unit["unit_type"] for unit in units] == ["key_point"]
