from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as database,
)
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_writer import (
    delete_source_related_links,
)


def cleanup_source_related_links(
    user_id: str,
    workspace_id: str,
    *,
    apply: bool = False,
    connect: Callable[[], Any] = database.connect,
) -> dict[str, object]:
    with connect() as conn:
        count = _count_source_related_links(
            conn,
            user_id,
            workspace_id,
        )
        if apply and count:
            delete_source_related_links(
                conn,
                user_id,
                workspace_id,
            )
    return {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "matched_count": count,
        "deleted_count": count if apply else 0,
        "dry_run": not apply,
    }


def _count_source_related_links(
    conn: Any,
    user_id: str,
    workspace_id: str,
) -> int:
    row = conn.execute(
        """
        SELECT count(*) AS matched_count
        FROM wiki_page_links l
        JOIN wiki_pages from_page ON from_page.id = l.from_page_id
        JOIN wiki_pages to_page ON to_page.id = l.to_page_id
        WHERE l.link_type = 'source_related_to'
          AND from_page.page_type = 'source'
          AND to_page.page_type = 'source'
          AND from_page.user_id = %s
          AND from_page.workspace_id = %s
          AND to_page.user_id = %s
          AND to_page.workspace_id = %s
        """,
        (user_id, workspace_id, user_id, workspace_id),
    ).fetchone()
    return int(row["matched_count"] if row else 0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Workspace의 legacy source_related_to link를 확인하거나 삭제합니다."
    )
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="지정하면 실제 삭제합니다. 생략하면 dry-run입니다.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = cleanup_source_related_links(
        args.user_id,
        args.workspace_id,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
