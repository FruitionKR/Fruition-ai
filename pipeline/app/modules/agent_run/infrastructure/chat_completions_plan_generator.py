import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.llm_env import (
    api_key_from_env,
    int_env,
    provider_api_key_env,
)
from app.core.response_language import with_response_language
from app.core.untrusted_input import validate_untrusted_payload
from app.modules.agent_run.application.plan_validation import ALLOWED_PLAN_TOOLS, prepare_agent_plan
from app.modules.agent_run.domain.entities import ContentArtifactReference
from app.modules.agent_run.domain.plan import AgentPlan, AgentPlanIntentClarificationRequired
from app.modules.wiki_generation.infrastructure.chat_completions_llm import ChatClientConfig, ChatCompletionsJsonClient


DEFAULT_PLAN_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "agent_folder_plan.system.md"
# 날짜 기준 정리의 기본 시간대. 사용자별 시간대 설정이 생기면 그 값으로 바꾼다.
DEFAULT_USER_TIMEZONE = "Asia/Seoul"
LOCAL_DATE_FIELDS = (("created_at", "created_date_local"), ("updated_at", "updated_date_local"))


class ChatCompletionsPlanGenerator:
    def __init__(self, client: ChatCompletionsJsonClient, system_prompt: str) -> None:
        self._client = client
        self._system_prompt = system_prompt

    def generate(
        self,
        *,
        run_id: str,
        plan_id: str,
        version: int,
        instruction: str,
        routing_action: str,
        hierarchy: list[dict[str, object]],
        skill_instructions: str | None,
        allowed_tools: tuple[str, ...] | None,
        content_artifacts: tuple[ContentArtifactReference, ...] = (),
        target_folder_id: str | None = None,
    ) -> AgentPlan:
        allowed_plan_tools = (
            ALLOWED_PLAN_TOOLS
            if allowed_tools is None
            else ALLOWED_PLAN_TOOLS.intersection(allowed_tools)
        )
        artifact_tools = {artifact.purpose for artifact in content_artifacts}
        if allowed_tools is None and artifact_tools:
            allowed_plan_tools = allowed_plan_tools.intersection(artifact_tools)
        elif allowed_tools is not None:
            allowed_plan_tools -= {"create_document", "apply_document_edit"} - artifact_tools
        payload = {
            "plan_id": plan_id,
            "instruction": instruction,
            "routing_action": routing_action,
            "user_timezone": DEFAULT_USER_TIMEZONE,
            "hierarchy": with_local_dates(hierarchy, DEFAULT_USER_TIMEZONE),
            "target_folder_id": target_folder_id,
            "skill_instructions": skill_instructions,
            "allowed_tools": sorted(allowed_plan_tools),
            "content_artifacts": [
                {
                    "id": artifact.id,
                    "content_hash": artifact.content_hash,
                    "purpose": artifact.purpose,
                    "document_id": artifact.document_id,
                    "base_version": artifact.base_version,
                    "target": artifact.target,
                }
                for artifact in content_artifacts
            ],
        }
        validate_untrusted_payload(payload)
        trusted_identifiers = [plan_id]
        for item in hierarchy:
            for key in ("id", "parent_id"):
                identifier = item.get(key)
                if isinstance(identifier, str) and identifier:
                    trusted_identifiers.append(identifier)
        for artifact in content_artifacts:
            trusted_identifiers.extend(
                identifier
                for identifier in (artifact.id, artifact.content_hash, artifact.document_id)
                if identifier
            )
        value = self._client.complete_json(
            # summary·reason·되묻기 질문은 승인 화면에 그대로 보이므로 run을 시작한 턴의 언어로 쓴다.
            with_response_language(self._system_prompt),
            json.dumps(payload, ensure_ascii=False, indent=2),
            trusted_identifiers=tuple(dict.fromkeys(trusted_identifiers)),
        )
        if type(value.get("intent_confirmed")) is not bool:
            raise ValueError("Agent plan intent_confirmed must be a boolean.")
        if not value["intent_confirmed"]:
            if (
                value.get("operations") != []
                or not isinstance(value.get("summary"), str)
                or not value["summary"].strip()
            ):
                raise ValueError("Unconfirmed Agent plan must have a summary and no operations.")
            raise AgentPlanIntentClarificationRequired(value["summary"].strip())
        return prepare_agent_plan(
            run_id=run_id,
            plan_id=plan_id,
            version=version,
            candidate=value,
            hierarchy=hierarchy,
            content_artifacts=content_artifacts,
            allowed_tools=allowed_plan_tools,
            target_folder_id=target_folder_id,
        )


def with_local_dates(hierarchy: list[dict[str, object]], timezone_name: str) -> list[dict[str, object]]:
    """created_at·updated_at(UTC)을 사용자 시간대 달력 날짜(YYYY-MM-DD)로 함께 넣는다.

    UTC 날짜로 묶으면 한국 시각 00~09시에 만든 문서가 전날 폴더로 간다.
    """
    zone = ZoneInfo(timezone_name)
    return [
        {
            **item,
            **{
                local_key: local_date
                for utc_key, local_key in LOCAL_DATE_FIELDS
                if (local_date := _local_date(item.get(utc_key), zone)) is not None
            },
        }
        for item in hierarchy
    ]


def _local_date(value: object, zone: ZoneInfo) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        # 백엔드 시각은 UTC다. 오프셋이 빠진 값도 UTC로 본다.
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(zone).date().isoformat()


def build_plan_generator(*, provider: str, model: str) -> ChatCompletionsPlanGenerator:
    api_key = api_key_from_env(provider=provider)
    if not api_key:
        raise RuntimeError(f"Set {provider_api_key_env(provider)}.")
    prompt_path = Path(os.environ.get("AGENT_PLAN_SYSTEM_PROMPT", str(DEFAULT_PLAN_PROMPT)))
    return ChatCompletionsPlanGenerator(
        ChatCompletionsJsonClient(
            ChatClientConfig(
                api_key=api_key,
                model=model,
                temperature=None if provider == "claude" else 0.0,
                timeout_seconds=int_env("AGENT_PLAN_LLM_TIMEOUT_SECONDS", 180),
                max_tokens=None,
                json_mode=True,
                provider=provider,
            )
        ),
        prompt_path.read_text(encoding="utf-8"),
    )
