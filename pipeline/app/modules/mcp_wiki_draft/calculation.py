from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from app.modules.wiki_generation.domain.concept_identity import (
    available_concept_slug,
    concept_name_keys,
)
from app.modules.wiki_generation.domain.text_utils import name_key, slugify
from app.modules.wiki_generation.infrastructure.assemble import SourcePageAssembler
from app.modules.wiki_ingestion.application.concept_contribution import (
    build_concept_contributions,
)
from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution
from app.modules.wiki_ingestion.infrastructure.backend_document_reader import (
    read_contributions,
)
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import (
    INVALIDATION,
    prune_dangling_entry_references,
    rebuild_active_concept_page_from_ledger,
    replay_concept_page_with_operation,
    surviving_evidence_units,
)
from app.modules.wiki_ingestion.infrastructure.object_storage import read_text_object
from app.modules.wiki_ingestion.infrastructure.pending_contributions import (
    with_pending_contributions,
)
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_writer import (
    load_concept_identity_index,
    read_optional_text_object,
)


class PreviewCalculationError(RuntimeError):
    pass


class SemanticMergeError(PreviewCalculationError):
    pass


def _hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _stable_page_id(draft_id: str, key: str) -> str:
    return f"wiki_page_{uuid5(UUID(draft_id), key)}"


def _version(value: datetime) -> int:
    return max(1, int(value.timestamp() * 1_000_000))


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _body(row: dict[str, Any]) -> str:
    uri = row.get("markdown_uri")
    if not uri:
        return ""
    try:
        return read_optional_text_object(str(uri))
    except Exception as exc:
        raise PreviewCalculationError(f"failed to read committed wiki body: {row['id']}") from exc


def _page_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    body = _body(row)
    return {
        "page_id": str(row["id"]),
        "version": _version(row["updated_at"]),
        "content_hash": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "markdown_uri": row.get("markdown_uri"),
        "status": row["status"],
        "page_type": row["page_type"],
        "slug": row["slug"],
        "title": row["title"],
        "body": body,
    }


def _allocate_identities(
    conn,
    *,
    draft_id: str,
    workspace_id: str,
    concepts: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    index = load_concept_identity_index(conn, workspace_id, include_deleted=True)
    occupied = {str(item["slug"]) for item in index}
    allocated: dict[str, dict[str, str]] = {}
    for concept in sorted(concepts, key=lambda item: (name_key(item["concept_name"]), item["concept_name"])):
        merge_target = concept.get("merge_target_page_id")
        if merge_target is not None:
            matches = [item for item in index if str(item["id"]) == merge_target and item["status"] == "active"]
            if len(matches) != 1:
                raise SemanticMergeError("semantic merge target is no longer an active workspace concept")
            target = matches[0]
            allocated[name_key(concept["concept_name"])] = {
                "page_id": str(target["id"]), "slug": str(target["slug"]),
                "prior_status": "active", "title": str(target["title"]),
            }
            continue
        incoming = {"title": concept["concept_name"], "identity_aliases": []}
        active = [
            item for item in index
            if item["status"] == "active" and concept_name_keys(incoming) & concept_name_keys(item)
        ]
        deleted = [
            item for item in index
            if item["status"] == "deleted" and concept_name_keys(incoming) & concept_name_keys(item)
        ]
        if len(active) == 1:
            page_id, slug, prior = str(active[0]["id"]), str(active[0]["slug"]), "active"
        elif not active and len(deleted) == 1:
            page_id, slug, prior = str(deleted[0]["id"]), str(deleted[0]["slug"]), "deleted"
        else:
            slug = available_concept_slug(slugify(concept["concept_name"]), occupied)
            page_id = _stable_page_id(draft_id, f"concept:{name_key(concept['concept_name'])}:{slug}")
            prior = "new"
        occupied.add(slug)
        allocated[name_key(concept["concept_name"])] = {
            "page_id": page_id,
            "slug": slug,
            "prior_status": prior,
            "title": concept["concept_name"],
        }
    return allocated, index


def _combined_concepts(submissions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    combined: dict[str, dict[str, Any]] = {}
    for submission in submissions:
        window = str(submission["window_ordinal"])
        for concept in submission["contributions"]:
            key = name_key(concept["concept_name"])
            target = combined.setdefault(key, {
                "concept_name": concept["concept_name"], "statements": [], "links": [],
            })
            merge_target = concept.get("merge_target_page_id")
            if merge_target is not None:
                if target.get("merge_target_page_id", merge_target) != merge_target:
                    raise SemanticMergeError("conflicting semantic merge targets for the same concept")
                target["merge_target_page_id"] = merge_target
            statement_ids = {
                item["statement_id"]: f"{window}:{item['statement_id']}"
                for item in concept["statements"]
            }
            target["statements"].extend(
                {**item, "statement_id": statement_ids[item["statement_id"]]}
                for item in concept["statements"]
            )
            target["links"].extend(
                {**item, "source_statement_id": statement_ids[item["source_statement_id"]]}
                for item in concept["links"]
            )
    return list(combined.values())


def _normalized(
    run: dict[str, Any],
    concepts: list[dict[str, Any]],
    allocated: dict[str, dict[str, str]],
    identity_index: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document_id = str(run["document_id"])
    evidence_units: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    source_key_points: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    existing_by_name: dict[str, list[dict[str, Any]]] = {}
    for item in identity_index:
        if item["status"] == "active":
            for key in concept_name_keys(item):
                existing_by_name.setdefault(key, []).append(item)
    for concept in concepts:
        identity = allocated[name_key(concept["concept_name"])]
        refs: list[str] = []
        evidence_ids: list[str] = []
        for statement in concept["statements"]:
            evidence_ids.append(statement["statement_id"])
            statement_refs = list(dict.fromkeys(item["block_id"] for item in statement["evidence"]))
            refs.extend(statement_refs)
            evidence_units.append({
                "evidence_id": statement["statement_id"],
                "claim": statement["text"],
                "anchor_reference_ids": statement_refs,
                "related_concept_slugs": [identity["slug"]],
                "source_document_id": document_id,
            })
            source_key_points.append({"text": statement["text"], "anchor_reference_ids": statement_refs})
        unique_refs = list(dict.fromkeys(refs))
        ledger.append({
            "title": identity["title"],
            "slug": identity["slug"],
            "definition": concept["statements"][0]["text"],
            "why_page_worthy": "",
            "aliases": [],
            "identity_aliases": [],
            "tags": [],
            "mention_count": len(unique_refs),
            "importance_score": 1.0,
            "anchor_reference_ids": unique_refs,
            "display_reference_ids": unique_refs,
            "mention_reference_ids": [],
            "evidence_claim_ids": evidence_ids,
            "source_document_ids": [document_id],
        })
        for link in concept["links"]:
            target_key = name_key(link["target_concept_name"])
            target = allocated.get(target_key)
            if target is None:
                matches = existing_by_name.get(target_key, [])
                if len(matches) != 1:
                    raise PreviewCalculationError("link target concept identity is unresolved or ambiguous")
                target = {"slug": str(matches[0]["slug"])}
            links.append({
                "source": f"concept:{identity['slug']}",
                "target": f"concept:{target['slug']}",
                "relation": link["relation"],
            })
    normalized = {
        "document": {
            "document_id": document_id,
            "source_path": document_id,
            "title": document_id,
        },
        "semantic_notes": [{"key_points": source_key_points}],
        "concept_ledger": ledger,
        "evidence_units": evidence_units,
        "source_key_points": source_key_points,
        "categories": [],
        "section_candidates": [],
        "mentions": [],
        "observations": [],
    }
    return normalized, links


def _load_reconciled(conn, workspace_id: str, page_ids: list[str]) -> list[dict[str, Any]]:
    if not page_ids:
        return []
    try:
        remote = read_contributions(page_ids, workspace_id)
        if any(str(item.get("page_id")) not in page_ids for item in remote):
            raise PreviewCalculationError("remote contribution ledger returned an unexpected page")
        return with_pending_contributions(
            conn, remote, page_ids, cleanup_resolved=False
        )
    except PreviewCalculationError:
        raise
    except Exception as exc:
        raise PreviewCalculationError("contribution ledger could not be reconciled") from exc


def _change(
    kind: str,
    action: str,
    page_id: str,
    subject_id: str,
    before: str | None,
    after: str | None,
    evidence: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    value = {
        "kind": kind,
        "action": action,
        "page_id": page_id,
        "subject_id": subject_id,
        "before_text": before,
        "after_text": after,
        "evidence": evidence or [],
    }
    return {"change_id": _hash(value), **value}


def _canonical_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _removed_statement_changes(
    page_id: str, loaded: list[PageContribution], contribution: dict[str, Any]
) -> list[dict[str, Any]]:
    before = surviving_evidence_units(loaded)
    appended = PageContribution(
        page_id, "concept", str(contribution["operation_id"]),
        max((item.sequence for item in loaded), default=0) + 1, "", contribution,
    )
    after = surviving_evidence_units([*loaded, appended])
    return [
        _change("CONCEPT_STATEMENT", "REMOVE", page_id, evidence_id,
                str(before[evidence_id]["claim"]), None)
        for evidence_id in sorted(before.keys() - after.keys())
    ]


def _structural_rows(conn, workspace_id: str, page_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not page_ids:
        return {"entries": [], "links": [], "references": [], "manual_links": [], "document_links": []}
    return {
        "entries": [dict(row) for row in conn.execute(
            "SELECT entry_id,page_id,name,parent_entry_id,definition,block_refs,slugs,aliases,tags "
            "FROM wiki_page_entries WHERE workspace_id=%s AND page_id=ANY(%s) ORDER BY page_id,entry_id",
            (workspace_id, page_ids),
        ).fetchall()],
        "links": [dict(row) for row in conn.execute(
            "SELECT link.from_page_id,link.to_page_id,link.link_type,link.label,link.confidence "
            "FROM wiki_page_links link "
            "WHERE link.workspace_id=%s AND (link.from_page_id=ANY(%s) OR link.to_page_id=ANY(%s)) "
            "ORDER BY link.from_page_id,link.link_type,link.to_page_id",
            (workspace_id, page_ids, page_ids),
        ).fetchall()],
        "references": [dict(row) for row in conn.execute(
            "SELECT page_id,entry_id,target_slug,name,parent_entry_id FROM wiki_page_entry_references "
            "WHERE workspace_id=%s AND page_id=ANY(%s) ORDER BY page_id,position",
            (workspace_id, page_ids),
        ).fetchall()],
        "manual_links": [dict(row) for row in conn.execute(
            "SELECT from_page_id,to_page_id,present FROM wiki_source_manual_links "
            "WHERE workspace_id=%s AND (from_page_id=ANY(%s) OR to_page_id=ANY(%s)) ORDER BY from_page_id,to_page_id",
            (workspace_id, page_ids, page_ids),
        ).fetchall()],
        "document_links": [dict(row) for row in conn.execute(
            "SELECT document_id,wiki_page_id,relation_type,confidence FROM document_wiki_links "
            "WHERE workspace_id=%s AND wiki_page_id=ANY(%s) ORDER BY document_id,wiki_page_id,relation_type",
            (workspace_id, page_ids),
        ).fetchall()],
    }


def calculate_ingest_preview(
    conn,
    *,
    run: dict[str, Any],
    submissions: list[dict[str, Any]],
    source_blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    workspace_id = str(run["workspace_id"])
    document_id = str(run["document_id"])
    draft_id = str(run["draft_id"])
    operation_id = draft_id
    concepts = _combined_concepts(submissions)
    allocated, identity_index = _allocate_identities(
        conn, draft_id=draft_id, workspace_id=workspace_id, concepts=concepts
    )
    # A title and its official alias can resolve to the same existing page.
    by_page: dict[str, dict[str, Any]] = {}
    for concept in concepts:
        page_id = allocated[name_key(concept["concept_name"])]["page_id"]
        target = by_page.setdefault(page_id, {
            "concept_name": concept["concept_name"], "statements": [], "links": [],
        })
        if concept.get("merge_target_page_id") is not None:
            target["concept_name"] = concept["concept_name"]
        target["statements"].extend(concept["statements"])
        target["links"].extend(concept["links"])
    concepts = list(by_page.values())
    normalized, links = _normalized(run, concepts, allocated, identity_index)
    current_source_blocks = [dict(row) for row in conn.execute(
        "SELECT block_id,text FROM source_blocks WHERE document_id=%s ORDER BY position NULLS LAST,block_id",
        (document_id,),
    ).fetchall()]
    incoming_by_id = {str(item["block_id"]): str(item["text"]) for item in source_blocks}
    invalidated = [
        str(item["block_id"])
        for item in current_source_blocks
        if incoming_by_id.get(str(item["block_id"])) != str(item["text"])
    ]
    contributions = build_concept_contributions(
        operation_id=operation_id,
        normalized=normalized,
        source_blocks=source_blocks,
        links=links,
        source_key_points=normalized["source_key_points"],
        invalidated_block_ids=invalidated,
    )
    target_by_page = {
        page_id: {
            **allocated[name_key(concept["concept_name"])],
            "contribution": contributions[allocated[name_key(concept["concept_name"])]["slug"]],
        }
        for page_id, concept in by_page.items()
    }
    linked_rows = conn.execute(
        "SELECT page.id,page.slug FROM document_wiki_links link JOIN wiki_pages page ON page.id=link.wiki_page_id "
        "WHERE link.document_id=%s AND link.workspace_id=%s AND link.relation_type='extracted_concept' "
        "AND page.page_type='concept' ORDER BY page.id",
        (document_id, workspace_id),
    ).fetchall()
    initial_ids = sorted(set(target_by_page) | {str(row["id"]) for row in linked_rows})
    initial_slugs = [
        str(item["slug"])
        for item in identity_index
        if str(item["id"]) in initial_ids
    ] + [value["slug"] for value in target_by_page.values()]
    inbound = conn.execute(
        "SELECT DISTINCT reference.page_id FROM wiki_page_entry_references reference "
        "JOIN wiki_pages page ON page.id=reference.page_id "
        "WHERE reference.workspace_id=%s AND reference.target_slug=ANY(%s) AND page.status='active' ORDER BY reference.page_id",
        (workspace_id, list(dict.fromkeys(initial_slugs)) or [""]),
    ).fetchall()
    page_ids = sorted(set(initial_ids) | {str(row["page_id"]) for row in inbound})
    existing_identity_ids = {str(item["id"]) for item in identity_index}
    ledger = _load_reconciled(
        conn, workspace_id, [page_id for page_id in page_ids if page_id in existing_identity_ids]
    )
    invalidated_refs = {f"{document_id}:{block_id}" for block_id in invalidated}
    rebuilt: dict[str, Any] = {}
    planned_contributions: dict[str, dict[str, Any]] = {}
    removed_statements: list[dict[str, Any]] = []
    for page_id in initial_ids:
        target = target_by_page.get(page_id)
        contribution = target["contribution"] if target else {
            "schema_version": 1,
            "artifact_type": INVALIDATION,
            "operation_id": operation_id,
            "document_id": document_id,
            "invalidated_reference_ids": sorted(invalidated_refs),
            "page_id": page_id,
        }
        planned_contributions[page_id] = contribution
        try:
            replay = replay_concept_page_with_operation(
                workspace_id=workspace_id,
                page_id=page_id,
                operation_id=operation_id,
                ledger=ledger,
                contribution=contribution,
                read_text=read_text_object,
                required_citations=None if target else invalidated_refs,
                allow_empty=bool(target and target["prior_status"] in {"new", "deleted"}),
            )
        except Exception as exc:
            raise PreviewCalculationError("concept contribution replay failed") from exc
        if replay is None:
            if target and target["prior_status"] == "active":
                raise PreviewCalculationError("active concept contribution ledger is missing")
            continue
        _loaded, rebuilt[page_id] = replay
        removed_statements.extend(_removed_statement_changes(page_id, _loaded, contribution))

    current_live = {
        (str(row["slug"]), str(row["entry_id"]))
        for row in conn.execute(
            "SELECT page.slug,entry.entry_id FROM wiki_page_entries entry JOIN wiki_pages page ON page.id=entry.page_id "
            "WHERE entry.workspace_id=%s AND page.status='active'",
            (workspace_id,),
        ).fetchall()
    }
    slug_by_id = {str(item["id"]): str(item["slug"]) for item in identity_index}
    slug_by_id.update({value["page_id"]: value["slug"] for value in target_by_page.values()})
    for page_id, page in rebuilt.items():
        page_slug = slug_by_id.get(page_id, "")
        current_live = {item for item in current_live if item[0] != page_slug}
        if not page.deleted:
            current_live.update((page_slug, str(entry["entry_id"])) for entry in page.entries)
    for row in inbound:
        page_id = str(row["page_id"])
        if page_id in rebuilt:
            continue
        try:
            replay = rebuild_active_concept_page_from_ledger(
                workspace_id=workspace_id, page_id=page_id, ledger=ledger,
                read_text=read_text_object,
            )
        except Exception as exc:
            raise PreviewCalculationError("reference cascade replay failed") from exc
        if replay is None:
            raise PreviewCalculationError("reference cascade contribution ledger is missing")
        _loaded, page = replay
        markdown, kept, removed = prune_dangling_entry_references(
            page.markdown, page.entries, page.supported_links, current_live
        )
        if removed:
            rebuilt[page_id] = {
                "page_id": page.page_id,
                "markdown": markdown,
                "supported_links": tuple(kept),
                "entries": page.entries,
                "deleted": page.deleted,
                "removed_references": removed,
            }

    source_row = conn.execute(
        "SELECT page.* FROM document_wiki_links link JOIN wiki_pages page ON page.id=link.wiki_page_id "
        "WHERE link.document_id=%s AND link.workspace_id=%s AND link.relation_type='source_of' "
        "AND page.page_type='source' ORDER BY page.updated_at DESC LIMIT 1",
        (document_id, workspace_id),
    ).fetchone()
    source_page_id = str(source_row["id"]) if source_row else _stable_page_id(draft_id, f"source:{document_id}")
    source_before = _body(dict(source_row)) if source_row else None
    source_after = SourcePageAssembler().build(normalized)["markdown"]

    slug_to_id = {str(item["slug"]): str(item["id"]) for item in identity_index}
    slug_to_id.update({value["slug"]: value["page_id"] for value in target_by_page.values()})
    planned_link_ids = {
        slug_to_id[slug]
        for link in links
        for slug in (
            str(link.get("source") or "").removeprefix("concept:"),
            str(link.get("target") or "").removeprefix("concept:"),
        )
        if slug in slug_to_id
    }
    existing_rows = conn.execute(
        "SELECT * FROM wiki_pages WHERE workspace_id=%s AND id=ANY(%s) ORDER BY id",
        (workspace_id, sorted(set(page_ids + ([source_page_id] if source_row else [])) | planned_link_ids)),
    ).fetchall()
    page_rows = {str(row["id"]): dict(row) for row in existing_rows}
    closure_ids = sorted(page_rows)
    structures = _structural_rows(conn, workspace_id, closure_ids)
    endpoint_ids = {
        str(value)
        for item in [*structures["links"], *structures["manual_links"]]
        for value in (item["from_page_id"], item["to_page_id"])
    } - set(page_rows)
    if endpoint_ids:
        endpoint_rows = conn.execute(
            "SELECT * FROM wiki_pages WHERE workspace_id=%s AND id=ANY(%s) ORDER BY id",
            (workspace_id, sorted(endpoint_ids)),
        ).fetchall()
        page_rows.update({str(row["id"]): dict(row) for row in endpoint_rows})
    changes = [_change("SOURCE_PAGE", "UPDATE" if source_before is not None else "ADD", source_page_id, document_id, source_before, source_after)]
    changes.extend(removed_statements)
    for concept in concepts:
        target = allocated[name_key(concept["concept_name"])]
        for statement in concept["statements"]:
            changes.append(_change(
                "CONCEPT_STATEMENT", "ADD", target["page_id"], statement["statement_id"], None,
                statement["text"], statement["evidence"],
            ))
    for page_id, page in rebuilt.items():
        if getattr(page, "deleted", page.get("deleted") if isinstance(page, dict) else False):
            before = page_rows.get(page_id)
            changes.append(_change("PAGE_DELETE", "DELETE", page_id, page_id, before["title"] if before else None, None))
        if page_id in initial_ids:
            for block_id in invalidated:
                changes.append(_change("EVIDENCE_INVALIDATION", "REMOVE", page_id, block_id, f"{document_id}:{block_id}", None))

    current_entries = {
        (str(item["page_id"]), str(item["entry_id"])): item for item in structures["entries"]
    }
    desired_entries = dict(current_entries)
    for page_id, page in rebuilt.items():
        desired_entries = {key: value for key, value in desired_entries.items() if key[0] != page_id}
        if not getattr(page, "deleted", page.get("deleted") if isinstance(page, dict) else False):
            for item in page.entries if hasattr(page, "entries") else page["entries"]:
                desired_entries[(page_id, str(item["entry_id"]))] = {"page_id": page_id, **item}
    for key in sorted(set(current_entries) | set(desired_entries)):
        before, after = current_entries.get(key), desired_entries.get(key)
        if before == after:
            continue
        action = "ADD" if before is None else "REMOVE" if after is None else "UPDATE"
        changes.append(_change("ENTRY", action, key[0], key[1], _canonical_text(before) if before else None, _canonical_text(after) if after else None))

    current_links = {
        (str(item["from_page_id"]), str(item["link_type"]), str(item["to_page_id"])): item
        for item in structures["links"]
    }
    desired_links = dict(current_links)
    for page_id, page in rebuilt.items():
        deleted = getattr(page, "deleted", page.get("deleted") if isinstance(page, dict) else False)
        desired_links = {
            key: value for key, value in desired_links.items()
            if key[0] != page_id and (not deleted or key[2] != page_id)
        }
        if deleted:
            continue
        for link in page.supported_links if hasattr(page, "supported_links") else page["supported_links"]:
            source_id = slug_to_id.get(str(link.get("source") or "").removeprefix("concept:"))
            target_id = slug_to_id.get(str(link.get("target") or "").removeprefix("concept:"))
            if source_id and target_id and source_id != target_id:
                desired_links[(source_id, str(link.get("relation") or "related_to"), target_id)] = {
                    "from_page_id": source_id, "to_page_id": target_id,
                    "link_type": str(link.get("relation") or "related_to"),
                    "label": None, "confidence": None,
                }
    deleted_ids = {
        page_id
        for page_id, page in rebuilt.items()
        if getattr(page, "deleted", page.get("deleted") if isinstance(page, dict) else False)
    }
    linked_to_document = {
        str(row["id"]) for row in linked_rows
    } | {
        page_id for page_id in target_by_page if page_id not in deleted_ids
    }
    manual_by_target = {
        str(item["to_page_id"]): bool(item["present"])
        for item in structures["manual_links"]
        if str(item["from_page_id"]) == source_page_id
    }
    for page_id in sorted(linked_to_document - deleted_ids):
        key = (source_page_id, "source_mentions_concept", page_id)
        if manual_by_target.get(page_id) is False:
            desired_links.pop(key, None)
        else:
            desired_links[key] = {
                "from_page_id": source_page_id, "to_page_id": page_id,
                "link_type": "source_mentions_concept", "label": None, "confidence": None,
            }
    for key in sorted(set(current_links) | set(desired_links)):
        before, after = current_links.get(key), desired_links.get(key)
        if before == after:
            continue
        action = "ADD" if before is None else "REMOVE" if after is None else "UPDATE"
        changes.append(_change("LINK", action, key[0], f"{key[1]}:{key[2]}", _canonical_text(before) if before else None, _canonical_text(after) if after else None))

    current_references = {
        (
            str(item["page_id"]), str(item["target_slug"]), str(item["entry_id"]),
            str(item.get("parent_entry_id") or ""),
        ): item
        for item in structures["references"]
    }
    desired_references = dict(current_references)
    for page_id, page in rebuilt.items():
        desired_references = {
            key: value for key, value in desired_references.items() if key[0] != page_id
        }
        if getattr(page, "deleted", page.get("deleted") if isinstance(page, dict) else False):
            continue
        supported = page.supported_links if hasattr(page, "supported_links") else page["supported_links"]
        for link in supported:
            if link.get("relation") != "entry_reference":
                continue
            key = (
                page_id,
                str(link.get("target") or "").removeprefix("concept:"),
                str(link.get("entry_id") or ""),
                str(link.get("parent_entry_id") or ""),
            )
            desired_references[key] = {
                "page_id": page_id,
                "target_slug": key[1],
                "entry_id": key[2],
                "parent_entry_id": link.get("parent_entry_id"),
                "name": str(link.get("name") or ""),
            }
    for key in sorted(set(current_references) | set(desired_references)):
        before, after = current_references.get(key), desired_references.get(key)
        if before == after:
            continue
        action = "ADD" if before is None else "REMOVE" if after is None else "UPDATE"
        changes.append(_change(
            "REFERENCE", action, key[0], f"{key[1]}:{key[2]}:{key[3]}",
            _canonical_text(before) if before else None,
            _canonical_text(after) if after else None,
        ))

    for value in allocated.values():
        if value["prior_status"] != "deleted":
            continue
        for link in structures["manual_links"]:
            if value["page_id"] in {str(link["from_page_id"]), str(link["to_page_id"])} and link["present"]:
                changes.append(_change("REVIVAL_LINK", "REVIVE", value["page_id"], f"{link['from_page_id']}:{link['to_page_id']}", None, _canonical_text(link)))

    changes = sorted({item["change_id"]: item for item in changes}.values(), key=lambda item: item["change_id"])
    state_pages = [_page_snapshot(row) for row in page_rows.values()]
    public_pages = [item for item in state_pages if item["status"] == "active"]
    target_pages = [
        {key: item[key] for key in ("page_id", "version", "content_hash")}
        for item in sorted(public_pages, key=lambda value: value["page_id"])
    ]
    public_state = {
        "pages": [{key: item[key] for key in ("page_id", "page_type", "slug", "status", "version", "content_hash", "markdown_uri")} for item in state_pages],
        **_jsonable(structures),
    }
    public_state_hash = _hash(public_state)
    source_snapshots = [{
        "snapshot_id": str(run["snapshot_id"]),
        "document_id": document_id,
        "source_version": int(run["source_version"]),
        "source_content_hash": str(run["source_content_hash"]),
    }]
    draft_hash = _hash({
        "draft_kind": "INGEST",
        "source_snapshots": source_snapshots,
        "target_pages": target_pages,
        "normalized_submissions": submissions,
        "changes": changes,
        "public_state_hash": public_state_hash,
    })
    preview = {
        "schema_version": "mcp.wiki-preview.v1",
        "draft_id": draft_id,
        "draft_kind": "INGEST",
        "draft_version": 1,
        "draft_hash": draft_hash,
        "source_snapshots": source_snapshots,
        "target_pages": target_pages,
        "public_state_hash": public_state_hash,
        "changes": changes,
        "complete": True,
    }
    publication_pages = []
    for page_id, page in sorted(rebuilt.items()):
        current = page_rows.get(page_id, {})
        deleted = getattr(page, "deleted", page.get("deleted") if isinstance(page, dict) else False)
        entries = page.entries if hasattr(page, "entries") else page["entries"]
        supported_links = page.supported_links if hasattr(page, "supported_links") else page["supported_links"]
        publication_pages.append({
            "page_id": page_id,
            "slug": slug_by_id.get(page_id, str(current.get("slug") or "")),
            "title": getattr(page, "title", None) or current.get("title"),
            "summary": getattr(page, "summary", None) or current.get("summary"),
            "page_type": "concept",
            "status": "deleted" if deleted else "active",
            "markdown": "" if deleted else str(getattr(page, "markdown", page.get("markdown") if isinstance(page, dict) else "")),
            "entries": sorted(_jsonable(entries), key=lambda item: str(item.get("entry_id"))),
            "supported_links": sorted(_jsonable(supported_links), key=_canonical_text),
            "identity_aliases": sorted(_jsonable(getattr(page, "identity_aliases", ()))),
            "source_document_ids": sorted(_jsonable(getattr(page, "source_document_ids", ()))),
        })
    desired_document_links = [{
        "document_id": document_id,
        "wiki_page_id": source_page_id,
        "relation_type": "source_of",
        "confidence": 1.0,
    }]
    for page in publication_pages:
        if page["status"] == "active":
            desired_document_links.extend({
                "document_id": source_document_id,
                "wiki_page_id": page["page_id"],
                "relation_type": "extracted_concept",
                "confidence": None,
            } for source_document_id in page["source_document_ids"])
    publication_plan = {
        "source_page": {
            "page_id": source_page_id,
            "slug": str(source_row["slug"]) if source_row else document_id,
            "title": str(source_row["title"]) if source_row else str(normalized["document"].get("title") or document_id),
            "summary": "",
            "page_type": "source",
            "status": "active",
            "markdown": source_after,
            "entries": [],
            "supported_links": [],
            "identity_aliases": [],
            "source_document_id": document_id,
        },
        "pages": publication_pages,
        "contributions": [
            {"page_id": page_id, "contribution": _jsonable(contribution)}
            for page_id, contribution in sorted(planned_contributions.items())
        ],
        "document_links": sorted(_jsonable(desired_document_links), key=_canonical_text),
        "wiki_links": sorted(_jsonable(desired_links.values()), key=_canonical_text),
        "entry_references": sorted(_jsonable(desired_references.values()), key=_canonical_text),
        "deleted_page_ids": sorted(deleted_ids),
    }
    return {
        "preview": preview,
        "publication_plan": publication_plan,
        "normalized_submissions": submissions,
        "calculation_inputs": {
            "identity_decisions": sorted(allocated.values(), key=lambda item: item["page_id"]),
            "contributions": contributions,
            "reconciled_ledger": sorted(_jsonable(ledger), key=lambda item: (str(item.get("page_id")), int(item.get("sequence_revision") or 0))),
            "invalidated_block_ids": invalidated,
            "public_state": public_state,
        },
    }
