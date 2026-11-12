from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from app.core.ai_markdown_sanitizer import external_urls, sanitize_ai_markdown
from app.modules.wiki_ingestion.domain.concept_entries import render_entry_index
from app.modules.mcp_wiki_draft.calculation import (
    LedgerContradictionError,
    PreviewCalculationError,
    _canonical_text,
    _change,
    _hash,
    _jsonable,
    _load_reconciled,
    _page_snapshot,
    _structural_rows,
)
from app.modules.mcp_wiki_draft.preview_summary import group_by_page, summary_item
from app.modules.wiki_ingestion.domain.concept_entry_lint import EntryLintPlan, EntryPage, plan_entry_lint
from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import (
    ConceptTitles,
    concept_title_lookup,
    load_concept_contributions,
    prune_dangling_entry_references,
    rebuild_concept_page,
    surviving_evidence_units,
)
from app.modules.wiki_ingestion.infrastructure.lint_operation_artifacts import lint_contribution
from app.modules.wiki_ingestion.infrastructure.concept_evidence import (
    append_concept_evidence,
    concept_evidence_updates,
)
from app.modules.wiki_ingestion.infrastructure.object_storage import read_text_object
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_ingestion_repository import (
    _page_link,
    _page_link_key,
)


def _text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def statement_text_hash(unit: dict[str, Any]) -> str:
    """REMOVE_STATEMENT의 `expected_text_hash` 기준. 남은 근거 단위의 claim 원문 그대로의 SHA-256이다."""
    return _text_hash(str(unit.get("claim") or ""))


def load_target_contributions(
    ledger: list[dict[str, Any]], workspace_id: str, page_id: str
) -> list[PageContribution]:
    """lint 대상 페이지의 활성 기여를 원장 순서대로 읽는다. lint 검증과 MCP 페이지 조회가 같이 쓴다."""
    kept = sorted(
        (
            item for item in ledger
            if str(item.get("page_id")) == page_id and item.get("active")
        ),
        key=lambda item: int(item.get("sequence_revision") or 0),
    )
    if not kept or any(not isinstance(item.get("sequence_revision"), int) or item["sequence_revision"] <= 0 for item in kept):
        raise PreviewCalculationError("active contribution ledger is missing or unresolved")
    return load_concept_contributions(
        workspace_id=workspace_id,
        page_id=page_id,
        keep_contributions=[
            {"operation_id": item["ingest_operation_id"], "sequence": item["sequence_revision"]}
            for item in kept
        ],
        read_text=read_text_object,
    )


def surviving_statements(conn, workspace_id: str, page_id: str) -> list[dict[str, str]]:
    """lint가 받는 문장 식별자. `statement_id`는 lint 검증의 `evidence_by_page` 키(전역 id) 그대로다."""
    ledger = _load_reconciled(conn, workspace_id, [page_id])
    units = surviving_evidence_units(load_target_contributions(ledger, workspace_id, page_id))
    return [
        {"statement_id": statement_id, "text": str(unit.get("claim") or ""), "text_hash": statement_text_hash(unit)}
        for statement_id, unit in units.items()
    ]


def _change_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "page_id": str(row["id"]),
        "slug": str(row["slug"]),
        "title": str(row["title"]),
        "content_action": "none",
        "claims": [],
        "added_links": [],
        "removed_links": [],
        "invalidated_evidence_ids": [],
        "entry_changes": [],
    }


def _merge_change(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    if incoming.get("content_action") in {"create", "append_evidence", "delete"}:
        target["content_action"] = incoming["content_action"]
    for field in (
        "claims", "added_links", "removed_links", "invalidated_evidence_ids", "entry_changes"
    ):
        for item in incoming.get(field, []):
            if item not in target[field]:
                target[field].append(item)
    for field in ("definition", "aliases", "split_from", "linked_operation_ids"):
        if incoming.get(field):
            target[field] = incoming[field]


def _entry_page(row: dict[str, Any], loaded, rebuilt) -> EntryPage:
    return EntryPage(
        page_id=str(row["id"]),
        slug=str(row["slug"]),
        title=str(row["title"]),
        contributions=tuple(dict(item.contribution or {}) for item in loaded),
        entries=tuple(dict(item) for item in rebuilt.entries),
        links=tuple(dict(item) for item in rebuilt.supported_links),
    )


def _has_change(change: dict[str, Any]) -> bool:
    return any(change.get(field) for field in (
        "claims", "added_links", "removed_links", "invalidated_evidence_ids", "entry_changes"
    )) or change["content_action"] != "none"


def _replay_page(
    page_id: str,
    draft_id: str,
    loaded: list[PageContribution],
    change: dict[str, Any],
    linked: list[dict[str, Any]],
    titles: ConceptTitles | None = None,
):
    copies = [item for item in linked if str(item.get("page_id")) == page_id]
    if not _has_change(change) and not copies:
        return rebuild_concept_page(loaded, concept_titles=titles), None
    contribution = lint_contribution(draft_id, change)
    sequence = max(item.sequence for item in loaded)
    additions = [
        PageContribution(page_id, "concept", str(item["operation_id"]), sequence + index, "", item)
        for index, item in enumerate(copies, start=1)
    ]
    additions.append(PageContribution(page_id, "concept", draft_id, sequence + len(additions) + 1, "", contribution))
    return rebuild_concept_page([*loaded, *additions], concept_titles=titles), contribution


def _is_republication(item: PageContribution) -> bool:
    """본문 전체를 새로 쓴 기여(수동 편집·lint·무효화·항목 이동/삭제). 그 뒤 편입은 이 본문에 근거를 덧붙인다."""
    contribution = item.contribution or {}
    return (
        contribution.get("artifact_type") in ("manual", "lint", "invalidation")
        or any(contribution.get(field) for field in ("invalidated_reference_ids", "invalidated_evidence_ids"))
        or any(change.get("op") != "add" for change in contribution.get("entry_changes", []))
    )


def _verified_publication_object(conn, page_id: str, item: PageContribution) -> str:
    """원장이 가리키는 기여의 저장된 게시 본문. 게시 기록(wiki_publication_objects)이 있으면 그 해시와 맞아야 한다.

    게시 기록이 생기기 전(2026-11-08 이전) 객체는 기록이 없다. 그때는 원장이 정한 키의 객체를 그대로 쓴다.
    """
    try:
        markdown = read_text_object(item.markdown_key)
    except Exception as exc:
        raise PreviewCalculationError("failed to read base contribution publication markdown") from exc
    recorded = conn.execute(
        "SELECT page_id, content_hash, deleted_at FROM wiki_publication_objects WHERE object_key=%s",
        (item.markdown_key,),
    ).fetchone()
    if recorded is not None and (
        str(recorded["page_id"]) != page_id
        or recorded["deleted_at"] is not None
        or str(recorded["content_hash"]) != _text_hash(markdown)
    ):
        raise LedgerContradictionError("base publication object contradicts its publication record")
    return markdown


def _freeze_page_moves(conn, workspace_id, pages, rows, busy, *, jev_allowed):
    """Plan only within the already validated target closure; persist effects with the draft."""
    from app.core.jev_client import JevUnavailable
    from app.modules.wiki_generation.infrastructure.jev_concept_resolver import (
        embedding_server_model, jev_concept_judge,
    )
    from app.modules.wiki_ingestion.infrastructure.postgres_wiki_ingestion_repository import (
        _part_of_heads_from_rows, _plan_page_moves,
    )

    # 크레딧이 없으면 Jev를 만들지 않고 이동 추론 없이 클라이언트 작업만 쓴다.
    judge = jev_concept_judge(None) if jev_allowed else None
    model = embedding_server_model() if judge is not None else None
    if judge is None or model is None:
        return {}
    plan = EntryLintPlan(page_changes={key: _change_record(rows[key]) for key in busy})
    entry_rows = [
        {**entry, "page_id": page.page_id, "title": page.title}
        for page in pages.values() for entry in page.entries
    ]

    def load(candidates):
        if any(str(row["id"]) not in pages for row in candidates):
            raise PreviewCalculationError("move cascade is outside the frozen target set")

    try:
        _plan_page_moves(
            conn, workspace_id, plan, pages, set(pages),
            {key: str(row.get("summary") or "") for key, row in rows.items()},
            load, judge.judge_part_of, model,
            candidate_heads=_part_of_heads_from_rows(list(rows.values()), entry_rows),
        )
    except JevUnavailable:
        logging.getLogger(__name__).info("Selected-target lint: Jev unavailable; no inferred moves")
        return {}
    deleted_refs = {pages[page_id].ref for page_id in plan.deleted_page_ids}
    for page in pages.values():
        if page.page_id in plan.deleted_page_ids:
            continue
        for link in page.links:
            if link.get("target") in deleted_refs and link.get("relation") != "entry_reference":
                change = plan.change(page.page_id, page.slug, page.title)
                if link not in change["removed_links"]:
                    change["removed_links"].append(link)
    return _jsonable({
        "page_changes": {key: value for key, value in plan.page_changes.items() if key not in busy or _has_change(value)},
        "linked": plan.linked, "decisions": plan.decisions,
        "deleted_page_ids": plan.deleted_page_ids,
    })


def calculate_lint_preview(
    conn,
    *,
    draft_id: str,
    workspace_id: str,
    target_snapshot: list[dict[str, Any]],
    source_snapshots: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    policy_version: str,
    max_structural_changes: int,
    max_deletes: int,
    infer_page_moves: bool = False,
    jev_allowed: bool = False,
    server_effects: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_ids = sorted(str(item["page_id"]) for item in target_snapshot)
    rows = conn.execute(
        "SELECT * FROM wiki_pages WHERE workspace_id=%s AND id=ANY(%s) ORDER BY id",
        (workspace_id, target_ids),
    ).fetchall()
    if len(rows) != len(target_ids) or any(row["page_type"] != "concept" or row["status"] != "active" for row in rows):
        raise PreviewCalculationError("lint target is missing or is not an active concept")
    row_by_id = {str(row["id"]): dict(row) for row in rows}
    snapshots = {item["page_id"]: item for item in (_page_snapshot(dict(row)) for row in rows)}
    expected = {str(item["page_id"]): item for item in target_snapshot}
    if any(
        snapshots[page_id]["version"] != int(expected[page_id]["version"])
        or snapshots[page_id]["content_hash"] != str(expected[page_id]["content_hash"])
        for page_id in target_ids
    ):
        raise PreviewCalculationError("lint target snapshot changed")

    slug_to_id = {str(row["slug"]): str(row["id"]) for row in rows}
    target_slugs = sorted(slug_to_id)
    structures = _structural_rows(conn, workspace_id, target_ids)
    linked_ids = {
        str(value)
        for item in structures["links"]
        # Source pages cannot be lint targets; keep their links in the frozen state.
        if item["link_type"] != "source_mentions_concept"
        for value in (item["from_page_id"], item["to_page_id"])
    }
    if linked_ids - set(target_ids) or any(
        str(item["target_slug"]) not in slug_to_id for item in structures["references"]
    ):
        raise PreviewCalculationError("target snapshot omits a linked identity")
    outside = conn.execute(
        "SELECT DISTINCT page.id FROM wiki_page_entry_references reference "
        "JOIN wiki_pages page ON page.id=reference.page_id "
        "WHERE reference.workspace_id=%s AND reference.target_slug=ANY(%s) "
        "AND page.status='active' AND NOT (page.id=ANY(%s))",
        (workspace_id, target_slugs, target_ids),
    ).fetchall()
    if outside:
        raise PreviewCalculationError("target snapshot omits an inbound reference cascade")

    ledger = _load_reconciled(conn, workspace_id, target_ids)
    titles = concept_title_lookup(conn, workspace_id)
    loaded_by_id: dict[str, list[PageContribution]] = {}
    current: dict[str, Any] = {}
    evidence_by_page: dict[str, dict[str, dict[str, Any]]] = {}
    for page_id in target_ids:
        loaded = load_target_contributions(ledger, workspace_id, page_id)
        rebuilt = rebuild_concept_page(loaded, concept_titles=titles)
        if rebuilt.deleted:
            raise LedgerContradictionError("public page contradicts its contribution ledger")
        if rebuilt.markdown != snapshots[page_id]["body"]:
            # Ingest appends to the last publication; manual/lint/invalidation establish a new base.
            if any((item.contribution or {}).get("artifact_type") not in (
                None, "ingest", "document", "manual", "lint", "invalidation",
            ) for item in loaded):
                raise LedgerContradictionError("public page contradicts its contribution ledger")
            base_index = max(
                (index for index, item in enumerate(loaded) if _is_republication(item)), default=0,
            )
            if _is_republication(loaded[base_index]):
                # 수동·lint·무효화 기여가 새로 쓴 본문은 그때의 재생 결과이고, 재생 렌더러는 바뀔 수 있다(정식 형태는
                # 지금의 재생 결과다). 그래서 다시 그려 비교하지 않고 원장이 가리키는 그 기여의 저장 본문을 기준으로 쓴다.
                # 그 뒤 기여의 덧붙임이 공개 본문을 그대로 재현하는지, 마지막 기여의 게시 본문이 공개 본문인지는 아래에서 본다.
                appended = _verified_publication_object(conn, page_id, loaded[base_index])
            else:
                baseline = rebuild_concept_page(loaded[:base_index + 1], concept_titles=titles).markdown
                initial = (loaded[0].contribution or {}).get("initial_markdown")
                try:
                    appended = read_text_object(loaded[base_index].markdown_key)
                except Exception as exc:
                    raise PreviewCalculationError("failed to read base contribution publication markdown") from exc
                # Legacy skeleton publications lack the recorded initial presentation (including sibling context),
                # and replay now draws Related Concepts from surviving links, so that section is not compared.
                related_section = r"(?ms)^## Related Concepts\n.*?(?=^## |\Z)"
                matches = appended == (initial if initial is not None else baseline)
                if not matches and initial is None:
                    matches = (
                        re.sub(related_section, "", appended, count=1)
                        == re.sub(related_section, "", baseline, count=1)
                    )
                if not matches:
                    raise LedgerContradictionError("initial publication contradicts its contribution ledger")
            for index, item in enumerate(loaded[base_index + 1:], start=base_index + 1):
                contribution = item.contribution or {}
                appended = sanitize_ai_markdown(
                    append_concept_evidence(appended, concept_evidence_updates(contribution)),
                    keep_urls=external_urls(appended),
                )
                if contribution.get("entry_changes") or any(
                    link.get("relation") == "entry_reference" for link in contribution.get("links", [])
                ):
                    state = rebuild_concept_page(loaded[:index + 1])
                    appended = render_entry_index(appended, list(state.entries), list(state.supported_links))
            if appended != snapshots[page_id]["body"]:
                raise LedgerContradictionError("public page contradicts its contribution ledger")
            try:
                published = read_text_object(loaded[-1].markdown_key)
            except Exception as exc:
                raise PreviewCalculationError("failed to read contribution publication markdown") from exc
            if published != snapshots[page_id]["body"]:
                raise LedgerContradictionError("public page contradicts its contribution ledger")
        loaded_by_id[page_id] = loaded
        current[page_id] = rebuilt
        evidence_by_page[page_id] = surviving_evidence_units(loaded)

    changes_by_page = {page_id: _change_record(row_by_id[page_id]) for page_id in target_ids}
    links_by_page = {page_id: [dict(item) for item in current[page_id].supported_links] for page_id in target_ids}
    linked: list[dict[str, Any]] = []
    explicit_changes: list[dict[str, Any]] = []
    orphan_checks: set[str] = set()
    summary: list[tuple[str, dict[str, Any]]] = []

    def title(page_id: str) -> str:
        return str(row_by_id[page_id]["title"])

    source_by_snapshot = {str(item["snapshot_id"]): item for item in source_snapshots}
    for operation in operations:
        page_id = str(operation["target_page_id"])
        arguments = operation["arguments"]
        change = changes_by_page[page_id]
        kind = operation["kind"]
        if kind in {"UPSERT_STATEMENT", "REMOVE_STATEMENT"}:
            statement_id = arguments.get("statement_id")
            if statement_id is not None:
                unit = evidence_by_page[page_id].get(str(statement_id))
                if unit is None:
                    raise PreviewCalculationError("statement identity is not a surviving target statement")
                if kind == "REMOVE_STATEMENT" and statement_text_hash(unit) != arguments["expected_text_hash"]:
                    raise PreviewCalculationError("statement text hash changed")
                change["invalidated_evidence_ids"].append(str(statement_id))
                evidence_by_page[page_id].pop(str(statement_id))
                explicit_changes.append(_change(
                    "CONCEPT_STATEMENT", "REMOVE" if kind == "REMOVE_STATEMENT" else "UPDATE",
                    page_id, str(statement_id), str(unit.get("claim") or ""),
                    None if kind == "REMOVE_STATEMENT" else str(arguments["text"]),
                    [] if kind == "REMOVE_STATEMENT" else list(arguments["evidence"]),
                ))
                summary.append((page_id, summary_item(
                    "STATEMENT_REMOVE", page_id, title(page_id), text=unit.get("claim"),
                ) if kind == "REMOVE_STATEMENT" else summary_item(
                    "STATEMENT_UPDATE", page_id, title(page_id), text=arguments["text"],
                    before_text=unit.get("claim"), evidence=list(arguments["evidence"]),
                )))
            if kind == "UPSERT_STATEMENT":
                if change["content_action"] == "none":
                    change["content_action"] = "append_evidence"
                # block_id는 document가 웹 분할·source_blocks 레지스트리로 받은 영구 ID다. 마지막 편입 뒤에 새로 생긴
                # 블록은 다음 번호를 잠정으로 받는다(그 사이 다른 편입이 없으면 다음 편입이 같은 번호를 준다).
                refs = [
                    f"{source_by_snapshot[item['snapshot_id']]['document_id']}:{item['block_id']}"
                    for item in arguments["evidence"]
                ]
                local_id = str(operation["operation_id"])
                change["claims"].append({"id": local_id, "claim": arguments["text"], "refs": refs})
                global_id = f"{draft_id}:{local_id}"
                evidence_by_page[page_id][global_id] = {
                    "evidence_id": global_id, "claim": arguments["text"], "anchor_reference_ids": refs
                }
                if statement_id is None:
                    explicit_changes.append(_change(
                        "CONCEPT_STATEMENT", "ADD", page_id, global_id, None,
                        str(arguments["text"]), list(arguments["evidence"]),
                    ))
                    summary.append((page_id, summary_item(
                        "STATEMENT_ADD", page_id, title(page_id), text=arguments["text"],
                        evidence=list(arguments["evidence"]),
                    )))
        elif kind in {"ADD_LINK", "REMOVE_LINK"}:
            source_slug = str(arguments["source_ref"]).removeprefix("concept:")
            target_slug = str(arguments["target_ref"]).removeprefix("concept:")
            if slug_to_id.get(source_slug) != page_id or target_slug not in slug_to_id:
                raise PreviewCalculationError("link reference is outside the frozen target set")
            link = {
                "source": arguments["source_ref"],
                "target": arguments["target_ref"],
                "relation": arguments["relation"],
            }
            # 웹 편입 링크는 confidence·decided_by·entry_id 같은 키를 더 싣는다. 재생·DB 간선과 같이 (source, target,
            # relation)으로 비교한다. 이미 있는 키의 ADD_LINK는 중복이라 막고, REMOVE_LINK는 그 키의 링크를 모두 지운다
            # (entry_id 없는 삭제 기록은 재생에서도 그 키를 모두 지운다).
            key = _page_link_key(link)
            present = any(_page_link_key(item) == key for item in links_by_page[page_id])
            if present != (kind == "REMOVE_LINK"):
                raise PreviewCalculationError("link precondition does not match public state")
            change["added_links" if kind == "ADD_LINK" else "removed_links"].append(link)
            summary.append((page_id, summary_item(
                "LINK_ADD" if kind == "ADD_LINK" else "LINK_REMOVE", page_id, title(page_id),
                related_page_title=title(slug_to_id[target_slug]),
            )))
            if kind == "ADD_LINK":
                links_by_page[page_id].append(link)
            else:
                links_by_page[page_id] = [item for item in links_by_page[page_id] if _page_link_key(item) != key]
        elif kind == "MERGE_CONCEPTS":
            intermediate = {
                candidate: _replay_page(
                    candidate, draft_id, loaded_by_id[candidate], changes_by_page[candidate], linked
                )[0]
                for candidate in target_ids
            }
            pages = {
                candidate: _entry_page(row_by_id[candidate], loaded_by_id[candidate], intermediate[candidate])
                for candidate in target_ids
            }
            sources = set(str(item) for item in arguments["source_page_ids"])
            plan = plan_entry_lint(pages, sources, lambda _page, _entry: None)
            if set(plan.deleted_page_ids) != sources or any(
                item.get("action") != "merge_back" or item.get("skipped_reason")
                for item in plan.decisions if item.get("page_id") in sources and item.get("entry_id") is None
            ):
                raise PreviewCalculationError("merge does not satisfy the closed entry identity rule")
            destination = str(arguments["destination_page_id"])
            if destination not in plan.page_changes or any(
                str((pages[source].split_record or {}).get("split_from", {}).get("page_id")) != destination
                for source in sources
            ):
                raise PreviewCalculationError("merge destination does not match the recorded split parent")
            for changed_id, planned in plan.page_changes.items():
                if changed_id not in changes_by_page:
                    raise PreviewCalculationError("merge cascade is outside the frozen target set")
                _merge_change(changes_by_page[changed_id], planned)
            linked.extend(dict(item) for item in plan.linked)
            summary.extend(
                (str(source), summary_item(
                    "CONCEPT_MERGE", str(source), title(str(source)), related_page_title=title(destination),
                ))
                for source in arguments["source_page_ids"]
            )
            for changed_id in target_ids:
                links_by_page[changed_id] = list(_replay_page(
                    changed_id, draft_id, loaded_by_id[changed_id], changes_by_page[changed_id], linked
                )[0].supported_links)
        else:
            if arguments["expected_content_hash"] != snapshots[page_id]["content_hash"]:
                raise PreviewCalculationError("orphan page content hash changed")
            orphan_checks.add(page_id)
            summary.append((page_id, summary_item("CONCEPT_DELETE", page_id, title(page_id))))

    if infer_page_moves:
        pages = {
            page_id: _entry_page(
                row_by_id[page_id], loaded_by_id[page_id],
                _replay_page(page_id, draft_id, loaded_by_id[page_id], changes_by_page[page_id], linked)[0],
            )
            for page_id in target_ids
        }
        server_effects = _freeze_page_moves(
            conn, workspace_id, pages, row_by_id,
            {key for key, value in changes_by_page.items() if _has_change(value)},
            jev_allowed=jev_allowed,
        )
    server_effects = server_effects or {}
    for page_id, change in server_effects.get("page_changes", {}).items():
        if page_id not in changes_by_page or change["slug"] != row_by_id[page_id]["slug"]:
            raise PreviewCalculationError("server move target snapshot changed")
        _merge_change(changes_by_page[page_id], change)
    for contribution in server_effects.get("linked", []):
        if str(contribution["page_id"]) not in changes_by_page:
            raise PreviewCalculationError("server move contribution is outside the frozen target set")
        linked.append(dict(contribution))
    # 서버가 추론한 페이지 이동은 그 페이지를 지우고 대상 개념의 항목으로 옮긴다. 사용자에게는 개념 합치기와 같다.
    moved = set(server_effects.get("deleted_page_ids", []))
    for decision in server_effects.get("decisions", []):
        if (
            decision.get("action") == "move" and not decision.get("skipped_reason")
            and str(decision.get("page_id")) in moved & set(row_by_id) and str(decision.get("target_page_id")) in row_by_id
        ):
            moved_id = str(decision["page_id"])
            summary.append((moved_id, summary_item(
                "CONCEPT_MERGE", moved_id, title(moved_id),
                related_page_title=title(str(decision["target_page_id"])),
            )))

    planned: dict[str, dict[str, Any]] = {}
    rebuilt: dict[str, Any] = dict(current)
    for page_id, change in changes_by_page.items():
        if not _has_change(change) and not any(str(item.get("page_id")) == page_id for item in linked):
            continue
        rebuilt[page_id], contribution = _replay_page(
            page_id, draft_id, loaded_by_id[page_id], change, linked, titles
        )
        if contribution is not None:
            planned[page_id] = contribution

    live_entries = {
        (str(row_by_id[page_id]["slug"]), str(entry["entry_id"]))
        for page_id, page in rebuilt.items() if not page.deleted for entry in page.entries
    }
    for page_id, page in list(rebuilt.items()):
        if page.deleted:
            continue
        _markdown, _kept, removed = prune_dangling_entry_references(
            page.markdown, page.entries, page.supported_links, live_entries
        )
        if removed:
            change = changes_by_page[page_id]
            for link in removed:
                if link not in change["removed_links"]:
                    change["removed_links"].append(link)
            contribution = lint_contribution(draft_id, change)
            planned[page_id] = contribution
            rebuilt[page_id] = _replay_page(
                page_id, draft_id, loaded_by_id[page_id], change, linked, titles
            )[0]

    desired_entries = {
        (str(item["page_id"]), str(item["entry_id"])): item for item in structures["entries"]
    }
    desired_links = {
        (str(item["from_page_id"]), str(item["link_type"]), str(item["to_page_id"])): item
        for item in structures["links"]
    }
    desired_refs = {
        (str(item["page_id"]), str(item["target_slug"]), str(item["entry_id"]), str(item.get("parent_entry_id") or "")): item
        for item in structures["references"]
    }
    changed_ids = set(planned)
    for page_id in changed_ids:
        page = rebuilt[page_id]
        desired_entries = {key: value for key, value in desired_entries.items() if key[0] != page_id}
        desired_links = {key: value for key, value in desired_links.items() if key[0] != page_id}
        desired_refs = {key: value for key, value in desired_refs.items() if key[0] != page_id}
        if page.deleted:
            continue
        for entry in page.entries:
            desired_entries[(page_id, str(entry["entry_id"]))] = {"page_id": page_id, **dict(entry)}
        for link in page.supported_links:
            if link.get("relation") == "entry_reference":
                key = (page_id, str(link["target"]).removeprefix("concept:"), str(link.get("entry_id") or ""), str(link.get("parent_entry_id") or ""))
                desired_refs[key] = {
                    "page_id": page_id, "target_slug": key[1], "entry_id": key[2],
                    "parent_entry_id": link.get("parent_entry_id"), "name": str(link.get("name") or ""),
                }
            else:
                source_id = slug_to_id.get(str(link.get("source") or "").removeprefix("concept:"))
                target_id = slug_to_id.get(str(link.get("target") or "").removeprefix("concept:"))
                if source_id and target_id:
                    desired_links[(source_id, str(link["relation"]), target_id)] = {
                        "from_page_id": source_id, "to_page_id": target_id,
                        "link_type": str(link["relation"]), "label": None, "confidence": None,
                    }

    for page_id in orphan_checks:
        slug = str(row_by_id[page_id]["slug"])
        has_inbound = any(key[2] == page_id for key in desired_links) or any(
            key[1] == slug and key[0] != page_id for key in desired_refs
        )
        if not rebuilt[page_id].deleted or has_inbound:
            raise PreviewCalculationError("page still has surviving evidence, contribution, or inbound reference")

    changes = list(explicit_changes)
    for page_id in sorted(changed_ids):
        before, after = snapshots[page_id]["body"], rebuilt[page_id]
        if after.deleted:
            changes.append(_change("PAGE_DELETE", "DELETE", page_id, page_id, before, None))
        elif before != after.markdown:
            changes.append(_change("PAGE_BODY", "UPDATE", page_id, page_id, before, after.markdown))
    for kind, current_rows, desired_rows in (
        ("ENTRY", {(str(item["page_id"]), str(item["entry_id"])): item for item in structures["entries"]}, desired_entries),
        ("LINK", {(str(item["from_page_id"]), str(item["link_type"]), str(item["to_page_id"])): item for item in structures["links"]}, desired_links),
        ("REFERENCE", {(str(item["page_id"]), str(item["target_slug"]), str(item["entry_id"]), str(item.get("parent_entry_id") or "")): item for item in structures["references"]}, desired_refs),
    ):
        for key in sorted(set(current_rows) | set(desired_rows)):
            before, after = current_rows.get(key), desired_rows.get(key)
            if before == after:
                continue
            action = "ADD" if before is None else "REMOVE" if after is None else "UPDATE"
            changes.append(_change(kind, action, key[0], ":".join(key[1:]), _canonical_text(before) if before else None, _canonical_text(after) if after else None))
    changes = sorted({item["change_id"]: item for item in changes}.values(), key=lambda item: item["change_id"])
    deletes = sum(1 for page_id in changed_ids if rebuilt[page_id].deleted)
    structural = sum(1 for item in changes if item["kind"] in {"ENTRY", "LINK", "REFERENCE", "PAGE_DELETE"})
    if deletes > max_deletes or structural > max_structural_changes:
        raise PreviewCalculationError("complete lint preview exceeds structural bounds")

    public_state = {
        "pages": [
            {key: snapshots[page_id][key] for key in ("page_id", "page_type", "slug", "status", "version", "content_hash", "markdown_uri")}
            for page_id in target_ids
        ],
        **_jsonable(structures),
    }
    public_state_hash = _hash(public_state)
    frozen_targets = sorted(
        ({"page_id": page_id, "version": snapshots[page_id]["version"], "content_hash": snapshots[page_id]["content_hash"]} for page_id in target_ids),
        key=lambda item: item["page_id"],
    )
    frozen_sources = sorted(
        ({key: item[key] for key in ("snapshot_id", "document_id", "source_version", "source_content_hash")} for item in source_snapshots),
        key=lambda item: item["snapshot_id"],
    )
    draft_hash = _hash({
        "draft_kind": "LINT", "source_snapshots": frozen_sources, "target_pages": frozen_targets,
        "operations": operations, "changes": changes, "public_state_hash": public_state_hash,
        "policy_version": policy_version,
    })
    preview = {
        "schema_version": "mcp.wiki-preview.v1", "draft_id": draft_id, "draft_kind": "LINT",
        "draft_version": 1, "draft_hash": draft_hash, "source_snapshots": frozen_sources,
        "target_pages": frozen_targets, "public_state_hash": public_state_hash,
        "changes": changes, "complete": True,
        # 승인 화면용 사람이 읽는 요약. 해시에 묶인 작업·공개 상태에서 파생하므로 draft_hash 입력이 아니다.
        "summary": group_by_page(summary),
    }
    # 웹 lint 발행(`persist_lint_operation_result`)의 입력 모양이다. 페이지 변경 기록과 재생 결과 상태를 그대로 넘긴다.
    page_changes = [
        {
            **changes_by_page[page_id],
            # 지우는 페이지의 기록 본문은 지우기 직전 본문이다(웹 lint와 같다).
            "markdown": snapshots[page_id]["body"] if rebuilt[page_id].deleted else rebuilt[page_id].markdown,
        }
        for page_id in sorted(changed_ids)
    ]
    state_pages = []
    link_changes: dict[str, list[dict[str, Any]]] = {"removed_links": [], "restored_links": []}
    for page_id in sorted(changed_ids):
        page = rebuilt[page_id]
        if page.deleted:
            continue
        state_pages.append({
            "page_id": page_id,
            "markdown": page.markdown,
            "title": None,
            "summary": None,
            "source_document_ids": _jsonable(list(page.source_document_ids)),
            "identity_aliases": _jsonable(list(page.identity_aliases)),
            "entries": _jsonable(list(page.entries)),
            "supported_links": _jsonable(list(page.supported_links)),
        })
        # DB 간선은 페이지 쌍 단위다. 재생한 링크의 페이지 쌍이 바뀐 것만 반영한다(웹 lint와 같다).
        old_keys = {_page_link_key(link) for link in current[page_id].supported_links}
        new_keys = {_page_link_key(link) for link in page.supported_links}
        link_changes["removed_links"].extend(_page_link(key) for key in sorted(old_keys - new_keys))
        link_changes["restored_links"].extend(_page_link(key) for key in sorted(new_keys - old_keys))
    return {
        "preview": preview,
        "publication_plan": {
            "page_changes": _jsonable(page_changes),
            "state_pages": state_pages,
            "link_changes": link_changes,
            "linked": _jsonable(linked),
            "deleted_page_ids": sorted(page_id for page_id in changed_ids if rebuilt[page_id].deleted),
        },
        "normalized_submissions": operations,
        "calculation_inputs": {
            "policy_version": policy_version,
            "server_effects": server_effects,
            "reconciled_ledger": sorted(_jsonable(ledger), key=lambda item: (str(item.get("page_id")), int(item.get("sequence_revision") or 0))),
            "public_state": public_state,
        },
    }
