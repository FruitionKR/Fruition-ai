from __future__ import annotations

import hashlib
from typing import Any

from app.modules.mcp_wiki_draft.calculation import (
    PreviewCalculationError,
    _canonical_text,
    _change,
    _hash,
    _jsonable,
    _load_reconciled,
    _page_snapshot,
    _structural_rows,
)
from app.modules.wiki_ingestion.domain.concept_entry_lint import EntryPage, plan_entry_lint
from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import (
    load_concept_contributions,
    prune_dangling_entry_references,
    rebuild_concept_page,
    surviving_evidence_units,
)
from app.modules.wiki_ingestion.infrastructure.lint_operation_artifacts import lint_contribution
from app.modules.wiki_ingestion.infrastructure.object_storage import read_text_object


def _text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    return any(change[field] for field in (
        "claims", "added_links", "removed_links", "invalidated_evidence_ids", "entry_changes"
    )) or change["content_action"] != "none"


def _replay_page(
    page_id: str,
    draft_id: str,
    loaded: list[PageContribution],
    change: dict[str, Any],
    linked: list[dict[str, Any]],
):
    copies = [item for item in linked if str(item.get("page_id")) == page_id]
    if not _has_change(change) and not copies:
        return rebuild_concept_page(loaded), None
    contribution = lint_contribution(draft_id, change)
    sequence = max(item.sequence for item in loaded)
    additions = [
        PageContribution(page_id, "concept", str(item["operation_id"]), sequence + index, "", item)
        for index, item in enumerate(copies, start=1)
    ]
    additions.append(PageContribution(page_id, "concept", draft_id, sequence + len(additions) + 1, "", contribution))
    return rebuild_concept_page([*loaded, *additions]), contribution


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
    loaded_by_id: dict[str, list[PageContribution]] = {}
    current: dict[str, Any] = {}
    evidence_by_page: dict[str, dict[str, dict[str, Any]]] = {}
    for page_id in target_ids:
        kept = sorted(
            (
                item for item in ledger
                if str(item.get("page_id")) == page_id and item.get("active")
            ),
            key=lambda item: int(item.get("sequence_revision") or 0),
        )
        if not kept or any(not isinstance(item.get("sequence_revision"), int) or item["sequence_revision"] <= 0 for item in kept):
            raise PreviewCalculationError("active contribution ledger is missing or unresolved")
        loaded = load_concept_contributions(
            workspace_id=workspace_id,
            page_id=page_id,
            keep_contributions=[
                {"operation_id": item["ingest_operation_id"], "sequence": item["sequence_revision"]}
                for item in kept
            ],
            read_text=read_text_object,
        )
        rebuilt = rebuild_concept_page(loaded)
        if rebuilt.deleted or rebuilt.markdown != snapshots[page_id]["body"]:
            raise PreviewCalculationError("public page contradicts its contribution ledger")
        loaded_by_id[page_id] = loaded
        current[page_id] = rebuilt
        evidence_by_page[page_id] = surviving_evidence_units(loaded)

    changes_by_page = {page_id: _change_record(row_by_id[page_id]) for page_id in target_ids}
    links_by_page = {page_id: [dict(item) for item in current[page_id].supported_links] for page_id in target_ids}
    linked: list[dict[str, Any]] = []
    explicit_changes: list[dict[str, Any]] = []
    orphan_checks: set[str] = set()
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
                if kind == "REMOVE_STATEMENT" and _text_hash(str(unit.get("claim") or "")) != arguments["expected_text_hash"]:
                    raise PreviewCalculationError("statement text hash changed")
                change["invalidated_evidence_ids"].append(str(statement_id))
                evidence_by_page[page_id].pop(str(statement_id))
                explicit_changes.append(_change(
                    "CONCEPT_STATEMENT", "REMOVE" if kind == "REMOVE_STATEMENT" else "UPDATE",
                    page_id, str(statement_id), str(unit.get("claim") or ""),
                    None if kind == "REMOVE_STATEMENT" else str(arguments["text"]),
                    [] if kind == "REMOVE_STATEMENT" else list(arguments["evidence"]),
                ))
            if kind == "UPSERT_STATEMENT":
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
            present = link in links_by_page[page_id]
            if present != (kind == "REMOVE_LINK"):
                raise PreviewCalculationError("link precondition does not match public state")
            change["added_links" if kind == "ADD_LINK" else "removed_links"].append(link)
            if kind == "ADD_LINK":
                links_by_page[page_id].append(link)
            else:
                links_by_page[page_id].remove(link)
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
            for changed_id in target_ids:
                links_by_page[changed_id] = list(_replay_page(
                    changed_id, draft_id, loaded_by_id[changed_id], changes_by_page[changed_id], linked
                )[0].supported_links)
        else:
            if arguments["expected_content_hash"] != snapshots[page_id]["content_hash"]:
                raise PreviewCalculationError("orphan page content hash changed")
            orphan_checks.add(page_id)

    planned: dict[str, dict[str, Any]] = {}
    rebuilt: dict[str, Any] = dict(current)
    for page_id, change in changes_by_page.items():
        if not _has_change(change) and not any(str(item.get("page_id")) == page_id for item in linked):
            continue
        rebuilt[page_id], contribution = _replay_page(
            page_id, draft_id, loaded_by_id[page_id], change, linked
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
                page_id, draft_id, loaded_by_id[page_id], change, linked
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
    }
    publication_pages = [
        {
            "page_id": page_id, "slug": str(row_by_id[page_id]["slug"]),
            "title": rebuilt[page_id].title or row_by_id[page_id]["title"],
            "summary": rebuilt[page_id].summary or row_by_id[page_id].get("summary"),
            "page_type": "concept", "status": "deleted" if rebuilt[page_id].deleted else "active",
            "markdown": "" if rebuilt[page_id].deleted else rebuilt[page_id].markdown,
            "entries": _jsonable(rebuilt[page_id].entries),
            "supported_links": _jsonable(rebuilt[page_id].supported_links),
            "identity_aliases": _jsonable(rebuilt[page_id].identity_aliases),
            "source_document_ids": _jsonable(rebuilt[page_id].source_document_ids),
        }
        for page_id in sorted(changed_ids)
    ]
    contributions = [
        {"page_id": page_id, "contribution": _jsonable(item)} for page_id, item in sorted(planned.items())
    ] + [{"page_id": str(item["page_id"]), "contribution": _jsonable(item)} for item in linked]
    return {
        "preview": preview,
        "publication_plan": {
            "source_page": None, "pages": publication_pages, "contributions": contributions,
            "document_links": _jsonable(structures["document_links"]),
            "wiki_links": sorted(_jsonable(desired_links.values()), key=_canonical_text),
            "entry_references": sorted(_jsonable(desired_refs.values()), key=_canonical_text),
            "deleted_page_ids": sorted(page_id for page_id in changed_ids if rebuilt[page_id].deleted),
        },
        "normalized_submissions": operations,
        "calculation_inputs": {
            "policy_version": policy_version,
            "reconciled_ledger": sorted(_jsonable(ledger), key=lambda item: (str(item.get("page_id")), int(item.get("sequence_revision") or 0))),
            "public_state": public_state,
        },
    }
