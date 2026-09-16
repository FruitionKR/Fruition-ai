from app.modules.query.domain.entities import EvidenceSnippet, RetrievedPage, TraversalPath


class AnswerContextFormatter:
    def __init__(
        self,
        max_related_pages: int = 8,
        max_paths: int = 5,
        max_paragraph_chars: int = 900,
    ) -> None:
        self._max_related_pages = max_related_pages
        self._max_paths = max_paths
        self._max_paragraph_chars = max_paragraph_chars

    def format(
        self,
        question: str,
        related_pages: list[RetrievedPage],
        traversal_paths: list[TraversalPath],
        evidence_snippets: list[EvidenceSnippet],
        original_question: str | None = None,
        answer_mode: str | None = None,
    ) -> str:
        pages_by_id = {item.page.id: item for item in related_pages}
        lines = [
            "# User Question",
            original_question or question,
            "",
        ]
        if original_question and original_question.strip() != question.strip():
            lines.extend(
                [
                    "# Resolved Retrieval Question",
                    question,
                    "",
                ]
            )
        lines.extend(self._output_policy(answer_mode))
        lines.extend(self._answer_mode_policy(answer_mode))
        lines.extend([
            "# Related Pages By Relevance",
        ])
        for item in related_pages[: self._max_related_pages]:
            lines.extend(
                [
                    f"- id: {item.page.id}",
                    f"  type: {item.page.page_type}",
                    f"  title: {item.page.title}",
                    f"  role: {item.role}",
                    f"  score: {item.score:.3f}",
                    f"  depth: {item.depth}",
                    f"  summary: {item.page.summary}",
                ]
            )
        lines.extend(["", "# Evidence Snippets By Relevance"])
        for snippet in evidence_snippets:
            lines.extend(
                [
                    f"## Evidence {snippet.rank}",
                    "- text:",
                    self._indent(self._excerpt(snippet.text, self._max_paragraph_chars), spaces=2),
                    "",
                ]
            )

        lines.extend(["", "# Traversal Paths"])
        for path in traversal_paths[: self._max_paths]:
            path_titles = [pages_by_id[node_id].page.title if node_id in pages_by_id else node_id for node_id in path.nodes]
            lines.extend(
                [
                    f"- titles: {' -> '.join(path_titles)}",
                ]
            )
        return "\n".join(lines).strip()

    def _output_policy(self, answer_mode: str | None) -> list[str]:
        output_policy = [
            "# Assistant Output Policy",
            "- Answer in Korean.",
            "- Write only the conversational answer body that should be shown to the user.",
            "- Mark the evidence used for each supported sentence with citation markers like [1] or [2].",
            "- Use only the evidence rank numbers listed below as citation markers.",
            "- Every sentence that contains factual content from evidence must end with at least one citation marker.",
            "- Cite only evidence that directly supports the sentence.",
            "- Prefer one citation per sentence; use at most three citation markers when multiple evidence snippets are genuinely needed.",
            "- Do not attach broad citation lists to a sentence.",
            "- Do not write uncited factual sentences.",
            "- Do not expose evidence lists, scores, path ids, page ids, or page URLs in the answer body.",
            "- Never output source block references such as [doc_id:B0001]. Use only rank markers like [1].",
            "- If the evidence directly answers the question, answer naturally from that evidence.",
            "- Do not create examples, analogies, or fictional cases that are not present in the context.",
            "- If an example is needed, use only entities or cases that appear in the evidence.",
            "- Do not add information from outside the context.",
            "",
        ]
        if answer_mode in {"internal_web_augmented", "web_fallback"}:
            output_policy.extend(
                [
                    "- Do not answer as an unsupported/refusal response when web evidence supports the question.",
                    "- Do not end by saying more information is needed when web evidence already supports the answer.",
                ]
            )
        else:
            output_policy.extend(
                [
                    "- If the evidence supports only part of the question, answer that supported part first and then state specifically what the provided internal documents do not support.",
                    "- If no evidence supports any useful part of the answer, do not answer from general knowledge; say that the provided internal documents do not support the question.",
                ]
            )
        output_policy.append("")
        return output_policy

    def _answer_mode_policy(self, answer_mode: str | None) -> list[str]:
        if answer_mode == "internal_web_augmented":
            return [
                "# Internal-Web Augmented Answer Policy",
                "- Use internal Wiki evidence to identify and constrain the user's referenced subject.",
                "- Use web evidence to answer the external implementation, deployment, current, or general-knowledge part of the question.",
                "- Start with the constructive implementation answer, not with an explanation of missing internal coverage.",
                "- Do not refuse merely because the external part is absent from internal Wiki evidence when web evidence supports it.",
                "- If useful, briefly state that web evidence was used because the requested external details were outside the internal Wiki evidence.",
                "- Do not cite absence claims as the reason to stop answering when web evidence is available.",
                "- Give a constructive answer by combining the retrieved Wiki evidence with the web implementation evidence.",
                "- Clearly separate what the internal Wiki supports from what the web evidence supports when that distinction matters.",
                "- If web evidence is still insufficient for a concrete step, state that specific limitation after giving the supported general guidance.",
                "",
            ]
        if answer_mode == "web_fallback":
            return [
                "# Web Fallback Answer Policy",
                "- Use web evidence as the grounding evidence for the answer.",
                "- Do not frame the answer as unsupported merely because internal Wiki evidence was insufficient.",
                "- If useful, briefly state that web evidence was used because internal Wiki evidence was insufficient for this question.",
                "- If web evidence is insufficient, state the specific missing detail instead of giving a broad refusal.",
                "",
            ]
        return []

    def _excerpt(self, text: str, limit: int = 500) -> str:
        normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3].rstrip() + "..."

    def _indent(self, text: str, spaces: int = 2) -> str:
        prefix = " " * spaces
        return "\n".join(f"{prefix}{line}" if line else "" for line in text.splitlines())
