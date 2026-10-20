"""원문 필요 여부(requires_source): 규칙·분류·명시값 우선순위와 저장·조회 계약."""

import unittest

from app.modules.skill.application.author_skill import AuthorSkillUseCase
from app.modules.skill.application.manage_skill import ManageSkillUseCase
from app.modules.skill.domain.policy import (
    can_create_document,
    inferred_requires_source,
    resolve_requires_source,
)
from app.modules.skill.infrastructure.chat_completions_skill_authoring_generator import (
    DEFAULT_CLASSIFIER_PROMPT,
)
from app.modules.skill.infrastructure.postgres_skill_repository import _row_to_version
from app.modules.skill.interfaces.http.schemas import (
    PublishAuthoredSkillRequest,
    SkillAuthoringResponse,
    SkillVersionResponse,
    UpdateSkillRequest,
)
from tests.modules.skill.test_author_skill import (
    FixedGenerator,
    FixedReferenceReader,
    InMemoryManageSkillRepository,
    draft_result,
    intent_result,
)

PLAIN_INSTRUCTIONS = "# 작성 절차\n\n- 핵심 내용을 먼저 정리한다."
SOURCE_INSTRUCTIONS = "# 작성 절차\n\n- {문서}의 핵심을 새 문서로 정리한다."


class RequiresSourcePolicyTest(unittest.TestCase):
    def test_document_placeholder_requires_source(self) -> None:
        self.assertTrue(resolve_requires_source(("document-create",), "create", SOURCE_INSTRUCTIONS))
        # template도 `{문서}`가 있으면 원문을 읽는다.
        self.assertTrue(resolve_requires_source(("template",), None, SOURCE_INSTRUCTIONS))

    def test_template_defaults_to_no_source(self) -> None:
        self.assertIs(inferred_requires_source(("template",), None, PLAIN_INSTRUCTIONS), False)
        self.assertFalse(resolve_requires_source(("template",), None, PLAIN_INSTRUCTIONS, classified=True))

    def test_undecided_rule_uses_llm_classification_then_false(self) -> None:
        self.assertIsNone(inferred_requires_source(("document-create",), "create", PLAIN_INSTRUCTIONS))
        self.assertTrue(resolve_requires_source(("document-create",), "auto", PLAIN_INSTRUCTIONS, classified=True))
        self.assertFalse(resolve_requires_source(("document-create",), "create", PLAIN_INSTRUCTIONS))

    def test_explicit_value_wins_over_rules(self) -> None:
        self.assertFalse(
            resolve_requires_source(("document-create",), "create", SOURCE_INSTRUCTIONS, explicit=False)
        )
        self.assertTrue(
            resolve_requires_source(("template",), None, PLAIN_INSTRUCTIONS, explicit=True, classified=False)
        )

    def test_non_create_skills_ignore_explicit_value(self) -> None:
        cases = (
            (("document-edit",), "edit"),
            (("document-create", "document-edit"), "edit"),
            (("folder-organize",), None),
        )
        for capabilities, result_mode in cases:
            with self.subTest(capabilities=capabilities, result_mode=result_mode):
                self.assertFalse(can_create_document(capabilities, result_mode))  # type: ignore[arg-type]
                self.assertFalse(
                    resolve_requires_source(
                        capabilities,  # type: ignore[arg-type]
                        result_mode,  # type: ignore[arg-type]
                        SOURCE_INSTRUCTIONS,
                        explicit=True,
                    )
                )

    def test_classifier_prompt_asks_for_requires_source(self) -> None:
        prompt = DEFAULT_CLASSIFIER_PROMPT.read_text(encoding="utf-8")
        self.assertIn('"requires_source"', prompt)
        self.assertIn("Set `requires_source`", prompt)


class RequiresSourceAuthoringTest(unittest.TestCase):
    def _use_case(self, generator: FixedGenerator) -> tuple[AuthorSkillUseCase, InMemoryManageSkillRepository]:
        repository = InMemoryManageSkillRepository()
        return (
            AuthorSkillUseCase(generator, FixedReferenceReader(), ManageSkillUseCase(repository)),  # type: ignore[arg-type]
            repository,
        )

    def _publish(self, use_case: AuthorSkillUseCase, instructions: str = PLAIN_INSTRUCTIONS, **options: object):
        return use_case.publish(
            workspace_id="workspace-1",
            user_id="user-1",
            scope_type="personal",
            name="new-document",
            description="고른 문서를 새 문서로 만듭니다.",
            instructions_markdown=instructions,
            expected_capabilities=options.pop("capabilities", ("document-create",)),  # type: ignore[arg-type]
            expected_allowed_tools=(),
            **options,  # type: ignore[arg-type]
        )

    def _update(self, use_case: AuthorSkillUseCase, skill_id: str, instructions: str, **options: object):
        return use_case.update(
            workspace_id="workspace-1",
            user_id="user-1",
            skill_id=skill_id,
            name="new-document",
            description="설명을 바꿨습니다.",
            instructions_markdown=instructions,
            **options,  # type: ignore[arg-type]
        )

    def test_draft_uses_classifier_requires_source_in_same_call(self) -> None:
        generator = FixedGenerator(draft_result(), {**intent_result(), "requires_source": True})
        use_case, _ = self._use_case(generator)

        result = use_case.execute(
            workspace_id="workspace-1",
            user_id="user-1",
            scope_type="personal",
            instruction="선택한 문서를 새 문서로 정리하는 스킬",
            reference_document_ids=(),
        )

        self.assertEqual(generator.classification_count, 1)
        self.assertTrue(result.proposal.requires_source)  # type: ignore[union-attr]
        self.assertTrue(SkillAuthoringResponse.from_domain(result).requires_source)

    def test_draft_without_classifier_field_defaults_to_false(self) -> None:
        use_case, _ = self._use_case(FixedGenerator(draft_result()))

        result = use_case.execute(
            workspace_id="workspace-1",
            user_id="user-1",
            scope_type="personal",
            instruction="회의 주제로 새 회의록을 작성하는 스킬",
            reference_document_ids=(),
        )

        self.assertFalse(result.proposal.requires_source)  # type: ignore[union-attr]

    def test_template_draft_ignores_classifier_true(self) -> None:
        generator = FixedGenerator(
            draft_result(),
            {**intent_result(skill_kinds=["template"]), "requires_source": True},
        )
        use_case, _ = self._use_case(generator)

        result = use_case.execute(
            workspace_id="workspace-1",
            user_id="user-1",
            scope_type="personal",
            instruction="주간 보고 양식 스킬",
            reference_document_ids=(),
        )

        self.assertFalse(result.proposal.requires_source)  # type: ignore[union-attr]

    def test_publish_stores_explicit_value_without_classifying(self) -> None:
        generator = FixedGenerator(draft_result())
        use_case, repository = self._use_case(generator)

        result = self._publish(use_case, result_mode="create", requires_source=True)

        self.assertEqual(generator.classification_count, 0)
        stored = repository.skills[result.skill.id].enabled_version  # type: ignore[union-attr]
        self.assertTrue(stored.requires_source)  # type: ignore[union-attr]
        self.assertTrue(SkillAuthoringResponse.from_domain(result).requires_source)

    def test_publish_without_explicit_value_uses_rules(self) -> None:
        use_case, _ = self._use_case(FixedGenerator(draft_result()))

        with_placeholder = self._publish(use_case, SOURCE_INSTRUCTIONS, result_mode="create")
        plain = self._publish(use_case, result_mode="create")

        self.assertTrue(with_placeholder.skill.enabled_version.requires_source)  # type: ignore[union-attr]
        self.assertFalse(plain.skill.enabled_version.requires_source)  # type: ignore[union-attr]

    def test_publish_ignores_explicit_value_for_edit_result_mode(self) -> None:
        use_case, _ = self._use_case(FixedGenerator(draft_result()))

        result = self._publish(
            use_case,
            SOURCE_INSTRUCTIONS,
            capabilities=("document-create", "document-edit"),
            result_mode="edit",
            requires_source=True,
        )

        self.assertFalse(result.skill.enabled_version.requires_source)  # type: ignore[union-attr]

    def test_update_with_same_instructions_keeps_previous_value(self) -> None:
        generator = FixedGenerator(draft_result())
        use_case, _ = self._use_case(generator)
        created = self._publish(use_case, result_mode="create", requires_source=True)

        kept = self._update(use_case, created.skill.id, PLAIN_INSTRUCTIONS)  # type: ignore[union-attr]
        changed = self._update(
            use_case, created.skill.id, PLAIN_INSTRUCTIONS, requires_source=False  # type: ignore[union-attr]
        )

        self.assertEqual(generator.classification_count, 0)
        self.assertTrue(kept.skill.enabled_version.requires_source)  # type: ignore[union-attr]
        self.assertFalse(changed.skill.enabled_version.requires_source)  # type: ignore[union-attr]

    def test_update_with_changed_instructions_uses_reclassified_value(self) -> None:
        generator = FixedGenerator(draft_result(), {**intent_result(), "requires_source": True})
        use_case, _ = self._use_case(generator)
        created = self._publish(use_case, result_mode="create")

        result = self._update(use_case, created.skill.id, "# 작성 절차\n\n- 고른 자료를 묶어 보고서로 만든다.")  # type: ignore[union-attr]

        self.assertEqual(generator.classification_count, 1)
        self.assertTrue(result.skill.enabled_version.requires_source)  # type: ignore[union-attr]

    def test_http_requests_accept_optional_requires_source(self) -> None:
        base = {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "workspace_id": "workspace-1",
            "user_id": "user-1",
            "name": "new-document",
            "description": "설명",
            "instructions_markdown": PLAIN_INSTRUCTIONS,
        }
        publish = PublishAuthoredSkillRequest.model_validate(
            {**base, "scope_type": "personal", "capabilities": ["document-create"], "requires_source": True}
        )
        update = UpdateSkillRequest.model_validate(base)

        self.assertTrue(publish.requires_source)
        self.assertIsNone(update.requires_source)


class RequiresSourceStoredRowTest(unittest.TestCase):
    def _row(self, *, instructions: str, capabilities: list[str], requires_source: bool | None, result_mode: str | None):
        return {
            "ev_id": "version-1",
            "ev_skill_id": "skill-1",
            "ev_version": 1,
            "ev_name": "new-document",
            "ev_description": "설명",
            "ev_instructions_markdown": instructions,
            "ev_capabilities": capabilities,
            "ev_allowed_tools": [],
            "ev_targets": [],
            "ev_result_mode": result_mode,
            "ev_requires_source": requires_source,
            "ev_safety_result": {},
            "ev_status": "published",
            "ev_created_by": "user-1",
            "ev_created_at": None,
            "ev_published_at": None,
        }

    def test_legacy_rows_compute_requires_source_on_read(self) -> None:
        # 이전에는 명시한 생성 Skill이 고른 문서를 항상 원문으로 썼다. `{문서}`가 없어도 그 동작을 지킨다.
        cases = (
            (SOURCE_INSTRUCTIONS, ["document-create"], "create", True),
            (PLAIN_INSTRUCTIONS, ["document-create"], "create", True),
            (PLAIN_INSTRUCTIONS, ["document-create", "document-edit"], "auto", True),
            (PLAIN_INSTRUCTIONS, ["document-create"], None, True),
            (PLAIN_INSTRUCTIONS, ["template"], None, False),
            (SOURCE_INSTRUCTIONS, ["document-create", "template"], None, False),
            (SOURCE_INSTRUCTIONS, ["document-create", "document-edit"], "edit", False),
            (SOURCE_INSTRUCTIONS, ["document-edit"], "edit", False),
            (PLAIN_INSTRUCTIONS, ["folder-organize"], None, False),
        )
        for instructions, capabilities, result_mode, expected in cases:
            with self.subTest(capabilities=capabilities, instructions=instructions):
                version = _row_to_version(
                    self._row(
                        instructions=instructions,
                        capabilities=capabilities,
                        requires_source=None,
                        result_mode=result_mode,
                    ),
                    "ev",
                )
                self.assertIs(version.requires_source, expected)  # type: ignore[union-attr]
                self.assertIs(SkillVersionResponse.from_domain(version).requires_source, expected)  # type: ignore[arg-type]

    def test_stored_value_wins_over_rules(self) -> None:
        version = _row_to_version(
            self._row(
                instructions=SOURCE_INSTRUCTIONS,
                capabilities=["document-create"],
                requires_source=False,
                result_mode="create",
            ),
            "ev",
        )

        self.assertFalse(version.requires_source)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
