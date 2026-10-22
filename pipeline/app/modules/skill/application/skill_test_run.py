from dataclasses import dataclass
from typing import Literal

from app.modules.markdown_edit.application.generate_markdown_document import GenerateMarkdownDocumentUseCase
from app.modules.markdown_edit.application.generate_markdown_edit import GenerateMarkdownEditUseCase
from app.modules.markdown_edit.domain.entities import (
    MarkdownCreateRequest,
    MarkdownEditRequest,
    MarkdownEditTarget,
)
from app.modules.markdown_edit.domain.markdown_target_scope import (
    apply_markdown_edit,
    markdown_line_count,
    markdown_line_range,
)
from app.modules.skill.application.author_skill import AuthorSkillUseCase
from app.modules.skill.domain.entities import SkillCapability
from app.modules.skill.domain.exceptions import SkillTestRunUnsupportedError
from app.modules.skill.domain.instruction_placeholders import (
    EDIT_PAYLOAD_DOCUMENT_REFERENCE,
    REFERENCE_CONTEXT_DOCUMENT_REFERENCE,
    render_skill_instructions,
)
from app.modules.skill.domain.policy import CAPABILITY_TOOLS, canonical_allowed_tools
from app.modules.skill.domain.section_changes import SectionChange, section_changes


@dataclass(frozen=True)
class SkillTestRunResult:
    status: Literal["completed", "blocked"]
    changes: tuple[SectionChange, ...] = ()
    untouched_sections: tuple[str, ...] = ()
    result_markdown: str | None = None
    issues: tuple[dict[str, object], ...] = ()


class SkillTestRunUseCase:
    """게시 전 Skill 정의를 문서 하나에 적용해 본다. Skill·문서·Agent run을 저장하지 않는다."""

    def __init__(
        self,
        authorer: AuthorSkillUseCase,
        markdown_edit_use_case: GenerateMarkdownEditUseCase,
        markdown_create_use_case: GenerateMarkdownDocumentUseCase,
    ) -> None:
        self._authorer = authorer
        self._markdown_edit_use_case = markdown_edit_use_case
        self._markdown_create_use_case = markdown_create_use_case

    def execute(
        self,
        *,
        workspace_id: str,
        user_id: str,
        name: str,
        description: str,
        instructions_markdown: str,
        capabilities: tuple[SkillCapability, ...],
        document_id: str,
        markdown: str,
        target: MarkdownEditTarget | None,
    ) -> SkillTestRunResult:
        require_test_runnable(capabilities)
        _validate_target(markdown, target)
        # 게시와 같은 안전 검토(preserve)를 거친다. 막히면 편집 LLM을 부르지 않는다.
        reviewed = self._authorer.execute(
            workspace_id=workspace_id,
            user_id=user_id,
            scope_type="personal",
            name=name,
            description=description,
            instruction=instructions_markdown,
            reference_document_ids=(),
            allow_clarification=False,
            authoring_mode="preserve",
            preserved_capabilities=capabilities,
            preserved_allowed_tools=canonical_allowed_tools(capabilities),
        )
        if reviewed.status == "blocked":
            return SkillTestRunResult(status="blocked", issues=tuple(issue.__dict__ for issue in reviewed.issues))
        if reviewed.status != "proposal_ready" or reviewed.proposal is None:
            raise ValueError("Skill test run review did not return a reviewed definition.")
        instructions = reviewed.proposal.instructions_markdown
        if "document-edit" in capabilities:
            return self._edit(name, instructions, workspace_id, user_id, markdown, target)
        return self._create(name, instructions, workspace_id, user_id, document_id, markdown)

    def _edit(
        self,
        name: str,
        instructions: str,
        workspace_id: str,
        user_id: str,
        markdown: str,
        target: MarkdownEditTarget | None,
    ) -> SkillTestRunResult:
        line_count = markdown_line_count(markdown)
        selection = (
            markdown_line_range(markdown, target.start_line, target.end_line)
            if target is not None and target.type == "selection"
            else None
        )
        if target is None or target.type == "whole_document":
            target = MarkdownEditTarget(type="whole_document", start_line=1, end_line=line_count)
        instruction = f"'{name}' Skill의 지침을 이 문서에 적용해 주세요."
        # 문서 본문은 편집 payload에 이미 있으므로 `{문서}`는 그 위치를 가리키게 한다.
        instructions = render_skill_instructions(
            instructions,
            request=instruction,
            selection=selection,
            document_reference=EDIT_PAYLOAD_DOCUMENT_REFERENCE,
        )
        # Agent markdown_edit와 같은 use case에 초안 지침을 넣어 실제 실행 결과와 같게 만든다.
        edit = self._markdown_edit_use_case.execute(
            MarkdownEditRequest(
                instruction=instruction,
                markdown=markdown,
                target=target,
                workspace_id=workspace_id,
                user_id=user_id,
                edit_goal="other",
                edit_operation="replace",
                edit_destination="target",
                skill_instructions=instructions,
            )
        ).edit
        edited = apply_markdown_edit(markdown, edit)
        changes, untouched = section_changes(markdown, edited, edit)
        return SkillTestRunResult(
            status="completed",
            changes=changes,
            untouched_sections=untouched,
            result_markdown=edited,
        )

    def _create(
        self,
        name: str,
        instructions: str,
        workspace_id: str,
        user_id: str,
        document_id: str,
        markdown: str,
    ) -> SkillTestRunResult:
        # 문서 생성형 Skill은 고른 문서를 참고 자료로 새 문서를 만든다. 만들 문서 본문만 돌려준다.
        instruction = f"'{name}' Skill의 지침과 형식으로 참고 문서를 바탕으로 새 문서 초안을 작성해 주세요."
        instructions = render_skill_instructions(
            instructions,
            request=instruction,
            document_reference=REFERENCE_CONTEXT_DOCUMENT_REFERENCE,
        )
        document = self._markdown_create_use_case.execute(
            MarkdownCreateRequest(
                instruction=instruction,
                workspace_id=workspace_id,
                user_id=user_id,
                reference_context={"source_document": {"document_id": document_id, "markdown": markdown}},
                skill_instructions=instructions,
            )
        ).document
        return SkillTestRunResult(status="completed", result_markdown=document.markdown)


def require_test_runnable(capabilities: tuple[SkillCapability, ...]) -> None:
    if not capabilities or any(capability not in CAPABILITY_TOOLS for capability in capabilities):
        raise ValueError("capabilities must contain supported values.")
    if "folder-organize" in capabilities:
        raise SkillTestRunUnsupportedError()


def _validate_target(markdown: str, target: MarkdownEditTarget | None) -> None:
    """LLM 호출 전에 범위가 원본 줄 안인지 확인한다."""
    if target is None or target.type == "whole_document":
        return
    if target.end_line < target.start_line or target.end_line > markdown_line_count(markdown):
        raise ValueError("target must be within the document lines.")


def skill_test_run_command(run_id: str, workspace_id: str, user_id: str) -> dict[str, str]:
    """작업 journal에 남기는 테스트 실행 command. 본문 없이 신원만 담아 실행·취소가 같은 해시를 쓴다."""
    return {"run_id": run_id, "kind": "skill_test_run", "workspace_id": workspace_id, "user_id": user_id}
