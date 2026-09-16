import hashlib

from app.modules.wiki_embedding.application.ports import EmbeddingModelPort, MarkdownReaderPort, WikiPageEmbeddingRepositoryPort
from app.modules.wiki_embedding.domain.entities import WikiPageEmbeddingInput, WikiPageEmbeddingTarget


class BuildWikiPageEmbeddingsUseCase:
    def __init__(
        self,
        repository: WikiPageEmbeddingRepositoryPort,
        embedding_model: EmbeddingModelPort,
        markdown_reader: MarkdownReaderPort,
    ) -> None:
        self._repository = repository
        self._embedding_model = embedding_model
        self._markdown_reader = markdown_reader

    def execute(self, page_ids: list[str]) -> dict[str, int]:
        unique_page_ids = list(dict.fromkeys(page_ids))
        if not unique_page_ids:
            return embedding_result()

        targets = self._repository.list_active_pages_by_ids(unique_page_ids)
        existing_hashes = self._repository.existing_hashes(unique_page_ids, self._embedding_model.model_name)
        inputs, read_failed_count = self._build_inputs(targets)
        pending = [item for item in inputs if existing_hashes.get(item.page_id) != item.representation_hash]
        skipped_count = len(inputs) - len(pending)
        embedded_count = 0
        failed_count = read_failed_count

        if not pending:
            return embedding_result(len(targets), embedded_count, skipped_count, failed_count)

        try:
            vectors = self._embedding_model.embed([item.representation for item in pending])
        except Exception as exc:
            for item in pending:
                self._repository.mark_failed(
                    item.page_id,
                    self._embedding_model.model_name,
                    item.representation_hash,
                    str(exc),
                    item.source_updated_at,
                )
            return embedding_result(len(targets), embedded_count, skipped_count, len(pending))

        for item, vector in zip(pending, vectors):
            try:
                self._repository.upsert_embedding(
                    item.page_id,
                    self._embedding_model.model_name,
                    item.representation_hash,
                    vector,
                    item.source_updated_at,
                )
                embedded_count += 1
            except Exception as exc:
                failed_count += 1
                self._repository.mark_failed(
                    item.page_id,
                    self._embedding_model.model_name,
                    item.representation_hash,
                    str(exc),
                    item.source_updated_at,
                )

        return embedding_result(len(targets), embedded_count, skipped_count, failed_count)

    def _build_inputs(self, targets: list[WikiPageEmbeddingTarget]) -> tuple[list[WikiPageEmbeddingInput], int]:
        inputs = []
        failed_count = 0
        for target in targets:
            try:
                markdown = self._markdown_reader.read_markdown(target.markdown_uri)
            except Exception as exc:
                failed_count += 1
                representation_hash = self._hash(self._representation(target, ""))
                self._repository.mark_failed(
                    target.page_id,
                    self._embedding_model.model_name,
                    representation_hash,
                    str(exc),
                    target.updated_at,
                )
                continue
            representation = self._representation(target, markdown)
            inputs.append(
                WikiPageEmbeddingInput(
                    page_id=target.page_id,
                    representation=representation,
                    representation_hash=self._hash(representation),
                    source_updated_at=target.updated_at,
                )
            )
        return inputs, failed_count

    def _representation(self, target: WikiPageEmbeddingTarget, markdown: str) -> str:
        return "\n".join([target.title, target.summary, markdown]).strip()

    def _hash(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embedding_result(
    target_count: int = 0,
    embedded_count: int = 0,
    skipped_count: int = 0,
    failed_count: int = 0,
) -> dict[str, int]:
    return {
        "target_count": target_count,
        "embedded_count": embedded_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
    }
