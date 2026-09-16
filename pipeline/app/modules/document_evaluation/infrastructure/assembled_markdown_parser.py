from __future__ import annotations

import re

from app.modules.document_evaluation.domain.entities import AssembledDocumentBlock


BLOCK_PATTERN = re.compile(
    r"<!--\s+(?P<id>\S+)\s+type=(?P<type>\S+)\s+"
    r"bbox=\[(?P<bbox>[^]]+)](?:\s+[^>]*)?-->"
)
PAGE_PATTERN = re.compile(r"_p(?P<page>\d+)_")


def parse_assembled_markdown(markdown: str) -> list[AssembledDocumentBlock]:
    matches = list(BLOCK_PATTERN.finditer(markdown))
    blocks = []
    for index, match in enumerate(matches):
        content_end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        content = markdown[match.end() : content_end].strip()
        content = re.sub(r"\n+## Page \d+\s*$", "", content).strip()
        page_match = PAGE_PATTERN.search(match.group("id"))
        bbox = tuple(float(value.strip()) for value in match.group("bbox").split(","))
        if page_match is None or len(bbox) != 4:
            raise ValueError(f"잘못된 block metadata: {match.group(0)}")
        blocks.append(
            AssembledDocumentBlock(
                block_id=match.group("id"),
                block_type=match.group("type"),
                page=int(page_match.group("page")),
                bbox=bbox,
                markdown=content,
            )
        )
    return blocks
