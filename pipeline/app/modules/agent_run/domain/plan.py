from dataclasses import dataclass
import hashlib
import json
from typing import Literal


PlanToolName = Literal[
    "create_folder",
    "rename_folder",
    "move_folder",
    "move_document",
    "rename_document",
    "create_document",
    "apply_document_edit",
]
OperationStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "forbidden",
    "conflicted",
    "verification_failed",
    "cancelled",
    "rolled_back",
]


class AgentPlanIntentClarificationRequired(ValueError):
    """사용자 요청만으로 변경 의도를 확인할 수 없다.

    question은 무엇이 불명확한지 사용자에게 설명하는 한국어 문장이다. str(exc)는 error_code로 쓰이므로
    question과 무관하게 "mutation_intent_required"로 유지한다."""

    def __init__(self, question: str | None = None) -> None:
        super().__init__("mutation_intent_required")
        self.question = (question or "").strip() or None


@dataclass(frozen=True)
class AgentPlanOperation:
    id: str
    sequence: int
    tool_name: PlanToolName
    target_type: Literal["folder", "document"]
    target_id: str | None
    base_version: int | None
    source_parent_id: str | None
    destination_parent_id: str | None
    arguments: dict[str, object]
    reason: str
    depends_on: tuple[str, ...] = ()
    status: OperationStatus = "pending"
    error_code: str | None = None


@dataclass(frozen=True)
class AgentPlan:
    id: str
    run_id: str
    version: int
    summary: str
    operation_hash: str
    operations: tuple[AgentPlanOperation, ...]
    status: str = "awaiting_approval"


def build_agent_plan(
    plan_id: str,
    run_id: str,
    version: int,
    summary: str,
    operations: tuple[AgentPlanOperation, ...],
) -> AgentPlan:
    if not 1 <= len(operations) <= 20:
        raise ValueError("Agent plan must contain between 1 and 20 operations.")
    if version < 1:
        raise ValueError("Agent plan version must be positive.")
    operation_ids = {operation.id for operation in operations}
    if len(operation_ids) != len(operations):
        raise ValueError("Agent plan operation ids must be unique.")
    expected_sequences = list(range(1, len(operations) + 1))
    if sorted(operation.sequence for operation in operations) != expected_sequences:
        raise ValueError("Agent plan operation sequence must be contiguous.")
    sequence_by_id = {operation.id: operation.sequence for operation in operations}
    for operation in operations:
        if any(dependency not in operation_ids for dependency in operation.depends_on):
            raise ValueError("Agent plan dependency must reference an operation in the same plan.")
        if any(sequence_by_id[dependency] >= operation.sequence for dependency in operation.depends_on):
            raise ValueError("Agent plan dependency must reference an earlier operation.")
    return AgentPlan(
        id=plan_id,
        run_id=run_id,
        version=version,
        summary=summary.strip(),
        operation_hash=_operation_hash(operations),
        operations=operations,
    )


# 부분 승인에서 고르지 않은 작업의 상태 코드. 실패가 아니므로 run 결과 판정에서 뺀다.
NOT_SELECTED_ERROR_CODE = "not_selected"


class AgentPlanSelectionInvalid(ValueError):
    """부분 승인으로 고른 작업 목록이 현재 계획과 맞지 않는다."""


def selected_operation_ids(plan: AgentPlan, operation_ids: list[str] | None) -> tuple[str, ...] | None:
    """부분 승인할 작업 ID를 계획에 비춰 검사한다. None이면 전체 승인이다.

    계획의 모든 작업을 고르면 전체 승인과 같아 None을 돌려준다. 고른 작업이 고르지 않은 작업의 결과에
    기대면(depends_on) 실행할 수 없으므로 거절한다.
    """
    if operation_ids is None:
        return None
    if not operation_ids:
        raise AgentPlanSelectionInvalid("operation_ids must not be empty.")
    if len(set(operation_ids)) != len(operation_ids):
        raise AgentPlanSelectionInvalid("operation_ids must not contain duplicates.")
    planned = {operation.id: operation for operation in plan.operations}
    unknown = [operation_id for operation_id in operation_ids if operation_id not in planned]
    if unknown:
        raise AgentPlanSelectionInvalid("operation_ids must reference operations in the current plan.")
    selected = set(operation_ids)
    for operation_id in operation_ids:
        if any(dependency not in selected for dependency in planned[operation_id].depends_on):
            raise AgentPlanSelectionInvalid("Selected operations must include every operation they depend on.")
    if selected == set(planned):
        return None
    return tuple(operation.id for operation in plan.operations if operation.id in selected)


def _operation_hash(operations: tuple[AgentPlanOperation, ...]) -> str:
    canonical = [
        {
            "id": operation.id,
            "sequence": operation.sequence,
            "tool_name": operation.tool_name,
            "target_type": operation.target_type,
            "target_id": operation.target_id,
            "base_version": operation.base_version,
            "source_parent_id": operation.source_parent_id,
            "destination_parent_id": operation.destination_parent_id,
            "arguments": operation.arguments,
            "depends_on": list(operation.depends_on),
        }
        for operation in sorted(operations, key=lambda item: item.sequence)
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
