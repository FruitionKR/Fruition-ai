from unittest.mock import Mock, patch

from app.modules.wiki_ingestion.application.models import PipelineRunCommand as _PipelineRunCommand
from app.modules.wiki_ingestion.infrastructure.pipeline_run_adapters import (
    RunLabPipelineRunner,
)


def PipelineRunCommand(**data: object) -> _PipelineRunCommand:
    data.setdefault("provider", "openai")
    data.setdefault("model", "gpt-5-nano")
    return _PipelineRunCommand(**data)


def test_run_lab_pipeline_runner_passes_typed_command_without_conversion() -> None:
    command = PipelineRunCommand(
        run_id="run-1",
        input="input.md",
        input_name="input.md",
        out="runs/run-1",
        user_id="user-1",
        workspace_id="workspace-1",
        selection_mode="partial",
    )

    with patch(
        "app.modules.wiki_ingestion.infrastructure.pipeline_run_adapters.run_pipeline",
        return_value={"manifest": "value"},
    ) as run_pipeline:
        result = RunLabPipelineRunner().run(command)

    run_pipeline.assert_called_once_with(command, progress_callback=None)
    assert result == {"manifest": "value"}


def test_run_lab_pipeline_runner_passes_progress_callback() -> None:
    command = PipelineRunCommand(
        run_id="run-1",
        input="input.md",
        input_name="input.md",
        out="runs/run-1",
        user_id="user-1",
        workspace_id="workspace-1",
    )
    progress_callback = Mock()

    with patch(
        "app.modules.wiki_ingestion.infrastructure.pipeline_run_adapters.run_pipeline",
        return_value={"manifest": "value"},
    ) as run_pipeline:
        RunLabPipelineRunner().run(command, progress_callback=progress_callback)

    run_pipeline.assert_called_once_with(
        command,
        progress_callback=progress_callback,
    )
