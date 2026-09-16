from typing import Any, Protocol

from app.modules.document_evaluation.domain.entities import DocumentEvaluationJob


class DocumentEvaluatorPort(Protocol):
    def evaluate(self, job: DocumentEvaluationJob) -> dict[str, Any]:
        ...
