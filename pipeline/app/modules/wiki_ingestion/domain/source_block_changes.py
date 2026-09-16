from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from app.modules.wiki_generation.domain.entities import SourceBlock


@dataclass(frozen=True)
class SourceBlockChanges:
    blocks: list[SourceBlock]
    ingest_blocks: list[SourceBlock]
    max_block_number: int
    unchanged_block_ids: list[str]
    moved_block_ids: list[str]
    added_block_ids: list[str]
    modified_block_ids: list[str]
    deleted_block_ids: list[str]

    @property
    def invalidated_block_ids(self) -> list[str]:
        return [*self.modified_block_ids, *self.deleted_block_ids]

    def to_manifest(self) -> dict[str, list[str]]:
        return {
            "unchanged_block_ids": self.unchanged_block_ids,
            "moved_block_ids": self.moved_block_ids,
            "added_block_ids": self.added_block_ids,
            "modified_block_ids": self.modified_block_ids,
            "deleted_block_ids": self.deleted_block_ids,
            "invalidated_block_ids": self.invalidated_block_ids,
        }


def compare_source_blocks(
    existing: list[dict[str, Any]],
    incoming: list[SourceBlock],
    previous_max_block_number: int = 0,
) -> SourceBlockChanges:
    old_texts = [str(item.get("text") or "") for item in existing]
    new_texts = [block.text for block in incoming]
    old_by_new: dict[int, int] = {}

    matcher = SequenceMatcher(a=old_texts, b=new_texts, autojunk=False)
    for old_start, new_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            old_by_new[new_start + offset] = old_start + offset

    _match_unique_moved_blocks(old_texts, new_texts, old_by_new)
    modified_pairs = _match_single_replacements(
        len(existing),
        len(incoming),
        old_by_new,
    )
    old_by_new.update(modified_pairs)

    matched_old = set(old_by_new.values())
    modified_new = set(modified_pairs)
    next_block_number = max(
        _next_block_number(existing),
        previous_max_block_number + 1,
    )
    blocks: list[SourceBlock] = []
    ingest_blocks: list[SourceBlock] = []
    unchanged_block_ids: list[str] = []
    moved_block_ids: list[str] = []
    added_block_ids: list[str] = []
    modified_block_ids: list[str] = []

    for new_index, block in enumerate(incoming):
        old_index = old_by_new.get(new_index)
        if old_index is None:
            block_id = f"B{next_block_number:04d}"
            next_block_number += 1
            added_block_ids.append(block_id)
            mapped = _with_block_id(block, block_id)
            ingest_blocks.append(mapped)
        else:
            block_id = str(existing[old_index]["block_id"])
            mapped = _with_block_id(block, block_id)
            if new_index in modified_new:
                modified_block_ids.append(block_id)
                ingest_blocks.append(mapped)
            else:
                unchanged_block_ids.append(block_id)
                if old_index != new_index:
                    moved_block_ids.append(block_id)
        blocks.append(mapped)

    deleted_block_ids = [
        str(item["block_id"])
        for index, item in enumerate(existing)
        if index not in matched_old
    ]
    return SourceBlockChanges(
        blocks=blocks,
        ingest_blocks=ingest_blocks,
        max_block_number=next_block_number - 1,
        unchanged_block_ids=unchanged_block_ids,
        moved_block_ids=moved_block_ids,
        added_block_ids=added_block_ids,
        modified_block_ids=modified_block_ids,
        deleted_block_ids=deleted_block_ids,
    )


def _match_unique_moved_blocks(
    old_texts: list[str],
    new_texts: list[str],
    old_by_new: dict[int, int],
) -> None:
    matched_old = set(old_by_new.values())
    old_indexes_by_text: dict[str, list[int]] = {}
    new_indexes_by_text: dict[str, list[int]] = {}
    for index, text in enumerate(old_texts):
        if index not in matched_old:
            old_indexes_by_text.setdefault(text, []).append(index)
    for index, text in enumerate(new_texts):
        if index not in old_by_new:
            new_indexes_by_text.setdefault(text, []).append(index)
    for text, old_indexes in old_indexes_by_text.items():
        new_indexes = new_indexes_by_text.get(text, [])
        if len(old_indexes) == 1 and len(new_indexes) == 1:
            old_by_new[new_indexes[0]] = old_indexes[0]


def _match_single_replacements(
    old_count: int,
    new_count: int,
    old_by_new: dict[int, int],
) -> dict[int, int]:
    anchors = [(-1, -1), *sorted((old, new) for new, old in old_by_new.items()), (old_count, new_count)]
    replacements: dict[int, int] = {}
    for (old_left, new_left), (old_right, new_right) in zip(anchors, anchors[1:]):
        old_gap = list(range(old_left + 1, old_right))
        new_gap = list(range(new_left + 1, new_right))
        if len(old_gap) == 1 and len(new_gap) == 1:
            replacements[new_gap[0]] = old_gap[0]
    return replacements


def _next_block_number(existing: list[dict[str, Any]]) -> int:
    numbers = []
    for item in existing:
        match = re.fullmatch(r"B(\d+)", str(item.get("block_id") or ""))
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def _with_block_id(block: SourceBlock, block_id: str) -> SourceBlock:
    return SourceBlock(
        document_id=block.document_id,
        block_id=block_id,
        source_reference_id=block_id,
        text=block.text,
        line_start=block.line_start,
        line_end=block.line_end,
        section_path=block.section_path,
        block_type=block.block_type,
    )
