"""개념 페이지 하위 항목(ADR 0042). 기여 기록의 `entry_changes`를 순서대로 재생해 만든다."""
from __future__ import annotations

from typing import Any

# 다른 페이지 하위 항목을 가리키는 참조 링크의 relation. 링크에 `entry_id`를 함께 싣는다.
ENTRY_REFERENCE = "entry_reference"
ENTRY_EVIDENCE_FIELDS = ("anchor_reference_ids", "evidence_claim_ids")
SUB_ENTRIES_HEADING = "## Sub-entries"


def replay_concept_entries(
    operation_artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """한 페이지의 기여를 적용 순서대로 받아 남은 하위 항목을 돌려준다.

    - add: 없으면 만들고, 있으면 근거만 더한다(이름·상위 항목은 처음 추가한 기여를 따른다).
    - move: 상위 항목만 바꾼다. 없는 항목이거나 자기 아래로 옮기면 무시한다.
    - remove: 항목과 그 아래 항목을 함께 지운다.
    상위 항목이 남지 않은(그 기여를 되돌린) 항목은 페이지 바로 아래로 올린다.
    """
    entries: dict[str, dict[str, Any]] = {}
    for artifact in operation_artifacts:
        for change in artifact.get("entry_changes", []):
            if not isinstance(change, dict) or not change.get("entry_id"):
                continue
            entry_id = str(change["entry_id"])
            parent_id = change.get("parent_entry_id") or None
            op = change.get("op")
            if op == "add":
                entry = entries.setdefault(
                    entry_id,
                    {
                        "entry_id": entry_id,
                        "name": str(change.get("name") or ""),
                        "parent_entry_id": parent_id,
                        **{field: [] for field in ENTRY_EVIDENCE_FIELDS},
                    },
                )
                for field in ENTRY_EVIDENCE_FIELDS:
                    entry[field] = list(dict.fromkeys([*entry[field], *change.get(field, [])]))
            elif op == "move" and entry_id in entries:
                if entry_id not in _ancestors(entries, parent_id):
                    entries[entry_id]["parent_entry_id"] = parent_id
            elif op == "remove":
                for removed in [item for item in entries if entry_id in _ancestors(entries, item)]:
                    del entries[removed]
    for entry_id, entry in entries.items():
        if entry["parent_entry_id"] not in entries or entry_id in _ancestors(entries, entry["parent_entry_id"]):
            entry["parent_entry_id"] = None
    return list(entries.values())


def render_entry_index(
    markdown: str,
    entries: list[dict[str, Any]],
    reference_links: list[dict[str, Any]],
) -> str:
    """본문의 Sub-entries 섹션을 데이터로 다시 그린다. 항목이 없으면 섹션을 뺀다."""
    entry_ids = {entry["entry_id"] for entry in entries}
    nodes = [
        *((entry["parent_entry_id"], f"- {entry['name']}", entry["entry_id"]) for entry in entries),
        *(
            (
                link.get("parent_entry_id") if link.get("parent_entry_id") in entry_ids else None,
                f"- [[{str(link.get('target') or '').removeprefix('concept:')}|{link.get('name') or link.get('entry_id')}]]",
                None,
            )
            for link in reference_links
            if link.get("relation") == ENTRY_REFERENCE
        ),
    ]
    section: list[str] = []

    def walk(parent_id: str | None, depth: int) -> None:
        for node_parent, line, node_id in nodes:
            if node_parent == parent_id:
                section.append("  " * depth + line)
                if node_id is not None:
                    walk(node_id, depth + 1)

    walk(None, 0)
    lines = markdown.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == SUB_ENTRIES_HEADING), -1)
    if start >= 0:
        end = start + 1
        while end < len(lines) and not lines[end].startswith("## "):
            end += 1
        del lines[start:end]
    elif not section:
        return markdown
    if section:
        insert_at = start if start >= 0 else next(
            (index for index, line in enumerate(lines) if line.strip() == "## Related Concepts"),
            len(lines),
        )
        block = [SUB_ENTRIES_HEADING, *section, ""]
        if insert_at > 0 and lines[insert_at - 1].strip():
            block.insert(0, "")
        lines[insert_at:insert_at] = block
    return "\n".join(lines).rstrip() + "\n"


def _ancestors(entries: dict[str, dict[str, Any]], entry_id: str | None) -> set[str]:
    """entry_id 자신과 그 위 항목들. 순환이 있어도 멈춘다."""
    seen: set[str] = set()
    while entry_id in entries and entry_id not in seen:
        seen.add(entry_id)
        entry_id = entries[entry_id]["parent_entry_id"]
    return seen
