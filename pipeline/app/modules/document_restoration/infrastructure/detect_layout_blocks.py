from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import re
from pathlib import Path
from typing import Any

import fitz


BASE_DIR = Path(__file__).resolve().parents[1]
PAPER_DIR = BASE_DIR.parent
DOCUMENT_SLUG = "document"

PDF_FILE = PAPER_DIR / f"{DOCUMENT_SLUG}.pdf"
OUTPUT_DIR = BASE_DIR / "layout" / "auto"
ASSET_DIR = OUTPUT_DIR / "assets"
MANIFEST_FILE = OUTPUT_DIR / f"{DOCUMENT_SLUG}.auto_block_manifest.json"


@dataclass(frozen=True)
class LayoutBlock:
    id: str
    page: int
    order: int
    type: str
    bbox: tuple[float, float, float, float]
    source_text: str
    markdown: str
    asset: str | None
    confidence: str


def decode_shifted_char(char: str) -> str:
    if char.isspace():
        return " "
    code = ord(char)
    if 0 <= code <= 93:
        return chr(code + 29)
    return char


def decode_shifted_text(text: str) -> str:
    decoded = "".join(decode_shifted_char(char) for char in text)
    replacements = {
        "\x1d": "",
        "\x1e": "",
        "\x1f": "",
        "\u00b6": "'",
        "\u00b2": "-",
        "\u00ad": "-",
        "\u00a3": "?",
        "\u00a9": "?",
        "\u00aa": "?",
        "\u00ab": "?",
        "\u00ac": "?",
        "\u00ae": "?",
        "\u00af": "?",
    }
    for old, new in replacements.items():
        decoded = decoded.replace(old, new)
    return " ".join(decoded.split())


def text_from_block(block: dict[str, Any]) -> str:
    lines = []
    for line in block.get("lines", []):
        spans = [span.get("text", "") for span in line.get("spans", [])]
        raw = "".join(spans).strip()
        if raw:
            lines.append(raw)
    return "\n".join(lines)


def normalized_bbox(block: dict[str, Any]) -> tuple[float, float, float, float]:
    return tuple(round(float(value), 2) for value in block["bbox"])  # type: ignore[return-value]


def is_footer(text: str, bbox: tuple[float, float, float, float]) -> bool:
    lowered = text.lower()
    x0, y0, x1, y1 = bbox
    return (
        "authorized licensed use limited to:" in lowered
        or "uthorized licensed use limited tow" in lowered
        or "ieee transportation electrification conference" in lowered
        or "fbbb qransportation blectrification" in lowered
        or "this work was supported" in lowered
        or "restrictions apply" in lowered
        or "oestrictions apply" in lowered
        or "doi:" in lowered
        or bool(re.search(r"\bdoiw\b", lowered))
        or (x1 <= 25 and y1 - y0 > 200)
        or (y0 >= 760 and x1 - x0 < 520)
        or bool(re.fullmatch(r"\d{3}", text.strip()))
        or (y0 >= 790 and bool(re.fullmatch(r"[A-Z]{3}", text.strip())))
    )


def looks_caption(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("Fig.") or stripped.startswith("TABLE ")


def looks_heading(text: str) -> bool:
    stripped = text.strip()
    return bool(re.match(r"^[IVX]+\.\s+", stripped) or re.match(r"^[A-D]\.\s+", stripped))


def looks_equation(text: str, bbox: tuple[float, float, float, float]) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    words = re.findall(r"[A-Za-z]{3,}", stripped)
    math_tokens = re.findall(r"[=×πγϕη−+/%≤≥<>∑∏√±∞≈≠]|\\[A-Za-z]+|[_^{}]", stripped)
    height = bbox[3] - bbox[1]
    width = bbox[2] - bbox[0]
    if looks_like_decoded_equation_sequence(stripped, bbox):
        return True
    if math_tokens and len(words) <= 8:
        return True
    if height <= 35 and width <= 280 and len(stripped.split()) <= 18 and any(char.isdigit() for char in stripped):
        return True
    return False


def looks_like_decoded_equation_sequence(text: str, bbox: tuple[float, float, float, float]) -> bool:
    height = bbox[3] - bbox[1]
    if height > 24:
        return False
    tokens = text.split()
    if len(tokens) < 8:
        return False
    symbolic_count = sum(
        1
        for token in tokens
        if re.fullmatch(r"[A-Za-z]{1,4}", token)
        or re.fullmatch(r"\d{1,2}", token)
        or re.search(r"[=×πγϕη−+/%≤≥<>_^{},]", token)
    )
    alpha_long_words = re.findall(r"[A-Za-z]{5,}", text)
    return symbolic_count / len(tokens) >= 0.65 and len(alpha_long_words) <= 2


def markdown_for_text(block_type: str, text: str) -> str:
    if block_type == "heading":
        if re.match(r"^[IVX]+\. ", text):
            return f"## {text}"
        return f"### {text}"
    if block_type == "caption":
        return text
    return text.replace("\n", " ")


def padded_rect(page: fitz.Page, bbox: tuple[float, float, float, float], padding: float) -> fitz.Rect:
    rect = fitz.Rect(bbox)
    if padding <= 0:
        return rect
    return fitz.Rect(
        max(0, rect.x0 - padding),
        max(0, rect.y0 - padding),
        min(page.rect.width, rect.x1 + padding),
        min(page.rect.height, rect.y1 + padding),
    )


def crop_asset(
    document: fitz.Document,
    page_index: int,
    bbox: tuple[float, float, float, float],
    subdir: str,
    filename: str,
    padding: float = 0,
) -> str:
    output_dir = ASSET_DIR / subdir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / filename
    page = document[page_index]
    pixmap = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=padded_rect(page, bbox, padding), alpha=False)
    pixmap.save(output_file)
    return str(output_file.relative_to(BASE_DIR))


def crop_asset_from_pdf(
    page_number: int,
    bbox: tuple[float, float, float, float],
    subdir: str,
    filename: str,
    padding: float = 0,
) -> str:
    document = fitz.open(PDF_FILE)
    return crop_asset(document, page_number - 1, bbox, subdir, filename, padding)


def union_bbox(blocks: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    x0 = min(float(block["bbox"][0]) for block in blocks)
    y0 = min(float(block["bbox"][1]) for block in blocks)
    x1 = max(float(block["bbox"][2]) for block in blocks)
    y1 = max(float(block["bbox"][3]) for block in blocks)
    return (round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2))


def union_layout_bbox(blocks: list[LayoutBlock]) -> tuple[float, float, float, float]:
    x0 = min(block.bbox[0] for block in blocks)
    y0 = min(block.bbox[1] for block in blocks)
    x1 = max(block.bbox[2] for block in blocks)
    y1 = max(block.bbox[3] for block in blocks)
    return (round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2))


def merge_image_stripes(raw_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index = 0
    while index < len(raw_blocks):
        block = raw_blocks[index]
        if block.get("type") != 1:
            merged.append(block)
            index += 1
            continue

        group = [block]
        index += 1
        while index < len(raw_blocks):
            candidate = raw_blocks[index]
            if candidate.get("type") != 1:
                break
            prev_bbox = group[-1]["bbox"]
            cand_bbox = candidate["bbox"]
            same_column = abs(float(prev_bbox[0]) - float(cand_bbox[0])) < 3 and abs(float(prev_bbox[2]) - float(cand_bbox[2])) < 3
            vertically_adjacent = abs(float(cand_bbox[1]) - float(prev_bbox[3])) < 2
            if not (same_column and vertically_adjacent):
                break
            group.append(candidate)
            index += 1

        if len(group) == 1:
            merged.append(group[0])
        else:
            merged_block = dict(group[0])
            merged_block["bbox"] = union_bbox(group)
            merged.append(merged_block)
    return merged


def classify_text_block(text: str, bbox: tuple[float, float, float, float]) -> tuple[str, str]:
    if is_footer(text, bbox):
        return "footer", "auto_footer"
    if looks_caption(text):
        return "caption", "auto_caption"
    if looks_heading(text):
        return "heading", "auto_heading"
    if looks_equation(text, bbox):
        return "equation_candidate", "auto_equation_candidate"
    return "paragraph", "auto_paragraph"


def reading_order_key(block: dict[str, Any]) -> tuple[int, float, float]:
    x0, y0, _, _ = block["bbox"]
    column = 0 if x0 < 300 else 1
    return (column, y0, x0)


def block_bbox_from_lines(lines: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    x0 = min(float(line["bbox"][0]) for line in lines)
    y0 = min(float(line["bbox"][1]) for line in lines)
    x1 = max(float(line["bbox"][2]) for line in lines)
    y1 = max(float(line["bbox"][3]) for line in lines)
    return (x0, y0, x1, y1)


def split_mixed_column_text_block(block: dict[str, Any]) -> list[dict[str, Any]]:
    if block.get("type") != 0:
        return [block]

    lines = block.get("lines", [])
    if not lines:
        return [block]

    left_lines = [line for line in lines if float(line["bbox"][0]) < 300]
    right_lines = [line for line in lines if float(line["bbox"][0]) >= 300]
    if not left_lines or not right_lines:
        return [block]

    split_blocks = []
    for grouped_lines in (left_lines, right_lines):
        split_block = dict(block)
        split_block["lines"] = grouped_lines
        split_block["bbox"] = block_bbox_from_lines(grouped_lines)
        split_blocks.append(split_block)
    return split_blocks


def split_mixed_column_blocks(raw_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    split_blocks: list[dict[str, Any]] = []
    for block in raw_blocks:
        split_blocks.extend(split_mixed_column_text_block(block))
    return sorted(split_blocks, key=reading_order_key)


def detect_blocks() -> list[LayoutBlock]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    document = fitz.open(PDF_FILE)
    detected: list[LayoutBlock] = []
    for page_index, page in enumerate(document):
        page_number = page_index + 1
        raw_blocks = sorted(page.get_text("dict", sort=True)["blocks"], key=reading_order_key)
        raw_blocks = merge_image_stripes(raw_blocks)
        raw_blocks = split_mixed_column_blocks(raw_blocks)
        order = 0
        for raw_block in raw_blocks:
            order += 1
            bbox = normalized_bbox(raw_block)
            block_id = f"p{page_number:02d}_b{order:03d}"
            asset = None
            if raw_block.get("type") == 1:
                asset = crop_asset(document, page_index, bbox, "figures", f"{block_id}.png")
                detected.append(
                    LayoutBlock(
                        id=block_id,
                        page=page_number,
                        order=order,
                        type="figure_candidate",
                        bbox=bbox,
                        source_text="",
                        markdown=f"![Figure candidate {block_id}](../{asset})",
                        asset=asset,
                        confidence="auto_figure_candidate",
                    )
                )
                continue

            raw_text = text_from_block(raw_block)
            if not raw_text.strip():
                continue
            decoded_text = decode_shifted_text(raw_text)
            if not decoded_text.strip():
                continue
            if is_footer(raw_text, bbox):
                block_type, confidence = "footer", "auto_footer"
                decoded_text = raw_text
            else:
                block_type, confidence = classify_text_block(decoded_text, bbox)
            if block_type == "equation_candidate":
                asset = crop_asset(document, page_index, bbox, "equations", f"{block_id}.png", padding=6)
                markdown = f"```text\n{decoded_text}\n```\n\n[equation candidate: {asset}]"
            elif block_type == "footer":
                markdown = f"<!-- footer omitted: {decoded_text} -->"
            else:
                markdown = markdown_for_text(block_type, decoded_text)
            detected.append(
                LayoutBlock(
                    id=block_id,
                    page=page_number,
                    order=order,
                    type=block_type,
                    bbox=bbox,
                    source_text=decoded_text,
                    markdown=markdown,
                    asset=asset,
                    confidence=confidence,
                )
            )
    return detected


def relabel_table_regions(blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    by_page: dict[int, list[LayoutBlock]] = {}
    for block in blocks:
        by_page.setdefault(block.page, []).append(block)

    relabeled: list[LayoutBlock] = []
    for page, page_blocks in by_page.items():
        table_windows: list[tuple[float, float, float, float]] = []
        for block in page_blocks:
            if block.type == "caption" and block.source_text.startswith("TABLE "):
                x0, y0, x1, _ = block.bbox
                table_windows.append((max(0, x0 - 40), y0, min(595, x1 + 40), y0 + 320))

        for block in page_blocks:
            if block.type == "equation_candidate":
                bx0, by0, bx1, by1 = block.bbox
                in_table = any(
                    bx0 >= x0 and bx1 <= x1 and by0 >= y0 and by1 <= y1
                    for x0, y0, x1, y1 in table_windows
                )
                if in_table:
                    relabeled.append(
                        LayoutBlock(
                            id=block.id,
                            page=block.page,
                            order=block.order,
                            type="table_candidate",
                            bbox=block.bbox,
                            source_text=block.source_text,
                            markdown=f"```text\n{block.source_text}\n```\n\n[table candidate row: {block.asset}]",
                            asset=block.asset,
                            confidence="auto_table_candidate_from_caption_window",
                        )
                    )
                    continue
            relabeled.append(block)
    return sorted(relabeled, key=lambda block: (block.page, block.order))


def is_short_math_paragraph(block: LayoutBlock) -> bool:
    if block.type != "paragraph":
        return False
    text = block.source_text.strip()
    if not text:
        return False
    if looks_like_decoded_equation_sequence(text, block.bbox):
        return True
    if len(text.split()) > 8:
        return False
    return bool(re.search(r"[=×πγϕη−+/%≤≥<>∑∏√±∞≈≠]|\\[A-Za-z]+|[_^{}]|\d", text))


def is_equation_fragment(block: LayoutBlock) -> bool:
    if block.type == "equation_candidate":
        return True
    return is_short_math_paragraph(block)


def same_column(left: LayoutBlock, right: LayoutBlock) -> bool:
    return (left.bbox[0] < 300 and right.bbox[0] < 300) or (left.bbox[0] >= 300 and right.bbox[0] >= 300)


def bbox_horizontally_related(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> bool:
    overlap = min(left[2], right[2]) - max(left[0], right[0])
    if overlap > 0:
        return True
    gap = max(left[0], right[0]) - min(left[2], right[2])
    return gap <= 45


def has_equation_number_token(text: str) -> bool:
    return bool(re.search(r"(?:^|\s)([1-9][0-9]?)(?:\s|$)", text))


def has_lhs_like_token(text: str) -> bool:
    return bool(re.search(r"\b[A-Za-z]{1,8}\b\s*=", text) or re.search(r"\b[A-Za-z]{1,8}\b", text))


def starts_new_equation(candidate: LayoutBlock) -> bool:
    text = candidate.source_text
    return has_equation_number_token(text) and has_lhs_like_token(text)


def can_bridge_equation_gap(candidate: LayoutBlock, vertical_gap: float) -> bool:
    if vertical_gap <= 4:
        return True
    if candidate.type != "equation_candidate":
        return False
    return bool(re.search(r"\d+\.\d+", candidate.source_text)) and not starts_new_equation(candidate)


def should_merge_equation_group(group: list[LayoutBlock], candidate: LayoutBlock) -> bool:
    previous = group[-1]
    group_bbox = union_layout_bbox(group)
    if previous.page != candidate.page or not same_column(previous, candidate):
        return False
    if not is_equation_fragment(candidate):
        return False
    vertical_gap = candidate.bbox[1] - group_bbox[3]
    overlaps_vertically = candidate.bbox[1] <= group_bbox[3] + 4
    close_vertically = 0 <= vertical_gap <= 24
    if not (overlaps_vertically or close_vertically):
        return False
    if not overlaps_vertically and not can_bridge_equation_gap(candidate, vertical_gap):
        return False
    if not overlaps_vertically and starts_new_equation(candidate):
        return False
    return bbox_horizontally_related(group_bbox, candidate.bbox)


def merged_equation_block(group: list[LayoutBlock]) -> LayoutBlock:
    first = group[0]
    bbox = union_layout_bbox(group)
    source_text = "\n".join(block.source_text for block in group if block.source_text.strip())
    asset = crop_asset_from_pdf(first.page, bbox, "equations", f"{first.id}_merged.png", padding=6)
    return LayoutBlock(
        id=first.id,
        page=first.page,
        order=first.order,
        type="equation_candidate",
        bbox=bbox,
        source_text=source_text,
        markdown=f"```text\n{source_text}\n```\n\n[equation candidate: {asset}]",
        asset=asset,
        confidence="auto_merged_equation_candidate",
    )


def merge_equation_candidates(blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    merged: list[LayoutBlock] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.type != "equation_candidate":
            merged.append(block)
            index += 1
            continue

        group = [block]
        index += 1
        while index < len(blocks) and should_merge_equation_group(group, blocks[index]):
            group.append(blocks[index])
            index += 1

        merged.append(merged_equation_block(group) if len(group) > 1 else block)
    return merged


def block_in_window(block: LayoutBlock, window: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = window
    bx0, by0, bx1, by1 = block.bbox
    return bx0 >= x0 and bx1 <= x1 and by0 >= y0 and by1 <= y1


def looks_table_row_block(block: LayoutBlock) -> bool:
    if block.type not in {"paragraph", "table_candidate", "equation_candidate"}:
        return False
    text = block.source_text.strip()
    if not text:
        return False
    tokens = text.split()
    if len(tokens) < 4:
        return False
    numeric_tokens = re.findall(r"[-+]?\d+(?:\.\d+)?", text)
    alpha_words = re.findall(r"[A-Za-z]{3,}", text)
    row_markers = re.findall(r"(?:^|\s)\d{1,2}(?:\s|$)", text)
    numeric_ratio = len(numeric_tokens) / max(1, len(tokens))
    return len(numeric_tokens) >= 4 and numeric_ratio >= 0.35 and (row_markers or len(alpha_words) <= 6)


def merged_table_block(caption: LayoutBlock, rows: list[LayoutBlock]) -> LayoutBlock:
    group = [caption, *rows]
    bbox = union_layout_bbox(group)
    source_text = "\n".join(block.source_text for block in group if block.source_text.strip())
    asset = crop_asset_from_pdf(caption.page, bbox, "tables", f"{caption.id}_merged.png", padding=8)
    return LayoutBlock(
        id=caption.id,
        page=caption.page,
        order=caption.order,
        type="table_candidate",
        bbox=bbox,
        source_text=source_text,
        markdown=f"```text\n{source_text}\n```\n\n[table candidate: {asset}]",
        asset=asset,
        confidence="auto_merged_table_candidate",
    )


def merge_table_candidates(blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    result: list[LayoutBlock] = []
    consumed: set[str] = set()

    for index, block in enumerate(blocks):
        if block.id in consumed:
            continue
        if block.type != "caption" or not block.source_text.startswith("TABLE "):
            result.append(block)
            continue

        x0, y0, x1, _ = block.bbox
        window = (max(0, x0 - 60), y0, min(595, x1 + 80), y0 + 320)
        rows: list[LayoutBlock] = []
        for candidate in blocks[index + 1 :]:
            if candidate.page != block.page:
                break
            if candidate.type in {"heading", "caption", "figure_candidate"} and candidate.bbox[1] > block.bbox[1] + 10:
                break
            if candidate.type in {"paragraph", "table_candidate", "equation_candidate"} and block_in_window(candidate, window):
                if candidate.type == "equation_candidate" and not looks_table_row_block(candidate):
                    continue
                rows.append(candidate)
                consumed.add(candidate.id)

        result.append(merged_table_block(block, rows) if rows else block)

    return sorted(result, key=lambda item: (item.page, item.order))


def attach_figure_captions(blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    attached: list[LayoutBlock] = []
    consumed_captions: set[str] = set()
    for block in blocks:
        if block.id in consumed_captions:
            continue
        if block.type != "figure_candidate":
            attached.append(block)
            continue

        caption = next(
            (
                candidate
                for candidate in blocks
                if candidate.page == block.page
                and candidate.type == "caption"
                and candidate.source_text.startswith("Fig.")
                and 0 <= candidate.bbox[1] - block.bbox[3] <= 35
                and abs(candidate.bbox[0] - block.bbox[0]) < 80
            ),
            None,
        )
        if caption is None:
            attached.append(block)
            continue

        alt_text = caption.source_text
        markdown = f"![{alt_text}](../{block.asset})\n\n{alt_text}"
        attached.append(
            LayoutBlock(
                id=block.id,
                page=block.page,
                order=block.order,
                type="figure_candidate",
                bbox=block.bbox,
                source_text=caption.source_text,
                markdown=markdown,
                asset=block.asset,
                confidence="auto_figure_candidate_with_caption",
            )
        )
        consumed_captions.add(caption.id)
    return attached


def write_manifest(blocks: list[LayoutBlock]) -> None:
    MANIFEST_FILE.write_text(json.dumps([asdict(block) for block in blocks], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    global BASE_DIR, PAPER_DIR, DOCUMENT_SLUG, PDF_FILE, OUTPUT_DIR, ASSET_DIR, MANIFEST_FILE

    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-file", type=Path, default=PDF_FILE)
    parser.add_argument("--output-dir", type=Path, default=BASE_DIR)
    parser.add_argument("--document-slug", default=DOCUMENT_SLUG)
    args = parser.parse_args()

    BASE_DIR = args.output_dir.resolve()
    PAPER_DIR = args.pdf_file.resolve().parent
    DOCUMENT_SLUG = args.document_slug
    PDF_FILE = args.pdf_file.resolve()
    OUTPUT_DIR = BASE_DIR / "layout" / "auto"
    ASSET_DIR = OUTPUT_DIR / "assets"
    MANIFEST_FILE = OUTPUT_DIR / f"{DOCUMENT_SLUG}.auto_block_manifest.json"

    blocks = detect_blocks()
    blocks = relabel_table_regions(blocks)
    blocks = merge_equation_candidates(blocks)
    blocks = merge_table_candidates(blocks)
    blocks = attach_figure_captions(blocks)
    write_manifest(blocks)
    counts: dict[str, int] = {}
    for block in blocks:
        counts[block.type] = counts.get(block.type, 0) + 1
    print(MANIFEST_FILE)
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
