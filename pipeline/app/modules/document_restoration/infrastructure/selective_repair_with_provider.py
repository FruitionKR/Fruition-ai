from __future__ import annotations

import argparse
import base64
import json
import logging
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fitz
import psycopg

from app.core.llm_env import api_key_from_env, inference_profile, resolve_llm_selection
from app.modules.document_restoration.domain.markdown_text import (
    has_balanced_braces,
    is_valid_markdown_table,
)
from app.modules.document_restoration.domain.text_quality import looks_glyph_encoded
from app.modules.model_usage.infrastructure.usage_ledger import SCOPE_ENV, track_call, usage_scope


PROMPT_FILE = (
    Path(__file__).resolve().parents[4]
    / "prompts"
    / "document_restoration"
    / "direct_visual_block_repair.md"
)
OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "block_id": {"type": "string"},
                    "action": {"type": "string", "enum": ["keep", "replace"]},
                    "replacement": {"type": "string"},
                },
                "required": ["block_id", "action", "replacement"],
            },
        }
    },
    "required": ["results"],
}
logger = logging.getLogger(__name__)
OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
CLAUDE_ENDPOINT = "https://api.anthropic.com/v1/messages"


def select_candidates(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        block
        for block in blocks
        if block["type"] in {"table_candidate", "equation_candidate"}
        or (
            block["type"] in {"paragraph", "heading"}
            and (
                block.get("text_decision") == "needs_text_adjudication"
                or looks_glyph_encoded(str(block.get("source_text", "")))
            )
        )
    ]


def candidate_lane(block: dict[str, Any]) -> str:
    if block["type"] in {"table_candidate", "equation_candidate"}:
        return "special"
    return "text"


def group_candidates(
    blocks: list[dict[str, Any]],
) -> dict[tuple[int, str], list[dict[str, Any]]]:
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for block in blocks:
        key = (int(block["page"]), candidate_lane(block))
        grouped.setdefault(key, []).append(block)
    return dict(sorted(grouped.items()))


def markdown_fragments(markdown: str) -> dict[str, str]:
    pattern = re.compile(r"<!-- (\S+) type=\S+ bbox=.*? -->\n")
    matches = list(pattern.finditer(markdown))
    fragments = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        value = markdown[match.end():end].strip()
        fragments[match.group(1)] = re.sub(
            r"\n+## Page \d+\s*$",
            "",
            value,
        ).strip()
    return fragments


def page_markdown(markdown: str) -> dict[int, str]:
    matches = list(re.finditer(r"^## Page (\d+)\s*$", markdown, re.MULTILINE))
    pages = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        pages[int(match.group(1))] = markdown[match.end():end].strip()
    return pages


def block_markdown(
    block: dict[str, Any],
    fragments: dict[str, str] | None = None,
) -> str:
    candidates = []
    if fragments is not None and block["id"] in fragments:
        candidates.append(fragments[block["id"]])
    candidates.extend(
        block.get(field)
        for field in ("markdown", "source_text", "fallback_text")
    )
    return max(
        (str(candidate or "") for candidate in candidates),
        key=lambda value: len(re.sub(r"\s+", "", value)),
        default="",
    )


def valid_replacement(
    block_type: str,
    replacement: str,
    required_tokens: list[str] | None = None,
    *,
    scope: str = "block",
    source_text: str = "",
) -> bool:
    if block_type == "equation_candidate":
        normalized = normalize_replacement(block_type, replacement)
        return normalized.count("$$") == 2 and has_balanced_braces(normalized)
    if block_type == "table_candidate":
        return is_valid_markdown_table(replacement)
    if block_type == "heading":
        return bool(re.fullmatch(r"#{1,6}\s+\S[^\n]*", replacement))
    if not replacement.strip() or (
        scope != "page_body" and "```" in replacement
    ):
        return False
    if scope == "page_body":
        source_length = len(re.sub(r"\s+", "", source_text))
        replacement_length = len(re.sub(r"\s+", "", replacement))
        if source_length and replacement_length < source_length / 2:
            return False
    return all(replacement.count(token) == 1 for token in required_tokens or [])


def normalize_replacement(block_type: str, replacement: str) -> str:
    normalized = replacement.strip()
    if (
        block_type == "equation_candidate"
        and normalized.startswith(r"\[")
        and normalized.endswith(r"\]")
    ):
        return "$$\n" + normalized[2:-2].strip() + "\n$$"
    return normalized


def image_data_url(data: bytes, media_type: str = "image/png") -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def image_file_data_url(path: Path) -> str:
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    return image_data_url(path.read_bytes(), media_type)


def render_page(pdf_file: Path, page_number: int) -> str:
    with fitz.open(pdf_file) as pdf:
        pixmap = pdf[page_number - 1].get_pixmap(
            matrix=fitz.Matrix(2, 2),
            alpha=False,
        )
    return image_data_url(pixmap.tobytes("png"))


def render_crop(pdf_file: Path, block: dict[str, Any]) -> str:
    with fitz.open(pdf_file) as pdf:
        page = pdf[int(block["page"]) - 1]
        rect = fitz.Rect(*(float(value) for value in block["bbox"]))
        rect = fitz.Rect(
            max(0, rect.x0 - 4),
            max(0, rect.y0 - 4),
            min(page.rect.width, rect.x1 + 4),
            min(page.rect.height, rect.y1 + 4),
        )
        pixmap = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=rect, alpha=False)
    return image_data_url(pixmap.tobytes("png"))


def block_image(
    output_dir: Path,
    pdf_file: Path,
    block: dict[str, Any],
) -> str:
    asset = block.get("asset")
    if asset:
        asset_path = output_dir / str(asset)
        if asset_path.exists():
            return image_file_data_url(asset_path)
    return render_crop(pdf_file, block)


def openai_response_text(response: dict[str, Any]) -> str:
    for output in response.get("output", []):
        if output.get("type") != "message":
            continue
        for content in output.get("content", []):
            if content.get("type") == "output_text":
                return str(content.get("text", ""))
    raise ValueError("Responses API output_text가 없습니다.")


def image_source(data_url: str) -> tuple[str, str]:
    match = re.fullmatch(r"data:([^;]+);base64,(.+)", data_url, re.DOTALL)
    if match is None:
        raise ValueError("지원하지 않는 image data URL입니다.")
    return match.group(1), match.group(2)


def post_json(
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
    api_name: str,
) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as raw:
            return json.loads(raw.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or exc.code >= 500:
            raise
        raise RuntimeError(f"{api_name} HTTP {exc.code}") from exc


def call_openai(
    *,
    receipt: dict[str, Any],
    api_key: str,
    model: str,
    prompt: str,
    payload: dict[str, Any],
    images: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    page_body = any(
        block.get("scope") == "page_body" for block in payload.get("blocks", [])
    )
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": "INPUT PAYLOAD:\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        }
    ]
    for sequence, image in enumerate(images):
        content.append(
            {
                "type": "input_image",
                "image_url": image,
                "detail": "original" if page_body or sequence > 0 else "auto",
            }
        )
    body: dict[str, Any] = {
        "model": model,
        "store": False,
        "input": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "document_block_restoration",
                "strict": True,
                "schema": OUTPUT_SCHEMA,
            }
        },
    }
    # 추론 수준은 지원하는 모델에만 보낸다.
    effort = inference_profile("openai", model).get("reasoning_effort")
    if effort is not None:
        body["reasoning"] = {"effort": effort}
    response = post_json(
        OPENAI_ENDPOINT,
        body,
        {"Authorization": f"Bearer {api_key}"},
        "OpenAI Responses API",
    )
    receipt["response"] = ledger_response("openai", response)
    return json.loads(openai_response_text(response)), response.get("usage") or {}


def call_gemini(
    *,
    receipt: dict[str, Any],
    api_key: str,
    model: str,
    prompt: str,
    payload: dict[str, Any],
    images: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    parts: list[dict[str, Any]] = [
        {
            "text": "INPUT PAYLOAD:\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        }
    ]
    for image in images:
        media_type, data = image_source(image)
        parts.append(
            {"inline_data": {"mime_type": media_type, "data": data}}
        )
    body = {
        "system_instruction": {"parts": [{"text": prompt}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": OUTPUT_SCHEMA,
            "thinkingConfig": {"thinkingLevel": "LOW"},
        },
    }
    endpoint = GEMINI_ENDPOINT.format(model=urllib.parse.quote(model, safe=""))
    response = post_json(
        endpoint,
        body,
        {"x-goog-api-key": api_key},
        "Gemini API",
    )
    receipt["response"] = ledger_response("gemini", response)
    try:
        text = response["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Gemini API response text가 없습니다.") from exc
    return json.loads(text), response.get("usageMetadata") or {}


def call_claude(
    *,
    receipt: dict[str, Any],
    api_key: str,
    model: str,
    prompt: str,
    payload: dict[str, Any],
    images: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for image in images:
        media_type, data = image_source(image)
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            }
        )
    content.append(
        {
            "type": "text",
            "text": "INPUT PAYLOAD:\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        }
    )
    body = {
        "model": model,
        "max_tokens": 16384,
        "system": prompt,
        "messages": [{"role": "user", "content": content}],
        "output_config": {
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}
        },
    }
    response = post_json(
        CLAUDE_ENDPOINT,
        body,
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        "Claude Messages API",
    )
    receipt["response"] = ledger_response("claude", response)
    try:
        text = next(
            block["text"]
            for block in response["content"]
            if block.get("type") == "text"
        )
    except (KeyError, StopIteration, TypeError) as exc:
        raise ValueError("Claude Messages API response text가 없습니다.") from exc
    return json.loads(text), response.get("usage") or {}


def call_page(
    *,
    provider: str,
    api_key: str,
    model: str,
    prompt: str,
    payload: dict[str, Any],
    images: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    callers = {
        "openai": call_openai,
        "gemini": call_gemini,
        "claude": call_claude,
    }
    # converter가 넘긴 actor로 기록한다. env가 없는 로컬 CLI 실행은 원장이 경고만 남긴다.
    scope = os.environ.get(SCOPE_ENV)
    result = None
    try:
        with (
            usage_scope(json.loads(scope)) if scope else nullcontext(),
            track_call(provider, model) as receipt,
        ):
            result = callers[provider](
                receipt=receipt,
                api_key=api_key,
                model=model,
                prompt=prompt,
                payload=payload,
                images=images,
            )
    except psycopg.Error as exc:
        if result is None:
            # 시작 기록 실패라 공급사를 호출하지 않았다. run()이 기본 변환 결과로 넘어간다.
            raise RuntimeError("model usage ledger unavailable") from exc
        # 이미 비용이 나간 응답은 버리지 않는다. started 행은 1시간 뒤 abandoned로 닫힌다.
        logger.warning("사용량 원장 갱신 실패: provider=%s model=%s", provider, model)
    return result


def _token(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _sum(*values: Any) -> int | None:
    # 첫 값(기준 토큰)을 모르면 합계도 모른다. 나머지 세부 항목은 없으면 0이다.
    first = _token(values[0])
    if first is None:
        return None
    return first + sum(_token(value) or 0 for value in values[1:])


def ledger_response(provider: str, response: Any) -> SimpleNamespace | None:
    """공급사 usage 원값을 원장의 LangChain 형태로 맞춘다.

    input_tokens는 캐시 입력을, output_tokens는 추론 토큰을 포함한다(docs/api/tasks.md 토큰 포함 관계).
    """
    if not isinstance(response, dict):
        return None
    if provider == "openai":
        usage = response.get("usage") or {}
        input_details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        normalized = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "input_token_details": {"cache_read": input_details.get("cached_tokens")},
            "output_token_details": {"reasoning": output_details.get("reasoning_tokens")},
        }
        model = response.get("model")
    elif provider == "gemini":
        usage = response.get("usageMetadata") or {}
        output = _sum(usage.get("candidatesTokenCount"), usage.get("thoughtsTokenCount"))
        incoming = _sum(usage.get("promptTokenCount"), usage.get("toolUsePromptTokenCount"))
        normalized = {
            "input_tokens": incoming,
            "output_tokens": output,
            # 기준 토큰을 모르면 캐시도 모른다. 0으로 확정하면 미확인 캐시 집계에서 빠진다.
            "input_token_details": {
                "cache_read": None if incoming is None else _token(usage.get("cachedContentTokenCount", 0))
            },
            "output_token_details": {
                "reasoning": None if output is None else _token(usage.get("thoughtsTokenCount", 0))
            },
        }
        model = response.get("modelVersion")
    else:
        usage = response.get("usage") or {}
        known = _token(usage.get("input_tokens")) is not None
        cache_read = _token(usage.get("cache_read_input_tokens", 0)) if known else None
        cache_creation = _token(usage.get("cache_creation_input_tokens", 0)) if known else None
        normalized = {
            # Anthropic input_tokens는 캐시 입력을 빼고 세므로 더한다.
            "input_tokens": _sum(usage.get("input_tokens"), cache_read, cache_creation),
            "output_tokens": usage.get("output_tokens"),
            "input_token_details": {"cache_read": cache_read, "cache_creation": cache_creation},
        }
        model = response.get("model")
    return SimpleNamespace(usage_metadata=normalized, response_metadata={"model": model})


def clean_previous_results(
    output_dir: Path,
) -> None:
    layout = output_dir / "layout" / "auto"
    result_dirs = (
        (layout / "evaluations", layout / "recovered_blocks"),
        (layout / "text_evaluations", layout / "text_recovered"),
    )
    for evaluation_dir, recovered_dir in result_dirs:
        for evaluation_file in evaluation_dir.glob("*.json"):
            evaluation_file.unlink()
        for recovered_file in recovered_dir.glob("*.md"):
            recovered_file.unlink()


def save_replacements(
    output_dir: Path,
    blocks: list[dict[str, Any]],
    result: dict[str, Any],
    provider: str,
    fragments: dict[str, str] | None = None,
) -> dict[str, int]:
    expected = {block["id"]: block for block in blocks}
    returned = result.get("results") or []
    ids = [item.get("block_id") for item in returned]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ValueError("Provider result ID mismatch")
    counts = {"replace": 0, "keep": 0, "rejected": 0}
    layout = output_dir / "layout" / "auto"
    for item in returned:
        block = expected[item["block_id"]]
        if item["action"] == "keep":
            if block.get("replacement_required"):
                counts["rejected"] += 1
                continue
            counts["keep"] += 1
            continue
        replacement = normalize_replacement(
            block["type"],
            str(item["replacement"]),
        )
        if not valid_replacement(
            block["type"],
            replacement,
            block.get("required_tokens"),
            scope=str(block.get("scope", "block")),
            source_text=block_markdown(block, fragments),
        ):
            counts["rejected"] += 1
            continue
        text_block = block["type"] in {"paragraph", "heading"}
        recovered_dir = layout / ("text_recovered" if text_block else "recovered_blocks")
        evaluation_dir = layout / ("text_evaluations" if text_block else "evaluations")
        recovered_dir.mkdir(parents=True, exist_ok=True)
        evaluation_dir.mkdir(parents=True, exist_ok=True)
        (recovered_dir / f"{block['id']}.md").write_text(
            replacement + "\n",
            encoding="utf-8",
        )
        (evaluation_dir / f"{block['id']}.json").write_text(
            json.dumps(
                {
                    "accepted": True,
                    "score": 1.0,
                    "reasons": [],
                    "recovery_source": f"{provider}_selective_repair",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        counts["replace"] += 1
    return counts


def rejected_candidates(
    blocks: list[dict[str, Any]],
    result: dict[str, Any],
    fragments: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    returned = {item["block_id"]: item for item in result["results"]}
    return [
        block
        for block in blocks
        if (
            returned[block["id"]]["action"] == "keep"
            and block.get("replacement_required")
        )
        or (
            returned[block["id"]]["action"] == "replace"
            and not valid_replacement(
                block["type"],
                str(returned[block["id"]]["replacement"]),
                block.get("required_tokens"),
                scope=str(block.get("scope", "block")),
                source_text=block_markdown(block, fragments),
            )
        )
    ]


def run(args: argparse.Namespace) -> dict[str, Any]:
    provider, model = resolve_llm_selection(args.provider, args.model)
    manifest = json.loads(args.manifest_file.read_text(encoding="utf-8"))
    selected = select_candidates(manifest)
    clean_previous_results(args.output_dir)
    summary_file = (
        args.output_dir / "final" / "selective_repair_summary.json"
    )
    api_key = api_key_from_env(provider=provider, strip=True)
    if not api_key:
        summary = {
            "provider": provider,
            "model": model,
            "calls": 0,
            "group_calls": 0,
            "fallback_calls": 0,
            "blocks": len(selected),
            "pages": [],
        }
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        summary_file.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return summary
    markdown = args.detected_markdown.read_text(encoding="utf-8")
    fragments = markdown_fragments(markdown)
    pages = page_markdown(markdown)
    prompt = PROMPT_FILE.read_text(encoding="utf-8")
    grouped = group_candidates(selected)

    def process(key: tuple[int, str]) -> dict[str, Any]:
        page, lane = key
        blocks = grouped[key]
        page_image = (
            None
            if all(block.get("scope") == "page_body" for block in blocks)
            else render_page(args.pdf_file, page)
        )
        block_images = {
            block["id"]: block_image(args.output_dir, args.pdf_file, block)
            for block in blocks
        }

        def request(
            request_blocks: list[dict[str, Any]],
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            payload_blocks = [
                {
                    "block_id": block["id"],
                    "page": block["page"],
                    "order": block["order"],
                    "type": block["type"],
                    "bbox": block["bbox"],
                    "crop_sequence": sequence,
                    "scope": block.get("scope", "block"),
                    "required_tokens": block.get("required_tokens", []),
                    "current_markdown": block_markdown(block, fragments),
                }
                for sequence, block in enumerate(request_blocks, 1)
            ]
            return call_page(
                provider=provider,
                api_key=api_key,
                model=model,
                prompt=prompt,
                payload={
                    "page_context": (
                        ""
                        if any(
                            block.get("scope") == "page_body"
                            for block in request_blocks
                        )
                        else pages.get(page, "")
                    ),
                    "blocks": payload_blocks,
                },
                images=(
                    ([] if page_image is None else [page_image])
                    + [block_images[block["id"]] for block in request_blocks]
                ),
            )

        counts = {"replace": 0, "keep": 0, "rejected": 0, "failed": 0}
        usage: dict[str, Any] = {}
        fallback_usage: list[dict[str, Any]] = []
        batch_error: str | None = None
        try:
            result, usage = request(blocks)
            batch_counts = save_replacements(
                args.output_dir, blocks, result, provider, fragments
            )
            for name, count in batch_counts.items():
                counts[name] += count
            fallback_blocks = rejected_candidates(blocks, result, fragments)
            counts["rejected"] -= len(fallback_blocks)
        except (TimeoutError, urllib.error.URLError, RuntimeError, ValueError) as exc:
            batch_error = type(exc).__name__
            fallback_blocks = blocks

        for block in fallback_blocks:
            try:
                result, item_usage = request([block])
                item_counts = save_replacements(
                    args.output_dir, [block], result, provider, fragments
                )
            except (TimeoutError, urllib.error.URLError, RuntimeError, ValueError):
                counts["failed"] += 1
                continue
            fallback_usage.append(item_usage)
            for name, count in item_counts.items():
                counts[name] += count

        return {
            "page": page,
            "lane": lane,
            "blocks": len(blocks),
            **counts,
            "usage": usage,
            "fallback_calls": len(fallback_blocks),
            "fallback_usage": fallback_usage,
            "batch_error": batch_error,
        }

    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        page_results = list(executor.map(process, grouped))
    summary = {
        "provider": provider,
        "model": model,
        "calls": sum(1 + result["fallback_calls"] for result in page_results),
        "group_calls": len(page_results),
        "fallback_calls": sum(result["fallback_calls"] for result in page_results),
        "blocks": len(selected),
        "pages": page_results,
    }
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.write_text(
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
    parser.add_argument(
        "--provider",
        choices=["openai", "gemini", "claude"],
        required=True,
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-workers", type=int, required=True)
    args = parser.parse_args()
    args.pdf_file = args.pdf_file.resolve()
    args.manifest_file = args.manifest_file.resolve()
    args.detected_markdown = args.detected_markdown.resolve()
    args.output_dir = args.output_dir.resolve()
    print(json.dumps(run(args), ensure_ascii=False))


if __name__ == "__main__":
    main()
