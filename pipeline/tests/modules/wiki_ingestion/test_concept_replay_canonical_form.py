"""기여 재생이 그리는 정식 페이지 형태: 정의 근거 표시는 한 번, 집계값은 합친 기여로, Related Concepts는 남은 링크로."""
from __future__ import annotations

from app.modules.wiki_generation.domain.text_utils import strip_citation_groups
from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import rebuild_concept_page


def _contribution(operation_id: str, concept: dict, **extra) -> dict:
    value = {
        "schema_version": 1, "operation_id": operation_id, "document_id": concept["source_document_ids"][0],
        "concept": {"slug": "alpha", "title": "Alpha", **concept},
        "evidence_units": [], "source_key_points": [], "links": [], "source_blocks": [], **extra,
    }
    return value


def _replay(*contributions: dict, titles=None):
    return rebuild_concept_page(
        [PageContribution("alpha-page", "concept", item["operation_id"], index, "", item)
         for index, item in enumerate(contributions, start=1)],
        concept_titles=titles,
    )


SEED = _contribution("seed", {
    "definition": "Alpha 정의", "display_reference_ids": ["B0003"], "anchor_reference_ids": ["B0003"],
    "mention_reference_ids": ["B0003", "B0004", "B0005"], "mention_count": 3, "importance_score": 15.75,
    "source_document_ids": ["doc-a"],
})
# 고치기 전 편입이 기존 페이지를 하위 항목 집으로 쓰며 남긴 개념: 본문에서 읽은 정의와 0 집계값.
POLLUTED_HOME = _contribution("home", {
    "definition": "Alpha 정의 [doc-a:B0003]", "display_reference_ids": [], "anchor_reference_ids": ["B0001"],
    "mention_reference_ids": [], "mention_count": 0, "importance_score": 0.0, "source_document_ids": ["doc-b"],
})


def test_strip_citation_groups_keeps_ordinary_brackets() -> None:
    assert strip_citation_groups("정의 [doc-a:B0003, doc_b:B0001] [doc-a:B0003]") == "정의"
    assert strip_citation_groups("비율 [16:9]과 [[slug|제목]], [링크](https://x.example)") == (
        "비율 [16:9]과 [[slug|제목]], [링크](https://x.example)"
    )


def test_stored_polluted_definition_and_zero_metrics_render_once_and_keep_merged_values() -> None:
    markdown = _replay(SEED, POLLUTED_HOME).markdown

    assert "## Definition\nAlpha 정의 [doc-a:B0003]\n" in markdown
    assert markdown.count("[doc-a:B0003] [doc-a:B0003]") == 0
    assert "mention_count: 3\n" in markdown and "- mention_count: 3\n" in markdown
    assert "importance_score: 15.75\n" in markdown
    # 다음 편입이 같은 일을 해도 세 번으로 늘지 않는다.
    again = _contribution("home-2", {**POLLUTED_HOME["concept"], "definition": "Alpha 정의 [doc-a:B0003] [doc-a:B0003]",
                                      "source_document_ids": ["doc-c"]})
    assert _replay(SEED, POLLUTED_HOME, again).markdown == markdown.replace("sources: doc-a, doc-b", "sources: doc-a, doc-b, doc-c")


def test_mention_count_follows_merged_mention_references() -> None:
    second = _contribution("second", {
        "definition": "", "mention_reference_ids": ["B0001", "B0002"], "mention_count": 2, "importance_score": 1.0,
        "source_document_ids": ["doc-b"],
    })

    markdown = _replay(SEED, second).markdown

    assert "mention_count: 5\n" in markdown and "importance_score: 15.75\n" in markdown


def test_related_concepts_come_from_surviving_concept_links() -> None:
    link = {"source": "concept:alpha", "target": "concept:beta", "relation": "concept_related_to"}
    gone = {"source": "concept:alpha", "target": "concept:gamma", "relation": "concept_related_to"}
    seed = {**SEED, "links": [link, gone, {**link, "target": "concept:alpha"}]}
    unlink = _contribution("unlink", {"source_document_ids": ["lint:unlink"]},
                           artifact_type="lint", content_action="none", removed_links=[gone])

    markdown = _replay(seed, unlink, titles=lambda slugs: {slug: slug.title() for slug in slugs}).markdown

    assert "## Related Concepts\n- [[beta|Beta]]\n\n" in markdown
    # 제목을 찾지 못한 대상은 그리지 않고, 남은 링크가 없으면 비었다고 쓴다.
    assert "## Related Concepts\n- 관련 개념 없음\n" in _replay(seed, unlink, titles=lambda slugs: {}).markdown
    assert "## Related Concepts\n- 관련 개념 없음\n" in _replay(SEED).markdown
