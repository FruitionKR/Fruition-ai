"""폴더 내용 조회에서 메시지가 지목한 폴더를 Jev가 고른다. 쓸 수 없으면 LLM 판단으로 처리한다.

선택지는 폴더 경로마다 하나, 이름이 같은 폴더가 여러 곳이면 그 이름 전부를 뜻하는 묶음 하나, 그리고 '지목 없음'이다.
Jev는 하나만 고르므로 같은 이름 폴더를 가릴 단서가 없는 요청은 묶음을 골라 되묻게 한다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from collections import defaultdict

from app.core.jev_client import JEV_FOLDER_LOOKUP_ENABLED_ENV, JevClient, JevUnavailable, build_jev_client
from app.modules.agent.application.ports import FolderReferenceResolverPort
from app.modules.agent.domain.entities import AgentTurnRequest
from app.modules.agent.domain.folder_lookup import FolderEntry
from app.modules.agent.infrastructure.chat_completions_folder_resolver import RECENT_MESSAGE_LIMIT
from app.modules.agent.infrastructure.jev_turn_router import MAX_CHOICES

NONE_CHOICE = "none"
FOLDER_POLICY = """사용자가 내용을 보려는 폴더를 고른다.
조사·따옴표·괄호·띄어쓰기·대소문자와 상관없이 뜻으로 판단한다(`회의록엔`, `「회의록」`은 `회의록`을 가리킨다).
`문서 폴더`처럼 폴더라고 부르면 그 폴더를 가리킨다. 다른 폴더의 내용을 묻는 말 속 단어는 폴더 지목이 아니다(`회의록 폴더에 문서 뭐 있어?`의 `문서`).
이름을 포함하는 다른 폴더는 다른 폴더다(`2026 회의록`은 `회의록`이 아니다). 문서 이름(`기획서`)은 폴더가 아니다.
`그 폴더`처럼 앞 대화를 가리키면 최근 대화로 판단한다.
같은 이름 폴더가 여러 곳이고 상위 폴더 같은 단서가 없으면 그 이름 전부를 고른다.
특정 폴더를 지목하지 않았으면(`이 폴더에 뭐 있어?`) 지목 없음을 고른다. 추측하지 않는다.
"""
logger = logging.getLogger(__name__)


def with_jev_folder_lookup(resolver: FolderReferenceResolverPort) -> FolderReferenceResolverPort:
    """`JEV_FOLDER_LOOKUP_ENABLED`와 API 키가 있으면 Jev를 먼저 쓰고, 아니면 LLM 판단을 그대로 쓴다."""
    client = build_jev_client(JEV_FOLDER_LOOKUP_ENABLED_ENV, interactive=True)
    return resolver if client is None else JevFolderResolver(client, resolver)


class JevFolderResolver:
    def __init__(self, client: JevClient, fallback: FolderReferenceResolverPort) -> None:
        self._client = client
        self._fallback = fallback

    def resolve(self, request: AgentTurnRequest, folders: list[FolderEntry]) -> list[str]:
        choices = folder_choices(folders)
        if len(choices) > MAX_CHOICES:
            return self._fallback.resolve(request, folders)
        conversation = request.conversation_context
        state = json.dumps({
            "message": request.message,
            "recent_messages": [
                {"role": message.role, "content": message.content}
                for message in (conversation.recent_messages[-RECENT_MESSAGE_LIMIT:] if conversation else ())
            ],
        }, ensure_ascii=False, indent=2)
        # 라우팅과 같이 요청 상태로 정한 고정 무작위 순서로 섞어 선택지 위치가 단서가 되지 않게 한다.
        keys = list(choices)
        random.Random(hashlib.sha256(state.encode()).hexdigest()).shuffle(keys)
        try:
            answers = self._client.choose(state, {
                "folder": {
                    "type": "choice",
                    "instructions": FOLDER_POLICY,
                    "criteria": {key: choices[key][0] for key in keys},
                }
            })
        except JevUnavailable as exc:
            logger.warning("[Jev folder 대체] %s", exc)
            return self._fallback.resolve(request, folders)
        return list(choices[answers["folder"]["choice"]][1])


def folder_choices(folders: list[FolderEntry]) -> dict[str, tuple[str, tuple[str, ...]]]:
    """선택지 키 → (설명, 폴더 id들). 폴더마다 하나, 같은 이름이 여러 곳이면 이름 묶음 하나, 그리고 지목 없음."""
    # LLM 경로 프롬프트와 같이 띄어쓰기·대소문자만 다른 이름은 같은 이름으로 본다.
    by_name: dict[str, list[FolderEntry]] = defaultdict(list)
    for folder in folders:
        by_name[_name_key(folder.name)].append(folder)
    choices: dict[str, tuple[str, tuple[str, ...]]] = {NONE_CHOICE: ("특정 폴더를 지목하지 않음", ())}
    for index, folder in enumerate(folders, start=1):
        same = len(by_name[_name_key(folder.name)])
        # 같은 이름이 여러 곳이면 개별 경로는 단서가 있을 때만 고르게 하고, 단서가 없으면 아래 묶음을 고르게 한다.
        hint = f" — 같은 이름 폴더가 {same}곳 있어, 이 경로를 가리키는 단서가 있을 때만" if same > 1 else ""
        top = ", 최상위" if len(folder.path) == 1 else ""
        choices[f"f{index}"] = (f"폴더 {_label(folder.name)} (경로: {' / '.join(folder.path)}{top}){hint}", (folder.id,))
    for index, same in enumerate((s for s in by_name.values() if len(s) > 1), start=1):
        name = same[0].name
        paths = ", ".join(" / ".join(folder.path) for folder in same)
        choices[f"n{index}"] = (
            f"이름이 {_label(name)}인 폴더 {len(same)}곳 전부 ({paths}) — 어느 쪽인지 단서 없이 이름만 말했을 때",
            tuple(folder.id for folder in same),
        )
    return choices


def _name_key(name: str) -> str:
    return "".join(name.split()).casefold()


def _label(name: str) -> str:
    # 대소문자만 다른 표기('archive')도 같은 폴더라는 것을 선택지에 드러낸다.
    lowered = name.casefold()
    return f"'{name}'" if lowered == name else f"'{name}'(또는 '{lowered}')"
