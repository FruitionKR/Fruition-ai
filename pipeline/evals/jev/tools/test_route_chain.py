"""run_route_chain 오프라인 확인. 사용: PYTHONPATH=<dev>/pipeline python test_route_chain.py"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import run_route_chain
from app.core.jev_client import JevUnavailable
from run_route_chain import goal_format_conflict

assert goal_format_conflict("checklist", ["numbered_list"]) == "list_goal_with_numbered_list"
assert goal_format_conflict("bullet_list", ["numbered_list"]) == "list_goal_with_numbered_list"
assert goal_format_conflict("convert_format", []) == "convert_format_without_format"
assert goal_format_conflict("checklist", []) is None
assert goal_format_conflict("convert_format", ["numbered_list"]) is None
assert goal_format_conflict("shorten", []) is None

# 실제 Jev 라우터가 LLM으로 넘긴 뒤 LLM도 실패해도 호출 횟수를 보존한다.
client = Mock()
client.choose.side_effect = JevUnavailable("offline Jev failure")
llm = Mock()
llm.route.side_effect = RuntimeError("offline fallback failure")
with TemporaryDirectory() as directory, \
        patch.dict(os.environ), \
        patch("sys.argv", ["run_route_chain.py", "--limit", "1"]), \
        patch.object(run_route_chain, "RUNS", 1), \
        patch.object(run_route_chain, "load_keys"), \
        patch.object(run_route_chain, "results_dir", return_value=Path(directory)), \
        patch("app.core.jev_client.build_jev_client", return_value=client), \
        patch("app.modules.agent.infrastructure.chat_completions_turn_router.build_agent_turn_router", return_value=llm), \
        patch("app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor.build_markdown_editor"):
    run_route_chain.main()
    rows = [json.loads(line) for line in (Path(directory) / "rows.jsonl").read_text().splitlines()]
    summary = json.loads((Path(directory) / "summary.json").read_text())
    client.choose.assert_called_once()
    assert llm.route.call_count == 2  # Jev fallback 한 번, LLM 직접 호출 한 번.
    jev_row = next(row for row in rows if row["variant"] == "jev")
    assert jev_row["ok"] is False and jev_row["error"] == "RuntimeError: offline fallback failure"
    assert jev_row["fallback_calls"] == 1, jev_row
    assert summary["jev"]["router_llm_fallback_rows"] == 1, summary
print("ok")
