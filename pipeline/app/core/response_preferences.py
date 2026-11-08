from __future__ import annotations

from typing import Literal


OutputLanguage = Literal["ko", "en", "document"]
ResponseLength = Literal["concise", "balanced", "detailed"]

# 질의 답변 프롬프트의 기본 언어 지시. 답변 언어가 정해지지 않았을 때만 그대로 쓴다.
DEFAULT_ANSWER_LANGUAGE_RULE = "Answer in Korean."


def answer_language_rule(output_language: OutputLanguage | None) -> str:
    """질의 답변 프롬프트에 둘 언어 지시를 고른다.

    기본 프롬프트가 한국어를 고정하면 뒤에 붙인 언어 선호를 모델이 따르지 않는 경우가 있어,
    답변 언어가 정해지면 고정 지시 자리를 그 언어 지시로 바꾼다. 값이 없으면 지금처럼 한국어다.
    """
    if output_language == "en":
        return "Answer in English."
    if output_language == "document":
        return (
            "Answer in the dominant language of the evidence document; "
            "if it cannot be determined, use the question language."
        )
    return DEFAULT_ANSWER_LANGUAGE_RULE


def with_response_preferences(
    system_prompt: str,
    output_language: OutputLanguage | None,
    response_length: ResponseLength | None,
    *,
    document_creation: bool = False,
) -> str:
    rules: list[str] = []
    if output_language == "ko":
        rules.append("Write the response in Korean.")
    elif output_language == "en":
        rules.append("Write the response in English.")
    elif output_language == "document":
        rules.append(
            "Use the dominant language of the evidence document; "
            "if it cannot be determined, use the question language."
        )
    if response_length == "concise":
        rules.append("Be concise and include only the essential explanation.")
    elif response_length == "balanced":
        rules.append("Use a balanced level of detail.")
    elif response_length == "detailed":
        rules.append("Give a detailed explanation without omitting necessary context.")
    if not rules:
        return system_prompt
    if response_length is not None:
        rules.append("Do not omit required facts, citations, or warnings.")
    if document_creation:
        rules.insert(
            0,
            "For a new document, an explicit language in the user instruction "
            "overrides the language preference below.",
        )
    rules.append("Keep quotations, code, and proper nouns in their original language.")
    return f"{system_prompt.rstrip()}\n\n# Response Preferences\n" + "\n".join(
        f"- {rule}" for rule in rules
    )
