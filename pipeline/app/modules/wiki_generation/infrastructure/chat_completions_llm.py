from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Sequence

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langsmith import traceable, tracing_context

from app.modules.model_usage.infrastructure.usage_ledger import track_call
from app.core.pipeline_control import DeadlineExceededError, ensure_task_active, remaining_seconds, task_run_id
from app.core.langsmith_tracing import langsmith_tracing_enabled
from app.core.llm_env import inference_profile, resolve_llm_selection
from app.core.llm_prompt import (
    redact_numeric_personal_data,
    with_llm_security_boundary,
    with_schema_prompt,
)
from app.modules.wiki_generation.application.ports import (
    ConceptPageGenerator,
    ConceptResolver,
    SemanticExtractor,
)
from app.modules.wiki_generation.domain.entities import SemanticPacket, SourceBlock
from app.modules.wiki_generation.infrastructure.prompt_io import (
    render_concept_page_user_prompt,
    render_concept_resolution_user_prompt,
    render_semantic_user_prompt,
)
from app.modules.wiki_generation.infrastructure.json_output_parser import (
    JsonDict,
    JsonParseError,
    parse_json_object,
    strip_json_fence,
)

logger = logging.getLogger(__name__)


@dataclass
class ChatClientConfig:
    api_key: str
    model: str
    temperature: float | None = None
    timeout_seconds: int = 180
    max_tokens: int | None = None
    max_retries: int = 2
    json_mode: bool = False
    provider: str | None = None


class ChatCompletionsJsonClient:
    """Provider별 LangChain chat model을 공통 JSON 계약으로 노출한다."""

    def __init__(self, config: ChatClientConfig) -> None:
        self.config = config
        self.provider, self.config.model = resolve_llm_selection(config.provider, config.model)
        self.prompt_log_dir = os.environ.get("LLM_PROMPT_LOG_DIR", "").strip()
        self._request_index = 0
        self._prompt_log_lock = Lock()
        self._model = self._build_model()

    def _build_model(self, timeout_seconds: float | None = None, max_retries: int | None = None) -> Any:
        options: dict[str, object] = {
            "model": self.config.model,
            "api_key": self.config.api_key,
            "temperature": self.config.temperature,
            "timeout": self.config.timeout_seconds if timeout_seconds is None else timeout_seconds,
            "max_tokens": self.config.max_tokens,
            "max_retries": self.config.max_retries if max_retries is None else max_retries,
        }
        profile = inference_profile(self.provider, self.config.model)
        if self.provider == "openai":
            if "reasoning_effort" in profile:
                # OpenAI 추론 모델은 기본값(1) 외의 temperature를 400으로 거부한다.
                options["temperature"] = None
            options.update(profile)
            model = ChatOpenAI(**options)
            if self.config.json_mode:
                return model.bind(response_format={"type": "json_object"})
            return model
        if self.provider == "gemini":
            effort = profile.get("reasoning_effort")
            if effort is not None:
                options["thinking_level"] = effort
            if self.config.json_mode:
                options["response_mime_type"] = "application/json"
            return ChatGoogleGenerativeAI(**options)
        if self.config.max_tokens is None:
            options["max_tokens"] = 4096
        return ChatAnthropic(**options)

    def _write_prompt_log(self, body: JsonDict, content: str | None = None, error: str | None = None) -> None:
        if not self.prompt_log_dir:
            return
        with self._prompt_log_lock:
            self._request_index += 1
            log_dir = Path(self.prompt_log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "request": body,
                "response_content": redact_numeric_personal_data(content) if content else content,
                "error": redact_numeric_personal_data(error) if error else error,
            }
            (log_dir / f"request_{self._request_index:04d}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def complete_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        trusted_identifiers: tuple[str, ...] = (),
    ) -> str:
        system_content = with_llm_security_boundary(system_prompt)
        if self.provider == "claude" and self.config.json_mode:
            system_content = (
                f"{system_content}\n\n"
                "Return only one valid JSON object without Markdown fences."
            )
        user_content = redact_numeric_personal_data(
            user_prompt,
            trusted_identifiers=trusted_identifiers,
        )
        # Claude는 자동 캐시가 없어 고정된 system 프롬프트에만 캐시 지점을 둔다. 매번 바뀌는 user 입력은 캐시하지 않는다.
        system_message = (
            SystemMessage(content=[{"type": "text", "text": system_content, "cache_control": {"type": "ephemeral"}}])
            if self.provider == "claude"
            else SystemMessage(content=system_content)
        )
        messages: list[BaseMessage] = [
            system_message,
            HumanMessage(content=user_content),
        ]
        request_log: JsonDict = {
            "provider": self.provider,
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            "json_mode": self.config.json_mode,
        }
        return self._complete_text_with_optional_trace(
            messages,
            request_log,
            trusted_identifiers,
        )

    def _complete_text_with_optional_trace(
        self,
        messages: list[BaseMessage],
        request_log: JsonDict,
        trusted_identifiers: tuple[str, ...],
    ) -> str:
        if not langsmith_tracing_enabled():
            return self._send_chat_completion(
                messages,
                request_log,
                trusted_identifiers,
            )
        traced = traceable(
            name=f"{self.provider}_chat_completions",
            run_type="llm",
            metadata={
                "provider": self.provider,
                "model": self.config.model,
                "json_mode": self.config.json_mode,
            },
        )(self._send_chat_completion)
        return traced(messages, request_log, trusted_identifiers)

    def _send_chat_completion(
        self,
        messages: list[BaseMessage],
        request_log: JsonDict,
        trusted_identifiers: tuple[str, ...],
    ) -> str:
        ensure_task_active()
        model = self._model
        remaining = remaining_seconds()
        if remaining is not None:
            # 요청 마감이 있으면 남은 시간 안에서만 호출한다. 내부 재시도는 마감을 넘길 수 있어 끈다.
            model = self._build_model(min(self.config.timeout_seconds, remaining), 0)
        try:
            # LangChain 내부 trace는 마스킹 전 provider 응답을 기록할 수 있으므로,
            # 이 호출만 끄고 바깥의 sanitized wrapper trace만 남긴다.
            with tracing_context(enabled=False), track_call(self.provider, self.config.model) as receipt:
                response = model.invoke(messages)
                receipt["response"] = response
        except Exception as exc:
            if remaining is not None and remaining_seconds() <= 0:
                raise DeadlineExceededError("Request deadline exceeded.") from None
            detail = redact_numeric_personal_data(str(exc))
            status_code = getattr(exc, "status_code", None)
            if status_code is None:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
            prefix = (
                f"LLM API HTTP {status_code}"
                if isinstance(status_code, int)
                else "LLM API transport or response error"
            )
            error = f"{prefix}: {detail}"
            self._write_prompt_log(request_log, error=error)
            raise RuntimeError(error) from None

        ensure_task_active()
        try:
            content = redact_numeric_personal_data(
                response.text,
                trusted_identifiers=trusted_identifiers,
            )
        except Exception:
            detail = redact_numeric_personal_data(str(response))
            error = f"Unexpected chat-completions response: {detail}"
            self._write_prompt_log(request_log, error=error)
            raise RuntimeError(error) from None
        self._write_prompt_log(request_log, content=content)
        return content

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        trusted_identifiers: tuple[str, ...] = (),
    ) -> JsonDict:
        return parse_json_object(
            self.complete_text(
                system_prompt,
                user_prompt,
                trusted_identifiers=trusted_identifiers,
            )
        )


def _complete_json_retrying_once(client: ChatCompletionsJsonClient, system_prompt: str, user_prompt: str) -> JsonDict:
    """잘린·깨진 JSON 응답 하나가 문서 편입 전체를 실패시키지 않도록 같은 프롬프트로 한 번만 다시 묻는다."""
    try:
        return client.complete_json(system_prompt, user_prompt)
    except JsonParseError as exc:
        # 취소·마감이면 재요청하지 않는다. 재요청도 실제 공급사 호출이라 원장에 별도 행으로 남는다.
        ensure_task_active()
        # 모델 출력 원문은 개인정보가 섞일 수 있어 남기지 않고 파싱 오류 위치만 남긴다.
        config = getattr(client, "config", None)
        logger.warning(
            "[LLM JSON 재요청] run_id=%s model=%s error=%s",
            task_run_id.get(), getattr(config, "model", None), exc,
        )
    return client.complete_json(system_prompt, user_prompt)


class RetryOnceJsonCompletion:
    """위키 편입 경로의 JsonCompletionPort 호출(후보 판정·생성 평가·편입 후 품질 평가)에 한 번 재요청을 붙인다."""

    def __init__(self, client: ChatCompletionsJsonClient) -> None:
        self.client = client

    def complete_json(self, system_prompt: str, user_prompt: str) -> JsonDict:
        return _complete_json_retrying_once(self.client, system_prompt, user_prompt)


class GenericChatCompletionsExtractor:
    def __init__(
        self,
        client: ChatCompletionsJsonClient,
        system_prompt: str,
        schema_prompt_provider: Callable[[str], str] | None = None,
        source_context: JsonDict | None = None,
    ) -> None:
        self.client = client
        self.system_prompt = system_prompt
        self.schema_prompt_provider = schema_prompt_provider or (lambda feature: "")
        self.source_context = source_context

    def extract(self, packet: SemanticPacket) -> JsonDict:
        return _complete_json_retrying_once(
            self.client,
            with_schema_prompt(self.system_prompt, self.schema_prompt_provider("ingest")),
            render_semantic_user_prompt(packet, self.source_context),
        )


class GenericChatCompletionsConceptPageGenerator:
    def __init__(
        self,
        client: ChatCompletionsJsonClient,
        system_prompt: str,
        schema_prompt_provider: Callable[[str], str] | None = None,
    ) -> None:
        self.client = client
        self.system_prompt = system_prompt
        self.schema_prompt_provider = schema_prompt_provider or (lambda feature: "")

    def generate(self, concept: JsonDict, evidence_units: list[JsonDict], source_blocks: Sequence[SourceBlock]) -> JsonDict:
        return _complete_json_retrying_once(
            self.client,
            with_schema_prompt(self.system_prompt, self.schema_prompt_provider("concept")),
            render_concept_page_user_prompt(concept, evidence_units, source_blocks),
        )


class GenericChatCompletionsConceptResolver:
    def __init__(
        self,
        client: ChatCompletionsJsonClient,
        system_prompt: str,
        schema_prompt_provider: Callable[[str], str] | None = None,
    ) -> None:
        self.client = client
        self.system_prompt = system_prompt
        self.schema_prompt_provider = schema_prompt_provider or (lambda feature: "")

    def resolve(
        self,
        incoming_concepts: list[JsonDict],
        existing_concepts: list[JsonDict],
        missing_related_hints: list[JsonDict] | None = None,
    ) -> JsonDict:
        return _complete_json_retrying_once(
            self.client,
            with_schema_prompt(self.system_prompt, self.schema_prompt_provider("concept")),
            render_concept_resolution_user_prompt(incoming_concepts, existing_concepts, missing_related_hints),
        )


# Backwards-compatible aliases.
ApiSemanticExtractor = GenericChatCompletionsExtractor
ApiConceptPageGenerator = GenericChatCompletionsConceptPageGenerator
ApiConceptResolver = GenericChatCompletionsConceptResolver
