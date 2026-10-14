import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.modules.wiki_embedding.interfaces.http import embedding_server


class FakeModel:
    model_name = "BAAI/bge-m3"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0] for text in texts]


class EmbeddingServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.model = patch.object(embedding_server, "_model", FakeModel())
        self.model.start()
        embedding_server._ready.set()
        self.env = patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "secret"})
        self.env.start()
        self.client = TestClient(embedding_server.app)
        self.headers = {"X-Internal-Token": "secret"}

    def tearDown(self) -> None:
        self.env.stop()
        self.model.stop()
        embedding_server._ready.clear()

    def test_embeds_texts_with_model_name(self) -> None:
        response = self.client.post("/embeddings", json={"texts": ["가나", "abc"]}, headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"model": "BAAI/bge-m3", "vectors": [[2.0, 1.0], [3.0, 1.0]]})

    def test_rejects_missing_or_wrong_token(self) -> None:
        self.assertEqual(self.client.post("/embeddings", json={"texts": ["a"]}).status_code, 401)
        wrong = self.client.post("/embeddings", json={"texts": ["a"]}, headers={"X-Internal-Token": "x"})
        self.assertEqual(wrong.status_code, 401)

    def test_rejects_when_token_is_not_configured(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post("/embeddings", json={"texts": ["a"]}, headers=self.headers)
        self.assertEqual(response.status_code, 503)

    def test_rejects_long_texts_and_oversized_batches(self) -> None:
        long_text = "가" * (embedding_server.MAX_TEXT_CHARS + 1)
        too_many = ["a"] * (embedding_server.MAX_TEXTS_PER_REQUEST + 1)

        self.assertEqual(
            self.client.post("/embeddings", json={"texts": [long_text]}, headers=self.headers).status_code, 413
        )
        self.assertEqual(self.client.post("/embeddings", json={"texts": too_many}, headers=self.headers).status_code, 422)
        self.assertEqual(self.client.post("/embeddings", json={"texts": []}, headers=self.headers).status_code, 422)

    def test_reports_loading_until_model_is_ready(self) -> None:
        embedding_server._ready.clear()

        self.assertEqual(self.client.get("/health").status_code, 503)
        self.assertEqual(self.client.post("/embeddings", json={"texts": ["a"]}, headers=self.headers).status_code, 503)

        embedding_server._ready.set()
        self.assertEqual(self.client.get("/health").json(), {"status": "ok", "model": "BAAI/bge-m3"})

    def test_startup_warms_up_with_one_inference_before_reporting_healthy(self) -> None:
        embedding_server._ready.clear()
        calls: list[list[str]] = []

        class RecordingModel(FakeModel):
            def embed(self, texts: list[str]) -> list[list[float]]:
                # 예열 추론 중에는 아직 health가 정상이 아니어야 한다.
                calls.append(texts)
                assert not embedding_server._ready.is_set()
                return super().embed(texts)

        with patch.object(embedding_server, "_model", RecordingModel()), TestClient(embedding_server.app) as client:
            self.assertTrue(embedding_server._ready.wait(timeout=5))
            self.assertEqual(client.get("/health").status_code, 200)

        self.assertEqual(calls, [["warmup"]])


if __name__ == "__main__":
    unittest.main()
