from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.modules.document_evaluation.application.ports import DocumentEvaluatorPort
from app.modules.document_evaluation.domain.entities import DocumentEvaluationJob
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)


DEFAULT_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "document_evaluator.system.md"


class ChatCompletionsDocumentEvaluator(DocumentEvaluatorPort):
    def __init__(self, client: ChatCompletionsJsonClient, system_prompt: str) -> None:
        self._client = client
        self._system_prompt = system_prompt

    def evaluate(self, job: DocumentEvaluationJob) -> dict[str, Any]:
        job_data = job.to_dict()
        evaluations = []
        for chunk in job.chunks:
            payload = {
                "job_id": job.job_id,
                "source": {"markdown_sha256": job.markdown_sha256},
                "constraints": job_data["constraints"],
                "result_contract": job_data["result_contract"],
                "chunk": chunk.to_dict(),
            }
            evaluations.append(self._client.complete_json(self._system_prompt, json.dumps(payload, ensure_ascii=False)))
        return {
            "schema_version": "document-evaluation-result.v1",
            "job_id": job.job_id,
            "chunk_evaluations": evaluations,
        }


def build_optional_document_evaluator() -> DocumentEvaluatorPort | None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    prompt_path = Path(os.environ.get("DOCUMENT_EVALUATOR_SYSTEM_PROMPT", str(DEFAULT_PROMPT)))
    return ChatCompletionsDocumentEvaluator(
        client=ChatCompletionsJsonClient(
            ChatClientConfig(
                api_key=api_key,
                model="gpt-5-nano",
                temperature=None,
                timeout_seconds=180,
                json_mode=True,
                provider="openai",
            )
        ),
        system_prompt=prompt_path.read_text(encoding="utf-8"),
    )
