from app.modules.query.infrastructure.web_search import TavilyWebSearch, build_web_search


def test_request_flag_controls_tavily_adapter(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "secret")
    monkeypatch.setenv("QUERY_WEB_SEARCH_MODE", "disabled")

    assert isinstance(build_web_search(True), TavilyWebSearch)

    monkeypatch.setenv("QUERY_WEB_SEARCH_MODE", "tavily")
    assert build_web_search(False) is None
