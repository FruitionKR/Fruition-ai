from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from app.modules.wiki_generation.domain.entities import SourceBlock, SourceDocument
from app.modules.wiki_generation.domain.text_utils import normalize_space, sha1_short


def _guess_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        if line.strip().startswith("#"):
            return line.strip().lstrip("#").strip() or fallback
    return fallback


class MarkdownBlockExtractor:
    """Splits Markdown into addressable blocks.

    This does not try to understand meaning. It only adds stable addresses so the
    LLM can cite short anchors like [B0012] while the backend keeps long refs.
    """

    def extract(self, path: str | Path) -> tuple[SourceDocument, list[SourceBlock]]:
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        return self.extract_text(text, source_path=str(path), fallback_title=path.stem)

    def extract_text(
        self,
        text: str,
        source_path: str,
        fallback_title: str | None = None,
    ) -> tuple[SourceDocument, list[SourceBlock]]:
        doc_hash = sha1_short(text)
        doc = self._document(text, source_path, fallback_title)
        raw_blocks = self._split_blocks(text)
        blocks: list[SourceBlock] = []
        section_path: list[str] = []
        for idx, (block_type, block_text, start, end) in enumerate(raw_blocks, start=1):
            stripped = block_text.strip()
            if block_type == "heading":
                level = len(stripped) - len(stripped.lstrip("#"))
                heading = stripped.lstrip("#").strip()
                section_path = section_path[: max(0, level - 1)] + [heading]
            blocks.append(
                SourceBlock(
                    document_id=doc.document_id,
                    block_id=f"B{idx:04d}",
                    source_reference_id=f"ref_{doc_hash}_md_b{idx:04d}",
                    text=normalize_space(block_text),
                    line_start=start,
                    line_end=end,
                    section_path=section_path.copy(),
                    block_type=block_type,
                )
            )
        return doc, blocks

    def blocks_from_records(
        self,
        records: list[dict[str, str]],
        *,
        text: str,
        source_path: str,
        fallback_title: str | None = None,
    ) -> tuple[SourceDocument, list[SourceBlock]]:
        """블록 경계와 anchor를 호출자가 확정해 넘기는 경로.

        채팅처럼 provenance를 본문에 실을 수 없는 입력에서 쓴다. block_id가 그대로
        source_reference_id가 되므로 본문에는 id가 들어가지 않는다.
        """
        doc = self._document(text, source_path, fallback_title)
        blocks = [
            SourceBlock(
                document_id=doc.document_id,
                block_id=record["block_id"],
                source_reference_id=record["block_id"],
                text=normalize_space(record["text"]),
                line_start=idx,
                line_end=idx,
            )
            for idx, record in enumerate(records, start=1)
        ]
        return doc, blocks

    def _document(
        self,
        text: str,
        source_path: str,
        fallback_title: str | None,
    ) -> SourceDocument:
        return SourceDocument(
            document_id=f"doc_{sha1_short(text)}",
            title=_guess_title(text, fallback_title or Path(source_path).stem),
            source_path=source_path,
            content_sha1=sha1_short(text, 40),
        )

    def _split_blocks(self, text: str) -> List[Tuple[str, str, int, int]]:
        lines = text.splitlines()
        blocks: list[tuple[str, str, int, int]] = []
        buf: list[str] = []
        buf_start = 1
        in_code = False

        def flush(end_line: int) -> None:
            nonlocal buf, buf_start
            if not buf:
                return
            raw = "\n".join(buf).strip("\n")
            if raw.strip():
                btype = "code" if raw.strip().startswith("```") else "paragraph"
                if raw.lstrip().startswith("#") and "\n" not in raw.strip():
                    btype = "heading"
                elif raw.lstrip().startswith(("- ", "* ", "1. ")):
                    btype = "list"
                blocks.append((btype, raw, buf_start, end_line))
            buf = []

        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("```"):
                if not in_code:
                    flush(i - 1)
                    buf_start = i
                    buf = [line]
                    in_code = True
                else:
                    buf.append(line)
                    flush(i)
                    in_code = False
                continue
            if in_code:
                buf.append(line)
                continue
            if not stripped:
                flush(i - 1)
                continue
            # Headings should be their own blocks.
            if stripped.startswith("#"):
                flush(i - 1)
                buf_start = i
                buf = [line]
                flush(i)
                continue
            if not buf:
                buf_start = i
            buf.append(line)
        flush(len(lines))
        return blocks
