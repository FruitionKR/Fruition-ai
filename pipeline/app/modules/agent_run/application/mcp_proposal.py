from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import unicodedata
from uuid import uuid4

from app.core.ai_markdown_sanitizer import external_urls, sanitize_ai_markdown
from app.modules.agent_run.application.plan_validation import prepare_agent_plan
from app.modules.agent_run.application.ports import (
    McpDocumentEditContextPort,
    McpProposalRepositoryPort,
)
from app.modules.agent_run.domain.edit_preview import build_edit_preview
from app.modules.agent_run.domain.content_duplicates import find_content_duplicates
from app.modules.agent_run.domain.plan_review import review_content_duplicates, review_duplicate_names
from app.modules.agent_run.domain.plan import AgentPlanOperation, build_agent_plan
from app.modules.skill.application.author_skill import accept_external_skill_definition
from app.modules.skill.application.manage_skill import ManageSkillUseCase
from app.modules.skill.domain.exceptions import SkillVersionConflictError
from app.modules.agent_run.domain.entities import (
    AgentRun,
    ContentArtifactReference,
    McpProposalCommand,
    McpProposalResult,
    McpPlanContext,
    StartAgentRunArtifact,
)
from app.modules.markdown_edit.domain.entities import (
    MarkdownEditOperation,
    MarkdownEditRequest,
    MarkdownEditTarget,
)
from app.modules.markdown_edit.domain.markdown_output_contract import validate_markdown_output
from app.modules.markdown_edit.domain.markdown_target_scope import (
    apply_markdown_edit,
    markdown_line_count,
)


class McpProposalError(ValueError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        operation_error: dict[str, object] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.operation_error = operation_error
        self.retryable = retryable


def mcp_proposal_request_hash(command: McpProposalCommand) -> str:
    actor = asdict(command.actor)
    actor.pop("invocation_id")
    canonical = {
        "schema_version": "mcp.agent-proposal.v1",
        "actor": actor,
        "proposal": command.proposal,
    }
    try:
        payload = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise McpProposalError(400, "INVALID_INPUT", "Proposal body is not canonical JSON.") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _finalized_conversion(*args: str) -> bool:
    from app.modules.mcp_pdf_conversion.service import finalized_conversion_exists
    return finalized_conversion_exists(*args)


class CreateMcpProposalUseCase:
    def __init__(
        self,
        repository: McpProposalRepositoryPort,
        context_reader: McpDocumentEditContextPort,
        skill_manager: ManageSkillUseCase | None = None,
        finalized_conversion=None,
    ) -> None:
        self._repository = repository
        self._context_reader = context_reader
        self._skill_manager = skill_manager
        # 원본 연결 create_document가 이 사용자의 MCP PDF 변환 조립 결과 그대로인지 확인한다.
        self._finalized_conversion = finalized_conversion or _finalized_conversion

    def execute(self, command: McpProposalCommand) -> McpProposalResult:
        if command.actor.tool_name == "propose_plan":
            return self._execute_plan(command)
        if command.actor.tool_name == "propose_skill_definition":
            return self._execute_skill_definition(command)
        operation, submitted_artifact = _single_edit(command)
        document_id = str(operation["target_id"])
        base_version = int(operation["base_version"])

        # Current authorization and document state are checked before idempotency lookup.
        context = self._context_reader.read(command.actor, document_id, base_version)
        _validate_context(command, operation, submitted_artifact, context)

        expected_request_hash = mcp_proposal_request_hash(command)
        if command.request_hash != expected_request_hash:
            raise McpProposalError(400, "INVALID_INPUT", "request_hash does not match the proposal body.")
        existing = self._repository.find_mcp_proposal(
            command.actor, command.idempotency_key, expected_request_hash
        )
        if existing is not None:
            return existing

        raw_replacement = str(submitted_artifact["markdown"])
        raw_content_hash = "sha256:" + hashlib.sha256(raw_replacement.encode("utf-8")).hexdigest()
        if submitted_artifact["content_hash"] != raw_content_hash:
            raise McpProposalError(400, "INVALID_INPUT", "Artifact content_hash does not match raw Markdown.")

        target_dict = dict(operation["arguments"]["target"])  # type: ignore[index]
        target = MarkdownEditTarget(
            type=target_dict["type"],  # type: ignore[arg-type]
            start_line=target_dict["start_line"],  # type: ignore[arg-type]
            end_line=target_dict["end_line"],  # type: ignore[arg-type]
        )
        request = MarkdownEditRequest(
            instruction=str(command.proposal["instruction"]),
            markdown=context.current_markdown,
            target=target,
            workspace_id=command.actor.workspace_id,
            user_id=command.actor.user_id,
            edit_operation="replace",
        )
        sanitized_replacement = sanitize_ai_markdown(
            raw_replacement,
            keep_urls=external_urls(context.current_markdown),
        )
        failures = validate_markdown_output(request, sanitized_replacement)
        if failures:
            raise McpProposalError(400, "INVALID_INPUT", "Artifact Markdown violates the edit contract.")

        final_markdown = apply_markdown_edit(
            context.current_markdown,
            MarkdownEditOperation(
                operation="replace",
                target=target,
                summary=str(operation["reason"]),
                replacement_markdown=sanitized_replacement,
            ),
        )
        if final_markdown == context.current_markdown:
            raise McpProposalError(
                400,
                "OBVIOUS_NO_OP",
                "The proposal does not change the document.",
                operation_error={
                    "operation_id": operation["id"],
                    "sequence": 1,
                    "tool_name": "apply_document_edit",
                    "reason_code": "UNCHANGED_TARGET",
                },
            )

        run_id = str(uuid4())
        plan_id = str(uuid4())
        artifact_id = f"{run_id}-artifact-1"
        final_hash = "sha256:" + hashlib.sha256(final_markdown.encode("utf-8")).hexdigest()
        candidate = {
            "summary": command.proposal["request_summary"],
            "operations": [
                {
                    "tool_name": "apply_document_edit",
                    "target_type": "document",
                    "target_id": document_id,
                    "base_version": base_version,
                    "source_parent_id": context.parent_folder_id,
                    "destination_parent_id": context.parent_folder_id,
                    "arguments": {
                        "document_id": document_id,
                        "base_version": base_version,
                        "target": target_dict,
                        "content_artifact_id": artifact_id,
                        "content_hash": final_hash,
                    },
                    "reason": operation["reason"],
                    "depends_on": [],
                }
            ],
        }
        reference = ContentArtifactReference(
            id=artifact_id,
            content_hash=final_hash,
            purpose="apply_document_edit",
            document_id=document_id,
            base_version=base_version,
            target=target_dict,
        )
        plan = prepare_agent_plan(
            run_id=run_id,
            plan_id=plan_id,
            version=1,
            candidate=candidate,
            hierarchy=[
                {
                    "id": document_id,
                    "type": "document",
                    "current_version": base_version,
                    "parent_id": context.parent_folder_id,
                }
            ],
            content_artifacts=(reference,),
            allowed_tools={"apply_document_edit"},
            target_folder_id=None,
        )
        preview_json = json.dumps(
            build_edit_preview(context.current_markdown, final_markdown),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        artifact = StartAgentRunArtifact(
            id=artifact_id,
            content_hash=final_hash,
            markdown=final_markdown,
            purpose="apply_document_edit",
            document_id=document_id,
            base_version=base_version,
            target=target_dict,
            display_name=context.display_name,
            summary=str(operation["reason"]),
            preview_json=preview_json,
        )
        run = AgentRun(
            id=run_id,
            workspace_id=command.actor.workspace_id,
            user_id=command.actor.user_id,
            action="workspace_workflow",
            skill_version_id=None,
            status="awaiting_approval",
            request_summary=str(command.proposal["request_summary"]),
            current_plan_id=plan_id,
            origin=command.actor.origin,
            grant_id=command.actor.grant_id,
            client_id=command.actor.client_id,
            policy_generation=command.actor.policy_generation,
            invocation_id=command.actor.invocation_id,
            spec_version=command.actor.spec_version,
            tool_name=command.actor.tool_name,
            proposal_request_hash=expected_request_hash,
        )
        return self._repository.create_mcp_proposal(
            run=run,
            plan=plan,
            artifacts=(artifact,),
            actor=command.actor,
            idempotency_key=command.idempotency_key,
            request_hash=expected_request_hash,
        )

    def _execute_skill_definition(self, command: McpProposalCommand) -> McpProposalResult:
        """Skill 정의 하나를 LLM 없이 검증해 publish_skill_definition 단일 operation으로 승인 대기에 올린다.

        operation arguments는 이어받은 값까지 확정한 저장 정의이고 operation_hash로 승인에 묶인다.
        실행은 Document가 현재 승인·grant·policy·skill.author를 확인한 뒤 /skills/mcp-acceptances로 한다.
        """
        if self._skill_manager is None:
            raise McpProposalError(503, "DEPENDENCY_UNAVAILABLE", "Skill management is unavailable.")
        expected_request_hash = mcp_proposal_request_hash(command)
        if command.request_hash != expected_request_hash:
            raise McpProposalError(400, "INVALID_INPUT", "request_hash does not match the proposal body.")
        definition = dict(command.proposal["definition"])  # type: ignore[arg-type]
        try:
            result, canonical = accept_external_skill_definition(
                self._skill_manager,
                workspace_id=command.actor.workspace_id,
                user_id=command.actor.user_id,
                skill_id=definition["skill_id"],  # type: ignore[arg-type]
                expected_version=definition["expected_version"],  # type: ignore[arg-type]
                scope_type=definition["scope_type"],  # type: ignore[arg-type]
                name=str(definition["name"]),
                description=str(definition["description"]),
                instructions_markdown=str(definition["instructions_markdown"]),
                capabilities=_optional_tuple(definition["capabilities"]),  # type: ignore[arg-type]
                targets=_optional_tuple(definition["targets"]),  # type: ignore[arg-type]
                result_mode=definition["result_mode"],  # type: ignore[arg-type]
                requires_source=definition["requires_source"],  # type: ignore[arg-type]
            )
        except SkillVersionConflictError as exc:
            raise McpProposalError(409, "SOURCE_CHANGED", "Skill version changed.") from exc
        except ValueError as exc:
            # 관리 권한 없음·존재하지 않는 Skill도 같은 거절로 숨긴다.
            raise McpProposalError(400, "INVALID_INPUT", "Skill definition is invalid.") from exc
        if canonical is None or result.status != "proposal_ready":
            raise McpProposalError(400, "INVALID_INPUT", "Skill definition has safety issues.")
        existing = self._repository.find_mcp_proposal(
            command.actor, command.idempotency_key, expected_request_hash
        )
        if existing is not None:
            return existing
        run_id = str(uuid4())
        plan_id = str(uuid4())
        summary = str(command.proposal["request_summary"]).strip()
        plan = build_agent_plan(plan_id, run_id, 1, summary, (
            AgentPlanOperation(
                id=f"{plan_id}-op-1",
                sequence=1,
                tool_name="publish_skill_definition",
                target_type="skill",
                target_id=canonical["skill_id"],  # type: ignore[arg-type]
                base_version=canonical["expected_version"],  # type: ignore[arg-type]
                source_parent_id=None,
                destination_parent_id=None,
                arguments=canonical,
                reason=summary,
            ),
        ))
        run = AgentRun(
            id=run_id,
            workspace_id=command.actor.workspace_id,
            user_id=command.actor.user_id,
            action="workspace_workflow",
            skill_version_id=None,
            status="awaiting_approval",
            request_summary=summary,
            current_plan_id=plan_id,
            origin=command.actor.origin,
            grant_id=command.actor.grant_id,
            client_id=command.actor.client_id,
            policy_generation=command.actor.policy_generation,
            invocation_id=command.actor.invocation_id,
            spec_version=command.actor.spec_version,
            tool_name=command.actor.tool_name,
            proposal_request_hash=expected_request_hash,
        )
        return self._repository.create_mcp_proposal(
            run=run,
            plan=plan,
            artifacts=(),
            actor=command.actor,
            idempotency_key=command.idempotency_key,
            request_hash=expected_request_hash,
        )

    def _execute_plan(self, command: McpProposalCommand) -> McpProposalResult:
        expected_request_hash = mcp_proposal_request_hash(command)
        if command.request_hash != expected_request_hash:
            raise McpProposalError(400, "INVALID_INPUT", "request_hash does not match the proposal body.")
        operations = command.proposal.get("operations")
        submitted_artifacts = command.proposal.get("artifacts")
        if not isinstance(operations, list) or not isinstance(submitted_artifacts, list):
            raise McpProposalError(400, "INVALID_INPUT", "Plan proposal is invalid.")
        resources, edits = _plan_context_request(operations)
        context = self._context_reader.read_plan(
            command.actor,
            command.proposal.get("target_folder_id"),  # type: ignore[arg-type]
            resources,
            edits,
        )
        if context.workspace_id != command.actor.workspace_id:
            raise McpProposalError(409, "SOURCE_CHANGED", "Plan context workspace changed.")
        _validate_plan_parent_bindings(operations, context)
        existing = self._repository.find_mcp_proposal(
            command.actor, command.idempotency_key, expected_request_hash
        )
        if existing is not None:
            return existing

        run_id, plan_id = str(uuid4()), str(uuid4())
        artifact_by_id = {
            str(item["id"]): item for item in submitted_artifacts if isinstance(item, dict)
        }
        persisted_artifact_ids = {
            str(item["id"]): f"{run_id}-artifact-{index}"
            for index, item in enumerate(submitted_artifacts, start=1)
            if isinstance(item, dict)
        }
        edit_by_id = {item.document_id: item for item in context.edit_documents}
        stored_artifacts: list[StartAgentRunArtifact] = []
        references: list[ContentArtifactReference] = []
        final_hashes: dict[str, str] = {}
        for operation in operations:
            if not isinstance(operation, dict) or operation["tool_name"] not in {
                "create_document", "apply_document_edit"
            }:
                continue
            arguments = operation["arguments"]
            artifact = artifact_by_id.get(str(arguments["content_artifact_id"]))
            if artifact is None:
                raise McpProposalError(400, "INVALID_INPUT", "Document mutation artifact is missing.")
            if artifact.get("purpose") != operation["tool_name"]:
                raise McpProposalError(400, "INVALID_INPUT", "Artifact purpose does not match its operation.")
            raw = str(artifact["markdown"])
            raw_hash = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
            if artifact["content_hash"] != raw_hash or arguments["content_hash"] != raw_hash:
                raise McpProposalError(400, "INVALID_INPUT", "Artifact content_hash does not match raw Markdown.")
            preview_json = None
            if operation["tool_name"] == "create_document":
                if "source_document_id" in arguments:
                    # PDF 변환본은 서버가 기존 조립·sanitize로 만든 결과만 받는다. 클라이언트가 쓴 Markdown은 받지 않는다.
                    if not self._finalized_conversion(
                        command.actor.workspace_id, command.actor.user_id, str(arguments["source_document_id"]),
                        str(arguments["source_content_hash"]), raw_hash,
                    ):
                        raise McpProposalError(400, "INVALID_INPUT", "PDF conversion artifact is not a finalized conversion.")
                    markdown = raw
                else:
                    markdown = sanitize_ai_markdown(raw, keep_urls=set())
                if markdown != raw:
                    raise McpProposalError(400, "INVALID_INPUT", "Artifact Markdown violates the storage contract.")
                document_id = base_version = target = None
                display_name = str(arguments["display_name"])
                preview_json = json.dumps(
                    build_edit_preview("", markdown), ensure_ascii=False, separators=(",", ":")
                )
            else:
                document_id = str(operation["target_id"])
                edit = edit_by_id.get(document_id)
                if edit is None:
                    raise McpProposalError(409, "SOURCE_CHANGED", "Current edit document context is missing.")
                _validate_edit_document(operation, artifact, edit, command)
                target = dict(arguments["target"])
                request = MarkdownEditRequest(
                    instruction=str(operation["reason"]), markdown=edit.current_markdown,
                    target=MarkdownEditTarget(**target), workspace_id=command.actor.workspace_id,
                    user_id=command.actor.user_id, edit_operation="replace",
                )
                replacement = sanitize_ai_markdown(raw, keep_urls=external_urls(edit.current_markdown))
                if validate_markdown_output(request, replacement):
                    raise McpProposalError(400, "INVALID_INPUT", "Artifact Markdown violates the edit contract.")
                markdown = apply_markdown_edit(
                    edit.current_markdown,
                    MarkdownEditOperation(
                        operation="replace", target=request.target,
                        summary=str(operation["reason"]), replacement_markdown=replacement,
                    ),
                )
                if markdown == edit.current_markdown:
                    _no_op(operation)
                base_version = edit.current_version
                display_name = edit.display_name
                preview_json = json.dumps(
                    build_edit_preview(edit.current_markdown, markdown),
                    ensure_ascii=False, separators=(",", ":"),
                )
            content_hash = "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            final_hashes[str(artifact["id"])] = content_hash
            persisted_id = persisted_artifact_ids[str(artifact["id"])]
            stored_artifacts.append(StartAgentRunArtifact(
                id=persisted_id, content_hash=content_hash, markdown=markdown,
                purpose=operation["tool_name"], document_id=document_id,
                base_version=base_version, target=target, display_name=display_name,
                summary=str(operation["reason"]), preview_json=preview_json,
            ))
            references.append(ContentArtifactReference(
                id=persisted_id, content_hash=content_hash,
                purpose=operation["tool_name"], document_id=document_id,
                base_version=base_version, target=target,
            ))

        candidate = _plan_candidate(
            command, operations, plan_id, final_hashes, persisted_artifact_ids
        )
        try:
            plan = prepare_agent_plan(
                run_id=run_id, plan_id=plan_id, version=1, candidate=candidate,
                hierarchy=list(context.hierarchy), content_artifacts=tuple(references),
                allowed_tools={
                    "create_folder", "rename_folder", "move_folder", "move_document",
                    "rename_document", "create_document", "apply_document_edit",
                },
                target_folder_id=command.proposal.get("target_folder_id"),  # type: ignore[arg-type]
                allow_source_bound_create=True,
            )
        except ValueError as exc:
            raise McpProposalError(400, "INVALID_INPUT", str(exc)) from exc
        _reject_hierarchy_no_ops(plan, list(context.hierarchy), operations)
        plan = review_duplicate_names(plan, list(context.hierarchy))
        create_artifacts = {artifact.id: artifact for artifact in stored_artifacts}
        create_documents = [
            {
                "id": operation.id,
                "name": operation.arguments["display_name"],
                "created_at": f"{operation.sequence:02d}",
            }
            for operation in plan.operations
            if operation.tool_name == "create_document"
        ]
        create_contents = {
            operation.id: create_artifacts[str(operation.arguments["content_artifact_id"])].markdown
            for operation in plan.operations
            if operation.tool_name == "create_document"
        }
        if len(create_documents) > 1:
            plan = review_content_duplicates(
                plan, find_content_duplicates(create_documents, create_contents).duplicates
            )
        run = AgentRun(
            id=run_id, workspace_id=command.actor.workspace_id, user_id=command.actor.user_id,
            action="workspace_workflow", skill_version_id=None, status="awaiting_approval",
            request_summary=str(command.proposal["request_summary"]), current_plan_id=plan_id,
            target_folder_id=command.proposal.get("target_folder_id"),  # type: ignore[arg-type]
            origin=command.actor.origin, grant_id=command.actor.grant_id,
            client_id=command.actor.client_id, policy_generation=command.actor.policy_generation,
            invocation_id=command.actor.invocation_id, spec_version=command.actor.spec_version,
            tool_name=command.actor.tool_name, proposal_request_hash=expected_request_hash,
        )
        return self._repository.create_mcp_proposal(
            run=run, plan=plan, artifacts=tuple(stored_artifacts), actor=command.actor,
            idempotency_key=command.idempotency_key, request_hash=expected_request_hash,
        )


def _single_edit(command: McpProposalCommand) -> tuple[dict[str, object], dict[str, object]]:
    proposal = command.proposal
    operations = proposal.get("operations")
    artifacts = proposal.get("artifacts")
    if not isinstance(operations, list) or len(operations) != 1 or not isinstance(artifacts, list) or len(artifacts) != 1:
        raise McpProposalError(400, "UNSUPPORTED_TOOL", "I4 accepts exactly one document edit.")
    operation = operations[0]
    artifact = artifacts[0]
    if not isinstance(operation, dict) or not isinstance(artifact, dict):
        raise McpProposalError(400, "INVALID_INPUT", "Proposal operation and artifact are invalid.")
    return operation, artifact


def _plan_context_request(
    operations: list[object],
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    resources: dict[tuple[str, str], dict[str, object]] = {}
    edits: list[dict[str, object]] = []
    for value in operations:
        if not isinstance(value, dict):
            continue
        target_id = value.get("target_id")
        target_type = value.get("target_type")
        base_version = value.get("base_version")
        if isinstance(target_id, str) and target_type in {"folder", "document"}:
            resources[(str(target_type), target_id)] = {
                "resource_type": target_type,
                "resource_id": target_id,
                "base_version": base_version,
            }
        arguments = value.get("arguments")
        if not isinstance(arguments, dict):
            continue
        destination_key = {
            "create_folder": "parent_folder_id", "create_document": "folder_id",
            "move_folder": "parent_folder_id", "move_document": "folder_id",
        }.get(value.get("tool_name"))
        destination = arguments.get(destination_key) if destination_key else None
        if isinstance(destination, str):
            resources.setdefault(("folder", destination), {
                "resource_type": "folder", "resource_id": destination, "base_version": None,
            })
        if value.get("tool_name") == "apply_document_edit" and isinstance(target_id, str):
            edits.append({"document_id": target_id, "base_version": base_version})
    return tuple(resources.values()), tuple(edits)


def _validate_plan_parent_bindings(operations: list[object], context: McpPlanContext) -> None:
    items = {
        str(item.get("id")): item for item in context.hierarchy if item.get("id") is not None
    }
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        created = operation.get("tool_name") in {"create_folder", "create_document"}
        if created:
            if operation.get("source_parent_id") is not None:
                raise McpProposalError(409, "SOURCE_CHANGED", "Create source parent must be null.")
            continue
        item = items.get(str(operation.get("target_id")))
        if item is None or item.get("parent_id") != operation.get("source_parent_id"):
            raise McpProposalError(409, "SOURCE_CHANGED", "Operation source parent changed.")
        if (
            operation.get("tool_name") in {"rename_folder", "rename_document", "apply_document_edit"}
            and item.get("parent_id") != operation.get("destination_parent_id")
        ):
            raise McpProposalError(409, "SOURCE_CHANGED", "Operation destination parent changed.")


def _rewrite_result_refs(value: object, ids: dict[str, str]) -> object:
    if isinstance(value, dict):
        if set(value) == {"$operation_result", "field"}:
            operation_id = value.get("$operation_result")
            return {
                **value,
                "$operation_result": ids.get(operation_id, operation_id)
                if isinstance(operation_id, str) else operation_id,
            }
        return {key: _rewrite_result_refs(item, ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_result_refs(item, ids) for item in value]
    return value


def _plan_candidate(
    command: McpProposalCommand,
    operations: list[object],
    plan_id: str,
    final_hashes: dict[str, str],
    persisted_artifact_ids: dict[str, str],
) -> dict[str, object]:
    raw = [item for item in operations if isinstance(item, dict)]
    sequences = {str(item["id"]): int(item["sequence"]) for item in raw}
    internal_ids = {operation_id: f"{plan_id}-op-{sequence}" for operation_id, sequence in sequences.items()}
    normalized: list[dict[str, object]] = []
    for item in raw:
        arguments = _rewrite_result_refs(dict(item["arguments"]), internal_ids)
        if isinstance(arguments, dict):
            artifact_id = arguments.get("content_artifact_id")
            if isinstance(artifact_id, str) and artifact_id in final_hashes:
                arguments["content_hash"] = final_hashes[artifact_id]
                arguments["content_artifact_id"] = persisted_artifact_ids[artifact_id]
        normalized.append({
            key: value for key, value in item.items() if key not in {"id", "sequence", "depends_on", "arguments"}
        } | {
            "arguments": arguments,
            "depends_on": [sequences[str(value)] for value in item["depends_on"]],
        })
    return {"summary": command.proposal["request_summary"], "operations": normalized}


def _validate_edit_document(operation, artifact, edit, command) -> None:
    arguments = operation["arguments"]
    target = arguments["target"]
    line_count = markdown_line_count(edit.current_markdown)
    current_hash = "sha256:" + hashlib.sha256(edit.current_markdown.encode("utf-8")).hexdigest()
    if (
        edit.current_version != operation["base_version"]
        or edit.current_version != arguments["base_version"]
        or edit.current_version != artifact["base_version"]
        or edit.document_id != artifact["document_id"]
        or artifact["target"] != target
        or edit.content_hash != current_hash
        or target["end_line"] > line_count
        or (
            target["type"] == "whole_document"
            and (target["start_line"] != 1 or target["end_line"] != line_count)
        )
    ):
        raise McpProposalError(409, "SOURCE_CHANGED", "Edit artifact does not match current document context.")


def _no_op(operation: dict[str, object]) -> None:
    raise McpProposalError(
        400, "OBVIOUS_NO_OP", "The proposal contains an operation that does not change its target.",
        operation_error={
            "operation_id": operation["id"], "sequence": operation["sequence"],
            "tool_name": operation["tool_name"], "reason_code": "UNCHANGED_TARGET",
        },
    )


def _normalized_name(value: object, item_type: object) -> str:
    name = unicodedata.normalize("NFC", str(value or "")).strip().casefold()
    return name.removesuffix(".md").strip() if item_type == "document" else name


def _reject_hierarchy_no_ops(
    plan, hierarchy: list[dict[str, object]], submitted: list[object]
) -> None:
    current = {str(item.get("id")): item for item in hierarchy if item.get("id") is not None}
    for operation in plan.operations:
        submitted_id = operation.id
        if operation.sequence <= len(submitted) and isinstance(submitted[operation.sequence - 1], dict):
            submitted_id = str(submitted[operation.sequence - 1].get("id", operation.id))
        item = current.get(operation.target_id or "")
        if item is None:
            continue
        if operation.tool_name in {"rename_folder", "rename_document"}:
            key = "name" if operation.tool_name == "rename_folder" else "display_name"
            if _normalized_name(item.get("name"), item.get("type")) == _normalized_name(
                operation.arguments.get(key), item.get("type")
            ):
                _no_op({
                    "id": submitted_id, "sequence": operation.sequence,
                    "tool_name": operation.tool_name,
                })
        if operation.tool_name in {"move_folder", "move_document"}:
            destination_key = "parent_folder_id" if operation.tool_name == "move_folder" else "folder_id"
            destination = operation.arguments.get(destination_key)
            if (
                not isinstance(destination, dict)
                and destination == item.get("parent_id")
                and operation.arguments.get("position") == item.get("position")
            ):
                _no_op({
                    "id": submitted_id, "sequence": operation.sequence,
                    "tool_name": operation.tool_name,
                })


def _validate_context(command, operation, artifact, context) -> None:
    instruction = command.proposal["instruction"]
    normalized = instruction.strip() if isinstance(instruction, str) else None
    arguments = operation["arguments"]
    target = arguments["target"]
    line_count = markdown_line_count(context.current_markdown)
    if (
        not normalized
        or command.proposal["request_summary"] != normalized
        or operation["reason"] != normalized
        or command.proposal["target_folder_id"] is not None
        or context.workspace_id != command.actor.workspace_id
        or context.document_id != operation["target_id"]
        or context.current_version != operation["base_version"]
        or arguments["document_id"] != context.document_id
        or arguments["base_version"] != context.current_version
        or artifact["document_id"] != context.document_id
        or artifact["base_version"] != context.current_version
        or artifact["target"] != target
        or artifact["id"] != arguments["content_artifact_id"]
        or artifact["content_hash"] != arguments["content_hash"]
        or operation["source_parent_id"] != context.parent_folder_id
        or operation["destination_parent_id"] != context.parent_folder_id
    ):
        raise McpProposalError(409, "SOURCE_CHANGED", "Proposal bindings do not match current document context.")
    start = target["start_line"]
    end = target["end_line"]
    if end > line_count or (target["type"] == "whole_document" and (start != 1 or end != line_count)):
        raise McpProposalError(409, "SOURCE_CHANGED", "Proposal target does not match current document lines.")
    current_hash = "sha256:" + hashlib.sha256(context.current_markdown.encode("utf-8")).hexdigest()
    if context.content_hash != current_hash:
        raise McpProposalError(503, "DEPENDENCY_UNAVAILABLE", "Document context hash is inconsistent.", retryable=True)


def _optional_tuple(value: object) -> tuple[str, ...] | None:
    return None if value is None else tuple(value)  # type: ignore[arg-type]
