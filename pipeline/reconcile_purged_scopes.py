"""access에 더 이상 없는 워크스페이스·사용자의 AI 데이터를 파기한다(과거 파기 누락분 일회성 정리).

access에서 내보낸 '현재 존재하는' ID 파일과 AI DB의 ID를 비교해, 없는 ID에 기존 파기 함수를 호출한다.
파기 함수가 멱등이라 재실행해도 같다. 기본은 dry-run이며 --apply로 실행한다. 절차는 docs/script.md 참고.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.modules.data_purge.infrastructure import postgres_purge

# purge_workspace / purge_user가 실제로 지우는 테이블의 (테이블, 범위 컬럼, 시각 컬럼)만 모은다. ai_model_usage는 정산용이라 제외한다.
# 생성 시각 컬럼이 없는 테이블은 그 이후에 갱신되는 컬럼을 쓴다. 실제 생성보다 늦은 시각은 "최근 생성"으로 보여 건너뛰는 쪽(안전)으로만 틀린다.
_WORKSPACE_SOURCES = (
    ("ai_task_runs", "workspace_id", "created_at"), ("agent_runs", "workspace_id", "created_at"),
    ("agent_run_artifacts", "workspace_id", "created_at"), ("skills", "workspace_id", "created_at"),
    ("wiki_pages", "workspace_id", "created_at"), ("wiki_schemas", "workspace_id", "created_at"),
    ("document_derived_state", "workspace_id", "updated_at"), ("document_wiki_links", "workspace_id", "created_at"),
    ("wiki_source_tombstones", "workspace_id", "deleted_at"), ("pipeline_runs", "workspace_id", "created_at"),
)
_USER_SOURCES = (
    ("ai_task_runs", "user_id", "created_at"), ("agent_runs", "user_id", "created_at"),
    ("agent_run_artifacts", "user_id", "created_at"), ("skills", "owner_user_id", "created_at"),
    ("wiki_schemas", "user_id", "created_at"),
)
# export 시점에 이미 있었지만 시계 차이·export 지연으로 놓치는 경우를 피하는 안전 여유.
EXPORT_SAFETY_MARGIN = timedelta(hours=1)


def read_ids(path: str) -> set[str]:
    ids = {line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()}
    if not ids:
        # 빈 파일은 export 실패일 가능성이 크다. 그대로 비교하면 모든 데이터를 파기한다.
        raise SystemExit(f"{path}: 빈 파일입니다. access에서 내보낸 ID 목록이 맞는지 확인하세요.")
    return ids


def _earliest_seen(conn: Any, sources: tuple[tuple[str, str, str], ...]) -> dict[str, datetime | None]:
    """AI 테이블에 나타난 ID별 가장 이른 시각. 시각 값이 하나도 없으면 None."""
    query = "SELECT id, min(seen) AS seen FROM (" + " UNION ALL ".join(
        f"SELECT {column} AS id, {seen} AS seen FROM {table} WHERE {column} IS NOT NULL"
        for table, column, seen in sources
    ) + ") t GROUP BY id"
    return {row["id"]: row["seen"] for row in conn.execute(query).fetchall()}


def _split(earliest: dict[str, datetime | None], existing: set[str], cutoff: datetime):
    purge, skipped = {}, {}
    for id_ in sorted(earliest.keys() - existing):
        seen = earliest[id_]
        if seen is None:
            skipped[id_] = "no timestamp evidence, skipped"
        elif seen >= cutoff:
            skipped[id_] = "created after export, skipped"
        else:
            purge[id_] = seen.isoformat()
    return purge, skipped


def reconcile(
    workspace_ids: set[str],
    user_ids: set[str],
    exported_at: datetime,
    *,
    apply: bool = False,
    connect: Callable[[], Any] = postgres_purge.connect,
    purge_workspace: Callable[[str], dict[str, int]] = postgres_purge.purge_workspace,
    purge_user: Callable[[str], dict[str, int]] = postgres_purge.purge_user,
) -> dict[str, object]:
    cutoff = exported_at - EXPORT_SAFETY_MARGIN
    with connect() as conn:
        # export 이후 새로 생긴 워크스페이스·사용자는 목록에 없어도 파기하지 않는다. 증거가 없으면 지우지 않는다.
        workspaces, skipped_workspaces = _split(_earliest_seen(conn, _WORKSPACE_SOURCES), workspace_ids, cutoff)
        users, skipped_users = _split(_earliest_seen(conn, _USER_SOURCES), user_ids, cutoff)
    purged: dict[str, dict[str, dict[str, int]]] = {"workspaces": {}, "users": {}}
    if apply:
        purged["workspaces"] = {i: purge_workspace(i) for i in workspaces}
        purged["users"] = {i: purge_user(i) for i in users}
    return {
        "dry_run": not apply,
        "cutoff": cutoff.isoformat(),
        "to_purge_workspaces": workspaces,
        "to_purge_users": users,
        "skipped_workspaces": skipped_workspaces,
        "skipped_users": skipped_users,
        "purged": purged,
    }


def parse_exported_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("시간대(예: +09:00, Z)가 있는 ISO-8601 시각이 필요합니다.")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="access에 없는 워크스페이스·사용자의 AI 데이터를 확인하거나 파기합니다.")
    parser.add_argument("--workspace-ids-file", required=True, help="access에 현재 존재하는 workspace id, 한 줄에 하나")
    parser.add_argument("--user-ids-file", required=True, help="access에 현재 존재하는 user id, 한 줄에 하나")
    parser.add_argument("--exported-at", required=True, type=parse_exported_at,
                        help="access ID 목록을 내보낸 시각(ISO-8601, 시간대 포함). 이보다 1시간 이상 전에 생긴 ID만 파기합니다.")
    parser.add_argument("--apply", action="store_true", help="지정하면 실제 파기합니다. 생략하면 dry-run입니다.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = reconcile(
        read_ids(args.workspace_ids_file), read_ids(args.user_ids_file), args.exported_at, apply=args.apply
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
