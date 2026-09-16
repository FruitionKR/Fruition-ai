import os

from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.infrastructure.chat_completions_conversation_replier import (
    build_conversation_replier,
)
from app.modules.agent.infrastructure.chat_completions_turn_router import (
    build_agent_turn_router,
)
from app.modules.agent.interfaces.http.schemas import AgentTurnRequestBody
from app.modules.agent_run.application.start_agent_run import StartAgentRunUseCase
from app.modules.agent_run.infrastructure.postgres_agent_run_repository import (
    PostgresAgentRunRepository,
)
from app.modules.markdown_edit.application.generate_markdown_document import (
    GenerateMarkdownDocumentUseCase,
)
from app.modules.markdown_edit.application.generate_markdown_edit import (
    GenerateMarkdownEditUseCase,
)
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import (
    build_markdown_editor,
)
from app.modules.query.application.ports import QueryEventPublisherPort
from app.modules.query.interfaces.http.dependencies import (
    build_answer_query_use_case,
    build_query_conversation_summarizer,
)
from app.modules.skill.application.select_skill import SelectSkillUseCase
from app.modules.skill.infrastructure.postgres_skill_repository import (
    PostgresSkillRepository,
)
from app.modules.skill.interfaces.http.dependencies import (
    get_author_skill_use_case,
    get_propose_skill_draft_use_case,
)


def build_handle_agent_turn_use_case(
    *,
    provider: str,
    model: str,
    event_publisher: QueryEventPublisherPort | None = None,
    parent_run_id: str | None = None,
) -> HandleAgentTurnUseCase:
    """`event_publisher`는 질의 갈래에만 전달한다. markdown·skill 갈래는 진행 이벤트를 내지 않는다."""
    markdown_editor = build_markdown_editor(provider=provider, model=model)
    query_use_case = build_answer_query_use_case(
        provider=provider, model=model, event_publisher=event_publisher
    )
    feature_enabled = os.environ.get("AGENT_SKILLS_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    agent_run_repository = PostgresAgentRunRepository(parent_run_id=parent_run_id)
    return HandleAgentTurnUseCase(
        router=build_agent_turn_router(provider=provider, model=model),
        query_use_case=query_use_case,
        web_search_query_use_case_factory=lambda: build_answer_query_use_case(
            provider=provider, model=model, allow_web_search=True, event_publisher=event_publisher
        ),
        markdown_edit_use_case=GenerateMarkdownEditUseCase(markdown_editor),
        markdown_create_use_case=GenerateMarkdownDocumentUseCase(markdown_editor),
        skill_selector=SelectSkillUseCase(PostgresSkillRepository(), feature_enabled=feature_enabled),
        agent_run_starter=StartAgentRunUseCase(agent_run_repository, feature_enabled=feature_enabled),
        markdown_turn_repository=agent_run_repository,
        skill_authorer=(get_author_skill_use_case(provider=provider, model=model, parent_run_id=parent_run_id) if feature_enabled else None),
        skill_draft_proposer=(
            get_propose_skill_draft_use_case(provider=provider, model=model)
            if feature_enabled
            else None
        ),
        conversation_summarizer=build_query_conversation_summarizer(
            provider=provider, model=model
        ),
        conversation_replier=build_conversation_replier(
            provider=provider, model=model
        ),
    )


def get_handle_agent_turn_use_case(payload: AgentTurnRequestBody) -> HandleAgentTurnUseCase:
    return build_handle_agent_turn_use_case(provider=payload.provider, model=payload.model)
