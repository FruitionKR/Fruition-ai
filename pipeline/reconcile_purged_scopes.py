"""access에 더 이상 없는 워크스페이스·사용자의 AI 데이터를 파기한다(과거 파기 누락분 일회성 정리).

access에서 내보낸 '현재 존재하는' ID 파일과 AI DB의 ID를 비교해, 없는 ID에 기존 파기 함수를 호출한다.
파기 함수가 멱등이라 재실행해도 같다. 기본은 dry-run이며 --apply로 실행한다. 절차는 docs/script.md 참고.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.modules.data_purge.infrastructure import postgres_purge

# purge_workspace / purge_user가 실제로 지우는 테이블의 범위 컬럼만 모은다. ai_model_usage는 정산용이라 제외한다.
_WORKSPACE_SOURCES = (
    ("ai_task_runs", "workspace_id"), ("agent_runs", "workspace_id"), ("agent_run_artifacts", "workspace_id"),
    ("skills", "workspace_id"), ("wiki_pages", "workspace_id"), ("wiki_schemas", "workspace_id"),
    ("document_derived_state", "workspace_id"), ("document_wiki_links", "workspace_id"),
    ("wiki_source_tombstones", "workspace_id"), ("pipeline_runs", "workspace_id"),
)
_USER_SOURCES = (
    ("ai_task_runs", "user_id"), ("agent_runs", "user_id"), ("agent_run_artifacts", "user_id"),
    ("skills", "owner_user_id"), ("wiki_schemas", "user_id"),
)


def read_ids(path: str) -> set[str]:
    ids = {line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()}
    if not ids:
        # 빈 파일은 export 실패일 가능성이 크다. 그대로 비교하면 모든 데이터를 파기한다.
        raise SystemExit(f"{path}: 빈 파일입니다. access에서 내보낸 ID 목록이 맞는지 확인하세요.")
    return ids


def _present(conn: Any, sources: tuple[tuple[str, str], ...]) -> set[str]:
    query = " UNION ".join(f"SELECT {column} AS id FROM {table} WHERE {column} IS NOT NULL" for table, column in sources)
    return {row["id"] for row in conn.execute(query).fetchall()}


def reconcile(
    workspace_ids: set[str],
    user_ids: set[str],
    *,
    apply: bool = False,
    connect: Callable[[], Any] = postgres_purge.connect,
    purge_workspace: Callable[[str], dict[str, int]] = postgres_purge.purge_workspace,
    purge_user: Callable[[str], dict[str, int]] = postgres_purge.purge_user,
) -> dict[str, object]:
    with connect() as conn:
        stale_workspaces = sorted(_present(conn, _WORKSPACE_SOURCES) - workspace_ids)
        stale_users = sorted(_present(conn, _USER_SOURCES) - user_ids)
    purged: dict[str, dict[str, dict[str, int]]] = {"workspaces": {}, "users": {}}
    if apply:
        purged["workspaces"] = {workspace_id: purge_workspace(workspace_id) for workspace_id in stale_workspaces}
        purged["users"] = {user_id: purge_user(user_id) for user_id in stale_users}
    return {
        "dry_run": not apply,
        "stale_workspace_ids": stale_workspaces,
        "stale_user_ids": stale_users,
        "purged": purged,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="access에 없는 워크스페이스·사용자의 AI 데이터를 확인하거나 파기합니다.")
    parser.add_argument("--workspace-ids-file", required=True, help="access에 현재 존재하는 workspace id, 한 줄에 하나")
    parser.add_argument("--user-ids-file", required=True, help="access에 현재 존재하는 user id, 한 줄에 하나")
    parser.add_argument("--apply", action="store_true", help="지정하면 실제 파기합니다. 생략하면 dry-run입니다.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = reconcile(
        read_ids(args.workspace_ids_file), read_ids(args.user_ids_file), apply=args.apply
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
