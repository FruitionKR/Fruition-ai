"""첨부 문서 수정 run의 미리 보기와 artifact."""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.agent_run.application.start_agent_run import StartAgentRunUseCase
from app.modules.agent_run.domain.edit_preview import build_edit_preview
from app.modules.agent_run.domain.entities import StartAgentRunContent, StartAgentRunRequest
from app.modules.agent_run.infrastructure.postgres_agent_run_repository import _preview_object_key
from app.modules.agent_run.interfaces.http.dependencies import get_agent_run_repository
from app.modules.agent_run.interfaces.http.routes import router

TARGET = {"type": "whole_document", "start_line": 1, "end_line": 3}


class TestEditPreview:
    def test_hunks_keep_three_context_lines_and_count_changes(self) -> None:
        before = "\n".join(f"line {number}" for number in range(1, 21))
        after = before.replace("line 10", "LINE 10").replace("line 11", "LINE 11\nLINE 11-2")

        preview = build_edit_preview(before, after)

        assert (preview["added_lines"], preview["removed_lines"]) == (3, 2)
        assert len(preview["hunks"]) == 1
        hunk = preview["hunks"][0]
        assert (hunk["before_start"], hunk["after_start"]) == (7, 7)
        assert [line["type"] for line in hunk["lines"][:3]] == ["context"] * 3
        assert [line["type"] for line in hunk["lines"][-3:]] == ["context"] * 3
        assert {"type": "delete", "text": "line 10"} in hunk["lines"]
        assert {"type": "insert", "text": "LINE 11-2"} in hunk["lines"]

    def test_far_changes_become_separate_hunks_with_their_own_starts(self) -> None:
        before = "\n".join(f"line {number}" for number in range(1, 41))
        after = before.replace("line 2\n", "line 2\nnew\n").replace("line 35", "LINE 35")

        preview = build_edit_preview(before, after)

        assert [(hunk["before_start"], hunk["after_start"]) for hunk in preview["hunks"]] == [(1, 1), (32, 33)]

    def test_line_numbers_follow_markdown_line_count_for_crlf(self) -> None:
        preview = build_edit_preview("a\r\nb\r\nc", "a\r\nB\r\nc")

        assert preview["hunks"][0]["lines"] == [
            {"type": "context", "text": "a"},
            {"type": "delete", "text": "b"},
            {"type": "insert", "text": "B"},
            {"type": "context", "text": "c"},
        ]

    def test_preview_has_only_the_contract_fields(self) -> None:
        preview = build_edit_preview("a", "b")

        assert set(preview) == {"added_lines", "removed_lines", "hunks"}

    def test_new_document_diff_has_no_synthetic_deleted_empty_line(self) -> None:
        preview = build_edit_preview("", "first\nsecond")

        assert (preview["added_lines"], preview["removed_lines"]) == (2, 0)


class TestStartRun:
    def test_contents_become_artifacts_in_order_with_preview_metadata(self) -> None:
        repository = MagicMock()
        repository.create_with_planning_job.side_effect = lambda run, job_id, artifacts=(): run
        contents = tuple(
            StartAgentRunContent(
                markdown=f"본문 {index}",
                purpose="apply_document_edit",
                document_id=f"doc-{index}",
                base_version=index,
                target=TARGET,
                display_name=f"문서 {index}",
                summary=f"요약 {index}",
                preview_json="{}",
            )
            for index in (1, 2)
        )

        StartAgentRunUseCase(repository).start(
            StartAgentRunRequest(
                workspace_id="workspace-1", user_id="user-1", instruction="고쳐줘",
                action="workspace_workflow", provider="openai", model="gpt-5-nano", contents=contents,
            )
        )

        artifacts = repository.create_with_planning_job.call_args.args[2]
        assert [artifact.document_id for artifact in artifacts] == ["doc-1", "doc-2"]
        assert artifacts[0].content_hash == "sha256:" + hashlib.sha256("본문 1".encode()).hexdigest()
        assert (artifacts[1].display_name, artifacts[1].summary, artifacts[1].preview_json) == ("문서 2", "요약 2", "{}")

    def test_single_content_still_creates_one_artifact(self) -> None:
        repository = MagicMock()
        repository.create_with_planning_job.side_effect = lambda run, job_id, artifacts=(): run

        StartAgentRunUseCase(repository).start(
            StartAgentRunRequest(
                workspace_id="workspace-1", user_id="user-1", instruction="만들어줘",
                action="workspace_workflow", provider="openai", model="gpt-5-nano",
                content=StartAgentRunContent(markdown="본문"),
            )
        )

        artifacts = repository.create_with_planning_job.call_args.args[2]
        assert [artifact.purpose for artifact in artifacts] == ["create_document"]
        assert artifacts[0].preview_json is None


def test_preview_object_stays_under_the_prefix_that_purge_sweeps() -> None:
    run_id = "run-1"
    scope = hashlib.sha256(run_id.encode()).hexdigest()

    assert _preview_object_key(run_id, "artifact-1").startswith(f"agent-runs/{scope}/")


def test_schema_adds_preview_columns_idempotently_for_existing_databases() -> None:
    schema = Path(__file__).resolve().parents[3].joinpath("db/ai_schema.sql").read_text()

    for column in ("display_name", "summary", "preview_object_key"):
        assert f"ADD COLUMN IF NOT EXISTS {column} text" in schema


class TestEditPreviewRoute:
    PREVIEWS = {
        "items": [
            {
                "operation_id": "op-1",
                "document_id": "doc-1",
                "display_name": "회의록",
                "base_version": 7,
                "summary": "요약",
                "added_lines": 1,
                "removed_lines": 1,
                "hunks": [
                    {
                        "before_start": 1,
                        "after_start": 1,
                        "lines": [{"type": "delete", "text": "a"}, {"type": "insert", "text": "b"}],
                    }
                ],
            },
            {
                "operation_id": "op-2",
                "tool_name": "create_document",
                "document_id": None,
                "display_name": "새 문서",
                "base_version": None,
                "summary": "생성",
                "added_lines": 1,
                "removed_lines": 0,
                "hunks": [{"before_start": 0, "after_start": 1,
                           "lines": [{"type": "insert", "text": "new"}]}],
            },
        ],
        "expires_at": "2026-11-05T00:00:00+00:00",
    }

    @pytest.fixture
    def client(self):
        repository = MagicMock()
        repository.get_edit_previews.side_effect = lambda workspace_id, user_id, run_id: (
            self.PREVIEWS if (workspace_id, user_id, run_id) == ("workspace-1", "user-1", "run-1") else None
        )
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_agent_run_repository] = lambda: repository
        yield TestClient(app)

    def test_owner_gets_contract_shape(self, client: TestClient) -> None:
        response = client.get("/agent/runs/run-1/edit-previews", params={"workspace_id": "workspace-1", "user_id": "user-1"})

        assert response.status_code == 200
        body = response.json()
        assert body["items"][0]["hunks"][0]["lines"][1] == {"type": "insert", "text": "b"}
        assert body["items"][0]["tool_name"] == "apply_document_edit"
        assert body["items"][0]["base_version"] == 7
        assert body["items"][1]["tool_name"] == "create_document"
        assert body["items"][1]["document_id"] is None and body["items"][1]["base_version"] is None
        assert body["items"][1]["removed_lines"] == 0
        assert body["expires_at"].startswith("2026-11-05")

    @pytest.mark.parametrize(
        "params",
        [{"workspace_id": "workspace-1", "user_id": "other"}, {"workspace_id": "other", "user_id": "user-1"}],
        ids=["other-user", "other-workspace"],
    )
    def test_other_actor_gets_404(self, client: TestClient, params: dict[str, str]) -> None:
        assert client.get("/agent/runs/run-1/edit-previews", params=params).status_code == 404

    def test_actor_is_required(self, client: TestClient) -> None:
        assert client.get("/agent/runs/run-1/edit-previews").status_code == 422


def test_preview_json_is_valid_for_storage() -> None:
    assert json.loads(json.dumps(build_edit_preview("a", "b"), ensure_ascii=False))["removed_lines"] == 1
