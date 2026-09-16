from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.modules.wiki_generation.application.models import GenerationEvaluation
from app.modules.wiki_generation.application.ports import JsonDict
from app.modules.wiki_generation.domain.text_utils import slugify


PATCH_COLLECTIONS = (
    "key_points",
    "observations",
    "categories",
    "core_concepts",
    "concept_candidates",
    "section_candidates",
    "mentions",
    "evidence_claims",
)
ANCHORED_COLLECTIONS = set(PATCH_COLLECTIONS) - {"categories"}


def build_semantic_patch_targets(
    notes: list[JsonDict],
    evaluation: GenerationEvaluation,
    target_block_ids: list[str],
) -> list[JsonDict]:
    evaluator_targets = {
        str(target).strip()
        for issue in evaluation.get("issues", []) or []
        for target in _targets(issue.get("target"))
        if str(target).strip()
    }
    target_blocks = set(target_block_ids)
    directly_targeted_blocks = evaluator_targets.intersection(target_blocks)
    rows: list[JsonDict] = []
    evidence_index = 0
    observation_index = 0
    for note in notes:
        chunk_id = str(note.get("chunk_id") or "")
        for collection in PATCH_COLLECTIONS:
            items = note.get(collection, []) or []
            if not isinstance(items, list):
                continue
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                identifiers = _item_identifiers(collection, item)
                if collection == "evidence_claims":
                    evidence_index += 1
                    identifiers.add(f"ev_{evidence_index:04d}")
                if collection == "observations" and (
                    str(item.get("title") or "").strip()
                    or str(item.get("summary") or "").strip()
                ):
                    observation_index += 1
                    identifiers.add(f"O{observation_index:03d}")
                anchors = set(_anchor_block_ids(item))
                if not evaluator_targets.intersection(identifiers) and not directly_targeted_blocks.intersection(anchors):
                    continue
                rows.append(
                    {
                        "chunk_id": chunk_id,
                        "collection": collection,
                        "index": index,
                        "identifiers": sorted(identifiers),
                        "value": item,
                    }
                )
    return rows


def apply_semantic_patch(
    notes: list[JsonDict],
    patch: JsonDict,
    editable_targets: list[JsonDict],
    allowed_block_ids: list[str],
) -> list[JsonDict] | None:
    operations = patch.get("operations")
    if not isinstance(operations, list) or not operations:
        return None

    editable_paths = {
        (str(item["chunk_id"]), str(item["collection"]), int(item["index"]))
        for item in editable_targets
    }
    editable_chunks = {path[0] for path in editable_paths}
    validated: list[JsonDict] = []
    mutated_paths: set[tuple[str, str, int]] = set()
    for operation in operations:
        normalized_operation = _validated_operation(
            operation,
            editable_paths,
            editable_chunks,
            set(allowed_block_ids),
        )
        if normalized_operation is None:
            return None
        if normalized_operation["op"] != "add":
            path = (
                normalized_operation["chunk_id"],
                normalized_operation["collection"],
                normalized_operation["index"],
            )
            if path in mutated_paths:
                return None
            mutated_paths.add(path)
        validated.append(normalized_operation)

    patched = deepcopy(notes)
    notes_by_chunk = {str(note.get("chunk_id")): note for note in patched}
    item_operations = sorted(
        (operation for operation in validated if operation["op"] != "add"),
        key=lambda operation: (
            operation["chunk_id"],
            operation["collection"],
            -operation["index"],
        ),
    )
    for operation in [*item_operations, *(item for item in validated if item["op"] == "add")]:
        note = notes_by_chunk.get(operation["chunk_id"])
        if note is None:
            return None
        collection = note.setdefault(operation["collection"], [])
        if not isinstance(collection, list):
            return None
        if operation["op"] == "remove":
            del collection[operation["index"]]
        elif operation["op"] == "replace":
            collection[operation["index"] : operation["index"] + 1] = operation["items"]
        else:
            collection.extend(operation["items"])
    return patched


def _validated_operation(
    operation: Any,
    editable_paths: set[tuple[str, str, int]],
    editable_chunks: set[str],
    allowed_block_ids: set[str],
) -> JsonDict | None:
    if not isinstance(operation, dict):
        return None
    op = str(operation.get("op") or "")
    chunk_id = str(operation.get("chunk_id") or "")
    collection = str(operation.get("collection") or "")
    if op not in {"replace", "remove", "add"} or collection not in PATCH_COLLECTIONS:
        return None
    if chunk_id not in editable_chunks:
        return None

    if op == "add":
        index = -1
    else:
        index = operation.get("index")
        if not isinstance(index, int) or (chunk_id, collection, index) not in editable_paths:
            return None

    raw_items = operation.get("items", [])
    if op == "remove":
        items: list[JsonDict] = []
    elif not isinstance(raw_items, list) or not raw_items:
        return None
    else:
        items = []
        for item in raw_items:
            if not isinstance(item, dict) or not _valid_patch_item(collection, item, allowed_block_ids):
                return None
            items.append(item)
    return {
        "op": op,
        "chunk_id": chunk_id,
        "collection": collection,
        "index": index,
        "items": items,
    }


def _valid_patch_item(collection: str, item: JsonDict, allowed_block_ids: set[str]) -> bool:
    anchors = set(_anchor_block_ids(item))
    if not anchors.issubset(allowed_block_ids):
        return False
    return collection not in ANCHORED_COLLECTIONS or bool(anchors)


def _item_identifiers(collection: str, item: JsonDict) -> set[str]:
    identifiers = {
        str(item.get(field)).strip()
        for field in ("slug", "slug_hint", "title", "name")
        if item.get(field)
    }
    if collection in {"core_concepts", "concept_candidates", "section_candidates"}:
        identifiers.add(slugify(item.get("slug_hint") or item.get("title") or ""))
    if collection in {"mentions", "categories"}:
        identifiers.add(slugify(item.get("slug_hint") or item.get("name") or ""))
    return {identifier for identifier in identifiers if identifier}


def _anchor_block_ids(item: JsonDict) -> list[str]:
    values = item.get("anchor_block_ids") or item.get("evidence_block_ids") or []
    return [str(value) for value in values if str(value)] if isinstance(values, list) else []


def _targets(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]
