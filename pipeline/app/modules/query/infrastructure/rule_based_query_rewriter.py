import re

from app.modules.query.application.ports import QueryRewritePort
from app.modules.query.domain.entities import QueryRewrite


class RuleBasedQueryRewriter(QueryRewritePort):
    def __init__(self, max_keywords: int = 8) -> None:
        self._max_keywords = max_keywords
        self._stopwords = {
            "이거",
            "그거",
            "저거",
            "뭐야",
            "무엇",
            "어떻게",
            "왜",
            "좀",
            "관련",
            "차이",
            "설명",
            "알려줘",
            "있어",
            "있는",
            "되는",
            "하는",
            "그리고",
            "근데",
            "the",
            "a",
            "an",
            "and",
            "or",
            "of",
            "to",
            "in",
            "is",
            "are",
        }

    def rewrite(self, question: str) -> QueryRewrite:
        tokens = [self._normalize_token(token) for token in self._tokens(question)]
        keywords = []
        for token in tokens:
            if token in self._stopwords or len(token) <= 1:
                continue
            if token not in keywords:
                keywords.append(token)
            if len(keywords) >= self._max_keywords:
                break
        retrieval_query = " ".join(keywords) if keywords else question
        return QueryRewrite(
            original_question=question,
            retrieval_query=retrieval_query,
            keywords=keywords,
        )

    def _tokens(self, text: str) -> list[str]:
        return re.findall(r"[A-Za-z0-9가-힣_.-]+", text.lower())

    def _normalize_token(self, token: str) -> str:
        if re.search(r"[가-힣]", token):
            for suffix in ["에서는", "으로부터", "로부터", "에게서", "한테서", "에게", "한테", "으로", "로", "이랑", "랑", "이나", "나", "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "와", "과"]:
                if token.endswith(suffix):
                    return token[: -len(suffix)] if len(token) > len(suffix) + 1 else token
        return token
