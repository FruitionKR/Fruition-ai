"""crop-first가 잘라낸 그림에 대체 텍스트와 짧은 설명을 붙인다(opt-in).

변환 명령의 describe_figures가 참일 때만 실행되며, 그때만 그림 이미지를 외부 공급사로 보낸다.
공급사 호출은 selective repair와 같은 call_page로 사용량 원장에 기록된다.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from app.core.llm_env import api_key_from_env, resolve_llm_selection
from app.modules.document_restoration.infrastructure.selective_repair_with_provider import (
    PROMPT_DIR,
    call_page,
    image_file_data_url,
)


logger = logging.getLogger(__name__)
FIGURE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "alt": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["alt", "description"],
}
# 한 변환 실행(쪽 범위 하나)에서 설명할 그림 수 상한이다. 넘는 그림은 skipped로 센다.
MAX_FIGURES = 50
# 한 변이 이보다 짧은 그림(PDF point, 72pt=1인치)은 아이콘·장식선으로 보고 보내지 않는다.
MIN_FIGURE_SIDE_POINTS = 48
CALL_TIMEOUT_SECONDS = 60
# alt·설명 JSON과 Gemini LOW 추론 토큰을 담는 출력 상한이다.
MAX_OUTPUT_TOKENS = 1024
MAX_ALT_LENGTH = 80
MAX_DESCRIPTION_LENGTH = 400
DESCRIPTION_DIR = Path("layout") / "crop_first" / "figure_descriptions"
SUMMARY_FILE = Path("final") / "figure_descriptions_summary.json"


def is_too_small(region: dict[str, Any]) -> bool:
    left, top, right, bottom = (float(value) for value in region["bbox"])
    return min(right - left, bottom - top) < MIN_FIGURE_SIDE_POINTS


def one_line(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit].strip()


def clean_description(result: dict[str, Any]) -> dict[str, str] | None:
    """Markdown 이미지 문법과 인용 블록을 깨지 않도록 한 줄로 줄이고, 비었으면 None이다."""
    alt = one_line(re.sub(r"[\[\]()!]", "", str(result.get("alt", ""))), MAX_ALT_LENGTH)
    description = one_line(str(result.get("description", "")), MAX_DESCRIPTION_LENGTH)
    if not alt or not description:
        return None
    return {"alt": alt, "description": description}


def write_summary(output_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    path = output_dir / SUMMARY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def run(args: argparse.Namespace) -> dict[str, Any]:
    provider, model = resolve_llm_selection(args.provider, args.model)
    manifest = json.loads(args.manifest_file.read_text(encoding="utf-8"))
    figures = [block for block in manifest if block["type"] == "figure_candidate"]
    sized = [figure for figure in figures if not is_too_small(figure)]
    eligible = sized[:MAX_FIGURES]
    skipped = len(figures) - len(eligible)
    api_key = api_key_from_env(provider=provider, strip=True)
    if not api_key:
        logger.warning("그림 설명 공급사 키 없음: provider=%s", provider)
        return write_summary(
            args.output_dir,
            {"requested": True, "described": 0, "failed": len(eligible), "skipped": skipped},
        )
    prompt = (PROMPT_DIR / "figure_description.md").read_text(encoding="utf-8")
    description_dir = args.output_dir / DESCRIPTION_DIR

    def describe(figure: dict[str, Any]) -> bool:
        try:
            result, _usage = call_page(
                provider=provider,
                api_key=api_key,
                model=model,
                prompt=prompt,
                payload={"task": "figure_description", "page": figure["page"]},
                images=[image_file_data_url(args.output_dir / str(figure["asset"]))],
                schema=FIGURE_SCHEMA,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                timeout=CALL_TIMEOUT_SECONDS,
            )
        except (TimeoutError, urllib.error.URLError, RuntimeError, ValueError, OSError) as exc:
            logger.warning("그림 설명 실패: figure=%s error_type=%s", figure["id"], type(exc).__name__)
            return False
        cleaned = clean_description(result if isinstance(result, dict) else {})
        if cleaned is None:
            return False
        description_dir.mkdir(parents=True, exist_ok=True)
        (description_dir / f"{figure['id']}.json").write_text(
            json.dumps(cleaned, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return True

    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as executor:
        outcomes = list(executor.map(describe, eligible))
    described = sum(outcomes)
    return write_summary(
        args.output_dir,
        {"requested": True, "described": described, "failed": len(outcomes) - described, "skipped": skipped},
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--provider", choices=["openai", "gemini", "claude"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-workers", type=int, required=True)
    args = parser.parse_args()
    args.manifest_file = args.manifest_file.resolve()
    args.output_dir = args.output_dir.resolve()
    print(json.dumps(run(args), ensure_ascii=False))


if __name__ == "__main__":
    main()
