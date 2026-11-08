import hashlib
from uuid import uuid4

from app.core.llm_env import resolve_llm_selection
from app.core.response_language import current_response_language
from app.modules.agent.domain.exceptions import AgentConfigurationError
from app.modules.agent_run.application.ports import AgentRunRepositoryPort, AgentRunStarterPort
from app.modules.agent_run.domain.entities import (
    AgentRun,
    StartAgentRunArtifact,
    StartAgentRunContent,
    StartAgentRunRequest,
)


class StartAgentRunUseCase(AgentRunStarterPort):
    def __init__(self, repository: AgentRunRepositoryPort, feature_enabled: bool = True) -> None:
        self._repository = repository
        self._feature_enabled = feature_enabled

    def start(self, request: StartAgentRunRequest) -> tuple[str, str]:
        if not self._feature_enabled:
            raise AgentConfigurationError("Agent Skill 기능이 비활성화되어 있습니다.")
        if not request.workspace_id or not request.user_id or not request.instruction.strip():
            raise ValueError("workspace_id, user_id, and instruction are required.")
        provider, model = resolve_llm_selection(request.provider, request.model)
        run = AgentRun(
            id=str(uuid4()),
            workspace_id=request.workspace_id,
            user_id=request.user_id,
            action=request.action,
            skill_version_id=request.skill_version_id,
            status="queued",
            request_summary=request.instruction.strip(),
            provider=provider,
            model=model,
            target_folder_id=request.target_folder_id,
            rule_summary=(request.rule_summary or "").strip() or None,
            # 승인 대기·수정 요청은 나중에 agent worker가 처리하므로 시작한 턴의 언어를 run에 남긴다.
            response_language=current_response_language(),
        )
        contents = (request.content, *request.contents) if request.content is not None else request.contents
        saved = self._repository.create_with_planning_job(
            run, str(uuid4()), tuple(_artifact(content) for content in contents)
        )
        return saved.id, saved.status


def _artifact(content: StartAgentRunContent) -> StartAgentRunArtifact:
    return StartAgentRunArtifact(
        id=str(uuid4()),
        content_hash=f"sha256:{hashlib.sha256(content.markdown.encode('utf-8')).hexdigest()}",
        markdown=content.markdown,
        purpose=content.purpose,
        document_id=content.document_id,
        base_version=content.base_version,
        target=content.target,
        display_name=content.display_name,
        summary=content.summary,
        preview_json=content.preview_json,
    )
