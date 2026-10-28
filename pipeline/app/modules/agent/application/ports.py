from typing import Protocol

from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute
from app.modules.agent.domain.folder_sources import FolderSourceListing


class AgentTurnRouterPort(Protocol):
    def route(self, request: AgentTurnRequest) -> AgentTurnRoute:
        ...


class ConversationReplierPort(Protocol):
    def reply(self, request: AgentTurnRequest) -> str:
        ...


class FolderSourceReaderPort(Protocol):
    def read(
        self,
        *,
        workspace_id: str,
        user_id: str,
        folder_id: str,
        max_documents: int,
    ) -> FolderSourceListing:
        """폴더 바로 아래 사용자가 읽을 수 있는 Markdown 문서를 최근 수정순으로 최대 max_documents개 읽는다.

        읽을 수 없으면 FolderSourceUnavailableError를 낸다.
        """
        ...


class TaskClarifierPort(Protocol):
    def ask(self, request: AgentTurnRequest, route: AgentTurnRoute) -> str:
        """작업 탭 요청에서 진행에 꼭 필요한 정보를 묻는 질문 한 문장."""
        ...
