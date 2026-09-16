"""커밋된 api-specs 명세가 전체 기능 프로필의 코드와 일치하는지 본다.

텍스트로 비교하는 이유: 의미만 비교하면 직렬화 스타일·순서가 흔들려도 통과해 diff가 신호를 잃는다.
backend(access-svc·document-svc)의 스냅샷 테스트와 같은 규칙이다.
"""

import os
import subprocess
import sys
from pathlib import Path

import yaml

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = PIPELINE_ROOT / "api-specs/openapi.yaml"


def render_openapi_yaml() -> str:
    environment = os.environ.copy()
    environment.update(AGENT_SKILLS_ENABLED="true", SKILL_API_ENABLED="true")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import copy, yaml, api; "
                "spec = copy.deepcopy(api.app.openapi()); "
                "spec.pop('servers', None); "
                "print(yaml.safe_dump(spec, allow_unicode=True, sort_keys=True, "
                "default_flow_style=False, width=10**9), end='')"
            ),
        ],
        cwd=PIPELINE_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_openapi_matches_committed_snapshot() -> None:
    actual = render_openapi_yaml()
    spec = yaml.safe_load(actual)
    agent_turn_headers = {
        parameter["name"]
        for parameter in spec["paths"]["/agent/turn"]["post"]["parameters"]
        if parameter["in"] == "header"
    }
    assert agent_turn_headers >= {"X-Internal-Token", "X-Agent-Service-Token"}

    # UPDATE_OPENAPI_SNAPSHOT=1 로 실행하면 비교 대신 커밋 대상 파일을 갱신한다.
    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT"):
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(actual, encoding="utf-8")
        return

    expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        f"OpenAPI 명세가 {SNAPSHOT_PATH}와 다릅니다. "
        "UPDATE_OPENAPI_SNAPSHOT=1 pytest tests/test_openapi_snapshot.py 로 갱신하세요."
    )
