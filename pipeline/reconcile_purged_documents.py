"""core에 더 이상 없는 문서의 AI 복사본을 지운다(영구 삭제 기능 전에 지운 문서의 일회성 정리).

core에서 내보낸 '현재 존재하는' 문서 ID와 AI의 휴지통 표시(wiki_source_tombstones)를 비교해, 없는 문서에 기존 purge_document를 호출한다.
purge_document는 요청 행을 먼저 커밋하므로 진행 중 run·잠금 실패로 끝내지 못한 문서는 agent worker가 이어서 끝낸다.
기본은 읽기 전용 dry-run이며 --apply로 실행한다. 출력에는 ID를 쓰지 않는다. 절차는 docs/script.md 참고.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.modules.data_purge.infrastructure import document_copy_purge as purge
from app.modules.data_purge.infrastructure.purged_document_markdown import without_document_clusters, without_document_log
from app.modules.task_cancellation.infrastructure import object_change_journal
from app.modules.wiki_generation.domain.text_utils import slugify
from app.modules.wiki_ingestion.infrastructure import object_storage
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

# 목록이 잘렸으면 살아 있는 문서를 지운다. 활성 소스 페이지의 이 비율도 목록에 없으면 목록을 믿지 않는다.
MIN_ACTIVE_SOURCE_COVERAGE = 0.5
MAX_EXPORT_AGE = timedelta(hours=24)
DEFAULT_MIN_AGE_HOURS = 24
_DELETED_DOCUMENT_TABLES = ("source_blocks", "source_block_snapshots", "document_derived_state")


@dataclass(frozen=True)
class Target:
    workspace_id: str
    document_id: str


def read_document_ids(path: str, minimum: int) -> set[str]:
    ids = {line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()}
    if not ids:
        raise SystemExit(f"{path}: 빈 파일입니다. core에서 내보낸 문서 ID 목록이 맞는지 확인하세요.")
    if len(ids) < minimum:
        raise SystemExit(f"{path}: 문서 ID {len(ids)}건은 --min-documents {minimum}보다 적습니다. 잘린 목록이면 살아 있는 문서를 지웁니다.")
    return ids


def _normalized(document_ids: set[str]) -> set[str]:
    # 소스 페이지 슬러그는 정규화한 문서 id라, 슬러그가 같은 살아 있는 문서가 있으면 그 페이지를 함께 지운다. 정규화 전후를 모두 본다.
    return document_ids | {slugify(document_id) for document_id in document_ids}


def _is_existing(document_id: str, existing: set[str]) -> bool:
    return document_id in existing or slugify(document_id) in existing


def _require_covering_list(conn: Any, existing: set[str], cutoff: datetime) -> None:
    slugs = [row["slug"] for row in conn.execute(
        "SELECT slug FROM wiki_pages WHERE page_type = 'source' AND status <> 'deleted' AND created_at < %s", (cutoff,),
    ).fetchall()]
    if not slugs:
        return
    coverage = sum(slug in existing for slug in slugs) / len(slugs)
    if coverage < MIN_ACTIVE_SOURCE_COVERAGE:
        raise SystemExit(f"활성 소스 페이지 {len(slugs)}개 중 목록에 있는 비율이 {coverage:.0%}입니다. 목록이 잘린 것으로 보고 거부합니다.")


def find_targets(conn: Any, existing: set[str], cutoff: datetime) -> tuple[list[Target], Counter[str]]:
    skipped: Counter[str] = Counter()
    rows = conn.execute(
        """
        SELECT tombstone.workspace_id, tombstone.document_id, tombstone.deleted_at,
               request.finished_at IS NOT NULL AS finished,
               scope.scope_id IS NOT NULL AS scope_purged,
               EXISTS (SELECT 1 FROM pipeline_runs run WHERE run.document_id = tombstone.document_id AND run.workspace_id <> tombstone.workspace_id)
               OR EXISTS (SELECT 1 FROM document_wiki_links link WHERE link.document_id = tombstone.document_id AND link.workspace_id <> tombstone.workspace_id)
                   AS other_workspace
        FROM wiki_source_tombstones tombstone
        LEFT JOIN ai_document_purges request
          ON request.workspace_id = tombstone.workspace_id AND request.document_id = tombstone.document_id
        LEFT JOIN ai_purged_scopes scope ON scope.scope_type = 'workspace' AND scope.scope_id = tombstone.workspace_id
        ORDER BY tombstone.workspace_id, tombstone.document_id
        """
    ).fetchall()
    targets = []
    for row in rows:
        if _is_existing(row["document_id"], existing):
            skipped["exists_in_core"] += 1
        elif row["scope_purged"]:
            # 워크스페이스 파기가 이미 지웠고, 파기된 범위의 작업 기록은 쓰기가 거절된다.
            skipped["workspace_purged"] += 1
        elif row["finished"]:
            skipped["already_finished"] += 1
        elif row["other_workspace"]:
            # purge_document가 거절해 worker가 영원히 다시 시도한다. 사람이 볼 일이다.
            skipped["other_workspace"] += 1
        elif row["deleted_at"] >= cutoff:
            skipped["too_recent"] += 1
        else:
            targets.append(Target(row["workspace_id"], row["document_id"]))
    return targets, skipped


def _count_untombstoned(conn: Any, existing: set[str], cutoff: datetime) -> int:
    """휴지통 표시 없이 사라진 문서. 이 스크립트 대상이 아니라서 건수만 알린다."""
    rows = conn.execute(
        """
        SELECT document_id FROM pipeline_runs
        WHERE document_id IS NOT NULL AND created_at < %(cutoff)s
          AND workspace_id NOT IN (SELECT scope_id FROM ai_purged_scopes WHERE scope_type = 'workspace')
        UNION
        SELECT document_id FROM document_wiki_links
        WHERE created_at < %(cutoff)s
          AND workspace_id NOT IN (SELECT scope_id FROM ai_purged_scopes WHERE scope_type = 'workspace')
        EXCEPT
        SELECT document_id FROM wiki_source_tombstones
        """,
        {"cutoff": cutoff},
    ).fetchall()
    return sum(not _is_existing(row["document_id"], existing) for row in rows)


def _citing_rows(conn: Any, target: tuple[str, str, str], params: dict[str, Any]) -> set[Any]:
    table, column, key = target
    rows = conn.execute(
        f"SELECT {key} AS key, {column}::text AS value FROM {table} WHERE workspace_id = %s AND {column}::text LIKE %s",
        (params["ws"], f"%{params['doc']}%"),
    ).fetchall()
    return {row["key"] for row in rows
            if purge._blank_source_text(json.loads(row["value"]), params["doc"]) != json.loads(row["value"])}


def _ids(conn: Any, query: str, params: dict[str, Any]) -> set[Any]:
    return {row["id"] for row in conn.execute(query, params).fetchall()}


def estimate_rows(conn: Any, workspace_id: str, document_id: str) -> Counter[str]:
    """purge_document가 바꿀 행 수. 쓰지 않고 같은 조건으로 센다."""
    params: dict[str, Any] = {"ws": workspace_id, "doc": document_id, "kinds": purge._DOCUMENT_TASK_KINDS}
    evidence = {target[0]: _citing_rows(conn, target, params) for target in purge._EVIDENCE_COLUMNS}
    runs = _ids(conn, "SELECT id FROM ai_task_runs WHERE workspace_id = %(ws)s AND command->>'document_id' = %(doc)s "
                      "AND kind = ANY(%(kinds)s)", params)
    params["runs"] = list(runs)
    counts: Counter[str] = Counter()
    counts["ai_task_runs"] = len(runs | evidence["ai_task_runs"])
    counts["agent_runs"] = len(evidence["agent_runs"] | _ids(
        conn, "SELECT id FROM agent_runs WHERE workspace_id = %(ws)s AND document_id = %(doc)s "
              "AND action = 'markdown_turn' AND result IS NOT NULL", params))
    counts["pipeline_runs"] = len(evidence["pipeline_runs"] | _ids(
        conn, "SELECT id FROM pipeline_runs WHERE workspace_id = %(ws)s AND (document_id = %(doc)s OR id::text = ANY(%(runs)s)) "
              "AND manifest IS NOT NULL", params))
    counts["document_quality_reviews"] = len(evidence["document_quality_reviews"] | _ids(
        conn, "SELECT document_id AS id FROM document_quality_reviews WHERE document_id = %(doc)s", params))
    counts["wiki_pages"] = len(purge._source_page_ids(conn, workspace_id, document_id))
    for table in _DELETED_DOCUMENT_TABLES:
        counts[table] = conn.execute(f"SELECT count(*) AS count FROM {table} WHERE document_id = %(doc)s", params).fetchone()["count"]
    query = purge._journal_change_params(conn, params)
    changes: set[Any] = set()
    for clause in purge._JOURNAL_CHANGE_TARGETS:
        alias = clause.split()[2]
        changes |= _ids(conn, f"SELECT {alias}.id AS id {clause}", query)
    for row in conn.execute(
        """
        SELECT change.id, change.before_value::text AS before_value, change.after_value::text AS after_value
        FROM ai_task_changes change
        WHERE change.run_id = ANY(%(workspace_runs)s) AND change.table_name = ANY(%(tables)s)
          AND (change.before_value::text LIKE %(pattern)s OR change.after_value::text LIKE %(pattern)s)
        """,
        {**query, "tables": purge._EVIDENCE_JOURNAL_TABLES, "pattern": f"%{document_id}%"},
    ).fetchall():
        for value in (row["before_value"], row["after_value"]):
            if value is not None and purge._blank_source_text(json.loads(value), document_id) != json.loads(value):
                changes.add(row["id"])
    counts["ai_task_changes"] = len(changes)
    return counts


class _Objects:
    """S3 예상치. 클러스터·로그는 워크스페이스마다 한 번만 읽는다."""

    def __init__(self) -> None:
        self.storage = object_storage.client()
        self.bucket = object_storage.bucket_name()
        self.shared: dict[str, list[tuple[str, str]]] = {}

    def _keys(self, prefix: str) -> list[str]:
        return [item.object_name for item in self.storage.list_objects(self.bucket, prefix=prefix, recursive=True)]

    def _shared(self, workspace_id: str) -> list[tuple[str, str]]:
        if workspace_id not in self.shared:
            root = f"wiki/{workspace_id}/"
            keys = [f"{root}clusters/active.md", f"{root}clusters/archived.md", *self._keys(f"{root}logs/")]
            snapshots = {key: object_change_journal.snapshot(self.storage, self.bucket, key) for key in keys}
            self.shared[workspace_id] = [(key, value["text"]) for key, value in snapshots.items() if value is not None]
        return self.shared[workspace_id]

    def count(self, conn: Any, workspace_id: str, document_id: str) -> Counter[str]:
        counts: Counter[str] = Counter()
        artifact_ids = [row["artifact_id"] for row in purge._target_operations(conn, {"ws": workspace_id, "doc": document_id}) if row["artifact_id"]]
        for row in conn.execute(
            "SELECT object_key, preview_object_key FROM agent_run_artifacts WHERE workspace_id = %s AND (document_id = %s OR id = ANY(%s))",
            (workspace_id, document_id, artifact_ids),
        ).fetchall():
            counts["s3_artifact_objects"] += sum(bool(key) for key in (row["object_key"], row["preview_object_key"]))
        pages = purge._source_page_ids(conn, workspace_id, document_id)
        if pages:
            counts["s3_source_objects"] += len(self._keys(f"wiki/{workspace_id}/sources/{document_id}.md"))
            counts["s3_source_objects"] += sum(len(self._keys(f"wiki/{workspace_id}/pages/{page}/")) for page in pages)
        logs = f"wiki/{workspace_id}/logs/"
        for key, text in self._shared(workspace_id):
            strip = without_document_log if key.startswith(logs) else without_document_clusters
            counts["s3_shared_objects_to_rewrite"] += strip(text, document_id) != text
        return counts


def write_ids(path: str, targets: list[Target]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)  # 이미 있던 파일의 권한도 좁힌다.
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.writelines(f"{target.workspace_id}\t{target.document_id}\n" for target in targets)


def reconcile(
    document_ids: set[str],
    exported_at: datetime,
    *,
    min_age: timedelta = timedelta(hours=DEFAULT_MIN_AGE_HOURS),
    apply: bool = False,
    expected_count: int | None = None,
    ids_out: str | None = None,
    purge_document: Callable[[str, str], purge.DocumentCopyPurge] = purge.purge_document,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    age = now() - exported_at
    if age < timedelta(0) or age > MAX_EXPORT_AGE:
        raise SystemExit(f"--exported-at이 지금과 {age} 차이 납니다. {MAX_EXPORT_AGE} 안에 내보낸 목록을 쓰세요.")
    cutoff = exported_at - min_age
    existing = _normalized(document_ids)
    with database.connect_ai() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        _require_covering_list(conn, existing, cutoff)
        targets, skipped = find_targets(conn, existing, cutoff)
        untombstoned = _count_untombstoned(conn, existing, cutoff)
        estimated: Counter[str] = Counter()
        storage_errors = 0
        objects = None
        for target in targets:
            estimated.update(estimate_rows(conn, target.workspace_id, target.document_id))
            try:
                objects = objects or _Objects()
                estimated.update(objects.count(conn, target.workspace_id, target.document_id))
            except Exception:  # 예상치일 뿐이라 건수만 알린다. 오류 원문은 키를 담을 수 있다.
                storage_errors += 1
    if ids_out:
        write_ids(ids_out, targets)
    result: dict[str, object] = {
        "dry_run": not apply,
        "cutoff": cutoff.isoformat(),
        "existing_documents": len(document_ids),
        "targets": len(targets),
        "workspaces": len({target.workspace_id for target in targets}),
        "skipped": dict(sorted(skipped.items())),
        "missing_without_tombstone_not_targeted": untombstoned,
        "estimated": dict(sorted(estimated.items())),
        "storage_estimate_errors": storage_errors,
    }
    if not apply:
        return result
    if expected_count != len(targets):
        raise SystemExit(f"대상 {len(targets)}건이 --expected-count {expected_count}와 다릅니다. dry-run을 다시 확인하세요.")
    outcomes: Counter[str] = Counter()
    source_pages = 0
    for target in targets:
        outcome = purge_document(target.workspace_id, target.document_id)
        source_pages += len(outcome.source_page_ids)
        outcomes.update(_outcome_keys(outcome))
    # 끝내지 못한 문서는 ai_document_purges에 남아 agent worker가 다시 시도한다.
    result["outcomes"] = dict(sorted(outcomes.items()))
    result["source_pages"] = source_pages
    return result


def _outcome_keys(outcome: purge.DocumentCopyPurge) -> list[str]:
    if outcome.finished:
        return ["finished"]
    reasons = {
        "failed": outcome.failed,
        "waiting_active_runs": outcome.active_runs > 0,
        "pending_artifacts": outcome.pending_artifacts > 0,
        "pending_source_objects": outcome.pending_source_objects > 0,
        "pending_evidence": outcome.pending_evidence > 0,
        "pending_shared_objects": outcome.pending_shared_objects > 0,
    }
    return [key for key, value in reasons.items() if value]


def parse_exported_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("시간대(예: +09:00, Z)가 있는 ISO-8601 시각이 필요합니다.")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="core에 없는 문서의 AI 복사본을 확인하거나 지웁니다.")
    parser.add_argument("--document-ids-file", required=True, help="core에 현재 존재하는 문서 id(휴지통 포함), 한 줄에 하나")
    parser.add_argument("--exported-at", required=True, type=parse_exported_at, help="목록을 내보낸 시각(ISO-8601, 시간대 포함)")
    parser.add_argument("--min-documents", required=True, type=int, help="목록에 있어야 할 최소 건수. 이보다 적으면 거부합니다.")
    parser.add_argument("--min-age-hours", type=int, default=DEFAULT_MIN_AGE_HOURS,
                        help="휴지통 표시가 --exported-at보다 이 시간 넘게 앞선 문서만 대상으로 합니다.")
    parser.add_argument("--ids-out", help="대상 workspace_id·document_id를 쓸 파일(권한 600). 표준 출력에는 id를 쓰지 않습니다.")
    parser.add_argument("--apply", action="store_true", help="지정하면 실제 지웁니다. 생략하면 dry-run입니다.")
    parser.add_argument("--expected-count", type=int, help="--apply에 필수. dry-run의 targets와 같아야 합니다.")
    args = parser.parse_args()
    if args.apply and args.expected_count is None:
        parser.error("--apply에는 --expected-count가 필요합니다.")
    return args


def main() -> None:
    args = parse_args()
    result = reconcile(
        read_document_ids(args.document_ids_file, args.min_documents),
        args.exported_at,
        min_age=timedelta(hours=args.min_age_hours),
        apply=args.apply,
        expected_count=args.expected_count,
        ids_out=args.ids_out,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
