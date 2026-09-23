from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    parse_json_object as compatible_parse_json_object,
)
from app.modules.wiki_generation.infrastructure.json_output_parser import (
    parse_json_object,
    strip_json_fence,
)


def test_parse_json_object_repairs_fence_surrounding_text_and_trailing_comma() -> None:
    content = 'LLM output:\n```json\n{"title": "테스트", "items": [1, 2,],}\n```\nend'

    parsed = parse_json_object(content)

    assert parsed == {"title": "테스트", "items": [1, 2]}


def test_parse_json_object_uses_first_complete_object_when_model_appends_json() -> None:
    content = '{"result": "ok"}\n{"explanation": "duplicate output"}'

    assert parse_json_object(content) == {"result": "ok"}


def test_parse_json_object_skips_array_prefix_before_object() -> None:
    assert parse_json_object('[1]\n{"result": "ok"}') == {"result": "ok"}


def test_parse_json_object_stays_available_from_chat_completions_module() -> None:
    assert compatible_parse_json_object('{"ok": true}') == {"ok": True}


def test_strip_json_fence_removes_plain_json_fence() -> None:
    assert strip_json_fence("```json\n{\"a\": 1}\n```") == '{"a": 1}'
