import sys
from unittest.mock import patch

from fastapi import HTTPException

import markdown_agent_http_lab
import markdown_edit_gfm_lab
from app.modules.agent.domain.entities import AgentTurnResult
from app.modules.agent.interfaces.http.routes import handle_agent_turn
from app.modules.agent.interfaces.http.schemas import AgentTurnRequestBody
from app.modules.markdown_edit.domain.markdown_target_scope import MarkdownTargetBoundaryError


def test_markdown_labs_default_to_openai_contract(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    for module in (markdown_agent_http_lab, markdown_edit_gfm_lab):
        with patch.object(sys, "argv", [module.__name__]):
            args = module.parse_args()

        assert not hasattr(args, "endpoint")
        assert args.api_key == "test-openai-key"
        assert args.model == "gpt-5-nano"


def test_markdown_labs_build_clients_without_legacy_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    with patch.object(sys, "argv", [markdown_agent_http_lab.__name__]):
        args = markdown_agent_http_lab.parse_args()
    markdown_agent_http_lab._build_use_case(args)

    with (
        patch.object(sys, "argv", [markdown_edit_gfm_lab.__name__]),
        patch.object(markdown_edit_gfm_lab, "CASES", ()),
    ):
        markdown_edit_gfm_lab.main()


class _Response:
    def __init__(self, status_code: int, body: dict[str, object]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, object]:
        return self._body


class _RecordingClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def post(self, _path: str, *, json: dict[str, object]) -> _Response:
        self.payloads.append(json)
        request = AgentTurnRequestBody.model_validate(json)
        if (
            request.active_markdown_context
            and request.active_markdown_context.target
            and request.active_markdown_context.target.start_line == 4
        ):
            try:
                handle_agent_turn(request, use_case=_PartialFenceUseCase())  # type: ignore[arg-type]
            except HTTPException as exc:
                return _Response(exc.status_code, {"detail": exc.detail})
        return _Response(
            200,
            {
                "action": "markdown_edit",
                "edit": {
                    "replacement_markdown": "배포 테스트 https://example.com/install\n```bash\n./deploy.sh --prod\n```",
                },
            },
        )


class _PartialFenceUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        raise MarkdownTargetBoundaryError("fence", 2, 4)


def test_markdown_lab_requests_include_selected_model_and_reach_domain_validation() -> None:
    client = _RecordingClient()
    model = "gpt-5-nano"

    results = [
        markdown_agent_http_lab._selection_cleanup(client, model),
        markdown_agent_http_lab._structured_translation(client, model),
        markdown_agent_http_lab._partial_fence_rejection(client, model),
    ]

    assert all(result["passed"] for result in results)
    assert len(client.payloads) == 3
    assert all(
        (payload["provider"], payload["model"]) == ("openai", model)
        for payload in client.payloads
    )
    assert results[2]["response"]["detail"]["code"] == "markdown_target_crosses_structure"
