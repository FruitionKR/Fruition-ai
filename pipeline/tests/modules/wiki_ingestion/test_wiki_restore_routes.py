from app.modules.wiki_ingestion.application.models import (
    IngestOperationRestoreCommand,
    LintOperationRestoreCommand,
)
from app.modules.wiki_ingestion.interfaces.http.routes import (
    router,
    restore_ingest_operation,
    restore_lint_operation,
)
from app.modules.wiki_ingestion.interfaces.http.schemas import (
    IngestOperationRestoreIn,
    LintOperationRestoreIn,
)


class RestoreUseCase:
    def __init__(self) -> None:
        self.commands: list[
            IngestOperationRestoreCommand | LintOperationRestoreCommand
        ] = []

    def execute_ingest(self, command: IngestOperationRestoreCommand) -> dict:
        self.commands.append(command)
        return {"operation_type": "ingest_restore", "status": "succeeded"}

    def execute_lint(self, command: LintOperationRestoreCommand) -> dict:
        self.commands.append(command)
        return {"operation_type": "lint_restore", "status": "succeeded"}


def test_operation_restore_routes_are_split_by_operation_type() -> None:
    paths = {route.path for route in router.routes}

    assert "/wiki/ingest-restore-runs" in paths
    assert "/wiki/lint-restore-runs" in paths
    assert "/wiki/restore-runs" not in paths


def test_ingest_restore_route_preserves_source_and_contribution_order() -> None:
    use_case = RestoreUseCase()

    result = restore_ingest_operation(
        IngestOperationRestoreIn(
            operation_id="restore-1",
            restore_to_operation_id="A2",
            cancel_operation_ids=["A3", "A4", "A5"],
            workspace_id="ws-1",
            source_page={"page_id": "S1", "document_id": "doc-source"},
            rebuild_pages=[
                {
                    "page_id": "C3",
                    "keep_contributions": [
                        {"operation_id": "A2", "document_id": "doc-A"},
                        {"operation_id": "L", "document_id": "lint:L"},
                    ],
                }
            ],
        ),
        use_case=use_case,
    )

    command = use_case.commands[0]
    assert result["operation_type"] == "ingest_restore"
    assert command.restore_to_operation_id == "A2"
    assert command.cancel_operation_ids == ("A3", "A4", "A5")
    assert command.source_page.document_id == "doc-source"
    assert [
        item.operation_id for item in command.rebuild_pages[0].keep_contributions
    ] == ["A2", "L"]


def test_lint_restore_route_identifies_deleted_affected_pages() -> None:
    use_case = RestoreUseCase()

    result = restore_lint_operation(
        LintOperationRestoreIn(
            operation_id="restore-2",
            target_operation_id="lint-B",
            workspace_id="ws-1",
            rebuild_pages=[],
            deleted_pages=["C9"],
        ),
        use_case=use_case,
    )

    command = use_case.commands[0]
    assert result["operation_type"] == "lint_restore"
    assert command.target_operation_id == "lint-B"
    assert command.deleted_pages == ("C9",)


def test_operation_restore_rejects_cancelled_operation_as_active_input() -> None:
    invalid_payloads = (
        {
            "operation_id": "restore-1",
            "restore_to_operation_id": "A2",
            "cancel_operation_ids": ["A3", "A4", "A5"],
            "workspace_id": "ws-1",
            "source_page": {"page_id": "S1", "document_id": "doc-source"},
            "rebuild_pages": [
                {
                    "page_id": "C3",
                    "keep_contributions": [
                        {"operation_id": "A4", "document_id": "doc-A"},
                    ],
                }
            ],
        },
        {
            "operation_id": "restore-1",
            "target_operation_id": "lint-B",
            "workspace_id": "ws-1",
            "rebuild_pages": [
                {
                    "page_id": "C3",
                    "keep_contributions": [
                        {
                            "operation_id": "lint-B",
                            "document_id": "lint:lint-B",
                        }
                    ],
                }
            ],
        },
    )

    for payload, schema in zip(
        invalid_payloads,
        (IngestOperationRestoreIn, LintOperationRestoreIn),
        strict=True,
    ):
        try:
            schema(**payload)
        except ValueError as exc:
            expected_field = (
                "cancel_operation_ids"
                if schema is IngestOperationRestoreIn
                else "target_operation_id"
            )
            assert expected_field in str(exc)
        else:
            raise AssertionError("취소 대상 operation은 활성 입력에 포함할 수 없다")


def test_operation_restore_rejects_deleted_page_as_rebuild_target() -> None:
    common = {
        "operation_id": "restore-1",
        "workspace_id": "ws-1",
        "rebuild_pages": [{"page_id": "C3", "keep_contributions": []}],
        "deleted_pages": ["C3"],
    }
    payloads = (
        (IngestOperationRestoreIn, {
            **common,
            "restore_to_operation_id": "A2",
            "cancel_operation_ids": ["A3"],
            "source_page": {"page_id": "S1", "document_id": "doc-source"},
        }),
        (LintOperationRestoreIn, {
            **common,
            "target_operation_id": "lint-B",
        }),
    )

    for schema, payload in payloads:
        try:
            schema(**payload)
        except ValueError as exc:
            assert "deleted_pages" in str(exc)
        else:
            raise AssertionError("삭제 Page는 재조립 대상에 포함할 수 없다")


def test_ingest_restore_rejects_restored_source_as_deleted() -> None:
    try:
        IngestOperationRestoreIn(
            operation_id="restore-1",
            workspace_id="ws-1",
            restore_to_operation_id="A2",
            cancel_operation_ids=["A3"],
            source_page={"page_id": "S1", "document_id": "doc-source"},
            rebuild_pages=[],
            deleted_pages=["S1"],
        )
    except ValueError as exc:
        assert "source_page" in str(exc)
    else:
        raise AssertionError("복원할 Source Page는 삭제 대상에 포함할 수 없다")


def test_ingest_restore_rejects_source_as_rebuild_target() -> None:
    for restore_to_operation_id in (None, "A2"):
        try:
            IngestOperationRestoreIn(
                operation_id="restore-1",
                workspace_id="ws-1",
                restore_to_operation_id=restore_to_operation_id,
                cancel_operation_ids=["A3"],
                source_page={"page_id": "C3", "document_id": "doc-source"},
                rebuild_pages=[
                    {"page_id": "C3", "keep_contributions": []}
                ],
                deleted_pages=[],
            )
        except ValueError as exc:
            assert "source_page" in str(exc)
        else:
            raise AssertionError("Source Page는 재조립 대상에 포함할 수 없다")


def test_ingest_restore_allows_restore_point_contribution() -> None:
    payload = IngestOperationRestoreIn(
        operation_id="restore-1",
        restore_to_operation_id="A2",
        cancel_operation_ids=["A3", "A4", "A5"],
        workspace_id="ws-1",
        source_page={"page_id": "S1", "document_id": "doc-source"},
        rebuild_pages=[
            {
                "page_id": "C3",
                "keep_contributions": [
                    {"operation_id": "A2", "document_id": "doc-A"},
                ],
            }
        ],
    )

    assert payload.restore_to_operation_id == "A2"


def test_ingest_restore_rejects_invalid_cancel_range() -> None:
    for cancel_operation_ids in ([], ["A2"], ["A3", "A3"]):
        try:
            IngestOperationRestoreIn(
                operation_id="restore-1",
                restore_to_operation_id="A2",
                cancel_operation_ids=cancel_operation_ids,
                workspace_id="ws-1",
                source_page={"page_id": "S1", "document_id": "doc-source"},
                rebuild_pages=[],
            )
        except ValueError as exc:
            assert "cancel_operation_ids" in str(exc)
        else:
            raise AssertionError("취소 범위는 비어 있거나 중복될 수 없다")


def test_ingest_restore_requires_new_restore_operation_id() -> None:
    try:
        IngestOperationRestoreIn(
            operation_id="A2",
            restore_to_operation_id="A2",
            cancel_operation_ids=["A3"],
            workspace_id="ws-1",
            source_page={"page_id": "S1", "document_id": "doc-source"},
            rebuild_pages=[],
        )
    except ValueError as exc:
        assert "restore_to_operation_id" in str(exc)
    else:
        raise AssertionError("restore는 restore point와 다른 operation이어야 한다")
