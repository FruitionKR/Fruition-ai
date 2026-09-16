from __future__ import annotations

from typing import Any


def append_concept_evidence(markdown: str, updates: list[dict[str, Any]]) -> str:
    evidence_lines = [_concept_evidence_line(update) for update in updates]
    evidence_lines = [line for line in evidence_lines if line]
    if not evidence_lines:
        return markdown
    lines = markdown.splitlines()
    heading_index = next((index for index, line in enumerate(lines) if line.strip() == "## Evidence"), -1)
    if heading_index < 0:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["## Evidence", *evidence_lines])
        return "\n".join(lines).rstrip() + "\n"
    end_index = heading_index + 1
    while end_index < len(lines) and not lines[end_index].startswith("## "):
        end_index += 1
    existing = {line.strip() for line in lines[heading_index + 1 : end_index] if line.strip()}
    if "- 아직 연결된 evidence claim 없음" in existing:
        remove_index = next(
            (index for index in range(heading_index + 1, end_index) if lines[index].strip() == "- 아직 연결된 evidence claim 없음"),
            -1,
        )
        if remove_index >= 0:
            lines.pop(remove_index)
            end_index -= 1
            existing.remove("- 아직 연결된 evidence claim 없음")
    insert_at = end_index
    for evidence_line in evidence_lines:
        if evidence_line.strip() in existing:
            continue
        lines.insert(insert_at, evidence_line)
        insert_at += 1
        existing.add(evidence_line.strip())
    return "\n".join(lines).rstrip() + "\n"


def _concept_evidence_line(update: dict[str, Any]) -> str:
    claim = str(update.get("claim") or "").strip()
    if not claim:
        return ""
    refs = [str(ref) for ref in update.get("refs", []) if ref]
    suffix = f" [{', '.join(refs)}]" if refs else ""
    claim_id = str(update.get("claim_id") or "").strip()
    prefix = f"{claim_id}: " if claim_id else ""
    return f"- {prefix}{claim}{suffix}"
