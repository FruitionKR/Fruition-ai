from app.modules.agent.domain.entities import AgentTurnResult
from app.modules.agent.interfaces.http.schemas import (
    AgentTurnResponse,
    AgentTurnRouteResponse,
    GeneratedMarkdownResponse,
    MarkdownEditOperationResponse,
    MarkdownEditTargetResponse,
    SkillCandidateResponse,
)
from app.modules.query.interfaces.http.responses import _to_response as query_to_response
from app.modules.skill.interfaces.http.schemas import SkillAuthoringResponse


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
