"""로컬 전체 스택 검증. RUN_LIVE_TASK_E2E=1일 때만 실제 모델과 서비스를 호출한다."""
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from dotenv import dotenv_values
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

pytestmark = pytest.mark.skipif(os.getenv("RUN_LIVE_TASK_E2E") != "1", reason="실서비스·모델 E2E는 명시적으로 실행합니다.")


def test_live_organization_and_query_cancellation():
    config = dotenv_values(Path(os.environ["LIVE_TASK_ENV_FILE"]))
    def connect(db):
        return psycopg.connect(make_conninfo(host="127.0.0.1", port=5432, dbname=db,
            user=config.get("POSTGRES_ADMIN_USER", "fruition_admin"), password=config["POSTGRES_ADMIN_PASSWORD"]), row_factory=dict_row)
    run_key = uuid4().hex
    report = {"input": "현재까지 업로드 한 문서, 알맞은 폴더 이름 생성해서 주제별로 정리해 줘"}
    token = None
    with httpx.Client(timeout=90) as client:
        def request(method, path, body=None, access=False):
            headers = {"Idempotency-Key": uuid4().hex}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            response = client.request(method, f"http://localhost:{8081 if access else 8080}" + path, json=body, headers=headers)
            assert response.is_success, f"{method} {path}: {response.status_code} {response.text[:1000]}"
            return response.json() if response.content else None
        def wait(fetch, predicate, timeout=180):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                value = fetch()
                if predicate(value):
                    return value
                time.sleep(0.1)
            pytest.fail(f"상태 대기 시간 초과: {value}")
        email, password = f"cancel-{run_key}@example.com", "cancellation-e2e-password"
        verification = request("POST", "/api/auth/email-verifications", {"email": email, "purpose": "signup"}, access=True)
        confirmed = request("POST", f"/api/auth/email-verifications/{verification['verification_id']}/confirm", {"code": "9700"}, access=True)
        request("POST", "/api/auth/signup", {"email": email, "password": password, "display_name": "취소 검증", "verification_token": confirmed["verification_token"]}, access=True)
        token = request("POST", "/api/auth/login", {"email": email, "password": password}, access=True)["access_token"]
        workspace = request("POST", "/api/workspaces", {"name": "취소 E2E " + run_key}, access=True)
        wid = workspace["id"]
        base = f"/api/workspaces/{wid}"
        report["workspace_id"] = wid
        # 새 workspace의 자동 안내 문서는 fixture에 포함하지 않는다.
        for item in request("GET", base + "/navigation")["items"]:
            if item["type"] == "document":
                request("DELETE", base + "/documents/" + item["id"], {"base_version": item["current_version"]})
        folders = [request("POST", base + "/folders", {"name": name}) for name in ("미분류", "자료")]
        docs = []
        for i, title in enumerate(("React 컴포넌트", "CSS 레이아웃", "Spring API", "PostgreSQL 설계", "광고 캠페인", "브랜드 전략")):
            docs.append(request("POST", base + "/documents/markdown", {"display_name": title,
                "markdown": f"# {title}\n\n{title}에 관한 자료입니다.\n", "folder_id": folders[i % 2]["id"]}))
        def structure():
            with connect("core_db") as conn:
                return {"folders": conn.execute("SELECT id::text, name, parent_folder_id::text, row_number() OVER (PARTITION BY parent_folder_id ORDER BY sort_order, id) AS position FROM folders WHERE workspace_id = %s AND deleted_at IS NULL ORDER BY id", (wid,)).fetchall(),
                        "documents": conn.execute("SELECT id, display_name, folder_id::text, row_number() OVER (PARTITION BY folder_id ORDER BY sort_order, id) AS position FROM documents WHERE workspace_id = %s AND deleted_at IS NULL ORDER BY id", (wid,)).fetchall()}
        before = structure()
        assert len(before["folders"]) == 2 and len(before["documents"]) == 6
        report["before"] = before
        session = request("POST", base + "/chat/sessions", {"title": "취소 검증"})
        turn = request("POST", base + "/agent/turn", {"session_id": session["id"], "message": report["input"],
                       "provider": "gemini", "model": "gemini-3.1-flash-lite", "allow_web_search": False})
        parent_id = turn["requestId"]
        def child_id():
            with connect("ai_db") as conn:
                row = conn.execute("SELECT result->>'run_id' AS child, status, error_code FROM agent_runs WHERE id = %s", (parent_id,)).fetchone()
                if row and row["status"] in {"failed", "rejected"}:
                    pytest.fail(f"Agent 라우팅 실패: {row}")
                return row["child"] if row else None
        child = wait(child_id, bool)
        plan = wait(lambda: request("GET", base + f"/agent/runs/{child}"), lambda value: value["status"] not in {"queued", "planning"})
        assert plan["status"] == "awaiting_approval", plan
        report["plan"] = plan["plan"]
        assert structure() == before
        operations = plan["plan"]["operations"]
        blocked = next(op for op in reversed(operations) if op["tool_name"] == "move_document")
        # 실제 HTTP·worker를 유지하고 문서 행 잠금만 잡아 실행 중 취소를 재현한다.
        with connect("core_db") as lock:
            lock.execute("SELECT id FROM documents WHERE id = %s FOR UPDATE", (blocked["arguments"]["document_id"],))
            request("POST", base + f"/agent/runs/{child}/approve", {"plan_version": plan["plan"]["version"], "operation_hash": plan["plan"]["operation_hash"]})
            def dispatched():
                with connect("ai_db") as conn:
                    return conn.execute("SELECT status FROM agent_tool_executions WHERE operation_id = %s AND status = 'running'", (blocked["id"],)).fetchone()
            wait(dispatched, bool)
            with connect("ai_db") as conn:
                completed = conn.execute("SELECT count(*) AS n FROM agent_plan_operations WHERE plan_id = %s AND status = 'succeeded'", (plan["plan"]["id"],)).fetchone()["n"]
            assert 0 < completed < len(operations)
            report["completed_before_cancel"] = completed
            report["cancel_response"] = request("POST", base + f"/ai/tasks/{parent_id}/cancel")
        final = wait(lambda: request("GET", base + f"/ai/tasks/{parent_id}"), lambda value: value["status"] in {"cancelled", "rollback_failed"})
        assert final["status"] == "cancelled", final
        with connect("ai_db") as conn:
            report["forward_results"] = conn.execute(
                "SELECT tool_name, status FROM agent_tool_executions WHERE run_id = %s ORDER BY finished_at",
                (child,),
            ).fetchall()
        report["after"] = structure()
        assert report["after"] == before
        report["organization_status"] = final
        # 같은 공통 경로가 일반 Query의 말풍선도 복구하는지 확인한다.
        query = request("POST", base + f"/chat/sessions/{session['id']}/query/runs", {"question": "문서 내용을 요약해 줘", "provider": "gemini", "model": "gemini-3.1-flash-lite", "allow_web_search": False})
        qid = query["request_id"]
        request("POST", base + f"/ai/tasks/{qid}/cancel")
        qfinal = wait(lambda: request("GET", base + f"/ai/tasks/{qid}"), lambda value: value["status"] in {"cancelled", "rollback_failed"})
        assert qfinal["status"] == "cancelled", qfinal
        with connect("core_db") as conn:
            assert conn.execute("SELECT count(*) AS n FROM chat_messages WHERE session_id = %s", (session["id"],)).fetchone()["n"] == 0
        report["query_status"] = qfinal
    output = Path(os.getenv("AI_CANCEL_E2E_REPORT", "/tmp/ai-cancel-e2e-report.json"))
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps(report, ensure_ascii=False, default=str))
