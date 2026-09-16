from dataclasses import dataclass, field
from typing import Any


class WikiMaintenanceConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class WikiMaintenanceCommand:
    user_id: str
    workspace_id: str
    provider: str
    model: str
    operation_id: str | None = None
    materialize_promotions: bool = False
    dry_run: bool = True


@dataclass(frozen=True)
class RestoreContributionCommand:
    operation_id: str
    document_id: str


@dataclass(frozen=True)
class RebuildPageCommand:
    page_id: str
    keep_contributions: tuple[RestoreContributionCommand, ...]


@dataclass(frozen=True)
class SourceSnapshotRestoreCommand:
    page_id: str
    document_id: str


@dataclass(frozen=True)
class IngestOperationRestoreCommand:
    operation_id: str
    restore_to_operation_id: str | None
    cancel_operation_ids: tuple[str, ...]
    workspace_id: str
    source_page: SourceSnapshotRestoreCommand
    rebuild_pages: tuple[RebuildPageCommand, ...]
    deleted_pages: tuple[str, ...] = ()


@dataclass(frozen=True)
class LintOperationRestoreCommand:
    operation_id: str
    target_operation_id: str
    workspace_id: str
    rebuild_pages: tuple[RebuildPageCommand, ...]
    deleted_pages: tuple[str, ...] = ()


@dataclass(frozen=True)
class PipelineRunRegistration:
    run_id: str
    document_id: str | None
    user_id: str | None
    workspace_id: str | None
    input_source: str
    output_dir: str
    mode: str


@dataclass(frozen=True)
class PipelineRunCommand:
    run_id: str | None
    input: str
    input_name: str
    out: str
    user_id: str
    workspace_id: str
    operation_id: str | None = None
    source_document_id: str | None = None
    source_revision: int | None = None
    source_content_hash: str | None = None
    selection_mode: str | None = None
    reingest: bool = False
    input_markdown: str | None = None
    # 채팅 경로 전용. 있으면 Markdown을 다시 쪼개지 않고 이 블록 목록을 그대로 쓴다.
    input_blocks: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "api"
    provider: str | None = None
    source_page_mode: str = "auto"
    concept_page_mode: str = "auto"
    max_packet_chars: int = 7000
    overlap_blocks: int = 1
    model: str | None = None
    timeout_seconds: int = 180
    system_prompt: str = "prompts/semantic_extraction.system.md"
    concept_system_prompt: str = "prompts/concept_page_generation.system.md"
    concept_resolution_system_prompt: str = "prompts/concept_resolution.system.md"
    section_polish_system_prompt: str = "prompts/section_polish.system.md"
    wiki_evaluator_system_prompt: str = "prompts/wiki_generation_evaluator.system.md"
    wiki_patch_system_prompt: str = "prompts/wiki_generation_patch.system.md"
    existing_wiki_dir: str | None = None
    existing_concept_index: list[dict[str, Any]] = field(default_factory=list)
    existing_source_artifact: dict[str, Any] | None = None
    existing_source_markdown: str | None = None
    existing_source_blocks: list[dict[str, Any]] = field(default_factory=list)
    wiki_evaluation_loop: bool = True
    max_eval_attempts: int = 2
    save_debug_json: bool = False
    log_path: str | None = None
    log_callback_url: str | None = None
