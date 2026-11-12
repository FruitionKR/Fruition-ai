import pytest
from pydantic import ValidationError

from app.modules.mcp_wiki_draft import calculation
from app.modules.mcp_wiki_draft.interfaces.http.schemas import ContributionRequest


def contribution(name, target=None):
    value = {
        "concept_name": name,
        "statements": [{"statement_id": name, "text": "Evidence", "evidence": [{"block_id": "B0001"}]}],
        "links": [],
    }
    if target is not None:
        value["merge_target_page_id"] = target
    return value


def manifest(submissions, index):
    """웹 편입의 AI 뒤 단계로 만든 manifest. 개념 목록은 웹 `list_active_concept_index` 모양이다."""
    return calculation.build_ingest_manifest(
        run={"document_id": "doc", "source_display_name": "Doc", "source_content_hash": "sha256:" + "0" * 64,
             "user_id": "user", "workspace_id": "workspace"},
        submissions=submissions,
        source_blocks=[{"block_id": "B0001", "ordinal": 1, "text": "Evidence"}],
        current_source_blocks=[],
        concept_index=[{"page_id": item["id"], "identity_aliases": [], **item} for item in index],
        operation_id="operation",
    )


def ledger(result):
    return {(item["slug"], item["title"]) for item in result["normalized"]["concept_ledger"]}


def test_name_match_precedes_client_target_and_target_preserves_canonical_title():
    index = [
        {"id": "canonical", "title": "Authentication", "slug": "authentication", "status": "active"},
        {"id": "homonym", "title": "Login", "slug": "login", "status": "active"},
    ]
    result = manifest([
        {"window_ordinal": 1, "contributions": [contribution("Login", "canonical")]},
        {"window_ordinal": 2, "contributions": [contribution("Sign in", "canonical")]},
    ], index)
    # 웹 편입과 같이 코드 이름 일치가 먼저다. 이름이 맞지 않는 개념에만 외부 AI 대상을 쓴다.
    assert ledger(result) == {("login", "Login"), ("authentication", "Authentication")}


@pytest.mark.parametrize("index", [[], [{"id": "target", "title": "Target", "slug": "target", "status": "deleted"}]])
def test_unavailable_target_never_falls_back_to_a_new_page(index):
    with pytest.raises(calculation.SemanticMergeError):
        manifest([{"window_ordinal": 1, "contributions": [contribution("Other name", "target")]}], index)


def test_cross_window_decision_is_preserved_and_conflicting_targets_are_rejected():
    submissions = [
        {"window_ordinal": 1, "contributions": [contribution("Login")]},
        {"window_ordinal": 2, "contributions": [contribution("Login", "target")]},
    ]
    combined = calculation._combined_concepts(submissions)
    assert combined[0]["merge_target_page_id"] == "target"
    assert [item["statement_id"] for item in combined[0]["statements"]] == ["1:Login", "2:Login"]
    submissions.append({"window_ordinal": 3, "contributions": [contribution("Login", "different")]})
    with pytest.raises(calculation.SemanticMergeError):
        calculation._combined_concepts(submissions)


def test_target_schema_is_bounded_and_legacy_contribution_remains_valid():
    value = {"concept_name": "Login", "statements": [], "links": []}
    assert ContributionRequest(**value).merge_target_page_id is None
    for invalid in ("", " padded", "https://example.com", "a" * 256, 123):
        with pytest.raises(ValidationError):
            ContributionRequest(**value, merge_target_page_id=invalid)


class FakeJudge:
    def __init__(self, resolutions=None, error=None):
        self.resolutions, self.error, self.calls = resolutions or {}, error, []

    def judge_concepts(self, concepts, pool):
        self.calls.append(([item["title"] for item in concepts], [item["slug"] for item in pool]))
        if self.error:
            raise self.error
        return self.resolutions


def test_jev_decision_is_frozen_once_and_outranks_client_target(monkeypatch):
    index = [{"id": "auth", "title": "Authentication", "slug": "authentication", "status": "active",
              "summary": "Verifying identity", "identity_aliases": []},
             {"id": "other", "title": "Other", "slug": "other", "status": "active", "summary": "x", "identity_aliases": []}]
    monkeypatch.setattr(calculation, "load_concept_identity_index", lambda *_args, **_kwargs: index)
    submissions = [{"window_ordinal": 1, "contributions": [contribution("Sign in", "other"), contribution("Totally new")]}]
    judge = FakeJudge({"mcp-incoming-0": {"incoming_slug": "mcp-incoming-0", "decision": "merge_into",
                                          "canonical_slug": "authentication"}})

    frozen = calculation.freeze_server_concept_merges(None, workspace_id="w", submissions=submissions, judge=judge)

    assert len(judge.calls) == 1
    by_name = {item["concept_name"]: item for item in frozen[0]["contributions"]}
    assert by_name["Sign in"]["server_merge_target_page_id"] == "auth"
    assert by_name["Totally new"]["server_merge_target_page_id"] is None, "asked and judged new: client target unused"
    # 적용 단계는 고정된 제출로 계산하며 판정기를 다시 부르지 않는다(판정기 인자가 없다).
    assert ledger(manifest(frozen, index)) == {("authentication", "Authentication"), ("totally-new", "Totally new")}


@pytest.mark.parametrize("judge", [None, FakeJudge(error=calculation.JevUnavailable("402"))])
def test_unavailable_jev_keeps_client_judgment_without_internal_llm(monkeypatch, judge):
    monkeypatch.setattr(calculation, "load_concept_identity_index", lambda *_args, **_kwargs: [])
    submissions = [{"window_ordinal": 1, "contributions": [contribution("Sign in", "auth")]}]
    assert calculation.freeze_server_concept_merges(None, workspace_id="w", submissions=submissions, judge=judge) == submissions
