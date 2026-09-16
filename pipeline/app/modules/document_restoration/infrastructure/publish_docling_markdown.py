from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def publish_docling_markdown(input_file: Path, output_file: Path) -> None:
    if not input_file.exists():
        raise FileNotFoundError(
            "Docling Markdown을 찾을 수 없습니다. 캐시된 JSON을 사용한다면 "
            "--docling-markdown도 전달해야 합니다: "
            f"{input_file}"
        )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(input_file, output_file)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file", type=Path, required=True)
    parser.add_argument("--output-file", type=Path, required=True)
    args = parser.parse_args()
    publish_docling_markdown(args.input_file, args.output_file)


if __name__ == "__main__":
    main()
