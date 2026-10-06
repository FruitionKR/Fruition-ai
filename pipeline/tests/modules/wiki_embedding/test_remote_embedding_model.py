import unittest

import httpx

from app.modules.wiki_embedding.infrastructure.remote_embedding_model import MAX_TEXTS_PER_REQUEST, RemoteEmbeddingModel


def _model(handler) -> RemoteEmbeddingModel:
    return RemoteEmbeddingModel("http://embedding-server:8000", "secret", transport=httpx.MockTransport(handler))


class RemoteEmbeddingModelTest(unittest.TestCase):
    def test_posts_texts_with_internal_token(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["token"] = request.headers["X-Internal-Token"]
            seen["body"] = request.read()
            return httpx.Response(200, json={"model": "BAAI/bge-m3", "vectors": [[0.1, 0.2]]})

        self.assertEqual(_model(handler).embed(["질문"]), [[0.1, 0.2]])
        self.assertEqual(seen["token"], "secret")
        self.assertIn("질문".encode(), seen["body"])

    def test_empty_input_does_not_call_server(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not be called")

        self.assertEqual(_model(handler).embed([]), [])

    def test_rejects_model_mismatch_and_vector_count_mismatch(self) -> None:
        other = _model(lambda request: httpx.Response(200, json={"model": "other", "vectors": [[0.1]]}))
        short = _model(lambda request: httpx.Response(200, json={"model": "BAAI/bge-m3", "vectors": []}))

        with self.assertRaisesRegex(RuntimeError, "does not match"):
            other.embed(["a"])
        with self.assertRaisesRegex(RuntimeError, "number of vectors"):
            short.embed(["a"])

    def test_retries_once_on_server_error_or_connection_failure(self) -> None:
        for first in (httpx.Response(503), httpx.ConnectError("down")):
            calls = []

            def handler(request: httpx.Request, first=first) -> httpx.Response:
                calls.append(request)
                if len(calls) == 1:
                    if isinstance(first, Exception):
                        raise first
                    return first
                return httpx.Response(200, json={"model": "BAAI/bge-m3", "vectors": [[1.0]]})

            self.assertEqual(_model(handler).embed(["a"]), [[1.0]])
            self.assertEqual(len(calls), 2)

    def test_does_not_retry_timeouts(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ReadTimeout("slow")

        with self.assertRaises(httpx.ReadTimeout):
            _model(handler).embed(["a"])
        self.assertEqual(len(calls), 1)

    def test_splits_requests_by_server_batch_limit(self) -> None:
        import json

        sizes = []

        def handler(request: httpx.Request) -> httpx.Response:
            texts = json.loads(request.read())["texts"]
            sizes.append(len(texts))
            return httpx.Response(200, json={"model": "BAAI/bge-m3", "vectors": [[float(len(t))] for t in texts]})

        vectors = _model(handler).embed(["a"] * (MAX_TEXTS_PER_REQUEST + 1))

        self.assertEqual(sizes, [MAX_TEXTS_PER_REQUEST, 1])
        self.assertEqual(len(vectors), MAX_TEXTS_PER_REQUEST + 1)

    def test_does_not_retry_client_errors(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(413)

        with self.assertRaises(httpx.HTTPStatusError):
            _model(handler).embed(["a"])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
