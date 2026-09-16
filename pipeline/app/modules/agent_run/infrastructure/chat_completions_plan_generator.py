import json
import os
from pathlib import Path
from typing import Any

from app.core.llm_env import (
    api_key_from_env,
    int_env,
    provider_api_key_env,
)
from app.core.untrusted_input import validate_untrusted_payload
from app.modules.agent_run.domain.plan import (
    AgentPlan,
    AgentPlanIntentClarificationRequired,
    AgentPlanOperation,
    build_agent_plan,
)
from app.modules.agent_run.domain.entities import ContentArtifactReference
from app.modules.wiki_generation.infrastructure.chat_completions_llm import ChatClientConfig, ChatCompletionsJsonClient


DEFAULT_PLAN_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "agent_folder_plan.system.md"
ALLOWED_PLAN_TOOLS = {
    "create_folder",
    "rename_folder",
    "move_folder",
    "move_document",
    "rename_document",
    "create_document",
    "apply_document_edit",
}


class ChatCompletionsPlanGenerator:
    def __init__(self, client: ChatCompletionsJsonClient, system_prompt: str) -> None:
        self._client = client
        self._system_prompt = system_prompt

    def generate(
        self,
        *,
        run_id: str,
        plan_id: str,
        version: int,
        instruction: str,
        routing_action: str,
        hierarchy: list[dict[str, object]],
        skill_instructions: str | None,
        allowed_tools: tuple[str, ...] | None,
        content_artifacts: tuple[ContentArtifactReference, ...] = (),
    ) -> AgentPlan:
        allowed_plan_tools = (
            ALLOWED_PLAN_TOOLS
            if allowed_tools is None
            else ALLOWED_PLAN_TOOLS.intersection(allowed_tools)
        )
        artifact_tools = {artifact.purpose for artifact in content_artifacts}
        if allowed_tools is None and artifact_tools:
            allowed_plan_tools = allowed_plan_tools.intersection(artifact_tools)
        elif allowed_tools is not None:
            allowed_plan_tools -= {"create_document", "apply_document_edit"} - artifact_tools
        payload = {
            "plan_id": plan_id,
            "instruction": instruction,
            "routing_action": routing_action,
            "hierarchy": hierarchy,
            "skill_instructions": skill_instructions,
            "allowed_tools": sorted(allowed_plan_tools),
            "content_artifacts": [
                {
                    "id": artifact.id,
                    "content_hash": artifact.content_hash,
                    "purpose": artifact.purpose,
                    "document_id": artifact.document_id,
                    "base_version": artifact.base_version,
                    "target": artifact.target,
                }
                for artifact in content_artifacts
            ],
        }
        validate_untrusted_payload(payload)
        trusted_identifiers = [plan_id]
        for item in hierarchy:
            for key in ("id", "parent_id"):
                identifier = item.get(key)
                if isinstance(identifier, str) and identifier:
                    trusted_identifiers.append(identifier)
        for artifact in content_artifacts:
            trusted_identifiers.extend(
                identifier
                for identifier in (artifact.id, artifact.content_hash, artifact.document_id)
                if identifier
            )
        value = self._client.complete_json(
            self._system_prompt,
            json.dumps(payload, ensure_ascii=False, indent=2),
            trusted_identifiers=tuple(dict.fromkeys(trusted_identifiers)),
        )
        if type(value.get("intent_confirmed")) is not bool:
            raise ValueError("Agent plan intent_confirmed must be a boolean.")
        if not value["intent_confirmed"]:
            if (
                value.get("operations") != []
                or not isinstance(value.get("summary"), str)
                or not value["summary"].strip()
            ):
                raise ValueError("Unconfirmed Agent plan must have a summary and no operations.")
            raise AgentPlanIntentClarificationRequired()
        plan = normalize_plan_candidate(run_id, plan_id, version, value)
        if any(operation.tool_name not in allowed_plan_tools for operation in plan.operations):
            raise ValueError("Agent plan contains a tool outside the trusted mutation scope.")
        _validate_plan_against_hierarchy(plan, hierarchy, content_artifacts)
        return plan


def normalize_plan_candidate(
    run_id: str,
    plan_id: str,
    version: int,
    value: dict[str, Any],
) -> AgentPlan:
    summary = value.get("summary")
    raw_operations = value.get("operations")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Agent plan summary is required.")
    if not isinstance(raw_operations, list) or not 1 <= len(raw_operations) <= 20:
        raise ValueError("Agent plan must contain between 1 and 20 operations.")
    operations: list[AgentPlanOperation] = []
    for index, raw in enumerate(raw_operations, start=1):
        if not isinstance(raw, dict):
            raise ValueError("Agent plan operation must be an object.")
        tool_name = _required_text(raw, "tool_name")
        if tool_name not in ALLOWED_PLAN_TOOLS:
            raise ValueError("Agent plan contains an unsupported tool.")
        target_type = _required_text(raw, "target_type")
        if target_type not in {"folder", "document"}:
            raise ValueError("Agent plan target_type is invalid.")
        arguments = raw.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("Agent plan arguments must be an object.")
        raw_dependencies = raw.get("depends_on", [])
        if not isinstance(raw_dependencies, list) or not all(isinstance(item, int) for item in raw_dependencies):
            raise ValueError("Agent plan dependencies must be operation sequence numbers.")
        operations.append(
            AgentPlanOperation(
                id=f"{plan_id}-op-{index}",
                sequence=index,
                tool_name=tool_name,  # type: ignore[arg-type]
                target_type=target_type,  # type: ignore[arg-type]
                target_id=_optional_text(raw.get("target_id")),
                base_version=_optional_int(raw.get("base_version")),
                source_parent_id=_optional_text(raw.get("source_parent_id")),
                destination_parent_id=_optional_text(raw.get("destination_parent_id")),
                arguments=arguments,
                reason=_required_text(raw, "reason"),
                depends_on=tuple(f"{plan_id}-op-{dependency}" for dependency in raw_dependencies),
            )
        )
    return build_agent_plan(plan_id, run_id, version, summary, tuple(operations))


def build_plan_generator(*, provider: str, model: str) -> ChatCompletionsPlanGenerator:
    api_key = api_key_from_env(provider=provider)
    if not api_key:
        raise RuntimeError(f"Set {provider_api_key_env(provider)}.")
    prompt_path = Path(os.environ.get("AGENT_PLAN_SYSTEM_PROMPT", str(DEFAULT_PLAN_PROMPT)))
    return ChatCompletionsPlanGenerator(
        ChatCompletionsJsonClient(
            ChatClientConfig(
                api_key=api_key,
                model=model,
                temperature=None if provider == "claude" else 0.0,
                timeout_seconds=int_env("AGENT_PLAN_LLM_TIMEOUT_SECONDS", 180),
                max_tokens=None,
                json_mode=True,
                provider=provider,
            )
        ),
        prompt_path.read_text(encoding="utf-8"),
    )


def _required_text(value: dict[str, Any], key: str) -> str:
    text = _optional_text(value.get(key))
    if text is None:
        raise ValueError(f"Agent plan {key} is required.")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Agent plan base_version must be an integer or null.")
    return value


def _validate_plan_against_hierarchy(
    plan: AgentPlan,
    hierarchy: list[dict[str, object]],
    content_artifacts: tuple[ContentArtifactReference, ...],
) -> None:
    items = {str(item.get("id")): item for item in hierarchy if item.get("id") is not None}
    operations = {operation.id: operation for operation in plan.operations}
    operation_ids = {operation.id for operation in plan.operations}
    required_arguments = {
        "create_folder": {"name", "parent_folder_id"},
        "rename_folder": {"folder_id", "name", "base_version"},
        "move_folder": {"folder_id", "parent_folder_id", "position", "base_version"},
        "move_document": {"document_id", "folder_id", "position", "base_version"},
        "rename_document": {"document_id", "display_name", "base_version"},
        "create_document": {"display_name", "folder_id", "content_artifact_id", "content_hash"},
        "apply_document_edit": {
            "document_id",
            "base_version",
            "target",
            "content_artifact_id",
            "content_hash",
        },
    }
    for operation in plan.operations:
        if set(operation.arguments) != required_arguments[operation.tool_name]:
            raise ValueError("Agent plan arguments do not match the tool contract.")
        expected_target_type = "folder" if operation.tool_name in {
            "create_folder",
            "rename_folder",
            "move_folder",
        } else "document"
        if operation.target_type != expected_target_type:
            raise ValueError("Agent plan target_type does not match the tool contract.")
        if operation.tool_name in {"create_folder", "create_document"}:
            if operation.target_id is not None or operation.base_version is not None:
                raise ValueError("Create operations cannot have an existing target or base_version.")
        else:
            item = items.get(operation.target_id or "")
            if item is None or item.get("type") != operation.target_type:
                raise ValueError("Agent plan target must exist in the hierarchy snapshot.")
            if (
                operation.tool_name != "apply_document_edit"
                and item.get("current_version") != operation.base_version
            ):
                raise ValueError("Agent plan base_version must match the hierarchy snapshot.")
            id_key = "folder_id" if operation.target_type == "folder" else "document_id"
            if operation.arguments.get(id_key) != operation.target_id:
                raise ValueError("Agent plan target id must match the tool arguments.")
            argument_base_version = operation.arguments.get("base_version")
            if argument_base_version != operation.base_version and not _valid_base_version_reference(
                operation,
                argument_base_version,
                operations,
            ):
                raise ValueError("Agent plan base_version must match the tool arguments.")
        destination_key = {
            "create_folder": "parent_folder_id",
            "create_document": "folder_id",
            "move_folder": "parent_folder_id",
            "move_document": "folder_id",
        }.get(operation.tool_name)
        if destination_key:
            destination = operation.arguments.get(destination_key)
            if (
                operation.tool_name in {"create_folder", "create_document"}
                and isinstance(destination, dict)
                and (
                    operation.destination_parent_id is not None
                    or not _valid_create_destination_reference(operation, destination, operations)
                )
            ):
                raise ValueError("Agent plan destination must match the tool arguments.")
            if not isinstance(destination, dict) and destination != operation.destination_parent_id:
                raise ValueError("Agent plan destination must match the tool arguments.")
            if isinstance(destination, str):
                target_folder = items.get(destination)
                if target_folder is None or target_folder.get("type") != "folder":
                    raise ValueError("Agent plan destination folder must exist.")
        if operation.tool_name in {"create_document", "apply_document_edit"}:
            _validate_artifact_arguments(operation)
            _validate_artifact_reference(operation, content_artifacts)
        references = _operation_references(operation.arguments)
        if not references.issubset(set(operation.depends_on)) or not references.issubset(operation_ids):
            raise ValueError("Agent plan result references must be declared dependencies.")


def _validate_artifact_arguments(operation: AgentPlanOperation) -> None:
    artifact_id = operation.arguments.get("content_artifact_id")
    content_hash = operation.arguments.get("content_hash")
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("Document mutation requires content_artifact_id.")
    if not isinstance(content_hash, str) or not content_hash.strip():
        raise ValueError("Document mutation requires content_hash.")
    if operation.tool_name == "apply_document_edit":
        target = operation.arguments.get("target")
        if not isinstance(target, dict) or set(target) != {"type", "start_line", "end_line"}:
            raise ValueError("apply_document_edit requires an exact target object.")
        if target.get("type") not in {"selection", "current_section", "whole_document"}:
            raise ValueError("apply_document_edit target type is invalid.")
        start_line = target.get("start_line")
        end_line = target.get("end_line")
        if (
            not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or not isinstance(end_line, int)
            or isinstance(end_line, bool)
            or start_line < 1
            or end_line < start_line
        ):
            raise ValueError("apply_document_edit target lines are invalid.")


def _validate_artifact_reference(
    operation: AgentPlanOperation,
    content_artifacts: tuple[ContentArtifactReference, ...],
) -> None:
    artifact = next(
        (
            candidate
            for candidate in content_artifacts
            if candidate.id == operation.arguments.get("content_artifact_id")
        ),
        None,
    )
    if artifact is None:
        raise ValueError("Document mutation artifact was not supplied by trusted context.")
    if artifact.content_hash != operation.arguments.get("content_hash"):
        raise ValueError("Document mutation content_hash does not match its artifact.")
    if artifact.purpose != operation.tool_name:
        raise ValueError("Document mutation artifact purpose does not match the tool.")
    if operation.tool_name == "apply_document_edit" and (
        artifact.document_id != operation.target_id
        or artifact.base_version != operation.base_version
        or artifact.target != operation.arguments.get("target")
    ):
        raise ValueError("Document edit artifact does not match the approved target.")


def _operation_references(value: object) -> set[str]:
    if isinstance(value, dict):
        if set(value) == {"$operation_result", "field"} and isinstance(value["$operation_result"], str):
            return {value["$operation_result"]}
        return set().union(*(_operation_references(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_operation_references(item) for item in value), set())
    return set()


def _valid_create_destination_reference(
    operation: AgentPlanOperation,
    value: dict[str, object],
    operations: dict[str, AgentPlanOperation],
) -> bool:
    if set(value) != {"$operation_result", "field"} or value.get("field") != "id":
        return False
    dependency_id = value.get("$operation_result")
    dependency = operations.get(dependency_id) if isinstance(dependency_id, str) else None
    return (
        dependency is not None
        and dependency.tool_name == "create_folder"
        and dependency.id in operation.depends_on
    )


def _valid_base_version_reference(
    operation: AgentPlanOperation,
    value: object,
    operations: dict[str, AgentPlanOperation],
) -> bool:
    if not isinstance(value, dict) or set(value) != {"$operation_result", "field"}:
        return False
    dependency_id = value.get("$operation_result")
    dependency = operations.get(dependency_id) if isinstance(dependency_id, str) else None
    return (
        value.get("field") == "current_version"
        and dependency is not None
        and dependency.id in operation.depends_on
        and dependency.target_id == operation.target_id
    )
