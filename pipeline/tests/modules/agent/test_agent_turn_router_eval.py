from pathlib import Path

import pytest

from app.modules.agent.domain.entities import AgentTurnRoute
from app.modules.agent.domain.exceptions import AgentTurnRouteContractError
from evaluate_agent_turn_router import (
    DEFAULT_DATASET,
    evaluate_cases,
    load_cases,
    request_from_case,
)


class SequenceRouter:
    def __init__(self, responses: list[AgentTurnRoute | Exception]) -> None:
        self.responses = responses

    def route(self, _request: object) -> AgentTurnRoute:
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_seed_cases_are_unique_contrast_pairs() -> None:
    cases = load_cases(DEFAULT_DATASET)
    pair_counts: dict[str, int] = {}
    for case in cases:
        pair_counts[case["pair"]] = pair_counts.get(case["pair"], 0) + 1

    assert cases
    assert all(count >= 2 for count in pair_counts.values())


def test_active_markdown_case_has_document_target_for_direct_verification() -> None:
    case = next(case for case in load_cases(DEFAULT_DATASET) if case["id"] == "persistence-command")

    assert request_from_case(case).document_id == "evaluation-document"


def test_reports_semantic_misroute_separately_from_turn_failure(tmp_path: Path) -> None:
    dataset = tmp_path / "routes.jsonl"
    dataset.write_text(
        "\n".join(
            (
                '{"id":"misroute","message":"설명해줘","expected":{"action":"chat_answer","retrieval_source":"workspace","document_operation":"none","persist":false,"required_capabilities":[],"edit_goal":null,"edit_operation":null,"edit_destination":null}}',
                '{"id":"failure","message":"실패","expected":{"action":"chat_answer","retrieval_source":"workspace","document_operation":"none","persist":false,"required_capabilities":[],"edit_goal":null,"edit_operation":null,"edit_destination":null}}',
            )
        ),
        encoding="utf-8",
    )
    router = SequenceRouter(
        [
            AgentTurnRoute(
                action="markdown_edit",
                confidence=0.9,
                reason="잘못된 편집 분류",
                retrieval_source="none",
                document_operation="edit",
                required_capabilities=("document-edit",),
                edit_goal="style_change",
                edit_operation="replace",
                edit_destination="target",
            ),
            RuntimeError("provider failed"),
        ]
    )

    report = evaluate_cases(router, load_cases(dataset))

    assert report["summary"] == {
        "total": 2,
        "route_correct": 0,
        "agent_turn_failed": 1,
        "mutation_false_positives": 1,
        "confusion": {"chat_answer -> markdown_edit": 1},
    }
    assert report["results"][1]["error_code"] == "RuntimeError"


def test_reports_route_contract_failures(tmp_path: Path) -> None:
    dataset = tmp_path / "routes.jsonl"
    dataset.write_text(
        '{"id":"failure","message":"편집해줘","expected":{"action":"markdown_edit","retrieval_source":"none","document_operation":"edit","persist":false,"required_capabilities":["document-edit"],"edit_goal":"other","edit_operation":"replace","edit_destination":"target"}}',
        encoding="utf-8",
    )
    router = SequenceRouter(
        [
            AgentTurnRouteContractError(
                ["document_operation edit requires edit_operation"]
            )
        ]
    )

    report = evaluate_cases(router, load_cases(dataset))

    assert report["results"][0]["contract_failures"] == [
        "document_operation edit requires edit_operation"
    ]


def test_accepts_multiple_routes_with_the_same_mutation_boundary(tmp_path: Path) -> None:
    dataset = tmp_path / "routes.jsonl"
    dataset.write_text(
        '{"id":"ambiguous","message":"보완해줘","expected":{"action":"markdown_edit","retrieval_source":"workspace","document_operation":"edit","persist":false,"required_capabilities":["document-edit"],"edit_goal":"other","edit_operation":"replace","edit_destination":"target"},"acceptable":[{"action":"markdown_edit","retrieval_source":"workspace","document_operation":"edit","persist":false,"required_capabilities":["document-edit"],"edit_goal":"other","edit_operation":"insert_after","edit_destination":"document_end"}]}',
        encoding="utf-8",
    )
    router = SequenceRouter(
        [
            AgentTurnRoute(
                action="markdown_edit",
                confidence=0.9,
                reason="기존 내용을 보존하며 보완",
                retrieval_source="workspace",
                document_operation="edit",
                required_capabilities=("document-edit",),
                edit_goal="other",
                edit_operation="insert_after",
                edit_destination="document_end",
            )
        ]
    )

    report = evaluate_cases(router, load_cases(dataset))

    assert report["summary"]["route_correct"] == 1
    assert report["results"][0]["route_correct"] is True


def test_rejects_acceptable_route_that_changes_mutation_boundary(tmp_path: Path) -> None:
    dataset = tmp_path / "routes.jsonl"
    dataset.write_text(
        '{"id":"unsafe","message":"설명해줘","expected":{"action":"chat_answer","retrieval_source":"workspace","document_operation":"none","persist":false,"required_capabilities":[],"edit_goal":null,"edit_operation":null,"edit_destination":null},"acceptable":[{"action":"markdown_edit","retrieval_source":"none","document_operation":"edit","persist":false,"required_capabilities":["document-edit"],"edit_goal":"other","edit_operation":"replace","edit_destination":"target"}]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="문서 변경·영속성 경계"):
        load_cases(dataset)
