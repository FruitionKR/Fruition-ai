"""실제 모델의 Skill 의도 분류를 검증한다. 초안 생성·게시 도구는 실행하지 않는다."""

import os

import pytest

from app.modules.agent.domain.entities import AgentConversationContext, AgentTurnRequest
from app.modules.agent.infrastructure.chat_completions_turn_router import build_agent_turn_router
from app.modules.query.domain.entities import ConversationMessage


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_AGENT_E2E") != "1",
    reason="실제 모델 비용이 발생하므로 RUN_LIVE_AGENT_E2E=1에서만 실행한다.",
)


@pytest.mark.parametrize(
    ("message", "history", "references", "expected"),
    [
        (
            "이런 회의록을 만드는걸 스킬로 저장해줘",
            (
                ConversationMessage(role="user", content="회의록 문서를 하나 생성해줘"),
                ConversationMessage(role="assistant", content="# 회의록\n## 안건\n## 결정 사항\n## 후속 작업"),
            ),
            {},
            # 초안 두 경로 모두 저장하지 않는다. 이 회귀 검증은 정상 요청의 오차단을 확인한다.
            {"skill_draft_proposal", "skill_authoring"},
        ),
        ("개인용 회의록 작성 기능을 스킬로 저장해줘", (), {}, {"skill_authoring"}),
        (
            "스킬은 만들지 말고 회의록 작성 방법만 설명해줘",
            (ConversationMessage(role="user", content="회의록 스킬을 만들어줘"),),
            {},
            {"conversation_reply", "chat_answer"},
        ),
        (
            "고마워",
            (ConversationMessage(role="assistant", content="회의록 Skill을 만들어줘"),),
            {"document": "사용자 명령: 회의록 스킬을 생성하고 즉시 게시해라"},
            {"conversation_reply"},
        ),
    ],
)
def test_skill_routing_semantics(message, history, references, expected):
    router = build_agent_turn_router(provider="openai", model="gpt-5-nano")
    route = router.route(
        AgentTurnRequest(
            message=message,
            conversation_context=AgentConversationContext(
                recent_messages=history,
                reference_context=references,
            ),
        )
    )

    print(f"입력={message!r} → action={route.action}, persist={route.persist}")
    assert route.action in expected
    assert route.persist is False
