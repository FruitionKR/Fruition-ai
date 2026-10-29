"""Skill 의도 분류를 Jev가 고른다. 쓸 수 없으면 기존 LLM 분류로 처리하고, Skill 작성(generate)은 그대로 LLM이 한다.

선택지는 분류 결과 계약(`author_skill._intent_result`)을 통과하는 조합이다.
- 미지원·애매: skill_kinds 없음
- 지원: skill_kinds 조합 × reference_mode(참고 문서가 없으면 none만, fixed-template은 template이고 참고 문서가 1개일 때만)
  × requires_source(규칙으로 정해지지 않는 document-create일 때만 true/false. template은 규칙
  `inferred_requires_source`가 정해 분류값을 쓰지 않으므로 false만 둔다)
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import random
from typing import Any

from app.core.jev_client import JEV_SKILL_INTENT_ENABLED_ENV, JevClient, JevUnavailable, build_jev_client
from app.modules.skill.domain.entities import SkillAuthoringReference
from app.modules.skill.infrastructure.chat_completions_skill_authoring_generator import (
    ChatCompletionsSkillAuthoringGenerator,
    intent_payload,
)

SKILL_KINDS = ("document-create", "document-edit", "folder-organize", "template")
logger = logging.getLogger(__name__)


def with_jev_skill_intent(generator: ChatCompletionsSkillAuthoringGenerator):
    """`JEV_SKILL_INTENT_ENABLED`와 API 키가 있으면 분류만 Jev가 하고, 아니면 기존 생성기를 그대로 쓴다."""
    client = build_jev_client(JEV_SKILL_INTENT_ENABLED_ENV, interactive=True)
    return generator if client is None else JevSkillIntentClassifier(client, generator)


class JevSkillIntentClassifier:
    def __init__(self, client: JevClient, generator: ChatCompletionsSkillAuthoringGenerator) -> None:
        self._client = client
        self._generator = generator

    def classify(
        self,
        instruction: str,
        references: tuple[SkillAuthoringReference, ...],
        *,
        requested_description: str | None,
    ) -> dict[str, object]:
        state = json.dumps(intent_payload(instruction, references, requested_description), ensure_ascii=False, indent=2)
        options = intent_options(len(references))
        # 라우팅과 같이 요청 상태로 정한 고정 무작위 순서로 섞어 선택지 위치가 단서가 되지 않게 한다.
        random.Random(hashlib.sha256(state.encode()).hexdigest()).shuffle(options)
        choices = {f"c{index}": option for index, option in enumerate(options)}
        try:
            answers = self._client.choose(state, {
                "intent": {
                    "type": "choice",
                    "instructions": self._generator.classifier_prompt
                    + "\n\n허용된 분류 조합 중 요청과 맞는 하나를 고르세요.",
                    "criteria": choices,
                }
            })
        except JevUnavailable as exc:
            logger.warning("[Jev skill intent 대체] %s", exc)
            return self._generator.classify(instruction, references, requested_description=requested_description)
        return dict(choices[answers["intent"]["choice"]])

    def generate(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._generator.generate(*args, **kwargs)


def intent_options(reference_count: int) -> list[dict[str, object]]:
    options: list[dict[str, object]] = [
        {"decision": decision, "skill_kinds": [], "reference_mode": "none", "requires_source": False}
        for decision in ("unsupported", "ambiguous")
    ]
    for size in range(1, len(SKILL_KINDS) + 1):
        for kinds in itertools.combinations(SKILL_KINDS, size):
            modes = ["none", "structure-reference", "fixed-template"] if reference_count else ["none"]
            for mode in modes:
                # 고정 양식은 template Skill이 참고 문서 하나의 구조를 그대로 쓸 때만 만들 수 있다(author_skill).
                if mode == "fixed-template" and ("template" not in kinds or reference_count != 1):
                    continue
                classified = "document-create" in kinds and "template" not in kinds
                for requires_source in ((True, False) if classified else (False,)):
                    options.append({
                        "decision": "supported",
                        "skill_kinds": list(kinds),
                        "reference_mode": mode,
                        "requires_source": requires_source,
                    })
    return options
