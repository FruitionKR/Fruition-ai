from app.modules.wiki_ingestion.domain.concept_split_rules import (
    MERGE_MAX_CHILDREN,
    MIN_CLUSTER_SUBCATEGORIES,
    MIN_PAGE_ITEMS,
    SPLIT_MIN_CHILDREN,
    EntryDecision,
    EntryNode,
    cluster_reaches_promotion,
    decide_entry_changes,
    reaches_page_threshold,
)


def _entry(entry_id="e1", *, children=0, refs=(), **kwargs):
    return EntryNode(
        entry_id=entry_id,
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


def test_분리와_합치기_사이_구간은_그대로_둔다():
    assert MERGE_MAX_CHILDREN < SPLIT_MIN_CHILDREN
    for children in range(MERGE_MAX_CHILDREN + 1, SPLIT_MIN_CHILDREN):
        assert _actions(_entry(children=children)) == []
        assert _actions(_entry(children=children, is_child_page=True)) == []


def test_기준값_경계_분리_5_합치기_2():
    # ADR 0042 최종 기준: 분리 C=5, 합치기 C_low=2. 근거 수는 보지 않는다. 되돌린 직후 추가 지연은 없다.
    assert (SPLIT_MIN_CHILDREN, MERGE_MAX_CHILDREN) == (5, 2)
    assert _actions(_entry(children=4)) == []
    assert _actions(_entry(children=5)) == [("split", "e1")]
    assert _actions(_entry(children=0)) == []
    assert _actions(_entry(children=2, is_child_page=True)) == [("merge_back", "e1")]
    assert _actions(_entry(children=3, is_child_page=True)) == []


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
