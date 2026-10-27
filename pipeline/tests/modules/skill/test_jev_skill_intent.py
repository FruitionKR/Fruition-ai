import unittest

from app.core.jev_client import JevUnavailable
from app.modules.skill.application.author_skill import _intent_result
from app.modules.skill.domain.entities import SkillAuthoringReference
from app.modules.skill.infrastructure.jev_skill_intent_classifier import JevSkillIntentClassifier, intent_options


class FakeGenerator:
    classifier_prompt = "classify"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def classify(self, instruction, references, *, requested_description):
        self.calls.append("classify")
        return {"decision": "ambiguous", "skill_kinds": [], "reference_mode": "none", "requires_source": False}

    def generate(self, *args, **kwargs):
        self.calls.append("generate")
        return {"status": "proposal"}


class FakeJev:
    def __init__(self, pick) -> None:
        self.pick = pick
        self.questions: list[dict] = []

    def choose(self, state, questions):
        self.questions.append(questions)
        if isinstance(self.pick, Exception):
            raise self.pick
        criteria = questions["intent"]["criteria"]
        return {"intent": {"choice": next(key for key, option in criteria.items() if self.pick(option))}}


class JevSkillIntentClassifierTest(unittest.TestCase):
    def test_every_option_passes_the_intent_contract(self) -> None:
        for has_references in (False, True):
            with self.subTest(has_references=has_references):
                options = intent_options(has_references)
                for option in options:
                    _intent_result(option, has_references)
                modes = {option["reference_mode"] for option in options}
                self.assertEqual(modes, {"none", "structure-reference", "fixed-template"} if has_references else {"none"})
                # 새 문서를 만들지 않는 종류는 원문 필요 여부를 true로 고를 수 없다.
                self.assertFalse(any(
                    option["requires_source"] for option in options
                    if not {"document-create", "template"} & set(option["skill_kinds"])
                ))

    def test_returns_the_chosen_classification(self) -> None:
        jev = FakeJev(lambda o: o["skill_kinds"] == ["document-edit"] and o["requires_source"] is False)
        classifier = JevSkillIntentClassifier(jev, FakeGenerator())  # type: ignore[arg-type]

        result = classifier.classify("요약", (), requested_description=None)

        self.assertEqual(result, {"decision": "supported", "skill_kinds": ["document-edit"],
                                  "reference_mode": "none", "requires_source": False})

    def test_offers_reference_modes_only_with_references(self) -> None:
        jev = FakeJev(lambda o: o["reference_mode"] == "fixed-template")
        classifier = JevSkillIntentClassifier(jev, FakeGenerator())  # type: ignore[arg-type]
        reference = SkillAuthoringReference(id="doc-1", name="주간 보고서", markdown="# 보고서\n\n## 이번 주")

        result = classifier.classify("이 목차를 고정 양식으로 써줘", (reference,), requested_description=None)

        self.assertIn("template", result["skill_kinds"])

    def test_falls_back_to_llm_classification_and_keeps_generation(self) -> None:
        generator = FakeGenerator()
        classifier = JevSkillIntentClassifier(FakeJev(JevUnavailable("down")), generator)  # type: ignore[arg-type]

        self.assertEqual(classifier.classify("요약", (), requested_description=None)["decision"], "ambiguous")
        self.assertEqual(classifier.generate("x"), {"status": "proposal"})
        self.assertEqual(generator.calls, ["classify", "generate"])
