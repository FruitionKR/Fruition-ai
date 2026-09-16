from app.modules.query.application.ports import AnswerGeneratorPort
from app.modules.query.domain.entities import GeneratedAnswer, QueryContext


class StaticAnswerGenerator(AnswerGeneratorPort):
    def generate_answer(self, context: QueryContext) -> GeneratedAnswer:
        if not context.evidence_snippets:
            return GeneratedAnswer(content="관련 근거를 찾지 못했어요. 문서에 직접적인 답이 있는지 확인이 더 필요합니다.")

        top = context.evidence_snippets[0]
        return GeneratedAnswer(content=f"문서 기준으로는 {top.text.strip()} [{top.rank}]")
