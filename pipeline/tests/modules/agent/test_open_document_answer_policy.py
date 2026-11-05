"""열린 문서에 없는 답이 첨부에 있을 때의 답변 규칙. 판정과 수정 대상은 바꾸지 않는다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.modules.agent.domain.attached_edit_request import answer_includes_open_document, uses_only_open_document
from app.modules.agent.domain.reference_documents import SelectedReferenceDocuments, reference_evidence_snippets
from app.modules.query.application.answer_context_formatter import AnswerContextFormatter
from app.modules.skill.domain.entities import SkillAuthoringReference

DATASET = Path(__file__).resolve().parents[3] / "evals" / "open_document_answer.jsonl"


def _context(message: str = "이 문서의 베타 공개일이 언제야?") -> str:
    selected = SelectedReferenceDocuments(
        documents=(
            SkillAuthoringReference(id="open-doc", name="", markdown="# 온보딩\n목표는 72%다."),
            SkillAuthoringReference(id="doc-1", name="베타 출시 계획", markdown="베타 공개일은 12월 9일이다."),
        ),
        open_document_id="open-doc",
    )
    return AnswerContextFormatter().format(message, [], [], list(reference_evidence_snippets(selected)))


def test_policy_says_to_answer_from_the_attachment_when_the_open_document_lacks_it() -> None:
    policy = _context().split("# Related Pages")[0]

    assert "does not contain the answer" in policy
    assert "name the attached document by its document_title" in policy
    assert "열린 문서에는 없고" in policy


def test_policy_keeps_attachments_out_when_the_open_document_answers() -> None:
    assert "do not mention attached documents" in _context().split("# Related Pages")[0]


def test_policy_says_when_neither_has_the_answer() -> None:
    assert "neither" in _context().split("# Related Pages")[0]


def test_attachment_title_reaches_the_answer_model() -> None:
    assert '- document_title: "베타 출시 계획"' in _context()


def test_policy_is_only_about_answers() -> None:
    assert "Never present the content of an attached document as content of the open document." in _context()


def test_this_document_still_means_the_open_document() -> None:
    assert uses_only_open_document("이 문서의 베타 공개일이 언제야?", ("베타 출시 계획",))
    assert answer_includes_open_document("이 문서의 베타 공개일이 언제야?")


def test_eval_covers_the_three_outcomes() -> None:
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert {case["outcome"] for case in cases} == {"attachment", "open_document", "neither"}
    assert len({case["id"] for case in cases}) == len(cases)
    assert all(case["must_contain"] is not None and case["must_not_contain"] is not None for case in cases)


@pytest.mark.parametrize("mode", ["chat"])
def test_chat_notice_reason_is_a_known_code(mode: str) -> None:
    from typing import get_args

    from app.modules.agent.domain.entities import ClarifyReason

    assert "chat_tab_notice" in get_args(ClarifyReason)
