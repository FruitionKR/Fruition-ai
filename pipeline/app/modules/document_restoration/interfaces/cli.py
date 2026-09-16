import argparse
from pathlib import Path

from app.core.llm_env import SUPPORTED_LLM_MODELS
from app.modules.document_restoration.application.models import RestoreDocumentCommand
from app.modules.document_restoration.application.restore_document import (
    RestoreDocumentUseCase,
)
from app.modules.document_restoration.domain.entities import RestorationMode
from app.modules.document_restoration.infrastructure.subprocess_restoration_stages import (
    SubprocessDocumentRestorationStages,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-file", type=Path, required=True)
    parser.add_argument("--docling-json", type=Path)
    parser.add_argument("--docling-markdown", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--document-slug", required=True)
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in RestorationMode],
        default=RestorationMode.CROP_FIRST.value,
        help=(
            "기본값은 AnyDoc crop-first입니다. Docling 결과만 게시하려면 "
            "docling-only를 사용합니다."
        ),
    )
    parser.add_argument(
        "--use-local-sllm",
        action="store_true",
        help="규칙 기반 코드로 복원하지 못한 수식에 로컬 SLLM 보완을 사용합니다.",
    )
    parser.add_argument("--use-local-vision", action="store_true")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434/v1/chat/completions")
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--vision-model", default="qwen2.5vl:7b")
    parser.add_argument("--max-vision-attempts", type=int, default=3)
    parser.add_argument("--docling-command", default="docling")
    parser.add_argument(
        "--selective-provider",
        choices=["openai", "gemini", "claude"],
        default="gemini",
    )
    parser.add_argument("--selective-model")
    parser.add_argument("--selective-max-workers", type=int, default=16)
    parser.add_argument("--anydoc-command", default="anydoc")
    parser.add_argument("--heron-command", default="raw-special-regions")
    parser.add_argument("--heron-model", type=Path)
    parser.add_argument("--pdfium-library", type=Path)
    parser.add_argument("--body-ai-budget", type=float, default=0.3)
    args = parser.parse_args()

    use_case = RestoreDocumentUseCase(SubprocessDocumentRestorationStages())
    use_case.execute(
        RestoreDocumentCommand(
            pdf_file=args.pdf_file.resolve(),
            output_dir=args.output_dir.resolve(),
            document_slug=args.document_slug,
            docling_json=args.docling_json.resolve() if args.docling_json else None,
            docling_markdown=(
                args.docling_markdown.resolve() if args.docling_markdown else None
            ),
            mode=RestorationMode(args.mode),
            use_local_sllm=args.use_local_sllm,
            use_local_vision=args.use_local_vision,
            endpoint=args.endpoint,
            model=args.model,
            vision_model=args.vision_model,
            max_vision_attempts=args.max_vision_attempts,
            docling_command=args.docling_command,
            selective_provider=args.selective_provider,
            selective_model=(
                args.selective_model
                or SUPPORTED_LLM_MODELS[args.selective_provider]
            ),
            selective_max_workers=args.selective_max_workers,
            anydoc_command=args.anydoc_command,
            heron_command=args.heron_command,
            heron_model=args.heron_model.resolve() if args.heron_model else None,
            pdfium_library=args.pdfium_library.resolve() if args.pdfium_library else None,
            body_ai_budget=args.body_ai_budget,
        )
    )


if __name__ == "__main__":
    main()
