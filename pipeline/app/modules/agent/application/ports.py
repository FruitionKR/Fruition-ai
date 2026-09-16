from typing import Protocol

from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute


class AgentTurnRouterPort(Protocol):
    def route(self, request: AgentTurnRequest) -> AgentTurnRoute:
        ...


class ConversationReplierPort(Protocol):
    def reply(self, request: AgentTurnRequest) -> str:
        ...
