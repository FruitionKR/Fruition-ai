import logging

from fastapi import APIRouter, Depends, HTTPException

from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.entities import AgentTurnResult
from app.modules.agent.domain.exceptions import AgentConfigurationError, AgentTurnRouteContractError
from app.modules.agent.interfaces.http.dependencies import get_handle_agent_turn_use_case
from app.modules.agent.interfaces.http.schemas import (
    AgentTurnRequestBody,
    AgentTurnResponse,
    AgentTurnRouteResponse,
    GeneratedMarkdownResponse,
    MarkdownEditOperationResponse,
    MarkdownEditTargetResponse,
    SkillCandidateResponse,
)
from app.modules.markdown_edit.domain.markdown_output_contract import (
    MarkdownCreateOutputContractError,
    MarkdownOutputContractError,
)
from app.modules.markdown_edit.domain.markdown_target_scope import MarkdownTargetBoundaryError
from app.modules.query.interfaces.http.routes import _to_response as query_to_response
from app.modules.skill.domain.exceptions import SkillDisabledError, SkillNotFoundError
from app.modules.skill.interfaces.http.schemas import SkillAuthoringResponse


router = APIRouter(prefix="/agent", tags=["agent"])
logger = logging.getLogger(__name__)


@router.post("/turn", response_model=AgentTurnResponse)
def handle_agent_turn(
    payload: AgentTurnRequestBody,
    use_case: HandleAgentTurnUseCase = Depends(get_handle_agent_turn_use_case),
) -> AgentTurnResponse:
    try:
        result = use_case.execute(payload.to_domain())
    except MarkdownOutputContractError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "markdown_output_contract_failed",
                "message": "Markdown 편집 결과가 문법 및 보존 조건을 충족하지 못했습니다.",
            },
        ) from exc
    except MarkdownCreateOutputContractError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "markdown_create_output_contract_failed",
                "message": "Markdown 생성 결과가 필수 출력 조건을 충족하지 못했습니다.",
            },
        ) from exc
    except AgentTurnRouteContractError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "agent_turn_route_contract_failed",
                "message": "Agent 요청 분류 결과가 필수 출력 조건을 충족하지 못했습니다.",
            },
        ) from exc
    except (SkillNotFoundError, SkillDisabledError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": exc.code,
                "message": "선택한 Skill을 사용할 수 없습니다.",
            },
        ) from exc
    except MarkdownTargetBoundaryError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "markdown_target_crosses_structure",
                "message": "선택 범위가 분리할 수 없는 Markdown 구조의 일부만 포함합니다.",
                "structure": exc.structure,
                "start_line": exc.start_line,
                "end_line": exc.end_line,
            },
        ) from exc
    except AgentConfigurationError as exc:
        # 요청이 아니라 서버 배선·기능 플래그 문제다. 내부 메시지를 노출하지 않고 500으로 알린다.
        logger.error("Agent turn 설정 오류: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "agent_not_configured",
                "message": "Agent 기능을 사용할 수 없습니다.",
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "Agent turn 처리 실패: error_code=internal_server_error error_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "code": "internal_server_error",
                "message": "요청을 처리하지 못했습니다.",
            },
        ) from exc
    return _to_response(result)


def _to_response(result: AgentTurnResult) -> AgentTurnResponse:
    return AgentTurnResponse(
        action=result.action,
        updated_conversation_summary=result.updated_conversation_summary,
        route=AgentTurnRouteResponse(
            action=result.route.action,
            confidence=result.route.confidence,
            reason=result.route.reason,
            edit_goal=result.route.edit_goal,
            edit_operation=result.route.edit_operation,
            edit_destination=result.route.edit_destination,
            selected_skill_id=result.route.selected_skill_id,
            skill_candidates=list(result.route.skill_candidates),
            retrieval_source=result.route.retrieval_source,
            document_operation=result.route.document_operation,
            persist=result.route.persist,
            required_capabilities=list(result.route.required_capabilities),
        ),
        message=result.message,
        chat=query_to_response(result.query_answer) if result.query_answer else None,
        edit=_edit_to_response(result) if result.edit else None,
        source_markdown_sha256=result.source_markdown_sha256,
        generated_markdown=_generated_markdown_to_response(result),
        skill_candidates=[
            SkillCandidateResponse(
                id=candidate.id,
                version_id=candidate.version_id,
                name=candidate.name,
                description=candidate.description,
                capabilities=list(candidate.capabilities),
            )
            for candidate in result.skill_candidates
        ],
        run_id=result.run_id,
        run_status=result.run_status,
        skill_authoring=(
            SkillAuthoringResponse.from_domain(result.skill_authoring_result)
            if result.skill_authoring_result
            else None
        ),
    )


def _edit_to_response(result: AgentTurnResult) -> MarkdownEditOperationResponse | None:
    if result.edit is None:
        return None
    requested_target = result.edit.effective_requested_target
    actual_target = result.edit.actual_target
    return MarkdownEditOperationResponse(
        operation=result.edit.operation,
        requested_target=MarkdownEditTargetResponse(
            type=requested_target.type,
            start_line=requested_target.start_line,
            end_line=requested_target.end_line,
        ),
        actual_target=MarkdownEditTargetResponse(
            type=actual_target.type,
            start_line=actual_target.start_line,
            end_line=actual_target.end_line,
        ),
        scope_expanded=result.edit.scope_expanded,
        changed=result.edit.changed,
        summary=result.edit.summary,
        replacement_markdown=result.edit.replacement_markdown,
    )


def _generated_markdown_to_response(result: AgentTurnResult) -> GeneratedMarkdownResponse | None:
    if result.generated_markdown is None:
        return None
    return GeneratedMarkdownResponse(
        title=result.generated_markdown.title,
        summary=result.generated_markdown.summary,
        markdown=result.generated_markdown.markdown,
    )
