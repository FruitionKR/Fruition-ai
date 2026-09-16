from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shlex
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

import fitz


SPECIAL_TYPES = {
    "formula": "equation_candidate",
    "table": "table_candidate",
    "picture": "figure_candidate",
}
LIGATURES = "ﬀﬁﬂﬃﬄﬅﬆ"


def normalized_words(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text)
    return re.findall(r"[^\W_]+", normalized, re.UNICODE)


def parse_heron_regions(output: str) -> list[dict[str, Any]]:
    regions = []
    page_orders: dict[int, int] = {}
    for line in output.splitlines():
        fields = line.split("\t")
        if not fields or fields[0] != "REGION":
            continue
        if len(fields) != 9 or fields[3] not in SPECIAL_TYPES:
            raise ValueError(f"잘못된 Heron detector 출력: {line}")
        _, _, page, label, score, left, top, right, bottom = fields
        page_number = int(page)
        page_orders[page_number] = page_orders.get(page_number, 0) + 1
        order = page_orders[page_number]
        regions.append(
            {
                "id": f"heron_{label}_p{page_number:03d}_{order:03d}",
                "page": page_number,
                "order": order,
                "type": SPECIAL_TYPES[label],
                "bbox": [float(left), float(top), float(right), float(bottom)],
                "source_text": "",
                "markdown": "",
                "confidence": "docling.rs Heron INT8",
                "score": float(score),
            }
        )
    for index, region in enumerate(regions, start=1):
        region["token"] = f"XQ{index:03d}QX"
        region["replacement_required"] = region["type"] != "figure_candidate"
    return regions


def run_heron(
    pdf_file: Path,
    command: str,
    model: Path | None,
    pdfium_library: Path | None,
) -> tuple[list[dict[str, Any]], str]:
    environment = os.environ.copy()
    if model:
        environment["DOCLING_LAYOUT_ONNX"] = str(model)
    if pdfium_library:
        environment["PDFIUM_DYNAMIC_LIB_PATH"] = str(pdfium_library)
    try:
        result = subprocess.run(
            [*shlex.split(command), str(pdf_file)],
            capture_output=True,
            check=True,
            env=environment,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Heron detector를 실행할 수 없습니다: {command}") from exc
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or "").strip().splitlines()
        detail = next(
            (line for line in reversed(message) if not line.startswith("note:")),
            f"exit {exc.returncode}",
        )
        raise RuntimeError(f"Heron detector 실행 실패: {detail}") from exc
    return parse_heron_regions(result.stdout), result.stderr


def missing_unicode_maps(document: fitz.Document, page_number: int) -> list[str]:
    missing = set()
    for font in document[page_number - 1].get_fonts(full=True):
        xref, _, kind, base_font, _, encoding, *_ = font
        if (
            kind == "Type0"
            and encoding == "Identity-H"
            and document.xref_get_key(xref, "ToUnicode")[0] == "null"
        ):
            missing.add(base_font)
    return sorted(missing)


def font_key(name: str) -> str:
    normalized = re.sub(r"^[A-Z]{6}\+", "", name).lower()
    return re.sub(r"[^a-z0-9]", "", normalized).replace("identityh", "")


def missing_unicode_usage(
    page: fitz.Page,
    missing_fonts: list[str],
) -> int:
    missing_keys = {font_key(name) for name in missing_fonts}
    return sum(
        1
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
        if font_key(span["font"]) in missing_keys
    )


def repair_ligatures(markdown: str, page: fitz.Page) -> str:
    candidates: dict[str, dict[str, int]] = {}
    for word in page.get_text("words"):
        value = str(word[4])
        if not any(character in value for character in LIGATURES):
            continue
        dropped = "".join(character for character in value if character not in LIGATURES)
        normalized = "".join(
            unicodedata.normalize("NFKC", character)
            if character in LIGATURES
            else character
            for character in value
        )
        variants = candidates.setdefault(dropped, {})
        variants[normalized] = variants.get(normalized, 0) + 1

    for dropped, variants in candidates.items():
        if not any(character.isalnum() for character in dropped) or len(variants) != 1:
            continue
        normalized, expected = next(iter(variants.items()))
        markdown = re.sub(
            rf"(?<!\w){re.escape(dropped)}(?!\w)",
            normalized,
            markdown,
            count=expected,
        )
    return markdown


def write_crop(
    document: fitz.Document,
    region: dict[str, Any],
    output_file: Path,
) -> None:
    page = document[int(region["page"]) - 1]
    rect = fitz.Rect(*(float(value) for value in region["bbox"]))
    page.get_pixmap(
        matrix=fitz.Matrix(3, 3),
        clip=rect,
        alpha=False,
    ).save(output_file)


def write_body_page(
    source: fitz.Document,
    page_number: int,
    regions: list[dict[str, Any]],
    pdf_file: Path,
    image_file: Path,
) -> None:
    target = fitz.open()
    target.insert_pdf(source, from_page=page_number - 1, to_page=page_number - 1)
    page = target[0]
    for region in regions:
        page.add_redact_annot(fitz.Rect(region["bbox"]), fill=(1, 1, 1))
    if regions:
        page.apply_redactions(images=0, graphics=0)
    for region in regions:
        rect = fitz.Rect(region["bbox"])
        page.insert_text(
            (rect.x0, rect.y0 + 5),
            str(region["token"]),
            fontsize=4,
            fontname="helv",
            color=(0, 0, 0),
        )
    page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False).save(image_file)
    target.save(pdf_file, garbage=4, deflate=True)
    target.close()


def run_anydoc(command: str, input_file: Path, output_file: Path, log_file: Path) -> int:
    output_file.write_text("", encoding="utf-8")
    try:
        result = subprocess.run(
            [*shlex.split(command), str(input_file), "-o", str(output_file)],
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"AnyDoc을 실행할 수 없습니다: {command}") from exc
    log_file.write_text(result.stdout + result.stderr, encoding="utf-8")
    return result.returncode


def select_body_pages(blocks: list[dict[str, Any]], budget: float) -> None:
    if not 0 < budget <= 1:
        raise ValueError("body AI budget은 0보다 크고 1 이하여야 합니다.")
    target = round(len(blocks) * budget)
    broken = [block for block in blocks if block["body_broken"]]
    remaining = sorted(
        (block for block in blocks if not block["body_broken"]),
        key=lambda block: (float(block["body_difference"]), -int(block["page"])),
        reverse=True,
    )
    selected = broken + remaining[: max(0, target - len(broken))]
    for block in selected:
        block["text_decision"] = "needs_text_adjudication"


def relative_asset(output_file: Path, output_dir: Path, asset: str) -> str:
    return Path(os.path.relpath(output_dir / asset, output_file.parent)).as_posix()


def recovered_text(output_dir: Path, block: dict[str, Any]) -> str | None:
    text_block = block["type"] == "paragraph"
    directory = "text_recovered" if text_block else "recovered_blocks"
    path = output_dir / "layout" / "auto" / directory / f"{block['id']}.md"
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def region_markdown(
    output_dir: Path,
    output_file: Path,
    region: dict[str, Any],
) -> str:
    asset = relative_asset(output_file, output_dir, str(region["asset"]))
    if region["type"] == "figure_candidate":
        return f"![figure]({asset})"
    recovered = recovered_text(output_dir, region)
    if recovered:
        return recovered
    label = "표" if region["type"] == "table_candidate" else "수식"
    return f"> {label} 자동 복원 실패\n\n[source crop]({asset})"


def assemble(manifest_file: Path, output_dir: Path, output_file: Path) -> None:
    blocks = json.loads(manifest_file.read_text(encoding="utf-8"))
    bodies = {int(block["page"]): block for block in blocks if block["type"] == "paragraph"}
    regions_by_page: dict[int, list[dict[str, Any]]] = {}
    for block in blocks:
        if block["type"] != "paragraph":
            regions_by_page.setdefault(int(block["page"]), []).append(block)

    chunks = []
    for page_number, body in sorted(bodies.items()):
        regions = sorted(regions_by_page.get(page_number, []), key=lambda item: item["order"])
        content = recovered_text(output_dir, body)
        if content is None and body["body_broken"]:
            content = (
                f"> 본문 자동 복원 실패\n\n{body['fallback_text']}"
            ).strip()
        elif content is None:
            content = str(body["source_text"])
        for region in regions:
            token = str(region["token"])
            replacement = region_markdown(output_dir, output_file, region)
            if token in content:
                before, after = content.split(token, 1)
                content = f"{before}{replacement}{after.replace(token, '')}"
            else:
                content = f"{content.rstrip()}\n\n{replacement}".strip()
        chunks.append(f"<!-- page {page_number} -->\n\n{content.strip()}")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("\n\n".join(chunks).strip() + "\n", encoding="utf-8")


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    crop_root = args.output_dir / "layout" / "crop_first"
    directories = {
        "body_pages": crop_root / "body_pages",
        "body_images": crop_root / "body_images",
        "body_markdown": crop_root / "body_markdown",
        "figures": crop_root / "assets" / "figures",
        "specials": crop_root / "assets" / "specials",
        "logs": crop_root / "logs",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)

    regions, heron_log = run_heron(
        args.pdf_file,
        args.heron_command,
        args.heron_model,
        args.pdfium_library,
    )
    (directories["logs"] / "heron.log").write_text(heron_log, encoding="utf-8")
    bodies = []
    with fitz.open(args.pdf_file) as document:
        for region in regions:
            target = directories[
                "figures" if region["type"] == "figure_candidate" else "specials"
            ] / f"{region['id']}.png"
            write_crop(document, region, target)
            region["asset"] = str(target.relative_to(args.output_dir))

        for page_number in range(1, document.page_count + 1):
            page_regions = [
                region for region in regions if int(region["page"]) == page_number
            ]
            body_pdf = directories["body_pages"] / f"page-{page_number:03d}.pdf"
            body_image = directories["body_images"] / f"page-{page_number:03d}.png"
            body_markdown = (
                directories["body_markdown"] / f"page-{page_number:03d}.md"
            )
            write_body_page(
                document,
                page_number,
                page_regions,
                body_pdf,
                body_image,
            )
            exit_code = run_anydoc(
                args.anydoc_command,
                body_pdf,
                body_markdown,
                directories["logs"] / f"anydoc-page-{page_number:03d}.log",
            )
            draft = repair_ligatures(
                body_markdown.read_text(encoding="utf-8"),
                document[page_number - 1],
            )
            body_markdown.write_text(draft, encoding="utf-8")
            required_tokens = [str(region["token"]) for region in page_regions]
            missing_fonts = missing_unicode_maps(document, page_number)
            reasons = []
            if exit_code != 0 or not draft.strip():
                reasons.append("anydoc_failed")
            if any(draft.count(token) != 1 for token in required_tokens):
                reasons.append("crop_token_mismatch")
            if "\ufffd" in draft:
                reasons.append("replacement_character")
            with fitz.open(body_pdf) as body_document:
                if missing_unicode_usage(body_document[0], missing_fonts):
                    reasons.append("unresolved_font_mapping")
                fallback_text = body_document[0].get_text("text", sort=False).strip()
                source_words = normalized_words(fallback_text)
            draft_words = normalized_words(draft)
            bodies.append(
                {
                    "id": f"anydoc_body_p{page_number:03d}",
                    "page": page_number,
                    "order": 0,
                    "type": "paragraph",
                    "bbox": [
                        0.0,
                        0.0,
                        float(document[page_number - 1].rect.width),
                        float(document[page_number - 1].rect.height),
                    ],
                    "source_text": draft,
                    "fallback_text": fallback_text,
                    "markdown": draft,
                    "asset": str(body_image.relative_to(args.output_dir)),
                    "confidence": "anydoc_crop_first",
                    "scope": "page_body",
                    "required_tokens": required_tokens,
                    "replacement_required": bool(reasons),
                    "body_broken": bool(reasons),
                    "body_reasons": reasons,
                    "body_difference": 1
                    - difflib.SequenceMatcher(
                        None,
                        source_words,
                        draft_words,
                        autojunk=False,
                    ).ratio(),
                }
            )

    select_body_pages(bodies, args.body_ai_budget)
    blocks = sorted([*bodies, *regions], key=lambda block: (block["page"], block["order"]))
    args.manifest_file.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_file.write_text(
        json.dumps(blocks, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    detected = []
    for body in bodies:
        detected.extend(
            [
                f"## Page {body['page']}",
                "",
                (
                    f"<!-- {body['id']} type=paragraph bbox={body['bbox']} "
                    f"confidence={body['confidence']} -->"
                ),
                str(body["source_text"]),
                "",
            ]
        )
    args.detected_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.detected_markdown.write_text("\n".join(detected).rstrip() + "\n", encoding="utf-8")
    summary = {
        "pages": len(bodies),
        "body_ai_pages": sum("text_decision" in body for body in bodies),
        "broken_body_pages": sum(body["body_broken"] for body in bodies),
        "tables": sum(region["type"] == "table_candidate" for region in regions),
        "equations": sum(region["type"] == "equation_candidate" for region in regions),
        "figures": sum(region["type"] == "figure_candidate" for region in regions),
    }
    (crop_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-file", type=Path, required=True)
    parser.add_argument("--manifest-file", type=Path, required=True)
    parser.add_argument("--detected-markdown", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output-file", type=Path)
    parser.add_argument("--anydoc-command", default="anydoc")
    parser.add_argument("--heron-command", default="raw-special-regions")
    parser.add_argument("--heron-model", type=Path)
    parser.add_argument("--pdfium-library", type=Path)
    parser.add_argument("--body-ai-budget", type=float, default=0.3)
    parser.add_argument("--assemble-only", action="store_true")
    args = parser.parse_args()
    args.pdf_file = args.pdf_file.resolve()
    args.manifest_file = args.manifest_file.resolve()
    args.detected_markdown = args.detected_markdown.resolve()
    args.output_dir = args.output_dir.resolve()
    if args.heron_model:
        args.heron_model = args.heron_model.resolve()
    if args.pdfium_library:
        args.pdfium_library = args.pdfium_library.resolve()
    if args.assemble_only:
        output_file = args.output_file or (
            args.output_dir / "final" / f"{args.pdf_file.stem}.restored.md"
        )
        assemble(args.manifest_file, args.output_dir, output_file.resolve())
        return
    print(json.dumps(prepare(args), ensure_ascii=False))


if __name__ == "__main__":
    main()
