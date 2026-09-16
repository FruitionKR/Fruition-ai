import unittest

from app.modules.wiki_schema.application.build_schema_preview import build_schema_preview
from app.modules.wiki_schema.domain.entities import SchemaFilterResult, SchemaFragments, SchemaIssue


class SchemaPreviewTest(unittest.TestCase):
    def test_renders_applied_blocked_and_unclear_sections(self) -> None:
        result = SchemaFilterResult(
            fragments=SchemaFragments(
                global_markdown="- 답변은 한국어 기술 문서 문체를 따른다.",
                edit_markdown="- 수식과 단위는 사용자의 명시적 요청 없이 변경하지 않는다.",
            ),
            issues=[
                SchemaIssue(
                    severity="blocked",
                    category="policy_weakening",
                    text="출처 없이 단정적으로 답한다.",
                    reason="근거 또는 불확실성 정책을 약화하는 요청입니다.",
                ),
                SchemaIssue(
                    severity="unclear",
                    category="unclear_preference",
                    text="중요한 내용은 자세히 설명한다.",
                    reason="사용자 확인이 필요한 모호한 설정입니다.",
                ),
            ],
        )

        preview = build_schema_preview(result)

        self.assertIn("# 적용될 Schema 설정", preview)
        self.assertIn("## 공통 작성 기준", preview)
        self.assertIn("## 문서 편집 기준", preview)
        self.assertIn("## 적용되지 않은 설정", preview)
        self.assertIn("## 확인 필요한 설정", preview)
        self.assertIn("출처 없이 단정적으로 답한다.", preview)
        self.assertIn("중요한 내용은 자세히 설명한다.", preview)

    def test_redacts_secret_like_values_in_preview(self) -> None:
        result = SchemaFilterResult(
            fragments=SchemaFragments(
                global_markdown="- token: sk-testsecret123456 값을 사용한다.",
            ),
            issues=[
                SchemaIssue(
                    severity="blocked",
                    category="secret",
                    text="api_key=sk-secret987654321",
                    reason="민감정보를 저장하거나 출력하려는 요청입니다.",
                )
            ],
        )

        preview = build_schema_preview(result)

        self.assertIn("[REDACTED_SECRET]", preview)
        self.assertNotIn("sk-testsecret123456", preview)
        self.assertNotIn("sk-secret987654321", preview)

    def test_renders_empty_applied_state(self) -> None:
        preview = build_schema_preview(SchemaFilterResult(fragments=SchemaFragments()))

        self.assertIn("적용될 설정이 없습니다.", preview)


if __name__ == "__main__":
    unittest.main()
