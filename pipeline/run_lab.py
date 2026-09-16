#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from time import monotonic
from typing import Any

from app.modules.wiki_generation.application.run_generation_loop import (
    EvaluationGuardRepairer,
    generation_evaluation_status,
)
from app.modules.wiki_generation.application.section_polish_mapping import (
    map_polish_output as _map_polish_output,
)
from app.modules.wiki_generation.application.judge_candidates import (
    judge_concept_update_candidates,
    normalize_concept_update_decisions,
)
from app.modules.wiki_generation.domain.entities import SourceBlock, SourceDocument
from app.modules.wiki_generation.infrastructure.assemble import (
    ConceptPageAssembler,
    GeneratedConceptPageAssembler,
    LinkBuilder,
    MeaningClusterArtifactAssembler,
    SourcePageAssembler,
)
from app.modules.wiki_generation.infrastructure.extract import MarkdownBlockExtractor
from app.modules.wiki_generation.infrastructure.generation_loop_adapters import (
    EvaluationArtifactAdapter,
    GenerationEvaluatorAdapter,
    SemanticGenerationAdapter,
)
from app.modules.wiki_generation.infrastructure.wiki_generation_evaluator_graph import (
    LangGraphWikiGenerationEvaluator,
)
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ApiConceptResolver,
    ApiConceptPageGenerator,
    ApiSectionPolisher,
    ChatClientConfig,
    ChatCompletionsJsonClient,
    SectionPolishParseError,
)
from app.modules.wiki_generation.infrastructure.concept_resolution import (
    apply_concept_resolutions,
    load_existing_concept_index,
    normalize_hint_resolution_output,
    normalize_resolution_output,
)
from app.modules.wiki_generation.infrastructure.normalize import SemanticNormalizer
from app.modules.wiki_generation.infrastructure.packet import SemanticPacketBuilder
from app.modules.wiki_generation.infrastructure.pipeline_log import PipelineLog
from app.modules.wiki_ingestion.infrastructure.object_storage import pipeline_log_uri
from app.modules.wiki_generation.infrastructure.prompt_io import collect_concept_source_blocks
from app.modules.wiki_generation.infrastructure.source_context_merge import (
    active_source_artifact,
    source_context_blocks,
    source_page_context_normalized,
)
from app.modules.wiki_ingestion.application.concept_contribution import (
    build_concept_contributions,
)
from app.modules.wiki_ingestion.application.models import PipelineRunCommand
from app.modules.wiki_ingestion.domain.source_block_changes import (
    SourceBlockChanges,
    compare_source_blocks,
)
from app.modules.wiki_ingestion.infrastructure.file_io import ensure_dir, write_json, write_text
from app.core.llm_env import (
    SUPPORTED_LLM_PROVIDERS,
    provider_api_key_env,
    resolve_llm_selection,
    resolve_llm_provider_defaults,
)


class PipelineConfigurationError(ValueError):
    """실행 전 설정 결함(API 키·모델·프롬프트 파일 누락).

    SystemExit은 worker의 `except Exception`을 통과해 프로세스를 죽이고
    Kafka 재전달 crash loop를 만들므로, 일반 예외로 던져 run 실패로 기록되게 한다.
    CLI 경로는 main()에서 SystemExit으로 변환한다.
    """


@dataclass(frozen=True)
class PipelinePrompts:
    semantic: str
    concept: str
    concept_resolution: str
    section_polish: str
    wiki_evaluator: str
    wiki_patch: str


@dataclass(frozen=True)
class WikiPageOutputs:
    source_page: dict[str, Any]
    source_page_normalized: dict[str, Any]
    concept_pages: list[dict[str, Any]]
    links: list[dict[str, Any]]
    source_page_mode: str
    concept_page_mode: str


@dataclass(frozen=True)
class _SourcePagePreparation:
    normalized: dict[str, Any]
    existing_context_blocks: list[SourceBlock]
    polish: dict[str, Any]
    key_points_for_concepts: list[dict[str, Any]]
    mode: str
    section_polisher: ApiSectionPolisher | None
    raw_polish_dir: Path | None
    invalid_polish_dir: Path


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Fruition API pipeline lab")
    ap.add_argument("--input", required=True, help="Input Markdown file")
    ap.add_argument("--out", default="runs/latest", help="Output directory")
    ap.add_argument("--mode", choices=["api", "generic-chat"], default="api", help="api/generic-chat=OpenAI-compatible chat-completions")
    ap.add_argument(
        "--provider",
        choices=SUPPORTED_LLM_PROVIDERS,
        required=True,
    )
    ap.add_argument(
        "--source-page-mode",
        choices=["auto", "skeleton", "section-polish"],
        default="auto",
        help="auto/section-polish: backend source page assembly with LLM-polished summary/key points; skeleton: backend only",
    )
    ap.add_argument(
        "--concept-page-mode",
        choices=["auto", "api", "full-llm", "skeleton", "section-polish"],
        default="auto",
        help="auto/skeleton: backend concept pages only; section-polish: optional LLM-polished concept sections; api/full-llm: legacy full concept page LLM writer",
    )
    ap.add_argument("--max-packet-chars", type=int, default=7000)
    ap.add_argument("--overlap-blocks", type=int, default=1)

    ap.add_argument("--model", required=True)
    ap.add_argument("--timeout-seconds", type=int, default=180)
    ap.add_argument("--log-path", help="Pipeline progress log path. Default: {out}/pipeline.log")
    ap.add_argument("--log-callback-url", help="Optional URL to POST each Korean pipeline log event to")
    ap.add_argument("--save-debug-json", action="store_true", help="Save intermediate/debug JSON such as raw LLM outputs, document.json, block_map.json, and api_config.json")
    ap.add_argument("--user-id", default=os.environ.get("WIKI_USER_ID", "local-user"), help="Wiki artifact owner namespace")
    ap.add_argument("--workspace-id", default=os.environ.get("WIKI_WORKSPACE_ID", "local-workspace"), help="Wiki artifact workspace namespace")

    ap.add_argument("--system-prompt", default="prompts/semantic_extraction.system.md")
    ap.add_argument("--concept-system-prompt", default="prompts/concept_page_generation.system.md")
    ap.add_argument("--concept-resolution-system-prompt", default="prompts/concept_resolution.system.md")
    ap.add_argument("--section-polish-system-prompt", default="prompts/section_polish.system.md")
    ap.add_argument("--wiki-evaluator-system-prompt", default="prompts/wiki_generation_evaluator.system.md")
    ap.add_argument("--wiki-patch-system-prompt", default="prompts/wiki_generation_patch.system.md")
    ap.add_argument("--existing-wiki-dir", help="Optional existing wiki directory. If set, existing wiki/concepts/*.md pages are used for concept resolution before page generation.")
    ap.add_argument(
        "--wiki-evaluation-loop",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Evaluate normalized wiki generation and retry semantic extraction with evaluator feedback when needed",
    )
    ap.add_argument("--max-eval-attempts", type=int, default=2)
    return ap.parse_args()




def resolve_api_defaults(command: PipelineRunCommand) -> PipelineRunCommand:
    """Resolve and validate the fixed provider/model selection."""
    provider, model = resolve_llm_selection(command.provider, command.model)
    defaults = resolve_llm_provider_defaults(
        provider=provider,
        model=model,
    )
    return replace(
        command,
        provider=defaults.provider,
        model=defaults.model,
    )


def read_prompt(path_like: str) -> str:
    prompt_path = Path(path_like)
    if not prompt_path.exists():
        prompt_path = Path(__file__).parent / path_like
    if not prompt_path.exists():
        raise PipelineConfigurationError(f"Prompt not found: {path_like}")
    return prompt_path.read_text(encoding="utf-8")


def load_api_client(args: PipelineRunCommand) -> ChatCompletionsJsonClient:
    api_key_env = provider_api_key_env(args.provider)
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise PipelineConfigurationError(f"Missing API key. Set {api_key_env}")
    if not args.model:
        raise PipelineConfigurationError("Missing model. Pass --model")
    return ChatCompletionsJsonClient(
        ChatClientConfig(
            api_key=api_key,
            model=args.model,
            temperature=None,
            timeout_seconds=args.timeout_seconds,
            max_tokens=None,
            json_mode=True,
            provider=args.provider,
        )
    )


def concept_page_mode(args: PipelineRunCommand) -> str:
    if args.concept_page_mode != "auto":
        return args.concept_page_mode
    return "skeleton"


def source_page_mode(args: PipelineRunCommand) -> str:
    if getattr(args, "source_page_mode", "auto") != "auto":
        return args.source_page_mode
    return "section-polish" if args.mode in {"api", "generic-chat"} else "skeleton"


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        pass
    if hasattr(value, "__dataclass_fields__"):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return str(value)


def _run_wiki_generation_loop(
    *,
    api_client: ChatCompletionsJsonClient,
    semantic_system_prompt: str,
    wiki_evaluator_system_prompt: str,
    packets: list[Any],
    raw_dir: Path | None,
    log: PipelineLog,
    normalizer: SemanticNormalizer,
    document: Any,
    blocks: list[Any],
    out: Path,
    save_debug_json: bool,
    wiki_evaluation_loop: bool,
    max_eval_attempts: int,
    source_context: dict[str, Any] | None = None,
    wiki_patch_system_prompt: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    return LangGraphWikiGenerationEvaluator(
        semantic_generation=SemanticGenerationAdapter(
            api_client,
            packets,
            raw_dir,
            log,
            blocks,
            wiki_patch_system_prompt,
        ),
        normalizer=normalizer,
        evaluator=GenerationEvaluatorAdapter(api_client, wiki_evaluator_system_prompt, document, blocks),
        repairer=EvaluationGuardRepairer(),
        events=log,
        evaluation_artifacts=EvaluationArtifactAdapter(out, save_debug_json),
        source_block_ids=[block.block_id for block in blocks],
        packet_block_ids={packet.chunk_id: packet.block_ids for packet in packets},
    ).run(
        semantic_system_prompt=semantic_system_prompt,
        source_context=source_context,
        evaluation_enabled=wiki_evaluation_loop,
        max_attempts=max_eval_attempts,
    )


def _prepare_source_page_polish(
    args: PipelineRunCommand,
    normalized: dict[str, Any],
    blocks: list[Any],
    section_polisher: ApiSectionPolisher | None,
    raw_polish_dir: Path | None,
    invalid_polish_dir: Path,
    log: PipelineLog,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    source_polish: dict[str, Any] = {}
    raw_source_key_points_for_concepts: list[dict[str, Any]] = [
        kp
        for note in normalized.get("semantic_notes", [])
        for kp in note.get("key_points", [])
    ]
    source_key_points_for_concepts = list(raw_source_key_points_for_concepts)
    sp_mode = source_page_mode(args)
    if sp_mode != "section-polish":
        return source_polish, source_key_points_for_concepts, sp_mode

    assert section_polisher is not None
    source_payload = {
        "page_type": "source",
        "section": "source_summary_and_key_points",
        "context": {
            "document": normalized["document"],
            "concept_slugs": [concept["slug"] for concept in normalized["concept_ledger"]],
            "existing_source_summary": normalized.get("existing_source_context", {}).get("summary", ""),
            "existing_source_markdown": normalized.get("existing_source_context", {}).get("source_markdown", ""),
            "summary_instruction": "기존 source page 문맥과 새 SOURCE BLOCKS를 함께 반영해 전체 source page 요약을 새로 작성한다. 기존 요약 뒤에 새 요약을 붙이는 append 형식으로 쓰지 않는다.",
        },
        "draft": {
            "new_summary_candidates": [n.get("semantic_summary", "") for n in normalized["semantic_notes"] if n.get("semantic_summary")],
            "key_points": [kp for note in normalized["semantic_notes"] for kp in note.get("key_points", [])],
        },
        "evidence": normalized["evidence_units"],
    }
    try:
        raw_source_polish = section_polisher.polish(source_payload, blocks)
    except SectionPolishParseError as exc:
        invalid_path = invalid_polish_dir / "source_page.txt"
        if args.save_debug_json:
            ensure_dir(invalid_polish_dir)
            write_text(invalid_path, exc.raw_content)
        normalized.setdefault("warnings", []).append("source_page: section polish output was not repairable; used backend skeleton")
        log.emit(
            "5-보조. Source Section Polish",
            "Source page section polish가 복구 불가능해 backend skeleton으로 대체했습니다.",
            {"invalid_raw": invalid_path if args.save_debug_json else "not_saved"},
        )
        return source_polish, source_key_points_for_concepts, sp_mode

    if raw_polish_dir is not None:
        write_json(raw_polish_dir / "source_page.json", raw_source_polish)
    mapped_source_polish = _map_polish_output(raw_source_polish, blocks, normalized.setdefault("warnings", []), "source_page")
    source_polish = {
        "title": mapped_source_polish.get("title"),
        "summary": mapped_source_polish,
        "key_points": mapped_source_polish,
    }
    source_key_points_for_concepts = [
        *mapped_source_polish.get("items", []),
        *raw_source_key_points_for_concepts,
    ]
    log.emit(
        "5-보조. Source Section Polish",
        "Source page의 summary/key points 섹션만 LLM으로 다듬었습니다.",
        {"confidence": mapped_source_polish.get("confidence"), "항목 수": len(mapped_source_polish.get("items", []))},
    )
    return source_polish, source_key_points_for_concepts, sp_mode


def _prepare_concept_section_polish(
    args: PipelineRunCommand,
    normalized: dict[str, Any],
    concept_source_blocks_by_slug: dict[str, list[Any]],
    source_key_points_for_concepts: list[dict[str, Any]],
    section_polisher: ApiSectionPolisher,
    raw_polish_dir: Path | None,
    invalid_polish_dir: Path,
    log: PipelineLog,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    concept_polish_by_slug: dict[str, Any] = {}
    generated_concept_pages: list[dict[str, Any]] = []

    for concept in normalized["concept_ledger"]:
        source_blocks = concept_source_blocks_by_slug.get(concept["slug"], [])
        related_evidence = [ev for ev in normalized["evidence_units"] if concept["slug"] in ev.get("related_concept_slugs", [])]
        resolution_links = [
            target
            for resolution in normalized.get("concept_resolutions", [])
            if (resolution.get("canonical_slug") or resolution.get("incoming_slug")) == concept["slug"]
            for target in resolution.get("link_targets", [])
        ]
        payload = {
            "page_type": "concept",
            "section": "concept_definition_key_points_and_related",
            "context": {
                "title": concept.get("title"),
                "slug": concept.get("slug"),
                "aliases": concept.get("aliases", []),
                "why_page_worthy": concept.get("why_page_worthy"),
                "resolution_link_targets": resolution_links,
            },
            "draft": {
                "definition": concept.get("definition"),
            },
            "evidence": related_evidence,
        }
        try:
            raw_polish = section_polisher.polish(payload, source_blocks)
        except SectionPolishParseError as exc:
            invalid_path = invalid_polish_dir / f"concept_{concept['slug']}.txt"
            if args.save_debug_json:
                ensure_dir(invalid_polish_dir)
                write_text(invalid_path, exc.raw_content)
            normalized.setdefault("warnings", []).append(f"concept:{concept['slug']}: section polish output was not repairable; used backend skeleton")
            log.emit(
                "6-보조. Concept Section Polish",
                "Concept section polish가 복구 불가능해 해당 concept은 backend skeleton으로 대체했습니다.",
                {"개념": concept["slug"], "invalid_raw": invalid_path if args.save_debug_json else "not_saved"},
            )
            continue

        if raw_polish_dir is not None:
            write_json(raw_polish_dir / f"concept_{concept['slug']}.json", raw_polish)
        mapped = _map_polish_output(raw_polish, source_blocks, normalized.setdefault("warnings", []), f"concept:{concept['slug']}")
        concept_polish_by_slug[concept["slug"]] = {
            "definition": mapped,
            "key_points": mapped,
            "related_concept_hints": mapped.get("related_concept_hints", []),
        }
        generated_concept_pages.append(
            {
                "slug": concept["slug"],
                "title": concept.get("title"),
                "confidence": mapped.get("confidence"),
                "related_concept_hints": mapped.get("related_concept_hints", []),
            }
        )
        log.emit(
            "6-보조. Concept Section Polish",
            "Concept page의 definition/key points/related hint 섹션만 LLM으로 다듬었습니다.",
            {"개념": concept["slug"], "근거 블록 수": len(source_blocks), "confidence": mapped.get("confidence")},
        )

    concept_pages = ConceptPageAssembler().build_top(
        normalized,
        top_n=None,
        polish_by_slug=concept_polish_by_slug,
        source_key_points=source_key_points_for_concepts,
    )
    log.emit("6. Concept Page 생성", "백엔드 조립과 섹션 polish로 concept page markdown 데이터를 생성했습니다.", {"페이지 수": len(concept_pages)})
    return concept_pages, generated_concept_pages


def _load_pipeline_prompts(args: PipelineRunCommand, log: PipelineLog) -> PipelinePrompts:
    wiki_patch_path = getattr(
        args,
        "wiki_patch_system_prompt",
        "prompts/wiki_generation_patch.system.md",
    )
    prompts = PipelinePrompts(
        semantic=read_prompt(args.system_prompt),
        concept=read_prompt(args.concept_system_prompt),
        concept_resolution=read_prompt(args.concept_resolution_system_prompt),
        section_polish=read_prompt(args.section_polish_system_prompt),
        wiki_evaluator=read_prompt(args.wiki_evaluator_system_prompt),
        wiki_patch=read_prompt(wiki_patch_path),
    )
    log.emit(
        "프롬프트 로드",
        "시스템 프롬프트를 메모리에 로드했습니다.",
        {
            "semantic": args.system_prompt,
            "concept": args.concept_system_prompt,
            "concept_resolution": args.concept_resolution_system_prompt,
            "section_polish": args.section_polish_system_prompt,
            "wiki_evaluator": args.wiki_evaluator_system_prompt,
            "wiki_patch": wiki_patch_path,
        },
    )
    return prompts


def _prepare_api_client(
    args: PipelineRunCommand,
    out: Path,
    log: PipelineLog,
) -> ChatCompletionsJsonClient | None:
    requires_api = (
        args.mode in {"api", "generic-chat"}
        or concept_page_mode(args) in {"api", "full-llm", "section-polish"}
        or source_page_mode(args) == "section-polish"
    )
    if not requires_api:
        return None

    api_client = load_api_client(args)
    if args.save_debug_json:
        write_json(
            out / "api_config.json",
            {
                "provider": args.provider,
                "model": args.model,
                "timeout_seconds": args.timeout_seconds,
                "secret_values_saved": False,
            },
        )
    log.emit(
        "API 설정",
        "LLM API 클라이언트를 준비했습니다.",
        {
            "provider": args.provider,
            "model": args.model,
        },
    )
    return api_client


def _extract_pipeline_source(
    args: PipelineRunCommand,
    *,
    input_text: str | None,
    input_source_name: str,
    input_path: Path,
    out: Path,
    log: PipelineLog,
) -> tuple[SourceDocument, list[SourceBlock], list[dict[str, str]]]:
    extractor = MarkdownBlockExtractor()
    if getattr(args, "input_blocks", None):
        # 채팅 경로: 문답 경계와 provenance를 backend가 확정해 보내므로 Markdown을 다시 쪼개지 않는다.
        document, blocks = extractor.blocks_from_records(
            args.input_blocks,
            text=input_text or "",
            source_path=input_source_name,
            fallback_title=Path(input_source_name).stem,
        )
    elif input_text is not None:
        document, blocks = extractor.extract_text(
            input_text,
            source_path=input_source_name,
            fallback_title=Path(input_source_name).stem,
        )
    else:
        document, blocks = extractor.extract(input_path)

    source_document_id = getattr(args, "source_document_id", None)
    if source_document_id:
        document.document_id = source_document_id
        for block in blocks:
            block.document_id = source_document_id
    if args.save_debug_json:
        write_json(out / "document.json", asdict(document))
        write_json(out / "block_map.json", {block.block_id: block.source_reference_id for block in blocks})

    source_block_records = [
        {
            "document_id": block.document_id,
            "block_id": block.block_id,
            "text": block.text,
        }
        for block in blocks
    ]
    log.emit(
        "1. 블록 추출",
        "Markdown 원문을 블록 객체로 변환했고, 이 블록 목록을 다음 단계 입력으로 전달합니다.",
        {
            "문서 ID": document.document_id,
            "문서 제목": document.title,
            "블록 수": len(blocks),
        },
    )
    return document, blocks, source_block_records


def _empty_normalized(document: SourceDocument) -> dict[str, Any]:
    return {
        "document": asdict(document),
        "semantic_notes": [],
        "concept_ledger": [],
        "categories": [],
        "section_candidates": [],
        "mentions": [],
        "observations": [],
        "evidence_units": [],
        "missing_related_concept_hints": [],
        "warnings": [],
    }


def _source_block_records(blocks: list[SourceBlock]) -> list[dict[str, str]]:
    return [
        {
            "document_id": block.document_id,
            "block_id": block.block_id,
            "text": block.text,
        }
        for block in blocks
    ]


def _resolve_pipeline_concepts(
    args: PipelineRunCommand,
    *,
    api_client: ChatCompletionsJsonClient,
    concept_resolution_prompt: str,
    normalized: dict[str, Any],
    out: Path,
    log: PipelineLog,
) -> dict[str, Any]:
    existing_concepts = getattr(args, "existing_concept_index", None)
    if existing_concepts is None:
        existing_concepts = load_existing_concept_index(
            getattr(args, "existing_wiki_dir", None)
        )
    assembler = MeaningClusterArtifactAssembler()
    concept_update_candidates = assembler.candidate_claims(normalized)
    concepts = [*normalized.get("concept_ledger", []), *existing_concepts]
    missing_related_hints = normalized.get("missing_related_concept_hints", [])
    resolver = ApiConceptResolver(
        api_client,
        concept_resolution_prompt,
    )

    def resolve_concepts() -> tuple[dict[str, Any], float]:
        started = monotonic()
        result = resolver.resolve(
            normalized["concept_ledger"],
            existing_concepts,
            missing_related_hints,
        )
        return result, monotonic() - started

    def judge_updates() -> tuple[list[dict[str, Any]], float]:
        started = monotonic()
        result = judge_concept_update_candidates(
            completion=api_client,
            concepts=concepts,
            candidates=concept_update_candidates,
        )
        return result, monotonic() - started

    parallel_started = monotonic()
    with ThreadPoolExecutor(max_workers=2) as executor:
        resolution_future = executor.submit(resolve_concepts)
        update_future = executor.submit(judge_updates)
        raw_resolution, resolution_seconds = resolution_future.result()
        concept_update_decisions, update_seconds = update_future.result()
    parallel_seconds = monotonic() - parallel_started
    if args.save_debug_json:
        write_json(
            ensure_dir(out / "raw_llm_outputs") / "concept_resolution.json",
            raw_resolution,
        )
    resolutions = normalize_resolution_output(
        raw_resolution,
        normalized["concept_ledger"],
        existing_concepts,
        normalized.setdefault("warnings", []),
    )
    hint_resolutions = normalize_hint_resolution_output(
        raw_resolution,
        missing_related_hints,
        normalized["concept_ledger"],
        existing_concepts,
        normalized.setdefault("warnings", []),
    )
    resolved = apply_concept_resolutions(
        normalized,
        resolutions,
        existing_concepts,
        hint_resolutions,
    )
    resolved_candidates = assembler.candidate_claims(resolved)
    resolved["concept_update_decisions"] = normalize_concept_update_decisions(
        {"concept_update_decisions": concept_update_decisions},
        concepts=[
            *resolved.get("concept_ledger", []),
            *resolved.get("existing_concept_index", []),
        ],
        candidates=resolved_candidates,
        concept_slug_map=resolved.get("concept_slug_map", {}),
    )
    log.emit(
        "4-보조. Concept Resolution",
        "Concept resolution과 근거 갱신 후보 판단을 병렬 실행하고 canonical slug를 반영했습니다.",
        {
            "기존 개념 수": len(existing_concepts),
            "해결 전 개념 수": len(resolutions),
            "해결 후 개념 수": len(resolved["concept_ledger"]),
            "missing hint 수": len(missing_related_hints),
            "hint 해결 수": sum(
                1
                for item in hint_resolutions
                if item.get("decision") not in {"unresolved", "promote_new_concept"}
            ),
            "병합 수": sum(
                1 for item in resolutions if item.get("decision") == "merge_into"
            ),
            "링크 판단 수": sum(1 for item in resolutions if item.get("link_targets")),
            "same_concept 수": sum(
                1
                for item in resolved["concept_update_decisions"]
                if item.get("decision") == "same_concept"
            ),
            "relation_candidate 수": sum(
                1
                for item in resolved["concept_update_decisions"]
                if item.get("decision") == "relation_candidate"
            ),
            "resolution 소요 시간(초)": f"{resolution_seconds:.2f}",
            "update judge 소요 시간(초)": f"{update_seconds:.2f}",
            "병렬 wall 시간(초)": f"{parallel_seconds:.2f}",
            "병렬 절감 시간(초)": f"{max(0.0, resolution_seconds + update_seconds - parallel_seconds):.2f}",
        },
    )
    return resolved


def _prepare_source_page_assembly(
    args: PipelineRunCommand,
    *,
    api_client: ChatCompletionsJsonClient | None,
    prompts: PipelinePrompts,
    normalized: dict[str, Any],
    blocks: list[SourceBlock],
    existing_source_artifact: dict[str, Any] | None,
    existing_source_markdown: str | None,
    out: Path,
    log: PipelineLog,
) -> _SourcePagePreparation:
    existing_source_artifact_with_markdown = (
        {**existing_source_artifact, "source_markdown": existing_source_markdown}
        if existing_source_artifact
        else None
    )
    source_page_normalized = normalized
    existing_source_context_blocks = source_context_blocks(
        existing_source_artifact_with_markdown
    )
    section_polisher = (
        ApiSectionPolisher(api_client, prompts.section_polish)
        if api_client is not None
        else None
    )
    raw_polish_dir = (
        ensure_dir(out / "raw_llm_outputs" / "section_polish")
        if args.save_debug_json
        else None
    )
    invalid_polish_dir = out / "raw_llm_outputs" / "section_polish_invalid"
    source_polish, source_key_points_for_concepts, source_mode = (
        _prepare_source_page_polish(
            args,
            source_page_normalized,
            [*existing_source_context_blocks, *blocks],
            section_polisher,
            raw_polish_dir,
            invalid_polish_dir,
            log,
        )
    )

    return _SourcePagePreparation(
        normalized=source_page_normalized,
        existing_context_blocks=existing_source_context_blocks,
        polish=source_polish,
        key_points_for_concepts=source_key_points_for_concepts,
        mode=source_mode,
        section_polisher=section_polisher,
        raw_polish_dir=raw_polish_dir,
        invalid_polish_dir=invalid_polish_dir,
    )


def _prepare_concept_source_blocks(
    normalized: dict[str, Any],
    *,
    existing_source_context_blocks: list[SourceBlock],
    blocks: list[SourceBlock],
    source_key_points_for_concepts: list[dict[str, Any]],
    log: PipelineLog,
) -> dict[str, list[SourceBlock]]:
    concept_source_blocks_by_slug: dict[str, list[SourceBlock]] = {}
    concept_input_blocks = [
        *existing_source_context_blocks,
        *blocks,
    ]
    for concept in normalized["concept_ledger"]:
        concept_blocks = collect_concept_source_blocks(
            concept,
            normalized["evidence_units"],
            concept_input_blocks,
            source_key_points=source_key_points_for_concepts,
        )
        concept_source_blocks_by_slug[concept["slug"]] = concept_blocks
    log.emit(
        "5-보조. Concept 입력 준비",
        "전체 개념별 source block을 메모리에 모았습니다.",
        {"대상 개념 수": len(concept_source_blocks_by_slug)},
    )
    return concept_source_blocks_by_slug


def _assemble_source_page(
    preparation: _SourcePagePreparation,
    *,
    log: PipelineLog,
) -> dict[str, Any]:
    source_page = SourcePageAssembler().build(
        preparation.normalized,
        polish=preparation.polish,
    )
    log.emit(
        "5. Source Page 생성",
        "백엔드 조립 방식으로 source page markdown 데이터를 생성했습니다.",
        {
            "source_page": source_page.get("markdown_path"),
            "source_json": bool(
                preparation.normalized.get("source_extraction_artifact")
            ),
            "mode": preparation.mode,
        },
    )
    return source_page


def _assemble_concept_pages(
    args: PipelineRunCommand,
    *,
    api_client: ChatCompletionsJsonClient | None,
    prompts: PipelinePrompts,
    normalized: dict[str, Any],
    concept_source_blocks_by_slug: dict[str, list[SourceBlock]],
    source_key_points_for_concepts: list[dict[str, Any]],
    section_polisher: ApiSectionPolisher | None,
    raw_polish_dir: Path | None,
    invalid_polish_dir: Path,
    out: Path,
    log: PipelineLog,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    generated_concept_pages = []
    raw_concept_dir = (
        ensure_dir(out / "raw_llm_outputs" / "concept_page_generation")
        if args.save_debug_json
        else None
    )
    concept_mode = concept_page_mode(args)
    if concept_mode == "section-polish":
        assert section_polisher is not None
        concept_pages, generated_concept_pages = _prepare_concept_section_polish(
            args,
            normalized,
            concept_source_blocks_by_slug,
            source_key_points_for_concepts,
            section_polisher,
            raw_polish_dir,
            invalid_polish_dir,
            log,
        )
    elif concept_mode in {"api", "full-llm"}:
        assert api_client is not None
        concept_generator = ApiConceptPageGenerator(api_client, prompts.concept)
        generator_assembler = GeneratedConceptPageAssembler()
        for concept in normalized["concept_ledger"]:
            concept_blocks = concept_source_blocks_by_slug.get(concept["slug"], [])
            raw_page = concept_generator.generate(
                concept,
                normalized["evidence_units"],
                concept_blocks,
            )
            if raw_concept_dir is not None:
                write_json(raw_concept_dir / f"{concept['slug']}.json", raw_page)
            generated_page = generator_assembler.normalize_generated_output(
                concept,
                raw_page,
                concept_blocks,
                normalized.setdefault("warnings", []),
            )
            generated_concept_pages.append(generated_page)
            log.emit(
                "6. Concept Page LLM 생성",
                "LLM concept page 출력을 backend 형식으로 정규화했습니다.",
                {
                    "개념": concept["slug"],
                    "근거 블록 수": len(concept_blocks),
                    "confidence": generated_page.get("confidence"),
                },
            )
        concept_pages = generator_assembler.build_pages(generated_concept_pages)
    else:
        concept_pages = ConceptPageAssembler().build_top(
            normalized,
            top_n=None,
            source_key_points=source_key_points_for_concepts,
        )
        log.emit(
            "6. Concept Page 생성",
            "Backend skeleton 방식으로 concept page markdown 데이터를 생성했습니다.",
            {"페이지 수": len(concept_pages)},
        )
    return concept_pages, generated_concept_pages, concept_mode


def _assemble_wiki_pages(
    args: PipelineRunCommand,
    *,
    api_client: ChatCompletionsJsonClient | None,
    prompts: PipelinePrompts,
    normalized: dict[str, Any],
    blocks: list[SourceBlock],
    existing_source_artifact: dict[str, Any] | None,
    existing_source_markdown: str | None,
    out: Path,
    log: PipelineLog,
    source_preparation: _SourcePagePreparation | None = None,
) -> WikiPageOutputs:
    source_preparation = source_preparation or _prepare_source_page_assembly(
        args,
        api_client=api_client,
        prompts=prompts,
        normalized=normalized,
        blocks=blocks,
        existing_source_artifact=existing_source_artifact,
        existing_source_markdown=existing_source_markdown,
        out=out,
        log=log,
    )
    concept_source_blocks_by_slug = _prepare_concept_source_blocks(
        normalized,
        existing_source_context_blocks=source_preparation.existing_context_blocks,
        blocks=blocks,
        source_key_points_for_concepts=source_preparation.key_points_for_concepts,
        log=log,
    )
    source_page = _assemble_source_page(source_preparation, log=log)
    concept_pages, generated_concept_pages, concept_mode = _assemble_concept_pages(
        args,
        api_client=api_client,
        prompts=prompts,
        normalized=normalized,
        concept_source_blocks_by_slug=concept_source_blocks_by_slug,
        source_key_points_for_concepts=source_preparation.key_points_for_concepts,
        section_polisher=source_preparation.section_polisher,
        raw_polish_dir=source_preparation.raw_polish_dir,
        invalid_polish_dir=source_preparation.invalid_polish_dir,
        out=out,
        log=log,
    )

    links = LinkBuilder().build(
        normalized,
        generated_concept_pages=generated_concept_pages,
    )
    log.emit(
        "7. 링크 생성",
        "위키 링크 데이터를 생성했습니다.",
        {"링크 수": len(links)},
    )
    return WikiPageOutputs(
        source_page=source_page,
        source_page_normalized=source_preparation.normalized,
        concept_pages=concept_pages,
        links=links,
        source_page_mode=source_preparation.mode,
        concept_page_mode=concept_mode,
    )


def _prepare_post_ingest_clusters(
    *,
    normalized: dict[str, Any],
    log: PipelineLog,
) -> tuple[dict[str, Any], dict[str, Any]]:
    assembler = MeaningClusterArtifactAssembler()
    candidates = assembler.candidate_claims(normalized)
    decisions = normalized.get("concept_update_decisions", [])
    candidates_by_id = {
        candidate["candidate_id"]: candidate for candidate in candidates
    }
    concept_updates = [
        {
            **item,
            "claim_id": candidates_by_id.get(item["candidate_id"], {}).get("claim_id"),
            "claim": candidates_by_id.get(item["candidate_id"], {}).get("claim"),
            "refs": candidates_by_id.get(item["candidate_id"], {}).get("refs", []),
            "candidate_type": candidates_by_id.get(item["candidate_id"], {}).get(
                "candidate_type"
            ),
        }
        for item in decisions
        if item.get("decision") == "same_concept"
    ]
    core_relations = [
        item for item in decisions if item.get("decision") == "relation_candidate"
    ]
    log.emit(
        "8. 후속 Meaning Cluster 준비",
        "Concept 근거 갱신은 현재 ingest에 유지하고 Meaning Cluster 판단 입력을 준비했습니다.",
        {
            "candidate 수": len(candidates),
            "same_concept 수": len(concept_updates),
            "후속 cluster 대상": len(candidates) - len(concept_updates),
        },
    )
    cluster_normalized = {
        key: normalized.get(key, [] if key != "document" else {})
        for key in (
            "document",
            "section_candidates",
            "mentions",
            "unresolved_related_concept_hints",
            "evidence_units",
        )
    }
    return (
        {"concept_update_decisions": concept_updates},
        {
            "cluster_normalized": cluster_normalized,
            "concept_update_decisions": concept_updates,
            "core_relation_decisions": core_relations,
        },
    )


def run_pipeline(
    command: PipelineRunCommand,
    progress_callback: Callable[[], bool | None] | None = None,
    finalization_callback: Callable[
        [Callable[[], dict[str, Any]]], dict[str, Any]
    ]
    | None = None,
) -> dict:
    pipeline_started = monotonic()
    args = resolve_api_defaults(command)
    input_text = getattr(args, "input_markdown", None)
    input_source_name = getattr(args, "input_name", None) or getattr(args, "input", None) or "inline.md"
    input_path = Path(args.input) if getattr(args, "input", None) else Path(input_source_name)
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    ensure_dir(out)
    log = PipelineLog(
        pipeline_log_uri(args.run_id)
        if args.run_id
        else (getattr(args, "log_path", None) or out / "pipeline.log"),
        callback_url=getattr(args, "log_callback_url", None),
        run_id=getattr(args, "run_id", None),
        progress_callback=progress_callback,
    )
    log.emit(
        "시작",
        "파이프라인 실행을 시작했습니다.",
        {
            "입력": input_source_name if input_text is not None else input_path,
            "출력 폴더": out,
            "실행 모드": args.mode,
            "선택 모드": getattr(args, "selection_mode", None),
            "Source Page 모드": source_page_mode(args),
            "Concept Page 모드": concept_page_mode(args),
        },
    )

    prompts = _load_pipeline_prompts(args, log)
    api_client = _prepare_api_client(args, out, log)
    document, blocks, source_block_records = _extract_pipeline_source(
        args,
        input_text=input_text,
        input_source_name=input_source_name,
        input_path=input_path,
        out=out,
        log=log,
    )
    source_changes: SourceBlockChanges | None = None
    extraction_blocks = blocks
    if args.reingest:
        source_changes = compare_source_blocks(
            args.existing_source_blocks,
            blocks,
            previous_max_block_number=int(
                (args.existing_source_artifact or {}).get(
                    "max_block_number",
                    0,
                )
                or 0
            ),
        )
        blocks = source_changes.blocks
        extraction_blocks = source_changes.ingest_blocks
        source_block_records = _source_block_records(blocks)
        log.emit(
            "1-보조. 재편입 블록 비교",
            "마지막 성공 블록과 최신 Markdown을 비교해 재추출 대상을 확정했습니다.",
            {
                "유지": len(source_changes.unchanged_block_ids),
                "이동": len(source_changes.moved_block_ids),
                "추가": len(source_changes.added_block_ids),
                "수정": len(source_changes.modified_block_ids),
                "삭제": len(source_changes.deleted_block_ids),
            },
        )

    # 2. Build LLM packets with short [B0001] anchors only.
    packet_builder = SemanticPacketBuilder(args.max_packet_chars, args.overlap_blocks)
    packets = packet_builder.build(document.document_id, extraction_blocks)
    if args.save_debug_json:
        packet_dir = ensure_dir(out / "packets")
        for p in packets:
            write_text(packet_dir / f"{p.chunk_id}.md", p.text)
    log.emit(
        "2. 패킷 생성",
        "블록 목록을 LLM 입력 패킷으로 나누었고, 패킷 객체 목록을 의미 추출 단계로 전달합니다.",
        {"패킷 수": len(packets), "최대 글자 수": args.max_packet_chars, "겹침 블록 수": args.overlap_blocks},
    )

    # 3. LLM semantic extraction: summary/key points/concepts/evidence with anchor_block_ids only.
    assert api_client is not None
    existing_source_artifact = (
        getattr(args, "existing_source_artifact", None) if args.reingest else None
    )
    if source_changes is not None:
        existing_source_artifact = active_source_artifact(
            existing_source_artifact,
            source_changes.unchanged_block_ids,
            current_has_blocks=bool(source_changes.blocks),
        )
    existing_source_markdown = (
        getattr(args, "existing_source_markdown", None) if args.reingest else None
    )
    raw_dir = ensure_dir(out / "raw_llm_outputs" / "semantic_extraction") if args.save_debug_json else None
    normalizer = SemanticNormalizer(document, extraction_blocks)
    max_eval_attempts = max(1, int(getattr(args, "max_eval_attempts", 2) or 2))
    if extraction_blocks:
        notes, normalized, generation_evaluations = _run_wiki_generation_loop(
            api_client=api_client,
            semantic_system_prompt=prompts.semantic,
            wiki_evaluator_system_prompt=prompts.wiki_evaluator,
            packets=packets,
            raw_dir=raw_dir,
            log=log,
            normalizer=normalizer,
            document=document,
            blocks=extraction_blocks,
            out=out,
            save_debug_json=args.save_debug_json,
            wiki_evaluation_loop=getattr(args, "wiki_evaluation_loop", True),
            max_eval_attempts=max_eval_attempts,
            source_context=None,
            wiki_patch_system_prompt=prompts.wiki_patch,
        )
    else:
        notes = []
        normalized = _empty_normalized(document)
        generation_evaluations = []
        log.emit(
            "3. 의미 추출 생략",
            "추가되거나 수정된 블록이 없어 LLM 의미 추출을 생략했습니다.",
            {},
        )

    # 4. Backend normalize/merge/mention expansion.
    log.emit(
        "4. 정규화",
        "의미 노트를 concept ledger와 evidence unit으로 정규화했습니다.",
        {
            "core concept 수": len(normalized["concept_ledger"]),
            "section candidate 수": len(normalized.get("section_candidates", [])),
            "mention 수": len(normalized.get("mentions", [])),
            "category 수": len(normalized.get("categories", [])),
            "근거 수": len(normalized["evidence_units"]),
            "경고 수": len(normalized.get("warnings", [])),
        },
    )

    # 4a. Resolve concepts while polishing the independent source page sections.
    assert api_client is not None
    source_preparation: _SourcePagePreparation | None = None
    if extraction_blocks:
        if source_page_mode(args) == "section-polish":
            source_normalized = copy.deepcopy(normalized)
            if args.reingest:
                source_normalized = source_page_context_normalized(
                    source_normalized,
                    existing_source_artifact,
                )

            def resolve_concepts_for_pages() -> tuple[dict[str, Any], float]:
                started = monotonic()
                result = _resolve_pipeline_concepts(
                    args,
                    api_client=api_client,
                    concept_resolution_prompt=prompts.concept_resolution,
                    normalized=normalized,
                    out=out,
                    log=log,
                )
                return result, monotonic() - started

            def prepare_source_for_pages() -> tuple[_SourcePagePreparation, float]:
                started = monotonic()
                result = _prepare_source_page_assembly(
                    args,
                    api_client=api_client,
                    prompts=prompts,
                    normalized=source_normalized,
                    blocks=blocks,
                    existing_source_artifact=existing_source_artifact,
                    existing_source_markdown=existing_source_markdown,
                    out=out,
                    log=log,
                )
                return result, monotonic() - started

            parallel_started = monotonic()
            with ThreadPoolExecutor(max_workers=2) as executor:
                resolution_future = executor.submit(resolve_concepts_for_pages)
                source_future = executor.submit(prepare_source_for_pages)
                normalized, resolution_seconds = resolution_future.result()
                source_preparation, source_seconds = source_future.result()
            parallel_seconds = monotonic() - parallel_started
            for warning in source_preparation.normalized.get("warnings", []):
                if warning not in normalized.setdefault("warnings", []):
                    normalized["warnings"].append(warning)
            log.emit(
                "4-보조. Concept/Source 병렬 준비",
                "Concept 판단과 source section polish를 병렬 실행했습니다.",
                {
                    "concept 판단 소요 시간(초)": f"{resolution_seconds:.2f}",
                    "source polish 소요 시간(초)": f"{source_seconds:.2f}",
                    "병렬 wall 시간(초)": f"{parallel_seconds:.2f}",
                    "병렬 절감 시간(초)": f"{max(0.0, resolution_seconds + source_seconds - parallel_seconds):.2f}",
                },
            )
        else:
            normalized = _resolve_pipeline_concepts(
                args,
                api_client=api_client,
                concept_resolution_prompt=prompts.concept_resolution,
                normalized=normalized,
                out=out,
                log=log,
            )
    contribution_normalized = normalized
    if args.reingest:
        normalized = source_page_context_normalized(
            normalized,
            existing_source_artifact,
        )
    if source_preparation is not None:
        source_preparation = replace(source_preparation, normalized=normalized)
    if args.save_debug_json:
        write_json(out / "normalized.json", normalized)
        if generation_evaluations:
            write_json(out / "wiki_generation_evaluations.json", generation_evaluations)

    page_outputs = _assemble_wiki_pages(
        args,
        api_client=api_client,
        prompts=prompts,
        normalized=normalized,
        blocks=blocks,
        existing_source_artifact=existing_source_artifact,
        existing_source_markdown=existing_source_markdown,
        out=out,
        log=log,
        source_preparation=source_preparation,
    )

    meaning_cluster_artifact, post_ingest = _prepare_post_ingest_clusters(
        normalized=contribution_normalized,
        log=log,
    )
    generation_status = generation_evaluation_status(generation_evaluations)
    post_ingest.update(
        {
            "quality_required": bool(extraction_blocks),
            "generation_evaluation_status": generation_status,
        }
    )

    def build_manifest() -> dict[str, Any]:
        source_extraction_artifact = _json_safe(
            page_outputs.source_page_normalized.get("source_extraction_artifact")
        )
        if source_changes is not None and isinstance(
            source_extraction_artifact,
            dict,
        ):
            source_extraction_artifact = {
                **source_extraction_artifact,
                "max_block_number": source_changes.max_block_number,
            }
        manifest = {
            "input": input_source_name if input_text is not None else str(input_path),
            "out": str(out),
            "mode": args.mode,
            "operation_id": args.operation_id,
            "selection_mode": getattr(args, "selection_mode", None),
            "user_id": args.user_id,
            "workspace_id": args.workspace_id,
            "source_page_mode": page_outputs.source_page_mode,
            "concept_page_mode": page_outputs.concept_page_mode,
            "document_id": document.document_id,
            "source_document_id": getattr(args, "source_document_id", None),
            "source_page": page_outputs.source_page,
            "source_extraction_artifact": source_extraction_artifact,
            "source_blocks": source_block_records,
            "source_block_changes": (
                source_changes.to_manifest()
                if source_changes is not None
                else None
            ),
            "concept_pages": page_outputs.concept_pages,
            "concept_contributions": build_concept_contributions(
                operation_id=args.operation_id,
                normalized=contribution_normalized,
                source_blocks=source_block_records,
                links=page_outputs.links,
                concept_update_decisions=meaning_cluster_artifact.get(
                    "concept_update_decisions",
                    [],
                ),
            )
            if args.operation_id
            else {},
            "links": page_outputs.links,
            "meaning_clusters": meaning_cluster_artifact,
            "maintenance_summary": {"status": "pending"},
            "post_ingest": post_ingest,
            "normalized": normalized,
            "generation_evaluations": generation_evaluations,
            "generation_evaluation_status": generation_status,
            "pipeline_log": str(log.path),
            "log_callback_url": getattr(args, "log_callback_url", None),
            "save_debug_json": args.save_debug_json,
            "warnings": normalized.get("warnings", []),
        }
        log.emit(
            "완료",
            "파이프라인 실행이 완료되었습니다.",
            {
                "문서 ID": document.document_id,
                "core concept 수": len(normalized["concept_ledger"]),
                "section candidate 수": len(normalized.get("section_candidates", [])),
                "mention 수": len(normalized.get("mentions", [])),
                "category 수": len(normalized.get("categories", [])),
                "근거 수": len(normalized["evidence_units"]),
                "총 소요 시간(초)": f"{monotonic() - pipeline_started:.2f}",
            },
        )
        return _json_safe(manifest)

    return (
        finalization_callback(build_manifest)
        if finalization_callback is not None
        else build_manifest()
    )


def pipeline_command_from_cli_args(args: argparse.Namespace) -> PipelineRunCommand:
    values = vars(args).copy()
    input_value = values.pop("input")
    return PipelineRunCommand(
        run_id=None,
        input=input_value,
        input_name=input_value,
        **values,
    )


def main() -> None:
    command = pipeline_command_from_cli_args(parse_args())
    try:
        manifest = run_pipeline(command)
    except PipelineConfigurationError as exc:
        # CLI에서는 기존처럼 traceback 없이 메시지만 남기고 종료한다.
        raise SystemExit(str(exc)) from exc
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
