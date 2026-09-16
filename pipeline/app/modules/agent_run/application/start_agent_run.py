import hashlib
from uuid import uuid4

from app.core.llm_env import resolve_llm_selection
from app.modules.agent.domain.exceptions import AgentConfigurationError
from app.modules.agent_run.application.ports import AgentRunRepositoryPort, AgentRunStarterPort
from app.modules.agent_run.domain.entities import AgentRun, StartAgentRunArtifact, StartAgentRunRequest


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
        )
        artifact = None
        if request.content is not None:
            artifact = StartAgentRunArtifact(
                id=str(uuid4()),
                content_hash=(
                    f"sha256:{hashlib.sha256(request.content.markdown.encode('utf-8')).hexdigest()}"
                ),
                markdown=request.content.markdown,
                purpose=request.content.purpose,
                document_id=request.content.document_id,
                base_version=request.content.base_version,
                target=request.content.target,
            )
        saved = self._repository.create_with_planning_job(run, str(uuid4()), artifact)
        return saved.id, saved.status
