import unittest

from markdown_edit_gfm_lab import CASES, accepted_route_goals, evaluate_replacement


class MarkdownEditGfmLabTest(unittest.TestCase):
    def test_accepts_valid_replacements(self) -> None:
        replacements = {
            "emphasis_quote": "**핵심: 배포 전 테스트를 완료한다.**\n\n> 주의: 운영 DB를 직접 수정하지 않는다.",
            "nested_list": (
                "- 환경 변수\n"
                "  - API URL\n"
                "  - timeout\n"
                "- 로그 설정\n"
                "  - level\n"
                "  - output"
            ),
            "preserve_code_link": (
                "아래 명령으로 설치한다.\n\n"
                "```bash\nnpm install\nnpm run dev\n```\n\n"
                "[설치 가이드](https://example.com/install)를 참고한다."
            ),
            "footnote": "배포 전 smoke test가 필요하다.[^1]\n\n[^1]: 운영 환경의 핵심 경로만 확인한다.",
            "table": "| 담당자 | 상태 |\n| --- | --- |\n| 민수 | 진행 중 |\n| 지수 | 완료 |",
            "task_list": "- [ ] API 문서를 검토한다.\n- [ ] 배포 전에 smoke test를 실행한다.",
            "heading_inline_styles": (
                "# 릴리스 정책\n\n"
                "**중요: 회귀 테스트**\n\n"
                "*선택: 문서 미리보기*\n\n"
                "~~폐기: 수동 배포~~"
            ),
            "numbered_list": "1. 의존성을 설치한다.\n2. 테스트를 실행한다.\n3. 결과를 검토한다.",
            "inline_code": "설정 키는 `DATABASE_URL`이고 기본 port 값은 `3000`이다.",
            "preserve_image_divider": (
                "아키텍처 그림을 확인할 수 있다.\n\n"
                "![아키텍처](https://example.com/architecture.png)\n\n"
                "---\n\n"
                "다음 절에서는 배포를 설명한다."
            ),
            "frontmatter": "---\ntitle: 배포 가이드\nstatus: draft\n---\n\n배포 전에 테스트해야 한다.",
            "meeting_notes": (
                "## 논의 사항\n- 캐시 정책을 논의했다.\n\n"
                "## 결정 사항\n- TTL은 10분으로 결정했다.\n\n"
                "## 다음 작업\n- 지수는 금요일까지 부하 테스트를 진행한다."
            ),
            "translate": "Back up the production database before deployment.",
            "shorten": "신규 사용자를 위해 시작 안내를 합치고 용어를 통일해야 한다.",
            "translate_structured": (
                "# 배포 가이드\n\n"
                "[설치 가이드](https://example.com/install)를 읽는다.[^1]\n\n"
                "```bash\nnpm install\n```\n\n"
                "| 환경 | 상태 |\n| --- | --- |\n| 운영 | 준비됨 |\n\n"
                "[^1]: 공식 문서"
            ),
            "shorten_anchors": "API cache TTL은 10분이며 운영자가 확인해야 한다.",
            "math": "$$\nE = mc^2\n$$",
            "mermaid": "```mermaid\nflowchart LR\n  요청 --> 검토 --> 적용\n```",
            "mixed_preservation": (
                "---\ntitle: 운영 점검\n---\n\n"
                "# 배포\n\n[Runbook](https://example.com/runbook)을 확인한다.[^1]\n\n"
                "```bash\n./deploy.sh\n```\n\n"
                "| 환경 | 상태 |\n| --- | --- |\n| prod | ready |\n\n"
                "[^1]: 승인 후 실행한다."
            ),
        }

        for case in CASES:
            with self.subTest(case=case.id):
                self.assertEqual(evaluate_replacement(case, replacements[case.id]), [])

    def test_reports_contract_failures(self) -> None:
        cases_by_id = {case.id: case for case in CASES}

        self.assertIn("들여쓰기된 하위 bullet이 없음", evaluate_replacement(cases_by_id["nested_list"], "- 환경 변수"))
        self.assertIn(
            "일반 bullet 요청에 task list를 사용함",
            evaluate_replacement(cases_by_id["nested_list"], "- 환경 변수\n  - [ ] API URL\n  - timeout\n- 로그 설정\n  - level\n  - output"),
        )
        self.assertIn("원본 bash code fence가 보존되지 않음", evaluate_replacement(cases_by_id["preserve_code_link"], "설명"))
        self.assertIn("각주 definition이 보존되지 않음", evaluate_replacement(cases_by_id["footnote"], "smoke test[^1] 운영 환경 핵심 경로"))
        self.assertIn("GFM 표 형태가 아님", evaluate_replacement(cases_by_id["table"], "민수 진행 중, 지수 완료"))
        self.assertIn("모든 작업이 미완료 task list 항목이 아님", evaluate_replacement(cases_by_id["task_list"], "- API 문서\n- smoke test"))
        self.assertIn("수식을 code fence 안에 넣음", evaluate_replacement(cases_by_id["math"], "```\n$$ E = mc^2 $$\n```"))
        self.assertIn(
            "축약 요청에 list marker를 추가함",
            evaluate_replacement(cases_by_id["shorten"], "- 신규 사용자를 위해 시작 안내와 용어를 통일한다."),
        )

    def test_accepts_only_format_specific_route_goals(self) -> None:
        cases_by_id = {case.id: case for case in CASES}

        self.assertEqual(accepted_route_goals(cases_by_id["nested_list"]), ("bullet_list",))
        self.assertEqual(accepted_route_goals(cases_by_id["task_list"]), ("checklist",))
        self.assertEqual(accepted_route_goals(cases_by_id["preserve_code_link"]), ("cleanup", "style_change"))
        self.assertEqual(accepted_route_goals(cases_by_id["math"]), ("convert_format",))
        self.assertEqual(accepted_route_goals(cases_by_id["translate"]), ("translate",))


if __name__ == "__main__":
    unittest.main()
