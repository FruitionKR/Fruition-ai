import os
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.modules.wiki_generation.domain.entities import SourceBlock, SourceDocument
from app.modules.wiki_ingestion.application.models import PipelineRunCommand
from run_lab import (
    PipelineConfigurationError,
    _SourcePagePreparation,
    _prepare_api_client,
    load_api_client,
    parse_args,
    pipeline_command_from_cli_args,
    read_prompt,
    resolve_api_defaults,
    run_pipeline,
)


def _args(*extra: str):
    with patch(
        "sys.argv",
        [
            "run_lab.py",
            "--input",
            "input.md",
            "--provider",
            "openai",
            "--model",
            "gpt-5-nano",
            *extra,
        ],
    ):
        return parse_args()


def test_wiki_evaluation_loop_is_enabled_by_default() -> None:
    assert _args().wiki_evaluation_loop is True


def test_wiki_evaluation_loop_can_be_disabled() -> None:
    assert _args("--no-wiki-evaluation-loop").wiki_evaluation_loop is False


def test_cli_args_are_converted_to_typed_pipeline_command() -> None:
    args = _args("--out", "runs/cli")
    command = pipeline_command_from_cli_args(args)

    assert command.run_id is None
    assert command.input == "input.md"
    assert command.input_name == "input.md"
    assert command.out == "runs/cli"
    assert command.provider == "openai"
    assert command.model == "gpt-5-nano"


def test_api_defaults_validate_and_preserve_request_snapshot() -> None:
    command = PipelineRunCommand(
        run_id=None,
        input="input.md",
        input_name="input.md",
        out="runs/cli",
        user_id="local-user",
        workspace_id="local-workspace",
        provider="gemini",
        model="gemini-3.1-flash-lite",
    )

    resolved = resolve_api_defaults(command)

    assert resolved.provider == "gemini"
    assert resolved.model == "gemini-3.1-flash-lite"
    assert command.provider == "gemini"
    assert command.model == "gemini-3.1-flash-lite"


def test_api_debug_and_log_metadata_exclude_provider_endpoint(tmp_path: Path) -> None:
    command = PipelineRunCommand(
        run_id=None,
        input="input.md",
        input_name="input.md",
        out=str(tmp_path),
        user_id="local-user",
        workspace_id="local-workspace",
        mode="api",
        provider="openai",
        model="gpt-5-nano",
        save_debug_json=True,
    )
    log = MagicMock()

    with (
        patch("run_lab.load_api_client", return_value=object()),
        patch("run_lab.write_json") as write_json,
    ):
        _prepare_api_client(command, tmp_path, log)

    debug_payload = write_json.call_args.args[1]
    assert debug_payload == {
        "provider": "openai",
        "model": "gpt-5-nano",
        "timeout_seconds": 180,
        "secret_values_saved": False,
    }
    log.emit.assert_called_once_with(
        "API 설정",
        "LLM API 클라이언트를 준비했습니다.",
        {"provider": "openai", "model": "gpt-5-nano"},
    )


def test_run_pipeline_manifest_preserves_log_callback_url(tmp_path: Path) -> None:
    command = PipelineRunCommand(
        run_id="run-1",
        input="input.md",
        input_name="input.md",
        out=str(tmp_path / "run"),
        user_id="local-user",
        workspace_id="local-workspace",
        provider="openai",
        model="gpt-5-nano",
        log_callback_url="https://example.test/logs",
    )
    normalized = {
        "concept_ledger": [],
        "section_candidates": [],
        "mentions": [],
        "categories": [],
        "evidence_units": [],
        "warnings": [],
    }
    page_outputs = SimpleNamespace(
        source_page={},
        source_page_normalized={},
        concept_pages=[],
        links=[],
        source_page_mode="section-polish",
        concept_page_mode="skeleton",
    )

    with (
        patch("run_lab.PipelineLog"),
        patch("run_lab._load_pipeline_prompts", return_value=SimpleNamespace()),
        patch("run_lab._prepare_api_client", return_value=object()),
        patch(
            "run_lab._extract_pipeline_source",
            return_value=(SimpleNamespace(document_id="doc-1"), [], []),
        ),
        patch("run_lab._empty_normalized", return_value=normalized),
        patch("run_lab._assemble_wiki_pages", return_value=page_outputs),
        patch("run_lab._prepare_post_ingest_clusters", return_value=({}, {})),
    ):
        manifest = run_pipeline(command)

    assert manifest["log_callback_url"] == "https://example.test/logs"
    assert "result_callback_url" not in manifest


def test_load_api_client_missing_api_key_raises_configuration_error() -> None:
    # SystemExit은 worker의 except Exception을 통과해 프로세스를 죽이므로
    # 설정 결함은 일반 예외로 던져야 한다.
    command = pipeline_command_from_cli_args(_args())

    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(PipelineConfigurationError):
            load_api_client(command)


def test_read_prompt_missing_file_raises_configuration_error(tmp_path: Path) -> None:
    # read_prompt도 worker 경로에서 호출되므로 SystemExit이 아닌 일반 예외를 던져야 한다.
    with pytest.raises(PipelineConfigurationError):
        read_prompt(str(tmp_path / "no-such-prompt.md"))


def test_run_pipeline_parallelizes_concept_resolution_and_source_polish(
    tmp_path: Path,
) -> None:
    command = PipelineRunCommand(
        run_id="run-parallel",
        input="input.md",
        input_name="input.md",
        out=str(tmp_path / "run"),
        user_id="local-user",
        workspace_id="local-workspace",
        provider="openai",
        model="gpt-5-nano",
        source_page_mode="section-polish",
        concept_page_mode="skeleton",
    )
    document = SourceDocument("doc-1", "문서", "input.md", "hash")
    block = SourceBlock("doc-1", "B0001", "B0001", "본문", 0, 2)
    normalized = {
        "document": {
            "document_id": "doc-1",
            "title": "문서",
            "source_path": "input.md",
        },
        "semantic_notes": [],
        "concept_ledger": [],
        "section_candidates": [],
        "mentions": [],
        "categories": [],
        "evidence_units": [],
        "warnings": [],
    }
    barrier = Barrier(2)

    def resolve(*_args, **_kwargs):
        barrier.wait(timeout=1)
        return {**normalized, "resolved": True, "warnings": []}

    def prepare(*_args, **kwargs):
        barrier.wait(timeout=1)
        source_normalized = kwargs["normalized"]
        source_normalized["warnings"].append("source warning")
        return _SourcePagePreparation(
            normalized=source_normalized,
            existing_context_blocks=[],
            polish={},
            key_points_for_concepts=[],
            mode="section-polish",
            section_polisher=None,
            raw_polish_dir=None,
            invalid_polish_dir=tmp_path / "invalid",
        )

    page_outputs = SimpleNamespace(
        source_page={},
        source_page_normalized={},
        concept_pages=[],
        links=[],
        source_page_mode="section-polish",
        concept_page_mode="skeleton",
    )
    with (
        patch("run_lab.PipelineLog"),
        patch("run_lab._prepare_api_client", return_value=object()),
        patch(
            "run_lab._extract_pipeline_source",
            return_value=(document, [block], [{"document_id": "doc-1", "block_id": "B0001", "text": "본문"}]),
        ),
        patch(
            "run_lab._run_wiki_generation_loop",
            return_value=([], normalized, []),
        ),
        patch("run_lab._resolve_pipeline_concepts", side_effect=resolve),
        patch("run_lab._prepare_source_page_assembly", side_effect=prepare),
        patch("run_lab._assemble_wiki_pages", return_value=page_outputs) as assemble,
        patch("run_lab._prepare_post_ingest_clusters", return_value=({}, {})),
    ):
        manifest = run_pipeline(command)

    preparation = assemble.call_args.kwargs["source_preparation"]
    assert preparation.normalized["resolved"] is True
    assert preparation.normalized["warnings"] == ["source warning"]
    assert manifest["warnings"] == ["source warning"]
