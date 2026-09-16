import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


PIPELINE_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("skill_enabled", "agent_enabled", "expected"),
    [
        (False, False, {"skill": False, "agent_run": False, "run_draft": False}),
        (True, False, {"skill": True, "agent_run": False, "run_draft": False}),
        (False, True, {"skill": False, "agent_run": True, "run_draft": True}),
        (True, True, {"skill": True, "agent_run": True, "run_draft": True}),
    ],
)
def test_skill_and_agent_run_routes_have_independent_flags(
    skill_enabled: bool,
    agent_enabled: bool,
    expected: dict[str, bool],
) -> None:
    environment = os.environ.copy()
    environment["SKILL_API_ENABLED"] = str(skill_enabled).lower()
    environment["AGENT_SKILLS_ENABLED"] = str(agent_enabled).lower()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, api; paths = api.app.openapi()['paths']; "
                "print(json.dumps({'skill': '/skills' in paths, "
                "'agent_run': any(path.startswith('/agent/runs/') for path in paths), "
                "'run_draft': '/skills/draft-from-runs/preview' in paths}))"
            ),
        ],
        cwd=PIPELINE_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == expected
