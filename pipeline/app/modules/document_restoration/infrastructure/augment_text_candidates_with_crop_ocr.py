from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import fitz

from app.modules.document_restoration.domain.bounding_box import bbox_match_score
from app.modules.document_restoration.infrastructure.docling_io import (
    item_page_and_bbox,
    load_docling,
    resolve_pdf_file,
)
from app.modules.document_restoration.infrastructure.process_auto_layout_blocks import (
    normalize_text,
)


def union_bbox(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def caption_bbox_for_figure(
    block: dict[str, Any],
    docling: dict[str, Any],
) -> tuple[int, tuple[float, float, float, float]] | None:
    pages = docling.get("pages", {})
    texts_by_ref = {str(item.get("self_ref")): item for item in docling.get("texts", [])}
    block_bbox = tuple(float(value) for value in block.get("bbox") or [])
    if len(block_bbox) != 4:
        return None
    matches = []
    for picture in docling.get("pictures", []):
        page_bbox = item_page_and_bbox(picture, pages)
        if not page_bbox:
            continue
        page, bbox = page_bbox
        if page != int(block.get("page", 0)):
            continue
        score = bbox_match_score(block_bbox, bbox)
        if score >= 0.55:
            matches.append((score, picture))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    caption_boxes = []
    page_no = int(block.get("page", 0))
    for caption in matches[0][1].get("captions") or []:
        item = texts_by_ref.get(str(caption.get("$ref")))
        if not item:
            continue
        page_bbox = item_page_and_bbox(item, pages)
        if not page_bbox:
            continue
        page_no, bbox = page_bbox
        caption_boxes.append(bbox)
    if not caption_boxes:
        return None
    return page_no, union_bbox(caption_boxes)


def crop_bbox(
    pdf: fitz.Document,
    base_dir: Path,
    block_id: str,
    page: int,
    bbox: tuple[float, float, float, float],
    padding: float,
) -> str:
    page_obj = pdf[page - 1]
    rect = fitz.Rect(bbox)
    rect = fitz.Rect(
        max(0, rect.x0 - padding),
        max(0, rect.y0 - padding),
        min(page_obj.rect.width, rect.x1 + padding),
        min(page_obj.rect.height, rect.y1 + padding),
    )
    output_dir = base_dir / "layout" / "auto" / "assets" / "text_ocr"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{block_id}.png"
    pixmap = page_obj.get_pixmap(matrix=fitz.Matrix(4, 4), clip=rect, alpha=False)
    pixmap.save(output_file)
    return str(output_file.relative_to(base_dir))


def run_tesseract(image_file: Path, psm: str) -> str:
    output = subprocess.run(
        ["tesseract", str(image_file), "stdout", "--psm", psm],
        check=False,
        capture_output=True,
        text=True,
    )
    return output.stdout.strip()


def text_quality(text: str) -> float:
    normalized = normalize_text(text)
    if not normalized:
        return -100.0
    alpha = sum(1 for char in normalized if char.isalpha())
    symbol_ratio = sum(1 for char in normalized if char in "$%&'()*;<=>@[\\]^_`{|}~") / max(1, len(normalized))
    vowel_ratio = sum(1 for char in normalized.lower() if char in "aeiou") / max(1, alpha)
    return alpha / max(1, len(normalized)) + vowel_ratio * 0.2 - symbol_ratio


def clean_ocr_text(text: str) -> str:
    lines = [normalize_text(line) for line in text.splitlines()]
    return normalize_text(" ".join(line for line in lines if line))


def append_candidate(block: dict[str, Any], source: str, text: str, asset: str) -> bool:
    cleaned = clean_ocr_text(text)
    if not cleaned:
        return False
    candidates = block.setdefault("text_candidates", [])
    existing = {normalize_text(str(candidate.get("text", ""))) for candidate in candidates}
    if cleaned in existing:
        return False
    candidates.append(
        {
            "source": source,
            "text": cleaned,
            "quality": round(text_quality(cleaned), 4),
            "match_score": 0.0,
            "asset": asset,
        }
    )
    return True


def crop_target_for_block(block: dict[str, Any], docling: dict[str, Any]) -> tuple[int, tuple[float, float, float, float], float] | None:
    if block.get("type") == "figure_candidate":
        caption_target = caption_bbox_for_figure(block, docling)
        if caption_target:
            page, bbox = caption_target
            return page, bbox, 2.0
    bbox = tuple(float(value) for value in block.get("bbox") or [])
    if len(bbox) != 4:
        return None
    return int(block["page"]), bbox, 2.0


def should_ocr_block(block: dict[str, Any], all_text_blocks: bool) -> bool:
    if block.get("type") == "figure_candidate":
        return block.get("text_decision") == "needs_text_adjudication"
    if block.get("type") in {"paragraph", "heading"}:
        return all_text_blocks or block.get("text_decision") == "needs_text_adjudication"
    return False


def augment_manifest(base_dir: Path, document_slug: str, manifest_file: Path, all_text_blocks: bool) -> tuple[int, int]:
    docling = load_docling(base_dir)
    pdf_file = resolve_pdf_file(base_dir, docling, document_slug)
    blocks = json.loads(manifest_file.read_text(encoding="utf-8"))
    target_count = 0
    added_count = 0
    with fitz.open(pdf_file) as pdf:
        for block in blocks:
            if not should_ocr_block(block, all_text_blocks):
                continue
            target = crop_target_for_block(block, docling)
            if not target:
                continue
            target_count += 1
            page, bbox, padding = target
            asset = crop_bbox(pdf, base_dir, block["id"], page, bbox, padding)
            block["text_ocr_asset"] = asset
            image_file = base_dir / asset
            for psm in ("7", "6", "11"):
                text = run_tesseract(image_file, psm)
                if append_candidate(block, f"crop_ocr_psm{psm}", text, asset):
                    added_count += 1
    manifest_file.write_text(json.dumps(blocks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target_count, added_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--document-slug", required=True)
    parser.add_argument("--manifest-file", type=Path, required=True)
    parser.add_argument("--all-text-blocks", action="store_true")
    args = parser.parse_args()

    targets, added = augment_manifest(
        args.output_dir.resolve(),
        args.document_slug,
        args.manifest_file.resolve(),
        args.all_text_blocks,
    )
    print(f"targets={targets}")
    print(f"added_candidates={added}")


if __name__ == "__main__":
    main()
