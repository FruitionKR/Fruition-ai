from app.modules.wiki_ingestion.domain.concept_split_rules import (
    MERGE_MAX_CHILDREN,
    MERGE_MAX_EVIDENCE,
    MIN_CLUSTER_SUBCATEGORIES,
    MIN_PAGE_ITEMS,
    SPLIT_MIN_CHILDREN,
    SPLIT_MIN_EVIDENCE,
    EntryDecision,
    EntryNode,
    cluster_reaches_promotion,
    decide_entry_changes,
    reaches_page_threshold,
)


def _entry(entry_id="e1", *, children=0, evidence=1, refs=(), **kwargs):
    return EntryNode(
        entry_id=entry_id,
        evidence_count=evidence,
        child_entry_ids=tuple(f"{entry_id}-c{i}" for i in range(children)),
        reference_page_ids=frozenset(refs),
        **kwargs,
    )


def _actions(*entries):
    return [(d.action, d.entry_id) for d in decide_entry_changes(entries)]


def test_페이지와_클러스터_승격_문턱():
    assert not reaches_page_threshold(MIN_PAGE_ITEMS - 1)
    assert reaches_page_threshold(MIN_PAGE_ITEMS)
    assert not cluster_reaches_promotion(MIN_CLUSTER_SUBCATEGORIES - 1)
    assert cluster_reaches_promotion(MIN_CLUSTER_SUBCATEGORIES)


def test_분리는_자식_근거_중_하나만_넘어도_한다():
    assert _actions(_entry(children=SPLIT_MIN_CHILDREN - 1, evidence=SPLIT_MIN_EVIDENCE - 1)) == []
    assert _actions(_entry(children=SPLIT_MIN_CHILDREN)) == [("split", "e1")]
    assert _actions(_entry(evidence=SPLIT_MIN_EVIDENCE)) == [("split", "e1")]


def test_다시_합치기는_모든_기준_아래일_때만_한다():
    assert _actions(_entry(children=MERGE_MAX_CHILDREN, is_child_page=True)) == [("merge_back", "e1")]
    assert _actions(_entry(children=MERGE_MAX_CHILDREN, evidence=MERGE_MAX_EVIDENCE + 1, is_child_page=True)) == []
    assert _actions(_entry(children=MERGE_MAX_CHILDREN + 1, is_child_page=True)) == []


def test_분리와_합치기_사이_구간은_그대로_둔다():
    assert MERGE_MAX_CHILDREN < SPLIT_MIN_CHILDREN and MERGE_MAX_EVIDENCE < SPLIT_MIN_EVIDENCE
    for children in range(MERGE_MAX_CHILDREN + 1, SPLIT_MIN_CHILDREN):
        assert _actions(_entry(children=children)) == []
        assert _actions(_entry(children=children, is_child_page=True)) == []
    for evidence in range(MERGE_MAX_EVIDENCE + 1, SPLIT_MIN_EVIDENCE):
        assert _actions(_entry(evidence=evidence)) == []
        assert _actions(_entry(evidence=evidence, is_child_page=True)) == []


def test_롤백_직후에는_합치지_않는다():
    assert _actions(_entry(is_child_page=True, restored_recently=True)) == []


def test_여러_부모에_걸린_항목은_분리보다_승격이_먼저다():
    assert _actions(_entry(refs=("p1",))) == []
    assert _actions(_entry(refs=("p1", "p2"), children=SPLIT_MIN_CHILDREN)) == [("promote", "e1")]
    assert _actions(_entry(refs=("p1", "p2"), is_child_page=True)) == []


def test_결과는_입력_순서와_무관하게_entry_id_순이다():
    entries = [_entry("b", children=SPLIT_MIN_CHILDREN), _entry("a", is_child_page=True), _entry("c")]
    assert decide_entry_changes(entries) == decide_entry_changes(reversed(entries)) == [
        EntryDecision("merge_back", "a"),
        EntryDecision("split", "b"),
    ]
