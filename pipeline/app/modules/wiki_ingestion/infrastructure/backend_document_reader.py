from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote


def read_document(document_id: str) -> dict[str, Any] | None:
    base_url = os.environ.get("DOCUMENT_INTERNAL_BASE_URL", "").rstrip("/")
    token = os.environ.get("INTERNAL_CALLBACK_TOKEN", "").strip()
    if not base_url or not token:
        raise RuntimeError(
            "Set DOCUMENT_INTERNAL_BASE_URL and INTERNAL_CALLBACK_TOKEN before ingest."
        )
    request = urllib.request.Request(
        f"{base_url}/internal/documents/{quote(document_id, safe='')}/pipeline-source",
        headers={"X-Internal-Token": token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(
            f"document-svc pipeline source lookup failed with HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("document-svc pipeline source lookup failed") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("document-svc pipeline source response must be an object")
    return payload


def read_contributions(
    page_ids: list[str],
    workspace_id: str,
) -> list[dict[str, Any]]:
    if not page_ids:
        return []
    base_url = os.environ.get("DOCUMENT_INTERNAL_BASE_URL", "").rstrip("/")
    token = os.environ.get("INTERNAL_CALLBACK_TOKEN", "").strip()
    if not base_url or not token:
        raise RuntimeError(
            "Set DOCUMENT_INTERNAL_BASE_URL and INTERNAL_CALLBACK_TOKEN before ingest."
        )
    request = urllib.request.Request(
        f"{base_url}/internal/wiki/contributions",
        data=json.dumps(
            {"page_ids": page_ids, "workspace_id": workspace_id}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Internal-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"document-svc contribution lookup failed with HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("document-svc contribution lookup failed") from exc
    if not isinstance(payload, list):
        raise RuntimeError("document-svc contribution response must be a list")
    return [item for item in payload if isinstance(item, dict)]
