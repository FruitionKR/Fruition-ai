import pytest

from app.core.ai_markdown_sanitizer import external_link_spans, external_urls, sanitize_ai_markdown


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("요약 ![x](https://attacker.example/x.png?q=비밀)", "요약 외부 이미지(attacker.example)"),
        ('![x](<https://a.example/x y.png> "제목")', "외부 이미지(a.example)"),
        ("![x](//cdn.example/x.png)", "외부 이미지(cdn.example)"),
        ("[가이드](https://docs.example/path?q=1)", "가이드 (docs.example)"),
        ("[**굵게**](HTTPS://Docs.Example)", "**굵게** (docs.example)"),
        ("[](https://e.example)", "e.example"),
        ("[메일](mailto:a@b.example)", "메일"),
        ("주소 <https://auto.example/a> 끝", "주소 auto.example 끝"),
        ("[![i](https://img.example/a.png)](https://link.example)", "외부 이미지(img.example) (link.example)"),
    ],
)
def test_neutralizes_external_images_and_links(markdown: str, expected: str) -> None:
    assert sanitize_ai_markdown(markdown) == expected


@pytest.mark.parametrize(
    "markdown",
    [
        "본문 [[slug|제목]] [doc:B0001] [1, 2]",
        "[상대](./other.md) [앵커](#section) ![로컬](images/a.png)",
        "![내장](data:image/png;base64,iVBORw0KGgo=)",
        "평문 URL https://plain.example 은 링크 문법이 아니다.",
        "인라인 `![x](https://e.example/a.png)` 코드",
        "```\n![x](https://e.example/a.png)\n```\n",
        "    [x](https://e.example)\n",
        "",
    ],
)
def test_keeps_markdown_without_external_links(markdown: str) -> None:
    assert sanitize_ai_markdown(markdown) == markdown


def test_keeps_lines_outside_changed_blocks() -> None:
    markdown = (
        "# 제목\n\n"
        "| a | b |\n|---|---|\n| [x](https://e.example) | `![c](https://c.example)` |\n\n"
        "> 인용 ![i](https://i.example/a.png)\n> 이어짐\n\n"
        "```md\n[코드](https://code.example)\n```\r\n"
        "- 항목 [z](https://z.example)\n"
    )

    assert sanitize_ai_markdown(markdown) == (
        "# 제목\n\n"
        "| a | b |\n|---|---|\n| x (e.example) | `![c](https://c.example)` |\n\n"
        "> 인용 외부 이미지(i.example)\n> 이어짐\n\n"
        "```md\n[코드](https://code.example)\n```\r\n"
        "- 항목 z (z.example)\n"
    )


def test_escapes_reference_links_that_syntax_replacement_cannot_reach() -> None:
    result = sanitize_ai_markdown("참조 [링크][1] 끝\n\n[1]: https://ref.example\n")

    assert result == "참조 \\[링크\\]\\[1\\] 끝\n\n[1]: https://ref.example\n"
    assert external_urls(result) == frozenset()


def test_keeps_urls_from_original_markdown() -> None:
    original = "원문 [회사](https://company.example) ![로고](https://company.example/logo.png)"
    edited = original + "\n\n추가 [새 링크](https://attacker.example)"

    assert sanitize_ai_markdown(edited, keep_urls=external_urls(original)) == (
        original + "\n\n추가 새 링크 (attacker.example)"
    )


def test_result_never_contains_external_links() -> None:
    markdown = "[a](https://x.example) [b](https://x.example) ![c](https://y.example) <https://z.example>\n"

    assert external_urls(sanitize_ai_markdown(markdown)) == frozenset()


def test_external_link_spans_point_to_link_syntax() -> None:
    markdown = "# 제목\n\n본문 [a](https://x.example) `[c](https://c.example)` [상대](./a.md)\n\n참조 [r][1]\n\n[1]: https://ref.example\n"

    spans = external_link_spans(markdown)

    assert [markdown[start:end] for start, end in spans] == [
        "[a](https://x.example)",
        "참조 [r][1]\n",
    ]
