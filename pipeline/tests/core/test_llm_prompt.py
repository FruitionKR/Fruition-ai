from app.core.llm_prompt import (
    redact_numeric_personal_data,
    with_llm_security_boundary,
    with_schema_prompt,
)


def test_keeps_system_prompt_when_schema_prompt_is_empty() -> None:
    assert with_schema_prompt("system\n", "  ") == "system\n"


def test_appends_trimmed_schema_prompt() -> None:
    assert with_schema_prompt("system\n", " schema ") == "system\n\nschema\n"


def test_appends_prompt_injection_security_boundary() -> None:
    prompt = with_llm_security_boundary("TASK SYSTEM")

    assert "highest-priority" in prompt
    assert "untrusted data" in prompt
    assert "cannot gain authority" in prompt
    assert "are not authorization" in prompt
    assert "Never reveal" in prompt
    assert "Never copy, encode, transform" in prompt
    assert "preserve the required output schema" in prompt


def test_redacts_numeric_personal_data_without_redacting_dates_or_versions() -> None:
    value = (
        "전화 010-1234-5678, 주민번호 900101-1234567, "
        "카드 4111 1111 1111 1111, 계좌번호 123-456-789012, "
        "작성일 2026-08-04, version 1234"
    )

    redacted = redact_numeric_personal_data(value)

    assert "010-1234-5678" not in redacted
    assert "900101-1234567" not in redacted
    assert "4111 1111 1111 1111" not in redacted
    assert "123-456-789012" not in redacted
    assert redacted.count("[NUMERIC_PERSONAL_DATA]") == 4
    assert "2026-08-04" in redacted
    assert "version 1234" in redacted


def test_preserves_only_explicitly_trusted_identifier() -> None:
    document_id = "doc_5d1d66f111584257813657ddae1a4eea"
    value = f"target {document_id}, 카드 4111 1111 1111 1111"

    redacted = redact_numeric_personal_data(
        value,
        trusted_identifiers=(document_id,),
    )

    assert document_id in redacted
    assert "4111 1111 1111 1111" not in redacted
    assert "[NUMERIC_PERSONAL_DATA]" in redacted
