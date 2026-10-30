import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from app.core.ai_markdown_sanitizer import external_urls, sanitize_ai_markdown
from app.core.llm_env import (
    api_key_from_env,
    float_env,
    int_env,
    optional_int_env,
    provider_api_key_env,
    resolve_llm_selection,
)
from app.core.llm_prompt import with_schema_and_skill_prompt
from app.core.response_preferences import with_response_preferences
from app.modules.query.application.ports import QueryEventPublisherPort
from app.modules.query.application.query_event import publish_query_event
from app.modules.markdown_edit.application.ports import MarkdownEditorPort
from app.modules.markdown_edit.domain.entities import (
    EditOperationType,
    GeneratedMarkdownDocument,
    MarkdownCreateRequest,
    MarkdownCreateResult,
    MarkdownEditOperation,
    MarkdownEditRequest,
    MarkdownEditResult,
    MarkdownEditTarget,
)
from app.modules.markdown_edit.domain.markdown_output_contract import (
    MarkdownCreateOutputContractError,
    MarkdownOutputContractError,
    ProtectedMarkdown,
    protect_markdown,
    repair_markdown_output,
    validate_markdown_create_output,
    validate_markdown_output,
)
from app.modules.markdown_edit.domain.markdown_target_scope import (
    MarkdownTargetBoundaryError,
    MarkdownTargetScope,
    build_markdown_target_scope,
    markdown_line_range,
)
from app.modules.markdown_edit.infrastructure.markdown_source_range import (
    MarkdownSourceRangePlan,
    apply_source_range_response,
    build_source_range_plan,
    source_range_payload,
    validate_markdown_target_boundary,
)
from app.modules.markdown_edit.infrastructure.markdown_syntax_validation import (
    validate_markdown_syntax,
)
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)
from app.modules.wiki_generation.infrastructure.json_output_parser import JsonParseError
from app.modules.wiki_schema.infrastructure.active_schema_prompt import (
    get_active_schema_prompt,
)

DEFAULT_MARKDOWN_EDIT_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "markdown_edit.system.md"
DEFAULT_MARKDOWN_CREATE_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "markdown_create.system.md"
DEFAULT_MARKDOWN_SOURCE_EDIT_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "markdown_source_edit.system.md"
DEFAULT_MARKDOWN_EDIT_EVALUATOR_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "markdown_edit_evaluator.system.md"
JSON_OBJECT_CONTRACT_FAILURE = "model output must be a JSON object"
LLM_EVALUATION_PREFIX = "LLM evaluation:"
logger = logging.getLogger(__name__)


class ChatCompletionsMarkdownEditor(MarkdownEditorPort):
    def __init__(
        self,
        client: ChatCompletionsJsonClient,
        system_prompt: str,
        create_system_prompt: str | None = None,
        source_edit_system_prompt: str | None = None,
        context_lines: int = 20,
        event_publisher: QueryEventPublisherPort | None = None,
        schema_prompt_provider: Callable[[str, str | None, str | None], str] | None = None,
    ) -> None:
        self._event_publisher = event_publisher
        self._client = client
        self._system_prompt = system_prompt
        self._evaluation_system_prompt = DEFAULT_MARKDOWN_EDIT_EVALUATOR_PROMPT.read_text(encoding="utf-8")
        self._create_system_prompt = create_system_prompt or system_prompt
        self._source_edit_system_prompt = source_edit_system_prompt or DEFAULT_MARKDOWN_SOURCE_EDIT_PROMPT.read_text(
            encoding="utf-8"
        )
        self._context_lines = context_lines
        self._schema_prompt_provider = schema_prompt_provider or (
            lambda feature, workspace_id, user_id: ""
        )

    def generate_edit(self, request: MarkdownEditRequest) -> MarkdownEditResult:
        validate_markdown_target_boundary(request.markdown, request.target)
        scope = build_markdown_target_scope(request.markdown, request.target, self._context_lines)
        scoped_request = replace(request, markdown=scope.markdown)
        requested_operation = request.edit_operation
        source_range_plan = build_source_range_plan(scoped_request)
        if source_range_plan is not None:
            source_range_result = self._generate_source_range_edit(scoped_request, source_range_plan, scope)
            if source_range_result is not None:
                return source_range_result

        protected = protect_markdown(scoped_request)
        payload = {
            "instruction": scoped_request.instruction,
            "edit_goal": scoped_request.edit_goal,
            "conversation_summary": scoped_request.conversation_summary,
            "reference_context": scoped_request.reference_context or {},
            "requested_target": {
                "type": scoped_request.target.type,
                "start_line": scoped_request.target.start_line,
                "end_line": scoped_request.target.end_line,
            },
            "markdown": protected.markdown,
            "editable_context": _editable_context_payload(scope, protected.markdown),
        }
        payload["requested_operation"] = requested_operation
        system_prompt = with_response_preferences(
            with_schema_and_skill_prompt(
                self._system_prompt,
                self._schema_prompt_provider(
                    "edit",
                    request.workspace_id,
                    request.user_id,
                ),
                request.skill_instructions or "",
            ),
            request.output_language,
            None,
        )
        try:
            result = self._complete_edit(system_prompt, payload, request, protected, scope)
        except JsonParseError:
            result = None
        if result is not None and not result[1]:
            return result[0]
        _log_contract_failures(
            "edit", 1, request, result[1] if result is not None else [JSON_OBJECT_CONTRACT_FAILURE],
        )

        publish_query_event(
            self._event_publisher, "agent.retrying",
            "검사에서 보완할 부분을 발견해 다시 작성하고 있어요.",
        )
        retry_payload = {
            **payload,
            "contract_failures": result[1] if result is not None else [JSON_OBJECT_CONTRACT_FAILURE],
            "retry_instruction": "Correct every contract failure and return the required JSON object again.",
        }
        if result is not None:
            retry_payload["previous_replacement_markdown"] = result[2]
        try:
            retried = self._complete_edit(
                system_prompt,
                retry_payload,
                request,
                protected,
                scope,
                is_retry=True,
            )
        except JsonParseError as exc:
            _log_contract_failures("edit", 2, request, [JSON_OBJECT_CONTRACT_FAILURE])
            raise MarkdownOutputContractError([JSON_OBJECT_CONTRACT_FAILURE], "") from exc
        if retried[1]:
            _log_contract_failures("edit", 2, request, retried[1])
            raise MarkdownOutputContractError(retried[1], retried[0].edit.replacement_markdown)
        return retried[0]

    def _generate_source_range_edit(
        self,
        request: MarkdownEditRequest,
        plan: MarkdownSourceRangePlan,
        scope: MarkdownTargetScope,
    ) -> MarkdownEditResult | None:
        payload = {
            "instruction": request.instruction,
            "edit_goal": request.edit_goal,
            "conversation_summary": request.conversation_summary,
            "reference_context": request.reference_context or {},
            "target": {
                "type": request.target.type,
                "start_line": request.target.start_line,
                "end_line": request.target.end_line,
            },
            **source_range_payload(plan),
            **_read_only_context_payload(scope),
        }
        system_prompt = with_response_preferences(
            with_schema_and_skill_prompt(
                self._source_edit_system_prompt,
                self._schema_prompt_provider(
                    "edit",
                    request.workspace_id,
                    request.user_id,
                ),
                request.skill_instructions or "",
            ),
            request.output_language,
            None,
        )
        try:
            result, failures, _ = self._complete_source_range_edit(system_prompt, payload, request, plan)
        except JsonParseError:
            result, failures = None, [JSON_OBJECT_CONTRACT_FAILURE]
        if result is not None and not failures:
            return result
        # 원문 범위 편집은 Markdown 구조를 그대로 둔 채 구간 텍스트만 바꾼다. 끊긴 줄·목록 합치기나 번역 중 줄 재배치처럼
        # 구조가 바뀌어야 하는 요청은 이 방식으로 다시 시도해도 통과하지 못하므로, 실패하면 일반 편집으로 넘긴다.
        _log_contract_failures("source_range_edit", 1, request, failures)
        return None

    def _complete_source_range_edit(
        self,
        system_prompt: str,
        payload: dict[str, object],
        request: MarkdownEditRequest,
        plan: MarkdownSourceRangePlan,
    ) -> tuple[MarkdownEditResult, list[str], dict[str, Any]]:
        raw = self._client.complete_json(
            system_prompt,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        replacement, failures = apply_source_range_response(plan, raw.get("edits"))
        failures.extend(validate_markdown_output(request, replacement))
        # summary는 채팅 메시지 본문에도 그대로 들어간다.
        summary = sanitize_ai_markdown(_contract_string(raw.get("summary"), "summary", failures).strip())
        if not summary:
            failures.append("summary must not be empty")
        result = MarkdownEditResult(
            edit=MarkdownEditOperation(
                operation="replace",
                target=request.target,
                summary=summary,
                replacement_markdown=replacement,
            )
        )
        if not failures:
            context = payload.get("read_only_context")
            failures.extend(self._evaluate_edit(request, result, context if isinstance(context, dict) else None))
        return result, failures, raw

    def _complete_edit(
        self,
        system_prompt: str,
        payload: dict[str, object],
        request: MarkdownEditRequest,
        protected: ProtectedMarkdown,
        scope: MarkdownTargetScope,
        is_retry: bool = False,
    ) -> tuple[MarkdownEditResult, list[str], str]:
        if is_retry:
            system_prompt += (
                "\n\nThe previous output failed trusted application validation. "
                "Return a different result that satisfies every system contract."
            )
        raw = self._client.complete_json(
            system_prompt,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        result, failures = _normalize_edit_result(
            raw,
            request.target,
            request.edit_operation,
        )
        if request.edit_operation == "insert_after":
            result = replace(
                result,
                edit=replace(result.edit, target=request.target),
            )
        protected_replacement = result.edit.replacement_markdown
        restored, protected_failures = protected.restore(protected_replacement)
        failures.extend(protected_failures)
        actual_request = _actual_target_request(
            request,
            result.edit.actual_target,
            scope,
            failures,
        )
        restored = repair_markdown_output(actual_request, restored)
        # 사용자 원문에 있던 링크는 지키고, AI가 새로 넣은 외부 이미지·링크만 무력화한다.
        restored = sanitize_ai_markdown(restored, keep_urls=external_urls(request.markdown))
        failures.extend(validate_markdown_output(actual_request, restored))
        failures.extend(validate_markdown_syntax(restored))
        restored_result = MarkdownEditResult(
            edit=MarkdownEditOperation(
                operation=result.edit.operation,
                target=result.edit.target,
                requested_target=request.target,
                summary=sanitize_ai_markdown(result.edit.summary),
                replacement_markdown=restored,
            )
        )
        if not failures:
            failures.extend(self._evaluate_edit(actual_request, restored_result, {
                "before": scope.context_before, "after": scope.context_after,
            }))
        return restored_result, failures, protected_replacement

    def _evaluate_edit(
        self,
        request: MarkdownEditRequest,
        result: MarkdownEditResult,
        surrounding_context: dict[str, object] | None = None,
    ) -> list[str]:
        payload = {
            "instruction": request.instruction,
            "edit_goal": request.edit_goal,
            "conversation_summary": request.conversation_summary,
            "reference_context": request.reference_context or {},
            "skill_instructions": request.skill_instructions or "",
            "output_language": request.output_language,
            "workspace_editing_rules": self._schema_prompt_provider(
                "edit", request.workspace_id, request.user_id,
            ),
            "requested_operation": request.edit_operation,
            "edit_destination": request.edit_destination,
            "original_markdown": request.markdown,
            "proposal": asdict(result.edit),
            "replacement_lines": _evaluated_replacement_lines(request, result),
        }
        if surrounding_context and any(surrounding_context.values()):
            payload["surrounding_context"] = surrounding_context
        publish_query_event(
            self._event_publisher, "agent.evaluating",
            "편집안이 요청에 맞는지 확인하고 있어요.",
        )
        invalid = ["LLM evaluation must return a boolean passed and consistent string-list failures"]
        try:
            raw = self._client.complete_json(
                self._evaluation_system_prompt,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except JsonParseError:
            return invalid
        if not isinstance(raw, dict) or type(raw.get("passed")) is not bool:
            return invalid
        failures = raw.get("failures")
        if not isinstance(failures, list) or any(
            not isinstance(item, str) or not item.strip() for item in failures
        ):
            return invalid
        if raw["passed"] != (not failures):
            return invalid
        return [f"{LLM_EVALUATION_PREFIX} {item.strip()}" for item in failures]

    def generate_markdown(self, request: MarkdownCreateRequest) -> MarkdownCreateResult:
        payload = {
            "instruction": request.instruction,
            "conversation_summary": request.conversation_summary,
            "reference_context": request.reference_context or {},
        }
        system_prompt = with_response_preferences(
            with_schema_and_skill_prompt(
                self._create_system_prompt,
                self._schema_prompt_provider(
                    "edit",
                    request.workspace_id,
                    request.user_id,
                ),
                request.skill_instructions or "",
            ),
            request.output_language,
            None,
            document_creation=True,
        )
        try:
            result, failures, raw = self._complete_markdown_create(
                system_prompt,
                payload,
                request,
            )
        except JsonParseError:
            result = None
            failures = [JSON_OBJECT_CONTRACT_FAILURE]
            raw = None
        if result is not None and not failures:
            return result

        publish_query_event(
            self._event_publisher, "agent.retrying",
            "검사에서 보완할 부분을 발견해 다시 작성하고 있어요.",
        )
        retry_payload = {
            **payload,
            "contract_failures": failures,
            "retry_instruction": "Correct every contract failure and return the required JSON object again.",
        }
        if raw is not None:
            retry_payload["previous_generated_markdown"] = raw
        try:
            retried, retry_failures, retried_raw = self._complete_markdown_create(
                system_prompt,
                retry_payload,
                request,
            )
        except JsonParseError as exc:
            raise MarkdownCreateOutputContractError([JSON_OBJECT_CONTRACT_FAILURE], {}) from exc
        if retry_failures:
            raise MarkdownCreateOutputContractError(retry_failures, retried_raw)
        return retried

    def _complete_markdown_create(
        self,
        system_prompt: str,
        payload: dict[str, object],
        request: MarkdownCreateRequest,
    ) -> tuple[MarkdownCreateResult, list[str], dict[str, Any]]:
        raw = self._client.complete_json(system_prompt, json.dumps(payload, ensure_ascii=False, indent=2))
        failures: list[str] = []
        result = _normalize_create_result(raw, failures)
        result = replace(
            result,
            document=replace(
                result.document,
                summary=sanitize_ai_markdown(result.document.summary),
                markdown=sanitize_ai_markdown(result.document.markdown),
            ),
        )
        failures.extend(validate_markdown_create_output(result.document))
        failures.extend(validate_markdown_syntax(result.document.markdown))
        return result, failures, raw


def build_markdown_editor(
    *,
    provider: str | None = None,
    model: str | None = None,
    event_publisher: QueryEventPublisherPort | None = None,
) -> MarkdownEditorPort:
    resolved_provider, resolved_model = resolve_llm_selection(provider, model)
    api_key = _api_key(resolved_provider)
    if not api_key:
        raise RuntimeError(f"Set {provider_api_key_env(resolved_provider)}.")
    prompt_path = Path(os.environ.get("MARKDOWN_EDIT_SYSTEM_PROMPT", str(DEFAULT_MARKDOWN_EDIT_PROMPT)))
    create_prompt_path = Path(os.environ.get("MARKDOWN_CREATE_SYSTEM_PROMPT", str(DEFAULT_MARKDOWN_CREATE_PROMPT)))
    source_edit_prompt_path = Path(
        os.environ.get("MARKDOWN_SOURCE_EDIT_SYSTEM_PROMPT", str(DEFAULT_MARKDOWN_SOURCE_EDIT_PROMPT))
    )
    return ChatCompletionsMarkdownEditor(
        ChatCompletionsJsonClient(
            ChatClientConfig(
                api_key=api_key,
                model=resolved_model,
                temperature=None,
                timeout_seconds=_int_env("MARKDOWN_EDIT_LLM_TIMEOUT_SECONDS", 180),
                max_tokens=_optional_int_env("MARKDOWN_EDIT_LLM_MAX_TOKENS"),
                json_mode=True,
                provider=resolved_provider,
            )
        ),
        system_prompt=prompt_path.read_text(encoding="utf-8"),
        create_system_prompt=create_prompt_path.read_text(encoding="utf-8"),
        source_edit_system_prompt=source_edit_prompt_path.read_text(encoding="utf-8"),
        context_lines=_int_env("MARKDOWN_EDIT_CONTEXT_LINES", 20),
        schema_prompt_provider=get_active_schema_prompt,
        event_publisher=event_publisher,
    )


def _evaluated_replacement_lines(request: MarkdownEditRequest, result: MarkdownEditResult) -> list[str]:
    """평가 모델에 보낼 결과 줄. insert_after 앞뒤 빈 줄은 문단 구분용 배치라 내용 판단에서 뺀다."""
    lines = result.edit.replacement_markdown.splitlines()
    if request.edit_operation != "insert_after":
        return lines
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _log_contract_failures(
    stage: str,
    attempt: int,
    request: MarkdownEditRequest,
    failures: list[str],
) -> None:
    """계약 위반을 코드·위치로만 남긴다. 문서 본문·모델 출력·평가 문장은 로그에 넣지 않는다."""
    logger.warning(
        "[Markdown 편집 계약 실패] stage=%s attempt=%d operation=%s destination=%s goal=%s "
        "target=%s:%d-%d failures=%s",
        stage,
        attempt,
        request.edit_operation,
        request.edit_destination,
        request.edit_goal,
        request.target.type,
        request.target.start_line,
        request.target.end_line,
        [contract_failure_code(failure) for failure in failures],
    )


def contract_failure_code(failure: str) -> str:
    """로그용 실패 코드. LLM 평가 문장과 `:` 뒤에 붙는 모델 출력 값(세그먼트 id 등)은 버린다."""
    if failure.startswith(LLM_EVALUATION_PREFIX):
        return "llm_evaluation_rejected"
    return failure.split(": ", 1)[0][:120]


def _read_only_context_payload(scope: MarkdownTargetScope) -> dict[str, object]:
    if not scope.context_before and not scope.context_after:
        return {}
    return {
        "read_only_context": {
            "before": scope.context_before,
            "after": scope.context_after,
        }
    }


def _editable_context_payload(
    scope: MarkdownTargetScope,
    protected_markdown: str,
) -> dict[str, object]:
    return {
        "start_line": scope.start_line,
        "end_line": scope.end_line,
        "before": scope.context_before,
        "target": protected_markdown,
        "after": scope.context_after,
    }


def _normalize_edit_result(
    value: dict[str, Any],
    requested_target: MarkdownEditTarget,
    requested_operation: EditOperationType,
) -> tuple[MarkdownEditResult, list[str]]:
    failures: list[str] = []
    raw_operation = value.get("operation")
    operation = requested_operation
    if raw_operation != requested_operation:
        failures.append(f"operation must be {requested_operation}")
    actual_target = _normalize_actual_target(
        value.get("actual_target"),
        requested_target,
        failures,
    )
    summary = _contract_string(value.get("summary"), "summary", failures).strip()
    replacement_markdown = _contract_string(
        value.get("replacement_markdown") or value.get("replacementMarkdown"),
        "replacement_markdown",
        failures,
    )
    result = MarkdownEditResult(
        edit=MarkdownEditOperation(
            operation=operation,
            target=actual_target,
            requested_target=requested_target,
            summary=summary,
            replacement_markdown=replacement_markdown,
        )
    )
    if not result.edit.summary:
        failures.append("summary must not be empty")
    if not result.edit.replacement_markdown.strip():
        failures.append("replacement_markdown must not be empty")
    return result, failures
def _normalize_actual_target(
    value: object,
    requested_target: MarkdownEditTarget,
    failures: list[str],
) -> MarkdownEditTarget:
    if not isinstance(value, dict):
        failures.append("actual_target must be an object")
        return requested_target
    target_type = value.get("type")
    start_line = value.get("start_line")
    end_line = value.get("end_line")
    if target_type not in {"selection", "current_section", "whole_document"}:
        failures.append("actual_target.type is not supported")
        return requested_target
    if (
        not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or not isinstance(end_line, int)
        or isinstance(end_line, bool)
    ):
        failures.append("actual_target line numbers must be integers")
        return requested_target
    return MarkdownEditTarget(
        type=target_type,
        start_line=start_line,
        end_line=end_line,
    )


def _nonblank_range(lines: list[str], target: MarkdownEditTarget) -> tuple[int, int]:
    """요청 범위에서 앞뒤 빈 줄을 뺀 범위. 현재 섹션은 다음 제목 앞 빈 줄까지 오므로 모델이 그 줄을 빼도 내용은 다 덮는다."""
    start, end = target.start_line, target.end_line
    while start < end and not lines[start - 1].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return start, end


def _actual_target_request(
    request: MarkdownEditRequest,
    actual_target: MarkdownEditTarget,
    scope: MarkdownTargetScope,
    failures: list[str],
) -> MarkdownEditRequest:
    lines = re.split(r"\r\n|\r|\n", request.markdown)
    appends_to_document = (
        request.edit_operation == "insert_after"
        and request.edit_destination == "document_end"
        and actual_target.type == "whole_document"
    )
    if (
        not appends_to_document
        and (actual_target.start_line < scope.start_line or actual_target.end_line > scope.end_line)
    ):
        failures.append("actual_target must stay within editable_context")
    if actual_target.start_line < 1:
        failures.append("actual_target.start_line must be greater than 0")
    if actual_target.end_line < actual_target.start_line:
        failures.append("actual_target.end_line must be greater than or equal to actual_target.start_line")
    if actual_target.end_line > len(lines):
        failures.append("actual_target.end_line must not exceed the Markdown line count")
    required_start, required_end = _nonblank_range(lines, request.target)
    if (
        not appends_to_document
        and (
            actual_target.start_line > required_start
            or actual_target.end_line < required_end
        )
    ):
        failures.append("actual_target must contain requested_target")
    if actual_target.type == "whole_document" and (
        actual_target.start_line != 1 or actual_target.end_line != len(lines)
    ):
        failures.append("whole_document actual_target must cover the entire Markdown document")
    if request.edit_operation == "insert_after":
        expected_target_type = (
            "whole_document"
            if request.edit_destination == "document_end"
            else "current_section"
        )
        if actual_target.type != expected_target_type:
            failures.append(
                f"insert_after operation requires a {expected_target_type} actual_target"
            )

    target_range_valid = (
        actual_target.start_line >= 1
        and actual_target.end_line >= actual_target.start_line
        and actual_target.end_line <= len(lines)
    )
    if target_range_valid:
        try:
            validate_markdown_target_boundary(request.markdown, actual_target)
        except (ValueError, MarkdownTargetBoundaryError) as exc:
            failures.append(str(exc))

    safe_start = max(1, min(actual_target.start_line, len(lines)))
    safe_end = max(safe_start, min(actual_target.end_line, len(lines)))
    return replace(
        request,
        markdown=markdown_line_range(request.markdown, safe_start, safe_end),
        target=actual_target,
    )


def _normalize_create_result(
    value: dict[str, Any],
    failures: list[str],
) -> MarkdownCreateResult:
    return MarkdownCreateResult(
        document=GeneratedMarkdownDocument(
            title=_contract_string(value.get("title"), "title", failures).strip(),
            summary=_contract_string(value.get("summary"), "summary", failures).strip(),
            markdown=_contract_string(
                value.get("markdown") or value.get("content"),
                "markdown",
                failures,
            ).strip(),
        )
    )


def _contract_string(
    value: object,
    field: str,
    failures: list[str],
) -> str:
    if not isinstance(value, str):
        failures.append(f"{field} must be a string")
        return ""
    return value


def _api_key(provider: str | None = None) -> str | None:
    return api_key_from_env(
        provider=provider,
    )


def _float_env(name: str, default: float) -> float:
    return float_env(name, default)


def _int_env(name: str, default: int) -> int:
    return int_env(name, default)


def _optional_int_env(name: str) -> int | None:
    return optional_int_env(name)
