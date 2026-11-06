"""사용자 문구가 원인별로 갈리도록 Agent turn 실패를 코드로 나눈다. 코드에는 평가 문장이나 본문이 실리지 않는다."""

import pytest

from app.core.pipeline_control import PipelineRunCancelledError
from app.modules.markdown_edit.domain.entities import (
    MarkdownCreateRequest,
    MarkdownEditRequest,
    MarkdownEditTarget,
)
from app.modules.markdown_edit.domain.markdown_output_contract import (
    MarkdownCreateOutputContractError,
    MarkdownOutputContractError,
)
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import (
    DEFAULT_MARKDOWN_CREATE_EVALUATOR_PROMPT,
    DEFAULT_MARKDOWN_EDIT_EVALUATOR_PROMPT,
    ChatCompletionsMarkdownEditor,
)
from app.modules.task_cancellation.infrastructure.postgres_task_journal import TaskFailedError
from app.modules.wiki_generation.infrastructure.json_output_parser import JsonParseError
from app.workers import task_worker

MARKDOWN = "# 제목\n\n첫 문단은 충분히 긴 문장으로 되어 있어서 다듬을 내용이 있습니다."
EDIT_EVALUATOR = DEFAULT_MARKDOWN_EDIT_EVALUATOR_PROMPT.read_text(encoding="utf-8")
CREATE_EVALUATOR = DEFAULT_MARKDOWN_CREATE_EVALUATOR_PROMPT.read_text(encoding="utf-8")
PRIVATE_REASON = "private evaluation sentence"
GOOD_EDIT = {
    "operation": "replace",
    "actual_target": {"type": "whole_document", "start_line": 1, "end_line": 3},
    "summary": "요약",
    "replacement_markdown": "# 제목\n\n첫 문단을 고쳤습니다.\n",
}
REFUSAL_EDIT = {**GOOD_EDIT, "replacement_markdown": "요청을 수행할 수 없습니다."}
GOOD_CREATE = {"title": "제목", "summary": "요약", "markdown": "# 제목\n\n본문\n"}
EVALUATION_REJECTED = {"passed": False, "failures": [PRIVATE_REASON]}
EVALUATION_INVALID = {"passed": "yes"}


class ScriptedClient:
    """생성 응답과 평가 응답을 따로 준다. 예외를 넣으면 그 호출에서 던진다."""

    def __init__(self, generated: list[object], evaluations: list[object] | None = None) -> None:
        self.generated = generated
        self.evaluations = evaluations or []

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
        queue = self.evaluations if system_prompt in {EDIT_EVALUATOR, CREATE_EVALUATOR} else self.generated
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


def edit_failure(generated: list[object], evaluations: list[object] | None = None) -> Exception:
    editor = ChatCompletionsMarkdownEditor(ScriptedClient(generated, evaluations), "system")  # type: ignore[arg-type]
    request = MarkdownEditRequest(
        instruction="첫 문단을 다듬어 줘",
        markdown=MARKDOWN,
        target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=3),
        edit_goal="other",
        edit_operation="replace",
    )
    with pytest.raises(MarkdownOutputContractError) as caught:
        editor.generate_edit(request)
    return caught.value


def create_failure(generated: list[object], evaluations: list[object] | None = None) -> Exception:
    editor = ChatCompletionsMarkdownEditor(ScriptedClient(generated, evaluations), "system")  # type: ignore[arg-type]
    request = MarkdownCreateRequest(instruction="문서를 만들어 줘", skill_instructions="제목을 넣는다")
    with pytest.raises(MarkdownCreateOutputContractError) as caught:
        editor.generate_markdown(request)
    return caught.value


@pytest.mark.parametrize("generated, evaluations, expected", [
    ([REFUSAL_EDIT, REFUSAL_EDIT], None, "markdown_edit_refused"),
    ([GOOD_EDIT, GOOD_EDIT], [EVALUATION_REJECTED, EVALUATION_REJECTED], "markdown_edit_evaluation_rejected"),
    ([GOOD_EDIT, GOOD_EDIT], [EVALUATION_INVALID, EVALUATION_INVALID], "markdown_edit_evaluation_invalid"),
    ([GOOD_EDIT, GOOD_EDIT], [JsonParseError("x"), JsonParseError("x")], "markdown_edit_evaluation_invalid"),
    ([JsonParseError("x"), JsonParseError("x")], None, "markdown_edit_response_invalid"),
    ([{**GOOD_EDIT, "summary": ""}, {**GOOD_EDIT, "summary": ""}], None, "markdown_output_contract_failed"),
])
def test_edit_failures_get_a_code_per_cause(generated, evaluations, expected) -> None:
    error = edit_failure(generated, evaluations)

    assert task_worker._agent_failure_code(error) == expected


@pytest.mark.parametrize("generated, evaluations, expected", [
    ([GOOD_CREATE, GOOD_CREATE], [EVALUATION_REJECTED, EVALUATION_REJECTED], "markdown_create_evaluation_rejected"),
    ([GOOD_CREATE, GOOD_CREATE], [EVALUATION_INVALID, EVALUATION_INVALID], "markdown_create_evaluation_invalid"),
    ([JsonParseError("x"), JsonParseError("x")], None, "markdown_create_response_invalid"),
    ([{**GOOD_CREATE, "title": ""}, {**GOOD_CREATE, "title": ""}], None, "markdown_create_output_contract_failed"),
])
def test_create_failures_get_a_code_per_cause(generated, evaluations, expected) -> None:
    error = create_failure(generated, evaluations)

    assert task_worker._agent_failure_code(error) == expected


def test_code_never_carries_the_evaluation_sentence() -> None:
    error = edit_failure([GOOD_EDIT, GOOD_EDIT], [EVALUATION_REJECTED, EVALUATION_REJECTED])

    code = task_worker._agent_failure_code(error)

    assert PRIVATE_REASON not in code
    assert code.isascii() and len(code) <= 64


def test_evaluator_provider_failure_is_reported_as_provider_failure() -> None:
    # 공급사 오류는 평가 실패가 아니다. 계약 실패로 묶이지 않고 공급사 코드로 나간다.
    class RateLimitError(Exception):
        status_code = 429

    def wrapped() -> Exception:
        try:
            try:
                raise RateLimitError("provider body")
            except RateLimitError:
                raise RuntimeError("LLM API HTTP 429: provider body") from None
        except RuntimeError as error:
            return error

    editor = ChatCompletionsMarkdownEditor(ScriptedClient([GOOD_EDIT], [wrapped()]), "system")  # type: ignore[arg-type]
    request = MarkdownEditRequest(
        instruction="첫 문단을 다듬어 줘",
        markdown=MARKDOWN,
        target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=3),
        edit_goal="other",
        edit_operation="replace",
    )
    with pytest.raises(RuntimeError) as caught:
        editor.generate_edit(request)

    assert task_worker._agent_failure_code(caught.value) == "provider_rate_limited"


@pytest.mark.parametrize("status, expected", [(429, "provider_rate_limited"), (503, "provider_unavailable")])
def test_provider_status_keeps_its_code(status: int, expected: str) -> None:
    error = RuntimeError("LLM API HTTP error")
    error.status_code = status  # type: ignore[attr-defined]

    assert task_worker._agent_failure_code(error) == expected


def test_unclassified_error_stays_agent_turn_failed() -> None:
    assert task_worker._agent_failure_code(RuntimeError("private detail")) == "agent_turn_failed"


def test_closed_run_failure_keeps_its_stored_code() -> None:
    # 중단 재시도 초과는 요청이 잘못된 것이 아니다.
    error = TaskFailedError("interrupted_attempts_exhausted")

    assert task_worker._agent_failure_code(error) == "interrupted_attempts_exhausted"


def test_stored_value_that_is_not_a_code_is_not_passed_on() -> None:
    assert task_worker._agent_failure_code(TaskFailedError("Traceback: private detail")) == "task_failed"


def test_cancellation_and_invalid_request_are_unchanged() -> None:
    assert task_worker._agent_failure_code(PipelineRunCancelledError()) == "agent_turn_cancelled"
    assert task_worker._agent_failure_code(ValueError("bad")) == "invalid_agent_turn_request"
