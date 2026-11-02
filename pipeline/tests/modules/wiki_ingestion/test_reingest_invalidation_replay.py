"""재편입이 무효화한 블록의 옛 근거를 재생에서 뺀다."""
from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import rebuild_concept_page


# 근거 단위에 related_concept_slugs가 없어도(실제 편입 기여에 흔함) 그 개념의 근거로 그려야 한다.
def _ingest(operation_id, units, invalidated=(), *, anchors=None, entry_changes=None):
    contribution = {
        "schema_version": 1,
        "operation_id": operation_id,
        "document_id": "doc",
        "concept": {
            "slug": "pod",
            "title": "파드",
            "definition": "가장 작은 배포 단위",
            "anchor_reference_ids": anchors if anchors is not None else [r for _, _, refs in units for r in refs],
            "evidence_claim_ids": [f"{operation_id}:{ev}" for ev, _, _ in units],
            "source_document_ids": ["doc"],
        },
        "evidence_units": [
            {"evidence_id": f"{operation_id}:{ev}", "claim": claim, "anchor_reference_ids": refs, "source_document_id": "doc"}
            for ev, claim, refs in units
        ],
        "links": [],
        "source_blocks": [],
    }
    if invalidated:
        contribution["invalidated_reference_ids"] = list(invalidated)
    if entry_changes is not None:
        contribution["entry_changes"] = entry_changes
    return contribution


def _rebuild(*contributions):
    return rebuild_concept_page([
        PageContribution(page_id="P", page_type="concept", operation_id=c["operation_id"], sequence=index,
                         markdown_key=f"k{index}", contribution=c)
        for index, c in enumerate(contributions, start=1)
    ])


def test_later_reingest_drops_evidence_of_modified_block_and_keeps_unchanged() -> None:
    first = _ingest("op1", [("ev1", "옛 문장", ["doc:B5"]), ("ev2", "그대로인 문장", ["doc:B1"]), ("ev3", "겹친 문장", ["doc:B1", "doc:B5"])])
    second = _ingest("op2", [("ev1", "새 문장", ["doc:B5"])], invalidated=["doc:B5"])
    page = _rebuild(first, second)
    assert "옛 문장" not in page.markdown and "겹친 문장" not in page.markdown
    assert "그대로인 문장" in page.markdown and "새 문장" in page.markdown
    # 같은 블록을 다시 고쳐도 줄이 쌓이지 않는다.
    third = _ingest("op3", [("ev1", "세 번째 문장", ["doc:B5"])], invalidated=["doc:B5"])
    page = _rebuild(first, second, third)
    assert "새 문장" not in page.markdown and page.markdown.count("doc:B5") == 1


def test_invalidation_only_record_cleans_page_without_new_evidence_and_deletes_when_nothing_remains() -> None:
    first = _ingest("op1", [("ev1", "옛 문장", ["doc:B5"]), ("ev2", "그대로인 문장", ["doc:B1"])])
    invalidation = {"schema_version": 1, "operation_id": "op2", "document_id": "doc", "artifact_type": "invalidation",
                    "invalidated_reference_ids": ["doc:B5"]}
    page = _rebuild(first, invalidation)
    assert not page.deleted and "옛 문장" not in page.markdown and "그대로인 문장" in page.markdown
    only_stale = _ingest("op1", [("ev1", "옛 문장", ["doc:B5"])])
    assert _rebuild(only_stale, invalidation).deleted
    # 인용 없이 이름만 나온 개념은 무효화와 무관하게 페이지를 받친다.
    assert not _rebuild(_ingest("op1", [], anchors=[]), invalidation).deleted


def test_sub_entry_citing_only_modified_block_is_removed_with_its_evidence() -> None:
    entry = {"op": "add", "entry_id": "entry:x", "name": "옛 항목", "definition": "", "anchor_reference_ids": ["doc:B5"],
             "evidence_claim_ids": ["op1:ev1"]}
    kept = {"op": "add", "entry_id": "entry:y", "name": "남는 항목", "definition": "", "anchor_reference_ids": ["doc:B1", "doc:B5"],
            "evidence_claim_ids": []}
    first = _ingest("op1", [("ev1", "옛 문장", ["doc:B5"]), ("ev2", "그대로", ["doc:B1"])], entry_changes=[entry, kept])
    second = _ingest("op2", [], invalidated=["doc:B5"], anchors=[])
    page = _rebuild(first, second)
    assert [e["entry_id"] for e in page.entries] == ["entry:y"]
    assert page.entries[0]["anchor_reference_ids"] == ["doc:B1"]
