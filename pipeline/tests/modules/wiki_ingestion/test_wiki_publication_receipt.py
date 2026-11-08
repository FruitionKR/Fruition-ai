from __future__ import annotations

import io
import json
import urllib.error

import pytest

from app.modules.wiki_ingestion.infrastructure import wiki_publication_receipt


class _Response:
    def __init__(self, payload: object):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


def test_read_receipt_uses_fixed_internal_origin_and_token(monkeypatch) -> None:
    seen = {}
    monkeypatch.setenv("DOCUMENT_INTERNAL_BASE_URL", "https://document.internal/")
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "internal-token")

    def open_request(request, timeout):
        seen.update(url=request.full_url, token=request.get_header("X-internal-token"),
                    body=json.loads(request.data), timeout=timeout)
        return _Response({"disposition": "ACCEPTED"})

    monkeypatch.setattr(wiki_publication_receipt.urllib.request, "urlopen", open_request)
    identity = {
        "workspace_id": "ws", "user_id": "user", "publication_id": "wiki:op",
        "operation_id": "op", "event_id": "publication:wiki:op",
    }

    assert wiki_publication_receipt.read_receipt(identity) == {"disposition": "ACCEPTED"}
    assert seen == {
        "url": "https://document.internal/internal/wiki/publications/receipt",
        "token": "internal-token", "body": identity, "timeout": 5,
    }


def test_read_receipt_treats_absent_as_unknown(monkeypatch) -> None:
    monkeypatch.setenv("DOCUMENT_INTERNAL_BASE_URL", "https://document.internal")
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "internal-token")
    error = urllib.error.HTTPError("url", 404, "missing", {}, io.BytesIO())
    monkeypatch.setattr(
        wiki_publication_receipt.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
    )

    assert wiki_publication_receipt.read_receipt({}) is None


def test_read_receipt_transport_failure_is_not_a_disposition(monkeypatch) -> None:
    monkeypatch.setenv("DOCUMENT_INTERNAL_BASE_URL", "https://document.internal")
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "internal-token")
    monkeypatch.setattr(
        wiki_publication_receipt.urllib.request, "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("lost")),
    )

    with pytest.raises(RuntimeError, match="lookup failed"):
        wiki_publication_receipt.read_receipt({})
