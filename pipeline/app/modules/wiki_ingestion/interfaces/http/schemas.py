from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.llm_env import resolve_llm_selection

from app.modules.wiki_ingestion.application.models import (
    IngestOperationRestoreCommand,
    LintOperationRestoreCommand,
    RebuildPageCommand,
    RestoreContributionCommand,
    SourceSnapshotRestoreCommand,
    WikiMaintenanceCommand,
)


DOCUMENT_SEMANTIC_PROMPT = "prompts/semantic_extraction.system.md"
CHAT_SEMANTIC_PROMPT = "prompts/chat_semantic_extraction.system.md"


class _PipelineRunBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    operation_id: str | None = None
    input_name: str | None = None
    out: str | None = None
    mode: Literal["api", "generic-chat"] = "api"
    provider: Literal["openai", "gemini", "claude"]
    source_page_mode: Literal["auto", "skeleton", "section-polish"] = "auto"
    concept_page_mode: Literal[
        "auto",
        "api",
        "full-llm",
        "skeleton",
        "section-polish",
    ] = Field(
        default="auto",
        description="auto는 backend skeleton concept page만 생성합니다. section-polish를 명시하면 concept별 LLM polish를 수행합니다.",
    )
    max_packet_chars: int = 7000
    overlap_blocks: int = 1
    model: str
    concept_system_prompt: str = "prompts/concept_page_generation.system.md"
    concept_resolution_system_prompt: str = "prompts/concept_resolution.system.md"
    section_polish_system_prompt: str = "prompts/section_polish.system.md"
    wiki_evaluator_system_prompt: str = "prompts/wiki_generation_evaluator.system.md"
    existing_wiki_dir: str | None = None
    wiki_evaluation_loop: bool = True
    max_eval_attempts: int = 2
    save_debug_json: bool = Field(
        default=False,
        description="True이면 raw LLM output, packet, block_map 같은 디버그 JSON을 저장합니다.",
    )
    log_callback_url: str | None = Field(
        default=None,
        description="설정하면 pipeline.log 이벤트가 생길 때마다 이 URL로 JSON POST합니다.",
    )
    wait: bool = Field(
        default=False,
        description="True이면 요청 안에서 완료까지 기다립니다. False이면 백그라운드 실행 후 로그를 조회합니다.",
    )
    user_id: str | None = Field(
        default=None,
        description="기존 backend 요청 호환 필드이며 Wiki 저장 범위에는 사용하지 않습니다.",
    )
    workspace_id: str | None = Field(
        default=None,
        description="기존 backend 요청 호환 필드이며 Wiki 저장 범위에는 사용하지 않습니다.",
    )
    source_revision: int | None = None
    source_content_hash: str | None = None

    @model_validator(mode="after")
    def validate_model_selection(self) -> Self:
        resolve_llm_selection(self.provider, self.model)
        return self

class PipelineRunIn(_PipelineRunBase):
    system_prompt: str = DOCUMENT_SEMANTIC_PROMPT


class ReingestRunIn(_PipelineRunBase):
    input_markdown: str
    system_prompt: str = DOCUMENT_SEMANTIC_PROMPT


class ChatSourceBlockIn(BaseModel):
    """채팅 문답 1쌍. block_id는 `session_id:pair_id` provenance이고 text에는 그 id가 들어가지 않는다."""

    block_id: str = Field(
        min_length=1,
        description="`session_id:pair_id` 형식의 문답 provenance. source_blocks.block_id로 그대로 저장됩니다.",
    )
    text: str = Field(
        min_length=1,
        description="문답 본문. id를 포함하지 않습니다.",
    )


class ChatWikiRunIn(_PipelineRunBase):
    selection_mode: Literal["partial"] = Field(
        description="채팅 Wiki export는 항상 독립 source page를 생성하므로 partial만 지원합니다.",
    )
    input_markdown: str = Field(
        min_length=1,
        description="backend가 직렬화한 채팅 Markdown입니다. 문서 제목 추출에만 쓰이며 블록 분할에는 쓰지 않습니다.",
    )
    input_blocks: list[ChatSourceBlockIn] = Field(
        min_length=1,
        description="문답 단위 source block 목록입니다. 파이프라인은 Markdown을 다시 쪼개지 않고 이 목록을 그대로 씁니다.",
    )
    chat_system_prompt: str = CHAT_SEMANTIC_PROMPT


class PipelineRunOut(BaseModel):
    run_id: str
    status: str
    manifest: dict[str, Any] | None = None
    output_dir: str
    log_path: str


class RestoreContributionIn(BaseModel):
    operation_id: str
    document_id: str


class RebuildPageIn(BaseModel):
    page_id: str
    keep_contributions: list[RestoreContributionIn]


class SourceSnapshotRestoreIn(BaseModel):
    page_id: str
    document_id: str = Field(min_length=1)


class _OperationRestoreIn(BaseModel):
    operation_id: str
    workspace_id: str
    rebuild_pages: list[RebuildPageIn]
    deleted_pages: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_operation_restore(self) -> Self:
        rebuild_page_ids = {page.page_id for page in self.rebuild_pages}
        if rebuild_page_ids.intersection(self.deleted_pages):
            raise ValueError("deleted_pages must not include rebuild_pages")
        return self

    def kept_operation_ids(self) -> set[str]:
        return {
            item.operation_id
            for page in self.rebuild_pages
            for item in page.keep_contributions
        }

    def rebuild_commands(self) -> tuple[RebuildPageCommand, ...]:
        return tuple(
            RebuildPageCommand(
                page_id=page.page_id,
                keep_contributions=tuple(
                    RestoreContributionCommand(
                        operation_id=item.operation_id,
                        document_id=item.document_id,
                    )
                    for item in page.keep_contributions
                ),
            )
            for page in self.rebuild_pages
        )


class IngestOperationRestoreIn(_OperationRestoreIn):
    restore_to_operation_id: str | None
    cancel_operation_ids: list[str]
    source_page: SourceSnapshotRestoreIn

    @model_validator(mode="after")
    def validate_restore_range(self) -> Self:
        if not self.cancel_operation_ids:
            raise ValueError("cancel_operation_ids must not be empty")
        if len(set(self.cancel_operation_ids)) != len(self.cancel_operation_ids):
            raise ValueError("cancel_operation_ids must not contain duplicates")
        if self.operation_id in self.cancel_operation_ids:
            raise ValueError(
                "operation_id must not be included in cancel_operation_ids"
            )
        if self.operation_id == self.restore_to_operation_id:
            raise ValueError(
                "operation_id and restore_to_operation_id must be different"
            )
        if self.restore_to_operation_id in self.cancel_operation_ids:
            raise ValueError(
                "restore_to_operation_id must not be included in "
                "cancel_operation_ids"
            )
        if (
            self.restore_to_operation_id is not None
            and self.source_page.page_id in self.deleted_pages
        ):
            raise ValueError(
                "deleted_pages must not include restored source_page"
            )
        if any(
            page.page_id == self.source_page.page_id
            for page in self.rebuild_pages
        ):
            raise ValueError("source_page must not be included in rebuild_pages")
        if self.kept_operation_ids().intersection(self.cancel_operation_ids):
            raise ValueError(
                "cancel_operation_ids must not be included in "
                "keep_contributions"
            )
        return self

    def to_command(self) -> IngestOperationRestoreCommand:
        return IngestOperationRestoreCommand(
            operation_id=self.operation_id,
            restore_to_operation_id=self.restore_to_operation_id,
            cancel_operation_ids=tuple(self.cancel_operation_ids),
            workspace_id=self.workspace_id,
            source_page=SourceSnapshotRestoreCommand(
                page_id=self.source_page.page_id,
                document_id=self.source_page.document_id,
            ),
            rebuild_pages=self.rebuild_commands(),
            deleted_pages=tuple(self.deleted_pages),
        )


class LintOperationRestoreIn(_OperationRestoreIn):
    target_operation_id: str

    @model_validator(mode="after")
    def validate_target_operation(self) -> Self:
        if self.operation_id == self.target_operation_id:
            raise ValueError(
                "operation_id and target_operation_id must be different"
            )
        if self.target_operation_id in self.kept_operation_ids():
            raise ValueError(
                "target_operation_id must not be included in keep_contributions"
            )
        return self

    def to_command(self) -> LintOperationRestoreCommand:
        return LintOperationRestoreCommand(
            operation_id=self.operation_id,
            target_operation_id=self.target_operation_id,
            workspace_id=self.workspace_id,
            rebuild_pages=self.rebuild_commands(),
            deleted_pages=tuple(self.deleted_pages),
        )


class WikiPageLookupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_ids: list[str]
    workspace_id: str | None = None


class WikiPageRenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    update_slug: bool = False


class WikiLintIn(BaseModel):
    user_id: str = "local-user"
    workspace_id: str = "local-workspace"
    operation_id: str | None = None
    materialize_promotions: bool = False
    dry_run: bool = True
    provider: Literal["openai", "gemini", "claude"]
    model: str

    @model_validator(mode="after")
    def validate_operation_id(self) -> Self:
        if not self.dry_run and not str(self.operation_id or "").strip():
            raise ValueError("operation_id is required when dry_run is false")
        return self

    @model_validator(mode="after")
    def validate_model_selection(self) -> Self:
        resolve_llm_selection(self.provider, self.model)
        return self

    def to_command(self) -> WikiMaintenanceCommand:
        return WikiMaintenanceCommand(**self.model_dump())


class WikiLintOut(BaseModel):
    user_id: str
    workspace_id: str
    operation_id: str | None = None
    active_path: str
    cluster_count: int
    source_ref_count: int
    orphan_refs: list[str]
    promotion_candidates: list[str]
    needs_review: list[str]
    relation_candidates: list[dict[str, Any]]
    invalid_relations: list[dict[str, Any]]
    invalid_promotions: list[dict[str, Any]]
    reconciliation_candidates: list[dict[str, Any]]
    applied_reconciliations: list[dict[str, Any]]
    applied_cluster_reconciliation: dict[str, list[dict[str, Any]]]
    materialized_promotions: list[dict[str, Any]]
    merged_promotions: list[dict[str, Any]]
    materialized_relations: list[dict[str, Any]]
    orphan_link_candidates: list[dict[str, Any]] = Field(default_factory=list)
    removed_orphan_links: list[dict[str, Any]] = Field(default_factory=list)
    operation_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    changed_pages: list[dict[str, Any]] = Field(default_factory=list)
