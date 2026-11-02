"""run_granularity 순수 함수와 subentry.json 형식 확인. 모델·스택 없이 돈다: python test_granularity.py"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from common import EVAL_DIR  # noqa: E402
from run_granularity import alignment, compare, head_groups, heads, names, summarize  # noqa: E402


def test_granularity() -> None:
    a = [{"slug": "d", "title": "Deployment", "aliases": ["디플로이먼트"]},
         {"slug": "r", "title": "Deployment Rollout", "aliases": []},
         {"slug": "l", "title": "Label Selector (Deployment)", "aliases": []}]
    b = [{"slug": "x", "title": "디플로이먼트", "aliases": []}, {"slug": "y", "title": "label selector", "aliases": []}]
    assert heads("Label Selector (Deployment)") == {"label", "deployment"}
    assert "labelselector" in names(a[2])
    assert head_groups(a) == {"deployment": ["Deployment", "Deployment Rollout", "Label Selector (Deployment)"]}
    assert alignment(a, b) == round(2 / 3, 3) and alignment(b, a) == 1.0 and alignment([], b) is None
    runs = {"m1": {"concepts": a, "summary": summarize(a, {"k8s": {"d", "r"}})},
            "m2": {"concepts": b, "summary": summarize(b, {"k8s": {"x"}})}}
    assert runs["m1"]["summary"]["unlinked_pages"] == 1
    got = compare(runs)
    assert got["pages"] == {"min": 2, "max": 3, "spread": 1} and got["documents"]["k8s"]["spread"] == 1
    assert set(got["alignment"]) == {"m1->m2", "m2->m1"}


def test_subentry_draft() -> None:
    data = json.loads((EVAL_DIR / "subentry.json").read_text())
    corpus = {p.stem: len(p.read_text(encoding="utf-8").splitlines()) for p in (EVAL_DIR / "corpus").glob("*.md")}
    cases = [(c, ("a", "b")) for c in data["pairs"]] + [(c, ("head", "detail")) for c in data["part_of"]]
    assert len({c["id"] for c, _ in cases}) == len(cases)
    for case, sides in cases:
        assert case.get("label", "part_of") in {*data["labels"], "part_of"} and case["reason"], case["id"]
        for side in sides:
            ref = case[side]
            start, end = ref["lines"]
            assert ref["name"] and 1 <= start <= end <= corpus[ref["source_id"]], case["id"]


if __name__ == "__main__":
    test_granularity()
    test_subentry_draft()
    print("ok")
