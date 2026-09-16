from fastapi import HTTPException
from pydantic import ValidationError

import api
from app.modules.wiki_ingestion.application.models import (
    WikiMaintenanceCommand,
    WikiMaintenanceConfigurationError,
)
from app.modules.wiki_ingestion.interfaces.http.routes import lint_wiki_workspace
from app.modules.wiki_ingestion.interfaces.http.schemas import WikiLintIn


class FakeWikiMaintenance:
    def __init__(self) -> None:
        self.commands: list[WikiMaintenanceCommand] = []

    def lint(self, command: WikiMaintenanceCommand) -> dict:
        self.commands.append(command)
        return {
            "user_id": command.user_id,
            "workspace_id": command.workspace_id,
            "active_path": "wiki/user-1/workspace-1/clusters/active.md",
            "cluster_count": 2,
            "source_ref_count": 3,
            "orphan_refs": ["doc-1:B9999"],
            "promotion_candidates": ["candidate-1"],
            "needs_review": [],
            "relation_candidates": [],
            "invalid_relations": [],
            "invalid_promotions": [],
            "reconciliation_candidates": [],
            "applied_reconciliations": [],
            "applied_cluster_reconciliation": {
                "removed_claims": [],
                "removed_relations": [],
            },
            "materialized_promotions": [],
            "merged_promotions": [],
            "materialized_relations": [],
        }


def test_wiki_maintenance_route_returns_workspace_result() -> None:
    maintenance = FakeWikiMaintenance()

    response = lint_wiki_workspace(
        WikiLintIn(
            user_id="user-1",
            workspace_id="workspace-1",
            provider="openai",
            model="gpt-5-nano",
        ),
        maintenance=maintenance,
    )

    assert maintenance.commands[0].dry_run is True
    assert maintenance.commands[0].provider == "openai"
    assert maintenance.commands[0].model == "gpt-5-nano"
    assert response.workspace_id == "workspace-1"
    assert response.promotion_candidates == ["candidate-1"]
    assert response.orphan_refs == ["doc-1:B9999"]


def test_wiki_lint_requires_operation_id_only_when_changes_are_applied() -> None:
    assert WikiLintIn(provider="openai", model="gpt-5-nano", dry_run=True).operation_id is None

    try:
        WikiLintIn(provider="openai", model="gpt-5-nano", dry_run=False)
    except ValidationError as exc:
        assert "operation_id" in str(exc)
    else:
        raise AssertionError("실행 lint는 operation_id가 필요해야 한다")

    payload = WikiLintIn(
        provider="openai",
        model="gpt-5-nano",
        dry_run=False,
        operation_id="lint-op-1",
    )

    assert payload.to_command().operation_id == "lint-op-1"


def test_wiki_maintenance_route_is_registered_on_app() -> None:
    operation = api.app.openapi()["paths"]["/wiki/maintenance/lint"]

    assert "post" in operation


def test_wiki_maintenance_route_hides_unexpected_failure_details() -> None:
    class FailingWikiMaintenance:
        def lint(self, command: WikiMaintenanceCommand) -> dict:
            raise ValueError("secret-internal-detail")

    try:
        lint_wiki_workspace(
            WikiLintIn(
                user_id="user-1",
                workspace_id="workspace-1",
                provider="openai",
                model="gpt-5-nano",
            ),
            maintenance=FailingWikiMaintenance(),
        )
    except HTTPException as exc:
        assert exc.status_code == 500
        assert exc.detail["code"] == "internal_server_error"
        assert "secret-internal-detail" not in str(exc.detail)
    else:
        raise AssertionError("unexpected lint failure should return HTTP 500")


def test_wiki_maintenance_route_returns_configuration_error_as_bad_request() -> None:
    class InvalidConfigurationWikiMaintenance:
        def lint(self, command: WikiMaintenanceCommand) -> dict:
            raise WikiMaintenanceConfigurationError("Missing API key")

    try:
        lint_wiki_workspace(
            WikiLintIn(
                user_id="user-1",
                workspace_id="workspace-1",
                provider="openai",
                model="gpt-5-nano",
            ),
            maintenance=InvalidConfigurationWikiMaintenance(),
        )
    except HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == "Missing API key"
    else:
        raise AssertionError("invalid lint configuration should return HTTP 400")
