import unittest
from datetime import UTC, datetime

from app.modules.wiki_embedding.application.build_wiki_page_embeddings import BuildWikiPageEmbeddingsUseCase, embedding_result
from app.modules.wiki_embedding.domain.entities import WikiPageEmbeddingTarget


class FakeRepository:
    def __init__(self, targets: list[WikiPageEmbeddingTarget], hashes: dict[str, str] | None = None) -> None:
        self.targets = targets
        self.hashes = hashes or {}
        self.upserts: list[tuple[str, str, str, list[float], datetime]] = []
        self.failures: list[tuple[str, str, str, str, datetime]] = []

    def list_active_pages_by_ids(self, page_ids: list[str]) -> list[WikiPageEmbeddingTarget]:
        page_id_set = set(page_ids)
        return [target for target in self.targets if target.page_id in page_id_set]

    def existing_hashes(self, page_ids: list[str], embedding_model: str) -> dict[str, str]:
        return {page_id: hash_value for page_id, hash_value in self.hashes.items() if page_id in page_ids}

    def upsert_embedding(
        self,
        page_id: str,
        embedding_model: str,
        representation_hash: str,
        embedding_vector: list[float],
        source_updated_at: datetime,
    ) -> None:
        self.upserts.append((page_id, embedding_model, representation_hash, embedding_vector, source_updated_at))

    def mark_failed(self, page_id: str, embedding_model: str, representation_hash: str,
                    error: str, source_updated_at: datetime) -> None:
        self.failures.append((page_id, embedding_model, representation_hash, error, source_updated_at))


class FakeEmbeddingModel:
    model_name = "test-model"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(index), 1.0] for index, _ in enumerate(texts, start=1)]


class FakeMarkdownReader:
    def __init__(self, markdown_by_uri: dict[str, str]) -> None:
        self.markdown_by_uri = markdown_by_uri

    def read_markdown(self, markdown_uri: str) -> str:
        if markdown_uri not in self.markdown_by_uri:
            raise RuntimeError("missing markdown")
        return self.markdown_by_uri[markdown_uri]


class BuildWikiPageEmbeddingsUseCaseTest(unittest.TestCase):
    UPDATED_AT = datetime(2026, 8, 10, tzinfo=UTC)

    def test_embedding_result_uses_stable_response_shape(self) -> None:
        self.assertEqual(
            embedding_result(target_count=2, embedded_count=1, skipped_count=1, failed_count=0),
            {"target_count": 2, "embedded_count": 1, "skipped_count": 1, "failed_count": 0},
        )

    def test_embeds_pages_and_skips_unchanged_hashes(self) -> None:
        targets = [
            WikiPageEmbeddingTarget("page:one", "One", "summary", "s3://test/one.md", self.UPDATED_AT),
            WikiPageEmbeddingTarget("page:two", "Two", "summary", "s3://test/two.md", self.UPDATED_AT),
        ]
        reader = FakeMarkdownReader({"s3://test/one.md": "body one", "s3://test/two.md": "body two"})
        repository = FakeRepository(targets)
        use_case = BuildWikiPageEmbeddingsUseCase(repository, FakeEmbeddingModel(), reader)

        first_result = use_case.execute(["page:one", "page:two"])
        existing_hash = repository.upserts[0][2]
        repository.hashes = {"page:one": existing_hash}
        repository.upserts = []
        second_result = use_case.execute(["page:one", "page:two"])

        self.assertEqual(first_result["embedded_count"], 2)
        self.assertEqual(second_result["skipped_count"], 1)
        self.assertEqual(second_result["embedded_count"], 1)
        self.assertEqual(repository.upserts[0][0], "page:two")

    def test_marks_page_failed_when_markdown_cannot_be_read(self) -> None:
        targets = [WikiPageEmbeddingTarget(
            "page:missing", "Missing", "summary", "s3://test/missing.md", self.UPDATED_AT
        )]
        repository = FakeRepository(targets)
        use_case = BuildWikiPageEmbeddingsUseCase(repository, FakeEmbeddingModel(), FakeMarkdownReader({}))

        result = use_case.execute(["page:missing"])

        self.assertEqual(result["embedded_count"], 0)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(len(repository.failures), 1)
        self.assertEqual(repository.failures[0][0], "page:missing")


if __name__ == "__main__":
    unittest.main()
