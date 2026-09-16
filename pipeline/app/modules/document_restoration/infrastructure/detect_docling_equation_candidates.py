#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import fitz

from app.modules.document_restoration.domain.bounding_box import (
    bbox_center_inside as center_inside,
    bbox_overlap_ratio as overlap_ratio,
)


BBox = tuple[float, float, float, float]


@dataclass
class DoclingFormula:
    id: str
    page: int
    bbox: BBox
    padded_bbox: BBox
    orig: str
    markdown: str
    asset: str
    matched_manifest_ids: list[str]
    status: str
    reason: str


@dataclass
class ManifestEquationReview:
    id: str
    page: int
    bbox: BBox
    source_text: str
    nearest_docling_formula: str | None
    max_overlap_ratio: float
    status: str
    reason: str


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def page_height(docling: dict[str, Any], page_no: int) -> float:
    page = docling["pages"][str(page_no)]
    return float(page["size"]["height"])


def convert_bbox(raw: dict[str, Any], height: float) -> BBox:
    x0 = float(raw["l"])
    x1 = float(raw["r"])
    if raw.get("coord_origin") == "BOTTOMLEFT":
        y0 = height - float(raw["t"])
        y1 = height - float(raw["b"])
    else:
        y0 = float(raw["t"])
        y1 = float(raw["b"])
    return normalize_bbox((x0, y0, x1, y1))


def normalize_bbox(bbox: BBox) -> BBox:
    x0, y0, x1, y1 = bbox
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def padded_bbox(bbox: BBox, page_rect: fitz.Rect, pad_x: float = 24.0, pad_y: float = 8.0) -> BBox:
    x0, y0, x1, y1 = bbox
    return (
        max(page_rect.x0, x0 - pad_x),
        max(page_rect.y0, y0 - pad_y),
        min(page_rect.x1, x1 + pad_x),
        min(page_rect.y1, y1 + pad_y),
    )


def area(bbox: BBox) -> float:
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def intersection(a: BBox, b: BBox) -> BBox:
    return (
        max(a[0], b[0]),
        max(a[1], b[1]),
        min(a[2], b[2]),
        min(a[3], b[3]),
    )


def union_bbox(boxes: list[BBox]) -> BBox | None:
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def crop(pdf: fitz.Document, page_no: int, bbox: BBox, output: Path) -> None:
    page = pdf[page_no - 1]
    matrix = fitz.Matrix(2.0, 2.0)
    pixmap = page.get_pixmap(matrix=matrix, clip=fitz.Rect(bbox), alpha=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(output)


def formula_text(item: dict[str, Any]) -> str:
    return (item.get("orig") or item.get("text") or "").strip()


def extract_docling_formulas(docling: dict[str, Any], pdf: fitz.Document, output_dir: Path) -> list[DoclingFormula]:
    formulas: list[DoclingFormula] = []
    index = 1
    for item in docling.get("texts", []):
        if item.get("label") != "formula" or not item.get("prov"):
            continue
        prov = item["prov"][0]
        page_no = int(prov["page_no"])
        bbox = convert_bbox(prov["bbox"], page_height(docling, page_no))
        expanded = padded_bbox(bbox, pdf[page_no - 1].rect)
        formula_id = f"docling_formula_p{page_no:02d}_{index:03d}"
        asset = Path("assets") / "docling_equations" / f"{formula_id}.png"
        crop(pdf, page_no, expanded, output_dir / asset)
        formulas.append(
            DoclingFormula(
                id=formula_id,
                page=page_no,
                bbox=bbox,
                padded_bbox=expanded,
                orig=formula_text(item),
                markdown=f"```text\n{formula_text(item)}\n```\n\n[equation crop: {asset.as_posix()}]",
                asset=asset.as_posix(),
                matched_manifest_ids=[],
                status="pending",
                reason="",
            )
        )
        index += 1
    return formulas


def manifest_equations(manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [block for block in manifest if block.get("type") == "equation_candidate"]


def review_formulas(
    formulas: list[DoclingFormula], equations: list[dict[str, Any]]
) -> tuple[list[DoclingFormula], list[ManifestEquationReview]]:
    manifest_reviews: list[ManifestEquationReview] = []
    formula_matches: dict[str, list[dict[str, Any]]] = {formula.id: [] for formula in formulas}

    for equation in equations:
        eq_bbox = tuple(float(value) for value in equation["bbox"])
        overlaps: list[tuple[DoclingFormula, float]] = []
        for formula in formulas:
            if formula.page != equation["page"]:
                continue
            ratio = overlap_ratio(eq_bbox, formula.padded_bbox)
            if ratio >= 0.25 or center_inside(eq_bbox, formula.padded_bbox):
                overlaps.append((formula, ratio))
                formula_matches[formula.id].append(equation)

        best = max(overlaps, key=lambda item: item[1], default=None)
        if best is None:
            manifest_reviews.append(
                ManifestEquationReview(
                    id=equation["id"],
                    page=int(equation["page"]),
                    bbox=eq_bbox,
                    source_text=equation.get("source_text", ""),
                    nearest_docling_formula=None,
                    max_overlap_ratio=0.0,
                    status="needs_sllm_review",
                    reason="Docling formula bbox와 겹치지 않는 기존 수식 후보입니다.",
                )
            )
        else:
            manifest_reviews.append(
                ManifestEquationReview(
                    id=equation["id"],
                    page=int(equation["page"]),
                    bbox=eq_bbox,
                    source_text=equation.get("source_text", ""),
                    nearest_docling_formula=best[0].id,
                    max_overlap_ratio=round(best[1], 4),
                    status="supported_by_docling_formula",
                    reason="기존 수식 후보 중심 또는 면적이 Docling formula bbox 안에 있습니다.",
                )
            )

    for formula in formulas:
        matches = formula_matches[formula.id]
        formula.matched_manifest_ids = [match["id"] for match in matches]
        matched_boxes = [tuple(float(value) for value in match["bbox"]) for match in matches]
        merged = union_bbox(matched_boxes)
        formula_area = area(formula.padded_bbox)
        merged_coverage = 0.0 if merged is None or formula_area == 0 else area(intersection(merged, formula.padded_bbox)) / formula_area
        if not matches:
            formula.status = "missing_from_manifest"
            formula.reason = "Docling은 formula로 봤지만 기존 manifest에는 대응 수식 후보가 없습니다."
        elif len(matches) > 1:
            formula.status = "fragmented_in_manifest"
            formula.reason = f"기존 manifest가 같은 Docling formula를 {len(matches)}개 후보로 쪼갰습니다."
        elif merged_coverage < 0.55:
            formula.status = "partial_manifest_crop"
            formula.reason = "기존 후보 crop이 Docling formula bbox의 일부만 덮습니다."
        else:
            formula.status = "covered_by_manifest"
            formula.reason = "기존 manifest의 수식 후보가 Docling formula bbox를 충분히 덮습니다."

    return formulas, manifest_reviews


def write_markdown(output_path: Path, formulas: list[DoclingFormula], reviews: list[ManifestEquationReview]) -> None:
    status_counts: dict[str, int] = {}
    for formula in formulas:
        status_counts[formula.status] = status_counts.get(formula.status, 0) + 1
    review_counts: dict[str, int] = {}
    for review in reviews:
        review_counts[review.status] = review_counts.get(review.status, 0) + 1

    lines = [
        "# Docling 수식 후보 진단",
        "",
        "Docling `formula` bbox를 기준으로 기존 manifest의 `equation_candidate`를 비교한 결과입니다.",
        "",
        "## 요약",
        "",
    ]
    for key in sorted(status_counts):
        lines.append(f"- Docling formula `{key}`: {status_counts[key]}")
    for key in sorted(review_counts):
        lines.append(f"- 기존 manifest equation `{key}`: {review_counts[key]}")

    lines.extend(["", "## Docling Formula", ""])
    for formula in formulas:
        lines.extend(
            [
                f"### {formula.id}",
                "",
                f"- page: {formula.page}",
                f"- status: `{formula.status}`",
                f"- reason: {formula.reason}",
                f"- bbox: {[round(v, 2) for v in formula.bbox]}",
                f"- matched_manifest_ids: {formula.matched_manifest_ids}",
                f"- orig: `{formula.orig}`",
                f"- asset: `{formula.asset}`",
                "",
            ]
        )

    needs_review = [review for review in reviews if review.status == "needs_sllm_review"]
    lines.extend(["## SLLM 검토 대상 기존 후보", ""])
    if not needs_review:
        lines.append("- 없음")
    for review in needs_review:
        lines.extend(
            [
                f"- `{review.id}` page {review.page}, bbox {[round(v, 2) for v in review.bbox]}",
                f"  - source_text: `{review.source_text}`",
            ]
        )
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--document-slug", required=True)
    parser.add_argument("--docling-json", type=Path)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve() / "layout" / "auto"
    manifest_path = output_dir / f"{args.document_slug}.auto_block_manifest.json"
    docling_path = args.docling_json or output_dir / "docling_ocr_baseline" / "docling.json"
    candidate_dir = output_dir / "docling_equation_candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)

    docling = load_json(docling_path)
    manifest = load_json(manifest_path)
    pdf = fitz.open(args.pdf_file)
    formulas = extract_docling_formulas(docling, pdf, output_dir)
    formulas, reviews = review_formulas(formulas, manifest_equations(manifest))

    summary = {
        "pdf_file": str(args.pdf_file),
        "manifest": str(manifest_path),
        "docling_json": str(docling_path),
        "formulas": [asdict(formula) for formula in formulas],
        "manifest_equation_reviews": [asdict(review) for review in reviews],
    }
    (candidate_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(candidate_dir / "summary.md", formulas, reviews)
    print(candidate_dir / "summary.json")
    print(json.dumps({
        "docling_formulas": len(formulas),
        "manifest_equations": len(reviews),
        "needs_sllm_review": sum(1 for review in reviews if review.status == "needs_sllm_review"),
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
