"""MCP 외부 AI 변환. AnyDoc 전처리 결과에서 기존 복원 단계가 공급사에 보내던 입력을 창(window)으로 만들고,
외부 AI가 낸 결과를 기존 검증(valid_replacement·clean_description)으로 걸러 기존 조립(assemble)로 Markdown을 만든다.

내부 공급사 호출은 하지 않는다. 창 하나는 selective repair의 요청 하나(쪽·종류·이어진 표 묶음) 또는 그림 하나다.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from app.modules.document_restoration.infrastructure.crop_first_with_anydoc import assemble
from app.modules.document_restoration.infrastructure.describe_figures_with_provider import (
    DESCRIPTION_DIR,
    FIGURE_SCHEMA,
    MAX_FIGURES,
    clean_description,
    is_too_small,
)
from app.modules.document_restoration.infrastructure.selective_repair_with_provider import (
    OUTPUT_SCHEMA,
    PROMPT_DIR,
    block_image,
    group_candidates,
    lane_prompt,
    markdown_fragments,
    page_markdown,
    rejected_candidates,
    repair_request,
    save_merged,
    save_replacements,
    select_candidates,
)

WINDOWS_FILE = Path("external") / "windows.json"
RECOVERY_SOURCE = "mcp_client"


def build_windows(pdf_file: Path, output_dir: Path, manifest_file: Path, detected_markdown: Path,
                  describe_figures: bool) -> list[dict[str, Any]]:
    """전처리 결과로 창 목록을 만들어 `external/windows.json`과 창 이미지 PNG를 쓴다."""
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    markdown = detected_markdown.read_text(encoding="utf-8")
    fragments, pages = markdown_fragments(markdown), page_markdown(markdown)
    prompts = {lane: lane_prompt(lane) for lane in ("text", "table", "equation")}
    image_dir = output_dir / "external" / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    windows: list[dict[str, Any]] = []

    def store(images: list[str]) -> list[str]:
        paths = []
        for data_url in images:
            index = len(list(image_dir.iterdir()))
            path = image_dir / f"{index:05d}.png"
            path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
            paths.append(str(path.relative_to(output_dir)))
        return paths

    for (page, lane, chain_head), blocks in group_candidates(select_candidates(manifest)).items():
        request = repair_request(
            pdf_file, lane, prompts[lane], blocks,
            chained=bool(chain_head),
            body_only=all(block.get("scope") == "page_body" for block in blocks),
            fragments=fragments, pages=pages, page_images={},
            block_images={block["id"]: block_image(output_dir, pdf_file, block) for block in blocks},
        )
        windows.append({
            "kind": "repair", "lane": lane, "page": page,
            # 이어진 표는 첫 조각만 묻는다(기존 복원과 같음). 나머지 조각은 첫 조각이 받아들여지면 합쳐진다.
            "result_block_ids": [blocks[0]["id"]] if chain_head else [block["id"] for block in blocks],
            "chain_block_ids": [block["id"] for block in blocks] if chain_head else [],
            "instructions": request["prompt"], "payload": request["payload"],
            "result_schema": OUTPUT_SCHEMA, "images": store(request["images"]),
        })
    if describe_figures:
        prompt = (PROMPT_DIR / "figure_description.md").read_text(encoding="utf-8")
        figures = [block for block in manifest if block["type"] == "figure_candidate" and not is_too_small(block)]
        for figure in figures[:MAX_FIGURES]:
            windows.append({
                "kind": "figure", "lane": "figure", "page": figure["page"],
                "result_block_ids": [figure["id"]], "chain_block_ids": [],
                "instructions": prompt, "payload": {"task": "figure_description", "page": figure["page"]},
                "result_schema": FIGURE_SCHEMA, "images": [str(figure["asset"])],
            })
    path = output_dir / WINDOWS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(windows, ensure_ascii=False) + "\n", encoding="utf-8")
    return windows


def load_context(manifest_file: Path, detected_markdown: Path):
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    return {block["id"]: block for block in manifest}, markdown_fragments(detected_markdown.read_text(encoding="utf-8"))


def validate_result(window: dict[str, Any], result: Any, blocks: dict[str, dict[str, Any]],
                    fragments: dict[str, str]) -> list[str]:
    """외부 AI 결과의 모양을 검사하고 기존 검증에서 떨어진 block id를 돌려준다. 모양이 틀리면 ValueError다."""
    if window["kind"] == "figure":
        if not isinstance(result, dict) or set(result) != {"alt", "description"} \
                or not all(isinstance(result[key], str) for key in result):
            raise ValueError("figure result must be {alt, description}")
        return [] if clean_description(result) is not None else list(window["result_block_ids"])
    items = result.get("results") if isinstance(result, dict) and set(result) == {"results"} else None
    if not isinstance(items, list):
        raise ValueError("repair result must be {results:[...]}")
    for item in items:
        if not isinstance(item, dict) or set(item) != {"block_id", "action", "replacement"} \
                or item["action"] not in {"keep", "replace"} or not isinstance(item["replacement"], str):
            raise ValueError("repair result item is malformed")
    ids = [item["block_id"] for item in items]
    if len(ids) != len(set(ids)) or set(ids) != set(window["result_block_ids"]):
        raise ValueError("repair result block ids do not match the window")
    expected = [blocks[block_id] for block_id in window["result_block_ids"]]
    return [block["id"] for block in rejected_candidates(expected, result, fragments)]


def assemble_external(output_dir: Path, manifest_file: Path, detected_markdown: Path,
                      results: list[tuple[dict[str, Any], Any]]) -> tuple[str, dict[str, Any]]:
    """받아들인 창 결과를 기존 결과 파일로 쓰고 기존 조립으로 Markdown과 복원 요약을 만든다.

    떨어진 block은 기존 복원과 같이 repair_rejected로 남겨 조립이 원문 대체 안내를 붙인다.
    """
    blocks, fragments = load_context(manifest_file, detected_markdown)
    failures: dict[str, str] = {}
    for window, result in results:
        expected = [blocks[block_id] for block_id in window["result_block_ids"]]
        if window["kind"] == "figure":
            cleaned = clean_description(result)
            if cleaned is not None:
                path = output_dir / DESCRIPTION_DIR / f"{expected[0]['id']}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(cleaned, ensure_ascii=False) + "\n", encoding="utf-8")
            continue
        counts = save_replacements(output_dir, expected, result, RECOVERY_SOURCE, fragments)
        for block in rejected_candidates(expected, result, fragments):
            failures[block["id"]] = "repair_rejected"
        if window["chain_block_ids"] and counts["replace"] == 1:
            chain = [blocks[block_id] for block_id in window["chain_block_ids"]]
            save_merged(output_dir, chain[0], chain[1:], RECOVERY_SOURCE)
    summary = output_dir / "final" / "selective_repair_summary.json"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps({"pages": [{"failures": failures}]}, ensure_ascii=False) + "\n", encoding="utf-8")
    output_file = output_dir / "final" / "external.restored.md"
    assemble(manifest_file, output_dir, output_file)
    restoration = json.loads((output_dir / "final" / "restoration_summary.json").read_text(encoding="utf-8"))
    return output_file.read_text(encoding="utf-8"), {**restoration, "rejected_block_ids": sorted(failures)}
