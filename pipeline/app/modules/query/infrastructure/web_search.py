import json
import os
import urllib.request
from typing import Any

from app.modules.query.application.ports import WebSearchPort
from app.modules.query.domain.entities import WebSearchResult


class TavilyWebSearch(WebSearchPort):
    def __init__(
        self,
        api_key: str,
        endpoint: str = "https://api.tavily.com/search",
        max_results: int = 5,
        timeout_seconds: int = 20,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._max_results = max_results
        self._timeout_seconds = timeout_seconds

    def search(self, query: str) -> list[WebSearchResult]:
        payload = {
            "api_key": self._api_key,
            "query": query,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "max_results": self._max_results,
        }
        request = urllib.request.Request(
            self._endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
        return self._results_from_response(body)

    def _results_from_response(self, body: dict[str, Any]) -> list[WebSearchResult]:
        results = []
        for item in body.get("results", []):
            title = str(item.get("title") or item.get("url") or "").strip()
            url = str(item.get("url") or "").strip()
            snippet = str(item.get("content") or item.get("snippet") or "").strip()
            if not title or not url or not snippet:
                continue
            score = item.get("score", 1.0)
            try:
                score_value = float(score)
            except (TypeError, ValueError):
                score_value = 1.0
            results.append(
                WebSearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    content=snippet,
                    score=max(0.0, min(1.0, score_value)),
                )
            )
        return results


def build_web_search(enabled: bool = False) -> WebSearchPort | None:
    if not enabled:
        return None
    api_key = os.environ.get("TAVILY_API_KEY") or os.environ.get("QUERY_WEB_SEARCH_API_KEY")
    if not api_key:
        return None
    endpoint = os.environ.get("TAVILY_ENDPOINT") or "https://api.tavily.com/search"
    max_results = _int_env("QUERY_WEB_SEARCH_MAX_RESULTS", 5)
    timeout_seconds = _int_env("QUERY_WEB_SEARCH_TIMEOUT_SECONDS", 20)
    return TavilyWebSearch(api_key, endpoint=endpoint, max_results=max_results, timeout_seconds=timeout_seconds)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
