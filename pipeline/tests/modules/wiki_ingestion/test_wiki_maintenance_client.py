from app.modules.wiki_ingestion.application.models import WikiMaintenanceCommand
from app.modules.wiki_ingestion.infrastructure.wiki_maintenance import (
    _lint_api_client,
)


def test_claude_lint_client_uses_selected_provider(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    client = _lint_api_client(
        WikiMaintenanceCommand(
            user_id="user-1",
            workspace_id="workspace-1",
            provider="claude",
            model="claude-sonnet-5",
        )
    )

    assert client.provider == "claude"
