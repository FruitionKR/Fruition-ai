#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import fitz


from app.modules.document_restoration.domain.text_quality import (
    language_score,
    looks_glyph_encoded,
)
from app.modules.document_restoration.domain.bounding_box import bbox_match_score
from app.modules.document_restoration.infrastructure.docling_io import (
    item_page_and_bbox,
    load_docling,
    resolve_pdf_file,
)
from app.modules.document_restoration.infrastructure.process_auto_layout_blocks import (
    normalize_text,
)


FIGURE_CAPTION_PATTERN = re.compile(r"^(?:fig(?:ure)?\.?)\s*\d+\b", flags=re.IGNORECASE)


def load_formula_summary(base_dir: Path) -> list[dict[str, Any]]:
    summary_file = base_dir / "layout" / "auto" / "docling_equation_candidates" / "summary.json"
    if not summary_file.exists():
        return []
    data = json.loads(summary_file.read_text(encoding="utf-8"))
    return data.get("formulas", [])


def ref_index(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("self_ref")): item for item in items}


def ordered_body_refs(docling: dict[str, Any]) -> list[str]:
    groups_by_ref = ref_index(docling.get("groups", []))
    visited_groups: set[str] = set()
    refs: list[str] = []

    def append_children(children: list[dict[str, Any]]) -> None:
        for child in children:
            ref = child.get("$ref")
            if not ref:
                continue
            group = groups_by_ref.get(ref)
            if group is None:
                refs.append(ref)
                continue
            if ref in visited_groups:
                continue
            visited_groups.add(ref)
            append_children(group.get("children", []))

    append_children(docling.get("body", {}).get("children", []))
    return refs


def clean_text(text: str) -> str:
    return normalize_text(text)


def crop_asset(
    pdf: fitz.Document,
    base_dir: Path,
    page: int,
    bbox: tuple[float, float, float, float],
    subdir: str,
    filename: str,
    padding: float = 0.0,
) -> str:
    page_obj = pdf[page - 1]
    rect = fitz.Rect(bbox)
    if padding > 0:
        rect = fitz.Rect(
            max(0, rect.x0 - padding),
            max(0, rect.y0 - padding),
            min(page_obj.rect.width, rect.x1 + padding),
            min(page_obj.rect.height, rect.y1 + padding),
        )
    output_dir = base_dir / "layout" / "auto" / "assets" / subdir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / filename
    pixmap = page_obj.get_pixmap(matrix=fitz.Matrix(3, 3), clip=rect, alpha=False)
    pixmap.save(output_file)
    return str(output_file.relative_to(base_dir))


def caption_text(item: dict[str, Any], texts_by_ref: dict[str, dict[str, Any]]) -> str:
    captions = []
    for caption in item.get("captions") or []:
        ref = caption.get("$ref")
        if ref and ref in texts_by_ref:
            captions.append(clean_text(texts_by_ref[ref].get("text") or texts_by_ref[ref].get("orig") or ""))
    return normalize_text(" ".join(captions))


def axis_overlap_ratio(first_start: float, first_end: float, second_start: float, second_end: float) -> float:
    overlap = max(0.0, min(first_end, second_end) - max(first_start, second_start))
    shorter = min(first_end - first_start, second_end - second_start)
    return overlap / shorter if shorter > 0 else 0.0


def caption_proximity(
    picture_bbox: tuple[float, float, float, float],
    caption_bbox: tuple[float, float, float, float],
) -> tuple[int, float, float] | None:
    horizontal_overlap = axis_overlap_ratio(picture_bbox[0], picture_bbox[2], caption_bbox[0], caption_bbox[2])
    vertical_overlap = axis_overlap_ratio(picture_bbox[1], picture_bbox[3], caption_bbox[1], caption_bbox[3])
    below_gap = caption_bbox[1] - picture_bbox[3]
    if 0 <= below_gap <= 48 and horizontal_overlap >= 0.15:
        return (0, below_gap, -horizontal_overlap)

    horizontal_gap = max(caption_bbox[0] - picture_bbox[2], picture_bbox[0] - caption_bbox[2], 0.0)
    if horizontal_gap <= 36 and vertical_overlap >= 0.15:
        return (1, horizontal_gap, -vertical_overlap)
    return None


def associate_picture_captions(
    pictures: list[dict[str, Any]],
    texts_by_ref: dict[str, dict[str, Any]],
    pages: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    all_captions = {
        ref: text
        for ref, text in texts_by_ref.items()
        if text.get("label") == "caption"
    }
    figure_captions = {
        ref: text
        for ref, text in all_captions.items()
        if FIGURE_CAPTION_PATTERN.match(clean_text(text.get("text") or text.get("orig") or ""))
    }
    associations: dict[str, dict[str, Any]] = {}
    used_caption_refs: set[str] = set()

    for picture in pictures:
        picture_ref = str(picture.get("self_ref"))
        for caption_ref_data in picture.get("captions") or []:
            caption_ref = str(caption_ref_data.get("$ref"))
            caption = all_captions.get(caption_ref)
            if caption is None:
                continue
            associations[picture_ref] = caption
            used_caption_refs.add(caption_ref)
            break

    matches: list[tuple[tuple[int, float, float], str, str]] = []
    for picture in pictures:
        picture_ref = str(picture.get("self_ref"))
        if picture_ref in associations:
            continue
        picture_position = item_page_and_bbox(picture, pages)
        if picture_position is None:
            continue
        picture_page, picture_bbox = picture_position
        for caption_ref, caption in figure_captions.items():
            if caption_ref in used_caption_refs:
                continue
            caption_position = item_page_and_bbox(caption, pages)
            if caption_position is None or caption_position[0] != picture_page:
                continue
            proximity = caption_proximity(picture_bbox, caption_position[1])
            if proximity is not None:
                matches.append((proximity, picture_ref, caption_ref))

    assigned_picture_refs = set(associations)
    for _, picture_ref, caption_ref in sorted(matches):
        if picture_ref in assigned_picture_refs or caption_ref in used_caption_refs:
            continue
        associations[picture_ref] = figure_captions[caption_ref]
        assigned_picture_refs.add(picture_ref)
        used_caption_refs.add(caption_ref)
    return associations


def formula_for_bbox(formulas: list[dict[str, Any]], page: int, bbox: tuple[float, float, float, float]) -> dict[str, Any] | None:
    candidates = []
    for formula in formulas:
        if int(formula.get("page", 0)) != page:
            continue
        formula_bbox = tuple(float(value) for value in formula.get("bbox") or formula.get("padded_bbox") or [])
        if len(formula_bbox) != 4:
            continue
        score = bbox_match_score(bbox, formula_bbox)
        if score >= 0.55:
            candidates.append((score, formula))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def formula_block(
    item: dict[str, Any],
    pdf: fitz.Document,
    base_dir: Path,
    pages: dict[str, Any],
    formulas: list[dict[str, Any]],
    fallback_index: int,
    order: int,
) -> dict[str, Any] | None:
    page_bbox = item_page_and_bbox(item, pages)
    if not page_bbox:
        return None
    page, bbox = page_bbox
    matched = formula_for_bbox(formulas, page, bbox)
    block_id = str(matched.get("id")) if matched else f"docling_primary_formula_p{page:02d}_{fallback_index:03d}"
    asset = matched.get("asset") if matched else None
    if isinstance(asset, str) and asset.startswith("assets/"):
        asset = f"layout/auto/{asset}"
    if not asset or not (base_dir / asset).exists():
        asset = crop_asset(pdf, base_dir, page, bbox, "equations", f"{block_id}.png", padding=4.0)
    return {
        "id": block_id,
        "page": page,
        "order": order,
        "type": "equation_candidate",
        "bbox": [round(value, 2) for value in bbox],
        "source_text": clean_text(item.get("orig") or item.get("text") or ""),
        "markdown": "",
        "asset": asset,
        "confidence": "docling_primary_formula",
    }


def table_block(
    table: dict[str, Any],
    pdf: fitz.Document,
    base_dir: Path,
    pages: dict[str, Any],
    texts_by_ref: dict[str, dict[str, Any]],
    index: int,
    order: int,
) -> dict[str, Any] | None:
    page_bbox = item_page_and_bbox(table, pages)
    if not page_bbox:
        return None
    page, bbox = page_bbox
    block_id = f"docling_table_p{page:02d}_{index:03d}"
    caption = caption_text(table, texts_by_ref)
    cell_text = " ".join(
        clean_text(str(cell.get("text", "")))
        for cell in table.get("data", {}).get("table_cells", [])
        if cell.get("text")
    )
    source = normalize_text(" ".join(part for part in (caption, cell_text) if part))
    asset = crop_asset(pdf, base_dir, page, bbox, "tables", f"{block_id}.png", padding=4.0)
    return {
        "id": block_id,
        "page": page,
        "order": order,
        "type": "table_candidate",
        "bbox": [round(value, 2) for value in bbox],
        "source_text": source,
        "markdown": "",
        "asset": asset,
        "confidence": "docling_primary_table",
    }


def picture_block(
    picture: dict[str, Any],
    pdf: fitz.Document,
    base_dir: Path,
    pages: dict[str, Any],
    caption_item: dict[str, Any] | None,
    index: int,
    order: int,
) -> dict[str, Any] | None:
    page_bbox = item_page_and_bbox(picture, pages)
    if not page_bbox:
        return None
    page, bbox = page_bbox
    block_id = f"docling_picture_p{page:02d}_{index:03d}"
    caption = clean_text((caption_item or {}).get("text") or (caption_item or {}).get("orig") or "")
    caption_position = item_page_and_bbox(caption_item, pages) if caption_item else None
    caption_bbox = caption_position[1] if caption_position and caption_position[0] == page else None
    asset = crop_asset(pdf, base_dir, page, bbox, "figures", f"{block_id}.png", padding=2.0)
    caption_asset = None
    if caption_bbox is not None:
        caption_asset = crop_asset(
            pdf,
            base_dir,
            page,
            caption_bbox,
            "figure_captions",
            f"{block_id}.png",
            padding=4.0,
        )
    block = {
        "id": block_id,
        "page": page,
        "order": order,
        "type": "figure_candidate",
        "bbox": [round(value, 2) for value in bbox],
        "source_text": caption,
        "markdown": "",
        "asset": asset,
        "caption_asset": caption_asset,
        "caption_bbox": [round(value, 2) for value in caption_bbox] if caption_bbox else None,
        "caption_expected": caption_item is not None,
        "confidence": "docling_primary_picture",
    }
    if needs_text_adjudication(caption):
        block["text_decision"] = "needs_text_adjudication"
        block["text_candidates"] = [candidate_record("docling_caption", caption)]
    return block


def load_auxiliary_blocks(base_dir: Path, document_slug: str) -> list[dict[str, Any]]:
    candidates = [
        base_dir / "layout" / "auto" / f"{document_slug}.docling_formula_manifest.json",
        base_dir / "layout" / "auto" / f"{document_slug}.auto_block_manifest.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return []


def text_quality(text: str) -> float:
    return language_score(text)


def needs_text_adjudication(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if len(normalized) < 8:
        return False
    probe = re.sub(r"\[[0-9,\-\s]+\]", "", normalized)
    probe = re.sub(r"\b(?:doi|https?)[:/][^\s]+", "", probe, flags=re.IGNORECASE)
    symbol_runs = len(re.findall(r"[,$%&'()*/;<=>@\[\\\]^_`{|}~]{3,}", probe))
    quality = text_quality(normalized)
    return looks_glyph_encoded(probe) or symbol_runs > 0 or quality < 0.2


def candidate_record(source: str, text: str, score: float = 0.0) -> dict[str, Any]:
    return {
        "source": source,
        "text": text,
        "quality": round(text_quality(text), 4),
        "match_score": round(score, 4),
    }


def auxiliary_text_for_block(
    aux_blocks: list[dict[str, Any]],
    page: int,
    bbox: tuple[float, float, float, float],
    current_text: str,
) -> tuple[str | None, list[dict[str, Any]]]:
    matches = []
    for block in aux_blocks:
        if block.get("type") not in {"paragraph", "heading"}:
            continue
        if int(block.get("page", 0)) != page:
            continue
        candidate_bbox = tuple(float(value) for value in block.get("bbox") or [])
        if len(candidate_bbox) != 4:
            continue
        score = bbox_match_score(bbox, candidate_bbox)
        if score < 0.5:
            continue
        text = clean_text(block.get("source_text") or block.get("markdown") or "")
        if text:
            matches.append((score, text))
    if not matches:
        return None, []
    matches.sort(key=lambda item: (text_quality(item[1]), item[0]), reverse=True)
    best = matches[0][1]
    candidates = [candidate_record("auxiliary", text, score) for score, text in matches[:3]]
    if text_quality(best) > text_quality(current_text) + 0.15:
        return best, candidates
    return None, candidates


def best_text_for_docling_item(
    item: dict[str, Any],
    pages: dict[str, Any],
    aux_blocks: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], bool]:
    current = clean_text(item.get("text") or item.get("orig") or "")
    candidates = [candidate_record("docling", current)]
    if item.get("label") == "list_item":
        return current, candidates, needs_text_adjudication(current)
    page_bbox = item_page_and_bbox(item, pages)
    if not page_bbox:
        return current, candidates, needs_text_adjudication(current)
    page, bbox = page_bbox
    auxiliary, auxiliary_candidates = auxiliary_text_for_block(aux_blocks, page, bbox, current)
    candidates.extend(auxiliary_candidates)
    selected = auxiliary or current
    return selected, candidates, needs_text_adjudication(selected)


def text_block(
    item: dict[str, Any],
    pages: dict[str, Any],
    aux_blocks: list[dict[str, Any]],
    index: int,
    order: int,
) -> dict[str, Any] | None:
    if item.get("content_layer") == "furniture":
        return None
    label = item.get("label")
    if label in {"formula", "caption", "page_header", "page_footer"}:
        return None
    text, candidates, needs_adjudication = best_text_for_docling_item(item, pages, aux_blocks)
    if not text or re.fullmatch(r"_+", text):
        return None
    page_bbox = item_page_and_bbox(item, pages)
    if not page_bbox:
        return None
    page, bbox = page_bbox
    block_type = "heading" if label == "section_header" else "paragraph"
    block = {
        "id": f"docling_text_p{page:02d}_{index:03d}",
        "page": page,
        "order": order,
        "type": block_type,
        "bbox": [round(value, 2) for value in bbox],
        "source_text": text,
        "markdown": text,
        "asset": None,
        "confidence": f"docling_primary_{label}",
    }
    if needs_adjudication:
        block["text_decision"] = "needs_text_adjudication"
        block["text_candidates"] = candidates
    return block


def build_manifest(base_dir: Path, document_slug: str) -> list[dict[str, Any]]:
    docling = load_docling(base_dir)
    pdf_file = resolve_pdf_file(base_dir, docling, document_slug)
    pages = docling.get("pages", {})
    texts_by_ref = ref_index(docling.get("texts", []))
    tables_by_ref = ref_index(docling.get("tables", []))
    pictures_by_ref = ref_index(docling.get("pictures", []))
    picture_captions = associate_picture_captions(docling.get("pictures", []), texts_by_ref, pages)
    formulas = load_formula_summary(base_dir)
    aux_blocks = load_auxiliary_blocks(base_dir, document_slug)

    blocks: list[dict[str, Any]] = []
    counters = {"text": 0, "table": 0, "picture": 0, "formula": 0}

    with fitz.open(pdf_file) as pdf:
        for order, ref in enumerate(ordered_body_refs(docling), start=1):
            block = None
            if ref in texts_by_ref:
                item = texts_by_ref[ref]
                if item.get("label") == "formula":
                    counters["formula"] += 1
                    block = formula_block(item, pdf, base_dir, pages, formulas, counters["formula"], order)
                else:
                    counters["text"] += 1
                    block = text_block(item, pages, aux_blocks, counters["text"], order)
            elif ref in tables_by_ref:
                counters["table"] += 1
                block = table_block(tables_by_ref[ref], pdf, base_dir, pages, texts_by_ref, counters["table"], order)
            elif ref in pictures_by_ref:
                counters["picture"] += 1
                block = picture_block(
                    pictures_by_ref[ref],
                    pdf,
                    base_dir,
                    pages,
                    picture_captions.get(ref),
                    counters["picture"],
                    order,
                )

            if block:
                blocks.append(block)
    return blocks


def clean_stale_docling_table_recoveries(base_dir: Path) -> None:
    recovered_dir = base_dir / "layout" / "auto" / "recovered_blocks"
    evaluation_dir = base_dir / "layout" / "auto" / "evaluations"
    for directory, suffix in ((recovered_dir, ".md"), (evaluation_dir, ".json")):
        if not directory.exists():
            continue
        for path in directory.glob(f"docling_table_*{suffix}"):
            path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--document-slug", required=True)
    args = parser.parse_args()

    base_dir = args.output_dir.resolve()
    blocks = build_manifest(base_dir, args.document_slug)
    output_file = base_dir / "layout" / "auto" / f"{args.document_slug}.docling_primary_manifest.json"
    output_file.write_text(json.dumps(blocks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    clean_stale_docling_table_recoveries(base_dir)
    print(output_file)
    print(f"blocks={len(blocks)}")


if __name__ == "__main__":
    main()
