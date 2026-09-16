import json

from app.modules.wiki_ingestion.infrastructure.lint_operation_artifacts import (
    persist_lint_operation_artifacts,
)
from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as repository,
)


def test_persists_replayable_lint_page_markdown_and_json() -> None:
    writes: list[tuple[str, str, str]] = []
    added_link = {
        "source": "concept:shared",
        "target": "concept:target",
        "relation": "related_to",
    }
    removed_link = {
        "source": "concept:shared",
        "target": "concept:old",
        "relation": "related_to",
    }

    artifacts = persist_lint_operation_artifacts(
        operation_id="lint-op-1",
        workspace_id="ws-1",
        page_changes=[
            {
                "page_id": "page-shared",
                "slug": "shared",
                "title": "Shared",
                "markdown": "# Shared\n\n## Evidence\n- lint 근거 [doc-A:B0001]\n",
                "content_action": "append_evidence",
                "claims": [
                    {
                        "id": "claim-1",
                        "claim": "lint 근거",
                        "refs": ["doc-A:B0001"],
                    }
                ],
                "added_links": [added_link],
                "removed_links": [removed_link],
            }
        ],
        write_text=lambda key, text, content_type: (
            writes.append((key, text, content_type)) or f"s3://bucket/{key}"
        ),
    )

    assert [item[0] for item in writes] == [
        "wiki/ws-1/pages/page-shared/ops/lint-op-1.md",
        "wiki/ws-1/pages/page-shared/ops/lint-op-1.json",
    ]
    payload = json.loads(writes[1][1])
    assert payload["artifact_type"] == "lint"
    assert payload["content_action"] == "append_evidence"
    assert payload["concept"]["slug"] == "shared"
    assert payload["evidence_units"][0]["anchor_reference_ids"] == [
        "doc-A:B0001"
    ]
    assert payload["added_links"] == [added_link]
    assert payload["removed_links"] == [removed_link]
    assert artifacts[0]["contribution_key"].endswith("lint-op-1.json")


def test_lint_artifact_requires_at_least_one_replayable_action() -> None:
    try:
        persist_lint_operation_artifacts(
            operation_id="lint-op-1",
            workspace_id="ws-1",
            page_changes=[
                {
                    "page_id": "page-shared",
                    "slug": "shared",
                    "title": "Shared",
                    "markdown": "# Shared\n",
                    "content_action": "none",
                    "claims": [],
                    "added_links": [],
                    "removed_links": [],
                }
            ],
            write_text=lambda *_args: "",
        )
    except ValueError as exc:
        assert "replayable action" in str(exc)
    else:
        raise AssertionError("동작 없는 lint artifact는 저장하면 안 된다")


def test_persists_link_only_lint_change_for_its_source_page(monkeypatch) -> None:
    link = {
        "source": "concept:shared",
        "target": "concept:target",
        "relation": "part_of",
    }
    writes: list[tuple[str, str, str]] = []

    class Result:
        def fetchall(self):
            return [
                {
                    "id": "page-shared",
                    "slug": "shared",
                    "title": "Shared",
                    "summary": "공유 개념",
                    "markdown_uri": "wiki/current/shared.md",
                }
            ]

    class Connection:
        def execute(self, query, params):
            assert params == ("user-1", "ws-1", ["shared"])
            assert "status = 'active'" not in query
            return Result()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(repository, "connect", lambda: Connection())
    monkeypatch.setattr(
        repository,
        "read_text_object",
        lambda _key: "# Shared\n",
    )
    monkeypatch.setattr(
        repository,
        "write_text_object",
        lambda key, text, content_type: (
            writes.append((key, text, content_type)) or f"s3://bucket/{key}"
        ),
    )

    artifacts = repository.persist_lint_operation_result(
        "user-1",
        "ws-1",
        "lint-op-1",
        {
            "materialized_relations": [
                {"from": "shared", "to": "target", "relation": "part_of"}
            ],
            "applied_reconciliations": [],
            "removed_orphan_links": [],
        },
    )

    payload = json.loads(writes[1][1])
    assert payload["content_action"] == "none"
    assert payload["added_links"] == [link]
    assert artifacts[0]["page_id"] == "page-shared"


def test_missing_slug_markdown_read_failure_propagates_instead_of_empty_markdown(
    monkeypatch,
) -> None:
    """object storage 읽기 실패는 빈 markdown이 아니라 예외로 드러나야 한다.

    _read_optional_text_object처럼 예외를 삼켜 ""를 반환하면, 일시적인 저장소 장애가
    진짜 빈 페이지와 구분되지 않은 채 operation artifact로 저장돼 콘텐츠 손실이 된다.
    """
    link = {
        "source": "concept:shared",
        "target": "concept:target",
        "relation": "part_of",
    }

    class Result:
        def fetchall(self):
            return [
                {
                    "id": "page-shared",
                    "slug": "shared",
                    "title": "Shared",
                    "summary": "공유 개념",
                    "markdown_uri": "wiki/current/shared.md",
                }
            ]

    class Connection:
        def execute(self, _query, _params):
            return Result()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(repository, "connect", lambda: Connection())

    def raise_storage_error(_key):
        raise OSError("storage read failed")

    monkeypatch.setattr(repository, "read_text_object", raise_storage_error)

    try:
        repository.persist_lint_operation_result(
            "user-1",
            "ws-1",
            "lint-op-1",
            {
                "materialized_relations": [
                    {"from": "shared", "to": "target", "relation": "part_of"}
                ],
                "applied_reconciliations": [],
                "removed_orphan_links": [],
            },
        )
    except OSError:
        pass
    else:
        raise AssertionError("storage 읽기 실패가 전파돼야 한다")
