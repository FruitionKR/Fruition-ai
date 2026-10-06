"""Jev가 계약을 통과하는 route 조합 중 하나를 고르는 라우터.

평가(output/reports/05·14)와 같은 방식이다. 먼저 작업 조합을 고르고, 편집이면 편집 목표·방식·위치
조합을 한 번 더 고른다. clarify는 평가에서 Jev 정확도가 0%여서 선택지에서 빼고, Skill 후보가
겹쳐 clarify가 필요할 수 있는 요청은 기존 라우터가 처리한다. Jev를 쓸 수 없으면 기존 라우터로 간다.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import random
from typing import Any

from app.core.jev_client import JEV_ROUTING_ENABLED_ENV, JevClient, JevUnavailable, build_jev_client
from app.core.llm_prompt import with_llm_security_boundary
from app.modules.agent.application.ports import AgentTurnRouterPort
from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute
from app.modules.agent.infrastructure.chat_completions_turn_router import (
    ALLOWED_ACTIONS,
    ALLOWED_DOCUMENT_OPERATIONS,
    ALLOWED_EDIT_DESTINATIONS,
    ALLOWED_EDIT_GOALS,
    ALLOWED_EDIT_OPERATIONS,
    ALLOWED_RETRIEVAL_SOURCES,
    _local_guard,
    _normalize_route,
    _route_failures,
    load_agent_turn_router_prompt,
    route_payload,
)
from app.modules.skill.domain.policy import CAPABILITY_TOOLS

# 평가(run_jev_controlled.PairedChoiceClient)의 ROUTE_FIELDS·SKILL_ROUTE_FIELDS와 같은 순서다.
ROUTE_CHOICE_FIELDS = (
    "action",
    "retrieval_source",
    "document_operation",
    "persist",
    "required_capabilities",
    "selected_skill_id",
    "skill_candidates",
)
EDIT_CHOICE_FIELDS = (
    "action",
    "retrieval_source",
    "document_operation",
    "persist",
    "required_capabilities",
    "edit_goal",
    "edit_operation",
    "edit_destination",
    "selected_skill_id",
    "skill_candidates",
)
MAX_CHOICES = 255
JEV_ROUTE_REASON = "Jev가 허용된 작업 조합에서 선택"
logger = logging.getLogger(__name__)


def with_jev_routing(router: AgentTurnRouterPort) -> AgentTurnRouterPort:
    """`JEV_ROUTING_ENABLED`와 API 키가 있으면 Jev를 먼저 쓰고, 아니면 기존 라우터를 그대로 쓴다."""
    client = build_jev_client(JEV_ROUTING_ENABLED_ENV)
    if client is None:
        return router
    return JevTurnRouter(client, router, load_agent_turn_router_prompt())


class JevTurnRouter(AgentTurnRouterPort):
    def __init__(self, client: JevClient, fallback: AgentTurnRouterPort, system_prompt: str) -> None:
        self._client = client
        self._fallback = fallback
        self._system_prompt = system_prompt

    def route(self, request: AgentTurnRequest) -> AgentTurnRoute:
        guarded = _local_guard(request)
        if guarded is not None:
            return guarded
        if _may_need_skill_clarification(request):
            return self._fallback.route(request)
        payload = route_payload(request)
        # 기존 라우터가 모델에 보내는 사용자 입력과 같은 형식이다.
        state = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            selected = self._choose(payload, state, route_options(request), ROUTE_CHOICE_FIELDS)
            if selected["document_operation"] == "edit":
                selected = self._choose(payload, state, edit_options(selected, request), EDIT_CHOICE_FIELDS)
        except JevUnavailable as exc:
            logger.warning("[Jev route 대체] %s", exc)
            return self._fallback.route(request)
        route, failures = _normalize_route(selected)
        failures.extend(_route_failures(route, request))
        if failures:
            logger.warning("[Jev route 대체] contractFailures=%s", failures)
            return self._fallback.route(request)
        return route

    def _choose(
        self,
        payload: dict[str, object],
        state: str,
        options: list[dict[str, Any]],
        fields: tuple[str, ...],
    ) -> dict[str, Any]:
        if not 1 <= len(options) <= MAX_CHOICES:
            raise JevUnavailable(f"route 선택지 수가 Jev 범위를 벗어났습니다: {len(options)}")
        detail = fields == EDIT_CHOICE_FIELDS
        # 평가와 같이 요청 상태로 정한 고정 무작위 순서로 섞어 선택지 위치가 단서가 되지 않게 한다.
        seed = hashlib.sha256(
            json.dumps({"state": payload, "detail": detail}, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        options = list(options)
        random.Random(seed).shuffle(options)
        choices = {f"r{index}": option for index, option in enumerate(options)}
        answers = self._client.choose(
            state,
            {
                "route": {
                    "type": "choice",
                    "instructions": with_llm_security_boundary(self._system_prompt)
                    + (
                        "\n\n허용된 완전한 편집 조합 중 사용자 요청과 맞는 하나를 고르세요."
                        if detail
                        else "\n\n허용된 작업 조합 중 사용자 요청과 맞는 하나를 고르세요. "
                        "편집 목표와 위치는 선택한 작업 안에서 다음 단계가 결정합니다."
                    ),
                    "criteria": {
                        key: {field: option[field] for field in fields}
                        for key, option in choices.items()
                    },
                }
            },
        )
        answer = answers["route"]
        confidence = answer.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0.0 <= confidence <= 1.0:
            confidence = 0.0
        return {**choices[answer["choice"]], "confidence": float(confidence)}


def route_options(request: AgentTurnRequest) -> list[dict[str, Any]]:
    """정답과 무관하게 기존 route 계약을 통과하는 작업 조합을 열거한다."""
    capabilities = sorted(CAPABILITY_TOOLS)
    groups = [
        list(group)
        for size in range(len(capabilities) + 1)
        for group in itertools.combinations(capabilities, size)
    ]
    options = []
    for action, source, operation, group in itertools.product(
        sorted(ALLOWED_ACTIONS - {"clarify"}),
        sorted(ALLOWED_RETRIEVAL_SOURCES),
        sorted(ALLOWED_DOCUMENT_OPERATIONS),
        groups,
    ):
        value = {
            "action": action,
            "retrieval_source": source,
            "document_operation": operation,
            "persist": action in {"folder_organize", "workspace_workflow"},
            "required_capabilities": group,
            # 편집 목표·방식·위치는 편집 단계에서 다시 고른다.
            "edit_goal": {"none": None, "create": "create_from_chat", "edit": "other"}[operation],
            "edit_operation": "replace" if operation == "edit" else None,
            "edit_destination": "target" if operation == "edit" else None,
            "confidence": 1.0,
            "reason": JEV_ROUTE_REASON,
            "selected_skill_id": None,
            "skill_candidates": [],
        }
        route, failures = _normalize_route(value)
        if failures or _route_failures(route, request):
            continue
        options.append(value)
        if request.skill_mode != "off" and group:
            for skill in request.available_skills:
                if set(group).issubset(_supported_capabilities(skill.capabilities)):
                    options.append({**value, "selected_skill_id": skill.id})
    return options


def edit_options(value: dict[str, Any], request: AgentTurnRequest) -> list[dict[str, Any]]:
    options = []
    for goal, operation, destination in itertools.product(
        sorted(ALLOWED_EDIT_GOALS - {"create_from_chat"}),
        sorted(ALLOWED_EDIT_OPERATIONS),
        sorted(ALLOWED_EDIT_DESTINATIONS),
    ):
        candidate = {
            **value,
            "edit_goal": goal,
            "edit_operation": operation,
            "edit_destination": destination,
        }
        route, failures = _normalize_route(candidate)
        if not failures and not _route_failures(route, request):
            options.append(candidate)
    return options


def _may_need_skill_clarification(request: AgentTurnRequest) -> bool:
    if request.skill_mode == "off":
        return False
    supported = [_supported_capabilities(skill.capabilities) for skill in request.available_skills]
    return any(left & right for left, right in itertools.combinations(supported, 2))


def _supported_capabilities(capabilities: tuple[str, ...]) -> set[str]:
    supported = set(capabilities)
    if "template" in supported:
        supported.update({"document-create", "document-edit"})
    return supported
