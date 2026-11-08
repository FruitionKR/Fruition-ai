import pytest

from tests.modules.agent_run.test_agent_run_postgres import repositories


@pytest.mark.parametrize("missing", ["policy_generation", "spec_version", "tool_name", "proposal_request_hash"])
def test_mcp_provenance_rejects_null_required_fields(repositories, missing):
    from psycopg.errors import CheckViolation

    _runs, _jobs, connect = repositories
    values = {"policy_generation": 1, "spec_version": "2025-11-25",
              "tool_name": "propose_document_edit", "proposal_request_hash": "sha256:" + "a" * 64}
    values[missing] = None
    with pytest.raises(CheckViolation), connect() as conn:
        conn.execute(
            """INSERT INTO agent_runs (
                id, workspace_id, user_id, action, status, request_summary,
                origin, grant_id, client_id, invocation_id,
                policy_generation, spec_version, tool_name, proposal_request_hash
            ) VALUES ('run', 'workspace', 'user', 'workspace_workflow', 'awaiting_approval', 'Edit',
                      'MCP_CLIENT', 'grant', 'https://client', 'invocation', %s, %s, %s, %s)""",
            tuple(values.values()),
        )
