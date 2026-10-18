"""워크스페이스 공용 모델 설정을 쓰는 단계는 Claude·허용 목록 밖 모델을 워크스페이스 기본 모델로 대체한다."""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.modules.agent.interfaces.http.schemas import AgentTurnRequestBody
from app.modules.meeting_notes.interfaces.command.schemas import MeetingNotesCommand
from app.modules.skill.interfaces.http.schemas import SkillLlmRequest
from app.modules.wiki_ingestion.interfaces.http.schemas import PipelineRunIn, WikiLintIn

WORKSPACE_DEFAULT = ("gemini", "gemini-3.5-flash-lite")


def _ingest(provider: str, model: str) -> PipelineRunIn:
    return PipelineRunIn(document_id="doc", provider=provider, model=model)


def _lint(provider: str, model: str) -> WikiLintIn:
    return WikiLintIn(provider=provider, model=model)


def _skill(provider: str, model: str) -> SkillLlmRequest:
    return SkillLlmRequest(provider=provider, model=model)


def _meeting(provider: str, model: str) -> MeetingNotesCommand:
    return MeetingNotesCommand(
        run_id="r", kind="meeting_notes", meeting_id="m", workspace_id="w", user_id="u",
        provider=provider, model=model, segments=[{"id": "s1", "text": "기록"}],
    )


WORKSPACE_STAGES = [_ingest, _lint, _skill, _meeting]


@pytest.fixture
def catalog_allowlist():
    with patch.dict("os.environ", {"AI_ALLOWED_LLM_MODELS": ""}):
        yield


@pytest.mark.parametrize("build", WORKSPACE_STAGES)
@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("claude", "claude-haiku-4-5-20251001"),
        ("claude", "claude-sonnet-5"),
        ("openai", "gpt-5.5"),
        ("gemini", "gemini-3.1-pro-preview"),
    ],
)
def test_workspace_stage_falls_back_to_workspace_default(catalog_allowlist, build, provider, model):
    request = build(provider, model)

    assert (request.provider, request.model) == WORKSPACE_DEFAULT


@pytest.mark.parametrize("build", WORKSPACE_STAGES)
@pytest.mark.parametrize(
    ("provider", "model"),
    [("openai", "gpt-6-luna"), ("gemini", "gemini-3.5-flash-lite")],
)
def test_workspace_stage_keeps_allowed_openai_and_gemini(catalog_allowlist, build, provider, model):
    request = build(provider, f" {model} ")

    assert (request.provider, request.model) == (provider, model)


def test_user_selected_agent_turn_accepts_claude_haiku_and_rejects_removed_models(catalog_allowlist):
    base = {"message": "요약해 줘", "workspace_id": "w", "user_id": "u"}

    body = AgentTurnRequestBody.model_validate(
        {**base, "provider": "claude", "model": "claude-haiku-4-5-20251001"}
    )
    assert (body.provider, body.model) == ("claude", "claude-haiku-4-5-20251001")

    with pytest.raises(ValidationError, match="Model not allowed"):
        AgentTurnRequestBody.model_validate({**base, "provider": "claude", "model": "claude-sonnet-5"})
