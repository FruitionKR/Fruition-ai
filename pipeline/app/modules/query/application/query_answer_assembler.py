import re
from collections.abc import Callable
from dataclasses import replace

from app.core.ai_markdown_sanitizer import sanitize_ai_markdown
from app.modules.query.application.extract_answer_citations import ExtractAnswerCitationsUseCase
from app.modules.query.application.ports import AnswerGeneratorPort
from app.modules.query.application.source_references import remove_block_refs
from app.modules.query.domain.entities import EvidenceSnippet, GeneratedAnswer, QueryContext

# 펜스는 같은 문자로 같은 길이 이상 닫혀야 끝나고, 닫히지 않으면 답변 끝까지 코드다.
# 목록 안 코드 블록은 4칸 이상 들여쓰므로 펜스 앞 들여쓰기는 제한하지 않는다.
# 인라인 코드는 문단(빈 줄)을 넘지 않는다. 짝 없는 백틱이 뒤 문단 인용까지 삼키지 않게 한다.
_CODE_PATTERN = re.compile(
    r"^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*(?:\n.*?(?:^[ \t]*(?P=fence)[`~]*[ \t]*$|\Z)|\Z)"
    r"|(?P<tick>`+)(?!`)(?:(?!\n[ \t]*\n).)+?(?<!`)(?P=tick)(?!`)",
    re.MULTILINE | re.DOTALL,
)


def _sub_outside_code(pattern: str, replacement: str | Callable[[re.Match[str]], str], content: str) -> str:
    parts: list[str] = []
    last = 0
    for match in _CODE_PATTERN.finditer(content):
        parts.append(re.sub(pattern, replacement, content[last : match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(re.sub(pattern, replacement, content[last:]))
    return "".join(parts)


class QueryAnswerAssembler:
    def __init__(
        self,
        answer_generator: AnswerGeneratorPort,
        extract_answer_citations: ExtractAnswerCitationsUseCase | None = None,
    ) -> None:
        self._answer_generator = answer_generator
        self._extract_answer_citations = extract_answer_citations or ExtractAnswerCitationsUseCase()

    def generate_supported_answer(self, query_context: QueryContext) -> tuple[GeneratedAnswer, list[EvidenceSnippet]]:
        answer = self._answer_generator.generate_answer(query_context)
        answer = GeneratedAnswer(
            content=self._extract_answer_citations.ensure_sentence_citations(
                answer.content,
                query_context.evidence_snippets[0].rank if query_context.evidence_snippets else None,
            )
        )
        return self.renumber_used_evidence(answer, query_context.evidence_snippets)

    def renumber_used_evidence(
        self,
        answer: GeneratedAnswer,
        evidence_snippets: list[EvidenceSnippet],
    ) -> tuple[GeneratedAnswer, list[EvidenceSnippet]]:
        snippets_by_rank = {snippet.rank: snippet for snippet in evidence_snippets}
        key_to_new_rank: dict[tuple[str, tuple[str, ...]], int] = {}
        snippets_by_new_rank: dict[int, EvidenceSnippet] = {}

        def next_rank(old_rank: int) -> int:
            snippet = snippets_by_rank[old_rank]
            key = (snippet.source_document_id, tuple(sorted(snippet.source_block_ids)))
            if key not in key_to_new_rank:
                new_rank = len(key_to_new_rank) + 1
                key_to_new_rank[key] = new_rank
                snippets_by_new_rank[new_rank] = replace(snippet, rank=new_rank)
            return key_to_new_rank[key]

        def replace_marker(match: re.Match[str]) -> str:
            ranks = [int(value) for value in re.findall(r"\d+", match.group(1))]
            remapped = list(
                dict.fromkeys(
                    str(next_rank(rank))
                    for rank in ranks
                    if rank in snippets_by_rank
                )
            )
            if not remapped:
                return ""
            return f"[{', '.join(remapped)}]"

        # 답변 본문의 인용 계약은 숫자 rank다. 위키 인라인 앵커([doc:B0001])가 새어 나오면
        # 사용자에게 노출되고 저장까지 되어 다음 턴에 LLM이 형식을 모방한다. 모든 답변 경로가
        # 이 함수를 지나므로 여기서 한 번 걷어낸다.
        # strip=False: 참조가 없는 답변은 한 글자도 건드리지 않는다. 들여쓰기 코드블록으로
        # 시작하는 답변에서 앞 공백이 잘리면 마크다운이 깨진다.
        content = remove_block_refs(answer.content, strip=False)
        # 근거 원문·웹 검색 결과에 숨은 지시로 외부 이미지·링크가 들어오면 열람만으로 내용이 새어 나간다.
        content = sanitize_ai_markdown(content)
        # 코드 블록·인라인 코드 안의 숫자 배열(예: [5, 2, 4])은 인용이 아니므로 건너뛴다.
        content = _sub_outside_code(r"\[((?:\d+)(?:\s*,\s*\d+)*)\]", replace_marker, content)
        content = _sub_outside_code(r"(\[\d+(?:,\s*\d+)*\])(?:\1)+", r"\1", content)
        used_snippets = list(snippets_by_new_rank.values())
        if not used_snippets:
            return GeneratedAnswer(content=content), []
        return GeneratedAnswer(content=content), used_snippets
