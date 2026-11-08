from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def read_receipt(identity: dict[str, str]) -> dict[str, Any] | None:
    base_url = os.environ.get("DOCUMENT_INTERNAL_BASE_URL", "").rstrip("/")
    token = os.environ.get("INTERNAL_CALLBACK_TOKEN", "").strip()
    if not base_url or not token:
        raise RuntimeError(
            "Set DOCUMENT_INTERNAL_BASE_URL and INTERNAL_CALLBACK_TOKEN for publication reconciliation."
        )
    request = urllib.request.Request(
        f"{base_url}/internal/wiki/publications/receipt",
        data=json.dumps(identity, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Internal-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(
            f"document-svc publication receipt lookup failed with HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("document-svc publication receipt lookup failed") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("document-svc publication receipt response must be an object")
    return payload
